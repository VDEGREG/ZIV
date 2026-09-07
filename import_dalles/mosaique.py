# -*- coding: utf-8 -*-
"""Assemblage des dalles en une mosaïque, et agrégation.

Deux façons d'obtenir un MNS à la résolution voulue, et elles ne donnent pas le
même terrain :

  * **moyenne** — les dalles sont demandées directement à la bonne résolution,
    le serveur moyennant les pixels natifs. C'est ce que fait la chaîne ZIV
    d'origine (`gdal:warpreproject` en `average`), à ceci près qu'ici le
    sous-échantillonnage a lieu **avant** le transfert : 16 fois moins de
    volume pour 2 m, résultat identique (vérifié : écart nul).
  * **maximum** — les dalles sont téléchargées à leur résolution native et
    agrégées localement en prenant le point le plus haut de chaque bloc. Sur un
    MNS, la moyenne rabote les cimes et les faîtages : mesuré, 0,93 m en
    moyenne et jusqu'à 24 m. Pour une zone d'influence visuelle, ces mètres-là
    sont précisément les masques.

Le choix appartient à l'utilisateur ; le défaut reste la moyenne, pour ne pas
changer en silence un résultat déjà rendu.
"""

import math

import numpy as np

from .geoplateforme import EPSG_TRAVAIL, ErreurGeoplateforme

SANS_DONNEE = -9999.0
MOYENNE, MAXIMUM = "moyenne", "maximum"

# Options d'écriture : DEFLATE + prédicteur flottant. Les dalles arrivent non
# compressées ; la mosaïque n'a aucune raison de le rester sur le disque.
_CREATION = ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES", "BIGTIFF=IF_SAFER"]


class JournalMuet:
    def info(self, message):
        pass

    def warning(self, message):
        pass


def construire_mosaique(chemins, sortie, agregation=MOYENNE, resolution_cible=None,
                        emprise=None, journal=None):
    """Assemble les dalles et écrit la mosaïque. Renvoie le chemin de sortie."""
    from osgeo import gdal

    gdal.UseExceptions()
    journal = journal or JournalMuet()
    chemins = [str(c) for c in chemins]
    if not chemins:
        raise ErreurGeoplateforme("Aucune dalle à assembler.")

    vrt = gdal.BuildVRT("", chemins, VRTNodata=SANS_DONNEE, srcNodata=SANS_DONNEE)
    if vrt is None:
        raise ErreurGeoplateforme("Assemblage impossible : GDAL n'a pas construit le VRT.")

    geotransform = vrt.GetGeoTransform()
    pixel = abs(geotransform[1])
    journal.info("Mosaique : %d dalle(s), %d x %d cellules de %g m."
                 % (len(chemins), vrt.RasterXSize, vrt.RasterYSize, pixel))

    if agregation == MAXIMUM:
        if resolution_cible is None:
            raise ErreurGeoplateforme("L'agrégation maximum exige une résolution cible.")
        intermediaire = _agreger_maximum(vrt, resolution_cible, journal)
        vrt = None
        source = intermediaire
    elif agregation == MOYENNE:
        source = vrt
    else:
        raise ErreurGeoplateforme("Agrégation inconnue : %r" % agregation)

    options = {"format": "GTiff", "creationOptions": _CREATION, "noData": SANS_DONNEE}
    if emprise is not None:
        # projWin s'exprime (xmin, ymax, xmax, ymin) et se cale sur les bords de
        # pixel : la grille de la mosaïque n'est pas déplacée par la découpe.
        options["projWin"] = [emprise[0], emprise[3], emprise[2], emprise[1]]
        options["projWinSRS"] = "EPSG:%d" % EPSG_TRAVAIL

    resultat = gdal.Translate(str(sortie), source, **options)
    if resultat is None:
        raise ErreurGeoplateforme("Écriture de la mosaïque impossible : %s" % sortie)
    bande = resultat.GetRasterBand(1)
    bande.SetNoDataValue(SANS_DONNEE)
    bande.ComputeStatistics(False)
    resultat = None
    source = None
    return str(sortie)


def _agreger_maximum(vrt, resolution_cible, journal, lignes_par_bloc=512):
    """Réduit le VRT natif à la résolution cible en gardant le point le plus haut.

    Travaillé par bandes : une mosaïque de 10 x 10 km à 0,50 m fait 400 millions
    de cellules, qu'on ne charge pas d'un bloc.
    """
    from osgeo import gdal, osr

    geotransform = vrt.GetGeoTransform()
    pixel = abs(geotransform[1])
    facteur = int(round(resolution_cible / pixel))
    if facteur < 1:
        raise ErreurGeoplateforme(
            "Résolution cible (%g m) plus fine que les dalles (%g m)."
            % (resolution_cible, pixel)
        )
    if abs(facteur * pixel - resolution_cible) > 1e-6:
        journal.warning(
            "Résolution ajustée à %g m : elle doit être un multiple entier de "
            "la maille des dalles (%g m)." % (facteur * pixel, pixel)
        )
    effective = facteur * pixel

    n_col = int(math.ceil(vrt.RasterXSize / facteur))
    n_lig = int(math.ceil(vrt.RasterYSize / facteur))
    journal.info("Agregation par maximum : blocs de %d x %d cellules -> %g m, %d x %d."
                 % (facteur, facteur, effective, n_col, n_lig))

    memoire = gdal.GetDriverByName("MEM").Create("", n_col, n_lig, 1, gdal.GDT_Float32)
    memoire.SetGeoTransform((geotransform[0], effective, 0.0,
                             geotransform[3], 0.0, -effective))
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(EPSG_TRAVAIL)
    memoire.SetSpatialRef(reference)
    bande_sortie = memoire.GetRasterBand(1)
    bande_sortie.SetNoDataValue(SANS_DONNEE)

    bande_source = vrt.GetRasterBand(1)
    sans_donnee_source = bande_source.GetNoDataValue()
    for debut in range(0, n_lig, lignes_par_bloc):
        hauteur_sortie = min(lignes_par_bloc, n_lig - debut)
        y = debut * facteur
        hauteur_lue = min(hauteur_sortie * facteur, vrt.RasterYSize - y)
        donnees = bande_source.ReadAsArray(0, y, vrt.RasterXSize, hauteur_lue)
        donnees = np.asarray(donnees, dtype=np.float32)
        if sans_donnee_source is not None:
            donnees = np.where(donnees == np.float32(sans_donnee_source), np.nan, donnees)

        # Compléter le dernier bloc pour que le remodelage tombe juste.
        pad_y = hauteur_sortie * facteur - donnees.shape[0]
        pad_x = n_col * facteur - donnees.shape[1]
        if pad_y or pad_x:
            donnees = np.pad(donnees, ((0, max(pad_y, 0)), (0, max(pad_x, 0))),
                             constant_values=np.nan)

        blocs = donnees.reshape(hauteur_sortie, facteur, n_col, facteur)
        with np.errstate(invalid="ignore"):
            reduit = np.nanmax(blocs, axis=(1, 3))
        reduit = np.where(np.isfinite(reduit), reduit, SANS_DONNEE).astype(np.float32)
        bande_sortie.WriteArray(reduit, 0, debut)

    bande_sortie.FlushCache()
    return memoire


def statistiques(chemin):
    """Ce qu'un raster contient vraiment : de quoi juger sur pièces."""
    from osgeo import gdal, osr

    gdal.UseExceptions()
    jeu = gdal.Open(str(chemin))
    if jeu is None:
        raise ErreurGeoplateforme("Raster illisible : %s" % chemin)
    bande = jeu.GetRasterBand(1)
    geotransform = jeu.GetGeoTransform()
    attendu = osr.SpatialReference()
    attendu.ImportFromEPSG(EPSG_TRAVAIL)
    scr = jeu.GetSpatialRef()
    minimum, maximum, moyenne, ecart = bande.ComputeStatistics(False)
    infos = {
        "colonnes": jeu.RasterXSize,
        "lignes": jeu.RasterYSize,
        "pixel": abs(geotransform[1]),
        "origine": (geotransform[0], geotransform[3]),
        "sans_donnee": bande.GetNoDataValue(),
        "type": gdal.GetDataTypeName(bande.DataType),
        "min": minimum,
        "max": maximum,
        "moyenne": moyenne,
        "ecart_type": ecart,
        "scr_conforme": bool(scr is not None and scr.IsSame(attendu)),
        "scr_nom": scr.GetName() if scr is not None else None,
    }
    jeu = None
    return infos
