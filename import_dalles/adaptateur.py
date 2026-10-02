# -*- coding: utf-8 -*-
"""Adaptateur : branche le moteur d'importation des dalles dans ce greffon.

Le greffon garde tout son environnement — emprise et tampon, dossier de
travail, manifeste de dalles, chaînage ZIV, symbologie, masquage forêt/bâti,
chemin nuage de points. **Seule l'importation des produits raster change**
(MNT, MNS, MNH) : découverte, téléchargement, calage, mosaïque.

Ce que ce moteur apporte, mesuré le 2026-09-01 :

  * les dalles sont demandées au serveur **directement à la résolution de
    travail**. La chaîne d'origine rapatriait 0,50 m puis rééchantillonnait en
    `average` : à 2 m, c'est 15,3 Mo contre 1,0 Mo par kilomètre carré, soit
    **16 fois moins**, pour un résultat identique au millimètre (le serveur
    applique la même moyenne de blocs) ;
  * le géoréférencement des dalles IGN est **réécrit** : le GeoTIFF livré porte
    un système nommé « EPSG:2154 » sans code d'autorité, dont `IsSame` est faux
    — c'est le « non-standard CRS WKT » que ce greffon accusait d'avoir fait
    planter QGIS ;
  * une erreur du service renvoyée en **HTTP 200 avec du XML** est détectée à
    l'arrivée, et non trois étapes plus loin sur une mosaïque incohérente.

Le nuage de points brut (type 3) ne passe pas par ici : il ne se sert ni ne
s'assemble comme un raster, et son chemin d'origine est conservé tel quel.
"""

import contextlib
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .geoplateforme import (
    PRODUITS,
    RESOLUTION_NATIVE,
    ErreurGeoplateforme,
    compter_dalles,
    format_octets,
    lister_dalles,
    volume_estime,
)
from .mosaique import MOYENNE, construire_mosaique, statistiques
from .telechargement import elaguer_manifeste, telecharger_dalles

# Index des types du greffon : 0 MNT, 1 MNS, 2 MNH, 3 nuage de points.
PRODUIT_PAR_TYPE = {0: "MNT", 1: "MNS", 2: "MNH"}
TYPE_NUAGE = 3


def est_raster(type_donnee):
    return type_donnee in PRODUIT_PAR_TYPE


def dalles_geoplateforme(emprise, code_couche, journal=None):
    """Découverte des dalles, rendue dans la forme attendue par le greffon.

    `emprise` est un quadruplet Lambert 93. Le résultat garde les clés
    `url` / `name` / `geometry` / `properties` du code d'origine, pour que le
    filtrage géométrique et la sélection en aval continuent de fonctionner.
    """
    dalles = lister_dalles(emprise, code_couche)
    sortie = []
    for dalle in dalles:
        xmin, ymin, xmax, ymax = dalle.emprise
        sortie.append({
            "url": dalle.url,
            "name": dalle.nom,
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[xmin, ymin], [xmax, ymin], [xmax, ymax],
                                 [xmin, ymax], [xmin, ymin]]],
            },
            "properties": {"name": dalle.nom, "url": dalle.url,
                           "timestamp": dalle.horodatage},
        })
    if journal is not None:
        journal.info("Geoplateforme : %d dalle(s) trouvee(s) pour %s."
                     % (len(sortie), code_couche))
    return sortie


class _JournalGreffon:
    """Passe les messages du noyau dans le journal du greffon."""

    def __init__(self, logger):
        self.logger = logger

    def info(self, message):
        with contextlib.suppress(Exception):
            self.logger.info(message)

    def warning(self, message):
        with contextlib.suppress(Exception):
            self.logger.warning(message)


# Les dalles ne sont qu'un intermediaire : elles vivent dans le dossier
# temporaire du systeme, pas dans le dossier de travail du projet. Le nom est
# stable pour que le cache serve encore d'un calcul a l'autre.
DOSSIER_DALLES = "ziv_dalles"


def importer_raster(algorithme, dalles, dossier, type_donnee, resolution,
                    fusionner, charger_couches, emprise=None):
    """Importe les dalles raster et rend le dictionnaire attendu par le greffon.

    Renvoie `{"OUTPUT_DIRECTORY", "OUTPUT_FILE", "OUTPUT_FILES"}` — exactement
    ce que `_finalize_result` consomme, pour que la suite de la chaîne (ZIV,
    symbologie, masquage) ne voie aucune différence.
    """
    from .geoplateforme import Dalle, _emprise_depuis_url

    journal = _JournalGreffon(algorithme.logger)
    dossier = Path(dossier)
    dossier_dalles = Path(tempfile.gettempdir()) / DOSSIER_DALLES

    objets = []
    for brut in dalles:
        url = brut.get("url")
        nom = brut.get("name")
        if not url or not nom:
            continue
        emprise_dalle = _emprise_depuis_url(url)
        if emprise_dalle is None:
            continue
        objets.append(Dalle(nom=nom, url=url, emprise=emprise_dalle,
                            horodatage=str((brut.get("properties") or {}).get("timestamp", ""))))
    if not objets:
        raise ErreurGeoplateforme("Aucune dalle exploitable apres selection.")

    # Sans fusion, l'utilisateur veut les dalles elles-memes : on ne degrade
    # pas ce qu'il a demande. Avec fusion, la resolution de travail suffit.
    resolution_dalles = resolution if fusionner else RESOLUTION_NATIVE
    journal.info(
        "Dalles dans le dossier temporaire : %s" % dossier_dalles
    )
    journal.info(
        "Import des dalles : %d dalle(s) a %g m, environ %s%s."
        % (len(objets), resolution_dalles,
           format_octets(volume_estime(len(objets), resolution_dalles)),
           "" if fusionner else " (resolution native : aucune fusion demandee)")
    )

    chemins = telecharger_dalles(
        objets, dossier_dalles, resolution_dalles, journal=journal,
        est_annule=getattr(algorithme.feedback, "isCanceled", None),
    )
    chemins = [str(c) for c in chemins]
    for chemin in chemins:
        with contextlib.suppress(Exception):
            algorithme._record_tile_in_manifest(Path(chemin))

    # Les dalles ne sont jamais chargees dans le projet : ce sont des
    # intermediaires, et une centaine de couches grises masqueraient le fond de
    # carte sans rien apprendre. Seul le resultat merite d'etre affiche.
    _ = charger_couches

    resultat = {
        "OUTPUT_DIRECTORY": str(dossier),
        "OUTPUT_FILE": chemins[0] if chemins else "",
        "OUTPUT_FILES": ";".join(chemins),
    }
    if not fusionner or len(chemins) < 1:
        return resultat

    horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
    mosaique = dossier / ("merged_output_%s.tif" % horodatage)
    construire_mosaique(chemins, mosaique, agregation=MOYENNE,
                        resolution_cible=resolution, emprise=emprise,
                        journal=journal)
    infos = statistiques(mosaique)
    journal.info(
        "Mosaique : %d x %d cellules de %g m, altitudes de %.1f a %.1f m, SCR %s."
        % (infos["colonnes"], infos["lignes"], infos["pixel"], infos["min"],
           infos["max"], "conforme EPSG:2154" if infos["scr_conforme"] else "NON CONFORME")
    )
    if not infos["scr_conforme"]:
        journal.warning(
            "Le MNS produit n'est pas exactement en EPSG:2154 : a verifier avant la ZIV."
        )
    resultat["OUTPUT_FILE"] = str(mosaique)
    # La mosaique est faite : les dalles n'ont plus aucune utilite. On les
    # efface tout de suite plutot que de laisser des centaines de mega-octets
    # s'accumuler dans le dossier temporaire. En cas d'echec de la fusion, au
    # contraire, on les garde : une reprise ne doit pas tout retelecharger.
    resultat["OUTPUT_FILES"] = ""
    _effacer_dalles(chemins, journal, dossier_dalles)
    return resultat


def _effacer_dalles(chemins, journal, dossier_dalles):
    """Supprime les dalles intermediaires et dit combien de place a ete rendue.

    Le manifeste du cache est elague dans le meme geste : il ne doit nommer que
    des dalles encore sur le disque, sans quoi il cumule toutes les executions
    passees et se lit comme le compte de ce qui vient d'etre fusionne.
    """
    octets, efface = 0, 0
    for chemin in chemins:
        with contextlib.suppress(OSError):
            octets += os.path.getsize(chemin)
            os.remove(chemin)
            efface += 1
    if efface:
        journal.info(
            "%d dalle(s) temporaire(s) effacee(s) apres la fusion, %s rendus."
            % (efface, format_octets(octets))
        )
    retirees = elaguer_manifeste(dossier_dalles)
    if retirees:
        journal.info("Manifeste du cache : %d entree(s) sans fichier retiree(s)."
                     % retirees)
