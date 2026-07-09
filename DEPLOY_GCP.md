# Déploiement 24/7 sur Google Cloud (e2-micro, gratuit à vie)

Objectif : `server.py` tourne en permanence → alertes ntfy + bot Telegram même PC éteint.

## Étape 1 — Compte + projet
1. Va sur https://console.cloud.google.com/ et connecte-toi (compte Google).
2. Accepte l'essai gratuit si proposé (carte demandée pour vérif, **pas de débit** dans
   les limites Always Free). Le e2-micro reste gratuit **même après** la fin de l'essai.
3. En haut, crée/choisis un **projet** (ex. « orderflow »).

## Étape 2 — Créer la VM e2-micro (bien rester dans le free)
1. Menu ☰ → **Compute Engine** → **VM instances** → **Create instance**.
   (Si « Compute Engine API » à activer : clique Enable, attends ~1 min.)
2. **Name** : `orderflow`
3. **Region** : OBLIGATOIRE une des 3 gratuites → **`us-central1`** (Iowa).
   **Zone** : `us-central1-a`.
4. **Machine configuration** : série **E2**, type **`e2-micro`** (2 vCPU partagés, 1 Go).
   ⚠️ e2-micro EXACTEMENT (pas e2-small) sinon ce n'est plus gratuit.
5. **Boot disk** : clique Change → **Ubuntu 22.04 LTS**, disque **Standard**, **30 Go** max
   (au-delà = payant).
6. Laisse le reste par défaut. **Create**.
7. Note l'**IP externe** qui apparaît (pas indispensable, on passe par le SSH navigateur).

## Étape 3 — Ouvrir le terminal (SSH navigateur)
Dans la liste des VM, ligne `orderflow` → clique le bouton **SSH**. Un terminal noir
s'ouvre dans le navigateur. Tout se fait là, aucune clé à gérer.

## Étape 4 — Installer Python
Dans ce terminal, colle :
```
sudo apt update && sudo apt install -y python3 python3-pip
mkdir -p ~/orderflow
```

## Étape 5 — Envoyer les fichiers
En haut à droite du terminal SSH : icône **⚙ (roue dentée)** → **Upload file**.
Envoie ces fichiers (depuis `C:\Users\rauli\Documents\trading\L2 software\files_4\orderbook_app\`) :
- `server.py`, `engine.py`, `connectors.py`, `wall_history.py`
- `alerts.py`, `ai_copilot.py`, `telegram_bot.py`
- `alerts_config.json`, `claude_key.txt`, `mes_niveaux.txt`
- `requirements-server.txt`, `orderflow.service`

Ils arrivent dans `/home/<toi>/`. Range-les dans le dossier :
```
mv ~/*.py ~/orderflow/ 2>/dev/null
mv ~/alerts_config.json ~/claude_key.txt ~/mes_niveaux.txt ~/requirements-server.txt ~/orderflow.service ~/orderflow/ 2>/dev/null
cd ~/orderflow && ls
```

## Étape 6 — Dépendances + test
```
cd ~/orderflow
pip3 install -r requirements-server.txt
python3 -u server.py
```
Tu dois voir « SERVEUR ORDER FLOW 24/7 DÉMARRÉ », les venues qui passent OK, un prix,
puis un battement de cœur. Envoie un message à ton bot Telegram → il doit répondre.
**Ctrl+C** pour arrêter.

## Étape 7 — Tourner en permanence (systemd)
Le service est écrit pour l'utilisateur `ubuntu` ; sur GCP ton utilisateur est différent,
donc on corrige le fichier automatiquement :
```
cd ~/orderflow
sed -i "s/^User=.*/User=$USER/" orderflow.service
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$HOME/orderflow#" orderflow.service
sed -i "s#^ExecStart=.*#ExecStart=/usr/bin/python3 -u $HOME/orderflow/server.py#" orderflow.service
sed -i "s#/home/ubuntu/orderflow/server.log#$HOME/orderflow/server.log#g" orderflow.service
sudo cp orderflow.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable orderflow
sudo systemctl start orderflow
sudo systemctl status orderflow      # doit être "active (running)"
```
Logs en direct :
```
tail -f ~/orderflow/server.log
```

## Mettre à jour la config plus tard
Édite `alerts_config.json` sur le serveur (`nano ~/orderflow/alerts_config.json`) puis :
```
sudo systemctl restart orderflow
```

## Rappels
- Même **topic ntfy** et même **token Telegram** que dans l'appli.
- Le **bot Telegram** ne doit tourner que sur UNE machine → une fois le serveur en route,
  **décoche « Activer le bot Telegram » dans l'appli PC** (sinon conflit).
- Pour éviter les alertes en double, laisse le serveur gérer les alertes et désactive-les
  dans l'appli PC (ou n'ouvre l'appli que pour l'interface).
