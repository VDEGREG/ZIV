# -*- coding: utf-8 -*-
"""Géométrie de la visibilité : courbure terrestre, réfraction, azimuts.

Ce module n'importe ni QGIS ni GDAL : il doit rester testable seul.
Toutes les distances sont en unités de la carte (mètres pour un SCR projeté).
"""

import numpy as np

# Rayon de la sphère d'autalie, valeur retenue par GDAL et GRASS pour les
# corrections de courbure. Un rayon « exact » n'a pas de sens ici : l'écart
# entre les rayons polaire et équatorial pèse moins que la réfraction.
RAYON_TERRE = 6370997.0

# Coefficient de réfraction atmosphérique standard. 0,13 est la valeur usuelle
# en topographie (l'air courbe le rayon lumineux vers le bas, ce qui fait voir
# un peu plus loin que la géométrie pure).
REFRACTION_STANDARD = 0.13


def abaissement(distance, refraction=REFRACTION_STANDARD, rayon_terre=RAYON_TERRE):
    """Abaissement apparent (m) d'un point situé à `distance`, réfraction déduite.

    À soustraire de l'altitude du terrain avant tout calcul d'angle.
    """
    return (1.0 - refraction) * np.square(distance) / (2.0 * rayon_terre)


def distance_horizon(hauteur, refraction=REFRACTION_STANDARD, rayon_terre=RAYON_TERRE):
    """Distance (m) à laquelle le sol plat disparaît sous l'horizon.

    Inverse analytique de `abaissement` : sert d'oracle de contrôle.
    """
    return np.sqrt(2.0 * rayon_terre * np.asarray(hauteur, dtype=float) / (1.0 - refraction))


def azimut(dx, dy):
    """Azimut en degrés depuis le nord, sens horaire, pour un vecteur carte."""
    return np.degrees(np.arctan2(dx, dy)) % 360.0


def dans_secteur(az, az_min, az_max):
    """Masque des azimuts compris dans [az_min, az_max], enroulement géré.

    Un secteur 350 → 10 couvre bien les 20° autour du nord.
    """
    if az_max - az_min >= 360.0:
        return np.ones(np.shape(az), dtype=bool)
    a0 = az_min % 360.0
    a1 = az_max % 360.0
    if a0 <= a1:
        return (az >= a0) & (az <= a1)
    return (az >= a0) | (az <= a1)
