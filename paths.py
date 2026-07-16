"""
paths.py — où l'application range ses données (journal, réglages, historiques).

- En mode normal (py app.py) : le dossier du code.
- En mode EXÉCUTABLE (.exe PyInstaller) : le dossier de l'EXE — et surtout PAS
  le dossier temporaire d'extraction (_MEIxxxx), qui est effacé à chaque
  fermeture. Sans ça, le journal de trades, les réglages du calculateur et
  l'historique des murs seraient perdus à chaque lancement.
"""

import os
import sys


def app_dir():
    """Dossier où l'appli lit/écrit ses fichiers de données."""
    if getattr(sys, "frozen", False):          # lancé depuis le .exe
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_file(name):
    """Chemin complet d'un fichier de données à côté de l'appli/de l'exe."""
    return os.path.join(app_dir(), name)
