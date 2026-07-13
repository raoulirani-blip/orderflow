"""
quant.py — données QUANT récupérées EN ARRIÈRE-PLAN (jamais de latence dans l'UI).

Deux flux, chacun dans son thread, avec cache : l'interface lit seulement le cache
(instantané), les appels réseau se font en fond.

- Options BTC (Deribit, API publique gratuite) : volatilité implicite (ATM IV), skew
  put/call, max pain (aimant d'expiration), put/call ratio, open interest options.
- Macro (Yahoo Finance, gratuit) : Nasdaq, S&P500, DXY (dollar), VIX (peur), Or, taux
  US 10 ans — pour le contexte risk-on / risk-off qui pousse (ou freine) BTC.
"""

import datetime
import re
import threading
import time

import requests

DERIBIT_SUMMARY = ("https://www.deribit.com/api/v2/public/"
                   "get_book_summary_by_currency?currency=BTC&kind=option")
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=5d"
UA = {"User-Agent": "Mozilla/5.0"}
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
MACRO_SYMBOLS = [
    ("Nasdaq", "^IXIC"), ("S&P 500", "^GSPC"), ("Dollar (DXY)", "DX-Y.NYB"),
    ("VIX (peur)", "^VIX"), ("Or", "GC=F"), ("US 10 ans", "^TNX"),
]


class QuantFeed:
    def __init__(self):
        self.options = None       # dict ou None
        self.macro = None         # dict {nom: {price, chg}} ou None
        self.opt_error = None
        self.macro_error = None
        self._running = True
        threading.Thread(target=self._loop_options, daemon=True).start()
        threading.Thread(target=self._loop_macro, daemon=True).start()

    def stop(self):
        self._running = False

    # ---------------- OPTIONS (Deribit) ----------------
    def _loop_options(self):
        while self._running:
            try:
                self.options = self._fetch_options()
                self.opt_error = None
            except Exception as e:
                self.opt_error = str(e)
            for _ in range(30):                # ~30s, coupable rapidement à l'arrêt
                if not self._running:
                    return
                time.sleep(1)

    def _fetch_options(self):
        data = requests.get(DERIBIT_SUMMARY, timeout=15).json().get("result", [])
        if not data:
            return None
        under = data[0].get("underlying_price") or data[0].get("mark_price") or 0
        opts = []
        for o in data:
            m = re.match(r"BTC-(\d+)([A-Z]{3})(\d{2})-(\d+)-([CP])", o["instrument_name"])
            if not m:
                continue
            try:
                exp = datetime.date(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
            except (ValueError, KeyError):
                continue
            opts.append({"exp": exp, "strike": float(m.group(4)), "cp": m.group(5),
                         "iv": o.get("mark_iv") or 0.0, "oi": o.get("open_interest") or 0.0})
        if not opts or not under:
            return None
        today = datetime.date.today()
        futures = sorted({o["exp"] for o in opts if o["exp"] >= today})
        front = futures[0] if futures else sorted({o["exp"] for o in opts})[-1]
        fe = [o for o in opts if o["exp"] == front]

        atm = min(fe, key=lambda o: abs(o["strike"] - under))
        puts = [o for o in fe if o["cp"] == "P" and o["iv"] > 0]
        calls = [o for o in fe if o["cp"] == "C" and o["iv"] > 0]

        def iv_near(lst, target):
            return min(lst, key=lambda o: abs(o["strike"] - target))["iv"] if lst else None
        put_iv = iv_near(puts, under * 0.9)      # OTM put ~10% sous le prix
        call_iv = iv_near(calls, under * 1.1)    # OTM call ~10% au-dessus
        skew = (put_iv - call_iv) if (put_iv is not None and call_iv is not None) else None

        strikes = sorted({o["strike"] for o in fe})

        def pain(k):
            return sum((max(0.0, k - o["strike"]) if o["cp"] == "C"
                        else max(0.0, o["strike"] - k)) * o["oi"] for o in fe)
        max_pain = min(strikes, key=pain) if strikes else None

        put_oi = sum(o["oi"] for o in fe if o["cp"] == "P")
        call_oi = sum(o["oi"] for o in fe if o["cp"] == "C")
        pcr = (put_oi / call_oi) if call_oi else None
        # OI total toutes expirations (en BTC)
        total_oi = sum(o["oi"] for o in opts)
        return {
            "under": under, "front": front.strftime("%d %b %y"),
            "atm_iv": atm["iv"], "put_iv": put_iv, "call_iv": call_iv, "skew": skew,
            "max_pain": max_pain, "pcr": pcr,
            "put_oi": put_oi, "call_oi": call_oi, "total_oi": total_oi,
            "ts": time.time(),
        }

    # ---------------- MACRO (Yahoo) ----------------
    def _loop_macro(self):
        while self._running:
            try:
                self.macro = self._fetch_macro()
                self.macro_error = None
            except Exception as e:
                self.macro_error = str(e)
            for _ in range(60):
                if not self._running:
                    return
                time.sleep(1)

    def _fetch_macro(self):
        out = {}
        for name, sym in MACRO_SYMBOLS:
            try:
                r = requests.get(YAHOO.format(sym), headers=UA, timeout=8)
                meta = r.json()["chart"]["result"][0]["meta"]
                px = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                chg = ((px - prev) / prev * 100) if (px and prev) else None
                out[name] = {"price": px, "chg": chg}
            except Exception:
                out[name] = {"price": None, "chg": None}
        out["ts"] = time.time()
        return out
