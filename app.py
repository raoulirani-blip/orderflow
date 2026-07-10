"""
app.py — Multi-exchange order-flow cockpit (PyQt6 + pyqtgraph), v3.

De-biased BTC perpetual order flow across Binance + OKX + Bybit, built for an
intraday technical trader adding Level 2. Every signal is explained so you learn
the order-flow read and fuse it with your own TA.

Panels:
  - Top: aggregated MID/SPREAD/IMBALANCE/CVD/TAPE/AGRESSEURS + per-venue status.
  - Left: aggregated DOM ladder (with venue-count per level) + pressure bar.
  - Center: SIGNAUX — explained order-flow signals + overall BIAIS.
  - Right: aggregated liquidity heatmap, walls (with N/3 venue confluence),
    live events feed.

Run:  py -3.12 app.py
"""

import sys
import numpy as np
from PyQt6 import QtCore, QtWidgets, QtGui
import pyqtgraph as pg

from engine import OrderFlowEngine, VENUES, N_VENUES
from analysis import SlowAnalyzer


# TradingView intégré (nécessite PyQt6-WebEngine ; import AVANT QApplication)
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except ImportError:
    HAS_WEBENGINE = False


class Bridge(QtCore.QObject):
    state = QtCore.pyqtSignal(dict)


TRADINGVIEW_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;height:100%;background:#0a0d12;}</style>
</head><body>
<div id="tv" style="height:100vh;width:100%;"></div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
new TradingView.widget({
  container_id: "tv",
  autosize: true,
  symbol: "BINANCE:BTCUSDT.P",
  interval: "1",
  timezone: "Etc/UTC",
  theme: "dark",
  style: "1",
  locale: "fr",
  hide_side_toolbar: false,
  allow_symbol_change: true,
  withdateranges: true,
  studies: ["VWAP@tv-basicstudies", "Volume@tv-basicstudies"]
});
</script>
</body></html>"""


BG="#0a0d12"; PANEL="#10151e"; PANEL2="#151b26"; BORDER="#1d2531"
TXT="#d8e0ea"; DIM="#69748a"; GREEN="#2ec27e"; RED="#f0494f"
ACCENT="#5aa0ff"; AMBER="#f5a623"; VIOLET="#a371f7"

pg.setConfigOptions(antialias=True, background=BG, foreground=TXT)

EXPLAIN = {
    "MID":f"Prix milieu agrege des {N_VENUES} exchanges.",
    "SPREAD":"Ecart meilleur achat/vente agrege. Petit = liquide.",
    "IMBALANCE":"Desequilibre du carnet agrege. >50% = pression acheteuse passive.",
    "CVD":f"Delta cumule ({N_VENUES} venues). Monte = achat net au marche.",
    "TAPE":f"Trades/seconde sur les {N_VENUES} venues. Eleve = volatil.",
    "AGRESSEURS":"Part des acheteurs au marche sur 5s, toutes venues.",
}
VENUE_COL = {"binance":"#f3ba2f","okx":"#5aa0ff","bybit":"#f7a600","hyperliquid":"#50d2c2"}


class Cockpit(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Order Flow Cockpit — BTC perp (Binance + OKX + Bybit)")
        self.resize(1620, 980)
        self.setStyleSheet(f"QMainWindow{{background:{BG};}}")
        self.bridge = Bridge()
        self.bridge.state.connect(self.on_state)
        self.engine = OrderFlowEngine(on_update=lambda s: self.bridge.state.emit(s))
        self.analyzer = SlowAnalyzer(verdict_window_s=10.0, zone_persist_s=3.0)
        self._last_state = None
        # copilote IA (créé AVANT l'UI car l'onglet IA lit sa config)
        from ai_copilot import AICopilot
        self.copilot = AICopilot(daily_budget_usd=2.20)
        self._ai_last_auto = 0.0
        self._ai_last_event = 0.0
        # notifier + config d'alertes créés AVANT l'UI (la page ALERTES lit la config)
        from alerts import Notifier
        self.notifier = Notifier(min_interval=25.0)
        self._alert_cfg = self._alerts_load_cfg()
        self._apply_notifier_cfg()
        self._alert_state = {}
        self._tape_ema = None
        self._tg_bot = None
        self._build_ui()
        # (le bot Telegram tourne sur le serveur, pas ici — voir server.py)
        self.engine.start()
        # Periodic bilans: each window refreshes at its own rhythm.
        self.WINDOWS = [5, 15, 30, 60]   # minutes
        self._report_timers = {}
        self._next_refresh = {}          # minutes -> epoch of next refresh
        import time as _t
        for m in self.WINDOWS:
            tm = QtCore.QTimer(self)
            tm.timeout.connect(lambda mm=m: self._refresh_window(mm))
            tm.start(m * 60 * 1000)      # 5min->5min, 15->15, etc.
            self._report_timers[m] = tm
            self._next_refresh[m] = _t.time() + m * 60
        # countdown ticker (updates the "prochain refresh dans X" labels every 1s)
        self._countdown_timer = QtCore.QTimer(self)
        self._countdown_timer.timeout.connect(self._tick_countdowns)
        self._countdown_timer.start(1000)
        # first fill shortly after launch so pages aren't empty
        for m in self.WINDOWS:
            QtCore.QTimer.singleShot(8000, lambda mm=m: self._refresh_window(mm))
        # walls page: refresh live
        self._wall_timer = QtCore.QTimer(self)
        self._wall_timer.timeout.connect(self._refresh_walls)
        self._wall_timer.start(1000)
        QtCore.QTimer.singleShot(6000, self._refresh_walls)

        # --- Timers pages PRO (quasi temps réel : les calculs font <3ms) ---
        self._vwap_timer = QtCore.QTimer(self)
        self._vwap_timer.timeout.connect(self._refresh_vwap)
        self._vwap_timer.start(300)
        QtCore.QTimer.singleShot(5000, self._refresh_vwap)

        self._instit_timer = QtCore.QTimer(self)
        self._instit_timer.timeout.connect(self._refresh_instit)
        self._instit_timer.start(500)
        QtCore.QTimer.singleShot(7000, self._refresh_instit)

        self._profil_timer = QtCore.QTimer(self)
        self._profil_timer.timeout.connect(self._refresh_profil)
        self._profil_timer.start(500)
        QtCore.QTimer.singleShot(9000, self._refresh_profil)

        self._pos_timer = QtCore.QTimer(self)
        self._pos_timer.timeout.connect(self._refresh_pos)
        self._pos_timer.start(1000)
        QtCore.QTimer.singleShot(8000, self._refresh_pos)

        self._exec_timer = QtCore.QTimer(self)
        self._exec_timer.timeout.connect(self._refresh_exec)
        self._exec_timer.start(1000)

        # alertes sonores : mémorise les événements déjà signalés
        self._sound_seen = set()
        self._sound_init = False

        # ALERTES & BOT TELEGRAM : DÉSACTIVÉS côté PC — c'est le serveur cloud 24/7
        # qui s'en occupe (server.py). On ne lance NI le moteur d'alertes NI le bot
        # ici, pour qu'il soit IMPOSSIBLE d'avoir des doublons ou un conflit Telegram,
        # même si l'appli PC est ouverte en même temps que le serveur.

        # flux news (thread de fond, rafraîchi toutes les 5 min)
        from news import NewsFeed
        self.newsfeed = NewsFeed(refresh_s=300)
        self.newsfeed.start()
        self._news_timer = QtCore.QTimer(self)
        self._news_timer.timeout.connect(self._refresh_news)
        self._news_timer.start(30000)          # l'UI relit le cache toutes les 30s
        QtCore.QTimer.singleShot(4000, self._refresh_news)

        # copilote IA : timer d'affichage + mode auto
        self._ai_timer = QtCore.QTimer(self)
        self._ai_timer.timeout.connect(self._ai_tick)
        self._ai_timer.start(1000)

    def _build_ui(self):
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setStyleSheet(
            f"QTabWidget::pane{{border:1px solid {BORDER};border-radius:8px;background:{BG};}}"
            f"QTabBar::tab{{background:{PANEL2};color:{DIM};padding:10px 22px;margin-right:4px;"
            f"border-top-left-radius:8px;border-top-right-radius:8px;font-weight:700;font-size:13px;}}"
            f"QTabBar::tab:selected{{background:{ACCENT};color:#08111f;}}")
        self.setCentralWidget(self.tabs)

        # --- Page 1: live cockpit ---
        live = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(live)
        root.setContentsMargins(12,12,12,12); root.setSpacing(10)
        root.addLayout(self._topbar())
        body = QtWidgets.QHBoxLayout(); body.setSpacing(10); root.addLayout(body,1)
        body.addLayout(self._left(),20)
        body.addLayout(self._center(),32)
        body.addLayout(self._right(),48)
        self.tabs.addTab(live, "  📊  DIRECT  ")

        # --- Page 3: walls study ---
        self.tabs.addTab(self._build_walls_page(), "  🧱  MURS  ")

        # --- Page 4: VWAP + CVD multi-fenêtres ---
        self.tabs.addTab(self._build_vwap_page(), "  📈  VWAP & CVD  ")

        # --- Page 5: Flux institutionnels ---
        self.tabs.addTab(self._build_instit_page(), "  🏦  INSTITUTIONNELS  ")

        # --- Page 6: Profil de volume + sweeps + stacked ---
        self.tabs.addTab(self._build_profil_page(), "  📊  PROFIL  ")

        # --- Page 7: OI + Funding + Liquidations ---
        self.tabs.addTab(self._build_pos_page(), "  🧭  POSITIONNEMENT  ")

        # --- Calculateur de position (money management) ---
        self.tabs.addTab(self._build_calc_page(), "  🧮  CALCULATEUR  ")

        # --- Journal de trades ---
        self.tabs.addTab(self._build_journal_page(), "  📓  JOURNAL  ")

        # (page ALERTES retirée : les alertes tournent sur le serveur cloud 24/7,
        #  pilotées depuis Telegram ; les niveaux surveillés = ceux de la page EXÉCUTION)

        # --- Page 8: News crypto + macro ---
        self.tabs.addTab(self._build_news_page(), "  🗞️  NEWS & MACRO  ")

        # --- Page 9: Copilote IA ---
        self.tabs.addTab(self._build_ai_page(), "  🤖  IA  ")

        # --- Page 10: Exécution des setups ---
        self.tabs.insertTab(1, self._build_exec_page(), "  🎯  EXÉCUTION  ")

    def _build_bilans_section(self):
        """Section BILANS PÉRIODIQUES (5/15/30/60 min) — autrefois dans la page
        ANALYSE (supprimée), désormais intégrée en bas de POSITIONNEMENT."""
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(0, 6, 0, 0); outer.setSpacing(8)

        # --- BILANS PÉRIODIQUES : 4 sous-onglets (5/15/30/60 min) ---
        bcap = QtWidgets.QLabel("BILANS PÉRIODIQUES  ·  chaque durée se met à jour à son propre rythme")
        bcap.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:0.5px;margin-top:6px;")
        outer.addWidget(bcap)

        self.bilan_tabs = QtWidgets.QTabWidget()
        self.bilan_tabs.setStyleSheet(
            f"QTabWidget::pane{{border:1px solid {BORDER};border-radius:8px;background:{BG};}}"
            f"QTabBar::tab{{background:{PANEL2};color:{DIM};padding:7px 18px;margin-right:3px;"
            f"border-top-left-radius:7px;border-top-right-radius:7px;font-weight:700;font-size:12px;}}"
            f"QTabBar::tab:selected{{background:{AMBER};color:#1a1205;}}")
        outer.addWidget(self.bilan_tabs, 2)

        # one page per window; keep references to its widgets
        self.bilan_widgets = {}   # minutes -> dict(header, stats, fine, para)
        for m in [5, 15, 30, 60]:
            bp = QtWidgets.QWidget()
            pl = QtWidgets.QVBoxLayout(bp); pl.setContentsMargins(10,10,10,10); pl.setSpacing(8)

            # header line: last update + next refresh countdown
            header = QtWidgets.QLabel("En attente du premier calcul…")
            header.setStyleSheet(f"color:{ACCENT};font-size:12px;font-weight:700;"
                                 f"background:{PANEL};border:1px solid {BORDER};border-radius:8px;padding:8px;")
            pl.addWidget(header)

            row = QtWidgets.QHBoxLayout(); row.setSpacing(10); pl.addLayout(row, 1)
            stats = QtWidgets.QTextEdit(); stats.setReadOnly(True)
            stats.setStyleSheet(f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
                                f"border-radius:12px;color:{TXT};font-size:13px;padding:12px;}}")
            row.addWidget(stats, 1)
            fine = QtWidgets.QTextEdit(); fine.setReadOnly(True)
            fine.setStyleSheet(f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
                               f"border-radius:12px;color:{TXT};font-size:13px;padding:12px;}}")
            row.addWidget(fine, 1)

            para = QtWidgets.QTextEdit(); para.setReadOnly(True); para.setMaximumHeight(120)
            para.setStyleSheet(f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
                               f"border-radius:12px;color:{TXT};font-size:14px;padding:12px;}}")
            pl.addWidget(para)

            stats.setHtml(f"<span style='color:{DIM};'>Accumulation des données… "
                          f"le bilan {m} min se remplit en laissant tourner l'appli.</span>")
            fine.setHtml(f"<span style='color:{DIM};'>L'analyse fine apparaîtra ici.</span>")
            para.setHtml(f"<span style='color:{DIM};'>Résumé…</span>")

            self.bilan_tabs.addTab(bp, f"  {m} min  ")
            self.bilan_widgets[m] = {"header": header, "stats": stats,
                                     "fine": fine, "para": para}
        return page

    def _build_walls_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16,16,16,16); outer.setSpacing(10)

        intro = QtWidgets.QLabel(
            "Étude des murs. Choisis une fenêtre de temps (1/5/15/30/60 min), puis un "
            "sous-onglet : 📋 TOUS · 🟢 ACTIFS (présents maintenant) · ✅ VALIDÉS (ont tenu "
            "puis partis = niveaux de support/résistance à re-surveiller dans le futur) · "
            "🔴 INVALIDÉS (cassés + spoofs). Rappel : N/4 = sur combien d'exchanges le mur "
            "est visible (le plus souvent 1 car seul Binance publie un carnet profond).")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # sélecteur de distance : focalise sur les murs proches du prix
        drow = QtWidgets.QHBoxLayout(); drow.setSpacing(8)
        dlbl = QtWidgets.QLabel("DISTANCE AU PRIX :")
        dlbl.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:0.5px;")
        drow.addWidget(dlbl)
        self.wall_dist_combo = QtWidgets.QComboBox()
        self.WALL_DISTS = {"± 100 $ (proche)": 100, "± 250 $": 250,
                           "± 500 $": 500, "± 1000 $": 1000, "Tout": None}
        self.wall_dist_combo.addItems(list(self.WALL_DISTS.keys()))
        self.wall_dist_combo.setCurrentText("± 250 $")
        self.wall_dist_combo.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:6px 12px;font-weight:700;}}")
        drow.addWidget(self.wall_dist_combo)
        dnote = QtWidgets.QLabel("· proche = actionnable")
        dnote.setStyleSheet(f"color:{DIM};font-size:11px;")
        drow.addWidget(dnote)
        # sélecteur de tri (décroissant)
        slbl = QtWidgets.QLabel("   TRIER PAR :")
        slbl.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:0.5px;")
        drow.addWidget(slbl)
        self.wall_sort_combo = QtWidgets.QComboBox()
        # label -> (clé du dict mur, décroissant)
        self.WALL_SORTS = {
            "Proximité (+ proche)": "dist",
            "Importance": None,
            "Taille (BTC) ↓": "max_qty",
            "Valeur ($) ↓": "usd",
            "Prix ↓": "price",
            "Durée ↓": "lifespan",
            "Tests ↓": "tests",
        }
        self.wall_sort_combo.addItems(list(self.WALL_SORTS.keys()))
        self.wall_sort_combo.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:6px 12px;font-weight:700;}}")
        drow.addWidget(self.wall_sort_combo)
        drow.addStretch()
        outer.addLayout(drow)

        self.wall_tabs = QtWidgets.QTabWidget()
        self.wall_tabs.setStyleSheet(
            f"QTabWidget::pane{{border:1px solid {BORDER};border-radius:8px;background:{BG};}}"
            f"QTabBar::tab{{background:{PANEL2};color:{DIM};padding:8px 20px;margin-right:3px;"
            f"border-top-left-radius:7px;border-top-right-radius:7px;font-weight:700;font-size:12px;}}"
            f"QTabBar::tab:selected{{background:{VIOLET};color:#0c0716;}}")
        outer.addWidget(self.wall_tabs, 1)

        self.WALL_WINDOWS = [1, 5, 15, 30, 60]
        self.wall_widgets = {}
        for m in self.WALL_WINDOWS:
            wp = QtWidgets.QWidget()
            pl = QtWidgets.QVBoxLayout(wp); pl.setContentsMargins(10,10,10,10); pl.setSpacing(8)

            header = QtWidgets.QLabel("En attente…")
            header.setStyleSheet(f"color:{VIOLET};font-size:12px;font-weight:700;"
                                 f"background:{PANEL};border:1px solid {BORDER};border-radius:8px;padding:8px;")
            pl.addWidget(header)

            # longest wall highlight
            longest = QtWidgets.QLabel("…")
            longest.setWordWrap(True)
            longest.setStyleSheet(f"color:{TXT};font-size:13px;font-weight:600;"
                                  f"background:{PANEL2};border:1px solid {BORDER};border-radius:8px;padding:10px;")
            pl.addWidget(longest)

            # bandeau de classification : compteurs par statut
            catbar = QtWidgets.QLabel("…")
            catbar.setStyleSheet(f"color:{TXT};font-size:12px;font-weight:700;"
                                 f"background:{PANEL2};border:1px solid {BORDER};"
                                 f"border-radius:8px;padding:8px;")
            pl.addWidget(catbar)

            def mk_table(cols):
                t = QtWidgets.QTableWidget(0, len(cols))
                t.setHorizontalHeaderLabels(cols)
                self._prep(t)
                return t

            # sous-onglets : un tableau plein écran par catégorie
            sub = QtWidgets.QTabWidget()
            sub.setStyleSheet(
                f"QTabWidget::pane{{border:1px solid {BORDER};border-radius:8px;background:{BG};}}"
                f"QTabBar::tab{{background:{PANEL2};color:{DIM};padding:7px 22px;margin-right:3px;"
                f"border-top-left-radius:7px;border-top-right-radius:7px;font-weight:700;font-size:12px;}}"
                f"QTabBar::tab:selected{{background:{ACCENT};color:#08111f;}}")

            table = mk_table(["Statut","Côté","Prix","Taille BTC","Valeur $","Durée","Tests"])
            sub.addTab(table, "  📋  TOUS LES MURS  ")
            solid = mk_table(["Statut","Côté","Prix","Taille BTC","Valeur $","Tests","Durée"])
            sub.addTab(solid, "  🟢  ACTIFS (présents)  ")
            valid = mk_table(["Côté","Prix","Taille max BTC","Valeur $","Tests","A tenu (s)"])
            sub.addTab(valid, "  ✅  VALIDÉS (ont tenu → niveaux futurs)  ")
            broken = mk_table(["Côté","Prix","Taille BTC","Valeur $","Sort","Durée","Tests"])
            sub.addTab(broken, "  🔴  INVALIDÉS  ")
            # 4e sous-onglet : pour chaque mur, ce qui a été EXÉCUTÉ (acheté/vendu
            # autour ±30$, 1h) vs ce qui reste EN ATTENTE (taille live du mur / pic).
            flux = mk_table(["Côté","Prix","En attente (mur)","Exécuté ACHAT",
                             "Exécuté VENTE","Net","Lecture"])
            sub.addTab(flux, "  💰  EXÉCUTÉ / EN ATTENTE  ")
            pl.addWidget(sub, 1)

            self.wall_tabs.addTab(wp, f"  {m} min  ")
            self.wall_widgets[m] = {"header": header, "longest": longest, "catbar": catbar,
                                    "table": table, "solid": solid, "valid": valid,
                                    "broken": broken, "flux": flux}
        return page

    def _topbar(self):
        bar = QtWidgets.QHBoxLayout(); bar.setSpacing(8)
        # venue status pills
        self.venue_pills = {}
        for v in VENUES:
            p = QtWidgets.QLabel(v.upper())
            p.setFixedHeight(40); p.setMinimumWidth(86)
            p.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            p.setStyleSheet(self._pill_css(v, False))
            self.venue_pills[v] = p
            bar.addWidget(p)
        sep = QtWidgets.QFrame(); sep.setFixedWidth(1); sep.setStyleSheet(f"background:{BORDER};")
        bar.addWidget(sep)
        self.s_mid=self._stat("MID","—"); self.s_spread=self._stat("SPREAD","—")
        self.s_imb=self._stat("IMBALANCE","—"); self.s_cvd=self._stat("CVD","—")
        self.s_tape=self._stat("TAPE","—"); self.s_agg=self._stat("AGRESSEURS","—")
        for w in (self.s_mid,self.s_spread,self.s_imb,self.s_cvd,self.s_tape,self.s_agg):
            bar.addWidget(w)
        bar.addStretch()
        return bar

    def _pill_css(self, venue, ok):
        c = VENUE_COL.get(venue, ACCENT)
        if ok:
            return (f"QLabel{{background:{c}22;color:{c};border:1px solid {c};"
                    f"border-radius:8px;font-weight:800;font-size:12px;}}")
        return (f"QLabel{{background:{PANEL2};color:{DIM};border:1px solid {BORDER};"
                f"border-radius:8px;font-weight:700;font-size:12px;}}")

    def _left(self):
        col = QtWidgets.QVBoxLayout(); col.setSpacing(10)
        col.addWidget(self._h("CARNET AGRÉGÉ (DOM)  ·  pts = nb venues"))
        self.ladder = QtWidgets.QTableWidget(32,4)
        self.ladder.setHorizontalHeaderLabels(["Prix","Taille","Cumul","Venues"])
        self._prep(self.ladder)
        col.addWidget(self.ladder,1)
        col.addWidget(self._h("PRESSION  (achat ▸)"))
        self.imb=QtWidgets.QProgressBar(); self.imb.setRange(0,1000)
        self.imb.setFormat("%p‰"); self.imb.setFixedHeight(26)
        col.addWidget(self.imb)
        # per-venue contribution
        chdr = self._h("LIQUIDITÉ PRÈS DU PRIX PAR VENUE  ·  à profondeur égale")
        chdr.setToolTip("Part de la liquidité affichée près du prix, comparée dans une bande "
                        "commune que les 4 exchanges couvrent tous (comparaison équitable, "
                        "sans biais de profondeur de publication). Mesure la liquidité "
                        "AFFICHÉE, pas le volume échangé.")
        col.addWidget(chdr)
        self.contrib = QtWidgets.QLabel("…")
        self.contrib.setStyleSheet(f"color:{TXT};font-size:12px;border:1px solid {BORDER};"
                                   f"border-radius:8px;background:{PANEL};padding:8px;")
        col.addWidget(self.contrib)
        return col

    def _center(self):
        col = QtWidgets.QVBoxLayout(); col.setSpacing(10)
        self.bias_box = QtWidgets.QFrame()
        self.bias_box.setStyleSheet(f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:12px;}}")
        bl=QtWidgets.QVBoxLayout(self.bias_box); bl.setContentsMargins(18,14,18,14); bl.setSpacing(4)
        cap=QtWidgets.QLabel("SYNTHÈSE ORDER FLOW")
        cap.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:1.5px;border:none;")
        self.bias_label=QtWidgets.QLabel("…")
        self.bias_label.setStyleSheet(f"color:{TXT};font-size:25px;font-weight:800;border:none;")
        self.bias_sub=QtWidgets.QLabel(""); self.bias_sub.setWordWrap(True)
        self.bias_sub.setStyleSheet(f"color:{DIM};font-size:12px;border:none;")
        bl.addWidget(cap); bl.addWidget(self.bias_label); bl.addWidget(self.bias_sub)
        col.addWidget(self.bias_box)
        col.addWidget(self._h("SIGNAUX EXPLIQUÉS  (chaque signal + pourquoi)"))
        self.signals = QtWidgets.QTextEdit(); self.signals.setReadOnly(True)
        self.signals.setStyleSheet(f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
                                   f"border-radius:12px;color:{TXT};font-size:13px;padding:10px;}}")
        col.addWidget(self.signals,1)
        return col

    def _right(self):
        col = QtWidgets.QVBoxLayout(); col.setSpacing(10)
        # entête heatmap + boutons de zoom prix
        hm_head = QtWidgets.QHBoxLayout(); hm_head.setSpacing(6)
        hm_head.addWidget(self._h("HEATMAP LIQUIDITÉ  ·  bleu=mid · orange=VWAP · violet=POC"))
        hm_head.addStretch()
        self.hm_zoom_lbl = QtWidgets.QLabel("±2.0%")
        self.hm_zoom_lbl.setStyleSheet(f"color:{ACCENT};font-size:11px;font-weight:700;")
        btn_css = (f"QPushButton{{background:{PANEL2};color:{TXT};border:1px solid {BORDER};"
                   f"border-radius:6px;font-weight:800;padding:2px 10px;}}"
                   f"QPushButton:hover{{border:1px solid {ACCENT};}}")
        zin = QtWidgets.QPushButton("🔍+"); zin.setStyleSheet(btn_css)
        zin.setToolTip("Zoomer (fourchette de prix plus serrée)")
        zin.clicked.connect(lambda: self._hm_zoom(0.5))
        zout = QtWidgets.QPushButton("🔍−"); zout.setStyleSheet(btn_css)
        zout.setToolTip("Dézoomer (fourchette de prix plus large)")
        zout.clicked.connect(lambda: self._hm_zoom(2.0))
        hm_head.addWidget(self.hm_zoom_lbl); hm_head.addWidget(zin); hm_head.addWidget(zout)
        col.addLayout(hm_head)
        self.hm=pg.PlotWidget(); self.hm.setLabel("left","Prix"); self.hm.setLabel("bottom","Temps →")
        self.hm.showGrid(x=False,y=True,alpha=0.12)
        self.hm_img=pg.ImageItem(); self.hm.addItem(self.hm_img)
        self.mid_line=pg.InfiniteLine(angle=0,pen=pg.mkPen(ACCENT,width=1,style=QtCore.Qt.PenStyle.DashLine))
        self.hm.addItem(self.mid_line)
        # lignes VWAP (orange) et POC (violet) mises à jour par les timers lents
        self.vwap_line=pg.InfiniteLine(angle=0,pen=pg.mkPen(AMBER,width=1))
        self.vwap_line.setVisible(False); self.hm.addItem(self.vwap_line)
        self.poc_line=pg.InfiniteLine(angle=0,pen=pg.mkPen(VIOLET,width=1,style=QtCore.Qt.PenStyle.DotLine))
        self.poc_line.setVisible(False); self.hm.addItem(self.poc_line)
        # petits labels de prix aux niveaux les plus chargés en ordres (liquidité)
        self.hm_labels = []
        _hf = QtGui.QFont(); _hf.setPointSize(8)
        for _ in range(6):
            t = pg.TextItem(color=(240, 240, 245), anchor=(0, 0.5), fill=(0, 0, 0, 150))
            t.textItem.setFont(_hf)
            t.setVisible(False); t.setZValue(20)
            self.hm.addItem(t)
            self.hm_labels.append(t)
        self.hm_img.setLookupTable(pg.colormap.get("inferno").getLookupTable(0.0,1.0,256))
        col.addWidget(self.hm,5)
        two=QtWidgets.QHBoxLayout(); two.setSpacing(10); col.addLayout(two,4)
        wc=QtWidgets.QVBoxLayout(); wc.setSpacing(6)
        wc.addWidget(self._h("MURS  ·  gros niveaux de liquidité près du prix"))
        self.walls=QtWidgets.QTableWidget(0,4)
        self.walls.setHorizontalHeaderLabels(["Côté","Prix","Taille","âge"])
        self._prep(self.walls)
        wc.addWidget(self.walls,1); two.addLayout(wc,1)
        ec=QtWidgets.QVBoxLayout(); ec.setSpacing(6)
        ec.addWidget(self._h("ÉVÉNEMENTS  (murs · sweeps)"))
        self.events=QtWidgets.QTextEdit(); self.events.setReadOnly(True)
        self.events.setStyleSheet(f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
                                  f"border-radius:10px;color:{TXT};font-size:12px;padding:8px;}}")
        ec.addWidget(self.events,1); two.addLayout(ec,1)
        return col

    def _h(self,t):
        l=QtWidgets.QLabel(t); l.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:0.5px;")
        return l

    def _stat(self,label,value,color=TXT):
        w=QtWidgets.QFrame(); w.setStyleSheet(f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:10px;}}")
        if label in EXPLAIN: w.setToolTip(EXPLAIN[label])
        lay=QtWidgets.QVBoxLayout(w); lay.setContentsMargins(13,7,13,7); lay.setSpacing(0)
        l1=QtWidgets.QLabel(label); l1.setStyleSheet(f"color:{DIM};font-size:9px;font-weight:700;letter-spacing:1px;border:none;")
        l2=QtWidgets.QLabel(value); l2.setStyleSheet(f"color:{color};font-size:17px;font-weight:800;border:none;")
        lay.addWidget(l1); lay.addWidget(l2); w._value=l2
        return w

    def _prep(self,t):
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        t.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        t.setStyleSheet(f"QTableWidget{{background:{PANEL};gridline-color:{BORDER};color:{TXT};"
                        f"font-size:12px;border:1px solid {BORDER};border-radius:10px;}}"
                        f"QHeaderView::section{{background:{PANEL2};color:{DIM};padding:5px;"
                        f"border:none;font-size:9px;font-weight:700;}}")

    def _col(self,tag):
        return {"hausse":GREEN,"baisse":RED,"alerte":AMBER,"neutre":DIM}.get(tag,TXT)

    def _hset(self, widget, html):
        """setHtml seulement si le contenu a changé — évite de casser le
        défilement quand on rafraîchit plusieurs fois par seconde."""
        if getattr(widget, "_last_html", None) != html:
            widget._last_html = html
            widget.setHtml(html)

    def on_state(self,s):
        self._last_state = s
        self.analyzer.feed(s)
        if "error" in s:
            self.bias_label.setText("ERREUR"); self.bias_sub.setText(s["error"][:120]); return
        # venue pills
        for v in VENUES:
            ok = s.get("status",{}).get(v)=="ok"
            self.venue_pills[v].setStyleSheet(self._pill_css(v, ok))
        if s.get("warming"):
            self.bias_label.setText("Connexion…")
            self.bias_sub.setText("Synchronisation des carnets Binance / OKX / Bybit…")
            return

        self.s_mid._value.setText(f"{s['mid']:,.1f}")
        self.s_spread._value.setText(f"{s['spread']:.1f} ({s['spread_bps']:.1f}bps)")
        imb=s["imbalance"]; self.s_imb._value.setText(f"{imb*100:.0f}%")
        self.s_imb._value.setStyleSheet(f"color:{GREEN if imb>0.5 else RED};font-size:17px;font-weight:800;border:none;")
        cvd=s["cvd_recent"]; self.s_cvd._value.setText(f"{cvd:+.1f}")
        self.s_cvd._value.setStyleSheet(f"color:{GREEN if cvd>=0 else RED};font-size:17px;font-weight:800;border:none;")
        self.s_tape._value.setText(f"{s['tape_speed']:.1f}/s")
        agg=s["aggressor_ratio"]; self.s_agg._value.setText(f"{agg*100:.0f}%")
        self.s_agg._value.setStyleSheet(f"color:{GREEN if agg>0.5 else RED};font-size:17px;font-weight:800;border:none;")

        self.imb.setValue(int(imb*1000))
        c=GREEN if imb>0.5 else RED
        self.imb.setStyleSheet(f"QProgressBar{{background:{PANEL2};border:1px solid {BORDER};border-radius:7px;"
                               f"text-align:center;color:{TXT};font-weight:700;}}"
                               f"QProgressBar::chunk{{background:{c};border-radius:6px;}}")
        # contribution
        cp=s.get("contrib_pct",{})
        parts=[]
        for v in VENUES:
            col=VENUE_COL.get(v,ACCENT)
            parts.append(f"<span style='color:{col};font-weight:700;'>{v}</span> {cp.get(v,0)*100:.0f}%")
        self.contrib.setText("  ·  ".join(parts))

        self._ladder(s); self._wallz(s); self._sig(s); self._evt(s); self._heat(s)
        self._sounds(s)

    def _sounds(self, s):
        """Détecte les nouveaux événements importants (alimente le copilote IA)."""
        events = s.get("events", [])
        if not self._sound_init:
            # premier passage : on mémorise l'existant sans déclencher
            for e in events:
                self._sound_seen.add((e["ts"], e["kind"], e["text"]))
            self._sound_init = True
            return
        for e in events[:8]:
            key = (e["ts"], e["kind"], e["text"])
            if key in self._sound_seen:
                continue
            self._sound_seen.add(key)
            if e["kind"] == "SWEEP":
                self._ai_on_event("SWEEP", e["text"])
            elif e["kind"] == "WALL-":
                self._ai_on_event("MUR RETIRÉ", e["text"])
        if len(self._sound_seen) > 600:
            self._sound_seen = set(list(self._sound_seen)[-300:])

    def _sig(self,s):
        tag,title,sub=s["bias"]; col=self._col(tag)
        self.bias_label.setText(title)
        self.bias_label.setStyleSheet(f"color:{col};font-size:25px;font-weight:800;border:none;")
        self.bias_sub.setText(sub)
        self.bias_box.setStyleSheet(f"QFrame{{background:{PANEL};border:1px solid {col};border-radius:12px;}}")
        html=[]
        for sg in s["signals"]:
            c=self._col(sg["tag"])
            dot="▲" if sg["tag"]=="hausse" else ("▼" if sg["tag"]=="baisse" else ("!" if sg["tag"]=="alerte" else "•"))
            html.append(
                f"<div style='margin-bottom:11px;'>"
                f"<span style='color:{c};font-weight:800;'>{dot} {sg['title']}</span><br>"
                f"<span style='color:{TXT};'>{sg['detail']}</span><br>"
                f"<span style='color:{DIM};font-style:italic;'>↳ {sg['why']}</span></div>")
        self.signals.setHtml("".join(html))

    def _evt(self,s):
        rows=[]
        for e in s["events"]:
            k=e["kind"]; c={"SWEEP":AMBER,"WALL+":ACCENT,"WALL-":RED}.get(k,DIM)
            rows.append(f"<div style='margin-bottom:6px;'><span style='color:{DIM};'>{e['ts']}</span> "
                        f"<span style='color:{c};font-weight:700;'>{k}</span> "
                        f"<span style='color:{TXT};'>{e['text']}</span></div>")
        self.events.setHtml("".join(rows) if rows else f"<span style='color:{DIM};'>En attente d'événements…</span>")

    def _ladder(self,s):
        asks=s["asks_ladder"][::-1]; bids=s["bids_ladder"]
        rows=asks+bids; self.ladder.setRowCount(len(rows))
        ac={}; c=0.0
        for p,q,_ in s["asks_ladder"]: c+=q; ac[p]=c
        bc={}; c=0.0
        for p,q,_ in s["bids_ladder"]: c+=q; bc[p]=c
        n_ask=len(asks); maxq=max((q for _,q,_ in rows),default=1)
        for i,(p,q,nv) in enumerate(rows):
            is_ask=i<n_ask; color=QtGui.QColor(RED) if is_ask else QtGui.QColor(GREEN)
            cumv=ac.get(p,0) if is_ask else bc.get(p,0)
            for j,v in enumerate([f"{p:,.0f}",f"{q:.2f}",f"{cumv:.1f}","●"*nv]):
                it=QtWidgets.QTableWidgetItem(v)
                if j==3: it.setForeground(QtGui.QColor(ACCENT))
                else: it.setForeground(color if j==0 else QtGui.QColor(TXT))
                self.ladder.setItem(i,j,it)
            tint=QtGui.QColor(color); tint.setAlpha(min(120,int(q/maxq*120)))
            self.ladder.item(i,1).setBackground(tint)

    def _wallz(self,s):
        walls=s["walls"]; self.walls.setRowCount(len(walls))
        for i,w in enumerate(walls):
            color=QtGui.QColor(GREEN) if w["side"]=="bid" else QtGui.QColor(RED)
            cells=["ACHAT" if w["side"]=="bid" else "VENTE",f"{w['price']:,.0f}",
                   f"{w['qty']:.1f}",f"{w['age_s']:.0f}s"]
            for j,v in enumerate(cells):
                it=QtWidgets.QTableWidgetItem(v)
                it.setForeground(color if j==0 else QtGui.QColor(TXT))
                self.walls.setItem(i,j,it)

    def _heat(self,s):
        hm=s["heatmap"]
        if not hm: return
        rows=len(hm[0]); arr=np.zeros((len(hm),rows))
        for i,c in enumerate(hm): arr[i,:]=c
        arr=np.log1p(arr)
        if arr.max()>0: arr=arr/arr.max()
        self.hm_img.setImage(arr,autoLevels=False,levels=(0,1))
        lo,hi=s["hm_lo"],s["hm_hi"]
        self.hm_img.setRect(QtCore.QRectF(0,lo,len(hm),hi-lo))
        self.mid_line.setValue(s["mid"])
        self.hm.setYRange(lo,hi,padding=0); self.hm.setXRange(0,len(hm),padding=0)

        # --- LABELS des niveaux les plus chargés en ordres (là où il y a le plus
        #     de liquidité, moyennée sur la période récente) ---
        labels = getattr(self, "hm_labels", [])
        if labels:
            for lbl in labels:
                lbl.setVisible(False)
            try:
                K = min(len(hm), 60)
                profile = np.asarray(hm[-K:], dtype=float).mean(axis=0)  # liq. moy. par ligne
                min_gap = max(2, rows // 22)          # évite 3 labels collés sur le même niveau
                picked = []
                for idx in np.argsort(profile)[::-1]:
                    if profile[idx] <= 0:
                        break
                    if all(abs(int(idx) - p) >= min_gap for p in picked):
                        picked.append(int(idx))
                    if len(picked) >= len(labels):
                        break
                for lbl, idx in zip(labels, picked):
                    price = lo + (idx + 0.5) / rows * (hi - lo)
                    lbl.setText(f"{price:,.0f}")
                    lbl.setPos(len(hm) * 0.80, price)   # calé vers la droite, en petit
                    lbl.setVisible(True)
            except Exception:
                pass

    def _refresh_window(self, minutes):
        import time as _t
        # schedule next refresh time for the countdown
        self._next_refresh[minutes] = _t.time() + minutes * 60
        w = self.bilan_widgets[minutes]
        r = self.engine.window_report(minutes)
        now_str = _t.strftime("%H:%M:%S")
        if not r.get("ready"):
            n = r.get("n", 0)
            w["header"].setText(f"⏳ Pas encore assez de données ({n} transactions). "
                                f"Dernier essai {now_str}. Laisse tourner.")
            return
        w["_last_update_str"] = now_str
        dom_col = self._col(r["dom_tag"])
        buy_pct = r["buy_share"]*100; sell_pct = (1-r["buy_share"])*100

        # STATS box
        rows = [
            f"<div style='font-size:15px;font-weight:800;color:{dom_col};margin-bottom:10px;'>"
            f"Dominant : {r['dominant']}</div>",
            f"<table style='color:{TXT};font-size:13px;' cellpadding='4'>"
            f"<tr><td style='color:{DIM};'>Ordres ACHAT</td>"
            f"<td style='color:{GREEN};font-weight:700;'>{r['buy_n']:,}</td>"
            f"<td style='color:{DIM};'>Ordres VENTE</td>"
            f"<td style='color:{RED};font-weight:700;'>{r['sell_n']:,}</td></tr>"
            f"<tr><td style='color:{DIM};'>Volume ACHAT</td>"
            f"<td style='color:{GREEN};font-weight:700;'>{r['buy_vol']:,.0f} BTC</td>"
            f"<td style='color:{DIM};'>Volume VENTE</td>"
            f"<td style='color:{RED};font-weight:700;'>{r['sell_vol']:,.0f} BTC</td></tr>"
            f"<tr><td style='color:{DIM};'>Montant ACHAT</td>"
            f"<td style='color:{GREEN};'>{r['buy_usd']/1e6:,.1f} M$</td>"
            f"<td style='color:{DIM};'>Montant VENTE</td>"
            f"<td style='color:{RED};'>{r['sell_usd']/1e6:,.1f} M$</td></tr>"
            f"<tr><td style='color:{DIM};'>Part achat/vente</td>"
            f"<td colspan='3' style='color:{TXT};'>{buy_pct:.0f}% / {sell_pct:.0f}%</td></tr>"
            f"<tr><td style='color:{DIM};'>Delta (achat-vente)</td>"
            f"<td colspan='3' style='color:{GREEN if r['delta_vol']>=0 else RED};font-weight:700;'>"
            f"{r['delta_vol']:+,.0f} BTC</td></tr>"
            f"<tr><td style='color:{DIM};'>Volume total</td>"
            f"<td colspan='3' style='color:{TXT};'>{r['total_vol']:,.0f} BTC "
            f"(~{r['total_usd']/1e6:,.1f} M$)</td></tr>"
            f"<tr><td style='color:{DIM};'>Nb transactions</td>"
            f"<td colspan='3' style='color:{TXT};'>{r['n_trades']:,} ({r['trades_per_min']:.0f}/min)</td></tr>"
            f"<tr><td style='color:{DIM};'>Variation prix</td>"
            f"<td colspan='3' style='color:{GREEN if r['price_change']>=0 else RED};font-weight:700;'>"
            f"{r['price_change']:+,.0f} USD ({r['lo_price']:,.0f} → {r['hi_price']:,.0f})</td></tr>"
            f"</table>",
            f"<div style='margin-top:12px;color:{DIM};font-weight:700;'>"
            f"PRIX LES PLUS ACTIFS (le plus de volume échangé) :</div>",
        ]
        for i, (p, v) in enumerate(r["top_levels"], 1):
            rows.append(f"<div style='color:{TXT};'>{i}. <b>{p:,.0f}</b> — {v:,.0f} BTC échangés</div>")
        w["stats"].setHtml("".join(rows))

        # FINE analysis box (interpretive)
        fhtml = [f"<div style='color:{DIM};font-weight:700;letter-spacing:1px;margin-bottom:10px;'>"
                 f"ANALYSE FINE — ce que ça veut dire</div>"]
        for tag, txt in r["fine"]:
            c = self._col(tag)
            dot = "▲" if tag=="hausse" else ("▼" if tag=="baisse" else ("!" if tag=="alerte" else "•"))
            fhtml.append(f"<div style='margin-bottom:10px;'>"
                         f"<span style='color:{c};font-weight:800;'>{dot}</span> "
                         f"<span style='color:{TXT};'>{txt}</span></div>")
        w["fine"].setHtml("".join(fhtml))

        # PARA box (synthesis)
        w["para"].setHtml(
            f"<div style='color:{TXT};font-size:14px;line-height:150%;'>{r['paragraph']}</div>")

    def _tick_countdowns(self):
        import time as _t
        now = _t.time()
        for m in getattr(self, "WINDOWS", []):
            w = self.bilan_widgets.get(m)
            if not w:
                continue
            remain = max(0, int(self._next_refresh.get(m, now) - now))
            mm, ss = divmod(remain, 60)
            last = w.get("_last_update_str", "—")
            w["header"].setText(
                f"🕒 Fenêtre {m} min   ·   dernière mise à jour : {last}   ·   "
                f"prochaine dans {mm:02d}:{ss:02d}")

    def _refresh_walls(self):
        import time as _t
        mid = self._last_state.get("mid") if self._last_state else None
        for m in getattr(self, "WALL_WINDOWS", []):
            w = self.wall_widgets[m]
            max_dist = self.WALL_DISTS.get(self.wall_dist_combo.currentText(), 250)
            # pool plus large si l'utilisateur trie autrement que par importance
            sort_key = self.WALL_SORTS.get(self.wall_sort_combo.currentText())
            pool = 40 if sort_key else 18
            rep = self.engine.wall_history.report(m, mid=mid, top_n=pool, max_dist=max_dist)
            if rep.get("ready") and sort_key == "dist" and mid:
                # proximité : le plus proche du prix actuel en premier (croissant)
                rep["top"] = sorted(rep["top"],
                                    key=lambda w: abs(w["price"] - mid))[:18]
            elif rep.get("ready") and sort_key:
                rep["top"] = sorted(rep["top"], key=lambda w: (w.get(sort_key) or 0),
                                    reverse=True)[:18]
            if not rep.get("ready"):
                dtxt = self.wall_dist_combo.currentText()
                msg = (f"🧱 Fenêtre {m} min — aucun mur à {dtxt} du prix. "
                       f"Élargis la distance en haut." if max_dist else
                       f"🧱 Fenêtre {m} min — accumulation des murs…")
                w["header"].setText(f"{msg}  ({_t.strftime('%H:%M:%S')})")
                # vide les tableaux
                for key in ("table", "solid", "valid", "broken", "flux"):
                    w[key].setRowCount(0)
                w["_top_walls"] = []
                continue
            w["header"].setText(
                f"🧱 Fenêtre {m} min   ·   {rep['n_total']} murs vus "
                f"({rep['n_buy']} support / {rep['n_sell']} résistance)   ·   "
                f"{rep['n_spoof']} spoof   ·   maj {_t.strftime('%H:%M:%S')}")

            lg = rep["longest"]
            status = ("🟢 toujours actif" if lg["active"] else
                      ("🔴 cassé" if lg["broken"] else
                       "⚪ retiré" if lg["pulled"] else "⚫ disparu"))
            w["longest"].setText(
                f"⏱ MUR LE PLUS TENACE : {lg['side_txt']} à {lg['price']:,.0f}  —  "
                f"resté {lg['lifespan']:.0f}s, taille max {lg['max_qty']:.1f} BTC "
                f"(~{lg['usd']/1e6:.1f} M$), testé {lg['tests']} fois  —  {status}")

            # bandeau de classification (compteurs par statut)
            STA = {"actif": ("🟢 ACTIFS", GREEN), "valide": ("✅ VALIDÉS", GREEN),
                   "invalide": ("🔴 INVALIDÉS", RED), "spoof": ("⚪ SPOOFS", AMBER),
                   "disparu": ("⚫ disparus", DIM)}
            cats = rep.get("categories", {})
            parts = []
            for key in ("actif", "valide", "invalide", "spoof", "disparu"):
                lbl, cc = STA[key]
                parts.append(f"<span style='color:{cc};font-weight:800;'>"
                             f"{lbl.split(' ',1)[0]} {len(cats.get(key,[]))}</span>"
                             f"<span style='color:{DIM};font-size:10px;'> {lbl.split(' ',1)[1]}</span>")
            w["catbar"].setText("   ".join(parts))

            def cell(txt, col=TXT):
                it = QtWidgets.QTableWidgetItem(txt)
                it.setForeground(QtGui.QColor(col))
                return it

            # --- TABLEAU PRINCIPAL : tous les murs par importance ---
            t = w["table"]; top = rep["top"]
            w["_top_walls"] = top          # mémorisé pour le sous-onglet EXÉCUTÉ/EN ATTENTE
            t.setRowCount(len(top))
            for i, wl in enumerate(top):
                sidecol = GREEN if wl["side"] == "bid" else RED
                slbl, scol = STA.get(wl["status"], ("?", TXT))
                cells = [
                    (slbl, scol),
                    ("ACHAT" if wl["side"] == "bid" else "VENTE", sidecol),
                    (f"{wl['price']:,.0f}", TXT),
                    (f"{wl['max_qty']:.1f}", TXT),
                    (f"{wl['usd']/1e6:.2f} M$", GREEN),
                    (f"{wl['lifespan']:.0f}s", TXT),
                    (str(wl["tests"]), AMBER if wl["tests"] >= 2 else TXT),
                ]
                for j, (v, cc) in enumerate(cells):
                    t.setItem(i, j, cell(v, cc))

            # cats = TOUTES les catégories complètes (pas seulement le top par
            # importance) -> les tableaux affichent TOUS les murs, comme le compteur.
            cats = rep.get("categories", {})

            def _wsort(lst):
                """Applique le tri choisi dans « TRIER PAR » (proximité / taille /
                valeur / prix / durée / tests), sur TOUTE la catégorie."""
                if sort_key == "dist" and mid:
                    return sorted(lst, key=lambda w: abs(w["price"] - mid))
                if sort_key:                       # max_qty, usd, price, lifespan, tests
                    return sorted(lst, key=lambda w: (w.get(sort_key) or 0), reverse=True)
                return list(lst)                   # « Importance » : déjà trié par score

            # --- TABLEAU ACTIFS : TOUS les murs présents, tri = choix utilisateur ---
            solid_list = _wsort(cats.get("actif", []))
            st = w["solid"]; st.setRowCount(len(solid_list))
            for i, wl in enumerate(solid_list):
                sidecol = GREEN if wl["side"] == "bid" else RED
                slbl, scol = STA.get(wl["status"], ("?", TXT))
                cells = [
                    (slbl, scol),
                    ("ACHAT" if wl["side"] == "bid" else "VENTE", sidecol),
                    (f"{wl['price']:,.0f}", TXT),
                    (f"{wl['max_qty']:.1f}", TXT),
                    (f"{wl['usd']/1e6:.2f} M$", GREEN),
                    (str(wl["tests"]), AMBER if wl["tests"] >= 2 else TXT),
                    (f"{wl['lifespan']:.0f}s", TXT),
                ]
                for j, (v, cc) in enumerate(cells):
                    st.setItem(i, j, cell(v, cc))

            # --- TABLEAU VALIDÉS : murs qui ont TENU puis sont partis (= niveaux
            #     de référence pour le futur : support/résistance à re-surveiller) ---
            valid_list = _wsort(cats.get("valide", []))
            vt = w["valid"]; vt.setRowCount(len(valid_list))
            for i, wl in enumerate(valid_list):
                sidecol = GREEN if wl["side"] == "bid" else RED
                cells = [
                    ("ACHAT/support" if wl["side"] == "bid" else "VENTE/résist.", sidecol),
                    (f"{wl['price']:,.0f}", TXT),
                    (f"{wl['max_qty']:.1f}", TXT),
                    (f"{wl['usd']/1e6:.2f} M$", GREEN),
                    (str(wl["tests"]), AMBER if wl["tests"] >= 2 else TXT),
                    (f"{wl['lifespan']:.0f}s", TXT),
                ]
                for j, (v, cc) in enumerate(cells):
                    vt.setItem(i, j, cell(v, cc))

            # --- TABLEAU INVALIDÉS : TOUS les cassés + spoofs (cassés d'abord),
            #     plafonné pour rester lisible quand il y a des centaines de spoofs ---
            broken_list = _wsort(cats.get("invalide", []) + cats.get("spoof", []))[:120]
            bt = w["broken"]; bt.setRowCount(len(broken_list))
            for i, wl in enumerate(broken_list):
                sidecol = GREEN if wl["side"] == "bid" else RED
                sort = ("🔴 CASSÉ" if wl["status"] == "invalide" else "⚪ SPOOF")
                sortcol = RED if wl["status"] == "invalide" else AMBER
                cells = [
                    ("ACHAT" if wl["side"] == "bid" else "VENTE", sidecol),
                    (f"{wl['price']:,.0f}", TXT),
                    (f"{wl['max_qty']:.1f}", TXT),
                    (f"{wl['usd']/1e6:.2f} M$", GREEN),
                    (sort, sortcol),
                    (f"{wl['lifespan']:.0f}s", TXT),
                    (str(wl["tests"]), TXT),
                ]
                for j, (v, cc) in enumerate(cells):
                    bt.setItem(i, j, cell(v, cc))

        # === Sous-onglets EXÉCUTÉ / EN ATTENTE (un seul calcul de flux partagé) ===
        s = self._last_state or {}
        live_walls = s.get("walls", []) if s else []

        def live_qty(price):
            """Taille live du mur le plus proche de `price` (±8$)."""
            best, bd = None, 8
            for lw in live_walls:
                d = abs(lw["price"] - price)
                if d <= bd:
                    bd = d; best = lw
            return best["qty"] if best else None

        # union des prix de murs affichés sur toutes les fenêtres -> UN seul scan
        all_prices = sorted({round(wl["price"], 1)
                             for m in getattr(self, "WALL_WINDOWS", [])
                             for wl in self.wall_widgets[m].get("_top_walls", [])})
        flow = self.engine.get_levels_flow(all_prices, tol=30.0, window_s=3600) if all_prices else {}

        def adcell(txt, col=TXT):
            it = QtWidgets.QTableWidgetItem(txt)
            it.setForeground(QtGui.QColor(col))
            return it

        for m in getattr(self, "WALL_WINDOWS", []):
            w = self.wall_widgets[m]
            # on retire les SPOOFS (faux murs retirés sans être touchés) : ils n'ont
            # rien de réel à montrer côté exécuté/en attente
            walls = [wl for wl in w.get("_top_walls", []) if wl["status"] != "spoof"]
            ft = w["flux"]; ft.setRowCount(len(walls))
            for i, wl in enumerate(walls):
                side_bid = wl["side"] == "bid"
                sidecol = GREEN if side_bid else RED
                px = wl["price"]

                def money(btc):
                    """BTC -> '12.3 M$' ou '840 k$' selon la taille (au prix du mur)."""
                    usd = btc * px
                    return f"{usd/1e6:.1f} M$" if abs(usd) >= 1e6 else f"{usd/1e3:.0f} k$"

                cur = live_qty(px)
                peak = wl.get("max_qty")
                if cur is not None and peak and peak > 0:
                    pct = min(100, cur / peak * 100)
                    att_txt = f"{cur:.0f} BTC · {money(cur)} ({pct:.0f}% du pic)"
                    att_col = GREEN if pct >= 66 else (AMBER if pct >= 33 else RED)
                elif peak:
                    att_txt = f"parti — pic {peak:.0f} BTC · {money(peak)}"
                    att_col = DIM
                else:
                    att_txt, att_col = "—", DIM
                lb, ls = flow.get(round(px, 1), (0.0, 0.0))
                net = lb - ls
                if lb + ls <= 0:
                    lect, lcol = "pas de volume échangé ici", DIM
                elif lb > ls * 1.3:
                    lect, lcol = "ACCUMULATION (achat net)", GREEN
                elif ls > lb * 1.3:
                    lect, lcol = "DISTRIBUTION (vente nette)", RED
                else:
                    lect, lcol = "équilibré", AMBER
                cells = [
                    ("ACHAT" if side_bid else "VENTE", sidecol),
                    (f"{px:,.0f}", TXT),
                    (att_txt, att_col),
                    (f"{lb:.0f} BTC · {money(lb)}", GREEN if lb > 0 else DIM),
                    (f"{ls:.0f} BTC · {money(ls)}", RED if ls > 0 else DIM),
                    (f"{net:+.0f} BTC · {money(net)}", GREEN if net >= 0 else RED),
                    (lect, lcol),
                ]
                for j, (v, cc) in enumerate(cells):
                    ft.setItem(i, j, adcell(v, cc))

    # ===========================================================
    # PAGE 4 — VWAP & CVD MULTI-FENÊTRES
    # ===========================================================

    def _build_vwap_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "VWAP de session (prix moyen pondéré par le volume depuis le lancement) + "
            "CVD segmenté par fenêtre. Le VWAP est la référence numéro 1 des institutionnels. "
            "Le CVD multi-fenêtres te montre si la pression s'accélère ou s'épuise.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # ---- VWAP hero ----
        self.vwap_box = QtWidgets.QFrame()
        self.vwap_box.setStyleSheet(
            f"QFrame{{background:{PANEL};border:2px solid {ACCENT};border-radius:14px;}}")
        vb = QtWidgets.QHBoxLayout(self.vwap_box)
        vb.setContentsMargins(24, 16, 24, 16); vb.setSpacing(32)

        def vwap_stat(label):
            f = QtWidgets.QFrame()
            f.setStyleSheet("QFrame{border:none;}")
            lay = QtWidgets.QVBoxLayout(f); lay.setSpacing(2); lay.setContentsMargins(0,0,0,0)
            lbl = QtWidgets.QLabel(label)
            lbl.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:700;letter-spacing:1px;")
            val = QtWidgets.QLabel("—")
            val.setStyleSheet(f"color:{TXT};font-size:22px;font-weight:800;")
            lay.addWidget(lbl); lay.addWidget(val)
            f._val = val
            return f

        self.vw_price   = vwap_stat("VWAP SESSION")
        self.vw_dev_pct = vwap_stat("ÉCART %")
        self.vw_dev_usd = vwap_stat("ÉCART $")
        self.vw_vol     = vwap_stat("VOLUME CUMULÉ")
        self.vw_pos     = vwap_stat("POSITION PRIX")
        self.vw_agg     = vwap_stat("AGRESSEURS (5s)")
        for w in (self.vw_price, self.vw_dev_pct, self.vw_dev_usd, self.vw_vol,
                  self.vw_pos, self.vw_agg):
            vb.addWidget(w)
        outer.addWidget(self.vwap_box)

        # barre agresseurs achat/vente (part des market orders acheteurs)
        agg_row = QtWidgets.QHBoxLayout(); agg_row.setSpacing(8)
        agg_cap = QtWidgets.QLabel("PRESSION AGRESSEURS  (vente ◂ | ▸ achat)")
        agg_cap.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:700;letter-spacing:1px;")
        self.vw_agg_bar = QtWidgets.QProgressBar()
        self.vw_agg_bar.setRange(0, 1000); self.vw_agg_bar.setFixedHeight(20)
        self.vw_agg_bar.setFormat("%p‰ achat")
        agg_row.addWidget(agg_cap); agg_row.addWidget(self.vw_agg_bar, 1)
        outer.addLayout(agg_row)

        # ---- interprétation VWAP ----
        self.vwap_interp = QtWidgets.QLabel("En attente des données…")
        self.vwap_interp.setWordWrap(True)
        self.vwap_interp.setStyleSheet(
            f"color:{TXT};font-size:14px;font-weight:600;"
            f"background:{PANEL2};border:1px solid {BORDER};border-radius:10px;padding:12px;")
        outer.addWidget(self.vwap_interp)

        # ---- CVD multi-fenêtres ----
        outer.addWidget(self._h("CVD PAR FENÊTRE TEMPORELLE  ·  1 min / 5 min / 15 min / 30 min"))
        cvd_row = QtWidgets.QHBoxLayout(); cvd_row.setSpacing(10)
        self.cvd_boxes = {}
        for m in [1, 5, 15, 30]:
            box = QtWidgets.QFrame()
            box.setStyleSheet(f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:12px;}}")
            bl = QtWidgets.QVBoxLayout(box); bl.setContentsMargins(16, 12, 16, 12); bl.setSpacing(6)
            cap = QtWidgets.QLabel(f"CVD {m} MIN")
            cap.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:700;letter-spacing:1px;border:none;")
            val = QtWidgets.QLabel("—")
            val.setStyleSheet(f"color:{TXT};font-size:26px;font-weight:800;border:none;")
            agg_lbl = QtWidgets.QLabel("Agresseurs : —")
            agg_lbl.setStyleSheet(f"color:{TXT};font-size:12px;font-weight:700;border:none;")
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 1000); bar.setFixedHeight(14)
            bar.setFormat("")
            detail = QtWidgets.QLabel("…")
            detail.setWordWrap(True)
            detail.setStyleSheet(f"color:{DIM};font-size:11px;border:none;")
            accel = QtWidgets.QLabel("")
            accel.setStyleSheet(f"color:{AMBER};font-size:11px;font-weight:700;border:none;")
            bl.addWidget(cap); bl.addWidget(val); bl.addWidget(agg_lbl); bl.addWidget(bar)
            bl.addWidget(detail); bl.addWidget(accel)
            bl.addStretch(1)   # colle le contenu en haut, supprime le vide au milieu
            cvd_row.addWidget(box, 1)
            self.cvd_boxes[m] = {"box": box, "val": val, "detail": detail,
                                 "accel": accel, "agg": agg_lbl, "bar": bar}
        outer.addLayout(cvd_row, 1)
        return page

    def _refresh_vwap(self):
        # VWAP
        r = self.engine.get_vwap()
        if not r:
            self.vwap_interp.setText("Accumulation des données… lance l'appli quelques secondes.")
            return
        c_dev = GREEN if r["above"] else RED
        self.vw_price._val.setText(f"{r['vwap']:,.1f}")
        self.vw_dev_pct._val.setText(f"{r['dev_pct']:+.2f}%")
        self.vw_dev_pct._val.setStyleSheet(f"color:{c_dev};font-size:22px;font-weight:800;")
        self.vw_dev_usd._val.setText(f"{r['dev_usd']:+,.0f} $")
        self.vw_dev_usd._val.setStyleSheet(f"color:{c_dev};font-size:22px;font-weight:800;")
        self.vw_vol._val.setText(f"{r['cum_vol']:,.0f} BTC")
        pos_txt = "AU-DESSUS ▲" if r["above"] else "EN-DESSOUS ▼"
        self.vw_pos._val.setText(pos_txt)
        self.vw_pos._val.setStyleSheet(f"color:{c_dev};font-size:18px;font-weight:800;")

        if r["above"]:
            if r["dev_pct"] > 0.3:
                interp = (f"Prix LARGEMENT au-dessus du VWAP ({r['dev_pct']:+.2f}%) : "
                          "les acheteurs contrôlent la session. Les institutionnels ont tendance "
                          "à vendre au-dessus du VWAP — méfiance si tu cherches un long.")
            else:
                interp = (f"Prix légèrement au-dessus du VWAP ({r['dev_pct']:+.2f}%) : "
                          f"biais haussier modéré. Le VWAP à {r['vwap']:,.0f} est un support dynamique — "
                          "un retour dessus sans cassure = opportunité long.")
        else:
            if r["dev_pct"] < -0.3:
                interp = (f"Prix LARGEMENT en-dessous du VWAP ({r['dev_pct']:+.2f}%) : "
                          "les vendeurs dominent la session. Les institutionnels ont tendance "
                          "à acheter sous le VWAP — cherche un rebond si le flux tourne.")
            else:
                interp = (f"Prix légèrement sous le VWAP ({r['dev_pct']:+.2f}%) : "
                          f"biais baissier modéré. Le VWAP à {r['vwap']:,.0f} est une résistance dynamique — "
                          "un retour dessus = momentum change.")
        self.vwap_interp.setText(interp)

        # ligne VWAP sur la heatmap (page DIRECT)
        self.vwap_line.setValue(r["vwap"])
        self.vwap_line.setVisible(True)

        # agresseurs live (part des market orders acheteurs sur 5s, 3 venues)
        agg = self._last_state.get("aggressor_ratio") if self._last_state else None
        if agg is not None:
            c_agg = GREEN if agg > 0.5 else RED
            self.vw_agg._val.setText(f"{agg*100:.0f}% achat")
            self.vw_agg._val.setStyleSheet(f"color:{c_agg};font-size:20px;font-weight:800;")
            self.vw_agg_bar.setValue(int(agg * 1000))
            self.vw_agg_bar.setStyleSheet(
                f"QProgressBar{{background:{PANEL2};border:1px solid {BORDER};"
                f"border-radius:6px;text-align:center;color:{TXT};font-weight:700;}}"
                f"QProgressBar::chunk{{background:{c_agg};border-radius:5px;}}")
        border = GREEN if r["above"] else RED
        self.vwap_box.setStyleSheet(
            f"QFrame{{background:{PANEL};border:2px solid {border};border-radius:14px;}}")

        # CVD multi-fenêtres
        cvds = self.engine.get_cvd_windows()
        for m, w in self.cvd_boxes.items():
            d = cvds.get(m, {})
            if not d.get("ready"):
                w["val"].setText("—")
                w["agg"].setText("Agresseurs : —")
                w["bar"].setValue(500)
                w["detail"].setText(f"Pas encore assez de données ({m} min)")
                w["accel"].setText("")
                w["box"].setStyleSheet(
                    f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:12px;}}")
                continue
            cvd = d["cvd"]
            col = GREEN if cvd >= 0 else RED
            w["val"].setText(f"{cvd:+.1f}")
            w["val"].setStyleSheet(f"color:{col};font-size:26px;font-weight:800;border:none;")

            bv = d["buy_vol"]; sv = d["sell_vol"]; tot = bv + sv
            ratio = bv / tot if tot else 0.5
            pct = ratio * 100
            c_r = GREEN if ratio > 0.5 else RED
            w["agg"].setText(f"Agresseurs : {pct:.0f}% achat / {100-pct:.0f}% vente")
            w["agg"].setStyleSheet(f"color:{c_r};font-size:12px;font-weight:700;border:none;")
            w["bar"].setValue(int(ratio * 1000))
            w["bar"].setStyleSheet(
                f"QProgressBar{{background:{PANEL2};border:1px solid {BORDER};border-radius:5px;}}"
                f"QProgressBar::chunk{{background:{c_r};border-radius:4px;}}")
            w["detail"].setText(
                f"Vol total : {tot:,.1f} BTC  ({d['n']:,} trades)\n"
                f"Achat {bv:,.1f} BTC  ·  Vente {sv:,.1f} BTC")

            accel = d["acceleration"]
            if abs(accel) > 1.5:
                dir_txt = "s'ACCÉLÈRE" if (accel > 0) == (cvd >= 0) else "RALENTIT"
                w["accel"].setText(f"⚡ Pression {dir_txt} (x{abs(accel):.1f})")
            else:
                w["accel"].setText("→ Pression stable")

            w["box"].setStyleSheet(
                f"QFrame{{background:{PANEL};border:2px solid {col};border-radius:12px;}}")

    # ===========================================================
    # PAGE 5 — FLUX INSTITUTIONNELS
    # ===========================================================

    def _build_instit_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Séparation du flux en 3 catégories : Retail (<0.5 BTC), Moyen (0.5-5 BTC), "
            "Institutionnel (>5 BTC). Les whales et institutions font bouger le prix — "
            "leur CVD seul est souvent bien plus prédictif que le CVD total.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # ---- Barres de segmentation ----
        seg_row = QtWidgets.QHBoxLayout(); seg_row.setSpacing(10)
        self.seg_boxes = {}
        for key, label, col in [
            ("retail", "RETAIL  <0.5 BTC",  DIM),
            ("mid",    "MOYEN  0.5–5 BTC",  AMBER),
            ("inst",   "INSTITUTIONNEL  >5 BTC", ACCENT),
        ]:
            box = QtWidgets.QFrame()
            box.setStyleSheet(
                f"QFrame{{background:{PANEL};border:2px solid {col};border-radius:12px;}}")
            bl = QtWidgets.QVBoxLayout(box); bl.setContentsMargins(16, 12, 16, 12); bl.setSpacing(4)
            cap = QtWidgets.QLabel(label)
            cap.setStyleSheet(f"color:{col};font-size:10px;font-weight:800;letter-spacing:1px;border:none;")
            delta = QtWidgets.QLabel("—")
            delta.setStyleSheet(f"color:{TXT};font-size:24px;font-weight:800;border:none;")
            ratio_bar = QtWidgets.QProgressBar()
            ratio_bar.setRange(0, 1000); ratio_bar.setFixedHeight(18)
            detail = QtWidgets.QLabel("…")
            detail.setWordWrap(True)
            detail.setStyleSheet(f"color:{DIM};font-size:11px;border:none;")
            bl.addWidget(cap); bl.addWidget(delta); bl.addWidget(ratio_bar); bl.addWidget(detail)
            seg_row.addWidget(box, 1)
            self.seg_boxes[key] = {"delta": delta, "bar": ratio_bar, "detail": detail, "box": box, "col": col}
        outer.addLayout(seg_row)

        # ---- Interprétation globale ----
        self.instit_interp = QtWidgets.QLabel("En attente…")
        self.instit_interp.setWordWrap(True)
        self.instit_interp.setStyleSheet(
            f"color:{TXT};font-size:14px;font-weight:600;"
            f"background:{PANEL2};border:1px solid {BORDER};border-radius:10px;padding:12px;")
        outer.addWidget(self.instit_interp)

        # ---- Big prints + Icebergs ----
        bottom = QtWidgets.QHBoxLayout(); bottom.setSpacing(10); outer.addLayout(bottom, 1)

        lc = QtWidgets.QVBoxLayout(); lc.setSpacing(6)
        lc.addWidget(self._h("GROS ORDRES DÉTECTÉS  ·  du plus récent au plus ancien"))
        self.big_table = QtWidgets.QTableWidget(0, 5)
        self.big_table.setHorizontalHeaderLabels(["Heure", "Côté", "Prix", "Taille BTC", "Valeur $"])
        self._prep(self.big_table)
        lc.addWidget(self.big_table, 1); bottom.addLayout(lc, 1)

        rc = QtWidgets.QVBoxLayout(); rc.setSpacing(6)
        rc.addWidget(self._h("ANALYSE  ·  comment lire ces chiffres"))
        self.instit_guide = QtWidgets.QTextEdit(); self.instit_guide.setReadOnly(True)
        self.instit_guide.setStyleSheet(
            f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;color:{TXT};font-size:13px;padding:12px;}}")
        rc.addWidget(self.instit_guide, 1); bottom.addLayout(rc, 1)
        return page

    def _refresh_instit(self):
        r = self.engine.get_flow_segments(window_s=300)
        if not r:
            self.instit_interp.setText("Accumulation des données… (5 min nécessaires)")
            return

        for key in ("retail", "mid", "inst"):
            d  = r[key]
            w  = self.seg_boxes[key]
            col_d = GREEN if d["delta"] >= 0 else RED
            w["delta"].setText(f"{d['delta']:+.2f} BTC")
            w["delta"].setStyleSheet(f"color:{col_d};font-size:24px;font-weight:800;border:none;")
            w["bar"].setValue(int(d["ratio"] * 1000))
            c_bar = GREEN if d["ratio"] > 0.5 else RED
            w["bar"].setStyleSheet(
                f"QProgressBar{{background:{PANEL2};border:1px solid {BORDER};"
                f"border-radius:5px;text-align:center;color:{TXT};}}"
                f"QProgressBar::chunk{{background:{c_bar};border-radius:4px;}}")
            tot_usd = d["buy_usd"] + d["sell_usd"]
            w["detail"].setText(
                f"Achat {d['ratio']*100:.0f}%  ·  {d['n']} ordres\n"
                f"Volume total ≈ {tot_usd/1e6:.2f} M$")

        # Interprétation
        inst = r["inst"]; retail = r["retail"]
        lines = []
        if inst["n"] == 0:
            lines.append("Aucun ordre institutionnel (>5 BTC) sur les 5 dernières minutes — marché retail.")
        else:
            if inst["delta"] > 0:
                lines.append(f"✅ Les INSTITUTIONNELS achètent net (+{inst['delta']:.2f} BTC) — signal fort. "
                             "Quand les gros acteurs achètent, c'est souvent directionnel.")
            elif inst["delta"] < 0:
                lines.append(f"⚠ Les INSTITUTIONNELS vendent net ({inst['delta']:.2f} BTC) — pression baissière réelle. "
                             "Méfiance sur les longs.")
            else:
                lines.append("Les institutionnels sont équilibrés — pas de conviction claire côté gros acteurs.")

        if retail["ratio"] > 0.6 and inst["delta"] < 0:
            lines.append("📌 Retail achète mais les instit vendent = piège haussier probable. "
                         "Le retail a souvent tort contre les whales.")
        elif retail["ratio"] < 0.4 and inst["delta"] > 0:
            lines.append("📌 Retail vend mais les instit achètent = retournement haussier possible. "
                         "Les gros acteurs absorbent la pression vendeuse.")

        self.instit_interp.setText("  ".join(lines))

        # Big prints table
        prints = r.get("big_prints", [])
        self.big_table.setRowCount(len(prints))
        for i, p in enumerate(prints):
            col = QtGui.QColor(GREEN) if p["side"] == "ACHAT" else QtGui.QColor(RED)
            cells = [p["ts"], p["side"], f"{p['price']:,.0f}", f"{p['qty']:.3f}",
                     f"{p['usd']:,.0f} $"]
            for j, v in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(v)
                it.setForeground(col if j <= 1 else QtGui.QColor(TXT))
                self.big_table.setItem(i, j, it)

        # Guide
        avg = r["avg_size"]; thr = r["big_threshold"]
        html = [
            f"<div style='color:{DIM};font-weight:700;margin-bottom:10px;'>COMMENT LIRE CES DONNÉES</div>",
            f"<div style='margin-bottom:8px;'><span style='color:{ACCENT};font-weight:700;'>Taille moyenne</span> : "
            f"{avg:.4f} BTC/trade — seuil gros ordre : {thr:.2f} BTC</div>",
            f"<div style='margin-bottom:8px;'><span style='color:{DIM};'>Retail</span> "
            f": bruit du marché. Nombreux mais peu directionnels.</div>",
            f"<div style='margin-bottom:8px;'><span style='color:{AMBER};'>Moyen</span> "
            f": acteurs semi-pro. Leur CVD est un bon filtre de tendance.</div>",
            f"<div style='margin-bottom:8px;'><span style='color:{ACCENT};'>Institutionnel</span> "
            f": whales, fonds, desks. Quand ils s'alignent dans une direction, "
            f"suis-les. Un delta instit positif avec prix qui monte = tendance haussière saine.</div>",
            f"<div style='color:{AMBER};'>⚡ Divergence clé : retail acheteur + instit vendeur "
            f"= méfiance. Instit acheteur + retail vendeur = potentiel retournement.</div>",
        ]
        self._hset(self.instit_guide, "".join(html))

    # ===========================================================
    # PAGE 6 — PROFIL DE VOLUME + SWEEPS + STACKED IMBALANCES
    # ===========================================================

    def _build_profil_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Volume Profile : où s'est vraiment traité le volume (POC = aimant à prix). "
            "Stacked Imbalances : zones où le carnet est déséquilibré sur plusieurs niveaux consécutifs. "
            "Sweeps en cascade : trades agressifs multi-niveaux en < 0.8s.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # sélecteur de fenêtre temporelle du profil
        wrow = QtWidgets.QHBoxLayout(); wrow.setSpacing(8)
        wlbl = QtWidgets.QLabel("FENÊTRE DU PROFIL :")
        wlbl.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:0.5px;")
        wrow.addWidget(wlbl)
        self.prof_window_combo = QtWidgets.QComboBox()
        self.PROF_WINDOWS = {"15 min": 900, "30 min": 1800, "1 heure": 3600,
                             "2 heures": 7200, "4 heures": 14400}
        self.prof_window_combo.addItems(list(self.PROF_WINDOWS.keys()))
        self.prof_window_combo.setCurrentText("1 heure")
        self.prof_window_combo.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:6px 12px;font-weight:700;}}")
        wrow.addWidget(self.prof_window_combo)
        note = QtWidgets.QLabel("· s'applique aux stats POC/VAH/VAL et au tableau "
                                "(le graphique TradingView a ses propres réglages)")
        note.setStyleSheet(f"color:{DIM};font-size:11px;")
        wrow.addWidget(note); wrow.addStretch()
        outer.addLayout(wrow)

        top = QtWidgets.QHBoxLayout(); top.setSpacing(10); outer.addLayout(top)

        # POC / VAH / VAL hero
        self.poc_box = QtWidgets.QFrame()
        self.poc_box.setStyleSheet(
            f"QFrame{{background:{PANEL};border:2px solid {VIOLET};border-radius:14px;}}")
        pb = QtWidgets.QHBoxLayout(self.poc_box)
        pb.setContentsMargins(22, 14, 22, 14); pb.setSpacing(28)

        def prof_stat(label):
            f = QtWidgets.QFrame(); f.setStyleSheet("QFrame{border:none;}")
            lay = QtWidgets.QVBoxLayout(f); lay.setSpacing(2); lay.setContentsMargins(0,0,0,0)
            lbl = QtWidgets.QLabel(label)
            lbl.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:700;letter-spacing:1px;")
            val = QtWidgets.QLabel("—")
            val.setStyleSheet(f"color:{TXT};font-size:20px;font-weight:800;")
            lay.addWidget(lbl); lay.addWidget(val); f._val = val
            return f

        self.pf_poc  = prof_stat("POC  (Point of Control)")
        self.pf_vah  = prof_stat("VAH  (Value Area High)")
        self.pf_val  = prof_stat("VAL  (Value Area Low)")
        self.pf_va   = prof_stat("VALUE AREA %")
        self.pf_vol  = prof_stat("VOLUME TOTAL (1h)")
        for w in (self.pf_poc, self.pf_vah, self.pf_val, self.pf_va, self.pf_vol):
            pb.addWidget(w)
        top.addWidget(self.poc_box, 2)

        body = QtWidgets.QHBoxLayout(); body.setSpacing(10); outer.addLayout(body, 2)

        # ---- vrai graphique TradingView intégré (temps réel, interactif) ----
        gc = QtWidgets.QVBoxLayout(); gc.setSpacing(6)
        gc.addWidget(self._h("GRAPHIQUE TRADINGVIEW  ·  BTCUSDT perp Binance  ·  "
                             "temps réel, VWAP inclus, tous les outils TradingView"))
        if HAS_WEBENGINE:
            self.tv_view = QWebEngineView()
            self.tv_view.setHtml(TRADINGVIEW_HTML, QtCore.QUrl("https://www.tradingview.com/"))
            self.tv_view.setStyleSheet(f"border:1px solid {BORDER};border-radius:8px;")
            gc.addWidget(self.tv_view, 1)
        else:
            missing = QtWidgets.QLabel(
                "⚠ Le graphique TradingView nécessite le module PyQt6-WebEngine.\n"
                "Lance :  py -3.12 -m pip install PyQt6-WebEngine  puis redémarre l'appli.")
            missing.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            missing.setStyleSheet(f"color:{AMBER};font-size:14px;background:{PANEL};"
                                  f"border:1px solid {BORDER};border-radius:12px;padding:30px;")
            gc.addWidget(missing, 1)
        body.addLayout(gc, 3)

        # ---- colonne droite : table (étendue) + cascades ----
        right = QtWidgets.QVBoxLayout(); right.setSpacing(6)
        right.addWidget(self._h("TOP NIVEAUX DE VOLUME"))
        self.prof_table = QtWidgets.QTableWidget(0, 4)
        self.prof_table.setHorizontalHeaderLabels(["Prix", "Volume BTC", "Achat %", "Statut"])
        self._prep(self.prof_table)
        right.addWidget(self.prof_table, 5)
        right.addWidget(self._h("SWEEPS EN CASCADE  (multi-niveaux < 0.8s)"))
        self.cascade_text = QtWidgets.QTextEdit(); self.cascade_text.setReadOnly(True)
        self.cascade_text.setStyleSheet(
            f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;color:{TXT};font-size:12px;padding:10px;}}")
        right.addWidget(self.cascade_text, 2)
        body.addLayout(right, 2)
        return page

    def _refresh_profil(self):
        mid = self._last_state.get("mid") if self._last_state else None

        # Volume Profile — fenêtre choisie par l'utilisateur
        window_s = self.PROF_WINDOWS.get(self.prof_window_combo.currentText(), 3600)
        vp = self.engine.get_volume_profile(window_s=window_s)
        if vp and mid:
            poc = vp["poc"]
            # ligne POC sur la heatmap (page DIRECT)
            self.poc_line.setValue(poc)
            self.poc_line.setVisible(True)

            # (le graphique TradingView intégré se met à jour tout seul)
            col_poc = GREEN if poc < mid else RED
            self.pf_poc._val.setText(f"{poc:,.0f}")
            self.pf_poc._val.setStyleSheet(f"color:{col_poc};font-size:20px;font-weight:800;")
            self.pf_vah._val.setText(f"{vp['vah']:,.0f}")
            self.pf_vah._val.setStyleSheet(f"color:{RED};font-size:20px;font-weight:800;")
            self.pf_val._val.setText(f"{vp['val']:,.0f}")
            self.pf_val._val.setStyleSheet(f"color:{GREEN};font-size:20px;font-weight:800;")
            self.pf_va._val.setText(f"{vp['va_pct']*100:.0f}%")
            self.pf_vol._val.setText(f"{vp['total_vol']:,.0f} BTC")

            # Top levels table
            top10 = vp["top10"]
            self.prof_table.setRowCount(len(top10))
            for i, (price, d) in enumerate(top10):
                tot = d["total"]; buy_pct = d["buy"] / tot * 100 if tot else 50
                is_poc = abs(price - poc) < 5
                status = "🎯 POC" if is_poc else (
                    "🟢 VAH" if abs(price - vp["vah"]) < 10 else (
                    "🔵 VAL" if abs(price - vp["val"]) < 10 else "—"))
                col = QtGui.QColor(VIOLET if is_poc else TXT)
                cells = [f"{price:,.0f}", f"{tot:.2f}", f"{buy_pct:.0f}%", status]
                for j, v in enumerate(cells):
                    it = QtWidgets.QTableWidgetItem(v)
                    it.setForeground(col if is_poc else QtGui.QColor(TXT))
                    self.prof_table.setItem(i, j, it)
        else:
            for w in (self.pf_poc, self.pf_vah, self.pf_val, self.pf_va, self.pf_vol):
                w._val.setText("—")

        # Cascade Sweeps
        cascades = self.engine.get_cascade_sweeps(window_s=120)
        if not cascades:
            self._hset(self.cascade_text,
                f"<span style='color:{DIM};'>Aucun sweep en cascade récent (2 dernières minutes).<br>"
                "Un sweep en cascade = série de trades agressifs du même côté sur 3+ niveaux "
                "en moins de 0.8 seconde. C'est souvent le signe d'un acteur qui veut "
                "prendre position rapidement — signal directionnel fort.</span>")
        else:
            html = []
            for c in cascades:
                col = RED if c["is_sell"] else GREEN
                dot = "▼" if c["is_sell"] else "▲"
                html.append(
                    f"<div style='margin-bottom:12px;'>"
                    f"<span style='color:{DIM};'>{c['ts']}</span> "
                    f"<span style='color:{col};font-weight:800;font-size:14px;'>"
                    f"{dot} SWEEP {c['side']}</span><br>"
                    f"<span style='color:{TXT};'>{c['qty']:.2f} BTC  ·  "
                    f"{c['levels']} niveaux touchés  ·  range {c['range']:.0f}$  ·  "
                    f"~{c['usd']:,}$</span><br>"
                    f"<span style='color:{DIM};font-style:italic;'>Prix balayés : "
                    f"{c['lo']:,.0f} → {c['hi']:,.0f}  ({c['n_trades']} trades en &lt;0.8s)</span></div>")
            self._hset(self.cascade_text, "".join(html))

    def _hm_zoom(self, mult):
        z = self.engine.hm_zoom * mult
        self.engine.set_heatmap_zoom(z)
        span_pct = 0.4 * 5 * self.engine.hm_zoom   # depth_pct=0.004 -> 0.4%
        self.hm_zoom_lbl.setText(f"±{span_pct:.1f}%")

    # ===========================================================
    # PAGE 7 — POSITIONNEMENT (OI + FUNDING + LIQUIDATIONS)
    # ===========================================================

    def _build_pos_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Positionnement du marché (OI + funding : Binance · liquidations : Bybit) : "
            "Open Interest = contrats ouverts, Funding = coût des longs vs shorts, "
            "Liquidations = positions fermées de force. Le carnet te dit ce qui se passe "
            "MAINTENANT ; cette page te dit comment le marché est positionné — donc dans "
            "quel sens une cascade peut partir. Note : les liquidations BTC arrivent par "
            "vagues pendant les mouvements violents ; un tableau vide = marché calme, c'est normal.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # ---- 3 blocs héros ----
        hero = QtWidgets.QHBoxLayout(); hero.setSpacing(10)

        def pos_box(title, col):
            box = QtWidgets.QFrame()
            box.setStyleSheet(f"QFrame{{background:{PANEL};border:2px solid {col};border-radius:12px;}}")
            bl = QtWidgets.QVBoxLayout(box); bl.setContentsMargins(16, 12, 16, 12); bl.setSpacing(4)
            cap = QtWidgets.QLabel(title)
            cap.setStyleSheet(f"color:{col};font-size:10px;font-weight:800;letter-spacing:1px;border:none;")
            val = QtWidgets.QLabel("—")
            val.setStyleSheet(f"color:{TXT};font-size:24px;font-weight:800;border:none;")
            sub = QtWidgets.QLabel("…")
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color:{DIM};font-size:11px;border:none;")
            bl.addWidget(cap); bl.addWidget(val); bl.addWidget(sub); bl.addStretch(1)
            box._val = val; box._sub = sub
            return box

        self.pos_funding = pos_box("FUNDING RATE  (8h)", AMBER)
        self.pos_oi      = pos_box("OPEN INTEREST  (BTC)", ACCENT)
        self.pos_liq     = pos_box("LIQUIDATIONS  (5 min)", RED)
        hero.addWidget(self.pos_funding, 1)
        hero.addWidget(self.pos_oi, 1)
        hero.addWidget(self.pos_liq, 1)
        outer.addLayout(hero)

        # ---- interprétation ----
        self.pos_interp = QtWidgets.QLabel("Accumulation des données… (l'OI met ~30s à apparaître)")
        self.pos_interp.setWordWrap(True)
        self.pos_interp.setStyleSheet(
            f"color:{TXT};font-size:14px;font-weight:600;"
            f"background:{PANEL2};border:1px solid {BORDER};border-radius:10px;padding:12px;")
        outer.addWidget(self.pos_interp)

        # ---- feed liquidations + guide ----
        body = QtWidgets.QHBoxLayout(); body.setSpacing(10); outer.addLayout(body, 1)

        lc = QtWidgets.QVBoxLayout(); lc.setSpacing(6)
        lc.addWidget(self._h("LIQUIDATIONS EN TEMPS RÉEL  (🔴 long liquidé = vente forcée · 🟢 short liquidé = achat forcé)"))
        self.liq_table = QtWidgets.QTableWidget(0, 5)
        self.liq_table.setHorizontalHeaderLabels(["Heure", "Position", "Prix", "Taille BTC", "Valeur $"])
        self._prep(self.liq_table)
        lc.addWidget(self.liq_table, 1); body.addLayout(lc, 3)

        rc = QtWidgets.QVBoxLayout(); rc.setSpacing(6)
        rc.addWidget(self._h("COMMENT LIRE CETTE PAGE"))
        guide = QtWidgets.QTextEdit(); guide.setReadOnly(True)
        guide.setStyleSheet(
            f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;color:{TXT};font-size:13px;padding:12px;}}")
        guide.setHtml(
            f"<div style='color:{ACCENT};font-weight:800;margin-bottom:6px;'>OPEN INTEREST</div>"
            f"<div style='margin-bottom:10px;color:{TXT};'>"
            "OI ↑ + prix ↑ = nouveaux longs, hausse saine.<br>"
            "OI ↓ + prix ↑ = short squeeze, hausse fragile.<br>"
            "OI ↑ + prix ↓ = nouveaux shorts, baisse saine.<br>"
            "OI ↓ + prix ↓ = longs qui capitulent, baisse en fin de course.</div>"
            f"<div style='color:{AMBER};font-weight:800;margin-bottom:6px;'>FUNDING</div>"
            f"<div style='margin-bottom:10px;color:{TXT};'>"
            "Très positif (&gt;0.03%) = marché sur-leveragé long → risque de flush baissier.<br>"
            "Négatif = marché short → un rebond peut squeezer les shorts vers le haut.</div>"
            f"<div style='color:{RED};font-weight:800;margin-bottom:6px;'>LIQUIDATIONS</div>"
            f"<div style='color:{TXT};'>"
            "Cluster de longs liquidés = cascade baissière en cours ; quand elle s'épuise, "
            "c'est souvent LE point bas. Cluster de shorts liquidés = squeeze haussier.</div>")
        rc.addWidget(guide, 1); body.addLayout(rc, 2)

        # --- BILANS PÉRIODIQUES (récupérés de l'ancienne page ANALYSE) ---
        bilans = self._build_bilans_section()
        bilans.setMaximumHeight(360)
        outer.addWidget(bilans)
        return page

    def _refresh_pos(self):
        r = self.engine.get_positioning()

        # FUNDING
        f = r.get("funding")
        if f:
            rate = f["rate_pct"]
            col = RED if rate > 0.03 else (GREEN if rate < 0 else TXT)
            self.pos_funding._val.setText(f"{rate:+.4f}%")
            self.pos_funding._val.setStyleSheet(f"color:{col};font-size:24px;font-weight:800;border:none;")
            nxt = f.get("next_in_min")
            nxt_txt = f"prochain dans {nxt:.0f} min" if nxt is not None else ""
            self.pos_funding._sub.setText(
                f"≈ {f['annual_pct']:+.1f}%/an  ·  {nxt_txt}\n"
                + ("⚠ Longs sur-leveragés" if rate > 0.03 else
                   "Shorts paient les longs" if rate < 0 else "Niveau normal"))

        # OPEN INTEREST
        oi = r.get("oi")
        if oi:
            self.pos_oi._val.setText(f"{oi['now']:,.0f}")
            c5 = GREEN if oi["chg_5m"] >= 0 else RED
            self.pos_oi._sub.setText(
                f"5 min : {oi['chg_5m']:+,.0f} BTC ({oi['chg_5m_pct']:+.2f}%)\n"
                f"15 min : {oi['chg_15m']:+,.0f} BTC ({oi['chg_15m_pct']:+.2f}%)")
            self.pos_oi._val.setStyleSheet(f"color:{c5};font-size:24px;font-weight:800;border:none;")

        # LIQUIDATIONS 5 min
        lq = r.get("liq_5m", {})
        lu = lq.get("long_usd", 0); su = lq.get("short_usd", 0)
        if lu + su > 0:
            dom_long = lu > su
            col = RED if dom_long else GREEN
            self.pos_liq._val.setText(f"{(lu+su)/1e6:.2f} M$")
            self.pos_liq._val.setStyleSheet(f"color:{col};font-size:24px;font-weight:800;border:none;")
            self.pos_liq._sub.setText(
                f"Longs liquidés : {lu/1e6:.2f} M$\nShorts liquidés : {su/1e6:.2f} M$")
        else:
            self.pos_liq._val.setText("0 $")
            self.pos_liq._sub.setText("Aucune liquidation sur 5 min — marché calme.")

        # INTERPRÉTATION (quadrant OI × prix + funding + liq)
        lines = []
        pc = r.get("price_chg_5m", 0.0)
        if oi and oi["n_samples"] >= 3:
            o = oi["chg_5m_pct"]
            if o > 0.05 and pc > 0:
                lines.append("📈 OI ↑ + prix ↑ : de nouveaux LONGS entrent — hausse alimentée "
                             "par de l'argent frais, tendance saine.")
            elif o < -0.05 and pc > 0:
                lines.append("⚠ OI ↓ + prix ↑ : SHORT SQUEEZE — la hausse vient de shorts qui "
                             "rachètent, pas de nouveaux acheteurs. Fragile, ne pas chasser.")
            elif o > 0.05 and pc < 0:
                lines.append("📉 OI ↑ + prix ↓ : de nouveaux SHORTS entrent — baisse alimentée, "
                             "tendance baissière saine.")
            elif o < -0.05 and pc < 0:
                lines.append("🔄 OI ↓ + prix ↓ : des longs capitulent — la baisse se vide, "
                             "un plancher peut se former bientôt.")
            else:
                lines.append("OI stable sur 5 min : pas de repositionnement marquant.")
        if f and f["rate_pct"] > 0.03:
            lines.append("🔥 Funding élevé : le marché est chargé en longs leveragés — "
                         "une petite baisse peut déclencher une cascade de liquidations.")
        if lu > su * 3 and lu > 1e6:
            lines.append("💥 Cascade de LONGS liquidés en cours — si le rythme ralentit, "
                         "surveille un point bas (les ventes forcées s'épuisent).")
        elif su > lu * 3 and su > 1e6:
            lines.append("🚀 Cascade de SHORTS liquidés — squeeze haussier en cours, "
                         "ne shorte pas tant que ça brûle.")
        if lines:
            self.pos_interp.setText("  ".join(lines))

        # table liquidations
        liqs = r.get("liqs", [])
        if not liqs:
            # message d'attente pour que la page ne paraisse pas vide/cassée
            self.liq_table.setRowCount(1)
            it = QtWidgets.QTableWidgetItem(
                "⏳ En attente de liquidations… (elles arrivent par vagues lors des mouvements violents)")
            it.setForeground(QtGui.QColor(DIM))
            self.liq_table.setItem(0, 0, it)
            for j in range(1, 5):
                self.liq_table.setItem(0, j, QtWidgets.QTableWidgetItem(""))
            return
        self.liq_table.setRowCount(len(liqs))
        for i, l in enumerate(liqs):
            is_long = l["side"] == "long"
            col = QtGui.QColor(RED) if is_long else QtGui.QColor(GREEN)
            cells = [l["ts"],
                     "🔴 LONG liquidé" if is_long else "🟢 SHORT liquidé",
                     f"{l['price']:,.1f}", f"{l['qty']:.4f}", f"{l['usd']:,} $"]
            for j, v in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(v)
                it.setForeground(col if j == 1 else QtGui.QColor(TXT))
                self.liq_table.setItem(i, j, it)

    # ===========================================================
    # CALCULATEUR DE POSITION (money management, réplique de l'Excel)
    # ===========================================================

    def _calc_input(self, default=""):
        e = QtWidgets.QLineEdit(str(default))
        e.setStyleSheet(
            f"QLineEdit{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:8px 12px;font-size:14px;font-weight:700;}}")
        e.textChanged.connect(self._calc_compute)
        return e

    def _build_calc_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Money management : à partir de ton capital, du risque accepté par trade et "
            "de la distance de ton stop, l'app calcule la TAILLE DE POSITION à prendre. "
            "Seuls Capital, Risque % et Distance du stop sont obligatoires. Prix d'entrée "
            "et Take Profit sont optionnels (pour la valeur $, le levier et le R:R).")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        body = QtWidgets.QHBoxLayout(); body.setSpacing(14); outer.addLayout(body, 1)

        # ----- colonne ENTRÉES -----
        inbox = QtWidgets.QVBoxLayout(); inbox.setSpacing(8)
        inbox.addWidget(self._h("PARAMÈTRES DU TRADE"))
        form = QtWidgets.QFormLayout(); form.setSpacing(10)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

        self.calc_capital = self._calc_input("10000")
        self.calc_risk    = self._calc_input("1.25")     # en %
        self.calc_sl      = self._calc_input("140")      # $ par BTC
        self.calc_entry   = self._calc_input("")         # optionnel
        self.calc_tp      = self._calc_input("")         # optionnel
        self.calc_side    = QtWidgets.QComboBox()
        self.calc_side.addItems(["Long", "Short"])
        self.calc_side.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:8px 12px;font-weight:700;}}")
        self.calc_side.currentIndexChanged.connect(self._calc_compute)

        def flbl(t):
            l = QtWidgets.QLabel(t); l.setStyleSheet(f"color:{TXT};font-size:13px;font-weight:600;")
            return l
        form.addRow(flbl("Capital total ($)"), self.calc_capital)
        form.addRow(flbl("Risque par trade (%)"), self.calc_risk)
        form.addRow(flbl("Distance du Stop Loss ($/BTC)"), self.calc_sl)
        form.addRow(flbl("Prix d'entrée BTC ($) — optionnel"), self.calc_entry)
        form.addRow(flbl("Prix Take Profit ($) — optionnel"), self.calc_tp)
        form.addRow(flbl("Sens du trade"), self.calc_side)
        inbox.addLayout(form)
        inbox.addStretch(1)
        body.addLayout(inbox, 1)

        # ----- colonne RÉSULTATS -----
        outbox = QtWidgets.QVBoxLayout(); outbox.setSpacing(8)
        outbox.addWidget(self._h("RÉSULTATS DU CALCULATEUR"))
        self.calc_table = QtWidgets.QTableWidget(10, 2)
        self.calc_table.setHorizontalHeaderLabels(["Indicateur", "Valeur"])
        self._prep(self.calc_table)
        self.calc_table.verticalHeader().setDefaultSectionSize(38)
        outbox.addWidget(self.calc_table, 1)

        self.calc_alert = QtWidgets.QLabel("—")
        self.calc_alert.setWordWrap(True)
        self.calc_alert.setStyleSheet(
            f"color:{TXT};font-size:14px;font-weight:800;background:{PANEL2};"
            f"border:2px solid {BORDER};border-radius:10px;padding:12px;")
        outbox.addWidget(self.calc_alert)
        body.addLayout(outbox, 1)

        # rangées fixes du tableau résultats
        self._calc_rows = [
            "Montant risqué ($)", "Taille de position (BTC)", "Valeur de la position ($)",
            "Levier utilisé (x)", "Prix du Stop Loss ($)", "Distance du Take Profit ($/BTC)",
            "Ratio Risque/Récompense (R:R)", "% du capital utilisé (marge)",
            "Gain au Take Profit ($)", "Gain au Take Profit (% capital)",
        ]
        for i, name in enumerate(self._calc_rows):
            it = QtWidgets.QTableWidgetItem(name)
            it.setForeground(QtGui.QColor(DIM))
            self.calc_table.setItem(i, 0, it)
            self.calc_table.setItem(i, 1, QtWidgets.QTableWidgetItem("—"))

        QtCore.QTimer.singleShot(100, self._calc_compute)
        return page

    def _calc_compute(self, *args):
        def num(widget):
            try:
                return float(str(widget.text()).replace(",", ".").replace(" ", "").replace("$", ""))
            except (ValueError, AttributeError):
                return None
        cap  = num(self.calc_capital)
        risk = num(self.calc_risk)
        sld  = num(self.calc_sl)
        entry = num(self.calc_entry)
        tp    = num(self.calc_tp)
        is_long = self.calc_side.currentText() == "Long"

        vals = ["—"] * len(self._calc_rows)
        cols = [TXT] * len(self._calc_rows)

        risk_frac = (risk / 100.0) if risk is not None else None
        risk_amt = cap * risk_frac if (cap is not None and risk_frac is not None) else None
        size = (risk_amt / sld) if (risk_amt is not None and sld) else None

        if risk_amt is not None:
            vals[0] = f"{risk_amt:,.2f} $"
        if size is not None:
            vals[1] = f"{size:.4f} BTC"; cols[1] = ACCENT
        pos_val = size * entry if (size is not None and entry) else None
        if pos_val is not None:
            vals[2] = f"{pos_val:,.0f} $"
        lev = pos_val / cap if (pos_val is not None and cap) else None
        if lev is not None:
            vals[3] = f"{lev:.2f} x"; cols[3] = AMBER if lev > 1 else TXT
        if entry and sld is not None:
            sl_price = entry - sld if is_long else entry + sld
            vals[4] = f"{sl_price:,.0f} $"; cols[4] = RED
        dist_tp = abs(tp - entry) if (tp and entry) else None
        if dist_tp is not None:
            vals[5] = f"{dist_tp:,.0f} $"
        rr = dist_tp / sld if (dist_tp is not None and sld) else None
        if rr is not None:
            vals[6] = f"{rr:.2f} : 1"; cols[6] = GREEN if rr >= 2 else (AMBER if rr >= 1 else RED)
        if lev is not None:
            vals[7] = f"{lev*100:.1f} %"
        gain_tp = dist_tp * size if (dist_tp is not None and size is not None) else None
        if gain_tp is not None:
            vals[8] = f"{gain_tp:,.2f} $"; cols[8] = GREEN
        if gain_tp is not None and cap:
            vals[9] = f"{gain_tp/cap*100:.2f} %"; cols[9] = GREEN

        for i, (v, c) in enumerate(zip(vals, cols)):
            it = QtWidgets.QTableWidgetItem(v)
            it.setForeground(QtGui.QColor(c))
            f = it.font(); f.setBold(True); f.setPointSize(13); it.setFont(f)
            self.calc_table.setItem(i, 1, it)

        # alerte marge / levier
        if lev is None:
            self.calc_alert.setText("Entre au moins Capital, Risque % et Distance du stop "
                                    "pour la taille de position. Ajoute le prix d'entrée pour "
                                    "la valeur $ et le levier.")
            self.calc_alert.setStyleSheet(
                f"color:{DIM};font-size:13px;font-weight:600;background:{PANEL2};"
                f"border:2px solid {BORDER};border-radius:10px;padding:12px;")
        elif lev > 1:
            self.calc_alert.setText(f"⚠️ Attention : la position ({pos_val:,.0f} $) dépasse ton "
                                    f"capital — levier {lev:.2f}x. Marge nécessaire au-delà de 100 %.")
            self.calc_alert.setStyleSheet(
                f"color:{RED};font-size:14px;font-weight:800;background:{PANEL2};"
                f"border:2px solid {RED};border-radius:10px;padding:12px;")
        else:
            self.calc_alert.setText(f"✅ OK : position dans les limites du capital "
                                    f"(levier {lev:.2f}x, {lev*100:.0f} % du capital en marge).")
            self.calc_alert.setStyleSheet(
                f"color:{GREEN};font-size:14px;font-weight:800;background:{PANEL2};"
                f"border:2px solid {GREEN};border-radius:10px;padding:12px;")

    # ===========================================================
    # JOURNAL DE TRADES (saisie manuelle + persistance)
    # ===========================================================

    def _journal_path(self):
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_journal.json")

    def _journal_load(self):
        import json, os
        p = self._journal_path()
        if not os.path.exists(p):
            return []
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _journal_save(self):
        import json
        try:
            with open(self._journal_path(), "w", encoding="utf-8") as f:
                json.dump(self._journal, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _build_journal_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Journal de trades : note chaque trade pris (date, sens, entrée, stop, take "
            "profit, taille, sortie). L'app calcule le R:R et le P&L, et garde tout sur "
            "disque (trade_journal.json). Un tableau de bord résume ton win-rate et ton P&L total.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # ----- formulaire de saisie -----
        formbox = QtWidgets.QHBoxLayout(); formbox.setSpacing(8)

        def jinput(ph, w=90):
            e = QtWidgets.QLineEdit(); e.setPlaceholderText(ph)
            e.setFixedWidth(w)
            e.setStyleSheet(
                f"QLineEdit{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
                f"color:{TXT};padding:7px 10px;font-size:13px;}}")
            return e

        self.j_side = QtWidgets.QComboBox(); self.j_side.addItems(["Long", "Short"])
        self.j_side.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:7px 10px;font-weight:700;}}")
        self.j_entry = jinput("Entrée")
        self.j_sl    = jinput("Stop")
        self.j_tp    = jinput("Take Profit")
        self.j_size  = jinput("Taille BTC", 80)
        self.j_exit  = jinput("Sortie", 80)
        self.j_note  = jinput("Note", 200)
        addbtn = QtWidgets.QPushButton("➕ Ajouter")
        addbtn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        addbtn.setStyleSheet(
            f"QPushButton{{background:{GREEN};color:#06210f;border:none;border-radius:8px;"
            f"padding:8px 16px;font-weight:800;}}QPushButton:hover{{background:#3ee08a;}}")
        addbtn.clicked.connect(self._journal_add)
        delbtn = QtWidgets.QPushButton("🗑 Supprimer sélection")
        delbtn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        delbtn.setStyleSheet(
            f"QPushButton{{background:{PANEL2};color:{RED};border:1px solid {RED};border-radius:8px;"
            f"padding:8px 14px;font-weight:700;}}QPushButton:hover{{background:{RED};color:#fff;}}")
        delbtn.clicked.connect(self._journal_delete)

        for w in (QtWidgets.QLabel("Sens"), self.j_side, self.j_entry, self.j_sl,
                  self.j_tp, self.j_size, self.j_exit, self.j_note, addbtn):
            if isinstance(w, QtWidgets.QLabel):
                w.setStyleSheet(f"color:{DIM};font-size:12px;font-weight:700;")
            formbox.addWidget(w)
        formbox.addStretch(1)
        formbox.addWidget(delbtn)
        outer.addLayout(formbox)

        # ----- tableau de bord (résumé) -----
        self.j_dash = QtWidgets.QLabel("Aucun trade pour l'instant.")
        self.j_dash.setStyleSheet(
            f"color:{TXT};font-size:14px;font-weight:700;background:{PANEL2};"
            f"border:1px solid {BORDER};border-radius:10px;padding:10px 14px;")
        outer.addWidget(self.j_dash)

        # ----- tableau des trades -----
        cols = ["Date", "Sens", "Entrée", "Stop", "Take Profit", "Sortie",
                "Taille BTC", "R:R prévu", "P&L $", "Résultat", "Note"]
        self.j_table = QtWidgets.QTableWidget(0, len(cols))
        self.j_table.setHorizontalHeaderLabels(cols)
        self._prep(self.j_table)
        self.j_table.verticalHeader().setDefaultSectionSize(34)
        outer.addWidget(self.j_table, 1)

        self._journal = self._journal_load()
        self._journal_refresh()
        return page

    def _journal_add(self):
        import time as _t

        def num(w):
            try:
                return float(str(w.text()).replace(",", ".").replace(" ", ""))
            except ValueError:
                return None
        entry = num(self.j_entry)
        if entry is None:
            self.j_dash.setText("⚠️ Entre au moins un prix d'entrée valide pour ajouter le trade.")
            return
        rec = {
            "date": _t.strftime("%Y-%m-%d %H:%M"),
            "side": self.j_side.currentText(),
            "entry": entry, "sl": num(self.j_sl), "tp": num(self.j_tp),
            "size": num(self.j_size), "exit": num(self.j_exit),
            "note": self.j_note.text().strip(),
        }
        self._journal.append(rec)
        self._journal_save()
        self._journal_refresh()
        for w in (self.j_entry, self.j_sl, self.j_tp, self.j_size, self.j_exit, self.j_note):
            w.clear()

    def _journal_delete(self):
        rows = sorted({i.row() for i in self.j_table.selectedItems()}, reverse=True)
        if not rows:
            return
        for r in rows:
            if 0 <= r < len(self._journal):
                self._journal.pop(r)
        self._journal_save()
        self._journal_refresh()

    def _journal_refresh(self):
        j = getattr(self, "_journal", [])
        self.j_table.setRowCount(len(j))
        tot_pnl = 0.0; wins = 0; losses = 0; closed = 0
        for i, r in enumerate(j):
            entry = r.get("entry"); sl = r.get("sl"); tp = r.get("tp")
            size = r.get("size"); ex = r.get("exit")
            is_long = r.get("side") == "Long"
            # R:R prévu = distance TP / distance SL
            rr = None
            if entry and tp and sl and abs(entry - sl) > 0:
                rr = abs(tp - entry) / abs(entry - sl)
            # P&L = (sortie - entrée) * taille * sens
            pnl = None
            if entry and ex and size:
                pnl = (ex - entry) * size * (1 if is_long else -1)
                tot_pnl += pnl; closed += 1
                if pnl >= 0:
                    wins += 1
                else:
                    losses += 1
            if pnl is None:
                res_txt, res_col = "ouvert", AMBER
            elif pnl >= 0:
                res_txt, res_col = "WIN", GREEN
            else:
                res_txt, res_col = "LOSS", RED
            cells = [
                (r.get("date", "—"), DIM),
                (r.get("side", "—"), GREEN if is_long else RED),
                (f"{entry:,.0f}" if entry else "—", TXT),
                (f"{sl:,.0f}" if sl else "—", RED),
                (f"{tp:,.0f}" if tp else "—", GREEN),
                (f"{ex:,.0f}" if ex else "—", TXT),
                (f"{size:.3f}" if size else "—", TXT),
                (f"{rr:.2f}:1" if rr else "—", GREEN if (rr and rr >= 2) else TXT),
                (f"{pnl:+,.0f} $" if pnl is not None else "—", GREEN if (pnl or 0) >= 0 else RED),
                (res_txt, res_col),
                (r.get("note", ""), DIM),
            ]
            for jx, (v, cc) in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(v)
                it.setForeground(QtGui.QColor(cc))
                self.j_table.setItem(i, jx, it)

        # tableau de bord
        if not j:
            self.j_dash.setText("Aucun trade pour l'instant. Ajoute ton premier trade ci-dessus.")
            return
        wr = (wins / closed * 100) if closed else 0
        pnlcol = GREEN if tot_pnl >= 0 else RED
        self.j_dash.setText(
            f"📊 {len(j)} trades  ·  {closed} clôturés  ·  "
            f"Win-rate : {wr:.0f}% ({wins}W / {losses}L)  ·  "
            f"P&L total : {tot_pnl:+,.0f} $  ·  "
            f"{len(j)-closed} ouvert(s)")
        self.j_dash.setStyleSheet(
            f"color:{pnlcol};font-size:14px;font-weight:800;background:{PANEL2};"
            f"border:1px solid {pnlcol};border-radius:10px;padding:10px 14px;")

    # ===========================================================
    # ALERTES WHATSAPP (critères précis + fenêtre horaire)
    # ===========================================================

    def _alerts_cfg_path(self):
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts_config.json")

    def _alerts_default_cfg(self):
        return {"enabled": False, "channel": "ntfy",
                "ntfy_topic": "", "phone": "", "apikey": "",
                "tg_token": "", "tg_chat": "", "tg_bot_on": False,
                "start_h": 13, "end_h": 16, "levels": [],
                "approach": 75.0, "live_interval": 45,
                "approach_on": True, "wall_on": True, "wall_min": 100.0,
                "accel_on": True, "accel_factor": 2.0}

    def _alerts_load_cfg(self):
        import json, os
        d = self._alerts_default_cfg()
        p = self._alerts_cfg_path()
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    d.update(json.load(f))
            except (OSError, ValueError):
                pass
        return d

    def _alerts_save_cfg(self):
        import json
        try:
            with open(self._alerts_cfg_path(), "w", encoding="utf-8") as f:
                json.dump(self._alert_cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _apply_notifier_cfg(self):
        ch = self._alert_cfg.get("channel", "ntfy")
        self.notifier.configure(backend=ch,
                                ntfy_topic=self._alert_cfg.get("ntfy_topic", ""),
                                phone=self._alert_cfg.get("phone", ""),
                                apikey=self._alert_cfg.get("apikey", ""),
                                tg_token=self._alert_cfg.get("tg_token", ""),
                                tg_chat=self._alert_cfg.get("tg_chat", ""))
        # ntfy/telegram = pas de limite de débit → maj live rapprochées possibles.
        # WhatsApp (CallMeBot) reste bridé à ~1 msg/25s.
        self.notifier.min_interval = 25.0 if ch == "whatsapp" else 3.0

    def _telegram_question(self, question):
        """Callback du bot Telegram : répond via le copilote + données live."""
        try:
            snap = self._ai_snapshot()
        except Exception:
            snap = "(instantané indisponible)"
        ok, text = self.copilot.chat_sync(question, snap)
        return text if ok else f"⚠ {text}"

    def _telegram_learn_chat(self, chat_id):
        self._alert_cfg["tg_chat"] = chat_id
        self._alerts_save_cfg()

    def _start_telegram_bot(self):
        """DÉSACTIVÉ sur le PC : le bot Telegram tourne EXCLUSIVEMENT sur le serveur
        cloud (server.py). On ne le lance jamais ici, pour qu'il soit impossible d'avoir
        deux bots en conflit. Filet de sécurité même si un vieux code l'appelle."""
        self._tg_bot = None
        return
        # (ancien code neutralisé — conservé pour référence)
        cfg = self._alert_cfg
        if cfg.get("tg_bot_on") and cfg.get("tg_token"):
            from telegram_bot import TelegramCopilotBot
            self._tg_bot = TelegramCopilotBot(
                cfg.get("tg_token", ""), cfg.get("tg_chat", ""),
                self._telegram_question, self._telegram_learn_chat)
            self._tg_bot.start()

    def _in_time_window(self, cfg):
        import time as _t
        h = _t.localtime().tm_hour
        a, b = int(cfg.get("start_h", 0)), int(cfg.get("end_h", 24))
        if a == b:
            return True
        if a < b:
            return a <= h < b
        return h >= a or h < b        # fenêtre qui passe minuit

    def _alert_confluence_text(self, lvl, mid, s, first):
        is_res = lvl > mid
        side = "résistance" if is_res else "support"
        d = abs(mid - lvl)
        cvds = self.engine.get_cvd_windows()
        c1 = cvds.get(1, {})
        cvd1 = c1.get("cvd", 0) if c1.get("ready") else 0
        agg = s.get("aggressor_ratio", 0.5) * 100
        wall = self._exec_nearest_wall(lvl, s)
        if wall:
            wtxt = (f"{wall['price']:,.0f} ({wall['qty']:.0f} BTC · "
                    f"{wall['qty']*wall['price']/1e6:.1f}M$)")
        else:
            wtxt = "aucun proche"
        vw = self.engine.get_vwap()
        vwtxt = ("prix>VWAP" if (vw and vw["above"]) else "prix<VWAP") if vw else "VWAP —"
        tape = s.get("tape_speed", 0)
        head = "⚡ APPROCHE" if first else "🔄 MAJ live"
        return (f"{head} {side} {lvl:,.0f}\n"
                f"Prix {mid:,.0f} (à {d:.0f}$)\n"
                f"CVD 1m {cvd1:+.0f} · agress {agg:.0f}% achat\n"
                f"Mur proche : {wtxt}\n"
                f"{vwtxt} · tape {tape:.0f}/s")

    def _alerts_tick(self):
        self._alerts_refresh_log()
        cfg = getattr(self, "_alert_cfg", None)
        if not cfg or not cfg.get("enabled"):
            return
        import time as _t
        s = self._last_state or {}
        mid = s.get("mid")
        # maj de la base d'accélération (toujours, pour une référence fiable)
        tape = s.get("tape_speed", 0.0)
        if mid and not s.get("warming"):
            self._tape_ema = tape if self._tape_ema is None else 0.98*self._tape_ema + 0.02*tape
        if not mid or s.get("warming") or not self._in_time_window(cfg):
            return
        now = _t.time()

        # --- A. approche des niveaux + mises à jour live ---
        if cfg.get("approach_on"):
            for lvl in cfg.get("levels", []):
                d = abs(mid - lvl)
                key = f"lvl_{lvl}"
                st = self._alert_state.get(key, {"in": False, "last": 0.0})
                if d <= cfg["approach"]:
                    if not st["in"]:
                        self.notifier.send(self._alert_confluence_text(lvl, mid, s, first=True))
                        st = {"in": True, "last": now}
                    elif now - st["last"] >= cfg.get("live_interval", 90):
                        self.notifier.send(self._alert_confluence_text(lvl, mid, s, first=False))
                        st["last"] = now
                elif d > cfg["approach"] * 1.6:
                    st["in"] = False        # sortie de zone (hystérésis anti-yoyo)
                self._alert_state[key] = st

        # --- B. gros mur qui apparaît près du prix ---
        if cfg.get("wall_on"):
            for w in s.get("walls", []):
                if w["qty"] >= cfg.get("wall_min", 100) and abs(w["price"]-mid) <= cfg["approach"]*2:
                    key = f"wall_{round(w['price'])}_{w['side']}"
                    if now - self._alert_state.get(key, 0) > 600:      # cooldown 10 min
                        sidetxt = "support" if w["side"] == "bid" else "résistance"
                        self.notifier.send(
                            f"🧱 Gros mur {sidetxt} @ {w['price']:,.0f} — {w['qty']:.0f} BTC "
                            f"({w['qty']*w['price']/1e6:.1f} M$), à {abs(w['price']-mid):.0f}$ du prix.")
                        self._alert_state[key] = now

        # --- C. accélération du volume (marché qui s'active anormalement) ---
        if cfg.get("accel_on") and self._tape_ema and self._tape_ema > 1:
            if tape >= cfg.get("accel_factor", 2.0) * self._tape_ema:
                key = "accel"
                if now - self._alert_state.get(key, 0) > 300:          # cooldown 5 min
                    self.notifier.send(
                        f"⚡ Accélération : {tape:.0f} trades/s vs ~{self._tape_ema:.0f} "
                        f"d'habitude ({tape/self._tape_ema:.1f}x) @ {mid:,.0f}. "
                        f"Le marché s'active — surveille.")
                    self._alert_state[key] = now

    def _alerts_refresh_log(self):
        w = getattr(self, "al_log", None)
        if w is None:
            return
        import time as _t
        logs = self.notifier.recent_log(20)
        if not logs:
            self._hset(w, f"<span style='color:{DIM};'>Aucune alerte envoyée pour l'instant.</span>")
            return
        rows = []
        for e in reversed(logs):
            t = _t.strftime("%H:%M:%S", _t.localtime(e["ts"]))
            col = GREEN if e["ok"] else RED
            mark = "✅" if e["ok"] else "❌"
            txt = e["text"].replace("\n", " · ")
            err = "" if e["ok"] else f" [{e['err']}]"
            rows.append(f"<div style='margin-bottom:5px;'><span style='color:{DIM};'>{t}</span> "
                        f"<span style='color:{col};'>{mark}</span> "
                        f"<span style='color:{TXT};'>{txt}{err}</span></div>")
        self._hset(w, "".join(rows))

    def _alerts_apply(self):
        import re
        def num(w, dflt):
            try:
                return float(str(w.text()).replace(",", ".").replace(" ", ""))
            except (ValueError, AttributeError):
                return dflt
        lv = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", self.al_levels.text())
              if float(x) > 100]
        self._alert_cfg.update({
            "enabled": self.al_enable.isChecked(),
            "channel": "ntfy",
            "ntfy_topic": self.al_ntfy.text().strip(),
            "tg_token": self.al_tgtoken.text().strip(),
            "tg_chat": self.al_tgchat.text().strip(),
            "tg_bot_on": self.al_tgbot_on.isChecked(),
            "start_h": self.al_start.value(), "end_h": self.al_end.value(),
            "levels": sorted(set(lv)),
            "approach": num(self.al_approach, 75.0),
            "live_interval": int(num(self.al_interval, 45)),
            "approach_on": self.al_approach_on.isChecked(),
            "wall_on": self.al_wall_on.isChecked(),
            "wall_min": num(self.al_wall_min, 100.0),
            "accel_on": self.al_accel_on.isChecked(),
            "accel_factor": num(self.al_accel_factor, 2.0),
        })
        self._alerts_save_cfg()
        self._apply_notifier_cfg()
        self._start_telegram_bot()
        self.al_status.setText("✅ Réglages enregistrés et appliqués.")
        self.al_status.setStyleSheet(f"color:{GREEN};font-size:13px;font-weight:700;")

    def _alerts_test(self):
        self._alerts_apply()
        ok, err = self.notifier.send_now(
            "✅ Test Order Flow Cockpit — si tu lis ça, tes alertes WhatsApp fonctionnent.")
        if ok:
            self.al_status.setText("✅ Message test envoyé sur WhatsApp.")
            self.al_status.setStyleSheet(f"color:{GREEN};font-size:13px;font-weight:700;")
        else:
            self.al_status.setText(f"❌ Échec de l'envoi : {err}")
            self.al_status.setStyleSheet(f"color:{RED};font-size:13px;font-weight:700;")

    def _build_alerts_page(self):
        """Page d'INFO : les alertes et le bot Telegram tournent désormais sur le
        serveur cloud 24/7 (server.py). Côté PC on ne fait plus rien — on rappelle
        juste comment tout se pilote depuis Telegram."""
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(28, 28, 28, 28); outer.setSpacing(16)

        title = QtWidgets.QLabel("🔔  Alertes & bot — gérés par ton serveur 24/7")
        title.setStyleSheet(f"color:{TXT};font-size:20px;font-weight:800;")
        outer.addWidget(title)

        info = QtWidgets.QLabel(
            "Les alertes ntfy et le bot Telegram tournent maintenant sur ton serveur "
            "cloud, en permanence — même PC éteint. Cette appli ne s'occupe PLUS des "
            "alertes ni du bot, exprès, pour qu'il soit impossible d'avoir des doublons "
            "ou un conflit. Tu n'as rien à activer ou désactiver ici.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{TXT};font-size:14px;background:{PANEL};"
                           f"border:1px solid {BORDER};border-radius:12px;padding:16px;line-height:150%;")
        outer.addWidget(info)

        howto = QtWidgets.QLabel(
            "<b>Tu pilotes tout depuis Telegram</b>, sur ton bot :<br><br>"
            "• <b>/status</b> — état du serveur + prix + tes niveaux<br>"
            "• <b>/niveaux 61000, 62000</b> — changer tes niveaux surveillés<br>"
            "• <b>/update</b> — forcer la dernière version du code<br>"
            "• <b>une question libre</b> — le copilote répond avec les données live<br><br>"
            "<span style='color:#8a94a6;'>Accès serveur (rare) : console.cloud.google.com "
            "→ Compute Engine → VM instances → bouton SSH.</span>")
        howto.setWordWrap(True); howto.setTextFormat(QtCore.Qt.TextFormat.RichText)
        howto.setStyleSheet(f"color:{TXT};font-size:14px;background:{PANEL2};"
                            f"border:1px solid {BORDER};border-radius:12px;padding:16px;line-height:170%;")
        outer.addWidget(howto)
        outer.addStretch(1)
        return page

    # ===========================================================
    # PAGE 8 — NEWS CRYPTO + GUIDE MACRO
    # ===========================================================

    def _build_news_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Actualités crypto en direct (CoinDesk + Cointelegraph, rafraîchies toutes les 5 min), "
            "triées par importance. Chaque titre est classé automatiquement : "
            "🔴 MAJEURE / 🟠 MOYENNE / ⚪ FAIBLE, avec l'impact probable sur BTC "
            "(📈 haussier / 📉 baissier / ➖ incertain). Classement par mots-clés : "
            "fiable pour filtrer, mais lis le titre avant de trader dessus.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # ---- Fear & Greed hero ----
        self.fng_box = QtWidgets.QFrame()
        self.fng_box.setStyleSheet(
            f"QFrame{{background:{PANEL};border:2px solid {BORDER};border-radius:14px;}}")
        fb = QtWidgets.QHBoxLayout(self.fng_box)
        fb.setContentsMargins(22, 14, 22, 14); fb.setSpacing(24)
        cap = QtWidgets.QLabel("FEAR & GREED")
        cap.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:1.5px;border:none;")
        self.fng_val = QtWidgets.QLabel("—")
        self.fng_val.setStyleSheet(f"color:{TXT};font-size:34px;font-weight:800;border:none;")
        self.fng_txt = QtWidgets.QLabel("Chargement de l'indice de sentiment…")
        self.fng_txt.setWordWrap(True)
        self.fng_txt.setStyleSheet(f"color:{TXT};font-size:13px;border:none;")
        fb.addWidget(cap); fb.addWidget(self.fng_val); fb.addWidget(self.fng_txt, 1)
        outer.addWidget(self.fng_box)

        # ---- fil d'actus trié, pleine largeur ----
        self.news_head = self._h("FIL D'ACTUALITÉS TRIÉ PAR IMPORTANCE  ·  en attente du premier chargement…")
        outer.addWidget(self.news_head)
        self.news_browser = QtWidgets.QTextBrowser()
        self.news_browser.setOpenExternalLinks(True)
        self.news_browser.setStyleSheet(
            f"QTextBrowser{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;color:{TXT};font-size:13px;padding:12px;}}")
        outer.addWidget(self.news_browser, 1)
        return page

    def _refresh_news(self):
        items, fng, last_fetch = self.newsfeed.get_news()

        # Fear & Greed
        if fng:
            v = fng["value"]
            col = RED if v < 25 else (AMBER if v < 45 else (DIM if v < 55 else GREEN))
            self.fng_val.setText(str(v))
            self.fng_val.setStyleSheet(f"color:{col};font-size:34px;font-weight:800;border:none;")
            trend = ""
            if "yesterday" in fng:
                d = v - fng["yesterday"]
                trend = f"  ({d:+d} vs hier)"
            label_fr = {"Extreme Fear": "PEUR EXTRÊME — historiquement une zone d'achat pour les patients",
                        "Fear": "PEUR — le marché est nerveux, les mains faibles vendent",
                        "Neutral": "NEUTRE — pas d'excès de sentiment",
                        "Greed": "AVIDITÉ — optimisme fort, attention aux excès",
                        "Extreme Greed": "AVIDITÉ EXTRÊME — euphorie, zone de distribution classique"}
            self.fng_txt.setText(f"{fng['label']}{trend} — "
                                 f"{label_fr.get(fng['label'], '')}")
            self.fng_box.setStyleSheet(
                f"QFrame{{background:{PANEL};border:2px solid {col};border-radius:14px;}}")

        # fil d'actus trié : importance décroissante, puis plus récent d'abord
        if items:
            import time as _t
            age_min = (_t.time() - last_fetch) / 60 if last_fetch else 0
            n3 = sum(1 for it in items if it.get("importance", 1) >= 3)
            n2 = sum(1 for it in items if it.get("importance", 1) == 2)
            self.news_head.setText(
                f"FIL TRIÉ PAR IMPORTANCE  ·  {len(items)} titres  ·  "
                f"🔴 {n3} majeures  ·  🟠 {n2} moyennes  ·  maj il y a {age_min:.0f} min")
            ordered = sorted(items, key=lambda it: (-it.get("importance", 1),
                                                    -it.get("ts", 0)))
            IMP = {3: ("🔴 MAJEURE", RED), 2: ("🟠 MOYENNE", AMBER), 1: ("⚪ FAIBLE", DIM)}
            IMPACT = {"haussier": ("📈 HAUSSIER BTC", GREEN),
                      "baissier": ("📉 BAISSIER BTC", RED),
                      "incertain": ("➖ INCERTAIN", DIM)}
            html = []
            for it in ordered:
                imp = it.get("importance", 1)
                imp_txt, imp_col = IMP[imp]
                ima_txt, ima_col = IMPACT[it.get("impact", "incertain")]
                src_col = "#f3ba2f" if it["source"] == "CoinDesk" else ACCENT
                date_short = it["date"][:22] if it["date"] else ""
                keys = it.get("keys", [])
                keys_html = (f"  <span style='color:{DIM};font-size:11px;'>"
                             f"[{', '.join(keys)}]</span>") if keys else ""
                html.append(
                    f"<div style='margin-bottom:12px;'>"
                    f"<span style='color:{imp_col};font-weight:800;font-size:11px;'>{imp_txt}</span>"
                    f"  <span style='color:{ima_col};font-weight:800;font-size:11px;'>{ima_txt}</span>"
                    f"  <span style='color:{DIM};font-size:11px;'>{date_short} · "
                    f"<span style='color:{src_col};'>{it['source']}</span></span>{keys_html}<br>"
                    f"<a href='{it['link']}' style='color:{AMBER if imp >= 3 else TXT};"
                    f"font-weight:{'800' if imp >= 3 else ('600' if imp == 2 else '400')};"
                    f"text-decoration:none;'>{it['title']}</a></div>")
            self._hset(self.news_browser, "".join(html))
        elif last_fetch == 0:
            self._hset(self.news_browser,
                f"<span style='color:{DIM};'>Chargement des actualités… "
                f"(premier fetch en cours, ~10 secondes)</span>")

    # ===========================================================
    # PAGE 9 — COPILOTE IA (Claude)
    # ===========================================================

    def _build_ai_page(self):
        from ai_copilot import MODELS as AI_MODELS
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Copilote Claude : il lit TOUT ton cockpit (carnet, CVD, VWAP, murs, "
            "positionnement, news) et conclut. Budget strictement plafonné à 2,20 $/jour — "
            "une fois atteint, plus aucun appel jusqu'à minuit. Chaque appel ≈ 2-3 Ko de "
            "connexion (négligeable). Mode auto : 1 analyse toutes les 5 min + analyse "
            "immédiate sur événement majeur (sweep, mur retiré).")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # ---- config : clé + modèle + auto ----
        cfg = QtWidgets.QHBoxLayout(); cfg.setSpacing(8)
        self.ai_key_edit = QtWidgets.QLineEdit()
        self.ai_key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.ai_key_edit.setPlaceholderText("Clé API Anthropic (sk-ant-…) — console.anthropic.com")
        self.ai_key_edit.setStyleSheet(
            f"QLineEdit{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:7px;}}")
        if self.copilot.key:
            self.ai_key_edit.setText(self.copilot.key)
        cfg.addWidget(self.ai_key_edit, 3)
        btn_css = (f"QPushButton{{background:{PANEL2};color:{TXT};border:1px solid {BORDER};"
                   f"border-radius:8px;font-weight:700;padding:7px 14px;}}"
                   f"QPushButton:hover{{border:1px solid {ACCENT};}}")
        save_btn = QtWidgets.QPushButton("💾 Enregistrer la clé")
        save_btn.setStyleSheet(btn_css)
        save_btn.clicked.connect(self._ai_save_key)
        cfg.addWidget(save_btn)
        self.ai_model_combo = QtWidgets.QComboBox()
        self.ai_model_combo.addItems(list(AI_MODELS.keys()))
        self.ai_model_combo.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:6px;}}")
        cfg.addWidget(self.ai_model_combo)
        self.ai_auto_chk = QtWidgets.QCheckBox("🔄 Auto (5 min + événements)")
        self.ai_auto_chk.setChecked(False)
        self.ai_auto_chk.setStyleSheet(f"QCheckBox{{color:{TXT};font-weight:700;}}")
        cfg.addWidget(self.ai_auto_chk)
        outer.addLayout(cfg)

        # ---- budget ----
        brow = QtWidgets.QHBoxLayout(); brow.setSpacing(10)
        self.ai_budget_lbl = QtWidgets.QLabel("Budget : —")
        self.ai_budget_lbl.setStyleSheet(f"color:{TXT};font-size:13px;font-weight:700;")
        self.ai_budget_bar = QtWidgets.QProgressBar()
        self.ai_budget_bar.setRange(0, 1000); self.ai_budget_bar.setFixedHeight(18)
        self.ai_budget_bar.setFormat("")
        brow.addWidget(self.ai_budget_lbl); brow.addWidget(self.ai_budget_bar, 1)
        analyse_btn = QtWidgets.QPushButton("🔍 Analyser maintenant")
        analyse_btn.setStyleSheet(btn_css)
        analyse_btn.clicked.connect(lambda: self._ai_request("demande manuelle"))
        brow.addWidget(analyse_btn)
        outer.addLayout(brow)

        # ---- statut ----
        self.ai_status = QtWidgets.QLabel("Prêt.")
        self.ai_status.setWordWrap(True)
        self.ai_status.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(self.ai_status)

        # ---- corps : analyse à gauche, chat à droite ----
        body = QtWidgets.QHBoxLayout(); body.setSpacing(10); outer.addLayout(body, 1)

        lc = QtWidgets.QVBoxLayout(); lc.setSpacing(6)
        lc.addWidget(self._h("ANALYSE DU COPILOTE  (auto / bouton)"))
        self.ai_output = QtWidgets.QTextEdit(); self.ai_output.setReadOnly(True)
        self.ai_output.setStyleSheet(
            f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;color:{TXT};font-size:14px;padding:14px;}}")
        self.ai_output.setHtml(
            f"<span style='color:{DIM};'>Colle ta clé API, coche Auto ou clique "
            f"« Analyser maintenant ». L'analyse apparaîtra ici.</span>")
        lc.addWidget(self.ai_output, 3)
        lc.addWidget(self._h("HISTORIQUE"))
        self.ai_history = QtWidgets.QTextEdit(); self.ai_history.setReadOnly(True)
        self.ai_history.setStyleSheet(
            f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;color:{TXT};font-size:12px;padding:10px;}}")
        lc.addWidget(self.ai_history, 1)
        self._ai_history_html = []
        body.addLayout(lc, 1)

        # ---- chat : parle avec le copilote ----
        rc = QtWidgets.QVBoxLayout(); rc.setSpacing(6)
        chat_head = QtWidgets.QHBoxLayout()
        chat_head.addWidget(self._h("💬 PARLE AVEC TON COPILOTE  ·  il voit tes données live"))
        chat_head.addStretch()
        clear_btn = QtWidgets.QPushButton("🗑 Nouvelle conversation")
        clear_btn.setStyleSheet(
            f"QPushButton{{background:{PANEL2};color:{DIM};border:1px solid {BORDER};"
            f"border-radius:6px;font-size:11px;padding:3px 10px;}}"
            f"QPushButton:hover{{border:1px solid {ACCENT};color:{TXT};}}")
        clear_btn.clicked.connect(self._ai_clear_chat)
        chat_head.addWidget(clear_btn)
        rc.addLayout(chat_head)

        self.ai_chat = QtWidgets.QTextEdit(); self.ai_chat.setReadOnly(True)
        self.ai_chat.setStyleSheet(
            f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;color:{TXT};font-size:13px;padding:12px;}}")
        self.ai_chat.setHtml(
            f"<span style='color:{DIM};'>Pose-lui n'importe quelle question : "
            f"« pourquoi le prix ne monte pas ? », « explique-moi le funding actuel », "
            f"« ce mur à tel prix, je le trade comment ? »… Il répond en voyant "
            f"tes données du moment et se souvient de la conversation.</span>")
        rc.addWidget(self.ai_chat, 1)

        input_row = QtWidgets.QHBoxLayout(); input_row.setSpacing(8)
        self.ai_chat_input = QtWidgets.QLineEdit()
        self.ai_chat_input.setPlaceholderText("Ta question… (Entrée pour envoyer)")
        self.ai_chat_input.setStyleSheet(
            f"QLineEdit{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:9px;font-size:13px;}}")
        self.ai_chat_input.returnPressed.connect(self._ai_send_chat)
        input_row.addWidget(self.ai_chat_input, 1)
        send_btn = QtWidgets.QPushButton("📤 Envoyer")
        send_btn.setStyleSheet(btn_css)
        send_btn.clicked.connect(self._ai_send_chat)
        input_row.addWidget(send_btn)
        rc.addLayout(input_row)
        body.addLayout(rc, 1)

        self._ai_chat_html = []
        return page

    def _ai_send_chat(self):
        q = self.ai_chat_input.text().strip()
        if not q:
            return
        self.copilot.model_label = self.ai_model_combo.currentText()
        ok = self.copilot.request_chat(q, self._ai_snapshot())
        if not ok:
            self.ai_status.setText(f"⚠ {self.copilot.error}")
            return
        self.ai_chat_input.clear()
        self._ai_chat_html.append(
            f"<div style='margin-bottom:10px;'><span style='color:{ACCENT};"
            f"font-weight:800;'>Toi :</span> <span style='color:{TXT};'>{q}</span></div>")
        self._ai_chat_html.append(
            f"<div style='margin-bottom:10px;color:{DIM};font-style:italic;' "
            f"id='pending'>⏳ Le copilote réfléchit…</div>")
        self.ai_chat.setHtml("".join(self._ai_chat_html))
        self.ai_chat.verticalScrollBar().setValue(
            self.ai_chat.verticalScrollBar().maximum())

    def _ai_clear_chat(self):
        self.copilot.reset_chat()
        self._ai_chat_html = []
        self.ai_chat.setHtml(f"<span style='color:{DIM};'>Nouvelle conversation — "
                             f"le copilote repart de zéro.</span>")

    def _ai_save_key(self):
        self.copilot.set_key(self.ai_key_edit.text())
        self.ai_status.setText("✅ Clé enregistrée." if self.copilot.key
                               else "Clé effacée.")

    def _ai_snapshot(self):
        """Construit l'instantané compact envoyé à Claude (~1-2 Ko)."""
        s = self._last_state or {}
        L = ["Instantané BTC perp (agrégé Binance+OKX+Bybit) :"]
        if s.get("mid"):
            L.append(f"prix={s['mid']:.0f} spread={s.get('spread', 0):.1f}$ "
                     f"carnet={s.get('imbalance', 0)*100:.0f}%achat "
                     f"agresseurs5s={s.get('aggressor_ratio', 0)*100:.0f}%achat "
                     f"tape={s.get('tape_speed', 0):.1f}tr/s")
        v = self.engine.get_vwap()
        if v:
            L.append(f"VWAP={v['vwap']:.0f} ecart={v['dev_pct']:+.2f}%")
        cvds = self.engine.get_cvd_windows()
        parts = [f"{m}min:{d['cvd']:+.0f}" for m, d in cvds.items() if d.get("ready")]
        if parts:
            L.append("CVD(BTC) " + " ".join(parts))
        # MURS : uniquement les murs SOLIDES PROCHES (±400$, actionnables), triés par
        # proximité. On reste dans la zone que l'utilisateur voit réellement à l'écran
        # (sa page MURS est souvent filtrée serré) → plus de murs "fantômes" lointains.
        mid = s.get("mid")
        if mid:
            rep = self.engine.wall_history.report(15, mid=mid, top_n=20, max_dist=400)
            if rep.get("ready"):
                solid = [wl for wl in rep["top"] if wl["status"] in ("actif", "valide")]
                solid.sort(key=lambda wl: abs(wl["price"] - mid))
                for wl in solid[:6]:
                    L.append(
                        f"mur {'ACHAT' if wl['side']=='bid' else 'VENTE'} @{wl['price']:.0f} "
                        f"{wl['max_qty']:.0f}BTC (~{wl['max_qty']*wl['price']/1e6:.1f}M$) "
                        f"dist={abs(wl['price']-mid):.0f}$ age={wl['lifespan']:.0f}s "
                        f"testé{wl['tests']}x statut={wl['status']}")
            if not any("mur" in x for x in L[-6:]):
                L.append("(aucun gros mur dans ±400$ du prix actuellement)")
        seg = self.engine.get_flow_segments(300)
        if seg:
            L.append(f"delta5min retail={seg['retail']['delta']:+.1f} "
                     f"moyen={seg['mid']['delta']:+.1f} instit={seg['inst']['delta']:+.1f} BTC")
        vp = self.engine.get_volume_profile(3600)
        if vp:
            L.append(f"POC={vp['poc']:.0f} VAH={vp['vah']:.0f} VAL={vp['val']:.0f}")
        pos = self.engine.get_positioning()
        f = pos.get("funding"); oi = pos.get("oi")
        if f:
            L.append(f"funding={f['rate_pct']:+.4f}%")
        if oi:
            L.append(f"OI={oi['now']:.0f}BTC 5min={oi['chg_5m_pct']:+.2f}% "
                     f"15min={oi['chg_15m_pct']:+.2f}%")
        lq = pos.get("liq_5m", {})
        if lq.get("long_usd", 0) + lq.get("short_usd", 0) > 0:
            L.append(f"liquidations5min longs={lq['long_usd']/1e6:.2f}M$ "
                     f"shorts={lq['short_usd']/1e6:.2f}M$")
        casc = self.engine.get_cascade_sweeps(300)
        if casc:
            c = casc[0]
            L.append(f"dernier sweep: {c['side']} {c['qty']:.1f}BTC "
                     f"sur {c['levels']} niveaux a {c['ts']}")
        # absorption détectée
        ab = s.get("absorption")
        if ab:
            L.append(f"ABSORPTION ({ab[0]}): {ab[1][:90]}")
        # high/low de session
        if s.get("sess_hi") and s.get("sess_lo"):
            L.append(f"session: high={s['sess_hi']:.0f} low={s['sess_lo']:.0f}")
        # TES niveaux (ceux saisis sur la page EXÉCUTION)
        my = getattr(self, "_manual_levels", [])
        if my and mid:
            lf = self.engine.get_levels_flow(my, tol=30, window_s=3600)
            for p in sorted(my, key=lambda x: abs(x - mid)):
                b, sv = lf.get(p, (0, 0))
                car = "accumulé" if b >= sv else "distribué"
                L.append(f"MON NIVEAU {p:.0f} (dist {abs(p-mid):.0f}$): "
                         f"achat {b:.0f} vs vente {sv:.0f} BTC = {car}")
        items, fng, _ = self.newsfeed.get_news()
        if fng:
            L.append(f"FearGreed={fng['value']} ({fng['label']})")
        hot = [it["title"] for it in items if it.get("hot")][:3]
        for t in hot:
            L.append(f"news importante: {t}")
        L.append("(fenêtre US 13h-16h = plus de mouvement)")
        return "\n".join(L)

    def _ai_request(self, reason):
        self.copilot.model_label = self.ai_model_combo.currentText()
        ok = self.copilot.request(self._ai_snapshot(), reason)
        if ok:
            self.ai_status.setText(f"⏳ Analyse en cours ({reason})…")
        elif self.copilot.error:
            self.ai_status.setText(f"⚠ {self.copilot.error}")

    def _ai_on_event(self, kind, text):
        """Déclenche une analyse immédiate sur événement majeur (mode auto)."""
        import time as _t
        if not getattr(self, "ai_auto_chk", None) or not self.ai_auto_chk.isChecked():
            return
        now = _t.time()
        if now - self._ai_last_event < 90:      # au max 1 analyse-événement / 90s
            return
        self._ai_last_event = now
        self._ai_request(f"événement: {kind} — {text[:60]}")

    def _ai_tick(self):
        import time as _t
        c = self.copilot
        # budget
        left = c.budget_left()
        spent = c.daily_budget - left
        self.ai_budget_lbl.setText(
            f"Aujourd'hui : {spent:.3f} $ / {c.daily_budget:.2f} $  ·  {c.n_calls_today} appels")
        frac = min(1.0, spent / c.daily_budget) if c.daily_budget else 0
        col = GREEN if frac < 0.7 else (AMBER if frac < 0.95 else RED)
        self.ai_budget_bar.setValue(int(frac * 1000))
        self.ai_budget_bar.setStyleSheet(
            f"QProgressBar{{background:{PANEL2};border:1px solid {BORDER};border-radius:5px;}}"
            f"QProgressBar::chunk{{background:{col};border-radius:4px;}}")
        # nouveau résultat ?
        r = c.consume_result()
        if r and r.get("kind") == "chat":
            # retire le "réfléchit…" et affiche la réponse
            self._ai_chat_html = [h for h in self._ai_chat_html if "pending" not in h]
            ans = r["text"].replace("\n", "<br>")
            self._ai_chat_html.append(
                f"<div style='margin-bottom:14px;'><span style='color:{GREEN};"
                f"font-weight:800;'>🤖 Copilote :</span><br>"
                f"<span style='color:{TXT};line-height:145%;'>{ans}</span><br>"
                f"<span style='color:{DIM};font-size:10px;'>{r['cost']*100:.2f} centimes</span></div>")
            self.ai_chat.setHtml("".join(self._ai_chat_html))
            self.ai_chat.verticalScrollBar().setValue(
                self.ai_chat.verticalScrollBar().maximum())
            self.ai_status.setText("✅ Réponse reçue.")
            r = None
        if r:
            html = r["text"].replace("\n", "<br>")
            for tag, colr in [("BIAIS:", ACCENT), ("LECTURE:", AMBER),
                              ("PROJECTION:", "#5ac8fa"), ("NIVEAUX:", VIOLET), ("PLAN:", GREEN)]:
                html = html.replace(tag, f"<b style='color:{colr};'>{tag}</b>")
            self.ai_output.setHtml(
                f"<div style='color:{DIM};font-size:11px;margin-bottom:8px;'>"
                f"{r['ts']}  ·  {r['model']}  ·  {r['reason']}  ·  "
                f"{r['cost']*100:.2f} centimes  ·  {r['in_tok']}→{r['out_tok']} tokens</div>"
                f"<div style='line-height:150%;'>{html}</div>")
            self._ai_history_html.insert(0,
                f"<div style='margin-bottom:10px;'><span style='color:{DIM};'>{r['ts']}"
                f" · {r['reason']}</span><br><span style='color:{TXT};'>"
                f"{r['text'][:200].replace(chr(10), ' ')}…</span></div>")
            self._ai_history_html = self._ai_history_html[:20]
            self.ai_history.setHtml("".join(self._ai_history_html))
            self.ai_status.setText("✅ Analyse reçue.")
        elif c.error and not c._busy:
            self.ai_status.setText(f"⚠ {c.error}")
        # mode auto : 1 analyse / 5 min
        if (self.ai_auto_chk.isChecked() and c.key and not c._busy
                and _t.time() - self._ai_last_auto >= 300):
            self._ai_last_auto = _t.time()
            self._ai_request("auto 5 min")

    # ===========================================================
    # PAGE EXÉCUTION — surveille les niveaux clés, attend l'approche,
    # lit le flux (CVD/agresseurs/VWAP/murs) → REVERSE ou CONTINUE
    # ===========================================================

    def _build_exec_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Page exécution : l'app surveille les niveaux clés (high/low de session, "
            "murs validés, VWAP, POC). Quand le prix S'APPROCHE d'un niveau, elle lit le "
            "flux (CVD + agresseurs + VWAP + mur) et conclut : REVERSE (rejet probable) "
            "ou CONTINUE (cassure probable). Filtre horaire : la fenêtre 13h-16h "
            "(session US) concentre le plus de mouvement.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # MES NIVEAUX : ceux tracés sur TradingView, saisis à la main
        self._manual_levels = self._exec_load_levels()
        mrow = QtWidgets.QHBoxLayout(); mrow.setSpacing(8)
        mlbl = QtWidgets.QLabel("🎯 MES NIVEAUX :")
        mlbl.setStyleSheet(f"color:{AMBER};font-size:12px;font-weight:800;")
        mrow.addWidget(mlbl)
        self.exec_levels_input = QtWidgets.QLineEdit()
        self.exec_levels_input.setText(", ".join(f"{x:.0f}" for x in self._manual_levels))
        self.exec_levels_input.setPlaceholderText(
            "Tes niveaux tracés sur TradingView, séparés par des virgules : 62500, 61800, 63000…")
        self.exec_levels_input.setStyleSheet(
            f"QLineEdit{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:8px;font-size:13px;}}")
        self.exec_levels_input.returnPressed.connect(self._exec_save_levels)
        mrow.addWidget(self.exec_levels_input, 1)
        savebtn = QtWidgets.QPushButton("💾 Enregistrer")
        savebtn.setStyleSheet(
            f"QPushButton{{background:{PANEL2};color:{TXT};border:1px solid {BORDER};"
            f"border-radius:8px;font-weight:700;padding:8px 14px;}}"
            f"QPushButton:hover{{border:1px solid {ACCENT};}}")
        savebtn.clicked.connect(self._exec_save_levels)
        mrow.addWidget(savebtn)
        outer.addLayout(mrow)

        # horloge de session
        crow = QtWidgets.QHBoxLayout(); crow.setSpacing(10)
        self.exec_clock = QtWidgets.QLabel("—")
        self.exec_clock.setStyleSheet(f"color:{TXT};font-size:15px;font-weight:800;"
                                      f"background:{PANEL};border:1px solid {BORDER};"
                                      f"border-radius:10px;padding:10px 16px;")
        crow.addWidget(self.exec_clock)
        hint = QtWidgets.QLabel("Fenêtre active recommandée : 13h00–16h00 (heure locale)")
        hint.setStyleSheet(f"color:{DIM};font-size:11px;")
        crow.addWidget(hint); crow.addStretch()
        outer.addLayout(crow)

        # bandeau FLUX ACTUEL (contexte global, une ligne claire)
        self.exec_flux = QtWidgets.QLabel("Flux : …")
        self.exec_flux.setStyleSheet(
            f"color:{TXT};font-size:14px;font-weight:700;background:{PANEL};"
            f"border:1px solid {BORDER};border-radius:10px;padding:12px 16px;")
        outer.addWidget(self.exec_flux)

        # setup actif : une ligne verdict qui ressort quand le prix approche
        self.exec_verdict = QtWidgets.QLabel("En attente d'une approche de niveau clé…")
        self.exec_verdict.setWordWrap(True)
        self.exec_verdict.setStyleSheet(
            f"color:{DIM};font-size:16px;font-weight:800;background:{PANEL2};"
            f"border:2px solid {BORDER};border-radius:10px;padding:12px 16px;")
        outer.addWidget(self.exec_verdict)

        # GRAND TABLEAU : uniquement TES niveaux + mur proche + flux + verdict
        outer.addWidget(self._h("ANALYSE DE TES NIVEAUX  ·  ⚡ = le prix approche  ·  "
                                "A/V ce niveau = volume acheté/vendu autour ±30$"))
        cols = ["État", "Ton niveau", "Côté", "Distance", "Mur le + proche", "Taille mur",
                "A/V à ce niveau", "A/V actuel (1min)", "CVD 1min", "Agress.",
                "VERDICT", "Action"]
        self.exec_table = QtWidgets.QTableWidget(0, len(cols))
        self.exec_table.setHorizontalHeaderLabels(cols)
        self._prep(self.exec_table)
        self.exec_table.verticalHeader().setDefaultSectionSize(36)   # lignes hautes
        outer.addWidget(self.exec_table, 1)
        return page

    def _exec_load_levels(self):
        try:
            import os
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mes_niveaux.txt")
            with open(p, encoding="utf-8") as f:
                import re
                return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", f.read())]
        except (OSError, ValueError):
            return []

    def _exec_save_levels(self):
        import re, os
        txt = self.exec_levels_input.text()
        vals = []
        for m in re.findall(r"\d+(?:\.\d+)?", txt):
            try:
                v = float(m)
                if v > 100:                      # filtre les labels/petits nombres
                    vals.append(v)
            except ValueError:
                pass
        self._manual_levels = sorted(set(vals))
        d = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(d, "mes_niveaux.txt"), "w", encoding="utf-8") as f:
                f.write(", ".join(f"{x:.0f}" for x in self._manual_levels))
        except OSError:
            pass
        # envoie les niveaux au SERVEUR 24/7 (via GitHub) — en tâche de fond
        self._push_levels_to_server(d)
        self.exec_levels_input.setText(", ".join(f"{x:.0f}" for x in self._manual_levels))

    def _push_levels_to_server(self, repo_dir):
        """Écrit niveaux.json et le pousse sur GitHub pour que le serveur cloud
        (server.py) le récupère et surveille ces niveaux 24/7, même PC éteint.
        Tourne dans un thread, ne bloque jamais l'appli, échoue en silence."""
        import json as _json
        levels = list(self._manual_levels)
        try:
            with open(os.path.join(repo_dir, "niveaux.json"), "w", encoding="utf-8") as f:
                _json.dump({"levels": levels}, f)
        except OSError:
            return

        def _push():
            import subprocess
            try:
                env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
                subprocess.run(["git", "add", "niveaux.json"], cwd=repo_dir,
                               timeout=20, capture_output=True, env=env)
                r = subprocess.run(["git", "commit", "-m", "maj niveaux"], cwd=repo_dir,
                                   timeout=20, capture_output=True, env=env)
                if r.returncode != 0 and b"nothing to commit" in (r.stdout + r.stderr):
                    return
                subprocess.run(["git", "pull", "--rebase", "--quiet"], cwd=repo_dir,
                               timeout=30, capture_output=True, env=env)
                subprocess.run(["git", "push", "--quiet"], cwd=repo_dir,
                               timeout=30, capture_output=True, env=env)
            except Exception:
                pass

        import threading
        threading.Thread(target=_push, daemon=True).start()

    def _exec_levels(self):
        """UNIQUEMENT tes niveaux saisis, triés par proximité au prix."""
        s = self._last_state or {}
        mid = s.get("mid")
        if not mid:
            return [], None
        out = [{"kind": "🎯", "price": p, "manual": True}
               for p in getattr(self, "_manual_levels", [])]
        out.sort(key=lambda x: abs(x["price"] - mid))
        return out, mid

    def _exec_nearest_wall(self, price, s):
        """Mur le plus proche d'un prix (dans ±400$) → (prix_mur, qty, usd) ou None."""
        best = None; bd = 400
        for w in s.get("walls", []):
            d = abs(w["price"] - price)
            if d <= bd:
                bd = d; best = w
        return best

    def _exec_flow(self, s):
        """Calcule le flux global UNE fois (partagé par tous les niveaux)."""
        agg = s.get("aggressor_ratio", 0.5)
        cvds = self.engine.get_cvd_windows()
        c1 = cvds.get(1, {})
        cvd1 = c1.get("cvd", 0) if c1.get("ready") else 0
        buy_vol = c1.get("buy_vol", 0) if c1.get("ready") else 0
        sell_vol = c1.get("sell_vol", 0) if c1.get("ready") else 0
        vw = self.engine.get_vwap()
        return {"agg": agg, "cvd1": cvd1, "tape": s.get("tape_speed", 0),
                "buy_vol": buy_vol, "sell_vol": sell_vol,
                "above_vwap": (vw["above"] if vw else None),
                "absorb": s.get("absorption"),
                "buyers": agg > 0.56 and cvd1 > 0,
                "sellers": agg < 0.44 and cvd1 < 0}

    def _exec_verdict(self, above, f):
        """Verdict pour un niveau donné selon le flux. -> (tag, dir, court, action)."""
        ab = f["absorb"]
        if above:   # RÉSISTANCE
            if f["buyers"] and f["tape"] > 6:
                return ("continue", "hausse", "CONTINUE ▲", "Ne pas shorter")
            if f["sellers"] or (ab and ab[0] == "baisse"):
                return ("reverse", "baisse", "REVERSE ▼", "Short si tient")
        else:       # SUPPORT
            if f["sellers"] and f["tape"] > 6:
                return ("continue", "baisse", "CONTINUE ▼", "Ne pas longer")
            if f["buyers"] or (ab and ab[0] == "hausse"):
                return ("reverse", "hausse", "REVERSE ▲", "Long si tient")
        return ("neutre", "neutre", "NEUTRE", "Attendre")

    def _refresh_exec(self):
        import time as _t
        now = _t.localtime()
        in_window = 13 <= now.tm_hour < 16
        wtxt = ("🔥 FENÊTRE ACTIVE (13h-16h) — mouvement maximal" if in_window
                else "⏸ hors fenêtre active — mouvements souvent plus mous")
        wcol = GREEN if in_window else DIM
        self.exec_clock.setText(f"🕐 {_t.strftime('%H:%M:%S')}   ·   {wtxt}")
        self.exec_clock.setStyleSheet(f"color:{wcol};font-size:15px;font-weight:800;"
                                      f"background:{PANEL};border:1px solid {wcol};"
                                      f"border-radius:10px;padding:10px 16px;")

        levels, mid = self._exec_levels()
        s = self._last_state or {}
        f = self._exec_flow(s) if mid else None
        approach = mid * 0.0012 if mid else 0   # ~75$ sur BTC

        # --- bandeau FLUX ACTUEL (avec volume achat vs vente) ---
        if f:
            vwtxt = (("prix > VWAP" if f["above_vwap"] else "prix < VWAP")
                     if f["above_vwap"] is not None else "VWAP —")
            self.exec_flux.setText(
                f"FLUX ACTUEL (1 min)   ·   Achat {f['buy_vol']:.0f} BTC  vs  "
                f"Vente {f['sell_vol']:.0f} BTC   ·   Agresseurs {f['agg']*100:.0f}% achat "
                f"(5s)   ·   CVD {f['cvd1']:+.0f}   ·   {vwtxt}   ·   Tape {f['tape']:.0f}/s"
                + ("   ·   ⚠ absorption" if f["absorb"] else ""))

        # pas de niveaux saisis → invite à en entrer
        if not levels or not mid:
            self.exec_table.setRowCount(0)
            self.exec_verdict.setText("Entre tes niveaux dans le champ 🎯 en haut — "
                                      "l'app fera l'analyse dessus.")
            self.exec_verdict.setStyleSheet(
                f"color:{AMBER};font-size:15px;font-weight:700;background:{PANEL2};"
                f"border:2px solid {AMBER};border-radius:10px;padding:12px 16px;")
            return

        # --- ligne verdict du niveau le plus proche ---
        nearest = levels[0]; nd = abs(nearest["price"] - mid)
        if nd <= approach:
            tag, dr, short, action = self._exec_verdict(nearest["price"] > mid, f)
            col = {"hausse": GREEN, "baisse": RED, "neutre": AMBER}.get(dr, TXT)
            side = "résistance" if nearest["price"] > mid else "support"
            warn = "" if in_window else "  ⚠ hors 13h-16h"
            self.exec_verdict.setText(
                f"⚡ SETUP : ton niveau @ {nearest['price']:,.0f} "
                f"({side}, {nd:.0f}$) → {short} · {action}{warn}")
            self.exec_verdict.setStyleSheet(
                f"color:{col};font-size:17px;font-weight:800;background:{PANEL2};"
                f"border:2px solid {col};border-radius:10px;padding:12px 16px;")
        else:
            self.exec_verdict.setText(
                f"Aucun de tes niveaux proche · le plus proche @ "
                f"{nearest['price']:,.0f} ({nd:.0f}$). Patiente jusqu'à l'approche.")
            self.exec_verdict.setStyleSheet(
                f"color:{DIM};font-size:15px;font-weight:700;background:{PANEL2};"
                f"border:2px solid {BORDER};border-radius:10px;padding:12px 16px;")

        # --- TABLEAU : tes niveaux + mur proche + flux par niveau + verdict ---
        self.exec_table.setRowCount(len(levels))
        # volume acheté/vendu autour de chaque niveau (±30$, 1h) — une seule passe
        prices = [x["price"] for x in levels]
        lvl_flow = self.engine.get_levels_flow(prices, tol=30.0, window_s=3600)
        # flux global actuel (1 min)
        gbuy, gsell = f["buy_vol"], f["sell_vol"]
        gav = f"{gbuy:.0f} / {gsell:.0f}"
        gcol = GREEN if gbuy >= gsell else RED
        for i, x in enumerate(levels):
            d = abs(x["price"] - mid)
            is_res = x["price"] > mid
            approaching = d <= approach
            tag, dr, short, action = self._exec_verdict(is_res, f)
            vcol = {"hausse": GREEN, "baisse": RED, "neutre": AMBER}.get(dr, TXT)
            wall = self._exec_nearest_wall(x["price"], s)
            wall_px = f"{wall['price']:,.0f} ({abs(wall['price']-x['price']):.0f}$)" if wall else "—"
            wall_sz = f"{wall['qty']:.0f} BTC · {wall['qty']*wall['price']/1e6:.1f} M$" if wall else "—"
            # A/V à ce niveau
            lb, ls = lvl_flow.get(x["price"], (0.0, 0.0))
            if lb + ls > 0:
                lav = f"{lb:.0f} / {ls:.0f} BTC"
                lavcol = GREEN if lb >= ls else RED
            else:
                lav, lavcol = "—", DIM
            etat, ecol = ("⚡ APPROCHE", AMBER) if approaching else ("surveillé", DIM)
            cells = [
                (etat, ecol),
                (f"🎯 {x['price']:,.0f}", AMBER),
                ("résistance" if is_res else "support", RED if is_res else GREEN),
                (f"{d:,.0f}$", AMBER if approaching else TXT),
                (wall_px, TXT if wall else DIM),
                (wall_sz, GREEN if wall else DIM),
                (lav, lavcol),
                (gav, gcol),
                (f"{f['cvd1']:+.0f}", GREEN if f['cvd1'] >= 0 else RED),
                (f"{f['agg']*100:.0f}%", GREEN if f['agg'] > 0.5 else RED),
                (short if approaching else "—", vcol if approaching else DIM),
                (action if approaching else "—", TXT if approaching else DIM),
            ]
            for j, (v, cc) in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(v)
                it.setForeground(QtGui.QColor(cc))
                self.exec_table.setItem(i, j, it)

    def closeEvent(self,e):
        self.newsfeed.stop()
        try:
            self.notifier.stop()
        except Exception:
            pass
        try:
            if self._tg_bot:
                self._tg_bot.stop()
        except Exception:
            pass
        self.engine.stop(); super().closeEvent(e)


def main():
    app=QtWidgets.QApplication(sys.argv)
    app.setStyleSheet("QWidget{font-family:'Segoe UI','SF Pro Display',sans-serif;}"
                      "QToolTip{background:#10151e;color:#d8e0ea;border:1px solid #2a3543;padding:6px;}")
    win=Cockpit(); win.show(); sys.exit(app.exec())


if __name__=="__main__":
    main()
