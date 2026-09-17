# -*- coding: utf-8 -*-
"""Fournisseurs de données par pays : le protocole, et le choix.

Ce paquet répond à une seule question : **où prend-on le terrain, la végétation,
le bâti et les routes ?** Tout le reste de la chaîne — emprise, tampon, moteur
de visibilité, symbologie, points de vue, cartouche — est commun aux deux pays
et ne connaît pas ce paquet autrement que par le protocole ci-dessous.

Le protocole déclare ses **capacités** plutôt que de les supposer, parce que les
deux fournisseurs sont réellement dissemblables :

  * la France livre un **MNS** — la canopée et les toitures sont déjà dans le
    terrain. Il n'y a rien à graver : `occulte()` rend faux.
  * les États-Unis ne livrent qu'un terrain nu. L'occultation se **reconstruit**
    par gravure de la végétation et du bâti : `occulte()` rend vrai.
  * la France masque le résultat par des empreintes **vectorielles** (BD Forêt),
    les États-Unis par un **seuil sur un raster** de hauteur déjà rapatrié.
  * les États-Unis ont un mode replié en résolution (1 m si le LiDAR couvre,
    ~10 m sinon), la France non.

Un fournisseur qui ne sait pas faire quelque chose le dit ; il ne lève pas.
"""

import math

# Emprises de couverture, en degrés WGS84 (lon_min, lat_min, lon_max, lat_max).
#
# Ce sont des garde-fous de premier niveau, pas la vérité : ils servent à rendre
# un refus LISIBLE plutôt qu'un « zéro dalle » incompréhensible trois étapes plus
# loin. L'autorité reste le service, qui rend du sans-donnée hors de sa
# couverture réelle.
COUVERTURE_FRANCE = (-5.3, 41.2, 9.7, 51.2)   # métropole, comme le garde-fou d'origine
COUVERTURE_CONUS = (-125.1, 24.4, -66.8, 49.5)  # 48 États contigus + DC


def fuseau_utm_nord(longitude):
    """Code EPSG du fuseau UTM WGS84 nord contenant cette longitude."""
    return 32600 + int(math.floor((longitude + 180.0) / 6.0)) + 1


def _dans(bbox, lon, lat):
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


class Fournisseur(object):
    """Ce que la chaîne attend d'un pays. Les défauts sont les plus prudents."""

    ID = ""       # identifiant technique, journalisé
    NOM = ""      # nom lisible, pour le journal et le cartouche
    PAYS = ""

    # ---- couverture et système de coordonnées ----

    COUVERTURE = None  # (lon_min, lat_min, lon_max, lat_max) en WGS84

    def couvre(self, lon, lat):
        return self.COUVERTURE is not None and _dans(self.COUVERTURE, lon, lat)

    def couverture_lisible(self):
        """La couverture en clair, pour le message de refus."""
        if self.COUVERTURE is None:
            return "(couverture non déclarée)"
        return ("longitude %.1f à %.1f, latitude %.1f à %.1f"
                % (self.COUVERTURE[0], self.COUVERTURE[2],
                   self.COUVERTURE[1], self.COUVERTURE[3]))

    def crs_travail(self, lon, lat):
        """Code EPSG projeté métrique dans lequel toute la chaîne travaille."""
        raise NotImplementedError

    def fournit_terrain(self):
        """Le terrain passe-t-il par ce fournisseur, ou par la chaîne d'origine ?

        La décision n°15 du plan fait de la chaîne française une **branche
        parallèle**, laissée rigoureusement inchangée : son terrain continue de
        passer par `import_dalles/adaptateur.py`, appelé directement par
        l'algorithme. Seul le fournisseur américain fournit son terrain par ce
        protocole. Un fournisseur qui rend faux ici n'implémente pas `terrain()`,
        et c'est voulu — pas un oubli.
        """
        return False

    def resolution_native(self, bbox_wgs84, journal=None):
        """Résolution la plus fine que le fournisseur sert vraiment ici, en m.

        Sert à borner la résolution de travail : demander plus fin que le natif
        ne rend pas d'information, seulement du volume et du faux détail.
        """
        raise NotImplementedError

    def resolution_de_travail(self, demandee, bbox_wgs84, journal=None):
        """La résolution à laquelle le calcul tournera vraiment, en mètres.

        Le défaut rend la résolution demandée telle quelle : c'est le cas de la
        France, dont la chaîne borne déjà au natif dans `url_a_resolution()`.
        Un fournisseur qui sert une mosaïque transparente — 3DEP accepte
        n'importe quelle résolution et interpole sans le dire — doit borner ici,
        et le dire au journal.
        """
        return demandee

    # ---- terrain ----

    def terrain(self, bbox, resolution, epsg, dossier, journal=None, est_annule=None):
        """Rend le chemin d'un raster d'élévation couvrant `bbox` dans `epsg`."""
        raise NotImplementedError

    # ---- occultation : reconstruite, ou déjà dans le terrain ----

    def occulte(self):
        """Faut-il graver la végétation et le bâti sur le terrain ?"""
        return False

    def graver_occultation(self, chemin_terrain, bbox, epsg, dossier, journal=None):
        """Grave la surface occultante EN PLACE. Rend un dict de statistiques."""
        return {}

    # ---- masquage du résultat ----

    def masquer_resultat(self, chemin_ziv, chemin_demande, dossier,
                         journal=None):
        """Force « hors visibilité » là où personne ne peut se tenir.

        Rend le chemin du raster masqué, ou celui d'origine si rien n'a pu être
        appliqué. Ne lève jamais : une ZIV non masquée reste exploitable, pas de
        ZIV du tout, non.
        """
        return str(chemin_ziv)

    # ---- routes, pour les points de vue photo ----

    def routes(self, bbox, epsg, chemin, journal=None):
        """Écrit les routes de l'emprise en GeoJSON. Rend True si non vide."""
        return False


def fournisseurs():
    """Les fournisseurs connus, dans l'ordre où ils sont interrogés."""
    from .france import FournisseurFrance
    from .usa import FournisseurUSA

    return [FournisseurFrance(), FournisseurUSA()]


class AucuneCouverture(RuntimeError):
    """Aucun fournisseur ne couvre cette emprise — et on dit lesquels ont été testés."""


def choisir(lon, lat, force=None, journal=None):
    """Le fournisseur qui couvre ce point, ou celui que l'utilisateur impose.

    `lon`/`lat` sont en degrés WGS84 : c'est le centroïde de l'emprise, reprojeté
    par l'appelant. Les deux couvertures ne se recoupent pas, donc la détection
    est sans ambiguïté.

    En cas d'échec, le message énumère les couvertures testées. C'est mieux que
    le comportement d'origine, qui avertissait puis rendait zéro dalle : ici on
    sait tout de suite que le problème est l'emprise, et pas le service.
    """
    connus = fournisseurs()
    if force:
        for fournisseur in connus:
            if fournisseur.ID == force:
                if journal is not None:
                    journal.info("Fournisseur imposé : %s." % fournisseur.NOM)
                return fournisseur
        raise AucuneCouverture(
            "Fournisseur inconnu : %r (attendu : %s)"
            % (force, ", ".join(f.ID for f in connus)))

    for fournisseur in connus:
        if fournisseur.couvre(lon, lat):
            if journal is not None:
                journal.info("Fournisseur retenu : %s (centroïde %.4f, %.4f)."
                             % (fournisseur.NOM, lon, lat))
            return fournisseur

    raise AucuneCouverture(
        "Aucun fournisseur ne couvre le centroïde de l'emprise (%.4f, %.4f en "
        "WGS84). Couvertures testées : %s. Vérifier le SCR de la couche "
        "d'emprise : une emprise mal projetée atterrit hors de tout pays."
        % (lon, lat, " ; ".join("%s %s" % (f.NOM, f.couverture_lisible())
                                for f in connus)))
