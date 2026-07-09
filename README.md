# Order Flow Cockpit — BTC perp (multi-exchange)

Vue **dé-biaisée** du carnet d'ordres et de l'order flow Bitcoin perpétuel, en
agrégeant **Binance + OKX + Bybit + Hyperliquid** en temps réel. Conçue pour un
trader technique qui ajoute le **Level 2 / order flow** à son analyse : chaque
signal est **expliqué** pour que tu apprennes à lire, et fusionnes ça avec TON AT.

Note technique : OKX publie son carnet en *contrats* (1 = 0,01 BTC) ; l'app le
convertit en BTC réels pour que l'agrégation des 4 sources soit cohérente.

## Pourquoi multi-exchange (le point clé)

Un seul exchange = une vue **biaisée**. Tu ne vois que la liquidité de CE venue,
et un gros acteur peut y poser un faux mur (spoof) pour t'influencer. En
agrégeant 4 sources (3 CEX + le DEX Hyperliquid), l'app distingue :

- **Mur sur 4/4 venues** = vraie liquidité, niveau fiable (rejet/cassure à suivre).
- **Mur sur 1/4 venue** = méfiance, probable leurre d'un seul exchange.

C'est exactement la différence entre "le carnet Binance" et "le marché".

## Panneaux

- **Haut** : statut des 4 venues + MID / SPREAD / IMBALANCE / CVD / TAPE /
  AGRESSEURS agrégés (survole chaque chiffre pour l'explication).
- **Gauche** : carnet agrégé (DOM) — chaque niveau montre sur combien de venues
  il existe (points). Jauge de pression + contribution par venue.
- **Centre — SIGNAUX EXPLIQUÉS** : chaque signal d'order flow avec, en dessous,
  *pourquoi ça compte* (↳ en italique). Plus un **BIAIS** global. C'est le mode
  d'apprentissage : tu lis le signal ET sa logique.
- **Droite** : heatmap de liquidité agrégée, table des murs (avec confluence
  N/4), flux d'événements en direct (murs, sweeps).

## Signaux order flow couverts

- **Imbalance** carnet (pression passive).
- **CVD** (delta cumulé) + **divergences prix/CVD** — très utile pour un
  technicien : prix qui monte mais CVD qui descend = hausse non soutenue.
- **Agresseurs** (qui frappe au marché) + **tape speed** (volatilité).
- **Absorption** : gros volume agressif qui ne bouge PAS le prix = un acteur
  passif absorbe (souvent plancher/plafond, signal de retournement).
- **Sweeps** : prints agressifs surdimensionnés.
- **Confluence multi-venues** des murs (le signal phare ici).

## Lancer

```powershell
py -3.12 -m pip install PyQt6 pyqtgraph numpy websockets requests sortedcontainers
py -3.12 app.py
```

Fichiers : `app.py` (interface), `engine.py` (agrégation + analytics),
`connectors.py` (un connecteur par exchange).

## En faire un .exe

```powershell
py -3.12 -m pip install pyinstaller
py -3.12 -m PyInstaller --noconsole --onefile --name "Cockpit" app.py
```

## Les vraies limites (à connaître)

- **Données = ces 4 sources**, pas le marché mondial total (il reste Coinbase,
  Kraken, l'OTC invisible…). Mais Binance+OKX+Bybit = l'essentiel du volume perp.
- **Agrégation à 100ms** (market-by-price, pas market-by-order). Largement
  suffisant pour de l'intraday discrétionnaire ; ce n'est pas du tick-by-tick HFT.
- Le **BIAIS et les signaux ne sont PAS des ordres d'achat/vente.** C'est une
  couche de confirmation. La décision vient de TON AT croisée avec ça. Un sweep
  ou un retrait de mur peut tout inverser en une seconde.
- Outil d'analyse et d'apprentissage. **Pas un conseil financier.**

## Comment t'en servir pour progresser

1. Marque tes niveaux AT comme d'habitude.
2. Quand le prix approche un niveau, regarde le carnet agrégé : y a-t-il un mur
   4/4 qui le confirme ? Le CVD va-t-il dans ton sens ?
3. Confluence AT + order flow = setup fort. Niveau AT sans rien dans le flux =
   fragile. C'est là qu'est le gain de rentabilité, pas dans le signal seul.

## Réglages (engine.py)

- `wall_k` : seuil de mur (× médiane locale, défaut 6).
- `depth_usd_pct` : bande autour du mid pour profondeur/imbalance.
- `bucket` : taille des paquets de prix pour l'agrégation (défaut 1 USDT).
- `venues` : liste des exchanges (retire-en un pour alléger).

---

## Page 2 — ANALYSE (nouveau)

L'appli a maintenant **2 onglets** en haut :

- **📊 DIRECT** : le cockpit temps réel (ce que tu avais déjà).
- **🧠 ANALYSE** : une page qui **ralentit et conclut**, faite pour être lisible
  quand le direct va trop vite.

La page ANALYSE contient :

1. **VERDICT DU CARNET** — une phrase qui résume les 10 dernières secondes
   (qui domine, le prix tient/monte/baisse, divergences). Se met à jour toutes
   les **5 secondes**, donc tu as le temps de lire.

2. **ZONES DE LIQUIDITÉ** — les plus gros niveaux de liquidité du moment,
   **classés**, chacun avec le *pourquoi* (taille, fiabilité N/4 venues, depuis
   combien de temps il tient). Un niveau n'apparaît que s'il a persisté quelques
   secondes → pas de clignotement, pas de bruit.

3. **NIVEAUX CLÉS À SURVEILLER** — résistance la plus proche, support le plus
   proche, et la zone de liquidité majeure. Pour chacun : **quoi attendre et
   quoi faire** (rejet = opportunité, cassure = ne pas se positionner contre).

Ces deux dernières se rafraîchissent toutes les **2 secondes**.

> But de cette page : tu n'as plus besoin de lire le flux qui défile. Tu vas sur
> ANALYSE, tu lis le verdict et les zones, tu reviens à ton graphique AT, et tu
> croises. C'est là que tu prends tes décisions.

---

## Page ANALYSE — BILANS PÉRIODIQUES (5 / 15 / 30 / 60 min)

En bas de l'onglet 🧠 ANALYSE, une zone **BILANS PÉRIODIQUES** avec **4 sous-onglets** :
**5 min · 15 min · 30 min · 60 min**. Chacun se met à jour **à son propre rythme**
(le 5 min toutes les 5 min, le 15 min toutes les 15 min, etc.).

Chaque bilan affiche :

- **Bandeau du haut** : l'heure de la **dernière mise à jour** + un **compte à
  rebours** jusqu'au prochain rafraîchissement.
- **Colonne chiffres** : dominant (acheteurs/vendeurs), nombre d'ordres achat vs
  vente, volumes (BTC), montants (M$), delta, volume total, nb de transactions
  (et par minute), variation de prix, et les **prix les plus actifs**.
- **Colonne ANALYSE FINE** : l'interprétation — *ce que les chiffres veulent dire*
  et *ce qu'on peut en conclure*. Niveau d'activité, présence de gros acteurs
  (gros ordres détectés), concentration du volume sur un niveau clé, cohérence
  delta/prix (et divergences = absorption), compression de range, etc.
- **Résumé** : un paragraphe qui relie le tout.

> Plus la fenêtre est longue (60 min), plus la lecture est « de fond » (tendance) ;
> plus elle est courte (5 min), plus c'est réactif (ce qui se passe maintenant).
> Comparer les 4 te montre si le court terme confirme ou contredit le fond.

Les bilans s'accumulent en mémoire : laisse l'appli tourner, ils se remplissent
tout seuls. Le 60 min sera complet après une heure de fonctionnement continu.

---

## Page MURS (🧱) — étude dédiée aux murs

Troisième page, à côté de ANALYSE. Elle étudie les murs sur **5 fenêtres de temps**
(sous-onglets **1 / 5 / 15 / 30 / 60 min**), rafraîchies toutes les 3 secondes.

Pour chaque fenêtre :

- **Bandeau** : nombre de murs vus, répartition support/résistance, nombre de
  spoofs détectés.
- **MUR LE PLUS TENACE** : celui qui a tenu le plus longtemps sur la période —
  prix, durée de vie, taille max (BTC + $), nombre de fois testé, et son sort
  (🟢 actif / 🔴 cassé / ⚪ retiré).
- **Tableau des murs les plus importants** (classés par taille × persistance ×
  venues) : Côté, Prix, Taille BTC, Valeur $, N/4 (fiabilité multi-venues),
  Durée de vie, Tests subis. L'emoji d'état montre s'il est actif/cassé/retiré.
- **ANALYSE DES MURS** : interprétation — taux de spoofing, équilibre
  support/résistance (biais du carnet), le mur le plus tenace, les zones de
  bataille (murs testés plusieurs fois).

> Note importante : les exchanges publient la **taille agrégée par niveau de prix**
> (market-by-price), **pas le nombre d'ordres individuels**. "Nombre d'ordres sur
> un mur" n'est donc pas disponible. À la place, la page donne ce qui est
> réellement exploitable : taille (BTC + $), durée de vie, nombre de tests, et
> fiabilité multi-venues — souvent plus parlant que le nombre d'ordres.

Comment t'en servir : un mur **gros + vieux + testé plusieurs fois + 4/4 venues**
qui tient = vraie zone forte, à croiser avec ton AT. Un mur **qui apparaît/disparaît
vite (spoof)** = à ignorer. Compare les fenêtres : un mur visible sur le 60 min ET
le 1 min = structure de fond solide.
