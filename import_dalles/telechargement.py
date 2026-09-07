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

import json
import os
from pathlib import Path

from .geoplateforme import EPSG_TRAVAIL, ErreurGeoplateforme, format_octets, url_a_resolution

MANIFESTE = "_dalles.json"

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
    try:
        (dossier / MANIFESTE).write_text(
            json.dumps(manifeste, indent=1, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


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
                       forcer=False, tentatives=3, est_annule=None):
    """Télécharge les dalles à la résolution demandée. Renvoie les chemins.

    Une dalle déjà présente à la même résolution n'est pas retéléchargée, sauf
    `forcer`. La progression et le volume sont journalisés au fur et à mesure :
    on doit pouvoir arrêter en connaissance de cause.
    """
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

        url, effective, largeur, hauteur = url_a_resolution(dalle.url, resolution)
        cle = _cle(dalle.nom, effective)
        fichier = dossier / ("%s_%gm.tif" % (dalle.nom, effective))

        if not forcer and cle in manifeste and fichier.is_file() and fichier.stat().st_size > 0:
            chemins.append(fichier)
            reprises += 1
            continue

        octets = _telecharger_une(url, fichier, transport, tentatives)
        corrigee = corriger_georeference(fichier)
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
               ", georeferencement corrige" if corrigee else "")
        )

    _ecrire_manifeste(dossier, manifeste)
    if reprises:
        journal.info("%d dalle(s) reprise(s) du cache, %d telechargee(s) (%s)."
                     % (reprises, len(chemins) - reprises, format_octets(total_octets)))
    return chemins


def _telecharger_une(url, fichier, transport, tentatives):
    """Télécharge une dalle dans un fichier temporaire avant de la publier.

    Écrire directement sous le nom final laisserait, en cas de coupure, un
    fichier tronqué que le cache reprendrait ensuite pour argent comptant.
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
                try:
                    os.remove(temporaire)
                except OSError:
                    pass
            if essai == tentatives:
                break
    raise ErreurGeoplateforme(
        "Échec du téléchargement de %s après %d tentative(s) : %s"
        % (fichier.name, tentatives, dernier)
    )
