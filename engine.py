"""
engine.py — multi-exchange aggregation + order-flow analytics (v3).

Fuses Binance + OKX + Bybit BTC perpetual books into one de-biased view and
computes a full Level-2 / order-flow read with EXPLAINED signals.

Key idea: a price level's liquidity is summed across venues, and we track on how
many venues it appears. A wall present on 3/3 venues is real; one present on 1/3
is likely a single-venue spoof.
"""

import asyncio
import os
import time
import threading
import statistics
from collections import deque, defaultdict

from connectors import CONNECTORS


VENUES = ["binance", "okx", "bybit", "hyperliquid"]
N_VENUES = len(VENUES)   # pour l'affichage "N/4" (confluence multi-venues)


class Aggregator:
    def __init__(self):
        self.books = {v: ({}, {}) for v in VENUES}
        self.status = {v: "..." for v in VENUES}
        self.trades = deque(maxlen=600)
        self.cum_delta = 0.0
        self.cvd_window = deque(maxlen=900)
        # 60-min trade buffer for periodic reports (ts, price, qty, is_sell)
        self.trades_hist = deque(maxlen=800000)
        # HISTORIQUE PRÉ-CHARGÉ (klines 1 min) : pseudo-trades (ts, price, qty, is_sell)
        # tous ANTÉRIEURS au lancement. Sert UNIQUEMENT aux métriques agrégées longues
        # (VWAP, CVD, volume profile, flux par niveau) — jamais au tape/gros ordres,
        # pour ne pas les polluer. Rempli une fois par engine._backfill().
        self.seed_trades = []
        # VWAP de session incrémental (O(1) à la lecture, pas de re-scan)
        self.vwap_pv = 0.0      # somme prix*qty
        self.vwap_v = 0.0       # somme qty
        self.first_trade_ts = None
        # positionnement (Binance) : funding, open interest, liquidations
        self.funding = None                 # (rate, next_funding_ts)
        self.oi_hist = deque(maxlen=400)    # (ts, oi) ≈ 1h40 à 15s/point
        self.liqs = deque(maxlen=2000)      # (ts, side, price, qty)
        self._lock = threading.Lock()

    def on_book(self, venue, bids, asks):
        with self._lock:
            self.books[venue] = (dict(bids), dict(asks))

    def on_trade(self, venue, price, qty, is_sell, ts):
        with self._lock:
            self.trades.append((ts, venue, price, qty, is_sell))
            self.trades_hist.append((ts, price, qty, is_sell))
            self.vwap_pv += price * qty
            self.vwap_v += qty
            if self.first_trade_ts is None:
                self.first_trade_ts = ts
            signed = -qty if is_sell else qty
            self.cum_delta += signed
            self.cvd_window.append((ts, signed))
            # drop trades older than 60 min to bound memory
            cutoff = ts - 3600.0
            while self.trades_hist and self.trades_hist[0][0] < cutoff:
                self.trades_hist.popleft()

    def on_status(self, venue, status):
        self.status[venue] = status

    def on_funding(self, rate, next_ts):
        with self._lock:
            self.funding = (rate, next_ts)

    def on_oi(self, oi, ts):
        with self._lock:
            self.oi_hist.append((ts, oi))

    def on_liquidation(self, side, price, qty, ts):
        with self._lock:
            self.liqs.append((ts, side, price, qty))


class OrderFlowEngine:
    def __init__(self, symbol="BTCUSDT", venues=None,
                 bucket=1.0, depth_usd_pct=0.004, wall_k=6.0,
                 heatmap_rows=130, heatmap_cols=240, on_update=None):
        self.symbol = symbol.upper()
        self.venues = venues or list(VENUES)
        self.bucket = bucket
        self.depth_pct = depth_usd_pct
        self.wall_k = wall_k
        self.on_update = on_update or (lambda s: None)

        self.agg = Aggregator()
        self._connectors = []
        self._thread = None
        self._loop = None
        self._running = False

        self._wall_first_seen = {}
        self._known_walls = set()
        self.events = deque(maxlen=50)
        self.sweeps = deque(maxlen=50)
        self._mid_hist = deque(maxlen=120)
        from wall_history import WallHistory
        self.wall_history = WallHistory()
        # persistance de l'historique des murs (survit aux redémarrages)
        self._wall_state_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "wall_state.json")
        n = self.wall_history.load(self._wall_state_path)
        if n:
            self._log("MURS", f"Historique des murs rechargé : {n} niveaux repris.")
        self._last_wall_save = time.time()

        self.heatmap_rows = heatmap_rows
        self.heatmap_cols = heatmap_cols
        self.heatmap = deque(maxlen=heatmap_cols)
        self._hm_lo = None
        self._hm_hi = None
        self.hm_zoom = 1.0   # 1.0 = ±2% ; 0.5 = ±1% (zoomé) ; 2.0 = ±4% (dézoomé)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        # dernière sauvegarde de l'historique des murs avant de couper
        try:
            self.wall_history.save(self._wall_state_path)
        except Exception:
            pass
        for c in self._connectors:
            c.stop()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _backfill(self, minutes=240):
        """Pré-charge l'historique AVANT le live pour que VWAP, CVD, volume profile
        et le flux par niveau soient exacts DÈS le lancement (plus besoin de laisser
        tourner l'appli des heures). Source : bougies 1 min Binance futures.

        On fabrique deux pseudo-trades par minute (volume acheté / volume vendu au
        prix typique de la bougie), stockés dans agg.seed_trades — SÉPARÉS du live
        pour ne pas fausser le tape ni les gros ordres. Les murs et la heatmap ne
        sont pas concernés (le carnet n'a pas d'historique)."""
        try:
            from connectors import fetch_klines
            kl = fetch_klines(self.symbol, "1m", minutes + 5)
        except Exception as e:
            self._log("BACKFILL", f"Pré-chargement historique indisponible : {e}")
            return
        if not isinstance(kl, list) or not kl:
            return
        now = time.time(); now_ms = now * 1000.0
        seed = []
        pv = v = delta = 0.0
        first_ts = None
        day = time.strftime("%Y-%m-%d")
        sess_hi = sess_lo = None
        for k in kl:
            try:
                open_ms = float(k[0]); close_ms = float(k[6])
                high = float(k[2]); low = float(k[3]); close = float(k[4])
                vol = float(k[5]); buy = float(k[9])
            except (IndexError, ValueError, TypeError):
                continue
            if close_ms > now_ms:      # bougie en cours -> laissée au flux live
                continue
            if vol <= 0:
                continue
            sell = max(0.0, vol - buy)
            typ = (high + low + close) / 3.0
            ts = open_ms / 1000.0 + 30.0
            seed.append((ts, typ, buy, False))
            seed.append((ts, typ, sell, True))
            pv += typ * vol; v += vol
            delta += (buy - sell)
            if first_ts is None:
                first_ts = open_ms / 1000.0
            if time.strftime("%Y-%m-%d", time.localtime(open_ms / 1000.0)) == day:
                sess_hi = high if sess_hi is None else max(sess_hi, high)
                sess_lo = low if sess_lo is None else min(sess_lo, low)
        if not seed:
            return
        seed.sort(key=lambda t: t[0])
        with self.agg._lock:
            self.agg.seed_trades = seed
            self.agg.vwap_pv += pv
            self.agg.vwap_v += v
            self.agg.cum_delta += delta
            if self.agg.first_trade_ts is None:
                self.agg.first_trade_ts = first_ts
        if sess_hi is not None:
            self._sess_day = day
            self._sess_hi = sess_hi
            self._sess_lo = sess_lo
        self._log("BACKFILL", f"Historique pré-chargé : {len(seed)//2} min "
                  f"(VWAP, CVD, volume profile & flux/niveau prêts dès le lancement).")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._backfill(240)        # pré-charge l'historique (jusqu'à 4h) avant le live
        except Exception as e:
            self._log("BACKFILL", f"échec : {e}")
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            self.on_update({"error": str(e)})

    async def _main(self):
        self._connectors = [CONNECTORS[v](self.symbol, self.agg)
                            for v in self.venues if v in CONNECTORS]
        # flux positionnement (OI + funding + liquidations, Binance)
        from connectors import BinancePositioning
        self._pos = BinancePositioning(self.symbol, self.agg)
        self._connectors.append(self._pos)
        tasks = [asyncio.create_task(c.run()) for c in self._connectors]
        tasks.append(asyncio.create_task(self._emit_loop()))
        await asyncio.gather(*tasks)

    async def _emit_loop(self):
        while self._running:
            await asyncio.sleep(0.10)
            st = self._snapshot()
            if st:
                self.on_update(st)
            # sauvegarde de l'historique des murs toutes les 20s
            if time.time() - self._last_wall_save > 20:
                self.wall_history.save(self._wall_state_path)
                self._last_wall_save = time.time()

    def _bucketize(self, price):
        return round(price / self.bucket) * self.bucket

    def _merged_book(self):
        bids = defaultdict(lambda: {"qty": 0.0, "venues": set()})
        asks = defaultdict(lambda: {"qty": 0.0, "venues": set()})
        with self.agg._lock:
            snapshot = {v: (dict(b), dict(a)) for v, (b, a) in self.agg.books.items()}
        for v in self.venues:
            b, a = snapshot.get(v, ({}, {}))
            for p, q in b.items():
                k = self._bucketize(p); bids[k]["qty"] += q; bids[k]["venues"].add(v)
            for p, q in a.items():
                k = self._bucketize(p); asks[k]["qty"] += q; asks[k]["venues"].add(v)
        return bids, asks

    def _recent_trades(self, window_s):
        # FIX : horloge réelle, pas le timestamp du dernier trade — sinon le tape,
        # les agresseurs et les sweeps restent "gelés" sur les derniers trades reçus
        # quand le marché se calme ou qu'un flux se met en pause.
        if not self.agg.trades:
            return []
        now = time.time()
        with self.agg._lock:
            return [t for t in self.agg.trades if now - t[0] <= window_s]

    def _tape_speed(self):
        r = self._recent_trades(3.0)
        if len(r) < 2:
            return 0.0
        span = max(0.5, r[-1][0] - r[0][0])
        return len(r) / span

    def _aggressor_ratio(self):
        r = self._recent_trades(5.0)
        buy = sum(q for _, _, _, q, s in r if not s)
        sell = sum(q for _, _, _, q, s in r if s)
        tot = buy + sell
        return (buy / tot) if tot else 0.5

    def _cvd_recent(self):
        return sum(s for _, s in self.agg.cvd_window)

    def _detect_sweep(self):
        r = self._recent_trades(8.0)
        if len(r) < 20:
            return
        avg = statistics.mean(q for _, _, _, q, _ in r)
        last_ts, last_v, last_p, last_q, last_s = r[-1]
        if last_q >= 5 * avg and (not self.sweeps or self.sweeps[0].get("_t") != last_ts):
            side = "vente (baisse)" if last_s else "achat (hausse)"
            rec = {"_t": last_ts, "ts": time.strftime("%H:%M:%S"), "venue": last_v,
                   "price": last_p, "qty": round(last_q, 3), "x": round(last_q / avg, 1),
                   "side": side}
            self.sweeps.appendleft(rec)
            self._log("SWEEP", f"Sweep {side} {last_q:.2f} BTC ({rec['x']}x moy.) @ {last_p:,.0f} [{last_v}]")

    def _detect_absorption(self, mid):
        r = self._recent_trades(4.0)
        if len(r) < 25 or len(self._mid_hist) < 10:
            return None
        avg_print = statistics.mean(q for _, _, _, q, _ in r)
        vol = sum(q for _, _, _, q, _ in r)
        moved = abs(self._mid_hist[-1] - self._mid_hist[-min(len(self._mid_hist), 30)])
        buy = sum(q for _, _, _, q, s in r if not s)
        sell = sum(q for _, _, _, q, s in r if s)
        if vol > 40 * avg_print and moved < mid * 0.0004:
            if buy > sell * 1.3:
                return ("baisse", "Absorption cote ACHAT : les acheteurs frappent fort "
                        "mais le prix ne monte pas - un gros vendeur passif absorbe. "
                        "Souvent un plafond (retournement baissier possible).")
            if sell > buy * 1.3:
                return ("hausse", "Absorption cote VENTE : les vendeurs frappent fort "
                        "mais le prix ne baisse pas - un gros acheteur passif absorbe. "
                        "Souvent un plancher (retournement haussier possible).")
        return None

    def _compute_walls(self, mid, bids, asks):
        lo = mid * (1 - self.depth_pct * 5)
        hi = mid * (1 + self.depth_pct * 5)
        near_b = [(p, d) for p, d in bids.items() if lo <= p <= mid]
        near_a = [(p, d) for p, d in asks.items() if mid <= p <= hi]
        qtys = [d["qty"] for _, d in near_b + near_a]
        if len(qtys) < 6:
            return [], 0.0
        med = statistics.median(qtys) or 1e-9
        walls = []
        now = time.time()
        live = set()
        for side, levels in (("bid", near_b), ("ask", near_a)):
            for p, d in levels:
                if d["qty"] >= self.wall_k * med:
                    live.add(p)
                    first = self._wall_first_seen.setdefault(p, now)
                    walls.append({"side": side, "price": p, "qty": round(d["qty"], 2),
                                  "ratio": round(d["qty"] / med, 1), "venues": len(d["venues"]),
                                  "venue_list": sorted(d["venues"]), "age_s": round(now - first, 1),
                                  "dist": abs(p - mid)})
        for p in live - self._known_walls:
            w = next((x for x in walls if x["price"] == p), None)
            if w:
                s = "achat" if w["side"] == "bid" else "vente"
                self._log("WALL+", f"Mur {s} @ {p:,.0f} ({w['qty']:.1f} BTC, {w['venues']}/{N_VENUES} venues)")
        for p in self._known_walls - live:
            self._log("WALL-", f"Mur retire @ {p:,.0f} (possible spoof)")
        self._known_walls = live
        for p in list(self._wall_first_seen):
            if p not in live:
                self._wall_first_seen.pop(p, None)
        walls.sort(key=lambda w: w["qty"], reverse=True)
        return walls[:14], med

    def set_heatmap_zoom(self, factor):
        """Change le zoom prix de la heatmap et repart proprement."""
        self.hm_zoom = max(0.125, min(4.0, factor))
        self._hm_lo = None       # force un recentrage à la prochaine frame
        self.heatmap.clear()     # l'ancien historique serait à la mauvaise échelle

    def _update_heatmap(self, mid, bids, asks):
        if self._hm_lo is None:
            span = mid * self.depth_pct * 5 * self.hm_zoom
            self._hm_lo = mid - span; self._hm_hi = mid + span
        lo, hi = self._hm_lo, self._hm_hi
        if mid < lo + (hi - lo) * 0.15 or mid > hi - (hi - lo) * 0.15:
            span = (hi - lo) / 2
            self._hm_lo = mid - span; self._hm_hi = mid + span
            # FIX : les colonnes déjà dessinées utilisaient l'ancienne échelle ->
            # on efface pour ne pas afficher la liquidité aux mauvais prix
            self.heatmap.clear()
            lo, hi = self._hm_lo, self._hm_hi
        rows = self.heatmap_rows
        col = [0.0] * rows
        step = (hi - lo) / rows
        if step <= 0:
            return
        for book in (bids, asks):
            for p, d in book.items():
                if lo <= p < hi:
                    idx = int((p - lo) / step)
                    if 0 <= idx < rows:
                        col[idx] += d["qty"]
        self.heatmap.append(col)

    def _log(self, kind, text):
        self.events.appendleft({"ts": time.strftime("%H:%M:%S"), "kind": kind, "text": text})

    def _build_signals(self, s):
        sig = []
        imb = s["imbalance"]; cvd = s["cvd_recent"]
        agg = s["aggressor_ratio"]; speed = s["tape_speed"]; walls = s["walls"]

        if imb > 0.60:
            sig.append({"tag": "hausse", "title": f"Carnet desequilibre ACHAT {imb*100:.0f}%",
                "detail": "Plus de liquidite passive a l'achat qu'a la vente pres du prix.",
                "why": "Un carnet penche achat amortit les baisses ; le prix tend a remonter "
                       "vers la liquidite. A confirmer avec le flux (CVD/agresseurs)."})
        elif imb < 0.40:
            sig.append({"tag": "baisse", "title": f"Carnet desequilibre VENTE {(1-imb)*100:.0f}%",
                "detail": "Plus de liquidite passive a la vente qu'a l'achat pres du prix.",
                "why": "Plafond de liquidite au-dessus : le prix bute plus facilement et "
                       "peut etre pousse vers le bas."})

        if agg > 0.60:
            sig.append({"tag": "hausse", "title": f"Agresseurs ACHETEURS {agg*100:.0f}%",
                "detail": "Sur 5s, surtout des ordres d'achat au marche.",
                "why": "Les agresseurs (market orders) FONT bouger le prix. Acheteurs "
                       "dominants = pression haussiere reelle, pas juste affichee."})
        elif agg < 0.40:
            sig.append({"tag": "baisse", "title": f"Agresseurs VENDEURS {(1-agg)*100:.0f}%",
                "detail": "Sur 5s, surtout des ventes au marche.",
                "why": "Vendeurs au marche = pression baissiere qui s'execute maintenant."})

        trend = s["trend"]
        if cvd > 0 and trend < 0:
            sig.append({"tag": "hausse", "title": "Divergence CVD haussiere",
                "detail": "Le prix baisse mais le CVD monte (achat net).",
                "why": "Divergence classique : on accumule pendant que le prix descend. "
                       "Souvent annonce d'un rebond. A croiser avec TES niveaux."})
        elif cvd < 0 and trend > 0:
            sig.append({"tag": "baisse", "title": "Divergence CVD baissiere",
                "detail": "Le prix monte mais le CVD descend (vente nette).",
                "why": "Hausse non soutenue par le flux : essoufflement possible. "
                       "Mefiance sur les longs en haut de mouvement."})
        elif cvd > 0:
            sig.append({"tag": "hausse", "title": "CVD positif",
                "detail": "Achat net cumule sur la fenetre.",
                "why": "Le delta cumule mesure qui gagne la bataille a l'execution."})
        elif cvd < 0:
            sig.append({"tag": "baisse", "title": "CVD negatif",
                "detail": "Vente nette cumulee sur la fenetre.",
                "why": "Le delta cumule mesure qui gagne la bataille a l'execution."})

        if speed > 9:
            sig.append({"tag": "alerte", "title": f"Tape rapide {speed:.0f}/s",
                "detail": "Cadence d'execution elevee.",
                "why": "Pic d'activite = volatilite. Les niveaux cassent plus vite ; "
                       "reduis la taille ou elargis les stops."})

        absorb = s.get("absorption")
        if absorb:
            tag, txt = absorb
            sig.append({"tag": tag, "title": "ABSORPTION detectee",
                "detail": txt.split(" - ")[0] if " - " in txt else txt, "why": txt})

        for w in walls[:3]:
            side_txt = "ACHAT/support" if w["side"] == "bid" else "VENTE/resistance"
            conf = w["venues"]
            if conf >= 2:
                tag = "hausse" if w["side"] == "bid" else "baisse"
                sig.append({"tag": tag,
                    "title": f"Mur {side_txt} @ {w['price']:,.0f} - {conf}/{N_VENUES} venues",
                    "detail": f"{w['qty']:.1f} BTC, present {w['age_s']:.0f}s, sur {', '.join(w['venue_list'])}.",
                    "why": "Present sur plusieurs exchanges = vraie liquidite, pas un spoof "
                           "d'un seul venue. Niveau fiable pour rejet/cassure."})
            else:
                sig.append({"tag": "alerte",
                    "title": f"Mur {side_txt} @ {w['price']:,.0f} - 1/{N_VENUES} venue",
                    "detail": f"{w['qty']:.1f} BTC sur {w['venue_list'][0]} uniquement.",
                    "why": "Present sur UN seul exchange = mefiance, possible leurre/spoof "
                           "destine a t'influencer. Ne base pas un trade dessus seul."})

        score = 0
        score += 1 if imb > 0.58 else (-1 if imb < 0.42 else 0)
        score += 1 if agg > 0.58 else (-1 if agg < 0.42 else 0)
        score += 1 if cvd > 0 else (-1 if cvd < 0 else 0)
        if score >= 2:
            bias = ("hausse", "BIAIS HAUSSIER", "Plusieurs signaux d'ordre alignes a l'achat. "
                    "Cherche une confluence avec TES supports/zones AT pour un long.")
        elif score <= -2:
            bias = ("baisse", "BIAIS BAISSIER", "Plusieurs signaux alignes a la vente. "
                    "Croise avec TES resistances pour un short.")
        else:
            bias = ("neutre", "PAS DE BIAIS CLAIR", "Flux mitige. Mieux vaut attendre "
                    "l'alignement carnet+flux sur un de tes niveaux que forcer.")
        return sig, bias

    def _trend(self):
        if len(self._mid_hist) < 12:
            return 0
        a = self._mid_hist[0]; b = self._mid_hist[-1]
        if b > a * 1.0004: return 1
        if b < a * 0.9996: return -1
        return 0

    def _snapshot(self):
        bids, asks = self._merged_book()
        if not bids or not asks:
            return {"warming": True, "status": dict(self.agg.status)}

        # MID & SPREAD ROBUSTES : agréger max(bids)/min(asks) sur des venues qui
        # cotent à des prix légèrement différents (Hyperliquid a une prime/décote)
        # produit un carnet "croisé" et un spread faux (0). On prend donc la MÉDIANE
        # des mids par venue et la médiane des spreads réels par venue.
        with self.agg._lock:
            books_snap = {v: (dict(b), dict(a))
                          for v, (b, a) in self.agg.books.items()}
        vmids, vspreads = {}, []
        for v in self.venues:
            vb, va = books_snap.get(v, ({}, {}))
            if vb and va:
                bb, ba = max(vb), min(va)
                if ba >= bb:                     # carnet sain de cette venue
                    vmids[v] = (bb + ba) / 2
                    vspreads.append(ba - bb)
        if vmids:
            mid = statistics.median(vmids.values())
            spread = statistics.median(vspreads) if vspreads else 0.0
        else:                                    # repli : ancienne méthode
            mid = (max(bids) + min(asks)) / 2
            spread = max(0.0, min(asks) - max(bids))
        best_bid = mid - spread / 2
        best_ask = mid + spread / 2
        self._mid_hist.append(mid)

        # high/low de session (reset chaque jour) — niveaux clés à surveiller
        day = time.strftime("%Y-%m-%d")
        if getattr(self, "_sess_day", None) != day:
            self._sess_day = day
            self._sess_hi = mid
            self._sess_lo = mid
        else:
            self._sess_hi = max(self._sess_hi, mid)
            self._sess_lo = min(self._sess_lo, mid)

        lo, hi = mid * (1 - self.depth_pct), mid * (1 + self.depth_pct)
        bid_depth = sum(d["qty"] for p, d in bids.items() if p >= lo)
        ask_depth = sum(d["qty"] for p, d in asks.items() if p <= hi)
        tot = bid_depth + ask_depth
        imbalance = (bid_depth / tot) if tot else 0.5

        self._detect_sweep()
        absorption = self._detect_absorption(mid)
        walls, med = self._compute_walls(mid, bids, asks)
        self._update_heatmap(mid, bids, asks)
        self.wall_history.update(walls, mid)

        bid_keys = sorted([p for p in bids if p <= mid], reverse=True)[:16]
        ask_keys = sorted([p for p in asks if p >= mid])[:16]
        bids_ladder = [(p, bids[p]["qty"], len(bids[p]["venues"])) for p in bid_keys]
        asks_ladder = [(p, asks[p]["qty"], len(asks[p]["venues"])) for p in ask_keys]

        # CONTRIBUTION ÉQUITABLE : chaque exchange publie une profondeur différente
        # (Binance ~1000 niveaux, Bybit 200, OKX 400, Hyperliquid ~20) ET cote parfois
        # à un prix légèrement différent. On réutilise books_snap et vmids calculés en
        # haut, et on compare chaque venue autour de SON PROPRE mid, dans une bande
        # commune = portée du plus "court".
        reaches = []
        for v in self.venues:
            vb, va = books_snap.get(v, ({}, {}))
            if v in vmids and vb and va:
                reaches.append(min(vmids[v] - min(vb), max(va) - vmids[v]))
        # bande commune (le plus court fixe la limite), plafonnée à ±0.15%
        band = min(min(reaches), mid * 0.0015) if reaches else mid * 0.0015
        contrib = {}
        for v in self.venues:
            vb, va = books_snap.get(v, ({}, {}))
            vmid = vmids.get(v)
            if vmid is None:
                contrib[v] = 0.0
                continue
            contrib[v] = (sum(q for p, q in vb.items() if p >= vmid - band) +
                          sum(q for p, q in va.items() if p <= vmid + band))
        ctot = sum(contrib.values()) or 1.0
        contrib_pct = {v: contrib[v] / ctot for v in self.venues}

        s = {
            "symbol": self.symbol, "ts": time.strftime("%H:%M:%S"),
            "venues": self.venues, "status": dict(self.agg.status), "contrib_pct": contrib_pct,
            "mid": mid, "best_bid": best_bid, "best_ask": best_ask,
            "spread": spread, "spread_bps": (spread / mid * 1e4) if mid else 0,
            "bid_depth": bid_depth, "ask_depth": ask_depth, "imbalance": imbalance,
            "cum_delta": self.agg.cum_delta, "cvd_recent": self._cvd_recent(),
            "tape_speed": self._tape_speed(), "aggressor_ratio": self._aggressor_ratio(),
            "walls": walls, "wall_median": med, "absorption": absorption,
            "sweeps": [dict(x) for x in list(self.sweeps)[:12]], "events": list(self.events)[:24],
            "bids_ladder": bids_ladder, "asks_ladder": asks_ladder,
            "heatmap": list(self.heatmap), "hm_lo": self._hm_lo, "hm_hi": self._hm_hi,
            "trend": self._trend(),
            "sess_hi": self._sess_hi, "sess_lo": self._sess_lo,
            "synced": all(self.agg.status.get(v) == "ok" for v in self.venues),
        }
        s["signals"], s["bias"] = self._build_signals(s)
        return s

    def window_report(self, minutes):
        """Aggregate the last `minutes` minutes of executed trades into a readable
        bilan + a finer interpretive analysis. Works for any window (5/15/30/60)."""
        window_s = minutes * 60.0
        # horloge réelle + parcours depuis la fin avec arrêt au cutoff
        trades = self._trades_window(window_s)
        if len(trades) < 20:
            return {"ready": False, "n": len(trades), "minutes": minutes}

        first_ts = trades[0][0]; last_ts = trades[-1][0]
        span_min = max(0.1, (last_ts - first_ts) / 60.0)

        buy_n = sell_n = 0
        buy_vol = sell_vol = 0.0
        buy_usd = sell_usd = 0.0
        vol_by_price = defaultdict(float)
        big_prints = 0
        sizes = []
        for ts, price, qty, is_sell in trades:
            usd = price * qty
            bucket = round(price / 10.0) * 10.0
            vol_by_price[bucket] += qty
            sizes.append(qty)
            if is_sell:
                sell_n += 1; sell_vol += qty; sell_usd += usd
            else:
                buy_n += 1; buy_vol += qty; buy_usd += usd

        total_vol = buy_vol + sell_vol
        total_usd = buy_usd + sell_usd
        delta_vol = buy_vol - sell_vol
        buy_share = (buy_vol / total_vol) if total_vol else 0.5
        n_trades = len(trades)
        avg_size = (total_vol / n_trades) if n_trades else 0
        trades_per_min = n_trades / span_min if span_min else 0

        # big prints = trades >= 5x the average size (institutional-ish)
        big_prints = sum(1 for q in sizes if q >= 5 * avg_size) if avg_size else 0

        top_levels = sorted(vol_by_price.items(), key=lambda kv: kv[1], reverse=True)[:5]
        # concentration: how much of the volume is in the single busiest level
        busiest_share = (top_levels[0][1] / total_vol) if (top_levels and total_vol) else 0

        hi_price = max(p for _, p, _, _ in trades)
        lo_price = min(p for _, p, _, _ in trades)
        first_price = trades[0][1]; last_price = trades[-1][1]
        price_change = last_price - first_price
        price_range = hi_price - lo_price

        if buy_share > 0.55:
            dom = "ACHETEURS"; dom_tag = "hausse"
        elif buy_share < 0.45:
            dom = "VENDEURS"; dom_tag = "baisse"
        else:
            dom = "ÉQUILIBRE"; dom_tag = "neutre"

        base = {
            "ready": True, "minutes": minutes, "span_min": span_min,
            "buy_n": buy_n, "sell_n": sell_n,
            "buy_vol": buy_vol, "sell_vol": sell_vol,
            "buy_usd": buy_usd, "sell_usd": sell_usd,
            "total_vol": total_vol, "total_usd": total_usd,
            "delta_vol": delta_vol, "buy_share": buy_share,
            "dominant": dom, "dom_tag": dom_tag,
            "top_levels": top_levels, "busiest_share": busiest_share,
            "hi_price": hi_price, "lo_price": lo_price, "price_range": price_range,
            "first_price": first_price, "last_price": last_price,
            "price_change": price_change,
            "n_trades": n_trades, "avg_size": avg_size,
            "trades_per_min": trades_per_min, "big_prints": big_prints,
        }
        base["paragraph"] = self._build_paragraph(base)
        base["fine"] = self._build_fine_analysis(base)
        return base

    def _build_paragraph(self, r):
        span_min = r["span_min"]; dom = r["dominant"]
        buy_share = r["buy_share"]; price_change = r["price_change"]
        most_traded = r["top_levels"][0][0] if r["top_levels"] else 0
        dir_txt = ("le prix a MONTÉ" if price_change > 0 else
                   "le prix a BAISSÉ" if price_change < 0 else "le prix est resté stable")
        if dom == "ACHETEURS":
            head = (f"Sur les {span_min:.0f} dernières minutes, les ACHETEURS ont dominé "
                    f"({buy_share*100:.0f}% du volume au marché).")
            coherence = ("Flux acheteur ET prix en hausse : mouvement cohérent et soutenu."
                         if price_change > 0 else
                         "Mais le prix n'a pas suivi (achat fort sans hausse) : possible "
                         "absorption par de gros vendeurs — méfiance, essoufflement possible.")
        elif dom == "VENDEURS":
            head = (f"Sur les {span_min:.0f} dernières minutes, les VENDEURS ont dominé "
                    f"({(1-buy_share)*100:.0f}% du volume au marché).")
            coherence = ("Flux vendeur ET prix en baisse : mouvement cohérent et soutenu."
                         if price_change < 0 else
                         "Mais le prix n'a pas baissé (vente forte sans chute) : possible "
                         "absorption par de gros acheteurs — un plancher se construit peut-être.")
        else:
            head = (f"Sur les {span_min:.0f} dernières minutes, acheteurs et vendeurs se sont "
                    f"équilibrés ({buy_share*100:.0f}% achat).")
            coherence = "Marché en range/indécision : pas de camp clair, prudence sur les cassures."
        return (f"{head} {dir_txt} ({price_change:+.0f} USD). "
                f"Volume total échangé : {r['total_vol']:,.0f} BTC "
                f"(~{r['total_usd']/1e6:,.0f} M$). "
                f"Le plus gros volume s'est traité autour de {most_traded:,.0f} — "
                f"c'est le niveau le plus actif, souvent un aimant à prix. {coherence}")

    def _build_fine_analysis(self, r):
        """Return a list of (tag, text) interpretive observations: what the
        numbers MEAN and what one can conclude. Beginner-friendly Level-2 read."""
        out = []
        # 1) activity level
        tpm = r["trades_per_min"]
        if tpm > 600:
            out.append(("alerte", f"Activité très forte ({tpm:.0f} trades/min) : "
                "période agitée, beaucoup de monde sur le marché. Les mouvements sont "
                "rapides et les niveaux peuvent casser vite — prudence sur la taille."))
        elif tpm < 120:
            out.append(("neutre", f"Activité faible ({tpm:.0f} trades/min) : "
                "marché calme, peu de participants. Les cassures ont moins de poids, "
                "et le prix peut traîner sans direction."))
        else:
            out.append(("neutre", f"Activité normale ({tpm:.0f} trades/min) : "
                "rythme sain, ni euphorie ni désert."))

        # 2) order count interpretation (beaucoup d'ordres = quoi)
        total_n = r["buy_n"] + r["sell_n"]
        ratio = (r["buy_n"] / r["sell_n"]) if r["sell_n"] else 99
        if ratio > 1.3:
            out.append(("hausse", f"Beaucoup plus d'ordres d'ACHAT que de vente "
                f"({r['buy_n']:,} contre {r['sell_n']:,}). Les acheteurs sont plus "
                "nombreux à frapper : intérêt acheteur réel, pas juste quelques gros ordres."))
        elif ratio < 0.77:
            out.append(("baisse", f"Beaucoup plus d'ordres de VENTE que d'achat "
                f"({r['sell_n']:,} contre {r['buy_n']:,}). Pression vendeuse partagée "
                "par de nombreux participants, pas un seul gros vendeur isolé."))
        else:
            out.append(("neutre", f"Nombre d'ordres achat/vente équilibré "
                f"({r['buy_n']:,} / {r['sell_n']:,}) : bataille serrée, pas de camp "
                "qui écrase l'autre en nombre."))

        # 3) big prints (gros acteurs)
        if r["big_prints"] > 0:
            share = r["big_prints"] / total_n * 100 if total_n else 0
            out.append(("alerte", f"{r['big_prints']} gros ordres détectés "
                f"({share:.1f}% des trades) nettement au-dessus de la taille moyenne. "
                "Signe de présence de gros acteurs (institutions, whales) sur la période. "
                "Quand les gros bougent, ça compte plus que le bruit des petits."))
        else:
            out.append(("neutre", "Pas de gros ordre marquant : flux composé surtout "
                "de tailles ordinaires, mouvement 'de foule' plutôt que piloté par un acteur."))

        # 4) concentration of volume (où se joue l'action)
        bs = r["busiest_share"]
        if r["top_levels"]:
            lvl = r["top_levels"][0][0]
            if bs > 0.25:
                out.append(("alerte", f"Le volume est très concentré autour de {lvl:,.0f} "
                    f"({bs*100:.0f}% du total à ce seul niveau). Zone de bataille clé : "
                    "le marché se décide ici. Une cassure nette de ce niveau = direction forte."))
            else:
                out.append(("neutre", f"Volume bien réparti sur la fourchette "
                    f"(plus actif vers {lvl:,.0f}). Pas de point de bataille unique : "
                    "le prix balaie une zone large."))

        # 5) delta vs price (la lecture order-flow clé)
        dv = r["delta_vol"]; pc = r["price_change"]
        if dv > 0 and pc > 0:
            out.append(("hausse", f"Delta positif (+{dv:,.0f} BTC nets achetés) ET prix en "
                "hausse : les acheteurs paient et le prix répond. Tendance haussière saine."))
        elif dv < 0 and pc < 0:
            out.append(("baisse", f"Delta négatif ({dv:,.0f} BTC nets vendus) ET prix en "
                "baisse : les vendeurs dominent et le prix cède. Tendance baissière saine."))
        elif dv > 0 and pc <= 0:
            out.append(("baisse", f"DIVERGENCE : on a acheté net (+{dv:,.0f} BTC) mais le prix "
                "n'est PAS monté. Quelqu'un de gros vend en face (absorption). Souvent "
                "annonce d'un retournement baissier — un des signaux les plus utiles."))
        elif dv < 0 and pc >= 0:
            out.append(("hausse", f"DIVERGENCE : on a vendu net ({dv:,.0f} BTC) mais le prix "
                "n'a PAS baissé. Un gros acheteur absorbe. Possible plancher / retournement "
                "haussier — à surveiller de près avec tes supports."))

        # 6) range vs trend conclusion
        rng = r["price_range"]; mid = r["last_price"]
        if mid and rng / mid < 0.0015:
            out.append(("neutre", f"Fourchette très serrée ({rng:.0f} USD) : compression. "
                "Le marché accumule de l'énergie — une sortie de range peut être violente. "
                "Prépare tes deux scénarios (haut et bas)."))
        return out


    # -----------------------------------------------------------------------
    # MÉTHODES PRO — appelées par les nouvelles pages toutes les 2-5s
    # Aucune modification du code existant : on ajoute uniquement.
    # -----------------------------------------------------------------------

    def _trades_window(self, window_s):
        """Retourne les trades des dernières window_s secondes.
        Optimisé : parcourt depuis la fin et s'arrête au cutoff — on ne
        re-scanne jamais les 800k trades de l'historique complet."""
        with self.agg._lock:
            all_trades = list(self.agg.trades_hist)   # copie C rapide
        cutoff = time.time() - window_s
        out = []
        for t in reversed(all_trades):
            if t[0] < cutoff:
                break
            out.append(t)
        out.reverse()
        return out

    def _trades_window_seed(self, window_s):
        """Comme _trades_window mais inclut l'historique pré-chargé (klines).
        Réservé aux métriques agrégées longues (VWAP, CVD, volume profile, flux par
        niveau) — le seed est toujours plus ancien que le live, donc pas de doublon."""
        cutoff = time.time() - window_s
        with self.agg._lock:
            live = list(self.agg.trades_hist)
            seed = self.agg.seed_trades
        out = []
        for t in reversed(seed):        # seed trié croissant -> reversed = décroissant
            if t[0] < cutoff:
                break
            out.append(t)
        out.reverse()                   # seed dans la fenêtre, croissant
        out.extend(t for t in live if t[0] >= cutoff)   # puis le live (plus récent)
        return out

    def get_vwap(self, window_s=None):
        """VWAP de session — incrémental O(1), plus aucun re-scan d'historique."""
        with self.agg._lock:
            cum_pv = self.agg.vwap_pv
            cum_v = self.agg.vwap_v
            first_ts = self.agg.first_trade_ts
            if self.agg.trades_hist:
                last_price = self.agg.trades_hist[-1][1]
            elif self.agg.seed_trades:                    # avant le 1er trade live
                last_price = self.agg.seed_trades[-1][1]
            else:
                last_price = None
        if not cum_v or last_price is None:
            return None
        vwap = cum_pv / cum_v
        span_min = max(0.1, (time.time() - first_ts) / 60) if first_ts else 0.1
        return {
            "vwap":      vwap,
            "last":      last_price,
            "dev_pct":   (last_price - vwap) / vwap * 100,
            "dev_usd":   last_price - vwap,
            "cum_vol":   cum_v,
            "span_min":  span_min,
            "above":     last_price > vwap,
        }

    def get_cvd_windows(self):
        """CVD segmenté 1/5/15/30 min — une seule passe sur 30 min de trades
        (au lieu de 4 scans complets de l'historique)."""
        now = time.time()
        wins = [1, 5, 15, 30]
        data = {m: {"cvd": 0.0, "buy": 0.0, "sell": 0.0, "n": 0, "cvd2": 0.0}
                for m in wins}
        with self.agg._lock:
            all_trades = list(self.agg.trades_hist)
            seed = self.agg.seed_trades
        # du plus récent au plus ancien : live puis seed (seed toujours antérieur)
        def _desc():
            for t in reversed(all_trades):
                yield t
            for t in reversed(seed):
                yield t
        for ts, price, qty, is_sell in _desc():
            age = now - ts
            if age > 1800:
                break
            signed = -qty if is_sell else qty
            for m in wins:
                w = m * 60
                if age <= w:
                    d = data[m]
                    d["cvd"] += signed
                    d["n"] += 1
                    if is_sell:
                        d["sell"] += qty
                    else:
                        d["buy"] += qty
                    if age <= w / 2:
                        d["cvd2"] += signed   # moitié récente (pour l'accélération)
        result = {}
        for m in wins:
            d = data[m]
            if d["n"] == 0:
                result[m] = {"ready": False}
                continue
            cvd2 = d["cvd2"]
            cvd1 = d["cvd"] - cvd2
            accel = (cvd2 / abs(cvd1)) if abs(cvd1) > 0.001 else 0.0
            result[m] = {
                "ready": True, "cvd": d["cvd"],
                "buy_vol": d["buy"], "sell_vol": d["sell"],
                "n": d["n"],
                "cvd_first": cvd1, "cvd_second": cvd2,
                "acceleration": accel,
            }
        return result

    def get_flow_segments(self, window_s=300):
        """Sépare retail (<0.5 BTC), moyen (0.5-5 BTC), institutionnel (>5 BTC)."""
        trades = self._trades_window(window_s)
        if len(trades) < 5:
            return None

        def seg(lo, hi):
            t   = [(p, q, s) for _, p, q, s in trades if lo <= q < hi]
            buy = sum(q for p, q, s in t if not s)
            sel = sum(q for p, q, s in t if s)
            tot = buy + sel
            return {
                "n": len(t), "buy": round(buy, 2), "sell": round(sel, 2),
                "delta": round(buy - sel, 2),
                "ratio": buy / tot if tot else 0.5,
                "buy_usd":  sum(p * q for p, q, s in t if not s),
                "sell_usd": sum(p * q for p, q, s in t if s),
            }

        avg_size      = sum(q for _, _, q, _ in trades) / len(trades)
        big_threshold = max(5.0, avg_size * 10)

        # gros ordres, triés par TEMPS (le plus récent en premier)
        big_prints = sorted(
            [(ts, p, q, s) for ts, p, q, s in trades if q >= big_threshold],
            key=lambda x: x[0], reverse=True
        )[:20]

        return {
            "retail": seg(0, 0.5),
            "mid":    seg(0.5, 5.0),
            "inst":   seg(5.0, 1e9),
            "big_threshold": round(big_threshold, 3),
            "avg_size":      round(avg_size, 4),
            "window_min":    window_s / 60,
            "big_prints": [
                {"ts":    time.strftime("%H:%M:%S", time.localtime(ts)),
                 "price": p, "qty": round(q, 3),
                 "side":  "VENTE" if s else "ACHAT",
                 "usd":   round(p * q)}
                for ts, p, q, s in big_prints
            ],
        }

    def get_volume_profile(self, window_s=3600, bucket=10.0):
        """POC + Value Area 70% depuis les trades exécutés (+ historique pré-chargé)."""
        trades = self._trades_window_seed(window_s)
        if len(trades) < 20:
            return None

        vol = defaultdict(lambda: {"total": 0.0, "buy": 0.0, "sell": 0.0})
        total_vol = 0.0
        for _, price, qty, is_sell in trades:
            b = round(price / bucket) * bucket
            vol[b]["total"] += qty
            if is_sell:
                vol[b]["sell"] += qty
            else:
                vol[b]["buy"] += qty
            total_vol += qty

        if not vol or total_vol == 0:
            return None

        poc = max(vol, key=lambda p: vol[p]["total"])

        # Value Area 70% en partant du POC
        prices_sorted = sorted(vol.keys())
        poc_idx = prices_sorted.index(poc)
        va_vol  = vol[poc]["total"]
        lo_idx, hi_idx = poc_idx, poc_idx
        target  = total_vol * 0.70
        while va_vol < target:
            lo_add = vol[prices_sorted[lo_idx - 1]]["total"] if lo_idx > 0 else 0
            hi_add = vol[prices_sorted[hi_idx + 1]]["total"] if hi_idx < len(prices_sorted) - 1 else 0
            if lo_add == 0 and hi_add == 0:
                break
            if lo_add >= hi_add and lo_idx > 0:
                lo_idx -= 1; va_vol += lo_add
            elif hi_idx < len(prices_sorted) - 1:
                hi_idx += 1; va_vol += hi_add
            else:
                break

        vah  = prices_sorted[hi_idx]
        val  = prices_sorted[lo_idx]
        top10 = sorted(vol.items(), key=lambda kv: kv[1]["total"], reverse=True)[:30]

        return {
            "poc": poc, "poc_vol": vol[poc]["total"],
            "poc_buy": vol[poc]["buy"], "poc_sell": vol[poc]["sell"],
            "vah": vah, "val": val,
            "va_pct":    va_vol / total_vol,
            "total_vol": total_vol,
            "top10":     [(p, d) for p, d in top10],
            # histogramme complet (prix -> volume) pour le graphique
            "levels":    [(p, vol[p]["total"]) for p in prices_sorted],
            "bucket":    bucket,
            "n_levels":  len(vol),
            "window_min": window_s / 60,
        }

    def get_cascade_sweeps(self, window_s=120):
        """Sweeps en cascade : plusieurs niveaux touchés en < 0.8s côté dominant."""
        trades = self._trades_window(window_s)
        if not trades:
            return []

        cascades = []
        i = 0
        while i < len(trades):
            ts0, p0, q0, s0 = trades[i]
            group = [(ts0, p0, q0, s0)]
            j = i + 1
            while j < len(trades) and trades[j][0] - ts0 <= 0.8:
                if trades[j][3] == s0:
                    group.append(trades[j])
                j += 1
            if len(group) >= 4:
                prices   = [p for _, p, _, _ in group]
                total_q  = sum(q for _, _, q, _ in group)
                distinct = len(set(round(p / 10) * 10 for p in prices))
                if distinct >= 3 and total_q >= 1.5:
                    cascades.append({
                        "ts":       time.strftime("%H:%M:%S", time.localtime(ts0)),
                        "ts_raw":   ts0,
                        "side":     "VENTE" if s0 else "ACHAT",
                        "is_sell":  s0,
                        "qty":      round(total_q, 2),
                        "levels":   distinct,
                        "range":    round(max(prices) - min(prices), 0),
                        "lo":       round(min(prices), 0),
                        "hi":       round(max(prices), 0),
                        "n_trades": len(group),
                        "usd":      round(sum(p * q for _, p, q, _ in group)),
                    })
            i = j if j > i else i + 1

        # dédoublonnage (< 2s d'écart)
        deduped = []
        for c in cascades:
            if not deduped or c["ts_raw"] - deduped[-1]["ts_raw"] > 2.0:
                deduped.append(c)
        return list(reversed(deduped))[:20]

    def get_levels_flow(self, prices, tol=30.0, window_s=3600):
        """Pour chaque prix donné : volume ACHETÉ vs VENDU exécuté autour (±tol$)
        sur la fenêtre. Montre si le niveau a été accumulé (achat) ou distribué
        (vente). Une seule passe sur les trades."""
        out = {p: [0.0, 0.0] for p in prices}   # prix -> [buy, sell]
        if not prices:
            return {}
        trades = self._trades_window_seed(window_s)
        for ts, price, q, is_sell in trades:
            for p in prices:
                if abs(price - p) <= tol:
                    out[p][1 if is_sell else 0] += q
        return {p: (b, s) for p, (b, s) in out.items()}

    def get_positioning(self):
        """OI + funding + liquidations (Binance) pour la page POSITIONNEMENT."""
        with self.agg._lock:
            funding = self.agg.funding
            oi_hist = list(self.agg.oi_hist)
            liqs = list(self.agg.liqs)
        now = time.time()
        out = {"funding": None, "oi": None}

        if funding:
            rate, next_ts = funding
            out["funding"] = {
                "rate": rate,
                "rate_pct": rate * 100,
                "annual_pct": rate * 3 * 365 * 100,   # 3 fundings/jour
                "next_in_min": max(0.0, (next_ts - now) / 60) if next_ts else None,
            }

        if oi_hist:
            oi_now = oi_hist[-1][1]

            def oi_ago(sec):
                target = now - sec
                best = oi_hist[0][1]
                for ts, v in oi_hist:
                    if ts <= target:
                        best = v
                    else:
                        break
                return best

            oi5, oi15 = oi_ago(300), oi_ago(900)
            out["oi"] = {
                "now": oi_now,
                "chg_5m": oi_now - oi5,
                "chg_5m_pct": (oi_now - oi5) / oi5 * 100 if oi5 else 0.0,
                "chg_15m": oi_now - oi15,
                "chg_15m_pct": (oi_now - oi15) / oi15 * 100 if oi15 else 0.0,
                "n_samples": len(oi_hist),
            }

        t5 = self._trades_window(300)
        out["price_chg_5m"] = (t5[-1][1] - t5[0][1]) if len(t5) >= 2 else 0.0

        recent = [(ts, s, p, q) for ts, s, p, q in liqs if now - ts <= 300]
        out["liq_5m"] = {
            "long_usd":  sum(p * q for _, s, p, q in recent if s == "long"),
            "short_usd": sum(p * q for _, s, p, q in recent if s == "short"),
            "n": len(recent),
        }
        out["liqs"] = [
            {"ts": time.strftime("%H:%M:%S", time.localtime(ts)),
             "side": s, "price": p, "qty": round(q, 4), "usd": round(p * q)}
            for ts, s, p, q in list(reversed(liqs))[:25]
        ]
        return out


if __name__ == "__main__":
    def printer(s):
        if "error" in s:
            print("ERR", s["error"]); return
        if s.get("warming"):
            print("warming...", s["status"]); return
        print(f"{s['ts']} mid={s['mid']:.1f} imb={s['imbalance']:.2f} "
              f"cvd={s['cvd_recent']:.1f} walls={len(s['walls'])} -> {s['bias'][1]} | {s['status']}")
    eng = OrderFlowEngine(on_update=printer)
    eng.start()
    try:
        time.sleep(25)
    finally:
        eng.stop()
