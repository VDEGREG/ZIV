# -*- coding: utf-8 -*-
"""Couches vectorielles de la Géoplateforme, et gravure dans un raster existant.

Deux services rendus au greffon, et rien d'autre :

  * `telecharger_couche` — une requête WFS bornée à une emprise, paginée, et
    écrite en GeoJSON. Sert au masquage forêt / bâti de la ZIV et au choix des
    points de vue photo ;
  * `graver_dans_raster` — écrit une valeur dans un raster **en place**, à partir
    d'un vecteur.

Deux choses à ne pas défaire, chèrement acquises :

  1. La gravure passe par l'**API GDAL directe** (`gdal.Rasterize` sur un jeu
     ouvert en écriture), jamais par les enveloppes Processing
     `gdal:rasterize_over*`. Celles-ci chargent le raster comme
     `QgsRasterLayer` et faisaient **planter QGIS** dans le fil d'exécution de
     Processing sur les rasters au système de coordonnées non standard. L'appel
     GDAL nu correspond à ce que fait `gdal_rasterize` en ligne de commande, qui
     lui fonctionne sur les mêmes fichiers.
  2. La gravure est **en place**, et non un masque séparé recombiné ensuite :
     `gdal:rasterize` marque toujours le 0 comme sans-donnée sur le masque, et
     le calculateur raster propageait ensuite ce sans-donnée sur toute la
     sortie — la ZIV entière devenait vide. Graver directement évite d'un coup
     le sans-donnée, l'alignement de grille et les questions de dimensions.

Le ton général est celui du reste du greffon : un échec ici n'arrête jamais un
calcul. On prévient, et on rend la ZIV telle quelle.
"""

import contextlib
import json
from pathlib import Path

# Point d'entrée WFS de la Géoplateforme, et codes de couches. Tous vérifiés par
# appel réel (DescribeFeatureType puis GetFeature) — un code de couche ne se
# devine pas, il se constate.
WFS_URL = "https://data.geopf.fr/wfs/ows"
EPSG_TRAVAIL = 2154

COUCHE_BATIMENTS = "BDTOPO_V3:batiment"
COUCHE_FORET = "LANDCOVER.FORESTINVENTORY.V2:formation_vegetale"
COUCHE_ROUTES = "BDTOPO_V3:troncon_de_route"

# Taille d'une page WFS. 5000 est le plafond **constaté** du service, pas une
# préférence : `COUNT=10000` rend 5000 entités, et le lien « next page » que le
# service fabrique lui-même porte `COUNT=5000`. Demander plus ne sert à rien.
PAS_DE_PAGE = 5000

# Garde-fou de volume **total**, toutes pages confondues : au-delà on cesse de
# paginer et on annonce la troncature. 200 000 couvre le pire cas réel avec de
# la marge — 147 440 bâtiments mesurés sur un rayon de 12 km, le plus vaste
# qu'une ZIV demande — tout en bornant le travail à 40 requêtes et le fichier
# produit à ~280 Mo. Ce n'est pas le plafond par requête : c'est `PAS_DE_PAGE`.
ENTITES_MAX = 200000

DELAI = (15, 120)


class JournalMuet:
    def info(self, message):
        pass

    def warning(self, message):
        pass


def emprise_de(objet):
    """Accepte un quadruplet ou un rectangle QGIS, rend un quadruplet."""
    if hasattr(objet, "xMinimum"):
        return (objet.xMinimum(), objet.yMinimum(), objet.xMaximum(), objet.yMaximum())
    return tuple(float(v) for v in objet)


def telecharger_couche(emprise, couche, chemin, journal=None, transport=None):
    """Télécharge une couche WFS bornée à l'emprise, en GeoJSON, page par page.

    Le service plafonne **chaque réponse** à `PAS_DE_PAGE` entités quoi qu'on
    demande. Une requête unique laissait donc au sol 79 à 97 % du bâti sur un
    rayon réel de 3 à 12 km — sans rien dire, puisque le test de troncature
    portait sur un plafond que le service n'atteint jamais. On boucle ici sur
    `STARTINDEX` jusqu'à ce qu'une page rende moins que demandé.

    Les pages sont écrites au fil de l'eau et non fusionnées en mémoire : un
    rayon de 12 km fait ~147 000 bâtiments, soit ~600 Mo de dictionnaires
    Python, contre ~20 Mo pour une page seule (mesuré). Effet de bord voulu :
    une panne en cours de pagination laisse sur le disque tout ce qui est déjà
    arrivé.

    Renvoie True si le fichier écrit contient au moins une entité. Tout échec —
    réseau, HTTP, réponse non JSON, couche vide — est **signalé et rendu False**,
    jamais levé : l'appelant doit pouvoir continuer sans la couche.
    """
    journal = journal or JournalMuet()
    chemin = Path(chemin)
    xmin, ymin, xmax, ymax = emprise_de(emprise)
    obtenir = transport or _transport_par_defaut()

    parametres = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": couche,
        "SRSNAME": "EPSG:%d" % EPSG_TRAVAIL,
        "BBOX": "%f,%f,%f,%f,EPSG:%d" % (xmin, ymin, xmax, ymax, EPSG_TRAVAIL),
        "OUTPUTFORMAT": "application/json",
        "COUNT": str(PAS_DE_PAGE),
    }

    fichier = None
    pages = 0
    total = 0
    alerte = ""
    try:
        while True:
            # Un dictionnaire neuf par page : un transport qui garderait la
            # reference verrait sinon le STARTINDEX des pages suivantes.
            entites, enveloppe, souci = _page_wfs(
                obtenir, dict(parametres, STARTINDEX=str(total)), couche)
            if souci:
                journal.warning(souci)
                if total == 0:
                    return False
                # Panne a la page N : les pages deja recues masquent deja du
                # bati. Les jeter parce que la suite manque priverait la ZIV de
                # tout pour la meme panne — on garde, et on dit que c'est
                # partiel.
                alerte = ("WFS IGN : %s tronquee a %d entites (%d page(s)) : la "
                          "page suivante a echoue." % (couche, total, pages))
                break

            pages += 1
            if entites:
                if fichier is None:
                    chemin.parent.mkdir(parents=True, exist_ok=True)
                    fichier = chemin.open("w", encoding="utf-8")
                    fichier.write(_entete_collection(enveloppe))
                morceau = ",".join(json.dumps(entite) for entite in entites)
                fichier.write(("," + morceau) if total else morceau)
                total += len(entites)

            # Une page moins pleine que demandee est la derniere : c'est le seul
            # signal fiable de fin, `numberMatched` etant absent de certaines
            # reponses.
            if len(entites) < PAS_DE_PAGE:
                break
            if total >= ENTITES_MAX:
                alerte = ("WFS IGN : %s tronquee au garde-fou de %d entites "
                          "(%d pages) : emprise trop vaste." % (couche, ENTITES_MAX, pages))
                break

        if fichier is None:
            journal.warning("WFS IGN : %s ne couvre pas cette emprise (0 entite)." % couche)
            return False
        fichier.write("]}")
        # Fermer ICI, dans le `try`, et non dans le `finally` : `close` est le
        # seul moment où la queue du tampon — le crochet `]}` compris — atteint
        # le disque. Une erreur avalée à la fermeture (disque plein, quota,
        # lecteur réseau tombé) ferait annoncer un téléchargement complet sur un
        # fichier tronqué, ce que l'ancien `Path.write_text` ne faisait pas :
        # son `with` propageait l'erreur de fermeture.
        fichier.close()
        fichier = None
    except OSError as erreur:
        journal.warning("WFS IGN : ecriture de %s impossible (%s)." % (chemin.name, erreur))
        # Fermer AVANT d'effacer : sous Windows un fichier encore ouvert ne peut
        # pas être supprimé, et c'est justement le cas quand c'est `close` qui a
        # échoué. Sans cet ordre, l'effacement ci-dessous ne servirait à rien.
        if fichier is not None:
            with contextlib.suppress(OSError):
                fichier.close()
            fichier = None
        # Un GeoJSON coupé au milieu d'une entité est illisible par OGR, et
        # l'étape des points de vue photo réutilise ce chemin sur son seul
        # `exists()` : le laisser en place empêcherait le re-téléchargement qui
        # aurait eu lieu sans lui.
        with contextlib.suppress(OSError):
            chemin.unlink()
        return False
    finally:
        # Filet : ne sert que si une exception a sauté la fermeture ci-dessus.
        if fichier is not None:
            with contextlib.suppress(OSError):
                fichier.close()

    if alerte:
        journal.warning(alerte)
    journal.info("WFS IGN : %s -> %s (%d entites, %d page(s))."
                 % (couche, chemin.name, total, pages))
    return True


def _page_wfs(obtenir, parametres, couche):
    """Une page de `GetFeature` : rend `(entites, enveloppe, souci)`.

    Rien n'est levé et rien n'est journalisé ici : `souci` non vide décrit la
    panne, et c'est l'appelant qui sait si elle est fatale (première page, donc
    aucune donnée) ou une simple troncature (page suivante).
    """
    try:
        reponse = obtenir(WFS_URL, parametres)
    except Exception as erreur:
        return [], {}, "WFS IGN : %s injoignable (%s)." % (couche, erreur)

    if reponse.status_code != 200:
        return [], {}, "WFS IGN : %s repond HTTP %s." % (couche, reponse.status_code)

    texte = reponse.text or ""
    if not texte.lstrip().startswith("{"):
        # Le service renvoie un ExceptionReport XML quand la requete lui deplait,
        # parfois avec un code 200 : le code HTTP ne suffit pas a conclure.
        return [], {}, ("WFS IGN : %s renvoie autre chose que du JSON (%s...)."
                        % (couche, texte[:160]))

    try:
        donnees = json.loads(texte)
    except ValueError as erreur:
        return [], {}, "WFS IGN : %s, JSON illisible (%s)." % (couche, erreur)

    return donnees.get("features") or [], donnees, ""


def _entete_collection(enveloppe):
    """Début du `FeatureCollection` fusionné, jusqu'au crochet des entités.

    Le membre `crs` de la première page est reporté tel quel, et c'est tout
    l'enjeu de cette fonction : sans lui, OGR lit les coordonnées Lambert 93
    comme des degrés, la reprojection échoue et la gravure ne grave rien — en
    silence. Les compteurs et le lien « next page », eux, ne décrivent que la
    page : les recopier mentirait sur le total fusionné.
    """
    debut = '{"type": "FeatureCollection"'
    if enveloppe.get("crs"):
        debut += ', "crs": %s' % json.dumps(enveloppe["crs"])
    return debut + ', "features": ['


def _transport_par_defaut():
    """Transport HTTP, isolé pour que les bancs puissent le remplacer."""
    def obtenir(url, parametres):
        import requests

        try:
            return requests.get(url, params=parametres, timeout=DELAI)
        except Exception as erreur:
            if "CERTIFICATE_VERIFY_FAILED" in str(erreur):
                raise RuntimeError(
                    "certificat TLS refuse (inspection SSL du poste ?) : installer "
                    "« truststore » et appeler truststore.inject_into_ssl()"
                )
            raise

    return obtenir


def graver_dans_raster(raster, vecteur, valeur=None, champ=None, ajouter=True, journal=None):
    """Grave un vecteur dans un raster existant, **en place**, sur la bande 1.

    Fournir soit `valeur` (constante), soit `champ` (attribut par entité).
    `ajouter` additionne à la valeur présente ; sinon elle est remplacée.
    Renvoie True si la gravure a eu lieu. Voir l'en-tête du module pour les
    deux raisons de ne pas revenir aux enveloppes Processing.
    """
    journal = journal or JournalMuet()
    if (valeur is None) == (champ is None):
        journal.warning("Gravure : fournir soit une valeur, soit un champ, pas les deux.")
        return False

    try:
        from osgeo import gdal
    except Exception as erreur:
        journal.warning("Gravure : GDAL indisponible (%s)." % erreur)
        return False

    gdal.UseExceptions()
    options = {"bands": [1], "add": bool(ajouter)}
    if champ is not None:
        options["attribute"] = champ
    else:
        options["burnValues"] = [float(valeur)]

    jeu = None
    try:
        jeu = gdal.Open(str(raster), gdal.GA_Update)
        if jeu is None:
            journal.warning("Gravure : %s n'a pas pu etre ouvert en ecriture." % raster)
            return False
        gdal.Rasterize(jeu, str(vecteur), options=gdal.RasterizeOptions(**options))
        jeu.FlushCache()
        return True
    except Exception as erreur:
        journal.warning("Gravure : echec sur %s (%s)." % (Path(str(raster)).name, erreur))
        return False
    finally:
        # Liberer le jeu avant de rendre la main : sous Windows, le fichier reste
        # verrouille tant que la derniere reference vit.
        jeu = None
