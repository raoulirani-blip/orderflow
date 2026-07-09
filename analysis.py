"""
analysis.py — slow, accumulating analysis layer for the second page.

The live engine emits ~10 states/sec, too fast to read. This module SUBSCRIBES
to those states, accumulates them over rolling time windows, and produces stable
conclusions that only change when something real changes:

  - liquidity_zones(): the biggest, most persistent liquidity levels right now,
    ranked, each with a plain-French reason (why it matters) and how stable it is.
  - slow_verdict(): one sentence summarizing the last N seconds of order flow,
    refreshed on a slow clock so it's readable.
  - key_levels(): the handful of prices a trader should watch, with what to expect.

Design: feed() is called on every fast state (cheap accumulation only). The GUI
reads the three outputs on a slow timer (e.g. every 2s and every 5s).
"""

import time
import statistics
from collections import deque, defaultdict


class SlowAnalyzer:
    def __init__(self, verdict_window_s=10.0, zone_persist_s=3.0):
        self.verdict_window_s = verdict_window_s
        self.zone_persist_s = zone_persist_s

        # rolling history of compact metric samples: (ts, dict)
        self._hist = deque(maxlen=400)
        # per-price-level persistence tracker: price -> dict(first_seen, last_seen,
        #   qty_samples, side, venues_max)
        self._levels = {}
        self._last_mid = None

    # ---------- ingestion (fast, called ~10x/s) ----------
    def feed(self, s):
        if s.get("warming") or "error" in s:
            return
        now = time.time()
        self._last_mid = s["mid"]
        self._hist.append((now, {
            "mid": s["mid"],
            "imbalance": s["imbalance"],
            "cvd": s["cvd_recent"],
            "aggressor": s["aggressor_ratio"],
            "tape": s["tape_speed"],
            "trend": s["trend"],
            "absorption": s.get("absorption"),
        }))
        # track walls for persistence (a zone must persist to count as "stable")
        seen = set()
        for w in s.get("walls", []):
            p = w["price"]; seen.add(p)
            lv = self._levels.get(p)
            if lv is None:
                self._levels[p] = {
                    "first_seen": now, "last_seen": now,
                    "side": w["side"], "qty": w["qty"],
                    "qty_max": w["qty"], "venues_max": w["venues"],
                    "ratio_max": w["ratio"],
                }
            else:
                lv["last_seen"] = now
                lv["qty"] = w["qty"]
                lv["qty_max"] = max(lv["qty_max"], w["qty"])
                lv["venues_max"] = max(lv["venues_max"], w["venues"])
                lv["ratio_max"] = max(lv["ratio_max"], w["ratio"])
        # expire levels not seen for >4s
        for p in list(self._levels):
            if now - self._levels[p]["last_seen"] > 4.0:
                self._levels.pop(p, None)

    def _recent(self, window_s):
        if not self._hist:
            return []
        now = self._hist[-1][0]
        return [d for ts, d in self._hist if now - ts <= window_s]

    # ---------- outputs (slow, called every 2-5s) ----------
    def liquidity_zones(self, top_n=6):
        """Ranked stable liquidity zones with reasons. Only levels that persisted
        at least `zone_persist_s` qualify, so this list is calm and readable."""
        if self._last_mid is None:
            return []
        now = time.time()
        out = []
        for p, lv in self._levels.items():
            age = now - lv["first_seen"]
            if age < self.zone_persist_s:
                continue  # too new, ignore to kill flicker
            dist = p - self._last_mid
            dist_pct = dist / self._last_mid * 100
            side_txt = "Support" if lv["side"] == "bid" else "Résistance"
            venues = lv["venues_max"]
            # reliability label
            if venues >= 3:
                rel = f"très fiable ({venues} exchanges)"
            elif venues == 2:
                rel = "fiable (2 exchanges)"
            else:
                rel = "à confirmer (1 seul exchange — méfiance spoof)"
            # stability from age
            if age > 60:
                stab = f"très stable ({age/60:.0f} min)"
            elif age > 20:
                stab = f"stable ({age:.0f}s)"
            else:
                stab = f"récent ({age:.0f}s)"
            reason = (f"{lv['qty_max']:.0f} BTC empilés ici, {rel}. "
                      f"Présent {stab}.")
            out.append({
                "price": p, "side": lv["side"], "side_txt": side_txt,
                "qty": lv["qty_max"], "venues": venues,
                "dist_pct": dist_pct, "age": age,
                "reliability": venues, "reason": reason,
            })
        # rank by a score: size * venues, nearest first as tiebreak
        out.sort(key=lambda z: (z["qty"] * z["venues"]), reverse=True)
        return out[:top_n]

    def slow_verdict(self):
        """One stable sentence about the last verdict_window_s seconds."""
        r = self._recent(self.verdict_window_s)
        if len(r) < 5:
            return ("neutre", "Analyse en cours… (accumulation des données)")
        imb = statistics.mean(d["imbalance"] for d in r)
        agg = statistics.mean(d["aggressor"] for d in r)
        cvd_start = r[0]["cvd"]; cvd_end = r[-1]["cvd"]
        cvd_dir = cvd_end - cvd_start
        tape = statistics.mean(d["tape"] for d in r)
        price_move = r[-1]["mid"] - r[0]["mid"]

        # build a calm, conclusive sentence
        buyers = (imb > 0.55) + (agg > 0.55) + (cvd_dir > 0)
        sellers = (imb < 0.45) + (agg < 0.45) + (cvd_dir < 0)

        win = self.verdict_window_s
        if buyers >= 2 and price_move >= 0:
            tag = "hausse"
            msg = (f"Sur les {win:.0f} dernières secondes : les ACHETEURS dominent "
                   f"(carnet penché achat, agresseurs acheteurs, CVD en hausse). "
                   f"Le prix tient ou monte. Pression haussière qui se confirme.")
        elif sellers >= 2 and price_move <= 0:
            tag = "baisse"
            msg = (f"Sur les {win:.0f} dernières secondes : les VENDEURS dominent "
                   f"(carnet penché vente, agresseurs vendeurs, CVD en baisse). "
                   f"Le prix tient ou baisse. Pression baissière qui se confirme.")
        elif buyers >= 2 and price_move < 0:
            tag = "hausse"
            msg = (f"Sur les {win:.0f} dernières secondes : DIVERGENCE — les acheteurs "
                   f"dominent le flux mais le prix baisse encore. Souvent signe d'un "
                   f"rebond proche. À surveiller avec tes supports.")
        elif sellers >= 2 and price_move > 0:
            tag = "baisse"
            msg = (f"Sur les {win:.0f} dernières secondes : DIVERGENCE — les vendeurs "
                   f"dominent le flux mais le prix monte encore. Hausse fragile, "
                   f"essoufflement possible. À surveiller avec tes résistances.")
        else:
            tag = "neutre"
            msg = (f"Sur les {win:.0f} dernières secondes : pas de camp dominant. "
                   f"Acheteurs et vendeurs s'équilibrent. Marché indécis — "
                   f"mieux vaut attendre un signal clair sur un de tes niveaux.")

        if tape > 9:
            msg += " ⚠ Activité élevée : volatilité, les niveaux peuvent casser vite."
        elif tape < 1.5:
            msg += " (Marché calme, peu d'activité.)"
        return (tag, msg)

    def key_levels(self):
        """The handful of prices to watch, with what to expect at each."""
        zones = self.liquidity_zones(top_n=8)
        if self._last_mid is None or not zones:
            return []
        mid = self._last_mid
        above = sorted([z for z in zones if z["price"] > mid], key=lambda z: z["price"])
        below = sorted([z for z in zones if z["price"] < mid], key=lambda z: z["price"], reverse=True)
        out = []
        if above:
            z = above[0]
            out.append({
                "price": z["price"], "label": "Résistance la plus proche",
                "dist_pct": z["dist_pct"], "venues": z["venues"],
                "expect": ("Le prix peut buter ici. S'il REJETTE = opportunité short "
                           "(avec ton AT). S'il CASSE avec volume = accélération haussière, "
                           "ne shorte pas."),
            })
        if below:
            z = below[0]
            out.append({
                "price": z["price"], "label": "Support le plus proche",
                "dist_pct": z["dist_pct"], "venues": z["venues"],
                "expect": ("Le prix peut rebondir ici. S'il TIENT = opportunité long "
                           "(avec ton AT). S'il CASSE avec volume = accélération baissière, "
                           "ne longe pas."),
            })
        # add the single biggest zone overall as "zone majeure"
        biggest = max(zones, key=lambda z: z["qty"] * z["venues"])
        if biggest["price"] not in [o["price"] for o in out]:
            out.append({
                "price": biggest["price"], "label": "Zone de liquidité majeure",
                "dist_pct": biggest["dist_pct"], "venues": biggest["venues"],
                "expect": ("Le plus gros bloc de liquidité visible. Aimant à prix : "
                           "le marché est souvent attiré vers ces niveaux."),
            })
        return out
