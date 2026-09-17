# -*- coding: utf-8 -*-
"""Outils partagés par les deux algorithmes de visibilité du greffon.

Les noms de champs sont ceux de la version d'origine : une couche de points de
vue déjà produite doit rester utilisable, et `creation_ziv.model3` ne connaît
que ceux-là.
"""

import numpy as np
from qgis.core import QgsProcessingException

CHAMP_ID = "ID"
CHAMP_HAUTEUR = "observ_hgt"
CHAMP_RAYON = "radius"
CHAMP_TARGET = "targ_hgt"
CHAMP_RAYON_IN = "radius_in"
CHAMP_AZIM_1 = "azim_1"
CHAMP_AZIM_2 = "azim_2"
CHAMP_ANGLE_UP = "angle_up"
CHAMP_ANGLE_DOWN = "angle_down"

CHAMPS_OBLIGATOIRES = (CHAMP_HAUTEUR, CHAMP_RAYON)


def charger_mnt(couche):
    """Ouvre le terrain d'une couche raster QGIS au format du moteur."""
    from ..moteur_visibilite.mnt import Mnt

    source = couche.source().split("|")[0]
    try:
        mnt = Mnt.depuis_fichier(source)
    except Exception as erreur:
        raise QgsProcessingException(
            "Terrain illisible par GDAL (« %s ») : %s\nL'exporter en GeoTIFF "
            "puis relancer." % (source, erreur)
        )
    if couche.crs().isValid():
        mnt.crs_wkt = couche.crs().toWkt()
    return mnt


def valeur_champ(entite, index, nom_champ, defaut):
    """Valeur d'un champ désigné, ou la valeur par défaut s'il est absent ou vide."""
    if not nom_champ:
        return defaut
    position = index.get(str(nom_champ).lower())
    if position is None:
        return defaut
    valeur = entite.attribute(position)
    if valeur is None:
        return defaut
    try:
        valeur = float(valeur)
    except (TypeError, ValueError):
        return valeur if isinstance(valeur, str) else defaut
    return defaut if not np.isfinite(valeur) else valeur


def champs_manquants(champs, requis=CHAMPS_OBLIGATOIRES):
    presents = {nom.lower() for nom in champs.names()}
    return [nom for nom in requis if nom.lower() not in presents]
