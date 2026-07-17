"""
wall_history.py — tracks the life of every wall over time for the Walls page.

The engine detects walls each tick (price, side, qty, venues, age). This module
keeps a HISTORY: for every wall (identified by price level + side) it records
when it appeared, its max size, how long it lived, how many times price came to
test it, and whether it ultimately HELD or was BROKEN/PULLED.

From that history we can answer, for any time window (1/5/15/30/60 min):
  - the most important walls (by size x persistence x venues),
  - the longest-lived wall,
  - per wall: max BTC, $ value, lifespan, times tested, venues, outcome,
  - aggregate stats: how many walls appeared, how many were spoofs (pulled fast),
    buy-vs-sell wall balance.

"Number of orders on a wall" is NOT available: exchanges publish aggregated
size per price level (market-by-price), not individual orders. We expose size in
BTC and $, plus venue count and test/hold behaviour, which is the actionable info.
"""

import json
import os
import time
from collections import deque


class WallRecord:
    __slots__ = ("price", "side", "first_seen", "last_seen", "max_qty",
                 "max_ratio", "venues_max", "tests", "_was_near", "broken",
                 "pulled", "last_price_rel", "absorbed")

    def __init__(self, price, side, qty, ratio, venues, now):
        self.price = price
        self.side = side            # 'bid' (support) or 'ask' (resistance)
        self.first_seen = now
        self.last_seen = now
        self.max_qty = qty
        self.max_ratio = ratio
        self.venues_max = venues
        self.tests = 0              # times price came close then left
        self._was_near = False
        self.broken = False         # price traded through it
        self.pulled = False         # disappeared fast while price not near (spoof)
        self.last_price_rel = None
        # volume AGRESSIF exécuté À ce niveau tant que le mur vivait. Beaucoup de
        # volume absorbé SANS que le mur cède = ICEBERG (ordre caché qui se recharge).
        self.absorbed = 0.0

    def update(self, qty, ratio, venues, mid, now):
        self.last_seen = now
        self.max_qty = max(self.max_qty, qty)
        self.max_ratio = max(self.max_ratio, ratio)
        self.venues_max = max(self.venues_max, venues)
        # count a "test": price comes within a tick of the wall then pulls back
        near = abs(mid - self.price) <= max(1.0, self.price * 0.0002)
        if near and not self._was_near:
            self.tests += 1
        self._was_near = near
        self.last_price_rel = mid - self.price

    @property
    def lifespan(self):
        return self.last_seen - self.first_seen

    def usd(self, ref_price=None):
        return self.max_qty * (ref_price or self.price)


class WallHistory:
    # --- mémoire LONG TERME : uniquement les murs qui comptent, gardés des semaines ---
    LONG_MIN_QTY = 25.0        # un mur de cette taille (BTC) est notable en soi
    LONG_ICE_MIN = 5.0         # volume absorbé mini pour parler d'iceberg

    def __init__(self, retention_s=3700, break_margin=15.0,
                 long_retention_s=21 * 24 * 3600):
        self.retention_s = retention_s          # keep ~1h+ of wall lives
        # HISTORIQUE LONG : le carnet n'a AUCUN historique public, donc la seule façon
        # d'avoir les niveaux des semaines passées est de les enregistrer nous-mêmes.
        # On ne garde QUE les murs significatifs (testés / gros / icebergs) — jamais le
        # bruit (spoofs, petits murs jamais touchés) — sinon le fichier explose.
        self.long_retention_s = long_retention_s
        self.longterm = deque(maxlen=30000)      # (ts_closed, WallRecord) significatifs
        # INVALIDATION : un mur est "cassé/invalidé" dès que le prix le TRAVERSE de
        # plus de break_margin dollars (résistance : prix > mur+marge ; support :
        # prix < mur-marge). Marge réglable en dollars.
        self.break_margin = break_margin
        self.active = {}                         # (price, side) -> WallRecord
        self.closed = deque(maxlen=5000)         # finished WallRecords (ts_closed, rec)

    def update(self, walls, mid, absorbed=None):
        """Feed the current detected walls (list of dicts) + mid price. `absorbed` =
        {prix_arrondi: volume agressif exécuté à ce niveau depuis le dernier tick}."""
        now = time.time()
        absorbed = absorbed or {}
        seen_keys = set()
        for w in walls:
            key = (round(w["price"], 1), w["side"])
            seen_keys.add(key)
            rec = self.active.get(key)
            if rec is None:
                rec = WallRecord(w["price"], w["side"], w["qty"], w["ratio"],
                                 w["venues"], now)
                self.active[key] = rec
            else:
                rec.update(w["qty"], w["ratio"], w["venues"], mid, now)
            rec.absorbed += absorbed.get(key[0], 0.0)

        # INVALIDATION explicite : le prix a-t-il traversé un mur de +break_margin$ ?
        # (on vérifie TOUS les murs suivis, même ceux qui viennent de disparaître)
        m = self.break_margin
        for rec in self.active.values():
            if rec.side == "ask" and mid >= rec.price + m:      # résistance percée par le haut
                rec.broken = True
            elif rec.side == "bid" and mid <= rec.price - m:    # support percé par le bas
                rec.broken = True

        # close walls no longer present
        for key in list(self.active):
            if key not in seen_keys:
                rec = self.active[key]
                if now - rec.last_seen > 1.5:    # gone for >1.5s -> closed
                    if rec.broken:
                        pass                     # déjà marqué cassé (prix a traversé)
                    elif rec.lifespan < 3.0 and abs(rec.last_price_rel or 1e9) > rec.price * 0.0003:
                        rec.pulled = True        # retiré vite, prix loin = spoof
                    self.closed.append((now, rec))
                    if self._is_significant(rec):     # -> mémoire longue (semaines)
                        self.longterm.append((now, rec))
                    self.active.pop(key, None)

        # purge de la mémoire longue (semaines)
        lcut = now - self.long_retention_s
        while self.longterm and self.longterm[0][1].last_seen < lcut:
            self.longterm.popleft()

        # purge very old closed records
        cutoff = now - self.retention_s
        while self.closed and self.closed[0][0] < cutoff:
            self.closed.popleft()

    def _is_significant(self, rec):
        """Ce mur mérite-t-il d'être gardé des SEMAINES ? On garde ce qui fait un vrai
        niveau : testé (donc validé ou cassé), iceberg, ou gros. On jette le bruit :
        spoofs et petits murs jamais touchés — sinon le fichier explose."""
        if rec.pulled:
            return False                          # spoof = bruit, jamais gardé
        if rec.tests >= 1:
            return True                           # le prix est venu le tester = vrai niveau
        if (rec.absorbed >= self.LONG_ICE_MIN
                and rec.absorbed >= 1.5 * max(rec.max_qty, 1e-9)):
            return True                           # iceberg : a absorbé sans céder
        return rec.max_qty >= self.LONG_MIN_QTY   # gros mur

    def peak_near(self, price, tol=8.0):
        """Retourne (pic BTC, taille actuelle BTC) du mur suivi le plus proche de
        `price` (dans ±tol$), ou None. Sert à mesurer combien un mur a fondu depuis
        son maximum : pic = plus grosse taille atteinte, actuel = taille la plus
        récente observée. Un mur toujours vivant a last_seen très récent."""
        best = None
        bd = tol
        for rec in self.active.values():
            d = abs(rec.price - price)
            if d <= bd:
                bd = d
                best = rec
        if best is None:
            return None
        # taille actuelle = max_qty n'est PAS l'actuel ; on renvoie le pic et on
        # laisse l'appelant fournir la taille live du carnet. Ici on ne dispose que
        # de max_qty côté historique, donc on renvoie le pic seul.
        return best.max_qty

    def _records_in_window(self, minutes):
        """Murs vivants dans la fenêtre. Fenêtre COURTE (<= rétention ~1h) : tout
        (active + closed). Fenêtre LONGUE (jours/semaines) : active + mémoire longue
        (= uniquement les murs significatifs) — pas de doublon, `closed` n'est pas relu."""
        now = time.time()
        window_start = now - minutes * 60
        recs = [rec for rec in self.active.values() if rec.last_seen >= window_start]
        source = self.closed if minutes * 60 <= self.retention_s else self.longterm
        for _ts, rec in source:
            if rec.last_seen >= window_start:
                recs.append(rec)
        return recs

    def report(self, minutes, mid=None, top_n=8, max_dist=None, cluster=0.1):
        recs = self._records_in_window(minutes)
        # filtre distance : ne garder que les murs à moins de max_dist $ du prix
        # (pour se concentrer sur les niveaux proches et actionnables)
        if max_dist and mid:
            recs = [r for r in recs if abs(r.price - mid) <= max_dist]
        if not recs:
            return {"ready": False, "minutes": minutes}

        # DÉ-DUPLICATION par niveau : un même (prix, côté) peut avoir plusieurs "vies"
        # (mur qui clignote : apparaît → disparaît >1.5s → réapparaît = nouveau record).
        # Sans ça, le même niveau s'affiche en 2-3 lignes identiques. On fusionne en UN
        # record : pic max, somme des tests, durée totale, ÉTAT LE PLUS RÉCENT.
        # `cluster` = largeur de la zone de regroupement en $. 0.1 = au prix exact
        # (fenêtres courtes) ; 25 = par zone (fenêtres longues), pour qu'un niveau
        # défendu 7 fois en 2 semaines ressorte comme UN niveau et pas 7 voisins.
        groups = {}
        for r in recs:
            groups.setdefault((round(r.price / cluster) * cluster, r.side), []).append(r)

        def _merge(group):
            if len(group) == 1:
                return group[0]
            latest = max(group, key=lambda r: r.last_seen)
            m = WallRecord(latest.price, latest.side, 0.0, 0.0, 1,
                           min(r.first_seen for r in group))
            m.max_qty = max(r.max_qty for r in group)
            m.max_ratio = max(r.max_ratio for r in group)
            m.venues_max = max(r.venues_max for r in group)
            m.tests = sum(r.tests for r in group)
            m.last_seen = latest.last_seen        # état courant = vie la plus récente
            m.broken = latest.broken
            m.pulled = latest.pulled
            m.last_price_rel = latest.last_price_rel
            m.absorbed = sum(r.absorbed for r in group)
            return m

        recs = [_merge(g) for g in groups.values()]

        # importance score: size * venues * sqrt(lifespan)
        def score(r):
            return r.max_qty * max(1, r.venues_max) * (max(1.0, r.lifespan) ** 0.5)

        ranked = sorted(recs, key=score, reverse=True)
        longest = max(recs, key=lambda r: r.lifespan)

        buy_walls = [r for r in recs if r.side == "bid"]
        sell_walls = [r for r in recs if r.side == "ask"]
        spoofs = [r for r in recs if r.pulled]
        held = [r for r in recs if not r.broken and not r.pulled]
        now = time.time()

        # ICEBERG : le mur a absorbé bien plus de volume agressif que sa taille
        # affichée SANS céder = ordre caché qui se recharge. À l'OPPOSÉ du spoof
        # (retiré sans être touché, ~0 volume absorbé) — jamais confondus.
        ICE_MULT = 1.5      # a absorbé ≥ 1.5× sa taille visible max
        ICE_MIN = 5.0       # et au moins 5 BTC (sinon ce n'est pas significatif)

        def is_iceberg(r):
            return (not r.broken and r.absorbed >= ICE_MIN
                    and r.absorbed >= ICE_MULT * max(r.max_qty, 1e-9))

        def classify(r):
            """Statut clair d'un mur : ACTIF / VALIDÉ / INVALIDÉ / SPOOF / DISPARU.
            (Le fait d'être un ICEBERG est une étiquette À PART, pas un statut : un
            iceberg peut être encore actif — on ne veut pas le sortir des ACTIFS.)"""
            if r.broken:
                return "invalide"       # le prix a TRAVERSÉ (prioritaire sur tout)
            if r.last_seen >= now - 1.6:
                return "actif"          # toujours présent et pas traversé
            if r.pulled:
                return "spoof"          # retiré vite sans être touché
            if r.tests >= 1:
                return "valide"         # testé au moins une fois et a tenu
            return "disparu"            # parti proprement sans avoir été testé

        def pack(r):
            return {
                "price": r.price, "side": r.side,
                "side_txt": "Support (achat)" if r.side == "bid" else "Résistance (vente)",
                "max_qty": r.max_qty,
                "usd": r.usd(mid),
                "venues": r.venues_max,
                "lifespan": r.lifespan,
                "tests": r.tests,
                "ratio": r.max_ratio,
                "broken": r.broken, "pulled": r.pulled,
                "dist": (mid - r.price) if mid else None,
                "active": r.last_seen >= now - 1.6,
                "status": classify(r),
                "iceberg": is_iceberg(r),
                "absorbed": round(r.absorbed, 1),
            }

        packed = [pack(r) for r in ranked]
        cats = {"actif": [], "valide": [], "invalide": [], "spoof": [], "disparu": []}
        for w in packed:
            cats[w["status"]].append(w)
        icebergs = [w for w in packed if w["iceberg"]]

        return {
            "ready": True, "minutes": minutes,
            "top": packed[:top_n],
            "longest": pack(longest),
            "n_total": len(recs),
            "n_buy": len(buy_walls), "n_sell": len(sell_walls),
            "n_spoof": len(spoofs), "n_held": len(held),
            "buy_liq": sum(r.max_qty for r in buy_walls),
            "sell_liq": sum(r.max_qty for r in sell_walls),
            # classification par statut (chaque liste déjà triée par importance)
            "categories": cats,
            "n_actif": len(cats["actif"]), "n_valide": len(cats["valide"]),
            "n_invalide": len(cats["invalide"]), "n_spoof2": len(cats["spoof"]),
            # ICEBERGS : étiquette à part (peuvent être actifs), jamais des spoofs
            "icebergs": icebergs, "n_iceberg": len(icebergs),
        }

    # -----------------------------------------------------------------------
    # PERSISTANCE : l'historique des murs (âges, tests, cassures) survit aux
    # redémarrages. Le carnet n'a pas d'historique côté exchange, mais CE que le
    # logiciel a observé est sauvegardé sur disque et rechargé au lancement.
    # -----------------------------------------------------------------------
    _FIELDS = ("price", "side", "first_seen", "last_seen", "max_qty", "max_ratio",
               "venues_max", "tests", "broken", "pulled", "last_price_rel", "absorbed")

    def _rec_to_dict(self, r):
        return {f: getattr(r, f) for f in self._FIELDS}

    def _dict_to_rec(self, d):
        r = WallRecord(d["price"], d["side"], d["max_qty"], d["max_ratio"],
                       d["venues_max"], d["first_seen"])
        r.last_seen = d["last_seen"]
        r.tests = d.get("tests", 0)
        r.broken = d.get("broken", False)
        r.pulled = d.get("pulled", False)
        r.last_price_rel = d.get("last_price_rel")
        r.absorbed = d.get("absorbed", 0.0)
        return r

    def save(self, path):
        """Écrit l'historique sur disque (atomique). Appelé périodiquement + à l'arrêt."""
        try:
            data = {
                "saved_at": time.time(),
                "active": [self._rec_to_dict(r) for r in self.active.values()],
                "closed": [(ts, self._rec_to_dict(r)) for ts, r in self.closed],
                # mémoire longue : les niveaux significatifs des dernières semaines
                "longterm": [(ts, self._rec_to_dict(r)) for ts, r in self.longterm],
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            pass    # une sauvegarde ratée ne doit jamais casser le moteur

    def load(self, path):
        """Recharge l'historique au lancement. Les murs trop vieux (appli fermée
        longtemps) sont ignorés — ils s'expireront de toute façon. Les murs récents
        reprennent leur vie ; s'ils sont toujours dans le carnet live, update() les
        ré-associe par (prix, côté) et continue leur âge/tests sans coupure."""
        try:
            if not os.path.exists(path):
                return 0
            with open(path) as f:
                data = json.load(f)
        except Exception:
            return 0
        cutoff = time.time() - self.retention_s
        n = 0
        for ts, d in data.get("closed", []):
            try:
                if d["last_seen"] >= cutoff:
                    self.closed.append((ts, self._dict_to_rec(d))); n += 1
            except (KeyError, TypeError):
                continue
        for d in data.get("active", []):
            try:
                if d["last_seen"] >= cutoff:
                    rec = self._dict_to_rec(d)
                    self.active[(round(rec.price, 1), rec.side)] = rec; n += 1
            except (KeyError, TypeError):
                continue
        # mémoire longue : rétention BEAUCOUP plus large (semaines)
        lcut = time.time() - self.long_retention_s
        for ts, d in data.get("longterm", []):
            try:
                if d["last_seen"] >= lcut:
                    self.longterm.append((ts, self._dict_to_rec(d))); n += 1
            except (KeyError, TypeError):
                continue
        return n

    def build_analysis(self, rep):
        """Plain-French interpretation of a window's wall report."""
        if not rep.get("ready"):
            return []
        out = []
        n = rep["n_total"]; spoof = rep["n_spoof"]
        # spoof ratio
        if n > 0:
            sr = spoof / n
            if sr > 0.4:
                out.append(("alerte", f"Beaucoup de faux murs : {spoof}/{n} murs ont été "
                    "retirés rapidement (spoofing). Le carnet est manipulé sur cette période — "
                    "ne te fie qu'aux murs présents sur 2-3 venues et qui durent."))
            elif spoof > 0:
                out.append(("neutre", f"{spoof} mur(s) retiré(s) vite sur {n} (un peu de "
                    "spoofing, normal en crypto). Le reste tient mieux."))
            else:
                out.append(("hausse", f"Aucun spoof flagrant sur {n} murs : la liquidité "
                    "affichée est plutôt honnête sur cette période."))

        # buy vs sell wall balance
        bl = rep["buy_liq"]; sl = rep["sell_liq"]
        if bl + sl > 0:
            if bl > sl * 1.4:
                out.append(("hausse", f"Bien plus de liquidité en SUPPORT (achat) qu'en "
                    f"résistance ({bl:,.0f} vs {sl:,.0f} BTC). Le carnet protège le bas : "
                    "les baisses sont amorties, biais plutôt haussier."))
            elif sl > bl * 1.4:
                out.append(("baisse", f"Bien plus de liquidité en RÉSISTANCE (vente) qu'en "
                    f"support ({sl:,.0f} vs {bl:,.0f} BTC). Le carnet plafonne le haut : "
                    "les hausses butent, biais plutôt baissier."))
            else:
                out.append(("neutre", f"Liquidité équilibrée support/résistance "
                    f"({bl:,.0f} vs {sl:,.0f} BTC) : pas de penchant clair du carnet."))

        # the longest wall
        lg = rep["longest"]
        out.append(("neutre", f"Mur le plus tenace : {lg['side_txt']} à {lg['price']:,.0f}, "
            f"resté {lg['lifespan']:.0f}s, taille max {lg['max_qty']:.1f} BTC "
            f"(~{lg['usd']/1e6:.1f} M$), testé {lg['tests']} fois. "
            + ("Toujours actif." if lg['active'] else
               ("Finalement cassé (le prix est passé au travers)." if lg['broken'] else
                "Retiré." if lg['pulled'] else "Disparu."))))

        # tested walls = battle zones
        tested = [w for w in rep["top"] if w["tests"] >= 2]
        if tested:
            t = max(tested, key=lambda w: w["tests"])
            out.append(("alerte", f"Zone de bataille : le mur à {t['price']:,.0f} a été "
                f"testé {t['tests']} fois. Un niveau attaqué plusieurs fois et qui tient = "
                "solide ; mais plus il est testé, plus il risque de finir par céder."))
        return out
