# -*- coding: utf-8 -*-
"""Moteur de visibilité : propagation d'horizon par secteurs.

Principe
--------
Depuis l'observateur, la grille est parcourue par anneaux carrés croissants,
découpée en quatre secteurs (est, ouest, nord, sud) selon l'axe dominant.
Chaque secteur avance colonne par colonne le long de son axe majeur ; on garde
pour chaque cellule de la colonne courante l'angle du **plan de référence** :
la plus haute ligne de visée rencontrée jusque-là.

Pour une cellule (i, j), la ligne de visée depuis l'observateur traverse la
colonne j-1 en i·(j-1)/j, c'est-à-dire **entre** deux cellules connues : son
horizon est l'interpolation linéaire des deux horizons voisins. La cellule est
visible si son angle dépasse cet horizon interpolé.

Ce parcours traite **chaque cellule exactement une fois** : pas de trou, pas de
recouvrement entre secteurs. C'est la différence avec le lancer de rayons, qui
laisse des cellules non atteintes dès que le rayon dépasse quelques centaines
de pixels.

Le recul
--------
Une propagation d'horizon purement récursive **fuit** : à chaque colonne, le
maximum se diffuse d'une cellule de côté, si bien que l'ombre d'un obstacle
s'élargit plus que la géométrie ne le permet, et de plus en plus loin. Mesuré
face à un oracle en force brute, ce défaut coûtait 5 % de surface vue sur un
relief de 2,9 m par cellule.

D'où le **recul** : au lieu de ne lire que l'horizon récursif de la colonne
précédente, on recalcule exactement l'angle du terrain sous la ligne de visée
pour les `recul` dernières colonnes, et on ne reprend la mémoire longue qu'au
delà. La fuite est bornée à ce qui a diffusé au-delà de `recul` colonnes, et
l'horizon corrigé est réinjecté dans la chaîne récursive — qui diffuse alors un
maximum déjà moins étalé. Le coût est proportionnel à `recul` et **indépendant
du rayon**. Mesures : recul 1 → 99,10 % d'accord avec l'oracle ; recul 2 →
99,40 % ; recul 8 → 99,63 % ; recul complet → 99,72 %.

Les angles sont manipulés sous forme de **tangentes** (dénivelé / distance)
partout où seule la comparaison compte : c'est monotone, donc équivalent, et
cela économise un arc-tangente par cellule.
"""

from dataclasses import dataclass

import numpy as np

from .geometrie import REFRACTION_STANDARD, abaissement, azimut, dans_secteur

# Horizon initial : « rien ne bloque encore ». Valeur finie et non -inf, sinon
# l'interpolation (1-w)·H0 + w·H1 produit un nan dès que w vaut exactement 0.
HORIZON_INITIAL = -1.0e30

# Les quatre secteurs : (nom, pas majeur en pixels, pas mineur, diagonales incluses).
# Est et ouest gardent les diagonales, nord et sud les excluent : chaque cellule
# de la grille appartient donc à un seul secteur, sans trou ni recouvrement.
_SECTEURS = (
    ("est", (0, 1), (-1, 0), True),
    ("ouest", (0, -1), (1, 0), True),
    ("nord", (-1, 0), (0, 1), False),
    ("sud", (1, 0), (0, -1), False),
)


class ObservateurHorsMnt(ValueError):
    """L'observateur tombe hors du MNT ou sur une cellule sans donnée."""


@dataclass
class Observateur:
    """Un point de vue et ses paramètres d'analyse."""

    x: float
    y: float
    h_obs: float = 1.6
    h_cible: float = 0.0
    rayon: float = 5000.0
    rayon_min: float = 0.0
    azimut_min: float = 0.0
    azimut_max: float = 360.0
    poids: float = 1.0
    identifiant: int = 0
    z_sol: float = None  # None : altitude lue dans le MNT


@dataclass
class Resultat:
    """Sortie d'un calcul, exprimée sur une fenêtre du MNT.

    `visible` : la cible est vue depuis l'observateur.
    `marge`   : angle de la cible moins angle de l'horizon, en degrés. Positif,
                donc visible ; négatif, c'est la « profondeur sous l'horizon » :
                de combien il faudrait relever la cible pour la faire paraître.
    `valide`  : cellule dans l'anneau **[rayon_min, rayon[** — borne
                exterieure STRICTE, interieure inclusive, donc deux anneaux
                adjacents pavent le disque sans trou ni recouvrement —, dans le secteur
                azimutal, et pourvue d'une altitude. Ailleurs rien n'a été
                calculé, et la sortie doit être sans-donnée.
    """

    visible: np.ndarray
    marge: np.ndarray
    valide: np.ndarray
    lig0: int
    col0: int
    lig_obs: int
    col_obs: int
    z_obs: float


RECUL_DEFAUT = 8


def calculer(mnt, obs, courbure=True, refraction=REFRACTION_STANDARD,
             recul=RECUL_DEFAUT, unite_marge="degres", calcul_marge=True):
    """Calcule la visibilité depuis `obs` sur `mnt`.

    `recul` : nombre de colonnes recalculées exactement avant de reprendre la
    mémoire longue. 1 = propagation purement récursive (le plus rapide, le plus
    fuyant), 8 = réglage par défaut, 32 = fin. Voir l'en-tête du module.

    `unite_marge` : unité du champ `marge` du résultat.
      - « degres »       : angle de la cible moins angle de l'horizon. Positif
                           donc visible, négatif donc caché.
      - « profondeur_m » : 0 si la cible est vue, sinon le nombre de mètres
                           qu'il faudrait lui ajouter pour qu'elle apparaisse.
                           C'est la convention du greffon d'origine.

    `calcul_marge` : faux quand seule la visibilité compte — `marge` vaut
    alors None, et le calcul épargne deux arc-tangentes par cellule ainsi
    qu'un tableau de la taille de la fenêtre.
    """
    if unite_marge not in ("degres", "profondeur_m"):
        raise ValueError("unite_marge inconnue : %r" % unite_marge)
    rc = mnt.cellule(obs.x, obs.y)
    if rc is None:
        raise ObservateurHorsMnt(
            "Observateur %s hors de l'emprise du MNT (x=%.3f, y=%.3f)."
            % (obs.identifiant, obs.x, obs.y)
        )
    lig_obs, col_obs = rc
    z_sol = float(mnt.z[lig_obs, col_obs]) if obs.z_sol is None else float(obs.z_sol)
    if not np.isfinite(z_sol):
        raise ObservateurHorsMnt(
            "Observateur %s posé sur une cellule sans donnée du MNT." % obs.identifiant
        )
    z_obs = z_sol + obs.h_obs

    fenetre = mnt.fenetre_disque(obs.x, obs.y, obs.rayon)
    if fenetre is None:
        raise ObservateurHorsMnt(
            "Le disque d'analyse de l'observateur %s ne recoupe pas le MNT."
            % obs.identifiant
        )
    lig0, col0, n_lig, n_col = fenetre

    visible = np.zeros((n_lig, n_col), dtype=bool)
    marge = (np.full((n_lig, n_col), np.nan, dtype=np.float32) if calcul_marge
             else None)
    valide = _masque_valide(mnt, obs, lig0, col0, n_lig, n_col, lig_obs, col_obs)

    for _nom, majeur, mineur, diagonales in _SECTEURS:
        _parcourir_secteur(
            mnt, obs, z_obs, lig_obs, col_obs, lig0, col0,
            visible, marge, majeur, mineur, diagonales, courbure, refraction,
            max(int(recul), 1), unite_marge,
        )

    # La cellule de l'observateur : distance nulle, donc aucun angle défini.
    # Elle est vue par construction dès lors qu'elle est dans l'anneau.
    lo, co = lig_obs - lig0, col_obs - col0
    if 0 <= lo < n_lig and 0 <= co < n_col and valide[lo, co]:
        visible[lo, co] = True
        if marge is not None:
            marge[lo, co] = 90.0 if unite_marge == "degres" else 0.0

    visible &= valide
    if marge is not None:
        marge[~valide] = np.nan
    return Resultat(visible, marge, valide, lig0, col0, lig_obs, col_obs, z_obs)


def _masque_valide(mnt, obs, lig0, col0, n_lig, n_col, lig_obs, col_obs):
    """Cellules réellement soumises au calcul (anneau, secteur, donnée présente)."""
    ecart_x = (np.arange(n_col, dtype=np.float32) + (col0 - col_obs)) * np.float32(mnt.dx)
    ecart_y = (np.float32(lig_obs - lig0) - np.arange(n_lig, dtype=np.float32)) * np.float32(mnt.dy)
    distance2 = np.square(ecart_y)[:, None] + np.square(ecart_x)[None, :]

    # Inegalite STRICTE, et non `<=` : une cellule situee exactement a la
    # distance du rayon est HORS analyse. Convention du moteur de reference,
    # deduite par la mesure le 2026-09-02 : 33 692 cellules valides sur la
    # scene synthetique du banc, contre 33 697 avec `<=`. Les 5 cellules
    # d'ecart sont celles pile a 1500 m (points d'axe, triplets
    # pythagoriciens type 900/1200). Ne pas « corriger » sans rejouer
    # ZIV-module/tests/critere2_scene_synthetique.py.
    valide = distance2 < np.float32(obs.rayon ** 2)
    if obs.rayon_min > 0:
        valide &= distance2 >= np.float32(obs.rayon_min ** 2)
    valide &= np.isfinite(mnt.z[lig0:lig0 + n_lig, col0:col0 + n_col])

    if obs.azimut_max - obs.azimut_min < 360.0:
        az = azimut(ecart_x[None, :], ecart_y[:, None])
        valide &= dans_secteur(az, obs.azimut_min, obs.azimut_max)
    return valide


def _parcourir_secteur(mnt, obs, z_obs, lig_obs, col_obs, lig0, col0,
                       visible, marge, majeur, mineur, diagonales,
                       courbure, refraction, recul, unite_marge):
    """Propage l'horizon le long d'un des quatre secteurs.

    `marge` à None : seule la visibilité est demandée, la marge n'est pas
    calculée.
    """
    pas_j = mnt.dx if majeur[1] != 0 else mnt.dy
    pas_i = mnt.dx if mineur[1] != 0 else mnt.dy
    n_j = int(obs.rayon // pas_j)
    if n_j < 1:
        return

    z = mnt.z
    n_lig_mnt, n_col_mnt = z.shape
    n_lig, n_col = visible.shape
    # Colonnes gardées en mémoire pour le recul : altitudes **brutes** (la
    # correction de courbure dépend de la distance du point d'interception, pas
    # de celle des cellules) et horizons. Les altitudes vivent dans un tampon
    # TOURNANT, une ligne par colonne calée à gauche (la colonne c occupe ses
    # 2c+1 premières cases) : le recul les relit d'un seul geste. `recul + 2`
    # lignes suffisent — on n'en relit jamais que les `recul` précédentes.
    tours = recul + 2
    altitudes = np.full((tours, 2 * n_j + 1), np.nan, dtype=np.float64)
    horizons = {}

    for j in range(1, n_j + 1):
        i = np.arange(-j, j + 1, dtype=np.int64)
        lignes = lig_obs + j * majeur[0] + i * mineur[0]
        colonnes = col_obs + j * majeur[1] + i * mineur[1]

        dedans = (
            (lignes >= 0) & (lignes < n_lig_mnt)
            & (colonnes >= 0) & (colonnes < n_col_mnt)
        )
        z_brut = np.full(i.shape, np.nan, dtype=np.float64)
        if dedans.any():
            z_brut[dedans] = z[lignes[dedans], colonnes[dedans]]
        altitudes[j % tours, :2 * j + 1] = z_brut

        distance = np.hypot(j * pas_j, i * pas_i)
        z_cellule = z_brut - abaissement(distance, refraction) if courbure else z_brut

        denivele = z_cellule - z_obs
        tan_terrain = denivele / distance
        tan_cible = (denivele + obs.h_cible) / distance

        tan_horizon = _horizon_avec_recul(
            i, j, recul, altitudes, horizons, z_obs, pas_j, pas_i, courbure, refraction
        )
        # Horizon transmis à la colonne suivante — on y réinjecte l'horizon
        # corrigé, pour que la mémoire longue diffuse un maximum moins étalé.
        # `fmax` ignore les nan : une cellule sans donnée laisse passer
        # l'horizon au lieu de le casser.
        horizons[j] = np.fmax(tan_terrain, tan_horizon)
        for vieux in (j - recul - 1, j - recul - 2):
            horizons.pop(vieux, None)

        vu = tan_cible >= tan_horizon  # nan >= x vaut False : sans donnée = non vu
        ecrit = dedans & (distance <= obs.rayon)
        if not diagonales:
            ecrit &= np.abs(i) < j
        if not ecrit.any():
            continue

        lw = lignes[ecrit] - lig0
        cw = colonnes[ecrit] - col0
        interne = (lw >= 0) & (lw < n_lig) & (cw >= 0) & (cw < n_col)
        lw, cw = lw[interne], cw[interne]
        visible[lw, cw] = vu[ecrit][interne]
        if marge is None:
            continue
        cible_ecrite = tan_cible[ecrit][interne]
        horizon_ecrit = tan_horizon[ecrit][interne]
        if unite_marge == "degres":
            marge[lw, cw] = np.degrees(np.arctan(cible_ecrite) - np.arctan(horizon_ecrit))
        else:
            # Mètres manquants sous l'horizon : 0 dès que la cible passe. Le
            # `maximum` évite aussi de multiplier l'horizon initial (-1e30) par
            # la distance, qui déborderait.
            marge[lw, cw] = (
                np.maximum(0.0, horizon_ecrit - cible_ecrite) * distance[ecrit][interne]
            )


def _interpoler_colonne(valeurs, demi_largeur, t):
    """Interpole `valeurs`, indexées de -demi_largeur à +demi_largeur, aux positions t.

    La ligne de visée d'une cellule traverse une colonne antérieure **entre**
    deux cellules : c'est là qu'il faut lire, pas sur la plus proche.
    """
    i0 = np.floor(t).astype(np.int64)
    poids = t - i0
    dernier = 2 * demi_largeur
    k0 = np.clip(i0 + demi_largeur, 0, dernier)
    k1 = np.clip(i0 + 1 + demi_largeur, 0, dernier)
    return (1.0 - poids) * valeurs[k0] + poids * valeurs[k1]


def _horizon_avec_recul(i, j, recul, altitudes, horizons, z_obs,
                        pas_j, pas_i, courbure, refraction):
    """Horizon vu par les cellules de la colonne j.

    Recalcul exact sur les `recul` dernières colonnes — le terrain y est lu
    sous la ligne de visée elle-même — puis reprise de la mémoire longue
    au-delà. Voir l'en-tête du module pour le pourquoi.
    """
    tan_horizon = np.full(i.shape, HORIZON_INITIAL, dtype=np.float64)
    profondeur = min(recul, j - 1)
    if profondeur:
        # Les `profondeur` colonnes d'un seul geste, sur un tableau
        # profondeur x (2j+1) : ce sont, cellule par cellule, les opérations
        # de la boucle d'origine, et le maximum ne dépend pas de l'ordre. Le
        # résultat est donc identique au bit près (vérifié sur les rasters
        # figés du banc) ; seuls disparaissent ~128 petits appels numpy par
        # colonne, qui faisaient l'essentiel du temps.
        colonnes = np.arange(j - 1, j - profondeur - 1, -1, dtype=np.int64)
        t = i[None, :] * (colonnes / j)[:, None]
        i0 = np.floor(t).astype(np.int64)
        poids = t - i0
        dernier = (2 * colonnes)[:, None]
        k0 = np.clip(i0 + colonnes[:, None], 0, dernier)
        k1 = np.clip(i0 + 1 + colonnes[:, None], 0, dernier)
        rangs = (colonnes % altitudes.shape[0])[:, None]
        z_vise = (1.0 - poids) * altitudes[rangs, k0] + poids * altitudes[rangs, k1]
        distance = np.hypot((colonnes * pas_j)[:, None], t * pas_i)
        if courbure:
            z_vise = z_vise - abaissement(distance, refraction)
        tan_horizon = np.fmax(tan_horizon,
                              np.fmax.reduce((z_vise - z_obs) / distance, axis=0))

    if j - 1 > profondeur:
        colonne = j - profondeur
        tan_horizon = np.fmax(
            tan_horizon,
            _interpoler_colonne(horizons[colonne], colonne, i * (colonne / j)),
        )
    return tan_horizon


def horizon_visible(resultat):
    """Cellules de crête : vues, mais dont la voisine vers l'extérieur ne l'est pas.

    Définition purement raster : pour chaque cellule vue, on regarde la cellule
    suivante en s'éloignant de l'observateur. Si elle est calculée et non vue,
    la cellule courante est sur une ligne d'horizon.
    """
    visible = resultat.visible
    n_lig, n_col = visible.shape
    lig, col = np.nonzero(visible)
    if lig.size == 0:
        return np.zeros_like(visible)
    di = lig - (resultat.lig_obs - resultat.lig0)
    dj = col - (resultat.col_obs - resultat.col0)
    norme = np.hypot(di, dj)
    norme[norme == 0] = 1.0
    lig_ext = lig + np.rint(di / norme).astype(np.int64)
    col_ext = col + np.rint(dj / norme).astype(np.int64)

    dedans = (lig_ext >= 0) & (lig_ext < n_lig) & (col_ext >= 0) & (col_ext < n_col)
    sortie = np.zeros_like(visible)
    lig, col = lig[dedans], col[dedans]
    lig_ext, col_ext = lig_ext[dedans], col_ext[dedans]
    sortie[lig, col] = resultat.valide[lig_ext, col_ext] & ~visible[lig_ext, col_ext]
    return sortie


def ligne_de_vue(mnt, x1, y1, z1, x2, y2, z2, courbure=True,
                 refraction=REFRACTION_STANDARD, pas=0.5):
    """Test de visibilité entre deux points, altitudes absolues.

    Renvoie (visible, marge_m, distance) : `marge_m` est le dégagement vertical
    minimal entre la ligne de visée et le terrain, en mètres. Négatif, c'est de
    combien le terrain dépasse au point le plus contraignant.
    """
    distance = float(np.hypot(x2 - x1, y2 - y1))
    if distance == 0.0:
        return True, float("inf"), 0.0
    pas_m = max(min(mnt.dx, mnt.dy) * pas, 1e-6)
    n = max(int(np.ceil(distance / pas_m)), 2)
    t = np.linspace(0.0, 1.0, n + 1)[1:-1]  # extrémités exclues
    if t.size == 0:
        return True, float("inf"), distance

    xs = x1 + (x2 - x1) * t
    ys = y1 + (y2 - y1) * t
    z_terrain = mnt.altitude_interpolee(xs, ys)
    if courbure:
        z_terrain = z_terrain - abaissement(distance * t, refraction)
    z_visee = z1 + (z2 - z1) * t
    degagement = z_visee - z_terrain
    if np.all(np.isnan(degagement)):
        return False, float("nan"), distance
    marge = float(np.nanmin(degagement))
    return bool(marge >= 0.0), marge, distance
