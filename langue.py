# -*- coding: utf-8 -*-
"""Le choix de langue du greffon, en un seul endroit.

Deux appelants : le greffon au demarrage (`__init__.py`) et les boutons FR/EN
du formulaire (`dialogue_langue.py`). Une seule facon de choisir la langue,
pas deux — sinon le bouton et le demarrage finiraient par ne plus dire la
meme chose.

Les sources du greffon sont en ANGLAIS ; le francais vient d'un `.qm` charge
par QTranslator. « Passer en anglais » consiste donc a RETIRER le traducteur,
pas a en installer un autre.
"""

from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication, QLocale, QTranslator
from qgis.core import QgsSettings

PAQUET = "ZIV_prime"
LANGUES = ("fr", "en")
CLE = "plugins/%s/langue" % PAQUET

# Le traducteur installe, garde en vie ici : sans reference Python, Qt le
# libere et la traduction disparait silencieusement.
_traducteur = None


def dossier():
    return Path(__file__).parent / "i18n"


def retenue():
    """La langue a appliquer : le choix explicite du greffon, sinon QGIS.

    Un greffon qui impose sa langue contre QGIS serait penible ; un greffon
    qui ignore un clic explicite le serait davantage. Le reglage n'existe
    donc que si l'utilisateur a clique.

    La locale de QGIS n'est PAS rendue telle quelle : le greffon ne parle que
    `LANGUES`, et tout ce qu'il ne parle pas retombe sur l'anglais, langue de
    ses sources. Rendre « de » ou « es » brut donnait une interface
    FRANCAISE — `appliquer()` repliait sur son defaut sans rien dire.
    """
    choisie = QgsSettings().value(CLE, "")
    if choisie in LANGUES:
        return choisie
    # QGIS n'ecrit `locale/userLocale` que si l'on a touche au reglage : sur un
    # profil neuf la cle est ABSENTE, et la langue reelle est celle du systeme.
    # L'ancien repli codait « fr » en dur a cette place — juste en France, faux
    # partout ailleurs pour un greffon bi-pays.
    demandee = (QgsSettings().value("locale/userLocale")
                or QLocale.system().name() or "")[0:2]
    return demandee if demandee in LANGUES else "en"


def appliquer(langue, memoriser=False):
    """Installe ou retire la traduction, a chaud. Rend la langue effective.

    Les libelles du greffon sont recalcules a chaque appel de `tr()`, donc le
    registre Processing rend le texte traduit des l'appel suivant — verifie le
    2026-09-10. Ce qui NE se retraduit pas tout seul, ce sont les widgets deja
    peints : c'est au formulaire de se reconstruire.
    """
    global _traducteur

    # Meme repli que `retenue()` : l'anglais, langue des sources. Les deux
    # replis divergeaient, et celui-ci gagnait — c'est lui qui rendait le
    # francais a un utilisateur allemand.
    if langue not in LANGUES:
        langue = "en"

    if _traducteur is not None:
        QCoreApplication.removeTranslator(_traducteur)
        _traducteur = None

    # L'anglais est la langue des sources : il n'a pas de .qm, et n'en aura
    # jamais. Retirer le traducteur SUFFIT a y revenir.
    if langue != "en":
        fichier = dossier() / ("%s_%s.qm" % (PAQUET, langue))
        if fichier.exists():
            traducteur = QTranslator()
            if traducteur.load(str(fichier)):
                QCoreApplication.installTranslator(traducteur)
                _traducteur = traducteur
        else:
            langue = "en"

    if memoriser:
        QgsSettings().setValue(CLE, langue)
    return langue


def rafraichir_fournisseurs():
    """Demande aux fournisseurs Processing de se relire.

    Sans objet pour les libelles eux-memes — ils suivent `tr()` — mais l'arbre
    de la boite a outils, lui, garde le texte peint a son dernier remplissage.
    """
    try:
        from qgis.core import QgsApplication

        registre = QgsApplication.processingRegistry()
        for identifiant in ("genziv", "pcfr_vis"):
            fournisseur = registre.providerById(identifiant)
            if fournisseur is not None:
                fournisseur.refreshAlgorithms()
    except Exception:
        # Le confort d'affichage ne justifie pas de faire echouer un clic.
        pass
