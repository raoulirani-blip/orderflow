========================================================================
 ENREGISTREUR D'HISTORIQUE DES MURS — MODE D'EMPLOI
========================================================================

À QUOI ÇA SERT
--------------
L'enregistreur (recorder.py) fait tourner le moteur en tâche de fond, sans
interface, et garde le fichier wall_state.json à jour en permanence.

Quand tu ouvres l'appli principale, elle recharge wall_state.json : tu
retrouves donc l'historique des murs (âge, tests, cassés/tenus) pour la
dernière heure MÊME si l'appli n'était pas ouverte — à condition que
l'enregistreur, lui, tournait.

C'est GRATUIT (ton PC + WebSockets publics) et léger (~30-60 Mo RAM).


DÉMARRAGE MANUEL
----------------
1. Double-clique sur run_recorder.bat
2. Une fenêtre noire s'ouvre et affiche un point de vie chaque minute.
3. Laisse-la ouverte. Ctrl+C pour arrêter proprement.

Tu peux lancer l'appli principale en même temps : les deux écrivent le
fichier de façon atomique, aucun risque de corruption.


LANCEMENT AUTOMATIQUE AU DÉMARRAGE DE WINDOWS (recommandé)
----------------------------------------------------------
Pour qu'il démarre tout seul à chaque allumage du PC :

1. Appuie sur   Win + R
2. Tape        shell:startup     puis Entrée
   (ça ouvre le dossier Démarrage de Windows)
3. Fais un clic droit dans ce dossier > Nouveau > Raccourci
4. Comme cible, mets le chemin complet de run_recorder.bat, par ex :
   "C:\Users\rauli\Documents\trading\L2 software\files_4\orderbook_app\run_recorder.bat"
5. Valide.

Désormais l'enregistreur se lance à chaque démarrage de Windows.
Astuce : pour qu'il démarre réduit, tu peux éditer les propriétés du
raccourci > Exécuter : Réduite.


LIMITE IMPORTANTE
-----------------
L'enregistreur ne capture QUE quand ton PC est allumé et connecté.
- PC éteint / en veille = pas d'enregistrement pendant ce temps.
- Actuellement l'historique utile va jusqu'à ~1 heure (fenêtres de la page
  MURS : 1/5/15/30/60 min). Faire tourner l'enregistreur des jours entiers
  n'ajoute pas plus d'1h visible tant qu'on n'étend pas la rétention.

Si tu veux un enregistrement VRAIMENT 24/7 (même PC éteint) ou conserver
PLUSIEURS JOURS d'historique de murs, dis-le : ça demande soit un petit
serveur cloud gratuit (Oracle Cloud Always Free), soit d'étendre la
rétention + ajouter des fenêtres plus longues à la page MURS. C'est un
chantier séparé.
========================================================================
