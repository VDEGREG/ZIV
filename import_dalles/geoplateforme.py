# -*- coding: utf-8 -*-
"""Découverte des dalles LiDAR HD sur la Géoplateforme IGN.

Ce module ne connaît ni QGIS ni GDAL : il interroge le WFS, lit ce qu'il
répond, et rend des objets `Dalle`. Le transport HTTP est **injecté**, ce qui
permet de le faire tourner sans réseau dans un banc de contrôle.

Ce que les appels réels du 2026-09-01 ont établi, et sur quoi ce module
s'appuie :

  * l'`url` d'une dalle n'est pas un fichier mais une requête WMS GetMap
    (`FORMAT=image/geotiff`, `WIDTH=HEIGHT=2000` pour 1 km² à 0,50 m) — on peut
    donc demander la résolution que l'on veut, et diviser d'autant le volume ;
  * le serveur sous-échantillonne par **moyenne** (vérifié : écart nul au
    bloc-moyenne 4×4) ;
  * `HEAD` est refusé (HTTP 400) : ne jamais sonder autrement que par GET ;
  * `RESULTTYPE=hits` répond en 850 octets et donne le compte exact : c'est ce
    qui permet d'annoncer le volume **avant** de télécharger.
"""

import json
import re
from dataclasses import dataclass

WFS_URL = "https://data.geopf.fr/wfs/ows"

# Codes de couches vérifiés en appel réel le 2026-09-01.
PRODUITS = {
    "MNT": "IGNF_MNT-LIDAR-HD:dalle",
    "MNS": "IGNF_MNS-LIDAR-HD:dalle",
    "MNH": "IGNF_MNH-LIDAR-HD:dalle",
}

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


def code_couche(produit):
    """Accepte un raccourci (« MNS ») ou un code de couche complet.

    Le nuage de points brut n'est pas dans `PRODUITS` — il ne se sert ni ne
    s'assemble comme un raster — mais sa **découverte** passe par le même WFS.
    Laisser entrer un code complet évite d'avoir à dupliquer cette découverte.
    """
    if ":" in str(produit):
        return str(produit)
    if produit not in PRODUITS:
        raise ErreurGeoplateforme(
            "Produit inconnu : %r (attendu : %s, ou un code de couche complet)"
            % (produit, ", ".join(PRODUITS))
        )
    return PRODUITS[produit]


def _parametres_communs(emprise, produit):
    return {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAME": code_couche(produit),
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

    dalles = []
    for entite in donnees.get("features", []):
        proprietes = entite.get("properties") or {}
        url = proprietes.get("url")
        nom = proprietes.get("name") or proprietes.get("nom")
        if not url or not nom:
            continue
        emprise_dalle = _emprise_geometrie(entite.get("geometry"))
        if emprise_dalle is None:
            emprise_dalle = _emprise_depuis_url(url)
        if emprise_dalle is None:
            continue
        dalles.append(
            Dalle(nom=nom, url=url, emprise=emprise_dalle,
                  horodatage=str(proprietes.get("timestamp") or ""))
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
