# -*- coding: utf-8 -*-
"""Terrain américain : USGS 3DEP, découpé, borné, et remis d'aplomb.

Ce module ne connaît ni QGIS ni le reste du greffon : il interroge deux services
ArcGIS et rend des objets `Dalle` que le cache de `import_dalles/` sait déjà
télécharger. Le transport HTTP est **injecté**, ce qui permet de le faire
tourner sans réseau dans un banc de contrôle.

Six faits mesurés le 2026-09-07 (`scripts/mesurer_services_us.py`), et ce que
chacun impose ici :

  1. `exportImage` accepte `bbox` + `size` + `imageSR` : on demande donc
     directement la résolution ET le fuseau de travail, comme la chaîne
     française demande sa résolution à la Géoplateforme. Le service transparent
     n'a pas de dalles : **on découpe soi-même**.
  2. Le service plafonne l'image à **8000 × 8000** px. Un rayon d'analyse de
     12 km à 2 m fait 12 000 px de côté, donc quatre morceaux. Le plafond est
     lu dans les métadonnées, pas supposé.
  3. Une erreur arrive en **HTTP 200 avec `Content-Type: image/tiff`** et un
     corps JSON. L'en-tête ment ; c'est le reniflement de signature de
     `telechargement.py` qui tranche, et c'est pour cela qu'on réemploie son
     cache plutôt que d'écrire un téléchargeur de plus.
  4. Le SCR livré est **propre** (code d'autorité présent, `IsSame` vrai) :
     contrairement à l'IGN, aucune correction de géoréférencement.
  5. Le service ne déclare **aucun sans-donnée**. `construire_mosaique`
     s'appuie sur `-9999` : on le pose à l'arrivée, sinon les trous se
     confondraient avec des altitudes réelles.
  6. Le service **ne refuse jamais** une résolution trop fine : il interpole et
     rend du faux relief plausible — 16 Mo pour 0,25 m demandé sur une zone à
     1 m. On borne donc au natif, que l'index de disponibilité nous donne.
"""

import math
import re
import urllib.parse

SERVICE = ("https://elevation.nationalmap.gov/arcgis/rest/services"
           "/3DEPElevation/ImageServer")

# Index de disponibilité par résolution. La couche 18 (« 1 Meter », groupe
# « Query ») est celle qui se requête ; la couche 1 porte le même nom mais sert
# l'affichage.
INDEX = ("https://index.nationalmap.gov/arcgis/rest/services"
         "/3DEPElevationIndex/MapServer")
INDEX_COUCHE_1M = 18

# Résolutions natives, en mètres. Le 1/3 arc-seconde vaut ~10 m sous nos
# latitudes ; c'est le repli national quand le LiDAR ne couvre pas.
RESOLUTION_LIDAR = 1.0
RESOLUTION_REPLI = 10.0

SANS_DONNEE = -9999.0

# Plafond de repli si les métadonnées du service deviennent illisibles. Choisi
# bas exprès : mieux vaut découper trop que se faire refuser une image entière.
PLAFOND_REPLI = 4096

# Plafond EFFECTIF, mesuré le 2026-09-11 sur l'emprise d'un utilisateur
# (Géorgie, rayon 3 000 m) : le service DÉCLARE 8 000 px mais rend HTTP 500,
# immédiat et sans variation d'un essai à l'autre, dès 3 463 x 3 400 px de
# GeoTIFF float32 ; découpé à 2 048 px, il rend 16,8 Mo de raster valide par
# morceau. Un plafond déclaré n'est pas un plafond constaté : on lit le
# premier, on ne dépasse jamais le second. Sous PLAFOND_MINIMAL, on renonce —
# un service qui refuse 512 px n'a pas un problème de taille.
PLAFOND_EFFECTIF = 2048
PLAFOND_MINIMAL = 512


class ErreurTerrain(RuntimeError):
    """Le service a répondu autre chose que ce qu'on lui demandait."""


class JournalMuet:
    def info(self, message):
        pass

    def warning(self, message):
        pass


def _transport_defaut():
    from ..import_dalles.geoplateforme import TransportRequests

    return TransportRequests()


def _json(url, params, transport):
    params = dict(params)
    params.setdefault("f", "json")
    reponse = transport.obtenir(url, params=params)
    if getattr(reponse, "status_code", 0) != 200:
        raise ErreurTerrain("HTTP %s sur %s" % (
            getattr(reponse, "status_code", "?"), url[:90]))
    import json

    texte = reponse.text or ""
    try:
        donnees = json.loads(texte)
    except ValueError:
        raise ErreurTerrain("réponse non JSON de %s (%s…)" % (url[:60], texte[:120]))

    # ArcGIS REST refuse EN HTTP 200, le refus étant dans le corps. Sans ce
    # test, le dictionnaire d'erreur repartait comme s'il portait des données :
    # l'appelant y cherchait ses clés, n'en trouvait aucune, et retombait sur
    # ses valeurs par défaut — un service en panne devenait une mesure muette.
    if isinstance(donnees, dict) and "error" in donnees:
        erreur = donnees.get("error") or {}
        if not isinstance(erreur, dict):
            erreur = {"message": str(erreur)}
        raise ErreurTerrain("le service refuse (%s) sur %s : %s" % (
            erreur.get("code", "?"), url[:60],
            erreur.get("message", "sans message")))
    return donnees


# ---------------------------------------------------------------------------
# Ce que le service annonce
# ---------------------------------------------------------------------------

def plafond_image(transport=None, journal=None):
    """Côté maximal d'une image, en pixels, LU dans les métadonnées.

    Mesuré à 8000 le 2026-09-07. Lu et non codé en dur : le jour où l'USGS le
    change, le découpage suit sans qu'on ait à s'en apercevoir sur un livrable.
    """
    transport = transport or _transport_defaut()
    journal = journal or JournalMuet()
    try:
        meta = _json(SERVICE, {}, transport)
        cote = min(int(meta.get("maxImageWidth") or 0),
                   int(meta.get("maxImageHeight") or 0))
    except Exception as erreur:
        journal.warning("3DEP : plafond d'image illisible (%s), repli sur %d px."
                        % (erreur, min(PLAFOND_REPLI, PLAFOND_EFFECTIF)))
        cote = PLAFOND_REPLI
    if cote <= 0:
        journal.warning("3DEP : plafond d'image non déclaré, repli sur %d px."
                        % min(PLAFOND_REPLI, PLAFOND_EFFECTIF))
        cote = PLAFOND_REPLI
    # Lu, puis borné : le déclaré suit le service s'il baisse, le constaté
    # interdit de le croire quand il promet plus qu'il ne rend.
    return min(cote, PLAFOND_EFFECTIF)


def couverture_lidar_1m(bbox_wgs84, transport=None, journal=None):
    """Projets LiDAR 1 m intersectant l'emprise. Rend (nombre, liste de noms).

    À interroger AVANT de choisir la résolution de travail : c'est la seule
    façon de savoir ce que la mosaïque transparente servira vraiment, et donc
    de ne pas annoncer du 1 m sur une zone à 10 m.
    """
    transport = transport or _transport_defaut()
    journal = journal or JournalMuet()
    requete = "%s/%d/query" % (INDEX, INDEX_COUCHE_1M)
    commun = {
        "geometry": "%f,%f,%f,%f" % tuple(bbox_wgs84),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
    }
    try:
        nombre = int(_json(requete, dict(commun, returnCountOnly="true"),
                           transport).get("count") or 0)
    except Exception as erreur:
        journal.warning("3DEP : index de disponibilité injoignable (%s). "
                        "Résolution non bornée par la mesure — repli prudent "
                        "sur %g m." % (erreur, RESOLUTION_REPLI))
        return None, []
    noms = []
    if nombre:
        try:
            donnees = _json(requete, dict(commun, outFields="project,pub_date",
                                          returnGeometry="false",
                                          resultRecordCount="5"), transport)
            for entite in donnees.get("features", []):
                attributs = entite.get("attributes") or {}
                nom = attributs.get("project")
                if nom:
                    noms.append("%s (%s)" % (nom, attributs.get("pub_date") or "?"))
        except Exception:
            pass  # le compte suffit à décider ; les noms ne sont qu'un confort
    return nombre, noms


def resolution_native(bbox_wgs84, transport=None, journal=None):
    """La résolution la plus fine réellement servie sur cette emprise.

    Rend 1 m si un projet LiDAR couvre l'emprise, sinon ~10 m. En cas d'index
    injoignable, rend le repli : mieux vaut une ZIV annoncée à 10 m qu'une ZIV
    annoncée à 1 m et interpolée depuis du 10 m.
    """
    journal = journal or JournalMuet()
    nombre, noms = couverture_lidar_1m(bbox_wgs84, transport, journal)
    if nombre is None:
        return RESOLUTION_REPLI
    if nombre:
        journal.info("3DEP : %d projet(s) LiDAR 1 m sur l'emprise%s."
                     % (nombre, (" — " + ", ".join(noms)) if noms else ""))
        return RESOLUTION_LIDAR
    journal.warning(
        "3DEP : aucun projet LiDAR 1 m sur l'emprise. Terrain servi au "
        "1/3 arc-seconde, soit environ %g m : le relief est lissé et les "
        "talus de moins de %g m disparaissent. À dire au cartouche."
        % (RESOLUTION_REPLI, RESOLUTION_REPLI))
    return RESOLUTION_REPLI


def borner_resolution(demandee, native, journal=None):
    """Ne jamais demander plus fin que ce que le service possède.

    Le service accepte tout et interpole en silence : 0,25 m demandé sur une
    zone à 1 m rend 16 Mo de détail fabriqué. Même garde-fou que
    `url_a_resolution()` côté IGN, et il se dit au journal.
    """
    journal = journal or JournalMuet()
    if demandee is None or demandee <= 0:
        return native
    if demandee < native:
        journal.warning(
            "Résolution de travail portée de %g m à %g m : le service ne "
            "possède pas plus fin ici et aurait interpolé sans le dire."
            % (demandee, native))
        return native
    return float(demandee)


# ---------------------------------------------------------------------------
# Découpage et fabrication des dalles
# ---------------------------------------------------------------------------

def decouper(bbox, resolution, plafond):
    """Découpe l'emprise en morceaux qui tiennent sous le plafond d'image.

    Les morceaux sont **alignés sur la grille** de la résolution demandée, de
    sorte que la mosaïque n'ait pas à recaler quoi que ce soit : deux morceaux
    voisins partagent exactement un bord de pixel.
    """
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    if xmax <= xmin or ymax <= ymin:
        raise ErreurTerrain("Emprise vide : %r" % (bbox,))
    if plafond < 16:
        raise ErreurTerrain("Plafond d'image absurde : %r" % plafond)

    # Caler l'origine sur la grille de la résolution : une grille commune vaut
    # mieux qu'un alignement recalculé à l'assemblage.
    xmin = math.floor(xmin / resolution) * resolution
    ymin = math.floor(ymin / resolution) * resolution
    xmax = math.ceil(xmax / resolution) * resolution
    ymax = math.ceil(ymax / resolution) * resolution

    cote = plafond * resolution
    n_x = int(math.ceil((xmax - xmin) / cote))
    n_y = int(math.ceil((ymax - ymin) / cote))
    morceaux = []
    for j in range(n_y):
        for i in range(n_x):
            x0 = xmin + i * cote
            y0 = ymin + j * cote
            morceaux.append((x0, y0, min(x0 + cote, xmax), min(y0 + cote, ymax)))
    return morceaux


def _taille_px(bbox, resolution):
    """Le couple (largeur, hauteur) en pixels demandé au service.

    Une seule formule pour l'URL et pour le nom du morceau : c'est ce qui
    garantit que le nom décrit exactement l'image qu'on est allé chercher.
    """
    return (max(int(round((bbox[2] - bbox[0]) / resolution)), 1),
            max(int(round((bbox[3] - bbox[1]) / resolution)), 1))


def _url_export(bbox, epsg, resolution):
    """URL `exportImage` d'un morceau, à la résolution voulue.

    La `bbox` est passée **dans le SCR de sortie** : mesuré, un rectangle donné
    en Albers puis rendu en UTM produit des pixels de 34,6 m au lieu de 30, le
    service calant la grille sur l'emprise reprojetée.
    """
    largeur, hauteur = _taille_px(bbox, resolution)
    params = {
        "bbox": "%f,%f,%f,%f" % tuple(bbox),
        "bboxSR": str(epsg),
        "imageSR": str(epsg),
        "size": "%d,%d" % (largeur, hauteur),
        "format": "tiff",
        "f": "image",
    }
    return "%s/exportImage?%s" % (SERVICE, urllib.parse.urlencode(params))


def dalles(bbox, resolution, epsg, plafond=None, transport=None, journal=None):
    """Les morceaux de l'emprise, sous la forme d'objets `Dalle`.

    On réemploie le `Dalle` de la chaîne française — un simple porteur de nom,
    d'URL et d'emprise — pour que `telecharger_dalles()` s'applique tel quel,
    avec son cache, ses reprises, son fichier `.partiel`, ses trois tentatives,
    son annulation et son reniflement de signature.
    """
    from ..import_dalles.geoplateforme import Dalle

    journal = journal or JournalMuet()
    plafond = plafond or plafond_image(transport, journal)
    morceaux = decouper(bbox, resolution, plafond)
    journal.info("3DEP : emprise découpée en %d morceau(x) de %d px au plus, "
                 "à %g m." % (len(morceaux), plafond, resolution))
    sortie = []
    for numero, morceau in enumerate(morceaux, 1):
        # Le nom porte l'emprise ET la taille : c'est la clé du cache, et deux
        # calculs voisins doivent pouvoir réutiliser le même morceau. Sans la
        # taille, le redécoupage sur refus 5xx redemande le même coin en plus
        # petit sous le MÊME nom — le cache rend alors le morceau de la
        # tentative précédente, d'une autre étendue, et le terrain se retrouve
        # troué SANS erreur. Deux résolutions de travail, elles, ne se
        # marchaient déjà pas dessus : `decouper` cale le coin sur la grille
        # de résolution (mesuré : 438326 à 2 m, 438320 à 10 m).
        largeur, hauteur = _taille_px(morceau, resolution)
        nom = "3dep_%d_%d_%d_%dx%d" % (
            epsg, int(round(morceau[0])), int(round(morceau[1])),
            largeur, hauteur)
        sortie.append(Dalle(nom=nom, url=_url_export(morceau, epsg, resolution),
                            emprise=morceau, horodatage=""))
    return sortie


# ---------------------------------------------------------------------------
# Les deux points d'injection de `telecharger_dalles`
# ---------------------------------------------------------------------------

def url_a_la_resolution(url, resolution):
    """Rend `(url, effective, largeur, hauteur)` pour une URL `exportImage`.

    Le défaut de `telecharger_dalles` réécrit `WIDTH`/`HEIGHT` d'une requête WMS
    de la Géoplateforme ; ici la taille est déjà dans `size=largeur,hauteur`,
    fixée quand le morceau a été fabriqué. On la relit plutôt que de la
    recalculer, pour que la clé de cache corresponde au fichier réellement servi.
    """
    trouve_taille = re.search(r"[?&]size=(\d+)%2C(\d+)", url, re.IGNORECASE)
    if trouve_taille is None:
        trouve_taille = re.search(r"[?&]size=(\d+),(\d+)", url, re.IGNORECASE)
    trouve_bbox = re.search(r"[?&]bbox=([^&]+)", url, re.IGNORECASE)
    if trouve_taille is None or trouve_bbox is None:
        raise ErreurTerrain("URL exportImage sans size ni bbox : %s" % url[:120])
    largeur, hauteur = int(trouve_taille.group(1)), int(trouve_taille.group(2))
    morceaux = urllib.parse.unquote(trouve_bbox.group(1)).split(",")
    etendue_x = float(morceaux[2]) - float(morceaux[0])
    return url, etendue_x / largeur, largeur, hauteur


def poser_sans_donnee(chemin):
    """Écrit `-9999` comme sans-donnée sur un morceau qui arrive sans.

    3DEP n'en déclare aucun — mesuré. Sans cette pose, `construire_mosaique`
    assemblerait avec `srcNodata=-9999` sur des fichiers où rien ne porte cette
    valeur : les trous de couverture passeraient pour des altitudes réelles.
    Rend le suffixe de journal attendu par `telecharger_dalles`.
    """
    from osgeo import gdal

    gdal.UseExceptions()
    jeu = gdal.Open(str(chemin), gdal.GA_Update)
    if jeu is None:
        raise ErreurTerrain("Morceau illisible par GDAL : %s" % chemin)
    try:
        bande = jeu.GetRasterBand(1)
        if bande.GetNoDataValue() is not None:
            return ""
        bande.SetNoDataValue(SANS_DONNEE)
        jeu.FlushCache()
        return ", sans-donnee pose"
    finally:
        # Sous Windows le fichier reste verrouillé tant que la dernière
        # référence vit : la libérer avant de rendre la main.
        jeu = None
