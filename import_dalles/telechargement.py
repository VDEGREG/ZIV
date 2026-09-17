# -*- coding: utf-8 -*-
"""Téléchargement des dalles, avec cache, et remise d'aplomb du géoréférencement.

Deux pièges mesurés le 2026-09-01, dont ce module se protège :

  * l'URL de dalle porte `EXCEPTIONS=text/xml` : en cas d'erreur, le service
    peut répondre **200 avec du XML**. Un fichier qui pèse 400 octets et
    commence par « <ServiceException » n'est pas un GeoTIFF, et il faut le dire
    tout de suite — pas trois étapes plus loin, sur une mosaïque incohérente.
  * le GeoTIFF livré porte un SCR **bancal** : son nom est « EPSG:2154 » mais il
    n'a aucun code d'autorité, et `IsSame(EPSG:2154)` est faux. C'est le
    « non-standard CRS WKT » que la chaîne d'origine accusait d'avoir fait
    planter QGIS. On le réécrit proprement dès l'arrivée.
"""

import contextlib
import json
import os
from pathlib import Path

from .geoplateforme import EPSG_TRAVAIL, ErreurGeoplateforme, format_octets, url_a_resolution

MANIFESTE = "_dalles.json"

# Attente avant la Nieme nouvelle tentative, en secondes. Volontairement
# courte : elle suffit a laisser passer un 504 passager sans rendre une
# annulation lente a prendre effet.
ATTENTES = {1: 2.0, 2: 5.0}

# Signatures TIFF : petit-boutiste, gros-boutiste, et BigTIFF.
_SIGNATURES = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


class JournalMuet:
    def info(self, message):
        pass

    def warning(self, message):
        pass


def _cle(nom, resolution):
    return "%s@%.3f" % (nom, resolution)


def _lire_manifeste(dossier):
    chemin = dossier / MANIFESTE
    if not chemin.is_file():
        return {}
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}  # un manifeste illisible se reconstruit, il ne bloque pas


def _ecrire_manifeste(dossier, manifeste):
    with contextlib.suppress(OSError):
        (dossier / MANIFESTE).write_text(
            json.dumps(manifeste, indent=1, ensure_ascii=False), encoding="utf-8"
        )


def corriger_georeference(chemin):
    """Réécrit le SCR de la dalle en EPSG:2154 en bonne et due forme.

    Renvoie True si une correction a été nécessaire.
    """
    from osgeo import gdal, osr

    gdal.UseExceptions()
    attendu = osr.SpatialReference()
    attendu.ImportFromEPSG(EPSG_TRAVAIL)

    jeu = gdal.Open(str(chemin), gdal.GA_Update)
    if jeu is None:
        raise ErreurGeoplateforme("Dalle illisible par GDAL : %s" % chemin)
    try:
        actuel = jeu.GetSpatialRef()
        if actuel is not None and actuel.IsSame(attendu):
            return False
        jeu.SetSpatialRef(attendu)
        jeu.FlushCache()
        return True
    finally:
        jeu = None


def _corriger_et_dire(chemin):
    """Défaut d'`apres_arrivee` : remet le SCR d'aplomb et le dit au journal.

    Le libellé est celui d'origine, mot pour mot : la sortie de la chaîne
    française ne doit pas bouger d'un caractère.
    """
    return ", georeferencement corrige" if corriger_georeference(chemin) else ""


def _verifier_contenu(chemin, octets):
    """Un GeoTIFF, ou une page d'erreur déguisée en succès ?"""
    with open(chemin, "rb") as flux:
        entete = flux.read(4)
    if entete not in _SIGNATURES:
        with open(chemin, "rb") as flux:
            debut = flux.read(400).decode("utf-8", "replace")
        raise ErreurGeoplateforme(
            "Le service a renvoyé %d octets qui ne sont pas un GeoTIFF. "
            "Début de la réponse : %s" % (octets, debut[:300])
        )


def telecharger_dalles(dalles, dossier, resolution, transport=None, journal=None,
                       forcer=False, tentatives=3, est_annule=None,
                       url_a_la_resolution=None, apres_arrivee=None):
    """Télécharge les dalles à la résolution demandée. Renvoie les chemins.

    Une dalle déjà présente à la même résolution n'est pas retéléchargée, sauf
    `forcer`. La progression et le volume sont journalisés au fur et à mesure :
    on doit pouvoir arrêter en connaissance de cause.

    Deux points d'injection, pour que ce cache — reprises, fichier `.partiel`,
    trois tentatives, annulation, reniflement de signature — serve aussi un
    service qui n'est pas celui de l'IGN :

      * `url_a_la_resolution(url, resolution)` rend `(url, effective, largeur,
        hauteur)`. Le défaut réécrit `WIDTH`/`HEIGHT` d'une requête WMS, ce qui
        est propre à la Géoplateforme ; un ImageServer ArcGIS attend
        `size=largeur,hauteur` en un seul paramètre.
      * `apres_arrivee(chemin)` rend un suffixe de journal, vide s'il n'y a rien
        à dire. Le défaut remet d'aplomb le SCR bancal des dalles IGN. Le 3DEP
        américain, lui, livre un SCR propre mais **aucun sans-donnée** : c'est
        là qu'il faut le poser.

    Omettre les deux rend exactement le comportement d'origine.
    """
    url_a_la_resolution = url_a_la_resolution or url_a_resolution
    apres_arrivee = apres_arrivee or _corriger_et_dire
    from .geoplateforme import TransportRequests

    transport = transport or TransportRequests()
    journal = journal or JournalMuet()
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    manifeste = _lire_manifeste(dossier)

    chemins, total_octets, reprises = [], 0, 0
    for numero, dalle in enumerate(dalles, 1):
        if est_annule is not None and est_annule():
            journal.warning("Téléchargement interrompu à la dalle %d/%d." % (numero, len(dalles)))
            break

        url, effective, largeur, hauteur = url_a_la_resolution(dalle.url, resolution)
        cle = _cle(dalle.nom, effective)
        fichier = dossier / ("%s_%gm.tif" % (dalle.nom, effective))

        if not forcer and cle in manifeste and fichier.is_file() and fichier.stat().st_size > 0:
            chemins.append(fichier)
            reprises += 1
            continue

        octets = _telecharger_une(url, fichier, transport, tentatives,
                                  attendre=_attendeur(est_annule))
        mention = apres_arrivee(fichier)
        manifeste[cle] = {
            "fichier": fichier.name,
            "resolution": effective,
            "octets": octets,
            "horodatage": dalle.horodatage,
            "pixels": [largeur, hauteur],
        }
        chemins.append(fichier)
        total_octets += octets
        journal.info(
            "Dalle %d/%d : %s (%s, %d x %d px%s)"
            % (numero, len(dalles), dalle.nom, format_octets(octets), largeur, hauteur,
               mention)
        )

    _ecrire_manifeste(dossier, manifeste)
    if reprises:
        journal.info("%d dalle(s) reprise(s) du cache, %d telechargee(s) (%s)."
                     % (reprises, len(chemins) - reprises, format_octets(total_octets)))
    return chemins


def _attendeur(est_annule):
    """Rend une fonction d'attente qui reste sensible à l'annulation.

    On patiente par tranches d'une demi-seconde plutôt qu'en un seul `sleep` :
    un utilisateur qui annule pendant une attente de cinq secondes ne doit pas
    attendre la fin de cette attente.
    """
    import time

    def attendre(secondes):
        reste = float(secondes)
        while reste > 0.0:
            if est_annule is not None and est_annule():
                return False
            tranche = min(0.5, reste)
            time.sleep(tranche)
            reste -= tranche
        return True

    return attendre


def _telecharger_une(url, fichier, transport, tentatives, attendre=None):
    """Télécharge une dalle dans un fichier temporaire avant de la publier.

    Écrire directement sous le nom final laisserait, en cas de coupure, un
    fichier tronqué que le cache reprendrait ensuite pour argent comptant.

    Les tentatives sont ESPACÉES. Mesuré le 2026-09-09 : le 3DEP américain rend
    des HTTP 504 passagers, et trois tentatives immédiates n'en sont qu'une avec
    des étapes en plus — un service momentanément saturé l'est encore vingt
    millisecondes plus tard. L'attente reste courte (2 s puis 5 s) et sensible
    à l'annulation : elle ne rend pas le greffon sourd.
    """
    temporaire = fichier.with_suffix(fichier.suffix + ".partiel")
    dernier = None
    for essai in range(1, tentatives + 1):
        try:
            reponse = transport.obtenir(url, flux=True)
            if reponse.status_code != 200:
                raise ErreurGeoplateforme(
                    "HTTP %s sur %s" % (reponse.status_code, fichier.name)
                )
            octets = 0
            with open(temporaire, "wb") as sortie:
                for morceau in reponse.iter_content(1 << 16):
                    if morceau:
                        sortie.write(morceau)
                        octets += len(morceau)
            _verifier_contenu(temporaire, octets)
            if fichier.exists():
                os.remove(fichier)
            os.replace(temporaire, fichier)
            return octets
        except Exception as erreur:
            dernier = erreur
            if temporaire.exists():
                with contextlib.suppress(OSError):
                    os.remove(temporaire)
            if essai == tentatives:
                break
            if attendre is not None and not attendre(ATTENTES.get(essai, 5.0)):
                # Annulation pendant l'attente : inutile de reessayer.
                break
    raise ErreurGeoplateforme(
        "Échec du téléchargement de %s après %d tentative(s) : %s"
        % (fichier.name, tentatives, dernier)
    )
