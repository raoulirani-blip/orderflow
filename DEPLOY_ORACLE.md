# Déploiement 24/7 sur Oracle Cloud Always Free

Objectif : faire tourner `server.py` en permanence sur une VM gratuite → alertes ntfy
et bot Telegram fonctionnent **même PC éteint**.

## Fichiers à envoyer sur le serveur (dossier `orderflow/`)
- `server.py`, `engine.py`, `connectors.py`, `wall_history.py`
- `alerts.py`, `ai_copilot.py`, `telegram_bot.py`
- `alerts_config.json` (ta config : topic ntfy, token Telegram, niveaux, fenêtre horaire)
- `claude_key.txt` (ta clé API Claude, pour le bot Telegram)
- `mes_niveaux.txt` (facultatif : tes niveaux)
- `requirements-server.txt`, `orderflow.service`

## Étape 1 — Créer le compte Oracle Cloud (gratuit à vie)
1. Va sur https://www.oracle.com/cloud/free/
2. « Start for free ». Il faut un mail + un numéro + une carte bancaire (vérif d'identité,
   **aucun débit** sur le tier Always Free ; tu ne paies rien tant que tu ne « upgrade » pas).
3. Choisis une région proche (ex. Paris / Frankfurt).

## Étape 2 — Créer la VM Always Free
1. Menu → Compute → Instances → **Create Instance**.
2. Image : **Canonical Ubuntu 22.04**.
3. Shape : **Ampere (ARM) VM.Standard.A1.Flex**, 1 OCPU / 6 Go (dans le quota Always Free).
   Si « out of capacity », réessaie plus tard ou change de domaine de disponibilité.
4. Ajoute/génère une **clé SSH** (télécharge la clé privée).
5. Create. Note l'**IP publique** de l'instance.

## Étape 3 — Se connecter en SSH
Depuis ton PC (PowerShell) :
```
ssh -i chemin\vers\ta_cle_privee ubuntu@IP_PUBLIQUE
```

## Étape 4 — Installer Python et les dépendances
```
sudo apt update && sudo apt install -y python3 python3-pip
mkdir -p ~/orderflow
```
Puis envoie les fichiers (depuis ton PC, nouvelle fenêtre PowerShell) :
```
scp -i ta_cle_privee C:\Users\rauli\Documents\trading\"L2 software"\files_4\orderbook_app\*.py ubuntu@IP:~/orderflow/
scp -i ta_cle_privee C:\Users\rauli\Documents\trading\"L2 software"\files_4\orderbook_app\alerts_config.json ubuntu@IP:~/orderflow/
scp -i ta_cle_privee C:\Users\rauli\Documents\trading\"L2 software"\files_4\orderbook_app\claude_key.txt ubuntu@IP:~/orderflow/
scp -i ta_cle_privee C:\Users\rauli\Documents\trading\"L2 software"\files_4\orderbook_app\requirements-server.txt ubuntu@IP:~/orderflow/
```
De retour dans la session SSH :
```
cd ~/orderflow
pip3 install -r requirements-server.txt
```

## Étape 5 — Tester à la main
```
python3 -u server.py
```
Tu dois voir « SERVEUR ORDER FLOW 24/7 DÉMARRÉ » puis un battement de cœur. Envoie un
message à ton bot Telegram : il doit répondre. Ctrl+C pour arrêter.

## Étape 6 — Le faire tourner en permanence (systemd)
```
sudo cp ~/orderflow/orderflow.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable orderflow
sudo systemctl start orderflow
sudo systemctl status orderflow      # doit être "active (running)"
```
Voir les logs en direct :
```
tail -f ~/orderflow/server.log
```
Désormais le serveur redémarre tout seul (reboot, crash) et tourne 24/7.

## Mettre à jour la config plus tard
Modifie `alerts_config.json` (niveaux, topic, token…) — soit en le ré-uploadant par scp,
soit en l'éditant sur le serveur (`nano alerts_config.json`), puis :
```
sudo systemctl restart orderflow
```

## Important
- Le **topic ntfy** et le **token Telegram** doivent être les mêmes que dans l'appli.
- Deux instances (ton PC + le serveur) peuvent tourner en même temps : tu recevras
  éventuellement l'alerte en double. En pratique, laisse tourner **le serveur** et n'ouvre
  l'appli PC que pour l'interface visuelle (ou désactive les alertes dans l'appli PC).
- Le bot Telegram : une seule instance doit interroger Telegram à la fois (sinon conflit
  getUpdates). Donc si le serveur fait tourner le bot, désactive le bot dans l'appli PC.
