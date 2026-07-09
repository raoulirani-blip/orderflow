"""
alerts.py — envoi de notifications vers le téléphone (WhatsApp via CallMeBot).

CallMeBot est le seul service WhatsApp gratuit et sans compte : on lui envoie une
requête HTTP GET et il te transfère le message sur WhatsApp. Limite : ~1 message
toutes les ~30-60 s, donc on met une file d'attente + un intervalle minimum pour
ne jamais se faire bloquer. Tout part dans un thread de fond : l'UI ne gèle jamais.

Backend interchangeable : 'whatsapp' (CallMeBot) par défaut, 'telegram' en secours
(sans limite de débit) si un jour la fréquence WhatsApp devient trop juste.
"""

import queue
import threading
import time
import urllib.parse

import requests


class Notifier:
    def __init__(self, min_interval=25.0):
        # intervalle minimum entre deux envois (CallMeBot ~30-60s : on reste prudent)
        self.min_interval = min_interval
        self._q = queue.Queue()
        self._cfg = {"backend": "ntfy", "phone": "", "apikey": "",
                     "ntfy_topic": "", "tg_token": "", "tg_chat": ""}
        self._last_send = 0.0
        self._running = True
        self._log = []                       # liste de dicts {ts, text, ok, err}
        self._lock = threading.Lock()
        self._t = threading.Thread(target=self._worker, daemon=True)
        self._t.start()

    def configure(self, **kw):
        with self._lock:
            for k, v in kw.items():
                if v is not None and k in self._cfg:
                    self._cfg[k] = v

    def send(self, text):
        """Met un message en file (retourne tout de suite, envoi async)."""
        self._q.put(text)

    def send_now(self, text):
        """Envoi synchrone (pour le bouton Tester) — retourne (ok, err)."""
        return self._deliver(text)

    def _worker(self):
        while self._running:
            try:
                text = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            wait = self.min_interval - (time.time() - self._last_send)
            if wait > 0:
                time.sleep(wait)
            ok, err = self._deliver(text)
            self._last_send = time.time()
            with self._lock:
                self._log.append({"ts": time.time(), "text": text, "ok": ok, "err": err})
                self._log = self._log[-120:]

    def _deliver(self, text):
        with self._lock:
            cfg = dict(self._cfg)
        b = cfg.get("backend", "ntfy")
        try:
            if b == "ntfy":
                # nettoyage : l'utilisateur peut coller "ntfy.sh/trading" ou une URL,
                # on ne garde que le nom du topic (sinon HTTP 404 sur double préfixe)
                topic = cfg.get("ntfy_topic", "").strip()
                for pre in ("https://", "http://"):
                    if topic.startswith(pre):
                        topic = topic[len(pre):]
                if topic.startswith("ntfy.sh/"):
                    topic = topic[len("ntfy.sh/"):]
                topic = topic.strip("/")
                if not topic:
                    return False, "nom de topic ntfy manquant"
                # titre = 1re ligne du message, en ASCII (en-tête HTTP = pas d'emoji)
                title = (text.split("\n", 1)[0][:80]
                         .encode("ascii", "ignore").decode().strip()) or "Alerte"
                r = requests.post(
                    f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                    headers={"Title": title, "Priority": "high",
                             "Tags": "chart_with_upwards_trend"},
                    timeout=20)
                return (r.status_code == 200), (None if r.status_code == 200
                                                else f"HTTP {r.status_code}")
            if b == "whatsapp":
                phone = cfg.get("phone", ""); key = cfg.get("apikey", "")
                if not phone or not key:
                    return False, "numéro ou clé API WhatsApp manquant"
                url = ("https://api.callmebot.com/whatsapp.php"
                       f"?phone={urllib.parse.quote(phone)}"
                       f"&text={urllib.parse.quote(text)}"
                       f"&apikey={urllib.parse.quote(key)}")
                r = requests.get(url, timeout=25)
                return (r.status_code == 200), (None if r.status_code == 200
                                                else f"HTTP {r.status_code}")
            if b == "telegram":
                tok = cfg.get("tg_token", ""); chat = cfg.get("tg_chat", "")
                if not tok or not chat:
                    return False, "token/chat Telegram manquant"
                r = requests.get(f"https://api.telegram.org/bot{tok}/sendMessage",
                                 params={"chat_id": chat, "text": text}, timeout=25)
                return (r.status_code == 200), (None if r.status_code == 200
                                                else f"HTTP {r.status_code}")
        except Exception as e:
            return False, str(e)
        return False, "backend inconnu"

    def recent_log(self, n=25):
        with self._lock:
            return list(self._log[-n:])

    def stop(self):
        self._running = False
