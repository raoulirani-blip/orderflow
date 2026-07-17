"""
github_sync.py — canal serveur -> PC via GitHub, pour que l'historique enregistré
24/7 par le serveur (murs, agresseurs/z-scores, liquidations) arrive dans l'appli
locale, PC allumé ou éteint.

- Le SERVEUR publie `server_history.json` via l'API GitHub (PUT authentifié par un
  token stocké dans github_token.txt). Voir publish().
- L'APPLI LOCALE le télécharge en RAW (sans auth) et le fusionne. Voir fetch().

Aucune IP à gérer : les URLs GitHub sont stables.
"""

import base64
import json
import os

import requests

REPO = "raoulirani-blip/orderflow"          # owner/repo (déduit de NIVEAUX_RAW)
BRANCH = "main"
HISTORY_PATH = "server_history.json"

RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{HISTORY_PATH}"
API_URL = f"https://api.github.com/repos/{REPO}/contents/{HISTORY_PATH}"


def read_token(here):
    """Lit le token GitHub depuis github_token.txt (jamais commité). None si absent."""
    p = os.path.join(here, "github_token.txt")
    try:
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
        return t or None
    except OSError:
        return None


def publish(token, data: dict):
    """Écrit/écrase server_history.json sur GitHub via l'API Contents. Renvoie
    (ok, message). Récupère d'abord le sha courant (obligatoire pour écraser)."""
    if not token:
        return False, "pas de token"
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "OrderFlowServer"}
    sha = None
    try:
        r = requests.get(API_URL, headers=headers, params={"ref": BRANCH}, timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception as e:
        return False, f"GET sha: {e}"
    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
    payload = {"message": "server history update",
               "content": base64.b64encode(body).decode("ascii"),
               "branch": BRANCH}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(API_URL, headers=headers, data=json.dumps(payload), timeout=30)
        if r.status_code in (200, 201):
            return True, f"publié ({len(body)//1024} Ko)"
        return False, f"PUT {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, f"PUT: {e}"


def fetch():
    """Télécharge server_history.json en RAW (sans auth). Renvoie le dict, ou None."""
    try:
        r = requests.get(RAW_URL, timeout=20,
                         headers={"User-Agent": "OrderFlowCockpit"})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None
