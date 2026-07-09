"""
recorder.py — enregistreur d'historique des murs 24/7 (SANS interface graphique).

But : faire tourner le moteur order-flow en tâche de fond, en continu, pour que
`wall_state.json` reste à jour même quand l'appli principale est fermée. Comme
l'appli recharge ce fichier au lancement, tu retrouves l'âge des murs, leurs tests
et les cassures pour les périodes où tu n'avais pas l'appli ouverte.

Ce que ça capture : l'historique des MURS observés (le seul historique de carnet
qui existe, celui que le logiciel voit lui-même en live — aucune API ne fournit
l'historique du carnet).

Ressources : négligeables — une poignée de connexions WebSocket, ~30-60 Mo de RAM,
CPU quasi nul. Tu peux le laisser tourner en permanence.

Usage :
    python recorder.py           (laisse la fenêtre ouverte, Ctrl+C pour arrêter)
Ou double-clique sur run_recorder.bat. Pour le lancer au démarrage de Windows,
voir RECORDER_README.txt.
"""

import time

from engine import OrderFlowEngine


def main():
    # on_update = no-op : on n'affiche rien, on ne fait qu'accumuler l'historique
    eng = OrderFlowEngine(on_update=lambda s: None)
    eng.start()
    print("=" * 60)
    print(" ENREGISTREUR DE MURS DÉMARRÉ")
    print(" Historique tenu à jour dans wall_state.json")
    print(" Laisse cette fenêtre ouverte. Ctrl+C pour arrêter proprement.")
    print("=" * 60)
    last_log = 0.0
    try:
        while True:
            time.sleep(5)
            now = time.time()
            if now - last_log > 60:          # un point de vie toutes les minutes
                actifs = len(eng.wall_history.active)
                histo = len(eng.wall_history.closed)
                statut = eng.agg.status
                oks = sum(1 for v in statut.values() if v == "ok")
                print(f"{time.strftime('%H:%M:%S')}  "
                      f"venues OK: {oks}/{len(statut)}  |  "
                      f"murs suivis: {actifs}  |  historique: {histo}")
                last_log = now
    except KeyboardInterrupt:
        print("\nArrêt demandé — sauvegarde finale de l'historique...")
        eng.stop()
        time.sleep(0.5)
        print("Historique sauvegardé dans wall_state.json. À bientôt.")


if __name__ == "__main__":
    main()
