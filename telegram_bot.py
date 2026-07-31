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

import json
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
            print("[Telegram] envoi annulé (token ou chat_id manquant)")
            return
        try:
            r = self._api("sendMessage", chat_id=self.chat_id, text=text)
            j = r.json()
            if not j.get("ok"):
                print(f"[Telegram] sendMessage KO: {j.get('description')}")
        except Exception as e:
            print(f"[Telegram] sendMessage erreur: {e}")

    def send_photo(self, png_bytes, caption=""):
        """Envoie une IMAGE (graphique) sur Telegram — cliquable/zoomable."""
        if not self.token or not self.chat_id or not png_bytes:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendPhoto",
                data={"chat_id": self.chat_id, "caption": caption[:1000]},
                files={"photo": ("graph.png", png_bytes, "image/png")}, timeout=30)
        except Exception:
            pass

    def set_commands(self, commands):
        """Enregistre le menu des commandes : Telegram affiche la liste dès qu'on
        tape « / » dans la conversation."""
        if not self.token:
            return
        try:
            self._api("setMyCommands", commands=json.dumps(commands))
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
                data = r.json()
                if not data.get("ok"):
                    # 409 Conflict = un AUTRE process interroge le même bot (ex : l'appli
                    # ET le serveur en même temps) -> le bot devient muet/erratique.
                    print(f"[Telegram] getUpdates KO: {data.get('description')}")
                    time.sleep(3)
                    continue
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    chat = str(msg.get("chat", {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    # /start : (RE)apprend TOUJOURS le chat courant -> permet de récupérer
                    # un chat_id périmé (sinon le bot ignore le vrai utilisateur en silence).
                    if text.lower() in ("/start", "start"):
                        if chat and chat != self.chat_id:
                            self.chat_id = chat
                            if self.on_learn_chat:
                                try:
                                    self.on_learn_chat(chat)
                                except Exception:
                                    pass
                        self.send("👋 Copilote Order Flow connecté. Pose-moi tes questions "
                                  "sur le marché, je réponds avec les données live du cockpit.")
                        continue
                    if not self.chat_id:            # apprentissage auto du 1er contact
                        self.chat_id = chat
                        if self.on_learn_chat:
                            try:
                                self.on_learn_chat(chat)
                            except Exception:
                                pass
                    if self.chat_id and chat != self.chat_id:
                        print(f"[Telegram] message IGNORÉ (chat {chat} ≠ {self.chat_id}) "
                              f"— envoie /start depuis ce chat pour le relier.")
                        continue
                    print(f"[Telegram] question reçue: {text[:60]}")
                    try:
                        reply = self.on_question(text)
                    except Exception as e:
                        reply = f"⚠ Erreur côté PC : {e}"
                    if reply:
                        # Telegram limite à 4096 caractères par message
                        self.send(reply[:4000])
            except Exception as e:
                print(f"[Telegram] boucle erreur: {e}")
                time.sleep(3)      # réseau coupé : on réessaie doucement
