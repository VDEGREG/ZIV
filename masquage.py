# -*- coding: utf-8 -*-
"""Masquage de la ZIV par les forêts et les bâtiments.

Sur un MNS, le calcul de visibilité traite la canopée et les toitures comme des
surfaces élevées qui « voient » le projet. Personne ne se tient dans une cime ni
sur un faîtage : ces cellules sont des artefacts, et on les force à « hors
visibilité ».

Méthode : on grave la valeur 0 **directement** dans le raster, à partir des
empreintes BD Forêt et BDTOPO téléchargées sur l'emprise de la ZIV.

Trois choses à ne pas défaire, chèrement acquises :

  1. La gravure passe par l'API GDAL nue (voir `donnees_ign`), jamais par les
     enveloppes Processing `gdal:rasterize_over*`, qui faisaient planter QGIS
     dans le fil d'exécution de Processing.
  2. On grave **en place**, jamais un masque séparé recombiné au calculateur
     raster : `gdal:rasterize` marque le 0 comme sans-donnée, que le calculateur
     propageait ensuite sur toute la sortie — la ZIV entière devenait vide.
  3. Les deux GeoJSON téléchargés sont **laissés sur le disque** à côté de la
     ZIV : l'étape des points de vue photo les relit pour s'épargner un second
     appel au WFS. Un « nettoyage » zélé casserait cette économie.

Rien ici n'est bloquant : hors couverture, WFS indisponible, erreur GDAL — on
prévient et on rend la ZIV telle quelle. Une ZIV non masquée reste exploitable ;
pas de ZIV du tout, non.
"""

import os
import shutil
from pathlib import Path

from . import donnees_ign

# Valeur gravée : « hors visibilité ». La chaîne ZIV lit 0 comme non vu, 0.1 pour
# l'empreinte du projet et 1 et plus pour le visible.
HORS_VISIBILITE = 0.0

# Noms des empreintes, relus tels quels par l'étape des points de vue photo.
NOM_BATIMENTS = "_ziv_mask_batiments.geojson"
NOM_FORET = "_ziv_mask_foret.geojson"


class JournalMuet:
    def info(self, message):
        pass

    def warning(self, message):
        pass


def _emprise_du_raster(chemin):
    """Emprise (xmin, ymin, xmax, ymax) d'un raster, sans passer par QGIS."""
    from osgeo import gdal

    gdal.UseExceptions()
    try:
        jeu = gdal.Open(str(chemin))
    except Exception:
        # UseExceptions() fait lever au lieu de rendre None : sans ce filet,
        # un raster illisible remonterait au message generique, et on perdrait
        # la cause exacte au journal.
        return None
    if jeu is None:
        return None
    gt = jeu.GetGeoTransform()
    largeur, hauteur = jeu.RasterXSize, jeu.RasterYSize
    jeu = None
    xmin, ymax = gt[0], gt[3]
    return (xmin, ymax + hauteur * gt[5], xmin + largeur * gt[1], ymax)


def _compter_entites(chemin):
    """Nombre d'entités d'un fichier vecteur, 0 s'il est illisible.

    Le `None` d'`ogr.Open` ne suffit plus : depuis GDAL 3.7,
    `gdal.UseExceptions()` est un reglage GLOBAL du processus, et QGIS comme
    n'importe quel autre greffon peut l'avoir arme. `ogr.Open` LEVE alors sur
    un fichier illisible au lieu de rendre `None` — la promesse « 0 s'il est
    illisible » tombait, et le masquage entier avec elle.
    """
    from osgeo import ogr

    try:
        source = ogr.Open(str(chemin))
        if source is None:
            return 0
        couche = source.GetLayer(0)
        nombre = couche.GetFeatureCount() if couche is not None else 0
        source = None
        return int(nombre)
    except Exception:
        return 0


def _preparer_destination(chemin_ziv, chemin_demande, journal):
    """Où graver : une copie, ou la ZIV elle-même.

    Le piège que cette fonction existe pour éviter : quand l'utilisateur donne
    un chemin de sortie explicite, le modèle a **déjà** écrit la ZIV à ce
    chemin. Copier la source sur elle-même lève `SameFileError`, l'ancienne
    version l'attrapait et rendait la ZIV **non masquée** — silencieusement, sur
    tous les livrables exportés vers un fichier choisi. On grave donc sur place
    dans ce cas, au lieu de renoncer.

    Renvoie (destination, sur_place) ou (None, False) si la copie a échoué.
    """
    source = Path(chemin_ziv)
    destination = Path(chemin_demande) if chemin_demande else (
        source.parent / ("%s_masque.tif" % source.stem)
    )
    try:
        if destination.exists() and os.path.samefile(str(source), str(destination)):
            return destination, True
    except OSError:
        pass
    if str(destination) == str(source):
        return destination, True

    try:
        shutil.copyfile(str(source), str(destination))
    except Exception as erreur:
        journal.warning("Masquage : copie de la ZIV echouee (%s) - ZIV inchangee." % erreur)
        return None, False
    return destination, False


def masquer_foret_et_bati(chemin_ziv, chemin_demande="", journal=None,
                          dossier_travail=None, transport=None):
    """Grave « hors visibilité » sous les forêts et les bâtiments.

    Renvoie le chemin du raster masqué, ou celui de la ZIV d'origine si rien
    n'a pu être appliqué. Ne lève jamais.
    """
    journal = journal or JournalMuet()
    source = Path(chemin_ziv)
    if not source.is_file():
        return str(chemin_ziv)

    try:
        emprise = _emprise_du_raster(source)
        if emprise is None:
            journal.warning("Masquage foret/bati : raster ZIV illisible - ignore.")
            return str(chemin_ziv)

        dossier = Path(dossier_travail) if dossier_travail else source.parent
        destination, sur_place = _preparer_destination(source, chemin_demande, journal)
        if destination is None:
            return str(chemin_ziv)
        if sur_place:
            journal.info("Masquage : gravure directe dans la ZIV demandee (%s)."
                         % destination.name)

        gravees = 0
        for couche, nom in ((donnees_ign.COUCHE_BATIMENTS, NOM_BATIMENTS),
                            (donnees_ign.COUCHE_FORET, NOM_FORET)):
            empreintes = dossier / nom
            if not donnees_ign.telecharger_couche(
                    emprise, couche, empreintes, journal, transport):
                continue
            if _compter_entites(empreintes) == 0:
                continue
            if donnees_ign.graver_dans_raster(
                    str(destination), str(empreintes),
                    valeur=HORS_VISIBILITE, ajouter=False, journal=journal):
                gravees += 1
            else:
                journal.warning("Masquage : gravure de %s echouee." % nom)

        if gravees == 0:
            journal.info(
                "Masquage foret/bati : aucune empreinte appliquee "
                "(hors couverture ou WFS indisponible)."
            )
            # Sans gravure, la destination n'est qu'une copie : on ne la garde
            # que si l'appelant avait demande ce chemin.
            return str(destination) if chemin_demande else str(chemin_ziv)

        journal.info(
            "Masquage foret/bati applique (%d couche(s), gravure %s) -> %s"
            % (gravees, "sur place" if sur_place else "sur copie", destination)
        )
        return str(destination)
    except Exception as erreur:
        journal.warning("Masquage foret/bati echoue (%s) - ZIV inchangee." % erreur)
        return str(chemin_ziv)
