# -*- coding: utf-8 -*-
"""Bâti et routes américains : USA Structures et TIGER/Line.

Deux services d'entités ArcGIS, et un GeoJSON écrit à la main.

**Pourquoi ne pas demander `f=geojson`.** `graver_dans_raster` passe par
`gdal.Rasterize`, qui ne reprojette pas : le vecteur doit déjà être dans le
système du raster. Or `tasks/lessons.md` documente le piège — GDAL lit un
GeoJSON en EPSG:4326 par défaut, et des coordonnées projetées interprétées en
degrés atterrissent hors du raster, ce qui fait rendre `True` à `gdal.Rasterize`
**sans graver une seule cellule**. La parade de l'IGN est un membre `crs` dans
le GeoJSON ; ici on demande donc de l'Esri JSON en `outSR`, et on écrit le
GeoJSON soi-même avec ce membre. Rien n'est laissé à un défaut.

**Pourquoi paginer à 2000 et pas plus.** Mesuré le 2026-09-07 : le service
plafonne chaque réponse à **2000 entités**, plafond déclaré ET réel, avec
`exceededTransferLimit` à vrai. Une emprise urbaine de 7 km compte ~15 000
bâtiments, soit 8 pages. Un pas de page au-dessus du plafond serveur rendrait
le test de troncature structurellement mort — c'est exactement le défaut qui a
laissé 79 à 97 % des toits non gravés côté WFS, sans une ligne de journal.

**Pourquoi `HEIGHT` ne suffit pas.** Mesuré sur quatre emprises : 78,5 % à
Austin, 91,8 % à Denver, 94,9 % à Sacramento — et **0 %** à Asheville. Le
champ est en mètres (alias `Height (meters)`, moyennes de 5,2 et 5,9 m sur
~48 000 bâtiments), mais des régions entières sont vides. D'où le forfait par
classe d'occupation, et la répartition journalisée à chaque exécution.
"""

import json
import os

BATI = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services"
        "/USA_Structures_View/FeatureServer/0")

ROUTES = ("https://tigerweb.geo.census.gov/arcgis/rest/services"
          "/TIGERweb/Transportation/MapServer")
# Une couche par classe de route : primaire (2), secondaire (6), locale (8),
# retenues sur les IDENTIFIANTS des troncons, ni sur les comptes ni sur les
# noms. Mesure du 2026-10-02 sur Asheville : 4, 5 et 6 portent les memes 45
# troncons au meme trace (11 902 sommets) — le suffixe d'echelle ne regle que
# l'affichage — et 7 et 8 les memes 1323. La 3, « Interstates and US
# Highways », n'en porte que 17, toutes comprises dans les 45 : retenue
# jusqu'en 1.6.2, elle laissait au sol les routes d'Etat. Controle permanent :
# U1 de `scripts/veiller_sources.py`.
COUCHES_ROUTES = (2, 6, 8)

# Plafond CONSTATE du service de bati, pas une preference. Le lien de page
# suivante que le service fabrique lui-meme porte la meme valeur.
PAS_DE_PAGE = 2000

# Garde-fou de volume total, toutes pages confondues : au-dela on cesse de
# paginer et on ANNONCE la troncature. 200 000 couvre le pire cas reel avec de
# la marge, tout en bornant le travail a 100 requetes.
ENTITES_MAX = 200000

# Hauteurs de repli par classe d'occupation, en metres. Hypothese assumee, et
# journalisee a chaque execution avec la part de batiments concernes. Les
# valeurs sont volontairement modestes : surestimer une hauteur inventee
# masquerait des visibilites reelles.
FORFAITS = {
    "Residential": 6.0,
    "Commercial": 8.0,
    "Industrial": 9.0,
    "Education": 8.0,
    "Government": 8.0,
    "Assembly": 8.0,
    "Utility and Misc": 5.0,
    "Unclassified": 6.0,
}
FORFAIT_DEFAUT = 6.0

# Nom du champ ou l'on ecrit la hauteur finalement retenue. C'est lui que
# `graver_dans_raster(champ=...)` grave.
CHAMP_HAUTEUR = "H_ZIV"

DELAI = (15, 180)


class ErreurVecteurs(RuntimeError):
    """Le service a répondu autre chose que ce qu'on lui demandait."""


class JournalMuet:
    def info(self, message):
        pass

    def warning(self, message):
        pass


def _transport_defaut():
    from ..import_dalles.geoplateforme import TransportRequests

    return TransportRequests(delai=DELAI)


def _json(url, params, transport):
    params = dict(params)
    params.setdefault("f", "json")
    reponse = transport.obtenir(url, params=params)
    if getattr(reponse, "status_code", 0) != 200:
        raise ErreurVecteurs("HTTP %s sur %s" % (
            getattr(reponse, "status_code", "?"), url[:90]))
    texte = reponse.text or ""
    try:
        donnees = json.loads(texte)
    except ValueError:
        raise ErreurVecteurs("réponse non JSON (%s…)" % texte[:140])
    if "error" in donnees:
        # ArcGIS annonce ses erreurs dans le corps, en HTTP 200.
        raise ErreurVecteurs("le service a refusé la requête : %s"
                             % str(donnees["error"])[:200])
    return donnees


def _emprise(bbox, epsg):
    return {
        "geometry": "%f,%f,%f,%f" % tuple(bbox),
        "geometryType": "esriGeometryEnvelope",
        "inSR": str(epsg),
        "spatialRel": "esriSpatialRelIntersects",
    }


def compter(service, bbox, epsg, transport=None, couche=None):
    """Compte exact avant tout transfert — l'équivalent de `RESULTTYPE=hits`."""
    transport = transport or _transport_defaut()
    url = service if couche is None else "%s/%d" % (service, couche)
    donnees = _json("%s/query" % url,
                    dict(_emprise(bbox, epsg), where="1=1", returnCountOnly="true"),
                    transport)
    return int(donnees.get("count") or 0)


# ---------------------------------------------------------------------------
# Esri JSON -> GeoJSON, avec le SCR écrit noir sur blanc
# ---------------------------------------------------------------------------

def _geometrie_geojson(brute):
    """Traduit une géométrie Esri en géométrie GeoJSON.

    Esri décrit les polygones par `rings` et les lignes par `paths`. Pour une
    gravure, l'orientation des anneaux importe peu : GDAL grave l'intérieur du
    premier anneau et retire les suivants, ce qui est le comportement voulu pour
    une cour intérieure.
    """
    if not brute:
        return None
    if "rings" in brute:
        anneaux = [a for a in brute["rings"] if a and len(a) >= 4]
        if not anneaux:
            return None
        return {"type": "Polygon", "coordinates": anneaux}
    if "paths" in brute:
        chemins = [c for c in brute["paths"] if c and len(c) >= 2]
        if not chemins:
            return None
        if len(chemins) == 1:
            return {"type": "LineString", "coordinates": chemins[0]}
        return {"type": "MultiLineString", "coordinates": chemins}
    if "x" in brute and "y" in brute:
        return {"type": "Point", "coordinates": [brute["x"], brute["y"]]}
    return None


def _ecrire_geojson(chemin, entites, epsg):
    """Écrit un GeoJSON avec un membre `crs` explicite.

    Sans ce membre, OGR lit le fichier en EPSG:4326 et la gravure ne touche
    aucune cellule — en rendant `True`. C'est le piège que `lessons.md` décrit
    et que l'IGN évite en publiant un `crs` ; on fait pareil.
    """
    contenu = {
        "type": "FeatureCollection",
        "crs": {"type": "name",
                "properties": {"name": "urn:ogc:def:crs:EPSG::%d" % epsg}},
        "features": entites,
    }
    with open(chemin, "w", encoding="utf-8") as flux:
        json.dump(contenu, flux)
    return chemin


# ---------------------------------------------------------------------------
# Bâti
# ---------------------------------------------------------------------------

def hauteur_batiment(attributs):
    """Hauteur à graver, et d'où elle vient. Rend `(hauteur, origine)`.

    `HEIGHT` quand il est renseigné et strictement positif ; sinon le forfait
    de la classe d'occupation. Un zéro n'est pas une hauteur : c'est une absence
    de mesure, et le traiter comme un bâtiment plat effacerait le bâti.
    """
    brute = attributs.get("HEIGHT")
    try:
        hauteur = float(brute)
    except (TypeError, ValueError):
        hauteur = 0.0
    if hauteur > 0.0:
        return hauteur, "mesuree"
    classe = (attributs.get("OCC_CLS") or "").strip()
    return FORFAITS.get(classe, FORFAIT_DEFAUT), "forfait"


def telecharger_bati(bbox, epsg, chemin, transport=None, journal=None):
    """Écrit le bâti de l'emprise en GeoJSON, avec sa hauteur retenue.

    Chaque entité porte `H_ZIV`, la hauteur à graver, et `H_SRC`, sa provenance.
    Rend un dict de statistiques, ou `None` si l'emprise n'a aucun bâtiment.
    """
    transport = transport or _transport_defaut()
    journal = journal or JournalMuet()

    total = compter(BATI, bbox, epsg, transport)
    if not total:
        journal.info("USA Structures : aucun bâtiment sur l'emprise.")
        return None
    journal.info("USA Structures : %d bâtiment(s) annoncé(s), %d page(s) de %d."
                 % (total, (total + PAS_DE_PAGE - 1) // PAS_DE_PAGE, PAS_DE_PAGE))

    entites = []
    mesurees = forfaits = sans_geometrie = 0
    somme = 0.0
    decalage = 0
    tronque = False
    while True:
        page = _json("%s/query" % BATI, dict(
            _emprise(bbox, epsg), where="1=1",
            outFields="HEIGHT,OCC_CLS", outSR=str(epsg),
            returnGeometry="true",
            resultOffset=str(decalage), resultRecordCount=str(PAS_DE_PAGE)),
            transport)
        lot = page.get("features") or []
        if not lot:
            break
        for entite in lot:
            attributs = entite.get("attributes") or {}
            geometrie = _geometrie_geojson(entite.get("geometry"))
            if geometrie is None:
                sans_geometrie += 1
                continue
            hauteur, origine = hauteur_batiment(attributs)
            if origine == "mesuree":
                mesurees += 1
            else:
                forfaits += 1
            somme += hauteur
            entites.append({
                "type": "Feature",
                "properties": {CHAMP_HAUTEUR: hauteur, "H_SRC": origine,
                               "OCC_CLS": attributs.get("OCC_CLS")},
                "geometry": geometrie,
            })
        decalage += len(lot)
        if len(lot) < PAS_DE_PAGE and not page.get("exceededTransferLimit"):
            break
        if decalage >= ENTITES_MAX:
            tronque = True
            break

    if tronque:
        journal.warning(
            "USA Structures : garde-fou de volume atteint à %d entités — le "
            "bâti est TRONQUÉ sur cette emprise." % ENTITES_MAX)
    if decalage < total and not tronque:
        journal.warning(
            "USA Structures : %d entités rapatriées pour %d annoncées. "
            "Pagination incomplète, le bâti est incomplet." % (decalage, total))

    if not entites:
        journal.warning("USA Structures : aucune entité exploitable après pagination.")
        return None

    _ecrire_geojson(chemin, entites, epsg)
    part = 100.0 * mesurees / len(entites)
    journal.info(
        "Bâti : %d entité(s) écrites, %.1f %% à hauteur mesurée, %.1f %% au "
        "forfait de classe, hauteur moyenne %.1f m."
        % (len(entites), part, 100.0 - part, somme / len(entites)))
    if part < 50.0:
        journal.warning(
            "Plus de la moitié du bâti est au forfait sur cette emprise : la "
            "hauteur des toits y est une hypothèse, pas une mesure. À dire au "
            "cartouche.")
    if sans_geometrie:
        journal.warning("Bâti : %d entité(s) sans géométrie exploitable, écartées."
                        % sans_geometrie)
    return {
        "chemin": chemin, "annonces": total, "ecrites": len(entites),
        "mesurees": mesurees, "forfaits": forfaits,
        "part_mesuree": part, "hauteur_moyenne": somme / len(entites),
        "tronque": tronque,
    }


def hauteur_bati_sur_grille(chemin_reference, chemin_bati):
    """Rend la hauteur du bâti, en mètres, sur la grille d'un raster de référence.

    Pourquoi passer par un tableau au lieu de graver directement sur le
    terrain : la hauteur occultante d'une cellule est **le sommet de ce qui s'y
    trouve**, pas la somme de tout ce qui s'y trouve. Une maison sous les
    arbres occulte la hauteur des arbres, non celle des arbres PLUS la sienne.
    À la maille de 30 m de LANDFIRE, beaucoup d'empreintes de bâti tombent dans
    des cellules classées forêt : additionner les deux gonflerait
    systématiquement l'occultation en zone boisée habitée. L'appelant combine
    donc les deux tableaux par un maximum, puis grave une seule fois.

    Le raster est créé en mémoire, à zéro, et le bâti y est gravé en
    REMPLACEMENT : sur un fond nul, la valeur écrite EST la hauteur.
    """
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    reference = gdal.Open(str(chemin_reference))
    if reference is None:
        raise ErreurVecteurs("Raster de référence illisible : %s" % chemin_reference)
    try:
        memoire = gdal.GetDriverByName("MEM").Create(
            "", reference.RasterXSize, reference.RasterYSize, 1, gdal.GDT_Float32)
        memoire.SetGeoTransform(reference.GetGeoTransform())
        memoire.SetProjection(reference.GetProjection())
        memoire.GetRasterBand(1).Fill(0.0)
        gdal.Rasterize(memoire, str(chemin_bati),
                       options=gdal.RasterizeOptions(
                           bands=[1], attribute=CHAMP_HAUTEUR, add=False))
        tableau = np.asarray(memoire.GetRasterBand(1).ReadAsArray(), dtype="float32")
        memoire = None
        return np.where(np.isfinite(tableau), tableau, 0.0).astype("float32")
    finally:
        reference = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def telecharger_routes(bbox, epsg, chemin, transport=None, journal=None):
    """Écrit les routes de l'emprise en GeoJSON. Rend True si non vide.

    Trois couches TIGER réunies en un seul fichier : les axes primaires, les
    routes secondaires et la voirie locale. C'est cette dernière qui compte
    pour placer un point de vue photo — un photographe accède par une petite
    route bien plus souvent que par une autoroute.
    """
    transport = transport or _transport_defaut()
    journal = journal or JournalMuet()

    entites = []
    for couche in COUCHES_ROUTES:
        url = "%s/%d/query" % (ROUTES, couche)
        decalage = 0
        while True:
            page = _json(url, dict(
                _emprise(bbox, epsg), where="1=1", outFields="NAME",
                outSR=str(epsg), returnGeometry="true",
                resultOffset=str(decalage), resultRecordCount=str(PAS_DE_PAGE)),
                transport)
            lot = page.get("features") or []
            if not lot:
                break
            for entite in lot:
                geometrie = _geometrie_geojson(entite.get("geometry"))
                if geometrie is not None:
                    entites.append({"type": "Feature", "properties": {},
                                    "geometry": geometrie})
            decalage += len(lot)
            if len(lot) < PAS_DE_PAGE and not page.get("exceededTransferLimit"):
                break
            if decalage >= ENTITES_MAX:
                journal.warning("TIGER couche %d : garde-fou de volume atteint."
                                % couche)
                break

    if not entites:
        journal.info("TIGER/Line : aucune route sur l'emprise.")
        return False
    _ecrire_geojson(chemin, entites, epsg)
    journal.info("TIGER/Line : %d tronçon(s) sur les couches %s."
                 % (len(entites), ", ".join(str(c) for c in COUCHES_ROUTES)))
    return True
