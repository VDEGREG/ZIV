# -*- coding: utf-8 -*-
"""Fournisseur France : une enveloppe mince sur la chaîne d'origine.

**Ce module ne contient aucune logique de données.** La chaîne française est
reprise telle quelle de `ZIV_prime` 1.5.3 et n'a pas été touchée : découverte
des dalles par le WFS de la Géoplateforme, téléchargement à la résolution de
travail, mosaïque, masquage par les empreintes BD Forêt et BDTOPO. Tout cela
vit dans `import_dalles/`, `donnees_ign.py` et `masquage.py`, et l'algorithme
continue de les appeler directement.

Ce fournisseur répond donc seulement aux **questions** que la détection de pays
pose — quelle couverture, quel système de coordonnées, quelle résolution
native — et déclare, par `fournit_terrain()` à faux, que son chemin de données
est l'autre branche.

C'est la décision n°15 du plan appliquée à la lettre : plutôt que de
déparamétrer une chaîne en production durement stabilisée, on la laisse
intacte et on branche l'américaine à côté. Le coût assumé est une asymétrie
entre les deux fournisseurs ; le bénéfice est qu'aucune ligne du chemin
français n'a bougé, ce que les sept bancs de tests vérifient.
"""

from . import COUVERTURE_FRANCE, Fournisseur

# Résolution du LiDAR HD tel que l'IGN le livre. Même valeur que
# `import_dalles/geoplateforme.RESOLUTION_NATIVE`, et pour la même raison :
# au-delà, le serveur ré-interpolerait une donnée qu'il n'a pas.
RESOLUTION_NATIVE = 0.5

# Système de travail de toute la chaîne française. En dur, comme partout
# ailleurs dans cette branche — c'est précisément ce qu'on a choisi de ne pas
# déparamétrer.
EPSG_TRAVAIL = 2154


class FournisseurFrance(Fournisseur):
    """France métropolitaine, LiDAR HD de l'IGN."""

    ID = "france_ign"
    NOM = "France — IGN LiDAR HD"
    PAYS = "FR"
    COUVERTURE = COUVERTURE_FRANCE

    def crs_travail(self, lon, lat):
        return EPSG_TRAVAIL

    def resolution_native(self, bbox_wgs84, journal=None):
        return RESOLUTION_NATIVE

    def fournit_terrain(self):
        # Faux à dessein : voir l'en-tête du module. Le terrain français passe
        # par `import_dalles/adaptateur.importer_raster`, inchangé.
        return False

    def occulte(self):
        # Le MNS de l'IGN contient déjà la canopée et les toitures : il n'y a
        # rien à graver. C'est toute la différence avec les États-Unis, qui ne
        # publient qu'un terrain nu.
        return False
