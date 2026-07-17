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
from paths import app_dir, data_file


class Bridge(QtCore.QObject):
    state = QtCore.pyqtSignal(dict)
    journal_closed = QtCore.pyqtSignal()   # rattrapage hors-ligne -> refresh UI
    server_history = QtCore.pyqtSignal(dict)   # historique 24/7 reçu du serveur (GitHub)


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
        self.bridge.journal_closed.connect(self._journal_refresh)
        self.bridge.server_history.connect(self._apply_server_history)
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

        # flux QUANT (options Deribit + macro) — threads de fond, cache lu par l'UI
        from quant import QuantFeed
        self.quant = QuantFeed()
        self._quant_timer = QtCore.QTimer(self)
        self._quant_timer.timeout.connect(self._refresh_quant)
        self._quant_timer.start(2000)          # lit le cache toutes les 2s (instantané)

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
        self._vwap_page = self._build_vwap_page()
        self.tabs.addTab(self._vwap_page, "  📈  VWAP & CVD  ")

        # --- Page 5: Flux institutionnels ---
        self.tabs.addTab(self._build_instit_page(), "  🏦  INSTITUTIONNELS  ")

        # --- Page 6: Profil de volume + sweeps + stacked ---
        self.tabs.addTab(self._build_profil_page(), "  📊  PROFIL  ")

        # --- Page 7: OI + Funding + Liquidations ---
        self.tabs.addTab(self._build_pos_page(), "  🧭  POSITIONNEMENT  ")

        # --- Quant : options Deribit + macro ---
        self.tabs.addTab(self._build_quant_page(), "  📐  QUANT  ")

        # --- Footprint (order flow par bougie) ---
        self.tabs.addTab(self._build_footprint_page(), "  👣  FOOTPRINT  ")

        # --- Z-scores multi-échelles ---
        self.tabs.addTab(self._build_zscore_page(), "  📉  Z-SCORES  ")

        # --- Options Deribit détaillées (graphiques) ---
        self.tabs.addTab(self._build_options_page(), "  🎰  OPTIONS  ")

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

        # --- Coin haut-droit : PRIX BTC live + accès VWAP en 1 clic (sur TOUTES les pages) ---
        corner = QtWidgets.QWidget()
        crow = QtWidgets.QHBoxLayout(corner)
        crow.setContentsMargins(0, 0, 10, 0); crow.setSpacing(8)
        self.corner_price = QtWidgets.QLabel("₿ —")
        self.corner_price.setStyleSheet(
            f"color:{ACCENT};font-size:15px;font-weight:800;background:{PANEL};"
            f"border:1px solid {BORDER};border-radius:8px;padding:4px 12px;")
        crow.addWidget(self.corner_price)
        # P&L flottant live des positions ouvertes (caché s'il n'y en a pas)
        self.corner_pnl = QtWidgets.QLabel("")
        self.corner_pnl.setVisible(False)
        crow.addWidget(self.corner_pnl)
        vwap_btn = QtWidgets.QPushButton("📈 VWAP")
        vwap_btn.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
        vwap_btn.setToolTip("Aller à la page VWAP & CVD (raccourci : Ctrl+V)")
        vwap_btn.setStyleSheet(
            f"QPushButton{{color:{TXT};font-size:13px;font-weight:800;background:{PANEL};"
            f"border:1px solid {BORDER};border-radius:8px;padding:4px 12px;}}"
            f"QPushButton:hover{{border:1px solid {ACCENT};color:{ACCENT};}}")
        vwap_btn.clicked.connect(lambda: self.tabs.setCurrentWidget(self._vwap_page))
        crow.addWidget(vwap_btn)
        self.tabs.setCornerWidget(corner, QtCore.Qt.Corner.TopRightCorner)
        # raccourci clavier Ctrl+V -> page VWAP
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+V"), self,
                        activated=lambda: self.tabs.setCurrentWidget(self._vwap_page))

        # historique 24/7 du serveur (murs + agresseurs + liquidations) via GitHub :
        # au lancement (comble les trous PC éteint) puis toutes les 30 min
        QtCore.QTimer.singleShot(4000, self._fetch_server_history)
        self._srv_hist_timer = QtCore.QTimer(self)
        self._srv_hist_timer.timeout.connect(self._fetch_server_history)
        self._srv_hist_timer.start(1800 * 1000)

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
        self.wall_dist_combo.setCurrentText("Tout")
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
        self.wall_sort_combo.setCurrentText("Taille (BTC) ↓")
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

        # fenêtres courtes (minutes) + fenêtres LONGUES (mémoire longue : uniquement
        # les niveaux significatifs — validés, gros, icebergs — enregistrés au fil du temps)
        self.WALL_WINDOWS = [1, 5, 15, 30, 60, 1440, 10080, 20160]
        self.WALL_WIN_LABELS = {1: "1 min", 5: "5 min", 15: "15 min", 30: "30 min",
                                60: "60 min", 1440: "1 jour", 10080: "1 semaine",
                                20160: "2 semaines"}
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

            self.wall_tabs.addTab(wp, f"  {self.WALL_WIN_LABELS.get(m, str(m) + ' min')}  ")
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
        # bulles de TRADES exécutés (bookmap) : achat vert / vente rouge, taille ∝ volume
        self.hm_trades=pg.ScatterPlotItem(pen=None)
        self.hm_trades.setZValue(15); self.hm.addItem(self.hm_trades)
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

    def _fetch_server_history(self):
        """Télécharge (en fond) l'historique 24/7 publié par le serveur sur GitHub
        et l'envoie au thread UI pour fusion. Silencieux si non configuré."""
        import threading

        def _run():
            try:
                import github_sync
                data = github_sync.fetch()
            except Exception:
                data = None
            if data:
                self.bridge.server_history.emit(data)
        threading.Thread(target=_run, daemon=True).start()

    def _apply_server_history(self, data):
        """Fusionne l'historique du serveur (murs + métriques + liquidations) dans
        l'appli : comble les trous des périodes PC éteint. Tourne sur le thread UI."""
        import time as _t
        # 1) murs : mémoire longue du serveur (utilisée par les fenêtres jour/semaine)
        try:
            self.engine.wall_history.set_server_longterm(data.get("walls_longterm"))
        except Exception:
            pass
        # 2) métriques (agresseurs, CVD…) : comble les trous, garde le détail local (5 s)
        zs = data.get("zscore") or []
        if zs and hasattr(self, "_zs_buf"):
            by_min = {int(x.get("t", 0) // 60): x for x in self._zs_buf}   # local prioritaire
            for x in zs:
                b = int(x.get("t", 0) // 60)
                by_min.setdefault(b, x)
            ordered = sorted(by_min.values(), key=lambda x: x.get("t", 0))
            self._zs_buf.clear()
            for x in ordered[-self._zs_buf.maxlen:]:
                self._zs_buf.append(x)
        # 3) liquidations : fusionne (dé-dupliqué, 48 h)
        liqs = data.get("liquidations") or []
        if liqs:
            cut = _t.time() - 48 * 3600
            with self.engine.agg._lock:
                have = {(round(t, 0), s, round(p, 1))
                        for x in self.engine.agg.liqs if len(x) == 4
                        for t, s, p, q in [x]}
                for item in liqs:
                    if not (isinstance(item, (list, tuple)) and len(item) == 4):
                        continue
                    t, s, p, q = item
                    if t < cut:
                        continue
                    k = (round(t, 0), s, round(p, 1))
                    if k not in have:
                        self.engine.agg.liqs.append(tuple(item)); have.add(k)
        # rafraîchit la vue z-scores si visible
        try:
            if self.tabs.currentWidget() is getattr(self, "_zs_page", None):
                self._refresh_zscores()
        except Exception:
            pass

    def _update_live_pnl(self, mid):
        """P&L FLOTTANT en direct sur les positions ouvertes : pastille dans le coin
        (toutes les pages) + rafraîchit le tableau du Journal (throttle 1.5s)."""
        import time as _t
        j = getattr(self, "_journal", None)
        tot = 0.0; n = 0
        if mid and j:
            for r in j:
                if r.get("exit") is None and r.get("entry") and r.get("size"):
                    tot += (mid - r["entry"]) * r["size"] * (1 if r.get("side") == "Long" else -1)
                    n += 1
        if hasattr(self, "corner_pnl"):
            if n:
                col = GREEN if tot >= 0 else RED
                self.corner_pnl.setText(f"{'▲' if tot >= 0 else '▼'} {tot:+,.0f} $")
                self.corner_pnl.setStyleSheet(
                    f"color:{col};font-size:15px;font-weight:800;background:{PANEL};"
                    f"border:1px solid {col};border-radius:8px;padding:4px 12px;")
                self.corner_pnl.setToolTip(f"P&L flottant sur {n} position(s) ouverte(s)")
                self.corner_pnl.setVisible(True)
            else:
                self.corner_pnl.setVisible(False)
        # rafraîchit le tableau Journal en direct (max 1x / 1.5s)
        if n and hasattr(self, "j_table") and _t.time() - getattr(self, "_jpnl_ts", 0) > 1.5:
            self._jpnl_ts = _t.time()
            self._journal_refresh()

    def on_state(self,s):
        self._last_state = s
        self.analyzer.feed(s)
        # prix BTC live dans le coin (visible sur toutes les pages)
        _m = s.get("mid")
        if _m and hasattr(self, "corner_price"):
            self.corner_price.setText(f"₿ {_m:,.1f} $")
        # clôture automatique des trades ouverts qui touchent leur TP / SL
        if _m:
            self._journal_autoclose(_m)
            self._update_live_pnl(_m)
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
        import time as _t
        # throttle : la bookmap ne se redessine que ~3x/s (les colonnes ne changent
        # que toutes les 0.4s de toute façon) -> gros gain de fluidité, zéro latence
        if _t.time() - getattr(self, "_heat_last", 0) < 0.33:
            return
        self._heat_last = _t.time()
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

        # --- BULLES DE TRADES (bookmap) : x=colonne temps, y=prix, taille∝volume ---
        ts = s.get("hm_ts") or []; trades = s.get("hm_trades") or []
        if ts and trades:
            tsa = np.asarray(ts); n = len(ts)
            spots = []
            for tt, pp, qq, ss in trades:
                xi = min(max(int(np.searchsorted(tsa, tt)), 0), n - 1)
                size = 3.0 + min(20.0, (qq ** 0.5) * 4.5)
                brush = (255, 80, 80, 210) if ss else (60, 220, 130, 210)
                spots.append({"pos": (xi + 0.5, pp), "size": size, "brush": brush, "pen": None})
            self.hm_trades.setData(spots)
        else:
            self.hm_trades.setData([])

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
                f"🕒 Fenêtre {self.WALL_WIN_LABELS.get(m, str(m)+chr(32)+'min')}   ·   dernière mise à jour : {last}   ·   "
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
            # fenêtres longues (jours/semaines) : on regroupe par ZONE de 25 $ pour
            # qu'un niveau défendu plusieurs fois ressorte comme UN seul niveau fort
            cluster = 25.0 if m >= 1440 else 0.1
            rep = self.engine.wall_history.report(m, mid=mid, top_n=pool,
                                                  max_dist=max_dist, cluster=cluster)
            if rep.get("ready") and sort_key == "dist" and mid:
                # proximité : le plus proche du prix actuel en premier (croissant)
                rep["top"] = sorted(rep["top"],
                                    key=lambda w: abs(w["price"] - mid))[:18]
            elif rep.get("ready") and sort_key:
                rep["top"] = sorted(rep["top"], key=lambda w: (w.get(sort_key) or 0),
                                    reverse=True)[:18]
            if not rep.get("ready"):
                dtxt = self.wall_dist_combo.currentText()
                lbl = self.WALL_WIN_LABELS.get(m, f"{m} min")
                msg = (f"🧱 {lbl} — aucun mur à {dtxt} du prix. "
                       f"Élargis la distance en haut." if max_dist else
                       f"🧱 {lbl} — accumulation des murs…")
                w["header"].setText(f"{msg}  ({_t.strftime('%H:%M:%S')})")
                # vide les tableaux
                for key in ("table", "solid", "valid", "broken", "flux"):
                    w[key].setRowCount(0)
                w["_top_walls"] = []
                continue
            w["header"].setText(
                f"🧱 {self.WALL_WIN_LABELS.get(m, str(m)+chr(32)+'min')}   ·   {rep['n_total']} niveaux "
                f"({rep['n_buy']} support / {rep['n_sell']} résistance)   ·   "
                f"{rep['n_spoof']} spoof   ·    🧊 {rep.get('n_iceberg', 0)} iceberg   ·   "
                f"maj {_t.strftime('%H:%M:%S')}")

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
            # ICEBERGS : étiquette à part (absorption réelle, opposé du spoof)
            parts.append(f"<span style='color:{ACCENT};font-weight:800;'>🧊 {rep.get('n_iceberg', 0)}</span>"
                         f"<span style='color:{DIM};font-size:10px;'> ICEBERGS (absorbent+se rechargent)</span>")
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
                if wl.get("iceberg"):
                    # a absorbé bien plus que sa taille sans céder = ordre caché
                    lect = f"🧊 ICEBERG — a absorbé {wl.get('absorbed', 0):.0f} BTC sans céder"
                    lcol = ACCENT
                elif lb + ls <= 0:
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
            "Profil footprint : pour chaque niveau de prix, le volume ACHAT (bleu, à droite) "
            "vs VENTE (rouge, à gauche). Le POC est l'aimant, la Value Area la zone d'équilibre, "
            "les murs actuels sont marqués 🧱. Survole un niveau pour les chiffres exacts.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # fenêtres COURTES (live + seed) en secondes / LONGUES (jours) via klines
        self.PROF_WINDOWS = {"15 min": 900, "30 min": 1800, "1 heure": 3600,
                             "2 heures": 7200, "4 heures": 14400}
        self.PROF_WINDOWS_LONG = {"1 jour": ("5m", 288), "3 jours": ("15m", 288),
                                  "1 semaine": ("30m", 336), "2 semaines": ("1h", 336),
                                  "1 mois": ("2h", 360)}
        # résolution -> (target_levels pour klines, bucket $ pour fenêtres courtes)
        self.PROF_RES = {"Auto": (110, 10.0), "Fine": (190, 5.0), "Large": (65, 25.0)}

        # ---- barre d'options ----
        wrow = QtWidgets.QHBoxLayout(); wrow.setSpacing(8)

        def _olbl(t):
            l = QtWidgets.QLabel(t)
            l.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:0.5px;")
            return l

        def _ocombo(items, cur):
            c = QtWidgets.QComboBox(); c.addItems(items); c.setCurrentText(cur)
            c.setStyleSheet(f"QComboBox{{background:{PANEL};border:1px solid {BORDER};"
                            f"border-radius:8px;color:{TXT};padding:6px 12px;font-weight:700;}}")
            return c

        self.prof_window_combo = _ocombo(list(self.PROF_WINDOWS.keys())
                                         + list(self.PROF_WINDOWS_LONG.keys()), "1 jour")
        self.prof_mode_combo = _ocombo(["Achat × Vente", "Delta net"], "Achat × Vente")
        self.prof_res_combo = _ocombo(list(self.PROF_RES.keys()), "Auto")
        self.prof_values_chk = QtWidgets.QCheckBox("Valeurs BTC")
        self.prof_values_chk.setChecked(True)
        self.prof_values_chk.setStyleSheet(f"color:{TXT};font-size:12px;font-weight:700;")
        for w in (_olbl("FENÊTRE :"), self.prof_window_combo, _olbl("  MODE :"),
                  self.prof_mode_combo, _olbl("  RÉSOLUTION :"), self.prof_res_combo,
                  self.prof_values_chk):
            wrow.addWidget(w)
        wrow.addStretch()
        for c in (self.prof_window_combo, self.prof_mode_combo, self.prof_res_combo):
            c.currentIndexChanged.connect(self._refresh_profil)
        self.prof_values_chk.stateChanged.connect(self._refresh_profil)
        outer.addLayout(wrow)

        # ---- bandeau stats POC / VAH / VAL / VA% / VOLUME (pleine largeur) ----
        self.poc_box = QtWidgets.QFrame()
        self.poc_box.setStyleSheet(
            f"QFrame{{background:{PANEL};border:2px solid {VIOLET};border-radius:14px;}}")
        pb = QtWidgets.QHBoxLayout(self.poc_box)
        pb.setContentsMargins(22, 12, 22, 12); pb.setSpacing(28)

        def prof_stat(label):
            f = QtWidgets.QFrame(); f.setStyleSheet("QFrame{border:none;}")
            lay = QtWidgets.QVBoxLayout(f); lay.setSpacing(2); lay.setContentsMargins(0, 0, 0, 0)
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
        self.pf_vol  = prof_stat("VOLUME TOTAL (fenêtre)")
        for w in (self.pf_poc, self.pf_vah, self.pf_val, self.pf_va, self.pf_vol):
            pb.addWidget(w)
        pb.addStretch()
        outer.addWidget(self.poc_box)

        # ---- PROFIL FOOTPRINT plein écran ----
        self.prof_fp_head = self._h("PROFIL FOOTPRINT  ·  vente ◄ | ► achat par niveau")
        outer.addWidget(self.prof_fp_head)
        self.prof_fp = pg.PlotWidget()
        self.prof_fp.setBackground(BG)
        self.prof_fp.showGrid(x=False, y=True, alpha=0.12)
        self.prof_fp.setMenuEnabled(False)
        self.prof_fp.getAxis("left").setTextPen(pg.mkColor(TXT))
        self.prof_fp.getAxis("bottom").setTextPen(pg.mkColor(DIM))
        self.prof_fp.getAxis("left").setWidth(74)
        self.prof_fp.setLabel("bottom", "volume BTC  (vente ◄ 0 ► achat)")
        self._prof_fp_items = []
        self._prof_cells = {}
        self._prof_bucket = 10.0
        # croix + survol pour lire les chiffres exacts d'un niveau
        self.prof_cross = pg.InfiniteLine(angle=0, movable=False,
                                          pen=pg.mkPen((150, 160, 180, 120), width=1,
                                                       style=QtCore.Qt.PenStyle.DashLine))
        self.prof_cross.setZValue(40); self.prof_fp.addItem(self.prof_cross)
        self.prof_fp.scene().sigMouseMoved.connect(self._prof_mouse)
        outer.addWidget(self.prof_fp, 1)

        # bandeau de lecture du niveau survolé
        self.prof_hover = QtWidgets.QLabel("Survole un niveau du profil pour voir "
                                           "prix · achat · vente · delta · total.")
        self.prof_hover.setStyleSheet(
            f"color:{TXT};font-size:13px;font-weight:700;background:{PANEL2};"
            f"border:1px solid {BORDER};border-radius:8px;padding:8px 12px;")
        outer.addWidget(self.prof_hover)
        return page

    def _prof_mouse(self, pos):
        """Survol du profil : affiche prix / achat / vente / delta / total du niveau."""
        if not self._prof_cells:
            return
        vb = self.prof_fp.getViewBox()
        if not self.prof_fp.sceneBoundingRect().contains(pos):
            return
        price = vb.mapSceneToView(pos).y()
        bucket = self._prof_bucket or 10.0
        lvl = round(price / bucket) * bucket
        d = self._prof_cells.get(lvl)
        if d is None:                        # cherche le niveau le plus proche
            keys = list(self._prof_cells)
            if not keys:
                return
            lvl = min(keys, key=lambda k: abs(k - price)); d = self._prof_cells[lvl]
        self.prof_cross.setValue(lvl)
        buy = d.get("buy", 0.0); sell = d.get("sell", 0.0); tot = buy + sell
        delta = buy - sell
        dcol = GREEN if delta >= 0 else RED
        self.prof_hover.setText(
            f"<span style='color:{VIOLET};font-weight:800;'>{lvl:,.0f} $</span>   "
            f"<span style='color:{ACCENT};'>achat {buy:,.0f}</span> · "
            f"<span style='color:{RED};'>vente {sell:,.0f}</span> · "
            f"<span style='color:{dcol};font-weight:800;'>delta {delta:+,.0f}</span> · "
            f"<span style='color:{TXT};'>total {tot:,.0f} BTC</span>")

    def _render_profile_fp(self, cells, poc, vah, val, lo, hi, bucket, mid, walls, title):
        """Dessine le PROFIL FOOTPRINT : barres divergentes vente(gauche)/achat(droite)
        par niveau, POC, value area, prix actuel et murs marqués."""
        plt = self.prof_fp
        for it in self._prof_fp_items:
            plt.removeItem(it)
        self._prof_fp_items = []
        self.prof_fp_head.setText(title)
        self._prof_cells = cells or {}
        self._prof_bucket = bucket or 10.0
        if not cells:
            return
        mode_delta = "Delta" in self.prof_mode_combo.currentText()
        show_vals = self.prof_values_chk.isChecked()
        ys = sorted(cells.keys())
        buys = [cells[p].get("buy", 0.0) for p in ys]
        sells = [cells[p].get("sell", 0.0) for p in ys]
        totals = [b + s for b, s in zip(buys, sells)]
        h = bucket * 0.86

        # value area (bande) en fond
        va = pg.LinearRegionItem(values=(val, vah), orientation="horizontal",
                                 movable=False, brush=(120, 120, 170, 26),
                                 pen=pg.mkPen((120, 120, 170, 60)))
        va.setZValue(-20); plt.addItem(va); self._prof_fp_items.append(va)

        if mode_delta:
            # DELTA NET par niveau : bleu si achat domine, rouge si vente domine
            deltas = [b - s for b, s in zip(buys, sells)]
            pos = [d if d > 0 else 0 for d in deltas]
            neg = [d if d < 0 else 0 for d in deltas]
            b_pos = pg.BarGraphItem(x0=[0] * len(ys), width=pos, y=ys, height=h,
                                    brush=(58, 120, 195), pen=None)
            b_neg = pg.BarGraphItem(x0=neg, width=[-n for n in neg], y=ys, height=h,
                                    brush=(205, 76, 88), pen=None)
            plt.addItem(b_neg); plt.addItem(b_pos)
            self._prof_fp_items += [b_pos, b_neg]
            span = max([abs(d) for d in deltas] + [1.0])
        else:
            # ACHAT × VENTE : vente à gauche (rouge), achat à droite (bleu)
            b_sell = pg.BarGraphItem(x0=[-s for s in sells], width=sells, y=ys, height=h,
                                     brush=(205, 76, 88), pen=None)
            b_buy = pg.BarGraphItem(x0=[0] * len(ys), width=buys, y=ys, height=h,
                                    brush=(58, 120, 195), pen=None)
            plt.addItem(b_sell); plt.addItem(b_buy)
            self._prof_fp_items += [b_sell, b_buy]
            span = max(buys + [1.0])

        # valeurs BTC sur les niveaux les plus volumineux
        if show_vals and totals:
            ranked = sorted(totals, reverse=True)[:14]
            thr = ranked[-1] if ranked else 0
            for p, b, s, t in zip(ys, buys, sells, totals):
                if t < thr or t <= 0:
                    continue
                if mode_delta:
                    d = b - s
                    col = GREEN if d >= 0 else RED
                    anchor = (0, 0.5) if d >= 0 else (1, 0.5)
                    xx = d + (span * 0.012 if d >= 0 else -span * 0.012)
                    lab = f"{d:+,.0f}"
                else:
                    col, anchor, xx, lab = TXT, (0, 0.5), b + span * 0.012, f"{t:,.0f}"
                txt = pg.TextItem(html=f"<span style='font-size:7pt;color:{col};"
                                       f"font-weight:700;'>{lab}</span>", anchor=anchor)
                txt.setPos(xx, p)
                plt.addItem(txt); self._prof_fp_items.append(txt)

        # POC (niveau le plus tradé) — aimant
        pl = pg.InfiniteLine(pos=poc, angle=0, pen=pg.mkPen(VIOLET, width=2),
                             label=f"POC {poc:,.0f}",
                             labelOpts={"color": VIOLET, "position": 0.02})
        plt.addItem(pl); self._prof_fp_items.append(pl)
        # prix actuel
        if mid:
            ml = pg.InfiniteLine(pos=mid, angle=0,
                                 pen=pg.mkPen("#00e5ff", width=1.4,
                                              style=QtCore.Qt.PenStyle.DashLine),
                                 label=f"{mid:,.0f}",
                                 labelOpts={"color": "#0d1117", "fill": (0, 229, 255, 230),
                                            "position": 0.98})
            plt.addItem(ml); self._prof_fp_items.append(ml)
        # murs (gros ordres actuels) marqués à leur niveau de prix
        wall_x = span * 1.04
        for w in walls:
            wp = w.get("price")
            if wp is None or not (lo <= wp <= hi):
                continue
            col = GREEN if w.get("side") == "bid" else RED
            wl = pg.InfiniteLine(pos=wp, angle=0,
                                 pen=pg.mkPen(col, width=1, style=QtCore.Qt.PenStyle.DotLine))
            plt.addItem(wl); self._prof_fp_items.append(wl)
            txt = pg.TextItem(html=f"<span style='font-size:8pt;color:{col};"
                                   f"font-weight:700;'>🧱 {w.get('qty', 0):.0f}</span>",
                              anchor=(0, 0.5))
            txt.setPos(wall_x, wp)
            plt.addItem(txt); self._prof_fp_items.append(txt)
        pad = (hi - lo) * 0.04 + bucket
        plt.setYRange(lo - pad, hi + pad, padding=0)

    def _refresh_profil(self, *args):
        import time as _t, threading
        mid = self._last_state.get("mid") if self._last_state else None
        if not hasattr(self, "_profL"):
            self._profL = {}; self._profL_loading = set()

        label = self.prof_window_combo.currentText()
        res = self.prof_res_combo.currentText()
        tgt_levels, bucket_short = self.PROF_RES.get(res, (110, 10.0))
        long_mode = label in self.PROF_WINDOWS_LONG
        vp = None
        if long_mode:
            interval, limit = self.PROF_WINDOWS_LONG[label]
            ckey = (label, res)
            entry = self._profL.get(ckey)
            fresh = entry and (_t.time() - entry[0] < 180)
            if not fresh and ckey not in self._profL_loading:
                self._profL_loading.add(ckey)

                def _load(k=ckey, iv=interval, lm=limit, tl=tgt_levels):
                    try:
                        data = self.engine.get_profile_klines(interval=iv, limit=lm, target_levels=tl)
                    except Exception:
                        data = None
                    self._profL[k] = (_t.time(), data)
                    self._profL_loading.discard(k)
                threading.Thread(target=_load, daemon=True).start()
            vp = entry[1] if entry else None
            if vp is None:
                self.prof_fp_head.setText(f"PROFIL FOOTPRINT · {label} · chargement des klines Binance…")
        else:
            window_s = self.PROF_WINDOWS.get(label, 3600)
            vp = self.engine.get_volume_profile(window_s=window_s, bucket=bucket_short)

        if vp:
            poc = vp["poc"]
            if not long_mode and mid:
                self.poc_line.setValue(poc); self.poc_line.setVisible(True)
            col_poc = GREEN if (mid and poc < mid) else (RED if mid else TXT)
            self.pf_poc._val.setText(f"{poc:,.0f}")
            self.pf_poc._val.setStyleSheet(f"color:{col_poc};font-size:20px;font-weight:800;")
            self.pf_vah._val.setText(f"{vp['vah']:,.0f}")
            self.pf_vah._val.setStyleSheet(f"color:{RED};font-size:20px;font-weight:800;")
            self.pf_val._val.setText(f"{vp['val']:,.0f}")
            self.pf_val._val.setStyleSheet(f"color:{GREEN};font-size:20px;font-weight:800;")
            self.pf_va._val.setText(f"{vp['va_pct']*100:.0f}%")
            self.pf_vol._val.setText(f"{vp['total_vol']:,.0f} BTC")

            # PROFIL FOOTPRINT : achat×vente par niveau + murs actuels marqués
            cells = vp.get("cells", {})
            walls = sorted((self._last_state or {}).get("walls", []),
                           key=lambda w: w.get("qty", 0), reverse=True)[:10]
            self._render_profile_fp(
                cells, poc, vp["vah"], vp["val"],
                vp.get("lo", min(cells) if cells else 0),
                vp.get("hi", max(cells) if cells else 0),
                vp.get("bucket", 10.0), mid, walls,
                f"PROFIL FOOTPRINT · {label} · vente ◄ | ► achat par niveau")
        else:
            for w in (self.pf_poc, self.pf_vah, self.pf_val, self.pf_va, self.pf_vol):
                w._val.setText("—")

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

        # TOUTES les entrées sont persistées : elles restent telles quelles tant que
        # tu ne les changes pas, même en fermant l'appli.
        st = self._calc_load_settings()
        self.calc_capital = self._calc_input(st.get("capital", "10000"))
        self.calc_risk    = self._calc_input(st.get("risk", "1.25"))    # en %
        self.calc_entry   = self._calc_input(st.get("entry", ""))       # prix d'entrée
        self.calc_sl      = self._calc_input(st.get("sl", ""))          # prix du stop
        self.calc_tp      = self._calc_input(st.get("tp", ""))          # prix take profit
        self.calc_side    = QtWidgets.QComboBox()
        self.calc_side.addItems(["Long", "Short"])
        self.calc_side.setCurrentText(st.get("side", "Long"))
        self.calc_side.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:8px 12px;font-weight:700;}}")
        self.calc_side.currentIndexChanged.connect(self._calc_compute)

        def flbl(t):
            l = QtWidgets.QLabel(t); l.setStyleSheet(f"color:{TXT};font-size:13px;font-weight:600;")
            return l
        form.addRow(flbl("Capital total ($)"), self.calc_capital)
        form.addRow(flbl("Risque par trade (%)"), self.calc_risk)
        form.addRow(flbl("Prix d'entrée BTC ($)"), self.calc_entry)
        form.addRow(flbl("Prix du Stop Loss ($)"), self.calc_sl)
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

        # ----- boutons de clôture -> Journal -----
        btnrow = QtWidgets.QHBoxLayout(); btnrow.setSpacing(8)

        def mkbtn(txt, bg, fg, cb):
            b = QtWidgets.QPushButton(txt)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton{{background:{bg};color:{fg};border:none;border-radius:9px;"
                f"padding:11px 14px;font-weight:800;font-size:13px;}}"
                f"QPushButton:hover{{opacity:0.9;}}")
            b.clicked.connect(cb)
            return b

        btnrow.addWidget(mkbtn("✅ Trade pris  →  Journal", GREEN, "#06210f",
                               self._calc_take_trade))
        btnrow.addWidget(mkbtn("⚪ Trade pas pris", PANEL2, TXT, self._calc_skip_trade))
        outbox.addLayout(btnrow)
        body.addLayout(outbox, 1)

        # rangées fixes du tableau résultats
        self._calc_rows = [
            "Montant risqué ($)", "Taille de position (BTC)", "Valeur de la position ($)",
            "Levier utilisé (x)", "Distance du Stop Loss ($)", "Distance du Take Profit ($/BTC)",
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
        sl_price = num(self.calc_sl)          # PRIX du stop (plus la distance)
        entry = num(self.calc_entry)
        tp    = num(self.calc_tp)
        is_long = self.calc_side.currentText() == "Long"
        # distance du stop = écart entre l'entrée et le prix du stop
        sld = abs(entry - sl_price) if (entry is not None and sl_price is not None) else None

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
        if sld is not None:
            vals[4] = f"{sld:,.0f} $"; cols[4] = RED
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
            self.calc_alert.setText("Entre Capital, Risque %, Prix d'entrée et Prix du Stop "
                                    "pour la taille de position, le levier et le R:R. "
                                    "Le Take Profit est optionnel.")
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

        # mémorise capital / risque / stop / sens pour la prochaine ouverture
        self._calc_save_settings()

    # ---- persistance des réglages du calculateur (capital, risque, stop, sens) ----
    def _calc_settings_path(self):
        import os
        return data_file("calc_settings.json")

    def _calc_load_settings(self):
        import json, os
        p = self._calc_settings_path()
        if not os.path.exists(p):
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _calc_save_settings(self):
        import json
        if not hasattr(self, "calc_capital"):
            return
        data = {"capital": self.calc_capital.text().strip(),
                "risk": self.calc_risk.text().strip(),
                "side": self.calc_side.currentText(),
                "entry": self.calc_entry.text().strip(),
                "sl": self.calc_sl.text().strip(),
                "tp": self.calc_tp.text().strip()}
        try:
            with open(self._calc_settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _calc_nums(self):
        """Lit les entrées du calculateur -> (cap, risk, sl_price, entry, tp, is_long)."""
        def num(w):
            try:
                return float(str(w.text()).replace(",", ".").replace(" ", "").replace("$", ""))
            except (ValueError, AttributeError):
                return None
        return (num(self.calc_capital), num(self.calc_risk), num(self.calc_sl),
                num(self.calc_entry), num(self.calc_tp),
                self.calc_side.currentText() == "Long")

    def _calc_take_trade(self):
        """TRADE PRIS : envoie la position au JOURNAL comme trade OUVERT (entrée, stop,
        TP, taille). Tu le clôtureras ensuite dans le Journal au TP (WIN) ou au SL
        (LOSS). Capital et risque conservés ; entrée/stop/TP vidés pour le suivant."""
        import time as _t
        cap, risk, sl_price, entry, tp, is_long = self._calc_nums()
        if entry is None or sl_price is None:
            self._calc_flash("⚠️ Entre au moins le prix d'entrée et le prix du stop "
                             "pour envoyer le trade au Journal.", RED)
            return
        sld = abs(entry - sl_price)
        size = (cap * (risk / 100.0) / sld) if (cap is not None and risk is not None and sld) else None
        rec = {
            "date": _t.strftime("%Y-%m-%d %H:%M:%S"),
            "side": "Long" if is_long else "Short",
            "entry": entry, "sl": sl_price, "tp": tp, "size": size,
            "exit": None,                     # position OUVERTE (à clôturer au Journal)
            "note": "Trade pris",
        }
        if not hasattr(self, "_journal"):
            self._journal = self._journal_load()
        self._journal.append(rec)
        self._journal_save()
        if hasattr(self, "_journal_refresh"):
            self._journal_refresh()
        # garde capital/risque, vide entrée/stop/TP pour le prochain trade
        self.calc_entry.clear(); self.calc_sl.clear(); self.calc_tp.clear()
        self._calc_flash("✅ Trade envoyé au Journal (position ouverte). Clôture-le au "
                         "TP (WIN) ou au SL (LOSS) depuis le Journal.", GREEN)

    def _calc_skip_trade(self):
        """TRADE PAS PRIS : ne journalise rien, remet à zéro l'entrée/stop/TP, garde
        le capital et le risque par trade d'avant."""
        self.calc_entry.clear(); self.calc_sl.clear(); self.calc_tp.clear()
        self._calc_flash("⚪ Trade non pris. Entrée/stop/TP remis à zéro, "
                         "capital et risque conservés.", DIM)

    def _calc_flash(self, msg, color):
        self.calc_alert.setText(msg)
        self.calc_alert.setStyleSheet(
            f"color:{color};font-size:14px;font-weight:800;background:{PANEL2};"
            f"border:2px solid {color};border-radius:10px;padding:12px;")

    # ===========================================================
    # JOURNAL DE TRADES (saisie manuelle + persistance)
    # ===========================================================

    def _journal_path(self):
        import os
        return data_file("trade_journal.json")

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
            "Journal de trades. Les positions envoyées depuis le calculateur se clôturent "
            "TOUTES SEULES dès que le prix live touche leur TP (WIN) ou leur SL (LOSS) — "
            "tant que l'appli est ouverte. Tu peux aussi clôturer à la main un trade "
            "sélectionné. L'app calcule le R:R et le P&L, garde tout sur disque, et résume "
            "ton win-rate. (Prix des bourses crypto — proche mais pas identique à ton broker.)")
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

        # clôture d'un trade ouvert sélectionné : au TP (WIN) ou au SL (LOSS)
        tpbtn = QtWidgets.QPushButton("🎯 Clôturer au TP (WIN)")
        tpbtn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        tpbtn.setStyleSheet(
            f"QPushButton{{background:{PANEL2};color:{GREEN};border:1px solid {GREEN};"
            f"border-radius:8px;padding:8px 12px;font-weight:700;}}"
            f"QPushButton:hover{{background:{GREEN};color:#06210f;}}")
        tpbtn.clicked.connect(lambda: self._journal_close("TP"))
        slbtn = QtWidgets.QPushButton("🛑 Clôturer au SL (LOSS)")
        slbtn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        slbtn.setStyleSheet(
            f"QPushButton{{background:{PANEL2};color:{RED};border:1px solid {RED};"
            f"border-radius:8px;padding:8px 12px;font-weight:700;}}"
            f"QPushButton:hover{{background:{RED};color:#fff;}}")
        slbtn.clicked.connect(lambda: self._journal_close("SL"))
        formbox.addWidget(tpbtn); formbox.addWidget(slbtn)
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
        # rattrape les clôtures TP/SL survenues pendant que le PC était éteint
        self._journal_backfill_closures()
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

    def _journal_autoclose(self, mid):
        """Surveille le prix live : clôture TOUTE SEULE chaque trade ouvert dès que le
        prix touche son TP (WIN) ou son SL (LOSS). Basé sur le prix des bourses
        (Binance/OKX/Bybit) — proche mais pas identique au prix exact de ton broker."""
        if not mid or not getattr(self, "_journal", None):
            return
        import time as _t
        changed = False
        for rec in self._journal:
            if rec.get("exit") is not None:
                continue
            tp = rec.get("tp"); sl = rec.get("sl")
            is_long = rec.get("side") == "Long"
            hit = None
            if is_long:
                if tp is not None and mid >= tp:
                    hit = ("TP", tp)
                elif sl is not None and mid <= sl:
                    hit = ("SL", sl)
            else:
                if tp is not None and mid <= tp:
                    hit = ("TP", tp)
                elif sl is not None and mid >= sl:
                    hit = ("SL", sl)
            if hit:
                kind, price = hit
                rec["exit"] = price
                rec["note"] = (f"TP atteint auto · {_t.strftime('%H:%M:%S')}" if kind == "TP"
                               else f"SL atteint auto · {_t.strftime('%H:%M:%S')}")
                changed = True
        if changed:
            self._journal_save()
            if hasattr(self, "j_table"):
                self._journal_refresh()

    def _journal_backfill_closures(self):
        """Au lancement : rattrape les trades ouverts qui ont touché leur TP/SL PENDANT
        que le PC était éteint, en relisant le vrai historique de prix (klines Binance).
        -> les trades se clôturent correctement même si le PC était éteint."""
        j = getattr(self, "_journal", None)
        if not j:
            return
        if not any(t.get("exit") is None and t.get("date") and
                   (t.get("tp") is not None or t.get("sl") is not None) for t in j):
            return
        import threading
        threading.Thread(target=self._journal_backfill_run, daemon=True).start()

    def _journal_backfill_run(self):
        import datetime
        changed = False
        for rec in list(self._journal):
            if rec.get("exit") is not None:
                continue
            tp = rec.get("tp"); sl = rec.get("sl")
            if tp is None and sl is None:
                continue
            try:
                open_dt = datetime.datetime.strptime(rec["date"][:19], "%Y-%m-%d %H:%M:%S")
                open_ts = open_dt.timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            hit = self._scan_klines_for_hit(open_ts, rec.get("side"), tp, sl)
            if hit:
                kind, price, when = hit
                rec["exit"] = price
                rec["note"] = f"{kind} atteint hors-ligne · {when}"
                changed = True
        if changed:
            self._journal_save()
            self.bridge.journal_closed.emit()   # refresh sur le thread UI

    def _scan_klines_for_hit(self, open_ts, side, tp, sl):
        """Parcourt les klines Binance depuis l'ouverture : renvoie (kind, prix, heure)
        du PREMIER niveau touché (TP ou SL), sinon None. En cas d'ambiguïté dans une
        même bougie (TP et SL touchés), on suppose le SL d'abord (prudent)."""
        import time as _t, datetime, requests
        now = _t.time()
        dur_min = max(1.0, (now - open_ts) / 60.0)
        for interval, mins in (("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240)):
            if dur_min / mins <= 1400:
                break
        url = "https://fapi.binance.com/fapi/v1/klines"
        out = []
        start_ms = int(open_ts * 1000)
        try:
            for _ in range(6):
                r = requests.get(url, params={"symbol": self.engine.symbol.upper(),
                                              "interval": interval, "startTime": start_ms,
                                              "limit": 1500}, timeout=10).json()
                if not isinstance(r, list) or not r:
                    break
                out.extend(r)
                if len(r) < 1500:
                    break
                start_ms = r[-1][0] + 1
        except Exception:
            return None
        is_long = side == "Long"
        for k in out:
            try:
                t0 = k[0] / 1000.0; high = float(k[2]); low = float(k[3])
            except (IndexError, ValueError, TypeError):
                continue
            if is_long:
                sl_hit = sl is not None and low <= sl
                tp_hit = tp is not None and high >= tp
            else:
                sl_hit = sl is not None and high >= sl
                tp_hit = tp is not None and low <= tp
            if sl_hit or tp_hit:
                when = datetime.datetime.fromtimestamp(t0).strftime("%d %b %H:%M")
                if sl_hit:                       # prudent : SL prioritaire si les deux
                    return ("SL", sl, when)
                return ("TP", tp, when)
        return None

    def _journal_close(self, kind):
        """Clôture le trade sélectionné : au TP -> sortie = TP (WIN), au SL -> sortie
        = SL (LOSS). Le P&L et le résultat WIN/LOSS sont recalculés automatiquement."""
        rows = sorted({i.row() for i in self.j_table.selectedItems()})
        if not rows:
            self.j_dash.setText("⚠️ Sélectionne d'abord un trade dans le tableau, "
                                "puis clique Clôturer au TP ou au SL.")
            return
        r = rows[0]
        if not (0 <= r < len(self._journal)):
            return
        rec = self._journal[r]
        price = rec.get("tp") if kind == "TP" else rec.get("sl")
        if price is None:
            self.j_dash.setText(f"⚠️ Ce trade n'a pas de {'Take Profit' if kind == 'TP' else 'Stop'} "
                                "défini — impossible de le clôturer ainsi.")
            return
        rec["exit"] = price
        rec["note"] = "TP atteint (WIN)" if kind == "TP" else "SL atteint (LOSS)"
        self._journal_save()
        self._journal_refresh()

    def _journal_refresh(self):
        j = getattr(self, "_journal", [])
        self.j_table.setRowCount(len(j))
        mid = (self._last_state or {}).get("mid")
        tot_pnl = 0.0; tot_float = 0.0; wins = 0; losses = 0; closed = 0
        for i, r in enumerate(j):
            entry = r.get("entry"); sl = r.get("sl"); tp = r.get("tp")
            size = r.get("size"); ex = r.get("exit")
            is_long = r.get("side") == "Long"
            # R:R prévu = distance TP / distance SL
            rr = None
            if entry and tp and sl and abs(entry - sl) > 0:
                rr = abs(tp - entry) / abs(entry - sl)
            pnl = None      # réalisé (trade clôturé)
            fpnl = None     # flottant (position ouverte, en direct)
            if entry and ex is not None and size:
                pnl = (ex - entry) * size * (1 if is_long else -1)
                tot_pnl += pnl; closed += 1
                if pnl >= 0:
                    wins += 1
                else:
                    losses += 1
            elif entry and size and mid:               # OUVERT -> P&L flottant live
                fpnl = (mid - entry) * size * (1 if is_long else -1)
                tot_float += fpnl
            # colonnes P&L + Résultat
            if pnl is not None:
                pnl_txt, pnl_col = f"{pnl:+,.0f} $", GREEN if pnl >= 0 else RED
                res_txt, res_col = ("WIN", GREEN) if pnl >= 0 else ("LOSS", RED)
            elif fpnl is not None:
                pnl_txt, pnl_col = f"~{fpnl:+,.0f} $", GREEN if fpnl >= 0 else RED
                res_txt = f"ouvert  {fpnl:+,.0f}$"; res_col = GREEN if fpnl >= 0 else RED
            else:
                pnl_txt, pnl_col = "—", DIM
                res_txt, res_col = "ouvert", AMBER
            cells = [
                (r.get("date", "—"), DIM),
                (r.get("side", "—"), GREEN if is_long else RED),
                (f"{entry:,.0f}" if entry else "—", TXT),
                (f"{sl:,.0f}" if sl else "—", RED),
                (f"{tp:,.0f}" if tp else "—", GREEN),
                (f"{ex:,.0f}" if ex is not None else "—", TXT),
                (f"{size:.3f}" if size else "—", TXT),
                (f"{rr:.2f}:1" if rr else "—", GREEN if (rr and rr >= 2) else TXT),
                (pnl_txt, pnl_col),
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
        open_n = len(j) - closed
        pnlcol = GREEN if tot_pnl >= 0 else RED
        float_txt = ""
        if open_n and mid:
            fcol = "▲" if tot_float >= 0 else "▼"
            float_txt = (f"  ·  <span style='color:{GREEN if tot_float>=0 else RED};'>"
                         f"P&L FLOTTANT (live) : {tot_float:+,.0f} $ {fcol}</span>")
        self.j_dash.setText(
            f"📊 {len(j)} trades  ·  {closed} clôturés  ·  "
            f"Win-rate : {wr:.0f}% ({wins}W / {losses}L)  ·  "
            f"<span style='color:{pnlcol};'>P&L réalisé : {tot_pnl:+,.0f} $</span>  ·  "
            f"{open_n} ouvert(s){float_txt}")
        self.j_dash.setStyleSheet(
            f"color:{TXT};font-size:14px;font-weight:800;background:{PANEL2};"
            f"border:1px solid {BORDER};border-radius:10px;padding:10px 14px;")

    # ===========================================================
    # ALERTES WHATSAPP (critères précis + fenêtre horaire)
    # ===========================================================

    def _alerts_cfg_path(self):
        import os
        return data_file("alerts_config.json")

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
    # PAGE QUANT — OPTIONS DERIBIT + CONTEXTE MACRO
    # ===========================================================

    def _quant_card(self, title, col):
        box = QtWidgets.QFrame()
        box.setStyleSheet(f"QFrame{{background:{PANEL};border:1px solid {BORDER};border-radius:12px;}}")
        bl = QtWidgets.QVBoxLayout(box); bl.setContentsMargins(14, 10, 14, 10); bl.setSpacing(2)
        cap = QtWidgets.QLabel(title)
        cap.setStyleSheet(f"color:{col};font-size:10px;font-weight:800;letter-spacing:1px;border:none;")
        val = QtWidgets.QLabel("—")
        val.setStyleSheet(f"color:{TXT};font-size:22px;font-weight:800;border:none;")
        sub = QtWidgets.QLabel(""); sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{DIM};font-size:11px;border:none;")
        bl.addWidget(cap); bl.addWidget(val); bl.addWidget(sub)
        box._val = val; box._sub = sub
        return box

    def _build_quant_page(self):
        from quant import MACRO_SYMBOLS
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(16, 16, 16, 16); outer.setSpacing(14)

        intro = QtWidgets.QLabel(
            "Quant : le contexte au-delà de l'order flow. OPTIONS (Deribit) = comment le marché "
            "price le risque et vers quel prix l'expiration attire. MACRO = le vent de fond "
            "(actions, dollar, peur) qui pousse ou freine BTC.")
        intro.setWordWrap(True); intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # ---- BANDEAU ANOMALIES (z-scores) ----
        self.q_anomaly = QtWidgets.QLabel("Extrêmes statistiques : accumulation des données…")
        self.q_anomaly.setWordWrap(True)
        self.q_anomaly.setStyleSheet(f"color:{DIM};font-size:14px;font-weight:800;"
                                     f"background:{PANEL2};border:2px solid {BORDER};border-radius:10px;padding:12px;")
        outer.addWidget(self.q_anomaly)

        # ---- OPTIONS ----
        outer.addWidget(self._h("OPTIONS BTC  ·  Deribit  (volatilité implicite · skew · max pain)"))
        orow = QtWidgets.QHBoxLayout(); orow.setSpacing(10)
        self.q_iv = self._quant_card("VOLATILITÉ IMPLICITE (ATM)", ACCENT)
        self.q_skew = self._quant_card("SKEW  PUT / CALL", AMBER)
        self.q_pain = self._quant_card("MAX PAIN  (aimant d'expi)", VIOLET)
        self.q_pcr = self._quant_card("PUT/CALL  ·  OPEN INTEREST", GREEN)
        for c in (self.q_iv, self.q_skew, self.q_pain, self.q_pcr):
            orow.addWidget(c, 1)
        outer.addLayout(orow)
        self.q_opt_read = QtWidgets.QLabel("Chargement des options Deribit…")
        self.q_opt_read.setWordWrap(True)
        self.q_opt_read.setStyleSheet(f"color:{TXT};font-size:13px;font-weight:600;"
                                      f"background:{PANEL2};border:1px solid {BORDER};border-radius:10px;padding:11px;")
        outer.addWidget(self.q_opt_read)

        # ---- MACRO ----
        outer.addWidget(self._h("CONTEXTE MACRO  ·  risk-on / risk-off  (ce qui pousse ou freine BTC)"))
        mgrid = QtWidgets.QGridLayout(); mgrid.setSpacing(10)
        self.q_macro_cards = {}
        for i, (name, _sym) in enumerate(MACRO_SYMBOLS):
            c = self._quant_card(name.upper(), ACCENT)
            self.q_macro_cards[name] = c
            mgrid.addWidget(c, i // 3, i % 3)
        outer.addLayout(mgrid)
        self.q_macro_read = QtWidgets.QLabel("Chargement macro…")
        self.q_macro_read.setWordWrap(True)
        self.q_macro_read.setStyleSheet(f"color:{TXT};font-size:14px;font-weight:700;"
                                        f"background:{PANEL2};border:1px solid {BORDER};border-radius:10px;padding:12px;")
        outer.addWidget(self.q_macro_read)

        # ---- AIMANTS DE LIQUIDATION ----
        outer.addWidget(self._h("AIMANTS DE LIQUIDATION  ·  où le prix est aspiré  (chasse aux liquidations)"))
        lrow = QtWidgets.QHBoxLayout(); lrow.setSpacing(10)
        self.q_lev = QtWidgets.QLabel("…")
        self.q_lev.setWordWrap(True)
        self.q_lev.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.q_lev.setStyleSheet(f"color:{TXT};font-size:13px;line-height:150%;background:{PANEL};"
                                 f"border:1px solid {BORDER};border-radius:10px;padding:12px;")
        lrow.addWidget(self.q_lev, 1)
        self.q_liq_table = QtWidgets.QTableWidget(0, 3)
        self.q_liq_table.setHorizontalHeaderLabels(["Prix", "Type liquidé", "Montant $"])
        self._prep(self.q_liq_table)
        self.q_liq_table.setMaximumHeight(230)
        lrow.addWidget(self.q_liq_table, 1)
        outer.addLayout(lrow)
        outer.addStretch(1)
        return page

    def _z_sample_and_score(self):
        """Échantillonne les métriques intraday (~25s) et renvoie leurs z-scores :
        z = (valeur actuelle − moyenne) / écart-type → |z|>2 = statistiquement anormal."""
        import time as _t
        import statistics
        from collections import deque
        if not hasattr(self, "_zbuf"):
            self._zbuf = deque(maxlen=160); self._z_last = 0.0
        now = _t.time()
        s = self._last_state or {}
        if now - self._z_last > 25 and s.get("mid") and not s.get("warming"):
            cvds = self.engine.get_cvd_windows(); c5 = cvds.get(5, {})
            pos = self.engine.get_positioning(); oi = pos.get("oi") or {}
            self._zbuf.append({
                "CVD 5min": c5.get("cvd", 0) if c5.get("ready") else 0,
                "Agresseurs": s.get("aggressor_ratio", 0.5),
                "Cadence (tape)": s.get("tape_speed", 0.0),
                "OI 5min %": oi.get("chg_5m_pct", 0.0),
            })
            self._z_last = now
        out = {}
        if len(self._zbuf) >= 12:
            for k in self._zbuf[-1]:
                vals = [x[k] for x in self._zbuf]
                m = statistics.mean(vals); sd = statistics.pstdev(vals)
                out[k] = (vals[-1] - m) / sd if sd > 1e-9 else 0.0
        return out

    def _refresh_quant(self):
        # --- anomalies statistiques (z-scores) ---
        z = self._z_sample_and_score()
        if not z:
            self.q_anomaly.setText("📊 Extrêmes statistiques : accumulation des données "
                                   "(prêt dans ~5 min de fonctionnement)…")
            self.q_anomaly.setStyleSheet(f"color:{DIM};font-size:13px;font-weight:700;"
                                         f"background:{PANEL2};border:2px solid {BORDER};border-radius:10px;padding:12px;")
        else:
            hot = [(k, v) for k, v in z.items() if abs(v) >= 2.0]
            if hot:
                hot.sort(key=lambda kv: abs(kv[1]), reverse=True)
                txt = " · ".join(f"{k} z={v:+.1f} ({'très fort' if abs(v)>=3 else 'anormal'})"
                                 for k, v in hot)
                self.q_anomaly.setText(f"⚠️ ANOMALIE : {txt} — mouvement inhabituel, "
                                       f"souvent un point de bascule intraday.")
                self.q_anomaly.setStyleSheet(f"color:{AMBER};font-size:14px;font-weight:800;"
                                             f"background:{PANEL2};border:2px solid {AMBER};border-radius:10px;padding:12px;")
            else:
                summary = " · ".join(f"{k} z={v:+.1f}" for k, v in z.items())
                self.q_anomaly.setText(f"🟢 Rien d'anormal statistiquement.  ({summary})")
                self.q_anomaly.setStyleSheet(f"color:{GREEN};font-size:13px;font-weight:700;"
                                             f"background:{PANEL2};border:2px solid {BORDER};border-radius:10px;padding:12px;")

        o = getattr(self, "quant", None) and self.quant.options
        if o:
            iv = o["atm_iv"]
            self.q_iv._val.setText(f"{iv:.1f}%")
            self.q_iv._sub.setText("calme" if iv < 35 else "normale" if iv < 55
                                   else "ÉLEVÉE — gros mouvements attendus")
            sk = o["skew"]
            if sk is not None:
                self.q_skew._val.setText(f"{sk:+.1f}")
                self.q_skew._val.setStyleSheet(
                    f"color:{RED if sk > 2 else GREEN if sk < -2 else TXT};"
                    f"font-size:22px;font-weight:800;border:none;")
                self.q_skew._sub.setText(
                    "puts plus chers = couverture baissière / peur" if sk > 2 else
                    "calls plus chers = appétit haussier" if sk < -2 else "équilibré")
            mp = o["max_pain"]; under = o["under"]
            self.q_pain._val.setText(f"{mp:,.0f}")
            d = mp - under
            self.q_pain._sub.setText(f"expi {o['front']} · {d:+,.0f}$ du prix "
                                     f"({'aimant au-dessus' if d > 0 else 'aimant en-dessous'})")
            pcr = o["pcr"]
            self.q_pcr._val.setText(f"{pcr:.2f}" if pcr else "—")
            self.q_pcr._sub.setText(f"OI total {o['total_oi']:,.0f} BTC "
                                    f"(~{o['total_oi']*under/1e9:.1f} Md$)")
            reads = [f"IV {iv:.0f}%"]
            if sk is not None:
                reads.append("skew côté PUTS (peur/couverture)" if sk > 2 else
                             "skew côté CALLS (appétit haussier)" if sk < -2 else "skew neutre")
            reads.append(f"le marché options « aime » {mp:,.0f} pour l'expi {o['front']} (aimant possible)")
            self.q_opt_read.setText("Lecture : " + "  ·  ".join(reads))

        m = getattr(self, "quant", None) and self.quant.macro
        if m:
            for name, c in self.q_macro_cards.items():
                v = m.get(name)
                if v and v.get("price") is not None:
                    c._val.setText(f"{v['price']:,.2f}")
                    chg = v.get("chg")
                    col = GREEN if (chg or 0) >= 0 else RED
                    c._sub.setText(f"{chg:+.2f}% aujourd'hui" if chg is not None else "—")
                    c._sub.setStyleSheet(f"color:{col};font-size:12px;font-weight:700;border:none;")

            def chg(n):
                v = m.get(n)
                return v["chg"] if (v and v.get("chg") is not None) else 0.0
            score = ((1 if chg("Nasdaq") > 0 else -1) + (1 if chg("S&P 500") > 0 else -1)
                     + (1 if chg("Dollar (DXY)") < 0 else -1) + (1 if chg("VIX (peur)") < 0 else -1))
            if score >= 2:
                self.q_macro_read.setText("🟢 RISK-ON : actions en hausse, dollar/peur en baisse "
                                          "→ vent porteur pour BTC.")
            elif score <= -2:
                self.q_macro_read.setText("🔴 RISK-OFF : actions en baisse, dollar/peur en hausse "
                                          "→ vent contraire pour BTC.")
            else:
                self.q_macro_read.setText("🟡 Macro mitigée : pas de vent de fond marqué pour BTC "
                                          "(BTC trade surtout sur son propre flux).")

        # ---- aimants de liquidation ----
        mid = (self._last_state or {}).get("mid")
        if mid:
            levs = self.engine.leverage_liq_levels(mid)
            html = [f"Prix actuel : <b style='color:{ACCENT};'>{mid:,.0f}</b><br>"
                    f"<span style='color:{DIM};'>Niveaux où les positions à levier sautent "
                    f"(aimants classiques) :</span>"]
            for lv in levs[:3]:
                html.append(
                    f"<b>{lv['lev']}x</b> · longs liquidés "
                    f"<span style='color:{RED};'>{lv['long_liq']:,.0f}</span> "
                    f"(-{lv['pct']:.0f}%) · shorts liquidés "
                    f"<span style='color:{GREEN};'>{lv['short_liq']:,.0f}</span> (+{lv['pct']:.0f}%)")
            self.q_lev.setText("<br>".join(html))
        clusters = self.engine.get_liq_clusters(3600, 50.0)
        self.q_liq_table.setRowCount(len(clusters))
        for i, c in enumerate(clusters):
            longs = c["long"] >= c["short"]
            typ = "longs" if longs else "shorts"
            cells = [
                (f"{c['price']:,.0f}", TXT),
                (f"{typ} liquidés", RED if longs else GREEN),
                (f"{c['total']/1e6:.2f} M$" if c["total"] >= 1e6 else f"{c['total']/1e3:.0f} k$", AMBER),
            ]
            for j, (v, cc) in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(v)
                it.setForeground(QtGui.QColor(cc))
                self.q_liq_table.setItem(i, j, it)

    # ===========================================================
    # PAGE OPTIONS — GRAPHIQUES DÉTAILLÉS (Deribit)
    # ===========================================================

    def _mk_qplot(self, glw, row, col, title):
        plt = glw.addPlot(row=row, col=col)
        plt.showGrid(x=True, y=True, alpha=0.08)
        plt.setTitle(title, color="#aab4c0", size="10pt")
        plt.getAxis("left").setTextPen(pg.mkPen("#66707e"))
        plt.getAxis("bottom").setTextPen(pg.mkPen("#66707e"))
        return plt

    def _build_options_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14); outer.setSpacing(10)

        intro = QtWidgets.QLabel(
            "OPTIONS BTC (Deribit) en graphiques. SMILE : l'IV par strike — le côté le plus "
            "haut dit où est la peur. OI PAR STRIKE : où sont les paris (calls verts / puts "
            "rouges) — les gros strikes agissent comme des aimants/murs. MAX PAIN : la courbe "
            "de douleur — son minimum est le prix qui ruine le plus d'acheteurs d'options "
            "(aimant d'expiration). TERM STRUCTURE : l'IV par échéance — inversée = stress court terme.")
        intro.setWordWrap(True); intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        self.opt_read = QtWidgets.QLabel("Chargement Deribit…")
        self.opt_read.setWordWrap(True)
        self.opt_read.setStyleSheet(f"color:{TXT};font-size:13px;font-weight:700;"
                                    f"background:{PANEL2};border:1px solid {BORDER};"
                                    f"border-radius:10px;padding:11px;")
        outer.addWidget(self.opt_read)

        glw = pg.GraphicsLayoutWidget(); glw.setBackground("#0d1117")
        self.opt_smile = self._mk_qplot(glw, 0, 0, "SMILE — IV % par strike (calls bleu · puts rouge)")
        self.opt_smile_c = self.opt_smile.plot([], [], pen=pg.mkPen("#4da3ff", width=2),
                                               symbol="o", symbolSize=4, symbolBrush="#4da3ff", symbolPen=None)
        self.opt_smile_p = self.opt_smile.plot([], [], pen=pg.mkPen("#ff5c5c", width=2),
                                               symbol="o", symbolSize=4, symbolBrush="#ff5c5c", symbolPen=None)
        self.opt_smile_spot = pg.InfiniteLine(angle=90, pen=pg.mkPen("#f5c518", width=1,
                                              style=QtCore.Qt.PenStyle.DashLine))
        self.opt_smile.addItem(self.opt_smile_spot)

        self.opt_oi = self._mk_qplot(glw, 0, 1, "OPEN INTEREST par strike (calls ↑ verts · puts ↓ rouges)")
        self.opt_oi_c = pg.BarGraphItem(x=[], height=[], width=180, brush=(61, 220, 132, 170))
        self.opt_oi_p = pg.BarGraphItem(x=[], height=[], width=180, brush=(255, 92, 92, 170))
        self.opt_oi.addItem(self.opt_oi_c); self.opt_oi.addItem(self.opt_oi_p)
        self.opt_oi_spot = pg.InfiniteLine(angle=90, pen=pg.mkPen("#f5c518", width=1,
                                           style=QtCore.Qt.PenStyle.DashLine))
        self.opt_oi.addItem(self.opt_oi_spot)

        self.opt_pain = self._mk_qplot(glw, 1, 0, "COURBE MAX PAIN (minimum = aimant d'expiration)")
        self.opt_pain_c = self.opt_pain.plot([], [], pen=pg.mkPen("#c48bff", width=2))
        self.opt_pain_min = self.opt_pain.plot([], [], pen=None, symbol="star",
                                               symbolSize=14, symbolBrush="#f5c518", symbolPen=None)
        self.opt_pain_spot = pg.InfiniteLine(angle=90, pen=pg.mkPen("#f5c518", width=1,
                                             style=QtCore.Qt.PenStyle.DashLine))
        self.opt_pain.addItem(self.opt_pain_spot)

        self.opt_term = self._mk_qplot(glw, 1, 1, "TERM STRUCTURE — IV ATM par échéance (jours)")
        self.opt_term_c = self.opt_term.plot([], [], pen=pg.mkPen("#3ddc84", width=2),
                                             symbol="o", symbolSize=6, symbolBrush="#3ddc84", symbolPen=None)
        outer.addWidget(glw, 1)
        self._opt_page = page

        self._opt_timer = QtCore.QTimer(self)
        self._opt_timer.timeout.connect(self._refresh_options_page)
        self._opt_timer.start(4000)
        return page

    def _refresh_options_page(self):
        if self.tabs.currentWidget() is not getattr(self, "_opt_page", None):
            return
        o = getattr(self, "quant", None) and self.quant.options
        if not o or not o.get("smile"):
            return
        sm = o["smile"]; under = o["under"]
        ks_c = [s["k"] for s in sm if s["civ"]]; iv_c = [s["civ"] for s in sm if s["civ"]]
        ks_p = [s["k"] for s in sm if s["piv"]]; iv_p = [s["piv"] for s in sm if s["piv"]]
        self.opt_smile_c.setData(ks_c, iv_c); self.opt_smile_p.setData(ks_p, iv_p)
        self.opt_smile_spot.setValue(under)
        ks = [s["k"] for s in sm]
        width = (max(ks) - min(ks)) / max(1, len(ks)) * 0.42 if len(ks) > 1 else 180
        self.opt_oi_c.setOpts(x=[k - width/2 for k in ks], height=[s["coi"] for s in sm], width=width)
        self.opt_oi_p.setOpts(x=[k + width/2 for k in ks], height=[-s["poi"] for s in sm], width=width)
        self.opt_oi_spot.setValue(under)
        pains = [s["pain"] for s in sm]
        self.opt_pain_c.setData(ks, pains)
        if o.get("max_pain") is not None:
            mp = o["max_pain"]
            mp_pain = next((s["pain"] for s in sm if s["k"] == mp), min(pains))
            self.opt_pain_min.setData([mp], [mp_pain])
        self.opt_pain_spot.setValue(under)
        terms = o.get("terms") or []
        self.opt_term_c.setData([t["days"] for t in terms], [t["iv"] for t in terms])
        # lecture synthétique
        sk = o.get("skew"); mp = o.get("max_pain")
        parts = [f"Spot {under:,.0f} · expi {o['front']} · IV ATM {o['atm_iv']:.0f}%"]
        if sk is not None:
            parts.append("peur côté BAS (puts chers)" if sk > 2 else
                         "appétit côté HAUT (calls chers)" if sk < -2 else "skew neutre")
        if mp:
            parts.append(f"max pain {mp:,.0f} ({mp-under:+,.0f}$) → aimant vers "
                         f"{'le haut' if mp > under else 'le bas'} à l'expiration")
        if terms and len(terms) >= 2:
            parts.append("term structure INVERSÉE = stress court terme"
                         if terms[0]["iv"] > terms[-1]["iv"] + 3 else "term structure normale")
        self.opt_read.setText("Lecture : " + "  ·  ".join(parts))

    # ===========================================================
    # PAGE MACRO — GRAPHIQUES INTRADAY (risk-on / risk-off)
    # ===========================================================

    def _build_macro_page(self):
        from quant import MACRO_SYMBOLS
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14); outer.setSpacing(10)

        intro = QtWidgets.QLabel(
            "MACRO intraday : la journée de chaque actif qui influence BTC. Nasdaq/S&P en "
            "hausse + dollar/VIX en baisse = RISK-ON (vent porteur BTC). L'inverse = RISK-OFF. "
            "Ligne pointillée = clôture de la veille (au-dessus = journée verte).")
        intro.setWordWrap(True); intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        self.mac_read = QtWidgets.QLabel("Chargement macro…")
        self.mac_read.setWordWrap(True)
        self.mac_read.setStyleSheet(f"color:{TXT};font-size:14px;font-weight:800;"
                                    f"background:{PANEL2};border:1px solid {BORDER};"
                                    f"border-radius:10px;padding:12px;")
        outer.addWidget(self.mac_read)

        glw = pg.GraphicsLayoutWidget(); glw.setBackground("#0d1117")
        self.mac_plots = {}
        for i, (name, _s) in enumerate(MACRO_SYMBOLS):
            plt = self._mk_qplot(glw, i // 3, i % 3, name)
            curve = plt.plot([], [], pen=pg.mkPen("#4da3ff", width=1.6))
            prev_l = plt.addLine(y=0, pen=pg.mkPen("#8a94a6", width=0.8,
                                                   style=QtCore.Qt.PenStyle.DashLine))
            self.mac_plots[name] = (plt, curve, prev_l)
        outer.addWidget(glw, 1)
        self._mac_page = page

        self._mac_timer = QtCore.QTimer(self)
        self._mac_timer.timeout.connect(self._refresh_macro_page)
        self._mac_timer.start(5000)
        return page

    def _refresh_macro_page(self):
        if self.tabs.currentWidget() is not getattr(self, "_mac_page", None):
            return
        m = getattr(self, "quant", None) and self.quant.macro
        if not m:
            return
        for name, (plt, curve, prev_l) in self.mac_plots.items():
            v = m.get(name) or {}
            series = v.get("series") or []
            if len(series) >= 2:
                t0 = series[0][0]
                xs = [(t - t0) / 3600.0 for t, _ in series]
                ys = [c for _, c in series]
                curve.setData(xs, ys)
                chg = v.get("chg")
                col = "#3ddc84" if (chg or 0) >= 0 else "#ff5c5c"
                curve.setPen(pg.mkPen(col, width=1.6))
                plt.setTitle(f"{name}   {v.get('price'):,.2f}   ({chg:+.2f}%)"
                             if chg is not None else name, color=col, size="10pt")
                if v.get("prev"):
                    prev_l.setValue(v["prev"])
        # verdict global (réutilise la logique du QUANT)
        def chg(n):
            v = m.get(n)
            return v["chg"] if (v and v.get("chg") is not None) else 0.0
        score = ((1 if chg("Nasdaq") > 0 else -1) + (1 if chg("S&P 500") > 0 else -1)
                 + (1 if chg("Dollar (DXY)") < 0 else -1) + (1 if chg("VIX (peur)") < 0 else -1))
        if score >= 2:
            self.mac_read.setText("🟢 RISK-ON — actions en hausse, dollar/peur en baisse : "
                                  "vent porteur pour BTC.")
        elif score <= -2:
            self.mac_read.setText("🔴 RISK-OFF — actions en baisse, dollar/peur en hausse : "
                                  "vent contraire pour BTC, prudence sur les longs.")
        else:
            self.mac_read.setText("🟡 Macro mitigée — BTC trade surtout sur son propre flux.")

    # ===========================================================
    # PAGE FOOTPRINT — ORDER FLOW PAR BOUGIE (institutionnel)
    # ===========================================================

    def _build_footprint_page(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14); outer.setSpacing(10)

        intro = QtWidgets.QLabel(
            "FOOTPRINT (cellules pures, ~4H). Mode <b>Δ net</b> : chaque cellule = delta net "
            "(achat−vente) au niveau — <span style='color:#3ddc84;'>vert = achat net</span>, "
            "<span style='color:#ff5c5c;'>rouge = vente nette</span>, barre horizontale ∝ "
            "volume traité au niveau. Mode <b>Vente × Achat</b> : les deux volumes bruts. "
            "Cadre jaune = POC. Sous chaque colonne : Δ + volume. En bas : DELTA CUMULÉ. "
            "Marqueurs : ▼▲DIV = divergence · ABS = absorption. Dossier complet à droite. "
            "Glisse horizontalement pour remonter les 4H. Historique pré-chargé au lancement.")
        intro.setWordWrap(True); intro.setTextFormat(QtCore.Qt.TextFormat.RichText)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        ctrl = QtWidgets.QHBoxLayout(); ctrl.setSpacing(8)
        def combo(items, cur):
            c = QtWidgets.QComboBox(); c.addItems(items); c.setCurrentText(cur)
            c.setStyleSheet(f"QComboBox{{background:{PANEL};border:1px solid {BORDER};"
                            f"border-radius:8px;color:{TXT};padding:6px 12px;font-weight:700;}}")
            return c
        lbl0 = QtWidgets.QLabel("MODE :"); lbl0.setStyleSheet(f"color:{DIM};font-weight:700;font-size:11px;")
        ctrl.addWidget(lbl0)
        self.fp_mode = combo(["Vente × Achat", "Δ net (delta)"], "Vente × Achat")
        ctrl.addWidget(self.fp_mode)
        lbl = QtWidgets.QLabel("  BOUGIE :"); lbl.setStyleSheet(f"color:{DIM};font-weight:700;font-size:11px;")
        ctrl.addWidget(lbl)
        self.fp_tf = combo(["1 min", "2 min", "5 min"], "1 min")
        ctrl.addWidget(self.fp_tf)
        lbl2 = QtWidgets.QLabel("  CELLULE :"); lbl2.setStyleSheet(f"color:{DIM};font-weight:700;font-size:11px;")
        ctrl.addWidget(lbl2)
        self.fp_bucket = combo(["10 $", "20 $", "25 $", "50 $"], "20 $")
        ctrl.addWidget(self.fp_bucket)
        self.fp_lock = QtWidgets.QCheckBox("🔒 figer l'échelle (zoom manuel)")
        self.fp_lock.setStyleSheet(f"QCheckBox{{color:{TXT};font-size:12px;font-weight:600;}}")
        ctrl.addWidget(self.fp_lock)
        self.fp_status = QtWidgets.QLabel("…")
        self.fp_status.setStyleSheet(f"color:{DIM};font-size:11px;")
        ctrl.addWidget(self.fp_status)
        ctrl.addStretch(1)
        outer.addLayout(ctrl)

        # bandeau SIGNAUX (divergences delta, absorptions détectées)
        self.fp_signals_lbl = QtWidgets.QLabel("Signaux : …")
        self.fp_signals_lbl.setWordWrap(True)
        self.fp_signals_lbl.setStyleSheet(f"color:{TXT};font-size:12px;font-weight:700;"
                                          f"background:{PANEL2};border:1px solid {BORDER};"
                                          f"border-radius:8px;padding:8px;")
        outer.addWidget(self.fp_signals_lbl)

        glw = pg.GraphicsLayoutWidget()
        glw.setBackground("#0d1117")
        self.fp_plot = glw.addPlot(row=0, col=0)
        self.fp_plot.showGrid(x=False, y=True, alpha=0.10)
        self.fp_plot.getAxis("left").setTextPen(pg.mkPen("#8a94a6"))
        self.fp_plot.getAxis("bottom").setTextPen(pg.mkPen("#8a94a6"))
        self.fp_item = FootprintItem()
        self.fp_plot.addItem(self.fp_item)
        # LIGNE DE PRIX EN DIRECT (suit le prix exact, avec la valeur affichée)
        self.fp_price_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#00e5ff", width=1.4, style=QtCore.Qt.PenStyle.DashLine),
            label="{value:,.0f}",
            labelOpts={"color": "#0d1117", "fill": (0, 229, 255, 230),
                       "position": 0.015, "movable": False})
        self.fp_price_line.setZValue(50)
        self.fp_plot.addItem(self.fp_price_line)
        self.fp_cum = glw.addPlot(row=1, col=0)
        self.fp_cum.setMaximumHeight(150)
        self.fp_cum.showGrid(x=False, y=True, alpha=0.10)
        self.fp_cum.getAxis("left").setTextPen(pg.mkPen("#8a94a6"))
        self.fp_cum.getAxis("bottom").setTextPen(pg.mkPen("#8a94a6"))
        self.fp_cum.setLabel("left", "Δ cumulé", color="#8a94a6")
        self.fp_cum.setXLink(self.fp_plot)
        self.fp_cum_curve = self.fp_cum.plot([], [], pen=pg.mkPen("#f5c518", width=2),
                                             symbol="o", symbolSize=4,
                                             symbolBrush="#f5c518", symbolPen=None)
        self.fp_cum.addLine(y=0, pen=pg.mkPen("#555", width=0.7))
        glw.ci.layout.setRowStretchFactor(0, 4)
        glw.ci.layout.setRowStretchFactor(1, 1)

        # chart à gauche + DOSSIER de stats détaillé à droite
        body = QtWidgets.QHBoxLayout(); body.setSpacing(10)
        body.addWidget(glw, 1)
        self.fp_dossier = QtWidgets.QTextEdit(); self.fp_dossier.setReadOnly(True)
        self.fp_dossier.setFixedWidth(300)
        self.fp_dossier.setStyleSheet(f"QTextEdit{{background:{PANEL};border:1px solid {BORDER};"
                                      f"border-radius:10px;color:{TXT};font-size:12px;padding:12px;}}")
        body.addWidget(self.fp_dossier)
        outer.addLayout(body, 1)
        self._fp_page = page

        self._fp_timer = QtCore.QTimer(self)
        self._fp_timer.timeout.connect(self._refresh_footprint)
        self._fp_timer.start(2000)
        return page

    def _refresh_footprint(self):
        # ne travaille que si la page est visible (zéro coût sinon)
        if self.tabs.currentWidget() is not getattr(self, "_fp_page", None):
            return
        import time as _t
        tf = {"1 min": 60, "2 min": 120, "5 min": 300}[self.fp_tf.currentText()]
        bucket = float(self.fp_bucket.currentText().replace("$", "").strip())
        n_bars = min(80, max(20, int(4 * 3600 / tf)))      # ~4H (cap 80 col pour la fluidité)
        fp = self.engine.get_footprint(tf, bucket, n_bars=n_bars)
        bars = fp["bars"]
        if not bars:
            self.fp_status.setText("· pré-chargement des trades (4H) en cours…")
            return
        n = len(bars)
        span_min = (bars[-1]["t0"] - bars[0]["t0"]) / 60 + tf / 60
        tot_trades = sum(b["buy"] + b["sell"] for b in bars)
        self.fp_status.setText(f"· {n} bougies · ~{span_min:.0f} min · {tot_trades:,.0f} BTC traités")

        # ---- SIGNAUX : divergence delta + absorption (couche spec) ----
        import time as _tt
        signals = []
        for i in range(2, n):
            b = bars[i]; prev = bars[i - 1]
            d_i = b["buy"] - b["sell"]; d_p = prev["buy"] - prev["sell"]
            look = bars[max(0, i - 5):i]
            if look:
                if b["h"] > max(x["h"] for x in look) and d_i < d_p and d_i < 0:
                    signals.append({"i": i, "t": "div_bear"})     # nouveau haut, delta qui rétrécit
                if b["l"] < min(x["l"] for x in look) and d_i > d_p and d_i > 0:
                    signals.append({"i": i, "t": "div_bull"})
            if i < n - 1 and b["cells"]:
                nxt = bars[i + 1]
                low_lvl = min(b["cells"]); high_lvl = max(b["cells"])
                lb, ls = b["cells"][low_lvl]; hb, hs = b["cells"][high_lvl]
                # grosses ventes au plus bas mais le niveau TIENT -> absorption acheteuse
                if ls >= 2 * lb and ls >= 0.8 and nxt["l"] >= b["l"]:
                    signals.append({"i": i, "t": "abs_bull"})
                # gros achats au plus haut mais ça ne passe pas -> absorption vendeuse
                if hb >= 2 * hs and hb >= 0.8 and nxt["h"] <= b["h"]:
                    signals.append({"i": i, "t": "abs_bear"})
        names = {"div_bear": "▼ divergence baissière", "div_bull": "▲ divergence haussière",
                 "abs_bull": "absorption ACHETEUSE (plancher)", "abs_bear": "absorption VENDEUSE (plafond)"}
        recent = [f"{_tt.strftime('%H:%M', _tt.localtime(bars[s['i']]['t0']))} {names[s['t']]}"
                  for s in signals[-5:]]
        self.fp_signals_lbl.setText("Signaux : " + ("  ·  ".join(recent) if recent
                                    else "aucun (divergences delta et absorptions s'affichent ici)"))

        # ligne de prix EN DIRECT (prix live du carnet, sinon dernière clôture)
        live = (self._last_state or {}).get("mid") or bars[-1]["c"]
        self.fp_price_line.setValue(live)

        mode = "delta" if self.fp_mode.currentText().startswith("Δ") else "split"
        y_lo = min(b["l"] for b in bars) - 3.0 * bucket
        y_hi = max(b["h"] for b in bars) + 4.4 * bucket
        self.fp_item.setFPData(bars, bucket, y_lo, y_hi, mode=mode, signals=signals)
        if not self.fp_lock.isChecked():
            # vue initiale : ~13 dernières bougies (cellules larges et lisibles comme
            # sur MotiveWave), on peut glisser à gauche pour parcourir tout l'historique 4H
            x0 = max(-0.6, n - 13)
            vis = bars[int(max(0, x0)):]
            vy_lo = min(b["l"] for b in vis) - 3.0 * bucket
            vy_hi = max(b["h"] for b in vis) + 4.4 * bucket
            self.fp_plot.setYRange(vy_lo, vy_hi, padding=0)
            self.fp_plot.setXRange(x0, n + 0.4, padding=0)
        # axe temps (HH:MM sous chaque 2e bougie)
        ticks = [(i, _t.strftime("%H:%M", _t.localtime(b["t0"])))
                 for i, b in enumerate(bars) if i % 2 == 0]
        self.fp_plot.getAxis("bottom").setTicks([ticks])
        self.fp_cum.getAxis("bottom").setTicks([ticks])
        # delta cumulé
        cum = []; c = 0.0
        for b in bars:
            c += b["buy"] - b["sell"]; cum.append(c)
        self.fp_cum_curve.setData(list(range(n)), cum)

        # ---- DOSSIER COMPLET (profil agrégé sur toute la période) ----
        from collections import defaultdict
        prof = defaultdict(lambda: [0.0, 0.0])
        for b in bars:
            for lvl, (bv, sv) in b["cells"].items():
                prof[lvl][0] += bv; prof[lvl][1] += sv
        tot_buy = sum(v[0] for v in prof.values())
        tot_sell = sum(v[1] for v in prof.values())
        tot_vol = tot_buy + tot_sell or 1.0
        poc = max(prof, key=lambda k: prof[k][0] + prof[k][1]) if prof else 0
        # value area 70% autour du POC
        levels_sorted = sorted(prof)
        vah = val = poc
        if poc:
            idx = levels_sorted.index(poc)
            va = prof[poc][0] + prof[poc][1]; lo_i = hi_i = idx
            target = tot_vol * 0.70
            while va < target and (lo_i > 0 or hi_i < len(levels_sorted) - 1):
                la = (prof[levels_sorted[lo_i-1]][0]+prof[levels_sorted[lo_i-1]][1]) if lo_i > 0 else -1
                ha = (prof[levels_sorted[hi_i+1]][0]+prof[levels_sorted[hi_i+1]][1]) if hi_i < len(levels_sorted)-1 else -1
                if la >= ha and lo_i > 0:
                    lo_i -= 1; va += la
                elif hi_i < len(levels_sorted)-1:
                    hi_i += 1; va += ha
                else:
                    break
            val = levels_sorted[lo_i]; vah = levels_sorted[hi_i]
        top_vol = sorted(prof.items(), key=lambda kv: kv[1][0]+kv[1][1], reverse=True)[:5]
        top_buy = sorted(prof.items(), key=lambda kv: kv[1][0]-kv[1][1], reverse=True)[:3]
        top_sell = sorted(prof.items(), key=lambda kv: kv[1][1]-kv[1][0], reverse=True)[:3]
        net = tot_buy - tot_sell
        H = [f"<div style='color:{DIM};font-weight:800;letter-spacing:1px;'>DOSSIER FOOTPRINT</div>",
             f"<div style='color:{DIM};font-size:11px;margin-bottom:8px;'>{n} bougies · "
             f"~{span_min:.0f} min · cellule {bucket:.0f}$</div>",
             "<b style='color:#aab4c0;'>VOLUME</b>",
             f"Total : <b>{tot_vol:,.0f}</b> BTC (~{tot_vol*bars[-1]['c']/1e6:,.0f} M$)",
             f"Achat : <span style='color:{GREEN};'>{tot_buy:,.0f}</span> · "
             f"Vente : <span style='color:{RED};'>{tot_sell:,.0f}</span>",
             f"Delta net : <b style='color:{GREEN if net>=0 else RED};'>{net:+,.0f}</b> BTC · "
             f"CVD : <b style='color:{GREEN if cum[-1]>=0 else RED};'>{cum[-1]:+,.0f}</b>",
             f"<br><b style='color:#aab4c0;'>NIVEAUX CLÉS</b>",
             f"POC : <b style='color:#f5c518;'>{poc:,.0f}</b>",
             f"Value Area 70% : <b>{val:,.0f} → {vah:,.0f}</b>",
             f"<br><b style='color:#aab4c0;'>TOP VOLUME</b>"]
        for lvl, (bv, sv) in top_vol:
            H.append(f"{lvl:,.0f} — {bv+sv:,.0f} BTC (Δ{bv-sv:+.0f})")
        H.append(f"<br><b style='color:{GREEN};'>PLUS FORT ACHAT NET</b>")
        for lvl, (bv, sv) in top_buy:
            H.append(f"{lvl:,.0f} — <span style='color:{GREEN};'>+{bv-sv:,.0f}</span> BTC")
        H.append(f"<b style='color:{RED};'>PLUS FORTE VENTE NETTE</b>")
        for lvl, (bv, sv) in top_sell:
            H.append(f"{lvl:,.0f} — <span style='color:{RED};'>{bv-sv:,.0f}</span> BTC")
        H.append(f"<br><b style='color:#aab4c0;'>SIGNAUX</b> : {len(signals)} détectés")
        self._hset(self.fp_dossier, "<br>".join(H))

    # ===========================================================
    # PAGE Z-SCORES — EXTRÊMES STATISTIQUES MULTI-ÉCHELLES
    # ===========================================================

    ZS_METRICS = ["CVD 1min", "CVD 5min", "CVD 15min", "Agresseurs %", "Tape (tr/s)",
                  "Imbalance %", "OI Δ5min %", "Funding %", "Instit Δ5min"]

    def _zs_sample(self):
        """Échantillonne toutes les métriques (5s) — buffer 3h pour les z-scores."""
        import time as _t
        s = self._last_state or {}
        if not s.get("mid") or s.get("warming"):
            return
        cv = self.engine.get_cvd_windows()
        pos = self.engine.get_positioning()
        seg = self.engine.get_flow_segments(300)
        oi = pos.get("oi") or {}
        f = pos.get("funding") or {}

        def cvd(m):
            d = cv.get(m, {})
            return d.get("cvd", 0.0) if d.get("ready") else 0.0
        self._zs_buf.append({
            "t": _t.time(),
            "CVD 1min": cvd(1), "CVD 5min": cvd(5), "CVD 15min": cvd(15),
            "Agresseurs %": s.get("aggressor_ratio", 0.5) * 100,
            "Tape (tr/s)": s.get("tape_speed", 0.0),
            "Imbalance %": s.get("imbalance", 0.5) * 100,
            "OI Δ5min %": oi.get("chg_5m_pct", 0.0),
            "Funding %": f.get("rate_pct", 0.0) or 0.0,
            "Instit Δ5min": seg["inst"]["delta"] if seg else 0.0,
        })
        # sauvegarde de l'historique toutes les ~60 s (survit aux redémarrages)
        if _t.time() - getattr(self, "_zs_last_save", 0) > 60:
            self._zs_last_save = _t.time()
            self._zs_save()
        if self.tabs.currentWidget() is getattr(self, "_zs_page", None):
            self._refresh_zscores()

    def _zs_path(self):
        return data_file("zscore_history.json")

    def _zs_load(self):
        """Recharge l'historique des métriques (agresseurs, CVD, tape…) au lancement."""
        import json, os, time as _t
        p = self._zs_path()
        if not os.path.exists(p):
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        cut = _t.time() - 24 * 3600
        for x in data:
            if isinstance(x, dict) and x.get("t", 0) >= cut:
                self._zs_buf.append(x)

    def _zs_save(self):
        import json
        try:
            with open(self._zs_path(), "w", encoding="utf-8") as f:
                json.dump(list(self._zs_buf), f)
        except OSError:
            pass

    @staticmethod
    def _zscore(vals):
        a = np.asarray(vals, dtype=float)
        sd = a.std()
        return float((a[-1] - a.mean()) / sd) if sd > 1e-9 else 0.0

    def _build_zscore_page(self):
        from collections import deque
        # historique PERSISTANT : 24 h à 5 s, sauvegardé sur disque -> ne se vide plus
        # à chaque redémarrage (avant : 3 h en mémoire, perdu à chaque lancement)
        self._zs_buf = deque(maxlen=17280)         # 24 h à 5 s
        self._zs_load()
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14); outer.setSpacing(10)

        intro = QtWidgets.QLabel(
            "Z-SCORES : à quel point chaque métrique est ANORMALE par rapport à son "
            "comportement récent. z = (valeur − moyenne) / écart-type, calculé sur 3 "
            "échelles (15 min / 1 h / 3 h). |z| ≥ 2 = anormal (2,3% du temps), |z| ≥ 3 = "
            "extrême (0,1%). Intraday, un extrême statistique = souvent un point de "
            "bascule ou le début d'un mouvement. Courbes : valeur brute + moyenne (gris) "
            "± 2σ (pointillés ambre) sur 1 h.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{DIM};font-size:12px;")
        outer.addWidget(intro)

        # sélecteur de fenêtre de temps des graphiques
        zrow = QtWidgets.QHBoxLayout(); zrow.setSpacing(8)
        zlbl = QtWidgets.QLabel("FENÊTRE DES GRAPHIQUES :")
        zlbl.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:0.5px;")
        zrow.addWidget(zlbl)
        self.ZS_WINDOWS = {"15 min": 15, "1 h": 60, "3 h": 180, "6 h": 360,
                           "12 h": 720, "24 h": 1440, "Tout": None}
        self.zs_win_combo = QtWidgets.QComboBox()
        self.zs_win_combo.addItems(list(self.ZS_WINDOWS.keys()))
        self.zs_win_combo.setCurrentText("3 h")
        self.zs_win_combo.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:6px 12px;font-weight:700;}}")
        self.zs_win_combo.currentIndexChanged.connect(self._refresh_zscores)
        zrow.addWidget(self.zs_win_combo)
        znote = QtWidgets.QLabel("· s'applique à tous les graphiques · double-clique un "
                                 "graphique pour l'AGRANDIR (avec sa propre fenêtre)")
        znote.setStyleSheet(f"color:{DIM};font-size:11px;")
        zrow.addWidget(znote); zrow.addStretch()
        outer.addLayout(zrow)

        # tableau récapitulatif multi-échelles
        cols = ["Métrique", "Valeur", "z 15min", "z 1h", "z 3h", "Lecture"]
        self.zs_table = QtWidgets.QTableWidget(len(self.ZS_METRICS), len(cols))
        self.zs_table.setHorizontalHeaderLabels(cols)
        self._prep(self.zs_table)
        self.zs_table.setMaximumHeight(320)
        outer.addWidget(self.zs_table)

        # grille de vrais graphiques 2D (3×3)
        glw = pg.GraphicsLayoutWidget()
        glw.setBackground("#0d1117")
        self.zs_plots = {}
        for i, mname in enumerate(self.ZS_METRICS):
            plt = glw.addPlot(row=i // 3, col=i % 3)
            plt.showGrid(x=False, y=True, alpha=0.08)
            plt.setTitle(mname, color="#aab4c0", size="9pt")
            plt.getAxis("left").setTextPen(pg.mkPen("#66707e"))
            plt.getAxis("bottom").setTextPen(pg.mkPen("#66707e"))
            curve = plt.plot([], [], pen=pg.mkPen("#4da3ff", width=1.4))
            mean_l = plt.addLine(y=0, pen=pg.mkPen("#8a94a6", width=0.8))
            up_l = plt.addLine(y=0, pen=pg.mkPen("#f5c518", width=0.8,
                                                 style=QtCore.Qt.PenStyle.DashLine))
            dn_l = plt.addLine(y=0, pen=pg.mkPen("#f5c518", width=0.8,
                                                 style=QtCore.Qt.PenStyle.DashLine))
            self.zs_plots[mname] = (plt, curve, mean_l, up_l, dn_l)
        outer.addWidget(glw, 1)
        # double-clic sur un graphique -> vue plein écran détaillée
        self._zs_glw = glw
        glw.scene().sigMouseClicked.connect(self._zs_plot_clicked)
        self._zs_detail_dlgs = []
        self._zs_page = page

        self._zs_timer = QtCore.QTimer(self)
        self._zs_timer.timeout.connect(self._zs_sample)
        self._zs_timer.start(5000)
        return page

    def _refresh_zscores(self, *args):
        # trié par temps : le buffer mélange historique persistant + live + serveur,
        # une courbe non-monotone en x provoquerait des zigzags
        buf = sorted(self._zs_buf, key=lambda x: x.get("t", 0))
        if len(buf) < 6:
            return
        now = buf[-1]["t"]

        def zs_read(metric, z):
            if abs(z) < 2:
                return ""
            hi = z > 0
            texts = {
                "CVD 1min": ("pression ACHETEUSE anormale", "pression VENDEUSE anormale"),
                "CVD 5min": ("achat net anormal", "vente nette anormale"),
                "CVD 15min": ("tendance acheteuse forte (fond)", "tendance vendeuse forte (fond)"),
                "Agresseurs %": ("acheteurs ultra-dominants", "vendeurs ultra-dominants"),
                "Tape (tr/s)": ("activité explosive", "marché anormalement mort"),
                "Imbalance %": ("carnet très penché achat", "carnet très penché vente"),
                "OI Δ5min %": ("levier qui RENTRE vite", "positions qui FERMENT vite"),
                "Funding %": ("longs sur-payés (risque flush)", "shorts sur-payés (risque squeeze)"),
                "Instit Δ5min": ("les GROS achètent anormalement", "les GROS vendent anormalement"),
            }
            a, b = texts.get(metric, ("anormalement haut", "anormalement bas"))
            return (a if hi else b) + (" ⚠ EXTRÊME" if abs(z) >= 3 else "")

        for i, mname in enumerate(self.ZS_METRICS):
            vals = [x.get(mname, 0.0) for x in buf]
            cur = vals[-1]
            zz = {}
            for scale, nsamp in (("15min", 180), ("1h", 720), ("3h", 2160)):
                sub = vals[-nsamp:]
                zz[scale] = self._zscore(sub) if len(sub) >= 24 else None
            worst = max((abs(v) for v in zz.values() if v is not None), default=0)
            zmax = next((v for v in zz.values() if v is not None and abs(v) == worst), 0)
            cells = [(mname, TXT), (f"{cur:+.2f}" if "CVD" in mname or "Δ" in mname
                                    else f"{cur:.2f}", ACCENT)]
            for scale in ("15min", "1h", "3h"):
                z = zz[scale]
                if z is None:
                    cells.append(("—", DIM))
                else:
                    col = RED if abs(z) >= 3 else (AMBER if abs(z) >= 2 else
                                                   TXT if abs(z) >= 1 else DIM)
                    cells.append((f"{z:+.1f}", col))
            read = zs_read(mname, zmax)
            cells.append((read, AMBER if read else DIM))
            for j, (v, cc) in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(v)
                it.setForeground(QtGui.QColor(cc))
                if j in (2, 3, 4) and v not in ("—",) and abs(float(v)) >= 2:
                    f = it.font(); f.setBold(True); it.setFont(f)
                self.zs_table.setItem(i, j, it)

        # fenêtre choisie par l'utilisateur (en minutes ; None = tout l'historique)
        win_min = self.ZS_WINDOWS.get(self.zs_win_combo.currentText(), 180)
        # graphiques : valeur brute + moyenne ±2σ, calculées sur la fenêtre visible
        for mname, (plt, curve, mean_l, up_l, dn_l) in self.zs_plots.items():
            xs = [(x["t"] - now) / 60.0 for x in buf]          # minutes (négatif → 0)
            vals = [x.get(mname, 0.0) for x in buf]
            curve.setData(xs, vals)
            # échantillons dans la fenêtre visible (pour la moyenne et les bandes)
            vis = [v for v, xm in zip(vals, xs) if win_min is None or xm >= -win_min]
            sub = np.asarray(vis if len(vis) >= 3 else vals, dtype=float)
            m, sd = float(sub.mean()), float(sub.std())
            mean_l.setValue(m); up_l.setValue(m + 2 * sd); dn_l.setValue(m - 2 * sd)
            if win_min is None:
                plt.enableAutoRange(axis="x")
            else:
                plt.setXRange(-win_min, win_min * 0.02, padding=0)

    def _zs_plot_clicked(self, ev):
        """Double-clic sur un des 9 graphiques -> ouvre la vue détaillée plein écran."""
        try:
            if not ev.double():
                return
            pos = ev.scenePos()
        except Exception:
            return
        for mname, (plt, *_rest) in self.zs_plots.items():
            if plt.getViewBox().sceneBoundingRect().contains(pos):
                self._zs_open_detail(mname)
                return

    def _zs_open_detail(self, metric):
        """Fenêtre GRAND FORMAT d'une seule métrique, avec son propre sélecteur de
        fenêtre de temps et les bandes ±2σ. Se met à jour en direct."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Détail — {metric}")
        dlg.resize(1150, 680)
        dlg.setStyleSheet(f"QDialog{{background:{BG};}}")
        lay = QtWidgets.QVBoxLayout(dlg); lay.setContentsMargins(14, 14, 14, 14); lay.setSpacing(10)

        top = QtWidgets.QHBoxLayout(); top.setSpacing(8)
        ttl = QtWidgets.QLabel(metric)
        ttl.setStyleSheet(f"color:{TXT};font-size:18px;font-weight:800;")
        top.addWidget(ttl); top.addStretch()
        wl = QtWidgets.QLabel("FENÊTRE :")
        wl.setStyleSheet(f"color:{DIM};font-size:11px;font-weight:700;letter-spacing:0.5px;")
        top.addWidget(wl)
        combo = QtWidgets.QComboBox(); combo.addItems(list(self.ZS_WINDOWS.keys()))
        combo.setCurrentText(self.zs_win_combo.currentText())
        combo.setStyleSheet(
            f"QComboBox{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;"
            f"color:{TXT};padding:6px 12px;font-weight:700;}}")
        top.addWidget(combo)
        lay.addLayout(top)

        # axe de temps réel (heures:minutes) -> l'utilisateur zoome/déplace librement
        axis = pg.DateAxisItem(orientation="bottom")
        plt = pg.PlotWidget(axisItems={"bottom": axis}); plt.setBackground(BG)
        plt.showGrid(x=True, y=True, alpha=0.12)
        plt.getAxis("left").setTextPen(pg.mkPen(TXT))
        plt.getAxis("bottom").setTextPen(pg.mkPen(DIM))
        plt.setMouseEnabled(x=True, y=True)          # molette = zoom, glisser = déplacer
        # vraies données brutes, aucun lissage (représentatif des vrais niveaux)
        curve = plt.plot([], [], pen=pg.mkPen("#4da3ff", width=1.4))
        mean_l = plt.addLine(y=0, pen=pg.mkPen("#8a94a6", width=1))
        up_l = plt.addLine(y=0, pen=pg.mkPen("#f5c518", width=1, style=QtCore.Qt.PenStyle.DashLine))
        dn_l = plt.addLine(y=0, pen=pg.mkPen("#f5c518", width=1, style=QtCore.Qt.PenStyle.DashLine))
        lay.addWidget(plt, 1)
        info = QtWidgets.QLabel("—")
        info.setStyleSheet(f"color:{TXT};font-size:13px;font-weight:700;background:{PANEL2};"
                           f"border:1px solid {BORDER};border-radius:8px;padding:8px 12px;")
        lay.addWidget(info)

        def redraw(*_):
            """Met à jour SEULEMENT les données + bandes + info. NE TOUCHE PAS à la vue
            (zoom/déplacement) -> l'utilisateur reste maître de la souris."""
            buf = sorted(self._zs_buf, key=lambda x: x.get("t", 0))
            if len(buf) < 3:
                return
            ts = [x.get("t", 0) for x in buf]                 # temps ABSOLU (secondes)
            vals = [x.get(metric, 0.0) for x in buf]
            curve.setData(ts, vals)
            # moyenne / ±2σ calculées sur ce qui est VISIBLE à l'écran (s'adapte au zoom)
            (x0, x1), _ = plt.getViewBox().viewRange()
            vis = [v for t, v in zip(ts, vals) if x0 <= t <= x1]
            sub = np.asarray(vis if len(vis) >= 3 else vals, dtype=float)
            m, sd = float(sub.mean()), float(sub.std())
            mean_l.setValue(m); up_l.setValue(m + 2 * sd); dn_l.setValue(m - 2 * sd)
            z = (vals[-1] - m) / sd if sd > 1e-9 else 0.0
            zc = RED if abs(z) >= 3 else (AMBER if abs(z) >= 2 else TXT)
            info.setText(f"Actuel : {vals[-1]:.2f}   ·   moyenne {m:.2f}   ·   "
                         f"±2σ [{m - 2 * sd:.2f} ; {m + 2 * sd:.2f}]   ·   "
                         f"<span style='color:{zc};'>z = {z:+.1f}</span>   ·   "
                         f"{len(vis)} points visibles")

        def jump_window(*_):
            """Le combo est un RACCOURCI : il cadre sur la fenêtre choisie, puis tu
            reprends la main à la souris (zoom molette, glisser gauche/droite)."""
            win = self.ZS_WINDOWS.get(combo.currentText(), 180)
            now = _t_now()
            if win is None:
                plt.enableAutoRange(axis="x")
            else:
                plt.setXRange(now - win * 60, now, padding=0.02)
            plt.enableAutoRange(axis="y")
            redraw()

        import time as _time_mod
        def _t_now():
            b = self._zs_buf
            return b[-1].get("t", _time_mod.time()) if b else _time_mod.time()

        combo.currentIndexChanged.connect(jump_window)
        # bouton pour recadrer si on s'est perdu dans le zoom
        reset = QtWidgets.QPushButton("⟲ Recadrer")
        reset.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
        reset.setStyleSheet(
            f"QPushButton{{background:{PANEL};color:{TXT};border:1px solid {BORDER};"
            f"border-radius:8px;padding:6px 12px;font-weight:700;}}"
            f"QPushButton:hover{{border:1px solid {ACCENT};color:{ACCENT};}}")
        reset.clicked.connect(jump_window)
        top.addWidget(reset)

        timer = QtCore.QTimer(dlg); timer.timeout.connect(redraw); timer.start(2000)
        jump_window()                              # cadrage initial, puis souris libre
        self._zs_detail_dlgs.append(dlg)           # garde une référence (non-modal)
        dlg.finished.connect(lambda *_: self._zs_detail_dlgs.remove(dlg)
                             if dlg in self._zs_detail_dlgs else None)
        dlg.show()

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

        # ---- calendrier économique US (CPI / PPI / FOMC / NFP) ----
        outer.addWidget(self._h("📅 CALENDRIER ÉCONOMIQUE US  ·  les rendez-vous qui font bouger le BTC (heure de Paris)"))
        self.econ_browser = QtWidgets.QTextBrowser()
        self.econ_browser.setFixedHeight(148)
        self.econ_browser.setStyleSheet(
            f"QTextBrowser{{background:{PANEL};border:1px solid {BORDER};"
            f"border-radius:12px;color:{TXT};font-size:13px;padding:10px;}}")
        outer.addWidget(self.econ_browser)

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

    def _econ_render(self):
        """Affiche les prochains CPI/PPI/FOMC/NFP en heure locale + compte à rebours."""
        import datetime
        events = self.newsfeed.get_econ() if hasattr(self, "newsfeed") else []
        if not events:
            self._hset(self.econ_browser,
                       f"<span style='color:{DIM};'>Chargement du calendrier économique…</span>")
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = []
        for e in events:
            try:
                dt = datetime.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                continue
            delta = (dt - now).total_seconds()
            if delta < -3 * 3600:          # passé depuis >3h -> on n'affiche plus
                continue
            local = dt.astimezone()        # heure locale (Paris)
            when = local.strftime("%a %d %b · %Hh%M")
            if delta < 0:
                cd, cdc = "EN COURS / publié", AMBER
            elif delta < 3600:
                cd, cdc = f"dans {delta/60:.0f} min", RED
            elif delta < 86400:
                cd, cdc = f"dans {delta/3600:.0f} h", RED
            elif delta < 2 * 86400:
                cd, cdc = "demain", AMBER
            else:
                cd, cdc = f"dans {delta/86400:.0f} j", DIM
            imp = e.get("importance", 0)
            dot = "🔴" if imp >= 1 else "🟠"
            prev = e.get("previous"); fcst = e.get("forecast")
            extra = []
            if fcst not in (None, ""):
                extra.append(f"prévu {fcst}")
            if prev not in (None, ""):
                extra.append(f"préc. {prev}")
            extra_html = (f"  <span style='color:{DIM};font-size:11px;'>"
                          f"({' · '.join(extra)})</span>") if extra else ""
            rows.append(
                f"<div style='margin-bottom:7px;'>{dot} "
                f"<span style='color:{TXT};font-weight:800;'>{e['label']}</span>"
                f"  <span style='color:{DIM};'>{when}</span>"
                f"  <span style='color:{cdc};font-weight:800;'>· {cd}</span>{extra_html}</div>")
            if len(rows) >= 6:
                break
        self._hset(self.econ_browser, "".join(rows) if rows else
                   f"<span style='color:{DIM};'>Aucun événement macro majeur à venir.</span>")

    def _refresh_news(self):
        self._econ_render()
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
        mtf = self.engine.multi_tf_text()          # analyse multi-timeframe 5min→3h
        if mtf:
            L.append(mtf)
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
            p = data_file("mes_niveaux.txt")
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
        d = app_dir()
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
            self._zs_save()          # sauvegarde finale de l'historique des métriques
        except Exception:
            pass
        try:
            self.quant.stop()
        except Exception:
            pass
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


class FootprintItem(pg.GraphicsObject):
    """Rendu FOOTPRINT professionnel : cellules vente×achat par prix, imbalances 3:1
    surlignées, POC encadré, bougie OHLC par-dessus, delta+volume sous chaque barre.
    Tout est peint en UNE passe (rapide) avec du texte en pixels (net à tout zoom)."""

    def __init__(self):
        super().__init__()
        self._bars = []
        self._bucket = 20.0
        self._mode = "delta"          # "delta" (net) ou "split" (vente × achat)
        self._signals = []            # [{'i': idx bougie, 't': type}]
        self._br = QtCore.QRectF(0, 0, 1, 1)

    def setFPData(self, bars, bucket, y_lo, y_hi, mode="delta", signals=None):
        self._bars = bars
        self._bucket = bucket
        self._mode = mode
        self._signals = signals or []
        self._br = QtCore.QRectF(-0.6, y_lo, len(bars) + 1.2, max(1e-6, y_hi - y_lo))
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        return self._br

    def paint(self, p, *args):
        bars, bucket = self._bars, self._bucket
        if not bars:
            return
        tr = p.transform()
        p.save()
        p.resetTransform()
        font = QtGui.QFont("Consolas", 8)
        font.setStyleHint(QtGui.QFont.StyleHint.Monospace)   # fallback monospace auto
        hfont = QtGui.QFont("Consolas", 8, QtGui.QFont.Weight.Bold)
        hfont.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        p.setFont(font)
        AL = QtCore.Qt.AlignmentFlag
        NB = QtCore.Qt.BrushStyle.NoBrush

        def mrect(x, y, w, h):
            return tr.mapRect(QtCore.QRectF(x, y, w, h)).normalized()

        GREENc = QtGui.QColor("#2ecc71"); REDc = QtGui.QColor("#e74c3c")
        POCC = QtGui.QColor("#f5c518"); WHITE = QtGui.QColor("#eef2f7")

        def fq(v):
            return f"{v:.0f}" if abs(v) >= 100 else (f"{v:.1f}" if abs(v) >= 1 else f"{v:.2f}")

        for i, bar in enumerate(bars):
            cells = bar["cells"]
            if not cells:
                continue
            mx = max(c[0] + c[1] for c in cells.values()) or 1.0
            poc = max(cells, key=lambda k: cells[k][0] + cells[k][1])
            up = bar["c"] >= bar["o"]
            # LA BOUGIE EST LE FOOTPRINT : colonne teintée bleu (haussière) / rouge (baissière)
            tint = (36, 74, 118) if up else (120, 46, 56)
            barcol = (58, 120, 195) if up else (205, 76, 88)

            for lvl, (bv, sv) in cells.items():
                r = mrect(i - 0.47, lvl - bucket / 2, 0.94, bucket)
                tot = bv + sv
                d = bv - sv
                # fond de cellule teinté direction, intensité ∝ volume au niveau
                p.fillRect(r, QtGui.QColor(tint[0], tint[1], tint[2],
                                           int(45 + 120 * min(1.0, tot / mx))))
                # barre de volume horizontale derrière (même teinte, plus vive)
                wbar = (r.width() - 2) * min(1.0, tot / mx)
                p.fillRect(QtCore.QRectF(r.left() + 1, r.top() + 1, wbar, r.height() - 2),
                           QtGui.QColor(barcol[0], barcol[1], barcol[2], 140))
                # imbalance 3:1 -> cellule encadrée (vert=achat / rouge=vente)
                imb_b = bv >= 3 * sv and bv >= 0.4
                imb_s = sv >= 3 * bv and sv >= 0.4
                if imb_b or imb_s:
                    p.setPen(QtGui.QPen(GREENc if imb_b else REDc, 1.6)); p.setBrush(NB)
                    p.drawRect(r.adjusted(1, 1, -1, -1))
                if lvl == poc:
                    p.setPen(QtGui.QPen(POCC, 1.5)); p.setBrush(NB)
                    p.drawRect(r)
                if r.height() >= 11 and r.width() >= 60:
                    if self._mode == "delta":
                        p.setPen(WHITE)
                        p.drawText(r.adjusted(6, 0, -6, 0), int(AL.AlignLeft | AL.AlignVCenter),
                                   f"{d:+.0f}")
                    else:
                        p.setPen(WHITE)
                        p.drawText(r.adjusted(5, 0, -32, 0), int(AL.AlignLeft | AL.AlignVCenter),
                                   f"{fq(sv)} × {fq(bv)}")
                        p.setPen(GREENc if d >= 0 else REDc)
                        p.drawText(r.adjusted(0, 0, -5, 0), int(AL.AlignRight | AL.AlignVCenter),
                                   f"{d:+.0f}")

            # ENTÊTE de colonne (au-dessus) : volume total + delta de la bougie
            vol = bar["buy"] + bar["sell"]; delta = bar["buy"] - bar["sell"]
            p.setFont(hfont)
            p.setPen(QtGui.QColor("#c9d3df"))
            p.drawText(mrect(i - 0.5, bar["h"] + 1.6 * bucket, 1.0, 1.3 * bucket),
                       int(AL.AlignCenter), f"{vol:.0f}")
            p.setPen(GREENc if delta >= 0 else REDc)
            p.drawText(mrect(i - 0.5, bar["h"] + 0.35 * bucket, 1.0, 1.2 * bucket),
                       int(AL.AlignCenter), f"{delta:+.0f}")
            p.setFont(font)

        # ---- LIGNE DE PROGRESSION DU PRIX (relie les clôtures des colonnes) ----
        pts = [tr.map(QtCore.QPointF(float(i), float(b["c"]))) for i, b in enumerate(bars)]
        p.setPen(QtGui.QPen(QtGui.QColor(230, 236, 245, 170), 1.4))
        for a, b2 in zip(pts, pts[1:]):
            p.drawLine(a, b2)

        # ---- MARQUEURS DE SIGNAUX (divergence delta / absorption) ----
        CENTER = int(AL.AlignCenter)
        for sig in self._signals:
            i = sig.get("i", -1)
            if not (0 <= i < len(bars)):
                continue
            bar = bars[i]; t = sig.get("t")
            if t in ("div_bear", "abs_bear"):
                p.setPen(QtGui.QColor("#ff9f1a") if t == "div_bear" else QtGui.QColor("#00e5ff"))
                p.drawText(mrect(i - 0.5, bar["h"] + 3.0 * bucket, 1.0, bucket), CENTER,
                           "DIV" if t == "div_bear" else "ABS")
            else:
                p.setPen(QtGui.QColor("#ff9f1a") if t == "div_bull" else QtGui.QColor("#00e5ff"))
                p.drawText(mrect(i - 0.5, bar["l"] - 1.7 * bucket, 1.0, bucket), CENTER,
                           "DIV" if t == "div_bull" else "ABS")
        p.restore()


def main():
    app=QtWidgets.QApplication(sys.argv)
    app.setStyleSheet("QWidget{font-family:'Segoe UI','SF Pro Display',sans-serif;}"
                      "QToolTip{background:#10151e;color:#d8e0ea;border:1px solid #2a3543;padding:6px;}")
    win=Cockpit(); win.show(); sys.exit(app.exec())


if __name__=="__main__":
    main()
