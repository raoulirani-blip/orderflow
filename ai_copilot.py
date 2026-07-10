"""
ai_copilot.py — copilote IA (Claude) pour le cockpit order flow.

Sécurités intégrées :
  - BUDGET QUOTIDIEN STRICT : le coût réel de chaque appel est calculé depuis
    l'usage retourné par l'API et additionné dans ai_budget.json ; dès que la
    limite du jour est atteinte, plus AUCUN appel n'est envoyé jusqu'à minuit.
  - L'appel tourne dans un thread de fond : l'interface ne bloque jamais,
    même si la connexion est lente (timeout 45s, échec silencieux affiché).
  - Conçu pour connexion faible : ~2-3 KB par appel.

La clé API est lue depuis la variable d'environnement ANTHROPIC_API_KEY ou le
fichier claude_key.txt à côté de l'appli (rempli via l'onglet IA).
"""

import os
import json
import time
import threading

_DIR = os.path.dirname(os.path.abspath(__file__))
BUDGET_FILE = os.path.join(_DIR, "ai_budget.json")
KEY_FILE = os.path.join(_DIR, "claude_key.txt")

# label -> (model_id, prix entree $/MTok, prix sortie $/MTok)
MODELS = {
    "Haiku 4.5 (éco)":      ("claude-haiku-4-5", 1.0, 5.0),
    "Sonnet 5 (équilibré)": ("claude-sonnet-5", 3.0, 15.0),
    "Opus 4.8 (max)":       ("claude-opus-4-8", 5.0, 25.0),
}
DEFAULT_MODEL = "Haiku 4.5 (éco)"

SYSTEM_PROMPT = (
    "Tu es un copilote d'analyse order flow pour un trader intraday sur BTC perpétuel dont les "
    "trades durent généralement 1 à 4h (parfois plus court). On te donne un instantané des données "
    "live (carnet agrégé Binance+OKX+Bybit+Hyperliquid, CVD, VWAP, murs, positionnement, "
    "liquidations, news). Le contexte de fond (CVD 15/30min, OI, funding, VWAP/POC, session, news) "
    "porte la thèse ; le flux immédiat (agresseurs 5s, CVD 1min, tape, absorption, sweeps) affine "
    "l'entrée. Croise les deux. "
    "Réponds en français, concis et actionnable, exactement dans ce format:\n"
    "BIAIS: haussier/baissier/neutre + conviction sur 10\n"
    "LECTURE: 2-3 phrases qui croisent les signaux les plus importants (fond + immédiat). "
    "Si des signaux se contredisent, dis-le clairement.\n"
    "PROJECTION: ce qui est le plus probable sur les prochaines MINUTES (flux immédiat) ET les "
    "prochaines HEURES (biais de fond), avec scénarios conditionnels à seuils : 'si casse X → cible Y', "
    "'si rejet X → retour Z', et lequel tu privilégies.\n"
    "NIVEAUX: les 2-3 prix clés à surveiller + ce qui s'y joue\n"
    "PLAN: entrée si quoi, stop où, invalidation. Attends la confluence, ne force pas.\n"
    "Direct et concret. Pas de disclaimers. Tu projettes des scénarios probables (pas des certitudes), "
    "mais tu t'engages sur le plus probable. Le trader décide seul."
)

CHAT_PROMPT = """Tu es le copilote de trading personnel intégré à "Order Flow Cockpit", un \
logiciel d'analyse order flow / Level 2 sur BTC perpétuel. Tu es un EXPERT complet du \
trading order flow et tu connais ce logiciel par cœur. Tu parles à ton utilisateur comme \
un mentor de trading expérimenté : franc, concret, pédagogue, jamais langue de bois.

QUI EST L'UTILISATEUR : un trader intraday sur BTC perp qui apprend le Level 2. Ses trades \
durent GÉNÉRALEMENT DE 1 À 4 HEURES (parfois du scalp plus court selon l'occasion). Il trace ses \
propres niveaux support/résistance sur TradingView et les saisit dans le logiciel. Il trade \
surtout la session US (13h-16h heure locale). Il veut progresser, pas qu'on décide à sa place. \
Aide-le à la fois à ANALYSER ET à COMPRENDRE (vulgarise ce qu'il ne maîtrise pas encore, avec \
ses vraies valeurs live comme exemples).

CADRE POUR DES TRADES DE 1 À 4H :
- La THÈSE du trade s'appuie sur le contexte de fond : CVD 15/30min, OI×prix, funding, structure \
de session (high/low), VWAP/POC, Fear&Greed, news. Sur 1-4h, ça compte vraiment.
- Le TIMING d'entrée et de sortie s'affine avec le flux immédiat : agresseurs 5s, CVD 1min, tape, \
réaction au touch d'un mur, absorption, sweeps. C'est ça qui dit "entre maintenant" ou "attends".
- Croise les deux : le contexte pour la direction et la conviction, le flux immédiat pour l'exécution.
- Stops derrière les niveaux structurels, objectifs cohérents avec un hold de 1-4h. Rappelle la \
gestion du risque et de ne jamais laisser courir une perte.

PROJECTION / ANTICIPATION — TRÈS IMPORTANT (ce que l'utilisateur veut EN PLUS de l'instantané) :
Ne te contente PAS de décrire le présent — PROJETTE ce qui est le plus probable, sur DEUX horizons :
- COURT (prochaines minutes) : dicté par le flux immédiat (agresseurs 5s, CVD 1min, tape, réaction \
aux murs proches, absorption, sweeps). Dis vers quel niveau le prix va probablement se diriger là, tout de suite.
- MOYEN (prochaines 1-4h) : dicté par le CONTEXTE (biais CVD 15/30min, OI×prix, funding, VWAP/POC, \
structure high/low de session, Fear&Greed). Donne la direction de fond, ex : "le biais 2h est haussier \
tant que 63 800 tient".
Donne des SCÉNARIOS conditionnels avec seuils PRÉCIS : "si casse 64 500 avec CVD qui suit → cible 65 000 \
(haussier)" / "si rejet 64 500 + agresseurs vendeurs → retour 63 800 (baissier)". Dis lequel a ta \
PRÉFÉRENCE et POURQUOI (les données live qui penchent), et donne toujours un niveau d'INVALIDATION \
("ce scénario est mort si..."). Par défaut, aligne-toi avec le biais dominant multi-timeframe, SAUF signal \
de retournement net (divergence prix/CVD, absorption, sweep contre-tendance) — là tu le signales.
Base-toi sur la section ÉVOLUTION du snapshot (comment prix/CVD/agresseurs ont bougé) pour juger la \
DYNAMIQUE, pas l'instant figé. Reste honnête : ce sont des PROBABILITÉS et des scénarios, jamais des \
certitudes — mais DONNE ton scénario le plus probable, ne te réfugie pas dans "j'attends".

CONNAISSANCE COMPLÈTE DU LOGICIEL — tu sais TOUT, tu peux le renvoyer à la bonne page :
• DONNÉES : carnet agrégé 4 sources = Binance + OKX + Bybit + Hyperliquid (BTC perp). Mid robuste \
(médiane des venues), OKX converti de contrats en BTC, spread médian. Confluence N/4 = sur combien \
d'exchanges un mur est visible (souvent 1 car seul Binance publie un carnet profond).
• Page DIRECT : pastilles de statut des venues ; MID, SPREAD, IMBALANCE (liquidité passive achat vs \
vente ±0.4%), CVD, TAPE (trades/s), AGRESSEURS (% achat au marché 5s) ; carnet DOM (prix/taille/cumul) \
+ barre de pression ; "Liquidité près du prix par venue" à profondeur égale ; SIGNAUX expliqués + \
BIAIS ; heatmap (lignes mid bleu / VWAP orange / POC violet, boutons zoom) ; table des MURS live ; \
flux d'ÉVÉNEMENTS (murs, sweeps).
• Page EXÉCUTION : l'utilisateur saisit SES niveaux ; horloge de session (fenêtre 13h-16h) ; bandeau \
FLUX ACTUEL (achat/vente BTC 1min, agresseurs 5s, CVD, VWAP, tape) ; tableau de SES niveaux avec, pour \
chacun : le mur le plus proche + sa taille, l'achat vs vente exécuté À CE NIVEAU (±30$ = accumulé ou \
distribué), le flux actuel, CVD, agresseurs, et un VERDICT REVERSE/CONTINUE + Action quand le prix approche.
• Page ANALYSE : VERDICT lent (10 dernières s) ; ZONES DE LIQUIDITÉ stables ; NIVEAUX CLÉS ; BILANS \
5/15/30/60min (volumes achat/vente, delta, prix actifs, interprétation).
• Page MURS : filtre DISTANCE au prix + TRI (proximité/taille/valeur/prix/durée/tests) ; fenêtres \
1/5/15/30/60min ; compteurs par statut ; MUR LE PLUS TENACE ; 3 sous-onglets (TOUS / SOLIDES=actifs+ \
validés / INVALIDÉS=cassés+spoofs). Statuts EXACTS : ACTIF (présent maintenant), VALIDÉ (le prix l'a \
testé et il a TENU), INVALIDÉ (le prix est passé AU TRAVERS = cassé), SPOOF (retiré TRÈS vite <3s alors \
que le prix était loin = manip flagrante), DISPARU (retiré plus lentement, le prix ne l'a JAMAIS atteint \
= ni spoof ni validé, non concluant, du bruit).
• Page VWAP&CVD : VWAP de session + écart + interprétation ; barre agresseurs ; 4 blocs CVD (1/5/15/30min) \
avec accélération (pression qui s'intensifie ou s'épuise).
• Page INSTITUTIONNELS : flux séparé retail (<0.5 BTC) / moyen (0.5-5) / institutionnel (>5), delta de \
chacun, feed des gros ordres. Règle : suivre le delta institutionnel, pas le retail.
• Page PROFIL : sélecteur de fenêtre ; POC / VAH / VAL / Value Area% ; top niveaux de volume ; vrai \
graphique TradingView intégré (BINANCE:BTCUSDT.P avec VWAP) ; sweeps en cascade.
• Page POSITIONNEMENT : funding rate (Binance) + annualisé ; Open Interest + variation 5/15min ; \
liquidations temps réel (Bybit, longs vs shorts) ; interprétation OI×prix (nouveaux longs / short \
squeeze / nouveaux shorts / capitulation) et cascades.
• Page NEWS&MACRO : indice Fear&Greed + variation ; fil d'actus classé par importance (majeure / \
moyenne / faible) et impact BTC (haussier / baissier).
• Page IA (toi) : budget plafonné à 2,20$/jour ; modèles Haiku/Sonnet/Opus ; mode Auto + ce chat.

TON EXPERTISE order flow (mobilise-la) : imbalance vs agresseurs (passif vs actif), CVD et \
divergences prix/CVD, absorption, sweeps, spoofing, confluence multi-venues, VWAP/POC comme aimants, \
OI×prix (nouveaux longs / short squeeze / capitulation), funding, cascades de liquidations, retail vs \
institutionnel, accumulation/distribution à un niveau, gestion du risque et psychologie.

COMMENT TU RÉPONDS — SOIS EFFICACE (règles importantes) :
- COURT PAR DÉFAUT. En plein trade il veut une lecture rapide, pas un essai. Vise 4 à 8 lignes : le \
constat, le point clé, l'action. Ne développe longuement QUE s'il demande "explique"/"pourquoi"/ \
"détaille", ou pour lui apprendre un concept qu'il découvre.
- SON ÉCRAN EST LA VÉRITÉ. Tes données peuvent avoir ~1s de retard ou être filtrées autrement que sa \
vue. S'il te dit ce qu'il voit ("le mur est cassé", "ça n'existe pas chez moi"), CROIS-LE et ajuste — \
n'argumente JAMAIS sur qui a les bonnes données. Reconcilie en une phrase max et avance.
- NE CITE QUE LES MURS/NIVEAUX PROCHES ET ACTIONNABLES (ceux du snapshot, près du prix). Ne balance pas \
une liste de murs lointains qu'il ne voit pas → confusion. Un objectif lointain : une ligne, pas tout le carnet.
- Rappelle brièvement une distinction utile si besoin (rejet ≠ cassure : niveau ACTIF = a tenu, INVALIDÉ = \
cassé) SANS faire la leçon. Une phrase suffit.
- Appuie-toi sur ses vrais chiffres live. Signaux contradictoires → dis-le en une phrase. Pousse à attendre \
la confluence. Un mur = zone de décision.
- Honnête sur l'incertitude : tu lis le flux ET tu PROJETTES des scénarios probables (avec seuils et \
niveau d'invalidation) sur les prochaines minutes et prochaines heures — mais jamais des certitudes. \
Hors sujet → ramène au trading.
- Pas de disclaimers robotiques, pas d'emojis à répétition. Le trader décide seul."""


class AICopilot:
    def __init__(self, daily_budget_usd=2.20):
        self.daily_budget = daily_budget_usd
        self.model_label = DEFAULT_MODEL
        self._lock = threading.Lock()
        self._busy = False
        self._result = None          # dict prêt à afficher (consommé par l'UI)
        self.error = None
        self.n_calls_today = 0
        self.last_call_ts = 0.0
        self._load_budget()
        self.key = os.environ.get("ANTHROPIC_API_KEY") or self._load_key()

    # ---------- clé API ----------
    def _load_key(self):
        try:
            with open(KEY_FILE, encoding="utf-8") as f:
                k = f.read().strip()
                return k or None
        except OSError:
            return None

    def set_key(self, key):
        key = (key or "").strip()
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        self.key = key or None

    # ---------- budget quotidien ----------
    def _today(self):
        return time.strftime("%Y-%m-%d")

    def _load_budget(self):
        self.spent_today = 0.0
        try:
            with open(BUDGET_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == self._today():
                self.spent_today = float(d.get("spent", 0.0))
                self.n_calls_today = int(d.get("calls", 0))
        except (OSError, ValueError):
            pass

    def _save_budget(self):
        try:
            with open(BUDGET_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": self._today(), "spent": self.spent_today,
                           "calls": self.n_calls_today}, f)
        except OSError:
            pass

    def _check_day_rollover(self):
        # si on a changé de jour, le compteur repart de zéro
        try:
            with open(BUDGET_FILE, encoding="utf-8") as f:
                if json.load(f).get("date") != self._today():
                    self.spent_today = 0.0
                    self.n_calls_today = 0
        except (OSError, ValueError):
            pass

    # ---------- état ----------
    def budget_left(self):
        self._check_day_rollover()
        return max(0.0, self.daily_budget - self.spent_today)

    def can_call(self):
        if not self.key:
            return False, "Clé API manquante — colle-la dans le champ ci-dessus."
        if self._busy:
            return False, "Analyse déjà en cours…"
        if self.budget_left() <= 0.001:
            return False, (f"Budget quotidien atteint ({self.daily_budget:.2f} $). "
                           "Plus aucun appel jusqu'à demain.")
        return True, ""

    def consume_result(self):
        """L'UI appelle ça sur un timer : retourne la dernière analyse une seule fois."""
        with self._lock:
            r = self._result
            self._result = None
            return r

    # ---------- appels ----------
    def request(self, snapshot_text, reason="analyse"):
        """Analyse automatique (format BIAIS/LECTURE/NIVEAUX/PLAN)."""
        ok, why = self.can_call()
        if not ok:
            self.error = why
            return False
        self._busy = True
        self.error = None
        msgs = [{"role": "user", "content": snapshot_text}]
        t = threading.Thread(target=self._call,
                             args=(msgs, SYSTEM_PROMPT, "analysis", reason),
                             daemon=True)
        t.start()
        return True

    def request_chat(self, question, snapshot_text):
        """Question libre de l'utilisateur, avec mémoire de la conversation."""
        ok, why = self.can_call()
        if not ok:
            self.error = why
            return False
        self._busy = True
        self.error = None
        if not hasattr(self, "chat_history"):
            self.chat_history = []
        # historique = questions/réponses passées SANS les instantanés (compact),
        # l'instantané live n'est joint qu'au message courant
        current = {"role": "user",
                   "content": f"[Données live du cockpit]\n{snapshot_text}\n\n"
                              f"[Question du trader]\n{question}"}
        msgs = self.chat_history[-16:] + [current]
        self._pending_question = question
        t = threading.Thread(target=self._call,
                             args=(msgs, CHAT_PROMPT, "chat", "chat"),
                             daemon=True)
        t.start()
        return True

    def chat_sync(self, question, snapshot_text):
        """Version SYNCHRONE de request_chat (pour le bot Telegram) : bloque et
        renvoie (ok, texte). Partage le budget et l'historique avec le chat de l'app."""
        ok, why = self.can_call()
        if not ok:
            return False, why
        if not hasattr(self, "chat_history"):
            self.chat_history = []
        try:
            import anthropic
        except ImportError:
            return False, "module anthropic manquant sur le PC"
        label = getattr(self, "model_label", None) or next(iter(MODELS))
        model_id, p_in, p_out = MODELS[label]
        current = {"role": "user",
                   "content": f"[Données live du cockpit]\n{snapshot_text}\n\n"
                              f"[Question du trader]\n{question}"}
        msgs = self.chat_history[-16:] + [current]
        try:
            client = anthropic.Anthropic(api_key=self.key, timeout=45.0, max_retries=1)
            resp = client.messages.create(
                model=model_id, max_tokens=1200,
                system=[{"type": "text", "text": CHAT_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=msgs)
            text = "".join(b.text for b in resp.content if b.type == "text")
            cost = (resp.usage.input_tokens * p_in
                    + resp.usage.output_tokens * p_out) / 1e6
            self.spent_today += cost
            self.n_calls_today += 1
            self.last_call_ts = time.time()
            self._save_budget()
            self.chat_history.append({"role": "user", "content": question})
            self.chat_history.append({"role": "assistant", "content": text})
            self.chat_history = self.chat_history[-24:]
            return True, text
        except Exception as e:
            return False, f"échec de l'appel ({type(e).__name__})"

    def reset_chat(self):
        self.chat_history = []

    def _call(self, messages, system, kind, reason):
        try:
            import anthropic
        except ImportError:
            self.error = "Module manquant : lance  pip install anthropic  puis redémarre."
            self._busy = False
            return
        model_id, p_in, p_out = MODELS[self.model_label]
        try:
            client = anthropic.Anthropic(api_key=self.key, timeout=45.0, max_retries=1)
            resp = client.messages.create(
                model=model_id,
                max_tokens=2200,   # large : réponses développées possibles sans coupure
                                   # (le français prend ~2x plus de tokens qu'en anglais)
                # cache du prompt système (constant) : les appels rapprochés le relisent
                # à ~10% du prix, ce qui compense sa longueur
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=messages,
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            # si jamais la réponse est quand même coupée, on le signale clairement
            if resp.stop_reason == "max_tokens":
                text += "\n\n…(réponse tronquée — pose une question plus précise)"
            cost = (resp.usage.input_tokens * p_in
                    + resp.usage.output_tokens * p_out) / 1e6
            self.spent_today += cost
            self.n_calls_today += 1
            self.last_call_ts = time.time()
            self._save_budget()
            if kind == "chat":
                # mémorise l'échange en version compacte (question seule, sans snapshot)
                if not hasattr(self, "chat_history"):
                    self.chat_history = []
                self.chat_history.append(
                    {"role": "user", "content": getattr(self, "_pending_question", "?")})
                self.chat_history.append({"role": "assistant", "content": text})
                self.chat_history = self.chat_history[-24:]
            with self._lock:
                self._result = {
                    "kind": kind,
                    "text": text,
                    "ts": time.strftime("%H:%M:%S"),
                    "reason": reason,
                    "cost": cost,
                    "model": self.model_label,
                    "in_tok": resp.usage.input_tokens,
                    "out_tok": resp.usage.output_tokens,
                }
        except Exception as e:
            # connexion faible / API down : on affiche, on ne crashe jamais
            self.error = f"Échec de l'appel ({type(e).__name__}) — réessaie plus tard."
        finally:
            self._busy = False
