"""
news.py — flux d'actualités crypto + indice Fear & Greed, sans clé API.

Sources gratuites :
  - RSS CoinDesk + Cointelegraph (titres d'actualités crypto en continu)
  - alternative.me Fear & Greed index (sentiment global du marché crypto)

Tout est récupéré dans un thread d'arrière-plan (jamais dans le thread UI),
stocké avec un lock, et lu par l'interface sur un timer lent.
"""

import re
import time
import threading
import xml.etree.ElementTree as ET

import requests


# ---------------------------------------------------------------------------
# Classification automatique : importance (1-3) + impact BTC (haussier/baissier)
# Heuristique par mots-clés sur les titres anglais des flux RSS.
# ---------------------------------------------------------------------------

# importance 3 = MAJEURE (macro US, régulateurs, catastrophes)
KEYWORDS_MAJOR = [
    "fed ", "fomc", "cpi", "inflation", "rate cut", "rate hike", "interest rate",
    "powell", "treasury", "recession", "tariff", "trump", "election", "war",
    "sec ", "etf approval", "etf approved", "etf reject", "ban", "hack",
    "exploit", "bankrupt", "collapse", "halving", "blackrock", "default",
]
# importance 2 = MOYENNE (institutionnel, régulation, gros acteurs)
KEYWORDS_MEDIUM = [
    "etf", "regulation", "regulator", "lawsuit", "sue", "fine", "settlement",
    "grayscale", "fidelity", "institutional", "whale", "liquidation",
    "microstrategy", "strategy", "tether", "usdt", "usdc", "stablecoin",
    "binance", "coinbase", "kraken", "exchange", "mining", "miner",
    "adoption", "reserve", "government", "senate", "congress", "cbdc",
]

# impact directionnel probable sur BTC
KEYWORDS_BULLISH = [
    "approval", "approve", "approved", "rate cut", "cuts rate", "inflow",
    "buys", "bought", "accumulate", "adoption", "adopts", "partnership",
    "invest", "investment", "launch", "record high", "all-time high",
    "bullish", "reserve", "purchase", "surge", "rally", "dovish",
]
KEYWORDS_BEARISH = [
    "ban", "bans", "hack", "hacked", "exploit", "stolen", "lawsuit", "sues",
    "reject", "rejected", "outflow", "sell-off", "selloff", "bankrupt",
    "crackdown", "rate hike", "hikes rate", "fine", "fined", "crash",
    "plunge", "dump", "liquidated", "bearish", "warning", "fraud",
    "investigation", "hawkish", "delay", "postpone",
]


def classify(title):
    """Retourne (importance 1-3, impact 'haussier'/'baissier'/'incertain',
    mots-clés détectés)."""
    low = " " + title.lower() + " "
    matched = []
    importance = 1
    for k in KEYWORDS_MAJOR:
        if k in low:
            importance = 3
            matched.append(k.strip())
    if importance < 3:
        for k in KEYWORDS_MEDIUM:
            if k in low:
                importance = 2
                matched.append(k.strip())
                break
    bull = sum(1 for k in KEYWORDS_BULLISH if k in low)
    bear = sum(1 for k in KEYWORDS_BEARISH if k in low)
    if bull > bear:
        impact = "haussier"
    elif bear > bull:
        impact = "baissier"
    else:
        impact = "incertain"
    return importance, impact, matched[:4]


class NewsFeed:
    FEEDS = [
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
    ]
    FNG_URL = "https://api.alternative.me/fng/?limit=2"

    def __init__(self, refresh_s=300):
        self.refresh_s = refresh_s
        self._lock = threading.Lock()
        self._items = []          # [{source,title,link,date,hot}]
        self._fng = None          # {"value":int,"label":str,"yesterday":int}
        self._last_fetch = 0
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._running = False

    # ---------- lecture (thread UI) ----------
    def get_news(self):
        with self._lock:
            return list(self._items), self._fng, self._last_fetch

    # ---------- fetch (thread fond) ----------
    def _loop(self):
        while self._running:
            try:
                items = []
                for source, url in self.FEEDS:
                    items.extend(self._fetch_rss(source, url))
                # tri du plus récent au plus ancien
                items.sort(key=lambda x: x.get("ts", 0), reverse=True)
                fng = self._fetch_fng()
                with self._lock:
                    self._items = items[:40]
                    if fng:
                        self._fng = fng
                    self._last_fetch = time.time()
            except Exception:
                pass
            # attente découpée pour pouvoir s'arrêter vite
            for _ in range(self.refresh_s):
                if not self._running:
                    return
                time.sleep(1)

    def _fetch_rss(self, source, url):
        out = []
        try:
            r = requests.get(url, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0 OrderFlowCockpit"})
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if not title:
                    continue
                ts = self._parse_date(pub)
                importance, impact, keys = classify(title)
                out.append({"source": source, "title": title, "link": link,
                            "date": pub, "ts": ts,
                            "importance": importance, "impact": impact,
                            "keys": keys, "hot": importance >= 3})
        except Exception:
            pass
        return out

    @staticmethod
    def _parse_date(pub):
        # format RSS classique : "Mon, 02 Jul 2026 12:30:00 +0000"
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                return time.mktime(time.strptime(pub[:31].strip(), fmt))
            except Exception:
                continue
        return 0

    def _fetch_fng(self):
        try:
            r = requests.get(self.FNG_URL, timeout=15).json()
            data = r.get("data", [])
            if not data:
                return None
            today = data[0]
            out = {"value": int(today.get("value", 50)),
                   "label": today.get("value_classification", "?")}
            if len(data) > 1:
                out["yesterday"] = int(data[1].get("value", 50))
            return out
        except Exception:
            return None
