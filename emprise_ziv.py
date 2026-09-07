# -*- coding: utf-8 -*-
"""Emprise du projet et rayon d'analyse : calage, tampon, shapefile.

Trois services, tous autour de la même question : où calcule-t-on ?

  * `caler_sur_le_terrain` — remet l'emprise projet dans le système du MNS, et
    refuse si les deux ne se recoupent pas ;
  * `tampon_autour` — le disque d'analyse autour du projet ;
  * `ecrire_shapefile_du_rayon` — le trace sur disque, pour la carte.

Deux pièges de terrain, mesurés, dont ce module est la parade :

  1. **Le système de coordonnées.** Le modèle de traitement ne reprojette pas
     l'emprise. Une emprise en Web Mercator au lieu du Lambert 93 laissait le
     tampon à des centaines de kilomètres du MNS, et `gdal:merge` en faisait
     l'union : mesuré, **94 638 × 320 342 cellules, 113 Go**, pour un calcul de
     6 × 6 km. D'où le calage en amont, et le refus net quand il n'y a rien à
     calculer.
  2. **Le verrou de fichier Windows.** Un shapefile d'une exécution précédente,
     encore ouvert comme couche dans QGIS, ne peut pas être écrasé — c'est la
     cause numéro un des « shapefile non généré ». La parade n'est pas de
     réessayer, c'est de **ne jamais réutiliser le même nom** : l'horodatage
     dans le nom de fichier n'est pas cosmétique.
"""

import contextlib
from datetime import datetime
from pathlib import Path

EPSG_TRAVAIL = 2154
SEGMENTS_TAMPON = 64  # arrondi du disque : 64 segments par quart de cercle
NOM_ENTITE = "Rayon d'analyse"


class JournalMuet:
    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


# --------------------------------------------------------------- calage SCR


def caler_sur_le_terrain(source, chemin_terrain, journal=None):
    """Rend l'emprise projet exprimée dans le système du MNS.

    Trois issues, et l'appelant doit distinguer les trois :
      * l'objet reçu **tel quel** si son système est déjà le bon (aucune copie
        inutile) ;
      * une couche mémoire reprojetée ;
      * `None` si l'emprise ne recoupe pas le MNS — il n'y a alors rien à
        calculer, et le dire vaut mieux que produire un raster vide et énorme.
    """
    from qgis.core import (QgsCoordinateTransform, QgsFeature, QgsProject,
                           QgsRasterLayer, QgsVectorLayer)

    journal = journal or JournalMuet()
    try:
        terrain = QgsRasterLayer(str(chemin_terrain), "_terrain_controle")
        if not terrain.isValid():
            journal.warning(
                "Terrain illisible pour le calage de l'emprise : elle est transmise "
                "telle quelle."
            )
            return source

        couche = source
        if not isinstance(couche, QgsVectorLayer):
            couche = QgsVectorLayer(str(source), "emprise_projet", "ogr")
        if not couche.isValid():
            journal.warning("Emprise projet illisible : transmise telle quelle.")
            return source

        scr_terrain = terrain.crs()
        if couche.crs() != scr_terrain:
            journal.info(
                "Emprise projet en %s, terrain en %s : reprojection avant le calcul."
                % (couche.crs().authid(), scr_terrain.authid())
            )
            transformation = QgsCoordinateTransform(
                couche.crs(), scr_terrain, QgsProject.instance()
            )
            calee = QgsVectorLayer(
                "Polygon?crs=" + scr_terrain.authid(), "emprise_projet_calee", "memory"
            )
            calee.dataProvider().addAttributes(couche.fields())
            calee.updateFields()
            entites = []
            for entite in couche.getFeatures():
                geometrie = entite.geometry()
                if geometrie.isEmpty():
                    continue
                geometrie.transform(transformation)
                copie = QgsFeature(calee.fields())
                copie.setGeometry(geometrie)
                copie.setAttributes(entite.attributes())
                entites.append(copie)
            if not entites:
                journal.error("Emprise projet vide après reprojection.")
                return None
            calee.dataProvider().addFeatures(entites)
            calee.updateExtents()
            couche = calee

        if not couche.extent().intersects(terrain.extent()):
            journal.error(
                "L'emprise projet (%s) ne recoupe pas le terrain (%s), dans le même "
                "système de coordonnées. Rien à calculer : vérifier le système de "
                "coordonnées de la couche d'emprise."
                % (couche.extent().toString(0), terrain.extent().toString(0))
            )
            return None
        return couche
    except Exception as erreur:
        journal.warning(
            "Calage de l'emprise impossible (%s) : elle est transmise telle quelle."
            % erreur
        )
        return source


# -------------------------------------------------------------------- tampon


def geometrie_unie(source, journal=None):
    """Réunit toutes les entités de la couche en une seule géométrie."""
    journal = journal or JournalMuet()
    ensemble = None
    for entite in source.getFeatures():
        geometrie = entite.geometry()
        if geometrie.isEmpty():
            continue
        ensemble = geometrie if ensemble is None else ensemble.combine(geometrie)
    if ensemble is None or ensemble.isEmpty():
        journal.error("La couche d'emprise ne contient aucune géométrie exploitable.")
        return None
    return ensemble


def tampon_autour(geometrie, rayon, journal=None):
    """Disque d'analyse autour de l'emprise, en unités de la couche."""
    journal = journal or JournalMuet()
    if geometrie is None or geometrie.isEmpty():
        return None
    if rayon is None or float(rayon) <= 0:
        return geometrie
    tampon = geometrie.buffer(float(rayon), SEGMENTS_TAMPON)
    if tampon.isEmpty():
        journal.warning("Tampon de %.0f m vide : emprise conservée telle quelle." % float(rayon))
        return geometrie
    return tampon


def emprise_de_travail(source, rayon, journal=None):
    """Rectangle englobant de l'emprise élargie du rayon, en quadruplet.

    La reprojection éventuelle porte sur les **géométries** et non sur le
    rectangle : reprojeter quatre coins ne donne pas l'emprise de la
    reprojection dès que les méridiens convergent — et ils convergent de près
    de 4° dans l'est de la France.
    """
    ensemble = geometrie_unie(source, journal)
    if ensemble is None:
        return None
    rectangle = tampon_autour(ensemble, rayon, journal).boundingBox()
    return (rectangle.xMinimum(), rectangle.yMinimum(),
            rectangle.xMaximum(), rectangle.yMaximum())


# ------------------------------------------------------- shapefile du rayon


def _chemin_libre(dossier, rayon):
    """Nom de fichier daté, et libre.

    L'horodatage à la seconde suffit en usage réel — deux calculs de ZIV ne se
    suivent pas à moins d'une seconde. Mais il ne suffit pas toujours : le
    greffon d'origine pouvait alors réécrire par-dessus un fichier encore
    verrouillé. Un suffixe numérique ferme ce cas au lieu de le documenter.
    """
    horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = "rayon_analyse_%dm_%s" % (int(round(rayon)), horodatage)
    chemin = dossier / (base + ".shp")
    numero = 2
    while chemin.exists():
        chemin = dossier / ("%s_%d.shp" % (base, numero))
        numero += 1
    return chemin


def ecrire_shapefile_du_rayon(geometrie, dossier, rayon, journal=None):
    """Écrit le disque d'analyse en shapefile Lambert 93 daté.

    Renvoie le chemin en cas de succès, la chaîne vide sinon — jamais None,
    jamais d'exception. Le nom porte l'horodatage à la seconde : c'est ce qui
    évite le verrou Windows sur un shapefile encore ouvert dans QGIS.
    """
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransformContext,
                           QgsFeature, QgsField, QgsFields, QgsVectorFileWriter,
                           QgsWkbTypes)
    from qgis.PyQt.QtCore import QMetaType

    journal = journal or JournalMuet()
    if geometrie is None or geometrie.isEmpty():
        journal.error("Rayon d'analyse : géométrie vide, rien à écrire.")
        return ""

    dossier = Path(dossier)
    chemin = _chemin_libre(dossier, float(rayon))

    champs = QgsFields()
    champs.append(QgsField("name", QMetaType.Type.QString, len=50))
    champs.append(QgsField("radius_m", QMetaType.Type.Double))

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "ESRI Shapefile"
    options.fileEncoding = "UTF-8"

    ecrivain = None
    try:
        dossier.mkdir(parents=True, exist_ok=True)
        ecrivain = QgsVectorFileWriter.create(
            str(chemin), champs, QgsWkbTypes.Type.Polygon,
            QgsCoordinateReferenceSystem("EPSG:%d" % EPSG_TRAVAIL),
            QgsCoordinateTransformContext(), options,
        )
        if ecrivain is None:
            journal.error("Rayon d'analyse : QgsVectorFileWriter n'a rien rendu pour %s" % chemin)
            return ""
        if ecrivain.hasError() != QgsVectorFileWriter.WriterError.NoError:
            journal.error("Rayon d'analyse : création impossible (%s)" % ecrivain.errorMessage())
            return ""

        entite = QgsFeature(champs)
        entite.setGeometry(geometrie)
        entite.setAttributes([NOM_ENTITE, float(rayon)])
        if not ecrivain.addFeature(entite):
            # On continue : le fichier existe peut-être quand même, et le
            # contrôle final tranchera sur pièces plutôt que sur ce retour.
            journal.error("Rayon d'analyse : entité non ajoutée (%s)" % ecrivain.errorMessage())
    except Exception as erreur:
        journal.error("Rayon d'analyse : échec de l'écriture (%s)" % erreur)
        with contextlib.suppress(Exception):
            import traceback

            journal.error(traceback.format_exc())
        return ""
    finally:
        # Détruire l'écrivain force le vidage des tampons sur le disque. Sans
        # ce `del`, le fichier peut rester vide ou tronqué.
        with contextlib.suppress(Exception):
            del ecrivain

    if not chemin.exists() or chemin.stat().st_size == 0:
        journal.error("Rayon d'analyse : fichier absent ou vide après écriture (%s)" % chemin)
        return ""
    journal.info("Rayon d'analyse enregistré : %s" % chemin)
    return str(chemin)
