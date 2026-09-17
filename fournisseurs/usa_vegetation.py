# -*- coding: utf-8 -*-
"""Surface occultante végétale : LANDFIRE EVH, complété par le CH.

Les États-Unis ne publient pas de MNS. La canopée est donc **gravée** sur le
terrain nu, à partir de deux produits LANDFIRE du même millésime.

Ce que le lot 0 a établi, et sur quoi ce module s'appuie :

  * le millésime **LF2023** est retenu parce qu'il réunit les deux conditions —
    service complet sur les six points de contrôle CONUS **et** table
    d'attributs publiée. LF2025, le plus récent, rend `NoData` sur cinq points
    sur six : le prendre aurait donné une canopée vide sur presque tout le pays ;
  * l'**EVH** ne porte pas des classes de hauteur mais la hauteur **au mètre
    entier**, par décalage de code. Décodage vérifié sur 139 classes de la table
    publiée, zéro désaccord ;
  * les codes `13` à `17` décrivent une végétation en zone bâtie **sans lui
    donner de hauteur**, et pèsent un quart de l'emprise de test. Le **CH**
    (hauteur de canopée) les comble : mesuré, 100 % des pixels des trois classes
    de forêt, de 19 à 27 m. Mais le CH est un produit de *combustible* et rend
    `Non-Forested` sur l'herbacé et l'arbustif — d'où un forfait pour `16` et
    `17`, tous deux de toute façon sous la hauteur d'œil de 1,70 m ;
  * là où les deux produits parlent, ils s'accordent sur **99,8 %** des pixels,
    écart médian 1,0 m. C'est ce contrôle qui autorise à se servir du CH là où
    l'EVH se taît.

Un piège de traitement, propre à la donnée thématique : **ne jamais
interpoler**. Entre les codes 199 (arbre de 99 m) et 201 (arbuste de 0,1 m), une
interpolation fabriquerait un 200 qui n'existe pas dans la table. Tous les
rééchantillonnages ici sont au plus proche voisin, imposés explicitement et non
laissés au défaut du service.
"""

import math
import os
import urllib.parse

MILLESIME = "LF2023"
RACINE = "https://lfps.usgs.gov/arcgis/rest/services"
EVH = "%s/Landfire_%s/%s_EVH_CONUS/ImageServer" % (RACINE, MILLESIME, MILLESIME)
CH = "%s/Landfire_%s/%s_CH_CONUS/ImageServer" % (RACINE, MILLESIME, MILLESIME)

RESOLUTION = 30.0     # maille native des deux produits
SANS_DONNEE = -9999   # déclaré par les deux services

# Forfaits, assumés et journalisés. Les codes 13 à 15 ne les atteignent
# qu'exceptionnellement — le CH les comble à 100 % là où on a mesuré — mais un
# repli muet serait pire qu'un repli chiffré.
FORFAIT_ARBRE_URBAIN = 12.0   # 13/14/15 si le CH ne dit rien
FORFAIT_HERBACE = 0.5         # 16, Developed-Upland Herbaceous
FORFAIT_ARBUSTIF = 1.5        # 17, Developed-Upland Shrubland

# Seuil du masque forêt. Hypothèse à énoncer : en dessous, un couvert n'empêche
# ni de se tenir debout ni de voir au loin. 5 m écarte les jeunes plantations et
# les haies basses tout en gardant les peuplements réels.
SEUIL_FORET = 5.0

# Valeur ecrite pour « hors visibilite ». Reprise de `masquage.py` et non
# redefinie : la chaine lit 0 comme non vu, 0,1 pour l'empreinte du projet et
# 1 et plus pour le visible. Deux definitions de cette convention finiraient
# par diverger, et la moitie de la chaine masquerait a cote.
HORS_VISIBILITE = 0.0

# Classes ligneuses, celles qui peuvent masquer un observateur. L'herbacé en est
# exclu même à 1 m : on voit par-dessus.
CODES_FORET_BATIE = (13, 14, 15)   # Developed-Upland Deciduous / Evergreen / Mixed
CODE_HERBACE_BATI = 16
CODE_ARBUSTIF_BATI = 17

ETIQUETTES = {
    11: "Open Water", 12: "Snow/Ice",
    13: "Developed-Upland Deciduous Forest", 14: "Developed-Upland Evergreen Forest",
    15: "Developed-Upland Mixed Forest", 16: "Developed-Upland Herbaceous",
    17: "Developed-Upland Shrubland", 21: "Developed - Open Space",
    22: "Developed - Low Intensity", 23: "Developed - Medium Intensity",
    24: "Developed - High Intensity", 25: "Developed-Roads", 31: "Barren",
    32: "Quarries", 61: "NASS-Vineyard", 63: "NASS-Row/Close Grown Crop",
    64: "NASS-Row Crop", 65: "NASS-Close Grown Crop", 68: "NASS-Wheat",
    69: "NASS-Aquaculture", 82: "Cultivated Crops", 100: "Sparse Vegetation Canopy",
}


class ErreurVegetation(RuntimeError):
    """Le service a répondu autre chose que ce qu'on lui demandait."""


class JournalMuet:
    def info(self, message):
        pass

    def warning(self, message):
        pass


def etiquette(code):
    """Nom de classe d'un code EVH, pour le journal."""
    if 101 <= code <= 199:
        return "Tree Height = %d m" % (code - 100)
    if 201 <= code <= 230:
        return "Shrub Height = %.1f m" % ((code - 200) / 10.0)
    if 301 <= code <= 310:
        return "Herb Height = %.1f m" % ((code - 300) / 10.0)
    return ETIQUETTES.get(int(code), "code %s" % code)


# ---------------------------------------------------------------------------
# Décodage, vérifié au lot 0 contre la table publiée
# ---------------------------------------------------------------------------

def hauteurs_evh(codes):
    """Hauteur en mètres d'un tableau de codes EVH ; `nan` si la classe n'en porte pas.

    Décalages constatés sur la table publiée du millésime : arbres 101-199 au
    mètre entier, arbustes 201-230 et herbacé 301-310 au décimètre.
    """
    import numpy as np

    codes = np.asarray(codes)
    hauteur = np.full(codes.shape, np.nan, dtype="float32")
    arbres = (codes >= 101) & (codes <= 199)
    arbustes = (codes >= 201) & (codes <= 230)
    herbes = (codes >= 301) & (codes <= 310)
    hauteur[arbres] = (codes[arbres] - 100).astype("float32")
    hauteur[arbustes] = (codes[arbustes] - 200).astype("float32") / 10.0
    hauteur[herbes] = (codes[herbes] - 300).astype("float32") / 10.0
    return hauteur


def hauteurs_ch(valeurs):
    """Hauteur en mètres d'un tableau de valeurs CH ; `nan` s'il n'y en a pas.

    Le CH est binné par tranches de 4 m et sa valeur est le **centre de classe
    en décimètres**. La valeur 0 signifie « Non-Forested » : c'est une absence,
    pas une hauteur nulle — les confondre reviendrait à raser une forêt.
    """
    import numpy as np

    valeurs = np.asarray(valeurs)
    hauteur = np.full(valeurs.shape, np.nan, dtype="float32")
    utiles = valeurs > 0
    hauteur[utiles] = valeurs[utiles].astype("float32") / 10.0
    return hauteur


def surface_vegetale(codes_evh, valeurs_ch):
    """Hauteur occultante à graver, et la répartition de ses provenances.

    Rend `(hauteur, provenance)` où `hauteur` est un tableau float32 en mètres,
    nul là où rien ne masque, et `provenance` un dict de comptes par origine —
    parce qu'un livrable doit pouvoir dire d'où vient ce qu'il montre.
    """
    import numpy as np

    codes = np.asarray(codes_evh).astype("int32")
    hauteur = hauteurs_evh(codes)
    provenance = {"evh": int(np.isfinite(hauteur).sum())}

    # Les classes 13 à 17 : de la vegetation en zone batie, occultante mais sans
    # hauteur dans l'EVH. Le CH comble les trois classes de foret ; l'herbace et
    # l'arbustif retombent sur un forfait, le CH y rendant Non-Forested.
    depuis_ch = hauteurs_ch(valeurs_ch)
    for code in CODES_FORET_BATIE:
        ici = (codes == code)
        if not ici.any():
            continue
        comble = ici & np.isfinite(depuis_ch)
        hauteur[comble] = depuis_ch[comble]
        provenance["ch"] = provenance.get("ch", 0) + int(comble.sum())
        reste = ici & ~np.isfinite(depuis_ch)
        hauteur[reste] = FORFAIT_ARBRE_URBAIN
        provenance["forfait_arbre"] = provenance.get("forfait_arbre", 0) + int(reste.sum())

    for code, forfait, cle in ((CODE_HERBACE_BATI, FORFAIT_HERBACE, "forfait_herbace"),
                               (CODE_ARBUSTIF_BATI, FORFAIT_ARBUSTIF, "forfait_arbustif")):
        ici = (codes == code)
        if ici.any():
            hauteur[ici] = forfait
            provenance[cle] = int(ici.sum())

    # Tout le reste — eau, neige, bâti, roche, cultures, végétation éparse — ne
    # masque pas. Le bâti est gravé séparément, depuis USA Structures.
    hauteur = np.where(np.isfinite(hauteur), hauteur, 0.0).astype("float32")
    provenance["sans_occultation"] = int((hauteur == 0.0).sum())
    return hauteur, provenance


def masque_ligneux(codes_evh, hauteur, seuil=SEUIL_FORET):
    """Où un observateur ne peut ni se tenir ni voir : sous un couvert ligneux.

    C'est l'équivalent de BD Forêt côté français, mais fondé sur la hauteur
    réellement mesurée et non sur une classification. La cohérence est acquise
    par construction : on occulte et on masque avec **le même** tableau de
    hauteur, donc la surface et le masque ne peuvent pas se contredire en
    lisière — ce qui laisserait des anneaux de cellules « visibles » autour de
    chaque bois.
    """
    import numpy as np

    codes = np.asarray(codes_evh).astype("int32")
    ligneux = ((codes >= 101) & (codes <= 199)) | np.isin(
        codes, CODES_FORET_BATIE + (CODE_ARBUSTIF_BATI,))
    return ligneux & (np.asarray(hauteur) >= seuil)


# ---------------------------------------------------------------------------
# Rapatriement des deux rasters, à leur maille native
# ---------------------------------------------------------------------------

def _transport_defaut():
    from ..import_dalles.geoplateforme import TransportRequests

    return TransportRequests()


def _grille_alignee(bbox, resolution):
    """Emprise élargie aux bords de la grille de `resolution`.

    Un pixel de 30 m ne se coupe pas en deux : demander une emprise quelconque
    laisserait le service recaler sa grille sur elle, ce qui décale les classes
    d'un demi-pixel et brouille les lisières.
    """
    xmin = math.floor(bbox[0] / resolution) * resolution
    ymin = math.floor(bbox[1] / resolution) * resolution
    xmax = math.ceil(bbox[2] / resolution) * resolution
    ymax = math.ceil(bbox[3] / resolution) * resolution
    largeur = max(int(round((xmax - xmin) / resolution)), 1)
    hauteur = max(int(round((ymax - ymin) / resolution)), 1)
    return (xmin, ymin, xmax, ymax), largeur, hauteur


def _telecharger(service, bbox, epsg, chemin, transport, journal):
    """Un raster thématique, à sa maille native, au plus proche voisin."""
    emprise, largeur, hauteur = _grille_alignee(bbox, RESOLUTION)
    params = {
        "bbox": "%f,%f,%f,%f" % emprise,
        "bboxSR": str(epsg), "imageSR": str(epsg),
        "size": "%d,%d" % (largeur, hauteur),
        "format": "tiff", "f": "image",
        # Impose : une donnee thematique ne s'interpole pas.
        "interpolation": "RSP_NearestNeighbor",
    }
    reponse = transport.obtenir("%s/exportImage" % service, params=params)
    if getattr(reponse, "status_code", 0) != 200:
        raise ErreurVegetation("HTTP %s sur %s" % (
            getattr(reponse, "status_code", "?"), service[:70]))
    octets = reponse.content
    # Le Content-Type mentait cote 3DEP — image/tiff sur un corps JSON. Seule la
    # signature tranche, ici comme la-bas.
    if octets[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        debut = octets[:200].decode("utf-8", "replace")
        raise ErreurVegetation(
            "%s a renvoyé %d octets qui ne sont pas un GeoTIFF : %s"
            % (service.rsplit("/", 2)[-2], len(octets), debut))
    with open(chemin, "wb") as flux:
        flux.write(octets)
    journal.info("LANDFIRE %s : %d x %d cellules de %g m."
                 % (service.rsplit("/", 2)[-2], largeur, hauteur, RESOLUTION))
    return chemin


def rasters(bbox, epsg, dossier, transport=None, journal=None):
    """Rapatrie l'EVH et le CH sur l'emprise, à 30 m. Rend leurs deux chemins.

    On rapatrie à la maille native puis on rééchantillonne localement, plutôt
    que de demander la grille de travail au service : un rayon de 12 km à 2 m
    ferait 12 000 px de côté, soit 288 Mo pour une donnée qui n'en contient que
    640 ko à sa vraie finesse.
    """
    transport = transport or _transport_defaut()
    journal = journal or JournalMuet()
    chemin_evh = os.path.join(dossier, "landfire_evh_%s.tif" % MILLESIME)
    chemin_ch = os.path.join(dossier, "landfire_ch_%s.tif" % MILLESIME)
    _telecharger(EVH, bbox, epsg, chemin_evh, transport, journal)
    _telecharger(CH, bbox, epsg, chemin_ch, transport, journal)
    return chemin_evh, chemin_ch


# ---------------------------------------------------------------------------
# Écriture et gravure
# ---------------------------------------------------------------------------

def _lire(chemin):
    from osgeo import gdal

    gdal.UseExceptions()
    jeu = gdal.Open(str(chemin))
    tableau = jeu.GetRasterBand(1).ReadAsArray()
    modele = (jeu.GetGeoTransform(), jeu.GetProjection(),
              jeu.RasterXSize, jeu.RasterYSize)
    jeu = None
    return tableau, modele


def _ecrire(chemin, tableau, modele, type_gdal=None):
    from osgeo import gdal

    gdal.UseExceptions()
    geotransform, projection, colonnes, lignes = modele
    type_gdal = type_gdal or gdal.GDT_Float32
    pilote = gdal.GetDriverByName("GTiff")
    jeu = pilote.Create(str(chemin), colonnes, lignes, 1, type_gdal,
                        options=["COMPRESS=DEFLATE", "TILED=YES"])
    jeu.SetGeoTransform(geotransform)
    jeu.SetProjection(projection)
    jeu.GetRasterBand(1).WriteArray(tableau)
    jeu.FlushCache()
    jeu = None
    return chemin


def surface_sur_disque(chemin_evh, chemin_ch, dossier, journal=None):
    """Écrit le raster de hauteur végétale à 30 m. Rend (chemin, provenance)."""
    journal = journal or JournalMuet()
    codes, modele = _lire(chemin_evh)
    valeurs_ch, _ = _lire(chemin_ch)
    if codes.shape != valeurs_ch.shape:
        raise ErreurVegetation(
            "EVH et CH ne se superposent pas : %r contre %r — les deux emprises "
            "doivent être alignées sur la même grille de 30 m."
            % (codes.shape, valeurs_ch.shape))

    hauteur, provenance = surface_vegetale(codes, valeurs_ch)
    chemin = _ecrire(os.path.join(dossier, "surface_vegetale_30m.tif"),
                     hauteur, modele)

    total = int(codes.size)
    journal.info(
        "Surface végétale : %.1f %% de l'emprise occultée — %.1f %% par l'EVH, "
        "%.1f %% comblés par le CH, %.1f %% au forfait."
        % (100.0 * (total - provenance["sans_occultation"]) / total,
           100.0 * provenance.get("evh", 0) / total,
           100.0 * provenance.get("ch", 0) / total,
           100.0 * sum(v for c, v in provenance.items()
                       if c.startswith("forfait")) / total))
    return chemin, provenance


def masquer_par_raster(chemin_ziv, chemin_evh, chemin_surface, seuil=SEUIL_FORET,
                       journal=None):
    """Force « hors visibilité » sous le couvert ligneux, EN PLACE dans la ZIV.

    L'équivalent américain de la gravure des empreintes BD Forêt : sur une
    surface qui porte la canopée, le calcul de visibilité traite les cimes
    comme des points hauts qui « voient » le projet. Personne ne se tient dans
    une cime — ces cellules sont des artefacts.

    Trois choses reprises de `masquage.py`, chèrement acquises et à ne pas
    défaire :

      1. la gravure est **en place**, jamais un masque séparé recombiné au
         calculateur raster, qui propagerait son sans-donnée sur toute la
         sortie et viderait la ZIV entière ;
      2. la valeur écrite est `0`, que la chaîne lit comme « non vu » — 0,1
         désigne l'empreinte du projet et 1 et plus le visible ;
      3. rien ici n'est bloquant. Une ZIV non masquée reste exploitable ; pas
         de ZIV du tout, non.

    Le masque arrive à 30 m et la ZIV est à la maille de travail : on
    rééchantillonne le MASQUE sur la grille de la ZIV, au plus proche voisin,
    jamais l'inverse.

    Rend le nombre de cellules forcées à zéro.
    """
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    journal = journal or JournalMuet()

    codes, modele = _lire(chemin_evh)
    hauteur, _ = _lire(chemin_surface)
    if codes.shape != hauteur.shape:
        journal.warning("Masquage : EVH et surface ne se superposent pas "
                        "(%r contre %r) — masquage sauté." % (codes.shape, hauteur.shape))
        return 0

    ligneux = masque_ligneux(codes, hauteur, seuil)
    if not ligneux.any():
        journal.info("Masquage forêt : aucun couvert ligneux au-dessus de %g m "
                     "sur l'emprise." % seuil)
        return 0

    # Le masque part sur disque en octets : gdal.Warp a besoin d'une source
    # georeferencee pour recaler, et un tableau numpy nu n'en est pas une.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ziv_masque_") as temporaire:
        chemin_masque = _ecrire(os.path.join(temporaire, "ligneux_30m.tif"),
                                ligneux.astype("uint8"), modele, gdal.GDT_Byte)

        ziv = gdal.Open(str(chemin_ziv), gdal.GA_Update)
        if ziv is None:
            journal.warning("Masquage : ZIV illisible en écriture (%s) — sauté."
                            % chemin_ziv)
            return 0
        try:
            geotransform = ziv.GetGeoTransform()
            colonnes, lignes = ziv.RasterXSize, ziv.RasterYSize
            emprise = (geotransform[0],
                       geotransform[3] + lignes * geotransform[5],
                       geotransform[0] + colonnes * geotransform[1],
                       geotransform[3])
            # Le systeme de la ZIV, et un repli sur celui du masque. Sans ce
            # garde-fou, `gdal.Warp` recevait un `-t_srs` VIDE et levait
            # « Invalid SRS for -t_srs » — message qui designe l'etape en cours
            # alors que la faute est trois etapes plus haut : un terrain sans
            # projection donne un viewshed sans projection, que `gdal:merge`
            # propage a la ZIV sans un mot. Signale le 2026-09-14.
            projection = ziv.GetProjection()
            if not projection:
                masque = gdal.Open(str(chemin_masque))
                projection = masque.GetProjection() if masque is not None else ""
                masque = None
                journal.warning(
                    "Masquage : la ZIV ne porte AUCUNE projection. Le masque "
                    "est cale sur %s a la place — mais le defaut vient d'avant, "
                    "un terrain sans systeme de coordonnees."
                    % ("celle du raster de vegetation" if projection
                       else "rien, et le calage va echouer"))
            cale = gdal.Warp("", chemin_masque, format="MEM",
                             outputBounds=emprise, width=colonnes, height=lignes,
                             dstSRS=projection, resampleAlg="near")
            sur_grille = np.asarray(cale.GetRasterBand(1).ReadAsArray()) > 0
            cale = None

            bande = ziv.GetRasterBand(1)
            valeurs = np.asarray(bande.ReadAsArray(), dtype="float32")
            a_masquer = sur_grille & (valeurs > HORS_VISIBILITE)
            nombre = int(a_masquer.sum())
            if nombre:
                valeurs[a_masquer] = HORS_VISIBILITE
                bande.WriteArray(valeurs)
                bande.ComputeStatistics(False)
                ziv.FlushCache()
            journal.info("Masquage forêt : %d cellule(s) forcées hors visibilité "
                         "sous un couvert de %g m ou plus." % (nombre, seuil))
            return nombre
        finally:
            # Sous Windows le fichier reste verrouille tant que la derniere
            # reference vit : la liberer avant de rendre la main.
            ziv = None


def grille_de(chemin_reference):
    """Emprise, taille et projection d'un raster — de quoi caler un autre dessus."""
    from osgeo import gdal

    gdal.UseExceptions()
    jeu = gdal.Open(str(chemin_reference))
    if jeu is None:
        raise ErreurVegetation("Raster de référence illisible : %s" % chemin_reference)
    try:
        gt = jeu.GetGeoTransform()
        colonnes, lignes = jeu.RasterXSize, jeu.RasterYSize
        return {
            "emprise": (gt[0], gt[3] + lignes * gt[5],
                        gt[0] + colonnes * gt[1], gt[3]),
            "colonnes": colonnes,
            "lignes": lignes,
            "projection": jeu.GetProjection(),
            "geotransform": gt,
        }
    finally:
        jeu = None


def hauteur_sur_grille(chemin_reference, chemin_hauteur):
    """Rééchantillonne un raster de hauteur sur la grille exacte d'un autre.

    Au **plus proche voisin**, et dans ce sens-là seulement : la hauteur arrive
    à 30 m, le terrain est à la maille de travail. Rééchantillonner le terrain
    sur 30 m reviendrait à jeter le relief qu'on a payé pour obtenir.
    """
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    grille = grille_de(chemin_reference)
    cale = gdal.Warp("", str(chemin_hauteur), format="MEM",
                     outputBounds=grille["emprise"],
                     width=grille["colonnes"], height=grille["lignes"],
                     dstSRS=grille["projection"], resampleAlg="near")
    if cale is None:
        raise ErreurVegetation("Calage impossible : %s" % chemin_hauteur)
    tableau = np.asarray(cale.GetRasterBand(1).ReadAsArray(), dtype="float32")
    cale = None
    return np.where(np.isfinite(tableau), tableau, 0.0).astype("float32")


def graver_hauteur(chemin_terrain, hauteur, journal=None, libelle="Gravure"):
    """Ajoute un tableau de hauteurs au terrain, EN PLACE.

    Le tableau doit déjà être sur la grille du terrain — voir
    `hauteur_sur_grille`. Rend `(cellules, maximum)`.

    Les trous de couverture sont laissés intacts : additionner 20 m à -9999
    fabriquerait une altitude absurde au lieu de laisser un trou visible.
    """
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    journal = journal or JournalMuet()

    terrain = gdal.Open(str(chemin_terrain), gdal.GA_Update)
    if terrain is None:
        raise ErreurVegetation("Terrain illisible en écriture : %s" % chemin_terrain)
    try:
        bande = terrain.GetRasterBand(1)
        altitudes = np.asarray(bande.ReadAsArray(), dtype="float32")
        if altitudes.shape != np.asarray(hauteur).shape:
            raise ErreurVegetation(
                "Hauteur et terrain ne se superposent pas : %r contre %r."
                % (np.asarray(hauteur).shape, altitudes.shape))
        sans_donnee = bande.GetNoDataValue()
        valides = np.isfinite(altitudes)
        if sans_donnee is not None:
            valides &= (altitudes != np.float32(sans_donnee))
        gravable = valides & (np.asarray(hauteur) > 0.0)
        altitudes[gravable] += np.asarray(hauteur, dtype="float32")[gravable]
        bande.WriteArray(altitudes)
        bande.ComputeStatistics(False)
        terrain.FlushCache()

        nombre = int(gravable.sum())
        maximum = float(np.asarray(hauteur)[gravable].max()) if nombre else 0.0
        journal.info("%s : %d cellule(s) surélevées, jusqu'à %.1f m."
                     % (libelle, nombre, maximum))
        return nombre, maximum
    finally:
        # Sous Windows le fichier reste verrouille tant que la derniere
        # reference vit : la liberer avant de rendre la main.
        terrain = None


def graver_sur_terrain(chemin_terrain, chemin_surface, journal=None):
    """Ajoute la hauteur végétale au terrain, EN PLACE, sur sa grille.

    Conservé pour le cas où la végétation est la seule occultation. Quand il y
    a aussi du bâti, passer par `hauteur_sur_grille` + `graver_hauteur` sur le
    **maximum** des deux : les additionner surélèverait deux fois une maison
    sous les arbres.

    Rend la hauteur maximale gravée, ou 0.0 si rien ne l'a été.
    """
    hauteur = hauteur_sur_grille(chemin_terrain, chemin_surface)
    _nombre, maximum = graver_hauteur(chemin_terrain, hauteur, journal,
                                      libelle="Gravure végétale")
    return maximum
