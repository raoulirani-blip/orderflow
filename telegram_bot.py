"""
telegram_bot.py — bot Telegram bidirectionnel pour DISCUTER avec le copilote.

Différent des alertes (ntfy) : ici tu ENVOIES des messages au bot depuis ton
téléphone ("il se passe quoi sur 63k ?", "j'ai pas compris l'absorption") et le
copilote Claude te répond avec les données live du cockpit. Pratique quand tu n'as
pas le PC.

Fonctionnement : long-polling getUpdates dans un thread de fond. Chaque message
reçu est passé à un callback (qui appelle le copilote) et la réponse est renvoyée.
Le chat_id est appris automatiquement au premier message si non fourni.
"""

import threading
import time

import requests


class TelegramCopilotBot:
    def __init__(self, token, chat_id, on_question, on_learn_chat=None):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.on_question = on_question          # callable(str) -> str (réponse)
        self.on_learn_chat = on_learn_chat      # callable(str) quand on apprend le chat_id
        self._running = False
        self._offset = None
        self._t = None

    def start(self):
        if self._running or not self.token:
            return
        self._running = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._running = False

    def _api(self, method, **params):
        return requests.get(f"https://api.telegram.org/bot{self.token}/{method}",
                            params=params, timeout=35)

    def send(self, text):
        if not self.token or not self.chat_id:
            return
        try:
            self._api("sendMessage", chat_id=self.chat_id, text=text)
        except Exception:
            pass

    def _loop(self):
        # purge les vieux messages en attente au démarrage (offset = dernier +1)
        try:
            r = self._api("getUpdates", timeout=0)
            res = r.json().get("result", [])
            if res:
                self._offset = res[-1]["update_id"] + 1
        except Exception:
            pass
        while self._running:
            try:
                params = {"timeout": 25}
                if self._offset:
                    params["offset"] = self._offset
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                 params=params, timeout=35)
                for upd in r.json().get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    chat = str(msg.get("chat", {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    if not self.chat_id:            # apprentissage auto du 1er contact
                        self.chat_id = chat
                        if self.on_learn_chat:
                            try:
                                self.on_learn_chat(chat)
                            except Exception:
                                pass
                    if self.chat_id and chat != self.chat_id:
                        continue                    # ignore les autres expéditeurs
                    if text.lower() in ("/start", "start"):
                        self.send("👋 Copilote Order Flow connecté. Pose-moi tes questions "
                                  "sur le marché, je réponds avec les données live du cockpit.")
                        continue
                    try:
                        reply = self.on_question(text)
                    except Exception as e:
                        reply = f"⚠ Erreur côté PC : {e}"
                    if reply:
                        # Telegram limite à 4096 caractères par message
                        self.send(reply[:4000])
            except Exception:
                time.sleep(3)      # réseau coupé : on réessaie doucement
