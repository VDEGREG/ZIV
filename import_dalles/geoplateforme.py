# -*- coding: utf-8 -*-
"""Découverte des dalles LiDAR HD sur la Géoplateforme IGN.

Ce module ne connaît ni QGIS ni GDAL : il interroge le WFS, lit ce qu'il
répond, et rend des objets `Dalle`. Le transport HTTP est **injecté**, ce qui
permet de le faire tourner sans réseau dans un banc de contrôle.

Ce que les appels réels du 2026-09-01 ont établi, et sur quoi ce module
s'appuie :

  * l'URL d'une dalle n'est pas un fichier mais une requête WMS GetMap
    (`FORMAT=image/geotiff`, `WIDTH=HEIGHT=2000` pour 1 km² à 0,50 m) — on peut
    donc demander la résolution que l'on veut, et diviser d'autant le volume ;
  * le serveur sous-échantillonne par **moyenne** (vérifié : écart nul au
    bloc-moyenne 4×4) ;
  * `HEAD` est refusé (HTTP 400) : ne jamais sonder autrement que par GET ;
  * `RESULTTYPE=hits` répond en 850 octets et donne le compte exact : c'est ce
    qui permet d'annoncer le volume **avant** de télécharger.

Ce que le 2026-09-09 a changé, et qui a mis la découverte hors service :

  * l'IGN a **retiré ses quatre couches de dalles** lors de la migration du
    portail vers `cartes.gouv.fr`, et les a fusionnées en une seule,
    `IGNF_LIDAR-HD_METADONNEE:metadata`. L'ancien nom fait maintenant répondre
    « Unknown namespace [IGNF_MNS-LIDAR-HD] » ;
  * les URL des quatre produits y sont des **attributs** (`url_mnt`, `url_mns`,
    `url_mnh`, `url_npl`) : la découverte est donc la même requête pour tous, et
    seule change la propriété d'où l'on tire l'URL ;
  * la nouvelle couche n'a **plus de propriété `name`**. L'identité vient du
    coin nord-ouest (`coordonnees_nw`), préfixé du produit — sans quoi les
    trois produits d'une même dalle partageraient une clé de cache ;
  * en compensation, elle porte une provenance bien plus riche :
    `code_mission`, `date_edition`, `systeme_altimetrique`, dates d'acquisition,
    `procede_classement`, `nombre_points`.
"""

import json
import re
from dataclasses import dataclass

WFS_URL = "https://data.geopf.fr/wfs/ows"

# Une SEULE couche porte desormais l'index des dalles, et les URL des quatre
# produits y sont des ATTRIBUTS. Constate par appel reel le 2026-09-09, apres
# que la decouverte a cesse de rendre quoi que ce soit.
#
# Avant, l'IGN publiait quatre couches distinctes : `IGNF_MNT-LIDAR-HD:dalle`,
# `IGNF_MNS-LIDAR-HD:dalle`, `IGNF_MNH-LIDAR-HD:dalle` et
# `IGNF_NUAGES-DE-POINTS-LIDAR-HD:dalle`. Elles ont ete RETIREES lors de la
# migration du portail vers cartes.gouv.fr : sur les 800 couches que le service
# annonce aujourd'hui, aucune ne porte plus MNT, MNS ni MNH, et le WFS repond
# « Unknown namespace [IGNF_MNS-LIDAR-HD] ».
#
# La lecon du 2026-09-01 vaut donc dans les deux sens : un code de couche ne se
# devine pas, il se constate — ET il peut changer sous nos pieds. La source
# d'autorite est GetCapabilities, pas notre memoire.
TYPENAME_DALLES = "IGNF_LIDAR-HD_METADONNEE:metadata"

# Propriete portant l'URL de telechargement, par produit. Verifie sur une
# entite reelle : l'URL est une requete WMS GetMap sur
# `IGNF_LIDAR-HD_<produit>_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93`, en
# `FORMAT=image/geotiff` avec `WIDTH=HEIGHT=2000` pour 1 km2 a 0,50 m — donc
# exactement la forme que `url_a_resolution()` sait reecrire. Rejoue telle
# quelle : HTTP 200, 16 000 513 octets, signature GeoTIFF valide.
PRODUITS = {
    "MNT": "url_mnt",
    "MNS": "url_mns",
    "MNH": "url_mnh",
}

# Le nuage de points brut, qui ne s'assemble pas comme un raster.
PROPRIETE_NUAGE = "url_npl"

RESOLUTION_NATIVE = 0.5  # m, résolution du LiDAR HD livré
EPSG_TRAVAIL = 2154

# Float32 non compressé : 4 octets par pixel. Mesuré : 15,3 Mo pour 2000×2000.
OCTETS_PAR_PIXEL = 4


class ErreurGeoplateforme(RuntimeError):
    """Le service a répondu autre chose que ce qu'on lui demandait."""


@dataclass
class Dalle:
    """Une dalle de 1 km², telle que le WFS la décrit."""

    nom: str
    url: str
    emprise: tuple  # (xmin, ymin, xmax, ymax) en Lambert 93
    horodatage: str = ""

    def largeur(self):
        return self.emprise[2] - self.emprise[0]

    def hauteur(self):
        return self.emprise[3] - self.emprise[1]


class TransportRequests:
    """Transport HTTP par défaut, sur `requests`."""

    def __init__(self, delai=(15, 300)):
        self.delai = delai

    def obtenir(self, url, params=None, flux=False):
        import requests

        try:
            return requests.get(url, params=params, timeout=self.delai, stream=flux)
        except Exception as erreur:
            # Derrière une inspection SSL d'entreprise, Python ne voit pas la CA
            # interne du magasin Windows. On le dit plutôt que de désactiver la
            # vérification TLS.
            if "CERTIFICATE_VERIFY_FAILED" in str(erreur):
                raise ErreurGeoplateforme(
                    "Certificat TLS refusé. Le poste passe probablement par une "
                    "inspection SSL : installer « truststore » et appeler "
                    "truststore.inject_into_ssl() avant l'import. Ne pas "
                    "désactiver la vérification."
                )
            raise ErreurGeoplateforme("Appel réseau impossible : %s" % erreur)


def propriete_url(produit):
    """Nom de la propriété portant l'URL, pour un raccourci ou une propriété.

    Accepte « MNS », « url_mns » ou `PROPRIETE_NUAGE` : la découverte est
    désormais la MÊME requête pour les quatre produits — une seule couche — et
    seule change la propriété d'où l'on tire l'URL. Laisser entrer un nom de
    propriété tel quel évite d'avoir à dupliquer cette découverte pour le nuage
    de points, qui n'est pas dans `PRODUITS`.
    """
    texte = str(produit)
    if texte.startswith("url_"):
        return texte
    if produit not in PRODUITS:
        raise ErreurGeoplateforme(
            "Produit inconnu : %r (attendu : %s, ou un nom de propriété url_*)"
            % (produit, ", ".join(PRODUITS))
        )
    return PRODUITS[produit]


# Conserve sous son ancien nom : des appelants exterieurs peuvent s'en servir,
# et le renommer sans prevenir serait une rupture gratuite.
code_couche = propriete_url


def _parametres_communs(emprise, produit):
    return {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        # Une seule couche pour les quatre produits : le TYPENAME ne depend
        # plus du produit demande, seule la propriete d'URL en depend.
        "TYPENAME": TYPENAME_DALLES,
        "SRSNAME": "EPSG:%d" % EPSG_TRAVAIL,
        "BBOX": "%f,%f,%f,%f,urn:ogc:def:crs:EPSG::%d"
        % (emprise[0], emprise[1], emprise[2], emprise[3], EPSG_TRAVAIL),
    }


def compter_dalles(emprise, produit, transport=None):
    """Nombre de dalles intersectant l'emprise, en une requête de 850 octets.

    Mesurer le volume avant de choisir de télécharger : `RESULTTYPE=hits` donne
    le compte exact sans rapatrier la moindre géométrie.
    """
    transport = transport or TransportRequests()
    params = _parametres_communs(emprise, produit)
    params["RESULTTYPE"] = "hits"
    reponse = transport.obtenir(WFS_URL, params=params)
    if reponse.status_code != 200:
        raise ErreurGeoplateforme(
            "WFS : HTTP %s sur le comptage des dalles %s." % (reponse.status_code, produit)
        )
    trouve = re.search(r'numberMatched="(\d+)"', reponse.text)
    if not trouve:
        trouve = re.search(r'"numberMatched"\s*:\s*"?(\d+)"?', reponse.text)
    if not trouve:
        raise ErreurGeoplateforme(
            "WFS : réponse de comptage illisible (%s…)." % reponse.text[:120]
        )
    return int(trouve.group(1))


def lister_dalles(emprise, produit, transport=None, maximum=10000):
    """Dalles intersectant l'emprise, avec leur URL de téléchargement."""
    transport = transport or TransportRequests()
    params = _parametres_communs(emprise, produit)
    params["OUTPUTFORMAT"] = "application/json"
    params["COUNT"] = str(maximum)
    reponse = transport.obtenir(WFS_URL, params=params)
    if reponse.status_code != 200:
        raise ErreurGeoplateforme(
            "WFS : HTTP %s sur la liste des dalles %s." % (reponse.status_code, produit)
        )
    texte = reponse.text or ""
    if not texte.lstrip().startswith("{"):
        # Le service renvoie un ExceptionReport XML quand la requête lui déplaît.
        raise ErreurGeoplateforme(
            "WFS : réponse non JSON (%s…). Vérifier le code de couche." % texte[:160]
        )
    try:
        donnees = json.loads(texte)
    except ValueError as erreur:
        raise ErreurGeoplateforme("WFS : JSON invalide (%s)." % erreur)

    cle_url = propriete_url(produit)
    # Le nom de dalle est la CLE DU CACHE : `telecharger_dalles` en derive le
    # nom de fichier et l'entree de manifeste. Il doit donc porter le produit,
    # car une seule couche sert desormais les quatre : sans ce prefixe, MNT,
    # MNS et MNH d'une meme dalle porteraient le nom « 1039-6820 » et, a
    # resolution egale, le second telechargement relirait le fichier du premier.
    # Un MNT rendu a la place d'un MNS ne se voit pas — c'est un raster
    # d'altitudes plausibles — et la ZIV serait fausse sans un mot.
    prefixe = cle_url[4:] if cle_url.startswith("url_") else str(cle_url)
    dalles = []
    for entite in donnees.get("features", []):
        proprietes = entite.get("properties") or {}
        url = proprietes.get(cle_url)
        # La nouvelle couche n'a PLUS de propriete `name`, et l'ancienne version
        # ecartait toute dalle qui en manquait : meme avec le bon TYPENAME,
        # toutes les dalles auraient ete sautees en silence. L'identite vient
        # desormais du coin nord-ouest — « 1039-6820 » — qui est justement ce
        # qui distingue une dalle d'une autre. L'`id` d'entite sert de repli.
        brut = (proprietes.get("coordonnees_nw")
                or proprietes.get("name") or proprietes.get("nom")
                or entite.get("id"))
        if not url or not brut:
            continue
        nom = "%s_%s" % (prefixe, str(brut).replace(".", "_"))
        emprise_dalle = _emprise_geometrie(entite.get("geometry"))
        if emprise_dalle is None:
            emprise_dalle = _emprise_depuis_url(url)
        if emprise_dalle is None:
            continue
        dalles.append(
            Dalle(nom=nom, url=url, emprise=emprise_dalle,
                  horodatage=str(proprietes.get("date_edition")
                                 or proprietes.get("timestamp") or ""))
        )
    dalles.sort(key=lambda d: d.nom)
    return dalles


def _emprise_geometrie(geometrie):
    """Rectangle englobant d'une géométrie GeoJSON, quel que soit son type."""
    if not geometrie:
        return None
    coordonnees = geometrie.get("coordinates")
    if coordonnees is None:
        return None
    xs, ys = [], []

    def parcourir(element):
        if (isinstance(element, (list, tuple)) and len(element) >= 2
                and all(isinstance(v, (int, float)) for v in element[:2])):
            xs.append(float(element[0]))
            ys.append(float(element[1]))
            return
        if isinstance(element, (list, tuple)):
            for sous in element:
                parcourir(sous)

    parcourir(coordonnees)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _emprise_depuis_url(url):
    """Repli : l'emprise est aussi dans la requête WMS elle-même."""
    trouve = re.search(r"[?&]BBOX=([^&]+)", url, re.IGNORECASE)
    if not trouve:
        return None
    morceaux = trouve.group(1).split(",")
    if len(morceaux) < 4:
        return None
    try:
        return tuple(float(v) for v in morceaux[:4])
    except ValueError:
        return None


def url_a_resolution(url, resolution):
    """Réécrit une URL de dalle pour qu'elle serve la résolution demandée.

    Renvoie (url, resolution_effective, largeur, hauteur). La résolution
    effective peut différer de celle demandée : le nombre de pixels est entier,
    donc une résolution qui ne divise pas la dalle est ajustée — et c'est dit,
    pas caché.
    """
    if resolution is None or resolution <= 0:
        raise ValueError("Résolution invalide : %r" % resolution)
    emprise = _emprise_depuis_url(url)
    if emprise is None:
        raise ErreurGeoplateforme("URL de dalle sans BBOX : %s" % url[:120])
    etendue_x = emprise[2] - emprise[0]
    etendue_y = emprise[3] - emprise[1]
    # Au-delà de la résolution native, on ne gagne rien : le serveur
    # ré-interpolerait une donnée qu'il n'a pas.
    resolution = max(float(resolution), RESOLUTION_NATIVE)
    largeur = max(int(round(etendue_x / resolution)), 1)
    hauteur = max(int(round(etendue_y / resolution)), 1)
    effective = etendue_x / largeur
    url = re.sub(r"([?&]WIDTH=)\d+", r"\g<1>%d" % largeur, url, flags=re.IGNORECASE)
    url = re.sub(r"([?&]HEIGHT=)\d+", r"\g<1>%d" % hauteur, url, flags=re.IGNORECASE)
    return url, effective, largeur, hauteur


def volume_estime(nombre_dalles, resolution, cote_dalle=1000.0):
    """Octets à télécharger, d'après la mesure : Float32 non compressé.

    Mesuré le 2026-09-01 : 15,3 Mo pour une dalle de 2000×2000, soit très
    exactement 4 octets par pixel plus l'en-tête.
    """
    pixels = (cote_dalle / max(float(resolution), RESOLUTION_NATIVE)) ** 2
    return int(nombre_dalles * pixels * OCTETS_PAR_PIXEL)


def format_octets(octets):
    for unite, seuil in (("Go", 2 ** 30), ("Mo", 2 ** 20), ("ko", 2 ** 10)):
        if octets >= seuil:
            return "%.1f %s" % (octets / seuil, unite)
    return "%d octets" % octets
