"""
connectors.py — per-exchange live order book + trade connectors.

Each connector maintains its OWN synced local book for BTC perpetual and calls
back into a shared sink with NORMALIZED updates, so the aggregator never has to
know exchange-specific message formats.

Normalized callbacks the sink must implement:
    on_book(exchange, bids: dict[price->qty], asks: dict[price->qty])
        -> full current book for that exchange (we push the whole local book each
           tick; simpler and robust for aggregation across 3 venues)
    on_trade(exchange, price, qty, is_sell_aggressor, ts)

Design notes:
  - All three use snapshot-then-delta. A level with qty 0 means "remove".
  - Binance futures: REST snapshot + @depth diff stream (lastUpdateId continuity).
  - OKX v5: `books` channel — first msg = snapshot, rest = deltas. Text ping/pong.
  - Bybit v5 linear: `orderbook.50.<sym>` — type=snapshot resets, type=delta updates.
  - Each connector is resilient: auto-reconnect with re-sync on any error.
"""

import asyncio
import json
import time

import requests
import websockets
from sortedcontainers import SortedDict


class BaseConnector:
    name = "base"

    def __init__(self, symbol, sink):
        self.symbol = symbol
        self.sink = sink
        self.bids = SortedDict()
        self.asks = SortedDict()
        self._running = True

    def book_dicts(self):
        return dict(self.bids), dict(self.asks)

    def _emit_book(self):
        self.sink.on_book(self.name, self.bids, self.asks)

    def _set(self, book, price, qty):
        if qty == 0:
            book.pop(price, None)
        else:
            book[price] = qty

    async def run(self):
        delay = 2
        while self._running:
            try:
                await self._stream()
                delay = 2
            except Exception as e:
                self.sink.on_status(self.name, f"reco: {e}")
                self.bids.clear(); self.asks.clear()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)   # backoff exponentiel : 2,4,8,16,30s

    def stop(self):
        self._running = False


# --------------------------------------------------------------------------
class BinanceFutures(BaseConnector):
    name = "binance"
    REST = "https://fapi.binance.com/fapi/v1/depth?symbol={sym}&limit=1000"
    WS = "wss://fstream.binance.com/stream?streams={s}"

    async def _stream(self):
        sym = self.symbol.lower()
        streams = f"{sym}@depth@100ms/{sym}@aggTrade"
        url = self.WS.format(s=streams)
        async with websockets.connect(url, max_queue=None) as ws:
            self.bids.clear(); self.asks.clear()
            last_id = 0
            buffer = []
            synced = False
            # snapshot
            loop = asyncio.get_event_loop()
            snap = await loop.run_in_executor(
                None, lambda: requests.get(self.REST.format(sym=self.symbol.upper()), timeout=10).json())
            last_id = snap["lastUpdateId"]
            for p, q in snap["bids"]:
                self._set(self.bids, float(p), float(q))
            for p, q in snap["asks"]:
                self._set(self.asks, float(p), float(q))
            synced = True
            self.sink.on_status(self.name, "ok")
            async for raw in ws:
                if not self._running:
                    break
                m = json.loads(raw)
                st = m.get("stream", ""); d = m.get("data", {})
                if "@depth" in st:
                    if d.get("u", 0) <= last_id:
                        continue
                    for p, q in d.get("b", []):
                        self._set(self.bids, float(p), float(q))
                    for p, q in d.get("a", []):
                        self._set(self.asks, float(p), float(q))
                    last_id = d["u"]
                    self._emit_book()
                elif "@aggTrade" in st:
                    self.sink.on_trade(self.name, float(d["p"]), float(d["q"]),
                                       bool(d["m"]), d["T"] / 1000.0)


# --------------------------------------------------------------------------
class OKXSwap(BaseConnector):
    name = "okx"
    WS = "wss://ws.okx.com:8443/ws/v5/public"

    # OKX exprime le carnet et les trades des SWAP en CONTRATS, pas en crypto.
    # Pour BTC-USDT-SWAP : 1 contrat = 0.01 BTC. On convertit tout en BTC pour
    # que l'agrégation soit cohérente avec Binance/Bybit/Hyperliquid (en BTC).
    CT_VAL = 0.01

    def __init__(self, symbol, sink):
        super().__init__(symbol, sink)
        # dérive l'instId OKX du symbole : BTCUSDT -> BTC-USDT-SWAP
        self.inst = symbol.upper().replace("USDT", "-USDT-SWAP")

    async def _stream(self):
        async with websockets.connect(self.WS, max_queue=None) as ws:
            self.bids.clear(); self.asks.clear()
            sub = {"op": "subscribe", "args": [
                {"channel": "books", "instId": self.inst},
                {"channel": "trades", "instId": self.inst},
            ]}
            await ws.send(json.dumps(sub))
            self.sink.on_status(self.name, "ok")
            last_ping = time.time()
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    await ws.send("ping")  # OKX text heartbeat
                    continue
                if raw == "pong":
                    continue
                m = json.loads(raw)
                if m.get("event"):  # subscribe ack / error
                    continue
                ch = m.get("arg", {}).get("channel")
                data = m.get("data", [])
                if ch == "books":
                    action = m.get("action", "snapshot")
                    for d in data:
                        if action == "snapshot":
                            self.bids.clear(); self.asks.clear()
                        for p, q, *_ in d.get("bids", []):
                            self._set(self.bids, float(p), float(q) * self.CT_VAL)
                        for p, q, *_ in d.get("asks", []):
                            self._set(self.asks, float(p), float(q) * self.CT_VAL)
                    self._emit_book()
                elif ch == "trades":
                    for d in data:
                        # side = "buy"/"sell" is the AGGRESSOR side on OKX
                        is_sell = d.get("side") == "sell"
                        self.sink.on_trade(self.name, float(d["px"]),
                                           float(d["sz"]) * self.CT_VAL,
                                           is_sell, float(d["ts"]) / 1000.0)
                # periodic ping
                if time.time() - last_ping > 20:
                    await ws.send("ping"); last_ping = time.time()


# --------------------------------------------------------------------------
class BybitLinear(BaseConnector):
    name = "bybit"
    WS = "wss://stream.bybit.com/v5/public/linear"

    async def _stream(self):
        async with websockets.connect(self.WS, max_queue=None) as ws:
            self.bids.clear(); self.asks.clear()
            sub = {"op": "subscribe", "args": [
                f"orderbook.200.{self.symbol.upper()}",   # 200 niveaux (max supporté en linear)
                f"publicTrade.{self.symbol.upper()}",
            ]}
            await ws.send(json.dumps(sub))
            self.sink.on_status(self.name, "ok")
            last_ping = time.time()
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    await ws.send(json.dumps({"op": "ping"})); last_ping = time.time()
                    continue
                m = json.loads(raw)
                topic = m.get("topic", "")
                if topic.startswith("orderbook"):
                    typ = m.get("type")
                    d = m.get("data", {})
                    if typ == "snapshot":
                        self.bids.clear(); self.asks.clear()
                    for p, q in d.get("b", []):
                        self._set(self.bids, float(p), float(q))
                    for p, q in d.get("a", []):
                        self._set(self.asks, float(p), float(q))
                    self._emit_book()
                elif topic.startswith("publicTrade"):
                    for d in m.get("data", []):
                        # S = "Buy"/"Sell" is the AGGRESSOR side
                        is_sell = d.get("S") == "Sell"
                        self.sink.on_trade(self.name, float(d["p"]), float(d["v"]),
                                           is_sell, float(d["T"]) / 1000.0)
                if time.time() - last_ping > 20:
                    await ws.send(json.dumps({"op": "ping"})); last_ping = time.time()


# --------------------------------------------------------------------------
class BinancePositioning:
    """Flux POSITIONNEMENT (données publiques gratuites) :
      - REST Binance openInterest + premiumIndex -> OI + funding (toutes les 15s)
      - WS Bybit allLiquidation -> liquidations en temps réel
        (les streams d'événements Binance sont bloqués sur certains réseaux/régions,
         Bybit passe partout)
    Callbacks sink : on_funding(rate, next_ts), on_oi(oi, ts),
                     on_liquidation(side, price, qty, ts)."""
    name = "binance_pos"
    LIQ_WS = "wss://stream.bybit.com/v5/public/linear"
    OI_REST = "https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}"
    FUND_REST = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"

    def __init__(self, symbol, sink):
        self.symbol = symbol
        self.sink = sink
        self._running = True

    def stop(self):
        self._running = False

    async def run(self):
        delay = 2
        while self._running:
            try:
                await self._stream()
                delay = 2
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _poll_rest(self):
        """OI + funding via REST toutes les 15s (fiable, léger)."""
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                r = await loop.run_in_executor(None, lambda: requests.get(
                    self.OI_REST.format(sym=self.symbol.upper()), timeout=10).json())
                oi = float(r.get("openInterest", 0) or 0)
                if oi:
                    self.sink.on_oi(oi, time.time())
            except Exception:
                pass
            try:
                r = await loop.run_in_executor(None, lambda: requests.get(
                    self.FUND_REST.format(sym=self.symbol.upper()), timeout=10).json())
                rate = float(r.get("lastFundingRate", 0) or 0)
                next_ts = float(r.get("nextFundingTime", 0) or 0) / 1000.0
                self.sink.on_funding(rate, next_ts)
            except Exception:
                pass
            await asyncio.sleep(15)

    async def _stream(self):
        poll = asyncio.create_task(self._poll_rest())
        try:
            async with websockets.connect(self.LIQ_WS, max_queue=None) as ws:
                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [f"allLiquidation.{self.symbol.upper()}"]}))
                last_ping = time.time()
                while self._running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"op": "ping"}))
                        last_ping = time.time()
                        continue
                    m = json.loads(raw)
                    if m.get("topic", "").startswith("allLiquidation"):
                        for d in m.get("data", []):
                            # Doc Bybit : S = "Position side". S="Buy" -> une position
                            # LONG a été liquidée ; S="Sell" -> une SHORT a été liquidée.
                            side = "long" if d.get("S") == "Buy" else "short"
                            price = float(d.get("p", 0) or 0)
                            qty = float(d.get("v", 0) or 0)
                            ts = float(d.get("T", 0) or 0) / 1000.0
                            if price and qty:
                                self.sink.on_liquidation(side, price, qty, ts)
                    if time.time() - last_ping > 20:
                        await ws.send(json.dumps({"op": "ping"}))
                        last_ping = time.time()
        finally:
            poll.cancel()


# --------------------------------------------------------------------------
class HyperliquidPerp(BaseConnector):
    """Hyperliquid — DEX perpétuel on-chain, API WebSocket publique et gratuite.
    Le canal l2Book renvoie un SNAPSHOT complet du carnet à chaque message,
    donc on efface et on reconstruit à chaque tick (comme OKX en snapshot)."""
    name = "hyperliquid"
    WS = "wss://api.hyperliquid.xyz/ws"

    def __init__(self, symbol, sink):
        super().__init__(symbol, sink)
        # Hyperliquid nomme les perp par la crypto seule : BTCUSDT -> "BTC"
        self.coin = symbol.upper().replace("USDT", "").replace("USD", "") or "BTC"

    async def _stream(self):
        async with websockets.connect(self.WS, max_queue=None) as ws:
            self.bids.clear(); self.asks.clear()
            for ch in ("l2Book", "trades"):
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": ch, "coin": self.coin}}))
            self.sink.on_status(self.name, "ok")
            last_ping = time.time()
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    await ws.send(json.dumps({"method": "ping"}))
                    last_ping = time.time()
                    continue
                m = json.loads(raw)
                ch = m.get("channel")
                if ch == "l2Book":
                    d = m.get("data", {})
                    levels = d.get("levels", [[], []])
                    self.bids.clear(); self.asks.clear()
                    for lv in levels[0]:      # bids
                        self._set(self.bids, float(lv["px"]), float(lv["sz"]))
                    for lv in levels[1]:      # asks
                        self._set(self.asks, float(lv["px"]), float(lv["sz"]))
                    self._emit_book()
                elif ch == "trades":
                    for d in m.get("data", []):
                        # side "A" = agresseur vendeur, "B" = agresseur acheteur
                        is_sell = d.get("side") == "A"
                        self.sink.on_trade(self.name, float(d["px"]), float(d["sz"]),
                                           is_sell, float(d["time"]) / 1000.0)
                if time.time() - last_ping > 30:
                    await ws.send(json.dumps({"method": "ping"}))
                    last_ping = time.time()


BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"


def fetch_klines(symbol, interval="1m", limit=240):
    """Historique de bougies Binance futures (pré-chargement au lancement).
    Chaque bougie = [openTime, o, h, l, c, volume, closeTime, quoteVol, count,
    takerBuyBaseVol, takerBuyQuoteVol, ignore]. On en tire volume total et
    volume AGRESSEUR ACHETEUR (index 9) -> split achat/vente exact par minute."""
    r = requests.get(BINANCE_KLINES,
                     params={"symbol": symbol.upper(), "interval": interval,
                             "limit": min(int(limit), 1500)},
                     timeout=15)
    return r.json()


CONNECTORS = {
    "binance": BinanceFutures,
    "okx": OKXSwap,
    "bybit": BybitLinear,
    "hyperliquid": HyperliquidPerp,
}
