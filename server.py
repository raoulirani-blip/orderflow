"""
server.py — Order Flow Cockpit en mode 24/7 SANS interface graphique.

Objectif : faire tourner, sur un petit serveur allumé en permanence (Oracle Cloud
Always Free), tout ce qui doit marcher MÊME PC ÉTEINT :
  - le moteur order flow (carnets Binance/OKX/Bybit/Hyperliquid en direct),
  - les ALERTES ntfy (approche de niveaux + gros murs + accélération volume),
  - le BOT TELEGRAM (tu poses des questions, le copilote Claude répond en live).

Il n'y a AUCUN PyQt ici : c'est du Python pur, déployable sur un serveur Linux.
Il réutilise exactement les mêmes modules que l'appli (engine, alerts, ai_copilot,
telegram_bot) et lit la même config alerts_config.json + claude_key.txt.

Lancement :  python server.py
"""

import json
import os
import threading
import time
from collections import deque

import requests

from engine import OrderFlowEngine
from alerts import Notifier
from ai_copilot import AICopilot
from telegram_bot import TelegramCopilotBot
import github_sync

HERE = os.path.dirname(os.path.abspath(__file__))

# fichier des niveaux partagé PC <-> serveur, via GitHub (lu en RAW, sans auth)
NIVEAUX_RAW = "https://raw.githubusercontent.com/raoulirani-blip/orderflow/main/niveaux.json"

# noms courts (Telegram) -> libellés modèles du copilote
MODEL_MAP = {
    "haiku": "Haiku 4.5 (éco)",
    "sonnet": "Sonnet 5 (équilibré)",
    "opus": "Opus 4.8 (max)",
}


def _cfg_path():
    return os.path.join(HERE, "alerts_config.json")


def _default_cfg():
    return {"enabled": True, "channel": "ntfy",
            "ntfy_topic": "", "tg_token": "", "tg_chat": "", "tg_bot_on": True,
            "model": "sonnet",
            "start_h": 0, "end_h": 24, "levels": [],
            "approach": 100.0, "live_interval": 45,
            "approach_on": True, "wall_on": True, "wall_min": 100.0,
            "accel_on": True, "accel_factor": 2.0}


class AlertServer:
    def __init__(self):
        self.cfg = self._load_cfg()
        self.state = {}
        self._alert_state = {}
        self._tape_ema = None
        self.engine = OrderFlowEngine(on_update=self._on_state)
        self.notifier = Notifier(min_interval=3.0)   # ntfy : pas de limite
        self._apply_notifier()
        self.copilot = AICopilot(daily_budget_usd=2.20)   # charge claude_key.txt seul
        # OPUS 4.8 en permanence (verrouillé) : on veut la meilleure analyse possible
        self.copilot.model_label = MODEL_MAP["opus"]
        # chaîne temporelle des métriques clés (pour analyser l'ÉVOLUTION sur 2-3h)
        self._hist = deque(maxlen=700)      # échantillon ~toutes les 20s -> ~3h50
        self._last_hist = 0.0
        self.tg = None
        self._start_telegram()
        # synchro des niveaux depuis l'appli PC (via GitHub) en tâche de fond
        self._last_niveaux_raw = None
        threading.Thread(target=self._poll_levels_loop, daemon=True).start()
        # AUTO-MISE À JOUR fiable : le serveur se surveille lui-même et se relance
        # tout seul quand du nouveau code arrive (pas de sudo/cron -> ne peut pas rater)
        threading.Thread(target=self._self_update_loop, daemon=True).start()
        # s'assure que matplotlib est présent (pour la commande /graph), sans bloquer
        threading.Thread(target=self._ensure_matplotlib, daemon=True).start()
        # HISTORIQUE 24/7 : échantillonne les métriques (agresseurs, CVD…) et PUBLIE
        # l'historique (murs + métriques + liquidations) sur GitHub pour l'appli locale
        self._zs_hist = deque(maxlen=4032)          # 14 j à 5 min
        self._zs_load()
        self._gh_token = github_sync.read_token(HERE)
        threading.Thread(target=self._zs_sample_loop, daemon=True).start()
        threading.Thread(target=self._publish_loop, daemon=True).start()

    # ---------- config ----------
    def _load_cfg(self):
        d = _default_cfg()
        p = _cfg_path()
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    d.update(json.load(f))
            except (OSError, ValueError):
                pass
        # niveaux : priorité au fichier niveaux.json (synchro depuis l'appli PC via GitHub)
        np = os.path.join(HERE, "niveaux.json")
        if os.path.exists(np):
            try:
                with open(np, encoding="utf-8") as f:
                    lv = [float(x) for x in json.load(f).get("levels", []) if float(x) > 100]
                if lv:
                    d["levels"] = sorted(set(lv))
            except (OSError, ValueError, TypeError):
                pass
        return d

    def _poll_levels_loop(self):
        """Récupère les niveaux posés depuis l'appli PC (fichier niveaux.json sur
        GitHub) toutes les 30 s, en RAW (aucune authentification). Met à jour les
        niveaux surveillés EN DIRECT, sans redémarrage."""
        while True:
            time.sleep(30)
            try:
                r = requests.get(NIVEAUX_RAW, timeout=15)
                if r.status_code != 200 or r.text == self._last_niveaux_raw:
                    continue
                self._last_niveaux_raw = r.text
                lv = [float(x) for x in json.loads(r.text).get("levels", []) if float(x) > 100]
                lv = sorted(set(lv))
                if lv != sorted(set(self.cfg.get("levels", []))):
                    self.cfg["levels"] = lv
                    self._save_cfg()
                    self._alert_state = {k: v for k, v in self._alert_state.items()
                                         if not k.startswith("lvl_")}
                    print(f"[NIVEAUX] synchro depuis l'appli PC : {lv}")
            except Exception:
                pass

    def _self_update_loop(self):
        """Vérifie GitHub toutes les 2 min. Si du CODE a changé, se met à jour et se
        relance tout seul (os._exit -> systemd Restart=always redémarre avec le neuf).
        Un simple changement de niveaux.json ne relance PAS (lu en direct ailleurs)."""
        import subprocess
        while True:
            time.sleep(120)
            try:
                subprocess.run(["git", "fetch", "origin", "--quiet"], cwd=HERE,
                               timeout=30, capture_output=True)
                before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE,
                                        capture_output=True, text=True).stdout.strip()
                after = subprocess.run(["git", "rev-parse", "origin/main"], cwd=HERE,
                                       capture_output=True, text=True).stdout.strip()
                if not before or before == after:
                    continue
                changed = subprocess.run(["git", "diff", "--name-only", before, after],
                                         cwd=HERE, capture_output=True, text=True).stdout
                subprocess.run(["git", "reset", "--hard", "origin/main", "--quiet"],
                               cwd=HERE, timeout=30, capture_output=True)
                _ignore = {"niveaux.json", "server_history.json"}
                code_changed = any(l.strip() and l.strip() not in _ignore
                                   for l in changed.splitlines())
                if code_changed:
                    print("[AUTO-UPDATE] nouveau code récupéré — redémarrage automatique")
                    os._exit(0)     # systemd relance immédiatement avec le nouveau code
            except Exception:
                pass

    def _ensure_autoupdate(self):
        """(Ré)écrit le script de mise à jour auto : il ne REDÉMARRE le serveur QUE si
        du CODE a changé — un simple changement de niveaux (niveaux.json) n'entraîne
        pas de redémarrage (les niveaux sont déjà lus en direct via GitHub)."""
        content = (
            "#!/bin/bash\n"
            "cd $HOME/orderflow\n"
            "git fetch origin --quiet 2>/dev/null\n"
            "before=$(git rev-parse HEAD 2>/dev/null)\n"
            "after=$(git rev-parse origin/main 2>/dev/null)\n"
            "if [ \"$before\" != \"$after\" ]; then\n"
            "  changed=$(git diff --name-only \"$before\" \"$after\" 2>/dev/null)\n"
            "  git reset --hard origin/main --quiet\n"
            "  if [ -n \"$(echo \"$changed\" | grep -vE '^(niveaux|server_history).json$')\" ]; then\n"
            "    sudo systemctl restart orderflow\n"
            "  fi\n"
            "fi\n"
        )
        try:
            p = os.path.join(HERE, "autoupdate.sh")
            with open(p, "w") as f:
                f.write(content)
            os.chmod(p, 0o755)
        except OSError:
            pass

    def _save_cfg(self):
        try:
            with open(_cfg_path(), "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _apply_notifier(self):
        self.notifier.configure(backend="ntfy", ntfy_topic=self.cfg.get("ntfy_topic", ""))

    def _on_state(self, s):
        self.state = s

    # ---------- Telegram ----------
    def _start_telegram(self):
        if self.cfg.get("tg_bot_on") and self.cfg.get("tg_token"):
            self.tg = TelegramCopilotBot(
                self.cfg.get("tg_token", ""), self.cfg.get("tg_chat", ""),
                self._telegram_question, self._telegram_learn_chat)
            self.tg.start()
            self.tg.set_commands([
                {"command": "graph", "description": "Graphique : prix + murs + CVD + flux institutionnel"},
                {"command": "live", "description": "Toutes les données du marché en direct"},
                {"command": "status", "description": "État du serveur + tes niveaux surveillés"},
                {"command": "niveaux", "description": "Définir tes niveaux (ex: 61000, 63000)"},
                {"command": "proximite", "description": "Distance d'alerte en $ (ex: 100)"},
                {"command": "liquidations", "description": "Zones de liquidation enregistrées 24/7 (aimants)"},
                {"command": "murs", "description": "Niveaux forts enregistrés 24/7 (2 dernières semaines)"},
                {"command": "update", "description": "Mettre à jour le serveur maintenant"},
                {"command": "aide", "description": "Liste des commandes"},
            ])
            print("[Telegram] bot démarré.")

    def _telegram_learn_chat(self, chat_id):
        self.cfg["tg_chat"] = chat_id
        self._save_cfg()
        print(f"[Telegram] chat_id appris : {chat_id}")

    def _telegram_question(self, question):
        q = question.strip()
        low = q.lower()
        # --- commandes de pilotage ---
        if low in ("/status", "/statut", "status"):
            return self._cmd_status()
        if low.startswith("/niveau"):
            return self._cmd_niveaux(q)
        if low in ("/live", "/marche", "/data", "/direct"):
            return self._cmd_live()
        if low in ("/graph", "/graphique", "/chart", "/courbe"):
            return self._cmd_graph()
        if low.startswith("/model"):
            return self._cmd_modele(q)
        if low.startswith("/proxi") or low.startswith("/distance"):
            return self._cmd_proximite(q)
        if low in ("/update", "/maj"):
            return self._cmd_update()
        if low.startswith("/liq"):
            return self._cmd_liquidations()
        if low.startswith("/mur") or low.startswith("/niveauxforts") or low.startswith("/wall"):
            return self._cmd_murs_forts()
        if low in ("/aide", "/help", "/start", "aide"):
            return ("🤖 Copilote Order Flow — commandes :\n"
                    "/live — TOUTES les données en direct (prix, CVD, murs, VWAP, POC…)\n"
                    "/status — état du serveur + tes niveaux\n"
                    "/niveaux 61000, 62000 — définir tes niveaux surveillés\n"
                    "/proximite 100 — distance d'alerte (à combien de $ ça te prévient)\n"
                    "/liquidations — zones de liquidation enregistrées 24/7 (aimants)\n"
                    "/murs — niveaux forts (murs) enregistrés 24/7 sur les 2 dernières semaines\n"
                    "/update — récupérer la dernière version du code\n"
                    "\n…ou pose une question libre (« je short ici ? ») → le copilote analyse.")
        # --- sinon : question libre au copilote ---
        ok, text = self.copilot.chat_sync(q, self._snapshot_text())
        return text if ok else f"⚠ {text}"

    def _cmd_status(self):
        s = self.state or {}
        mid = s.get("mid")
        oks = sum(1 for v in self.engine.agg.status.values() if v == "ok")
        lv = ", ".join(f"{x:.0f}" for x in self.cfg.get("levels", [])) or "(aucun)"
        prix = f"{mid:,.0f}$" if mid else "en synchro…"
        return (f"🟢 Serveur actif\n"
                f"Prix : {prix}\n"
                f"Venues : {oks}/4 OK\n"
                f"Niveaux surveillés : {lv}\n"
                f"Fenêtre : {self.cfg.get('start_h')}h–{self.cfg.get('end_h')}h\n"
                f"Alertes : {'ON' if self.cfg.get('enabled') else 'OFF'}")

    def _cmd_murs_forts(self):
        """Niveaux forts (murs significatifs) enregistrés 24/7 sur les 2 dernières
        semaines — regroupés par zone. Le carnet n'a pas d'historique public : c'est le
        serveur qui les accumule en continu, même quand le PC est éteint."""
        s = self.state or {}
        mid = s.get("mid")
        rep = self.engine.wall_history.report(20160, mid=mid, top_n=40,
                                              max_dist=None, cluster=25.0)
        if not rep.get("ready"):
            return ("🧱 NIVEAUX FORTS (2 semaines)\n\nPas encore de niveau enregistré — "
                    "le serveur accumule au fil du temps (reviens dans quelques jours).")
        # les plus tenaces : d'abord testés/tenus, puis les plus proches du prix
        walls = [w for w in rep["top"] if w.get("tests", 0) >= 1 or w.get("iceberg")]
        walls.sort(key=lambda w: (-(w.get("tests", 0)),
                                  abs(w["price"] - mid) if mid else 0))
        lines = ["🧱 NIVEAUX FORTS — 2 semaines (enregistrés 24/7)"]
        if mid:
            lines.append(f"Prix : {mid:,.0f}$")
        lines.append("")
        for w in walls[:12]:
            cote = "support" if w["side"] == "bid" else "résistance"
            dist = f" ({w['price'] - mid:+.0f}$)" if mid else ""
            ice = " 🧊ICEBERG" if w.get("iceberg") else ""
            etat = {"valide": "✅ a tenu", "invalide": "🔴 cassé",
                    "actif": "🟢 actif"}.get(w.get("status"), "")
            lines.append(f"  {w['price']:,.0f}${dist} · {cote} · testé "
                         f"{w.get('tests', 0)}× · {w['max_qty']:.0f} BTC {etat}{ice}")
        if len(lines) <= 3:
            return ("🧱 NIVEAUX FORTS (2 semaines)\n\nAucun niveau testé plusieurs fois "
                    "pour l'instant — le serveur continue d'accumuler.")
        return "\n".join(lines)

    def _cmd_liquidations(self):
        """Zones de liquidation RÉELLES enregistrées en continu (24/7, persistées sur
        disque -> survivent aux redémarrages) + niveaux-aimants estimés par levier."""
        s = self.state or {}
        mid = s.get("mid")
        clusters = self.engine.get_liq_clusters(window_s=24 * 3600, bucket=50.0)
        lines = ["💥 LIQUIDATIONS (enregistrées 24/7)"]
        if mid:
            lines.append(f"Prix : {mid:,.0f}$")
        if clusters:
            lines.append("\nZones où le plus de positions ont sauté :")
            for c in clusters[:8]:
                dom = "LONGS" if c["long"] >= c["short"] else "SHORTS"
                dist = f" ({c['price'] - mid:+.0f}$)" if mid else ""
                lines.append(f"  {c['price']:,.0f}${dist} · {c['total']/1e6:.1f}M$ ({dom})")
        else:
            lines.append("\n(aucune liquidation enregistrée pour l'instant)")
        levs = self.engine.leverage_liq_levels(mid) if mid else []
        if levs:
            lines.append("\nAimants estimés par levier :")
            for lv in levs:
                lines.append(f"  {lv['lev']}x → longs {lv['long_liq']:,.0f}$ · "
                             f"shorts {lv['short_liq']:,.0f}$")
        return "\n".join(lines)

    def _cmd_niveaux(self, q):
        import re
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", q) if float(x) > 100]
        if not nums:
            cur = ", ".join(f"{x:.0f}" for x in self.cfg.get("levels", [])) or "(aucun)"
            return (f"Tes niveaux actuels : {cur}\n"
                    "Pour les changer : /niveaux 61000, 62000, 63500")
        self.cfg["levels"] = sorted(set(nums))
        self._save_cfg()
        # réinitialise l'état des zones d'approche pour les nouveaux niveaux
        self._alert_state = {k: v for k, v in self._alert_state.items()
                             if not k.startswith("lvl_")}
        return "✅ Niveaux mis à jour : " + ", ".join(f"{x:.0f}" for x in self.cfg["levels"])

    def _cmd_live(self):
        """Tableau de bord complet en direct (instantané, gratuit, sans IA)."""
        s = self.state or {}
        mid = s.get("mid")
        if not mid or s.get("warming"):
            return "🟡 En synchro… réessaie dans quelques secondes."
        import time as _t
        L = [f"📊 BTC EN DIRECT — {_t.strftime('%H:%M:%S')}",
             f"Prix {mid:,.0f}$ · spread {s.get('spread',0):.1f}$",
             f"Carnet {s.get('imbalance',0)*100:.0f}% achat · "
             f"agress 5s {s.get('aggressor_ratio',0)*100:.0f}% · tape {s.get('tape_speed',0):.0f}/s"]
        cvds = self.engine.get_cvd_windows()
        parts = [f"{m}m {cvds[m]['cvd']:+.0f}" for m in (1, 5, 15, 30)
                 if cvds.get(m, {}).get("ready")]
        if parts:
            L.append("CVD : " + " · ".join(parts))
        v = self.engine.get_vwap()
        if v:
            L.append(f"VWAP {v['vwap']:,.0f} ({v['dev_pct']:+.2f}%)")
        vp = self.engine.get_volume_profile(3600)
        if vp:
            L.append(f"POC {vp['poc']:,.0f} · VAH {vp['vah']:,.0f} · VAL {vp['val']:,.0f}")
        pos = self.engine.get_positioning()
        f = pos.get("funding"); oi = pos.get("oi")
        extra = []
        if f:
            extra.append(f"funding {f['rate_pct']:+.3f}%")
        if oi:
            extra.append(f"OI {oi['now']:,.0f} ({oi['chg_5m_pct']:+.1f}% 5m)")
        if extra:
            L.append(" · ".join(extra))
        if s.get("sess_hi") and s.get("sess_lo"):
            L.append(f"Session : haut {s['sess_hi']:,.0f} · bas {s['sess_lo']:,.0f}")
        ab = s.get("absorption")
        if ab:
            L.append(f"⚠️ absorption ({ab[0]})")
        rep = self.engine.wall_history.report(30, mid=mid, top_n=60, max_dist=None)
        if rep.get("ready"):
            solid = [w for w in rep["top"] if w["status"] in ("actif", "valide")]
            solid.sort(key=lambda w: abs(w["price"] - mid))
            if solid:
                L.append(f"\n🧱 Murs ({len(solid)} solides, du + proche) :")
                for w in solid[:12]:
                    ic = "🟢" if w["side"] == "bid" else "🔴"
                    side = "sup" if w["side"] == "bid" else "rés"
                    L.append(f"{ic} {side} {w['price']:,.0f} — {w['max_qty']:.0f} BTC "
                             f"({w['max_qty']*w['price']/1e6:.1f}M$) à {abs(w['price']-mid):.0f}$")
        my = self.cfg.get("levels", [])
        if my:
            lf = self.engine.get_levels_flow(my, tol=30, window_s=3600)
            L.append("\n🎯 Tes niveaux :")
            for p in sorted(my, key=lambda x: abs(x - mid)):
                b, sv = lf.get(p, (0, 0))
                car = "accumulé" if b >= sv else "distribué"
                L.append(f"{p:,.0f} (à {abs(p-mid):.0f}$) : {car} — achat {b:.0f} / vente {sv:.0f}")
        evo = self._evolution_text()          # mini-graphs d'évolution
        if evo:
            L.append(evo)
        return "\n".join(L)

    def _cmd_proximite(self, q):
        import re
        m = re.findall(r"\d+(?:\.\d+)?", q)
        if not m:
            return (f"Distance d'alerte actuelle : {self.cfg.get('approach', 100):.0f}$\n"
                    "Pour changer : /proximite 100  (= alerte quand le prix est à 100$ d'un niveau)")
        self.cfg["approach"] = float(m[0])
        self._save_cfg()
        return f"✅ Alerte quand le prix arrive à {self.cfg['approach']:.0f}$ (ou moins) d'un de tes niveaux."

    def _cmd_modele(self, q):
        # verrouillé sur Opus 4.8 (le plus puissant) — on veut la meilleure analyse
        self.copilot.model_label = MODEL_MAP["opus"]
        return "🔒 Le copilote utilise Opus 4.8 (le modèle le plus puissant) en permanence."

    def _ensure_matplotlib(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            import subprocess
            import sys
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "matplotlib"],
                               timeout=300, capture_output=True)
                print("[graph] matplotlib installé")
            except Exception:
                pass

    def _build_chart_png(self):
        """Vrai graphique (PNG) : Prix + murs + VWAP · CVD 15m · Flux institutionnel."""
        import matplotlib
        matplotlib.use("Agg")
        import io
        import matplotlib.pyplot as plt
        import numpy as np
        h = list(self._hist)
        if len(h) < 5:
            return None
        now = time.time()
        x = np.array([(p["t"] - now) / 60.0 for p in h])       # min (0 = maintenant)
        price = [p["price"] for p in h]
        cvd15 = np.array([p.get("cvd15", 0) for p in h], dtype=float)
        inst = np.array([p.get("inst", 0) for p in h], dtype=float)
        s = self.state or {}
        mid = s.get("mid", price[-1])
        span = (h[-1]["t"] - h[0]["t"]) / 60

        fig, (a1, a2, a3) = plt.subplots(
            3, 1, figsize=(8.5, 8.6), sharex=True, facecolor="#0d1117",
            gridspec_kw={"height_ratios": [3, 2, 2], "hspace": 0.12})
        for ax in (a1, a2, a3):
            ax.set_facecolor("#0d1117"); ax.grid(alpha=0.12)
            ax.tick_params(colors="#8a94a6", labelsize=8)
            for sp in ax.spines.values():
                sp.set_color("#33404f")

        # PRIX + murs proches + VWAP + tes niveaux
        a1.plot(x, price, color="#4da3ff", lw=1.8, zorder=3)
        for w in sorted(s.get("walls", []), key=lambda w: abs(w["price"] - mid))[:6]:
            c = "#3ddc84" if w["side"] == "bid" else "#ff5c5c"
            a1.axhline(w["price"], color=c, lw=0.9, alpha=0.5)
            a1.text(x[0], w["price"], f" {w['price']:,.0f} ({w['qty']:.0f}BTC)",
                    color=c, fontsize=7, va="center")
        vw = self.engine.get_vwap()
        if vw:
            a1.axhline(vw["vwap"], color="#f5c518", lw=1.0, ls="--", alpha=0.8)
        for lv in self.cfg.get("levels", []):
            a1.axhline(lv, color="#00e5ff", lw=1.1, ls=":", alpha=0.9)
        a1.set_ylabel("Prix $", color="#aab4c0", fontsize=9)
        a1.set_title(f"BTC {mid:,.0f}$   ·   {time.strftime('%H:%M')}   ·   {span:.0f} min",
                     color="#e8eef5", fontsize=13, fontweight="bold")

        # CVD 15m
        a2.plot(x, cvd15, color="#f5c518", lw=1.4)
        a2.fill_between(x, cvd15, 0, where=cvd15 >= 0, color="#3ddc84", alpha=0.25)
        a2.fill_between(x, cvd15, 0, where=cvd15 < 0, color="#ff5c5c", alpha=0.25)
        a2.axhline(0, color="#555", lw=0.6)
        a2.set_ylabel("CVD 15m", color="#aab4c0", fontsize=9)

        # FLUX INSTITUTIONNEL
        a3.plot(x, inst, color="#c48bff", lw=1.4)
        a3.fill_between(x, inst, 0, where=inst >= 0, color="#3ddc84", alpha=0.3)
        a3.fill_between(x, inst, 0, where=inst < 0, color="#ff5c5c", alpha=0.3)
        a3.axhline(0, color="#555", lw=0.6)
        a3.set_ylabel("Flux instit Δ5m", color="#aab4c0", fontsize=9)
        a3.set_xlabel("minutes (0 = maintenant)", color="#aab4c0", fontsize=9)

        fig.subplots_adjust(left=0.10, right=0.97, top=0.92, bottom=0.07)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, facecolor="#0d1117")
        plt.close(fig)
        return buf.getvalue()

    def _cmd_graph(self):
        try:
            png = self._build_chart_png()
        except ImportError:
            return ("Le module graphique s'installe (1re fois, ~1 min) — "
                    "réessaie /graph dans une minute.")
        except Exception as e:
            return f"⚠ Souci génération graphique : {type(e).__name__}"
        if not png:
            return "Pas encore assez de données pour un graphique — laisse tourner 1-2 min."
        if self.tg:
            self.tg.send_photo(png, "📈 Prix + murs + VWAP · CVD 15m · Flux institutionnel")
            return ""
        return "Graphique généré (bot non connecté)."

    def _cmd_update(self):
        import subprocess
        try:
            subprocess.run(["git", "fetch", "origin", "--quiet"], cwd=HERE, timeout=30)
            before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE,
                                    capture_output=True, text=True).stdout.strip()
            after = subprocess.run(["git", "rev-parse", "origin/main"], cwd=HERE,
                                   capture_output=True, text=True).stdout.strip()
            if before == after:
                return "ℹ️ Déjà à jour (aucune nouvelle version)."
            subprocess.run(["git", "reset", "--hard", "origin/main", "--quiet"],
                           cwd=HERE, timeout=30)
            subprocess.Popen("sleep 3 && sudo systemctl restart orderflow", shell=True)
            return "✅ Nouvelle version récupérée. Redémarrage dans 3 s…"
        except Exception as e:
            return f"⚠ Échec de la mise à jour : {e}"

    # ---------- helpers marché ----------
    def _nearest_wall(self, price):
        best, bd = None, 400
        for w in self.state.get("walls", []):
            d = abs(w["price"] - price)
            if d <= bd:
                bd = d; best = w
        return best

    def _in_time_window(self):
        h = time.localtime().tm_hour
        a, b = int(self.cfg.get("start_h", 0)), int(self.cfg.get("end_h", 24))
        if a == b:
            return True
        if a < b:
            return a <= h < b
        return h >= a or h < b

    def _confluence_text(self, lvl, mid, first):
        is_res = lvl > mid
        side = "résistance" if is_res else "support"
        d = abs(mid - lvl)
        c1 = self.engine.get_cvd_windows().get(1, {})
        cvd1 = c1.get("cvd", 0) if c1.get("ready") else 0
        agg = self.state.get("aggressor_ratio", 0.5) * 100
        wall = self._nearest_wall(lvl)
        wtxt = (f"{wall['price']:,.0f} ({wall['qty']:.0f}BTC·{wall['qty']*wall['price']/1e6:.1f}M$)"
                if wall else "aucun proche")
        vw = self.engine.get_vwap()
        vwtxt = ("prix>VWAP" if (vw and vw["above"]) else "prix<VWAP") if vw else "VWAP —"
        tape = self.state.get("tape_speed", 0)
        head = "⚡ APPROCHE" if first else "🔄 MAJ live"
        return (f"{head} {side} {lvl:,.0f}\nPrix {mid:,.0f} (à {d:.0f}$)\n"
                f"CVD 1m {cvd1:+.0f} · agress {agg:.0f}% achat\n"
                f"Mur proche : {wtxt}\n{vwtxt} · tape {tape:.0f}/s")

    def _sample_history(self):
        """Enregistre un point de la chaîne temporelle toutes les ~15s."""
        now = time.time()
        if now - self._last_hist < 20:
            return
        s = self.state or {}
        mid = s.get("mid")
        if not mid or s.get("warming"):
            return
        cvds = self.engine.get_cvd_windows()
        c1 = cvds.get(1, {}); c15 = cvds.get(15, {})
        seg = self.engine.get_flow_segments(300)
        self._hist.append({
            "t": now, "price": mid,
            "cvd1": c1.get("cvd", 0) if c1.get("ready") else 0,
            "cvd15": c15.get("cvd", 0) if c15.get("ready") else 0,
            "agg": s.get("aggressor_ratio", 0.5),
            "imb": s.get("imbalance", 0.5),
            "tape": s.get("tape_speed", 0.0),
            "inst": seg["inst"]["delta"] if seg else 0.0,   # flux INSTITUTIONNEL 5min
        })
        self._last_hist = now

    @staticmethod
    def _spark(vals):
        """Mini-graphe texte (sparkline) : ▁▂▃▄▅▆▇█ du plus ancien au plus récent."""
        vals = [v for v in vals if v is not None]
        if len(vals) < 2:
            return ""
        lo, hi = min(vals), max(vals)
        chars = "▁▂▃▄▅▆▇█"
        if hi == lo:
            return chars[3] * len(vals)
        return "".join(chars[int((v - lo) / (hi - lo) * 7)] for v in vals)

    def _series(self, key, n=28):
        """n points régulièrement espacés d'une métrique sur tout l'historique."""
        h = list(self._hist)
        if len(h) < 2:
            return []
        step = max(1, len(h) // n)
        return [x.get(key) for x in h[::step]]

    def _evolution_text(self):
        """MINI-GRAPHS d'évolution (sparklines) : TOUTES les métriques montrées comme
        des courbes dans le temps, pas des valeurs figées. Focus flux institutionnel."""
        if len(self._hist) < 4:
            return ""
        span = (time.time() - self._hist[0]["t"]) / 60
        L = [f"\nGRAPHS D'ÉVOLUTION (~{span:.0f} min · gauche=ancien → droite=maintenant) :"]

        def g(lbl, key, fmt):
            ser = self._series(key)
            ser = [v for v in ser if v is not None]
            if len(ser) < 2:
                return
            a, b = ser[0], ser[-1]
            trend = "↑" if b > a else ("↓" if b < a else "→")
            L.append(f"  {lbl:9s}{self._spark(ser)}  {fmt.format(a)}→{fmt.format(b)} {trend}")

        g("Prix", "price", "{:,.0f}")
        g("Instit", "inst", "{:+.0f}")          # LE flux institutionnel dans le temps
        g("CVD 1m", "cvd1", "{:+.0f}")
        g("CVD 15m", "cvd15", "{:+.0f}")
        g("Agress", "agg", "{:.0%}")
        g("Carnet", "imb", "{:.0%}")
        g("Tape/s", "tape", "{:.0f}")
        return "\n".join(L)

    def _snapshot_text(self):
        """Instantané compact pour le copilote (identique en esprit à l'appli)."""
        s = self.state or {}
        L = ["Données live BTC perp (agrégé Binance+OKX+Bybit+Hyperliquid). "
             "Ce snapshot contient TOUTES les données du logiciel : prix/spread/imbalance, agresseurs, "
             "tape, VWAP, CVD 1/5/15/30min, TOUS les murs (actifs/validés/cassés), le FIL des gros "
             "ordres un par un (>5 BTC), le flux retail/moyen/institutionnel sur 5/30/60min, les bilans "
             "par fenêtre, le volume profile 1h et 4h, les liquidations, les sweeps, le positionnement "
             "(OI, funding), et l'ÉVOLUTION dans le temps. Tu as accès à TOUT ça ici — ne dis JAMAIS "
             "que tu n'as pas accès à une donnée qui figure ci-dessous ; sers-t'en. "
             "PRIORITÉ AU FLUX INSTITUTIONNEL : dis toujours ce que font les GROS (delta institutionnel "
             "par fenêtre + fil des gros ordres) — ce sont eux qui comptent, pas le retail. "
             "IMPORTANT : lis les GRAPHS D'ÉVOLUTION (sparklines ▁▂▃▄▅▆▇█) et l'analyse MULTI-TIMEFRAME "
             "pour raisonner sur les TENDANCES et la dynamique, JAMAIS sur une valeur figée à l'instant."]
        mid = s.get("mid")
        if mid:
            L.append(f"prix={mid:.0f} spread={s.get('spread',0):.1f}$ "
                     f"carnet={s.get('imbalance',0)*100:.0f}%achat "
                     f"agresseurs5s={s.get('aggressor_ratio',0)*100:.0f}%achat "
                     f"tape={s.get('tape_speed',0):.1f}tr/s")
        v = self.engine.get_vwap()
        if v:
            L.append(f"VWAP={v['vwap']:.0f} ecart={v['dev_pct']:+.2f}%")
        cvds = self.engine.get_cvd_windows()
        parts = [f"{m}min:{d['cvd']:+.0f}" for m, d in cvds.items() if d.get("ready")]
        if parts:
            L.append("CVD(BTC) " + " ".join(parts))
        if mid:
            # TOUS les murs (aucune limite de distance) pour que le copilote ait accès
            # à chaque détail, du plus proche au plus loin
            rep = self.engine.wall_history.report(30, mid=mid, top_n=60, max_dist=None)
            if rep.get("ready"):
                solid = [wl for wl in rep["top"] if wl["status"] in ("actif", "valide")]
                solid.sort(key=lambda wl: abs(wl["price"] - mid))
                L.append(f"TOUS LES MURS solides ({len(solid)}), du + proche au + loin :")
                for wl in solid[:22]:
                    L.append(f"  mur {'ACHAT' if wl['side']=='bid' else 'VENTE'} @{wl['price']:.0f} "
                             f"{wl['max_qty']:.0f}BTC (~{wl['max_qty']*wl['price']/1e6:.1f}M$) "
                             f"dist={abs(wl['price']-mid):.0f}$ testé{wl['tests']}x {wl['status']}")
                inval = [wl for wl in rep["top"] if wl["status"] == "invalide"][:5]
                if inval:
                    L.append("Murs récemment CASSÉS/invalidés (contexte) : " +
                             ", ".join(f"{wl['price']:.0f}" for wl in inval))
        # FLUX retail/moyen/instit sur plusieurs fenêtres (page INSTITUTIONNELS)
        for win_s, lbl in ((300, "5min"), (1800, "30min"), (3600, "60min")):
            seg = self.engine.get_flow_segments(win_s)
            if seg:
                L.append(f"delta{lbl} retail={seg['retail']['delta']:+.1f} "
                         f"moyen={seg['mid']['delta']:+.1f} instit={seg['inst']['delta']:+.1f}BTC "
                         f"(instit: achat {seg['inst']['buy']:.1f} / vente {seg['inst']['sell']:.1f})")
        # FIL DES GROS ORDRES un par un (>5 BTC), le + récent d'abord (page INSTITUTIONNELS)
        seg5 = self.engine.get_flow_segments(300)
        if seg5 and seg5.get("big_prints"):
            L.append("GROS ORDRES récents (heure · côté · prix · taille) :")
            for bp in seg5["big_prints"][:15]:
                L.append(f"  {bp['ts']} {bp['side']} @{bp['price']:.0f} {bp['qty']:.2f}BTC "
                         f"(~{bp['usd']/1e6:.2f}M$)")
        elif seg5 is not None:
            L.append("Gros ordres (>5 BTC) : aucun sur 5min (rien côté gros pour l'instant).")
        # ANALYSE MULTI-TIMEFRAME (5min → 3h) : évolution réelle à chaque horizon
        mtf = self.engine.multi_tf_text()
        if mtf:
            L.append(mtf)
        # VOLUME PROFILE multi-fenêtres
        for win_s, lbl in ((3600, "1h"), (14400, "4h")):
            vp = self.engine.get_volume_profile(win_s)
            if vp:
                L.append(f"volume profile {lbl}: POC={vp['poc']:.0f} VAH={vp['vah']:.0f} VAL={vp['val']:.0f}")
        # sweeps en cascade récents
        casc = self.engine.get_cascade_sweeps(180)
        if casc:
            c0 = casc[0]
            L.append(f"dernier SWEEP cascade: {c0['side']} {c0['qty']:.1f}BTC sur {c0['levels']} "
                     f"niveaux ({c0['lo']:.0f}-{c0['hi']:.0f}) à {c0['ts']}")
        pos = self.engine.get_positioning()
        f = pos.get("funding"); oi = pos.get("oi")
        if f:
            L.append(f"funding={f['rate_pct']:+.4f}% (annualisé {f.get('annual_pct',0):+.1f}%)")
        if oi:
            L.append(f"OI={oi['now']:.0f}BTC 5min={oi['chg_5m_pct']:+.2f}% 15min={oi['chg_15m_pct']:+.2f}%")
        # liquidations (page POSITIONNEMENT)
        lq = pos.get("liq_5m", {})
        if lq.get("long_usd", 0) + lq.get("short_usd", 0) > 0:
            L.append(f"liquidations 5min: longs {lq['long_usd']/1e6:.2f}M$ / "
                     f"shorts {lq['short_usd']/1e6:.2f}M$ ({lq.get('n',0)} events)")
        for liq in pos.get("liqs", [])[:4]:
            L.append(f"  liq {liq['side']} @{liq['price']:.0f} {liq['qty']:.3f}BTC à {liq['ts']}")
        ab = s.get("absorption")
        if ab:
            L.append(f"ABSORPTION ({ab[0]}): {ab[1][:90]}")
        if s.get("sess_hi") and s.get("sess_lo"):
            L.append(f"session: high={s['sess_hi']:.0f} low={s['sess_lo']:.0f}")
        my = self.cfg.get("levels", [])
        if my and mid:
            lf = self.engine.get_levels_flow(my, tol=30, window_s=3600)
            for p in sorted(my, key=lambda x: abs(x - mid)):
                b, sv = lf.get(p, (0, 0))
                car = "accumulé" if b >= sv else "distribué"
                L.append(f"MON NIVEAU {p:.0f} (dist {abs(p-mid):.0f}$): "
                         f"achat {b:.0f} vs vente {sv:.0f} BTC = {car}")
        evo = self._evolution_text()
        if evo:
            L.append(evo)
        return "\n".join(L)

    # ---------- moteur d'alertes ----------
    def _alerts_tick(self):
        cfg = self.cfg
        if not cfg.get("enabled"):
            return
        s = self.state or {}
        mid = s.get("mid")
        tape = s.get("tape_speed", 0.0)
        if mid and not s.get("warming"):
            self._tape_ema = tape if self._tape_ema is None else 0.98*self._tape_ema + 0.02*tape
        if not mid or s.get("warming") or not self._in_time_window():
            return
        now = time.time()

        # A. approche des niveaux + maj live
        if cfg.get("approach_on"):
            for lvl in cfg.get("levels", []):
                d = abs(mid - lvl)
                key = f"lvl_{lvl}"
                st = self._alert_state.get(key, {"in": False, "last": 0.0})
                if d <= cfg["approach"]:
                    if not st["in"]:
                        self.notifier.send(self._confluence_text(lvl, mid, first=True))
                        print(f"[ALERTE] approche {lvl}")
                        st = {"in": True, "last": now}
                    elif now - st["last"] >= cfg.get("live_interval", 45):
                        self.notifier.send(self._confluence_text(lvl, mid, first=False))
                        st["last"] = now
                elif d > cfg["approach"] * 1.6:
                    st["in"] = False
                self._alert_state[key] = st

        # B. gros mur près du prix
        if cfg.get("wall_on"):
            for w in s.get("walls", []):
                if w["qty"] >= cfg.get("wall_min", 100) and abs(w["price"]-mid) <= cfg["approach"]*2:
                    key = f"wall_{round(w['price'])}_{w['side']}"
                    if now - self._alert_state.get(key, 0) > 600:
                        sidetxt = "support" if w["side"] == "bid" else "résistance"
                        self.notifier.send(
                            f"🧱 Gros mur {sidetxt} @ {w['price']:,.0f} — {w['qty']:.0f} BTC "
                            f"({w['qty']*w['price']/1e6:.1f} M$), à {abs(w['price']-mid):.0f}$ du prix.")
                        print(f"[ALERTE] gros mur {w['price']}")
                        self._alert_state[key] = now

        # C. accélération du volume
        if cfg.get("accel_on") and self._tape_ema and self._tape_ema > 1:
            if tape >= cfg.get("accel_factor", 2.0) * self._tape_ema:
                if now - self._alert_state.get("accel", 0) > 300:
                    self.notifier.send(
                        f"⚡ Accélération : {tape:.0f} trades/s vs ~{self._tape_ema:.0f} "
                        f"d'habitude ({tape/self._tape_ema:.1f}x) @ {mid:,.0f}. Le marché s'active.")
                    print("[ALERTE] accélération")
                    self._alert_state["accel"] = now

    # ---------- historique 24/7 des métriques + publication GitHub ----------
    def _zs_path(self):
        return os.path.join(HERE, "zscore_history.json")

    def _zs_load(self):
        import time as _t
        try:
            with open(self._zs_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        cut = _t.time() - 14 * 24 * 3600
        for x in data:
            if isinstance(x, dict) and x.get("t", 0) >= cut:
                self._zs_hist.append(x)

    def _zs_save(self):
        try:
            with open(self._zs_path(), "w", encoding="utf-8") as f:
                json.dump(list(self._zs_hist), f, separators=(",", ":"))
        except OSError:
            pass

    def _zs_sample_loop(self):
        """Échantillonne les 9 métriques toutes les 5 min, 24/7 (même PC éteint)."""
        import time as _t
        last_save = 0.0
        while True:
            _t.sleep(300)
            s = self.state or {}
            if not s.get("mid") or s.get("warming"):
                continue
            try:
                cv = self.engine.get_cvd_windows()
                pos = self.engine.get_positioning()
                seg = self.engine.get_flow_segments(300)
                oi = pos.get("oi") or {}
                fd = pos.get("funding") or {}

                def cvd(m):
                    d = cv.get(m, {})
                    return d.get("cvd", 0.0) if d.get("ready") else 0.0
                self._zs_hist.append({
                    "t": _t.time(),
                    "CVD 1min": cvd(1), "CVD 5min": cvd(5), "CVD 15min": cvd(15),
                    "Agresseurs %": s.get("aggressor_ratio", 0.5) * 100,
                    "Tape (tr/s)": s.get("tape_speed", 0.0),
                    "Imbalance %": s.get("imbalance", 0.5) * 100,
                    "OI Δ5min %": oi.get("chg_5m_pct", 0.0),
                    "Funding %": fd.get("rate_pct", 0.0) or 0.0,
                    "Instit Δ5min": seg["inst"]["delta"] if seg else 0.0,
                })
                if _t.time() - last_save > 120:
                    last_save = _t.time()
                    self._zs_save()
            except Exception:
                pass

    def _publish_loop(self):
        """Publie l'historique complet sur GitHub toutes les 30 min (léger : < 1 Mo)."""
        import time as _t
        _t.sleep(60)                        # laisse le moteur se remplir un peu
        while True:
            if self._gh_token:
                try:
                    wh = self.engine.wall_history
                    walls = [wh._rec_to_dict(r) for _ts, r in list(wh.longterm)][-3000:]
                    with self.engine.agg._lock:
                        liqs = list(self.engine.agg.liqs)[-1500:]
                    data = {"saved_at": _t.time(),
                            "walls_longterm": walls,
                            "zscore": list(self._zs_hist)[-2016:],   # ~7 j à 5 min
                            "liquidations": liqs}
                    ok, msg = github_sync.publish(self._gh_token, data)
                    print(f"[SYNC] publication historique : {msg}")
                except Exception as e:
                    print(f"[SYNC] erreur publication : {e}")
            else:
                print("[SYNC] github_token.txt absent — publication désactivée. "
                      "Voir DEPLOY : créer un token GitHub pour activer la synchro.")
            for _ in range(1800):
                _t.sleep(1)

    def run(self):
        self.engine.start()
        print("=" * 60)
        print(" SERVEUR ORDER FLOW 24/7 DÉMARRÉ (sans interface)")
        print(f" ntfy topic : {self.cfg.get('ntfy_topic') or '(non configuré)'}")
        print(f" Telegram   : {'activé' if self.tg else 'désactivé'}")
        print(f" Fenêtre    : {self.cfg.get('start_h')}h-{self.cfg.get('end_h')}h  "
              f"· niveaux : {self.cfg.get('levels')}")
        print(" Ctrl+C pour arrêter.")
        print("=" * 60)
        last_beat = 0.0
        try:
            while True:
                time.sleep(2)
                try:
                    self._alerts_tick()
                    self._sample_history()
                except Exception as e:
                    print("[erreur alerte]", e)
                now = time.time()
                if now - last_beat > 120:
                    oks = sum(1 for v in self.engine.agg.status.values() if v == "ok")
                    mid = self.state.get("mid")
                    print(f"{time.strftime('%H:%M:%S')} vivant · venues {oks}/4 · "
                          f"prix {mid:.0f}" if mid else f"{time.strftime('%H:%M:%S')} en synchro…")
                    last_beat = now
        except KeyboardInterrupt:
            print("\nArrêt du serveur…")
            self.engine.stop()
            self.notifier.stop()
            if self.tg:
                self.tg.stop()


if __name__ == "__main__":
    AlertServer().run()
