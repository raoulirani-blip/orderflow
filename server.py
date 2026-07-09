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
        # chaîne temporelle des métriques clés (pour analyser l'ÉVOLUTION, pas l'instant)
        self._hist = deque(maxlen=300)      # échantillon ~toutes les 15s -> ~75 min
        self._last_hist = 0.0
        self.tg = None
        self._start_telegram()
        self._ensure_autoupdate()           # script de MAJ auto (auto-réparé)
        # synchro des niveaux depuis l'appli PC (via GitHub) en tâche de fond
        self._last_niveaux_raw = None
        threading.Thread(target=self._poll_levels_loop, daemon=True).start()

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
            "  if [ -n \"$(echo \"$changed\" | grep -v '^niveaux.json$')\" ]; then\n"
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
                {"command": "live", "description": "Toutes les données du marché en direct"},
                {"command": "status", "description": "État du serveur + tes niveaux surveillés"},
                {"command": "niveaux", "description": "Définir tes niveaux (ex: 61000, 63000)"},
                {"command": "proximite", "description": "Distance d'alerte en $ (ex: 100)"},
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
        if low.startswith("/model"):
            return self._cmd_modele(q)
        if low.startswith("/proxi") or low.startswith("/distance"):
            return self._cmd_proximite(q)
        if low in ("/update", "/maj"):
            return self._cmd_update()
        if low in ("/aide", "/help", "/start", "aide"):
            return ("🤖 Copilote Order Flow — commandes :\n"
                    "/live — TOUTES les données en direct (prix, CVD, murs, VWAP, POC…)\n"
                    "/status — état du serveur + tes niveaux\n"
                    "/niveaux 61000, 62000 — définir tes niveaux surveillés\n"
                    "/proximite 100 — distance d'alerte (à combien de $ ça te prévient)\n"
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
        rep = self.engine.wall_history.report(15, mid=mid, top_n=20, max_dist=400)
        if rep.get("ready"):
            solid = [w for w in rep["top"] if w["status"] in ("actif", "valide")]
            solid.sort(key=lambda w: abs(w["price"] - mid))
            if solid:
                L.append("\n🧱 Murs proches :")
                for w in solid[:5]:
                    ic = "🟢" if w["side"] == "bid" else "🔴"
                    side = "support" if w["side"] == "bid" else "résist."
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
        if now - self._last_hist < 15:
            return
        s = self.state or {}
        mid = s.get("mid")
        if not mid or s.get("warming"):
            return
        c1 = self.engine.get_cvd_windows().get(1, {})
        self._hist.append({
            "t": now, "price": mid,
            "cvd1": c1.get("cvd", 0) if c1.get("ready") else 0,
            "agg": s.get("aggressor_ratio", 0.5),
            "imb": s.get("imbalance", 0.5),
            "tape": s.get("tape_speed", 0.0),
        })
        self._last_hist = now

    def _evolution_text(self):
        """Chaîne temporelle lisible : comment prix/CVD/agresseurs ont ÉVOLUÉ.
        C'est ce qui permet à Opus d'analyser la dynamique, pas juste l'instant."""
        if len(self._hist) < 3:
            return ""
        now = time.time()

        def at(mins):
            target = now - mins * 60
            return min(self._hist, key=lambda h: abs(h["t"] - target))

        oldest_min = (now - self._hist[0]["t"]) / 60
        L = [f"\nÉVOLUTION sur ~{oldest_min:.0f} min (pour analyser la DYNAMIQUE, "
             "pas l'instant figé) :"]
        for mins in (30, 20, 15, 10, 5, 2, 0):
            if mins / 60 > 0 and mins > oldest_min + 1:
                continue
            h = self._hist[-1] if mins == 0 else at(mins)
            lbl = "maintenant" if mins == 0 else f"-{mins}min"
            L.append(f"  {lbl}: prix {h['price']:,.0f} · CVD1m {h['cvd1']:+.0f} · "
                     f"agress {h['agg']*100:.0f}% · carnet {h['imb']*100:.0f}%ach · "
                     f"tape {h['tape']:.0f}/s")
        # vitesse récente du prix
        p_now = self._hist[-1]["price"]; p_5 = at(5)["price"]
        dp = p_now - p_5
        L.append(f"  → sur 5 min le prix a {('MONTÉ' if dp>0 else 'BAISSÉ' if dp<0 else 'stagné')} "
                 f"de {abs(dp):,.0f}$")
        return "\n".join(L)

    def _snapshot_text(self):
        """Instantané compact pour le copilote (identique en esprit à l'appli)."""
        s = self.state or {}
        L = ["Données live BTC perp (agrégé Binance+OKX+Bybit+Hyperliquid). "
             "IMPORTANT : base ton analyse sur l'ÉVOLUTION/la dynamique (section en bas), "
             "pas seulement sur l'instant. Regarde comment le prix réagit aux niveaux/murs "
             "(vitesse, cassure ou rejet), la tendance du CVD et des agresseurs dans le temps."]
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
            rep = self.engine.wall_history.report(15, mid=mid, top_n=20, max_dist=400)
            if rep.get("ready"):
                solid = [wl for wl in rep["top"] if wl["status"] in ("actif", "valide")]
                solid.sort(key=lambda wl: abs(wl["price"] - mid))
                for wl in solid[:6]:
                    L.append(f"mur {'ACHAT' if wl['side']=='bid' else 'VENTE'} @{wl['price']:.0f} "
                             f"{wl['max_qty']:.0f}BTC dist={abs(wl['price']-mid):.0f}$ "
                             f"testé{wl['tests']}x statut={wl['status']}")
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
            L.append(f"OI={oi['now']:.0f}BTC 5min={oi['chg_5m_pct']:+.2f}%")
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
