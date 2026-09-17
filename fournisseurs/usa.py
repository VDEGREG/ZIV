# -*- coding: utf-8 -*-
"""Fournisseur États-Unis : le terrain 3DEP, et la surface reconstruite.

Les États-Unis n'ont **pas de MNS national**. C'est le fait qui commande toute
la forme de ce fournisseur : là où l'IGN livre une surface prête à l'emploi —
canopée et toitures incluses — l'USGS ne publie qu'un terrain nu. La surface
occultante est donc **reconstruite** par gravure, sur trois sources mesurées :

  * terrain : USGS 3DEP, servi à la résolution et dans le fuseau demandés ;
  * végétation : LANDFIRE EVH (hauteur au mètre entier), complété par le CH
    là où l'EVH décrit une végétation sans lui donner de hauteur ;
  * bâti : USA Structures (FEMA / ORNL), hauteur mesurée quand elle existe,
    forfait par classe d'occupation sinon.

Périmètre : les 48 États contigus plus le district de Columbia. L'Alaska,
Hawaï et les territoires sont hors de cette version — chacun a son propre
point d'accès LANDFIRE, et l'Alaska ajoute l'antiméridien, qui ferait dérailler
en silence le calcul du fuseau UTM par centroïde.
"""

from . import COUVERTURE_CONUS, Fournisseur, fuseau_utm_nord
from . import usa_terrain


class FournisseurUSA(Fournisseur):
    """États-Unis continentaux, terrain 3DEP et surface reconstruite."""

    ID = "usa_3dep"
    NOM = "États-Unis (CONUS) — USGS 3DEP"
    PAYS = "US"
    COUVERTURE = COUVERTURE_CONUS

    def crs_travail(self, lon, lat):
        """UTM WGS84, fuseau déduit de la longitude du centroïde.

        Conforme et métrique, donc distances, azimuts et tampons restent justes
        localement : le rail de code reste « un CRS projeté en mètres », comme
        avec le Lambert 93. Deux écarts assumés et négligeables à la maille de
        travail : une emprise à cheval sur deux fuseaux prend celui du
        centroïde (erreur d'échelle d'environ 1/1000 en bord de fuseau, soit
        ~12 m sur 12 km de rayon), et 3DEP est nativement en NAD83, dont
        l'écart à WGS84 est de 1 à 2 m aux États-Unis.
        """
        return fuseau_utm_nord(lon)

    def resolution_native(self, bbox_wgs84, journal=None):
        """1 m si un projet LiDAR couvre l'emprise, sinon ~10 m.

        Se mesure sur l'index de disponibilité, jamais sur la foi de la
        mosaïque transparente : celle-ci sert du 1 m et du 1/3 arc-seconde sans
        dire lequel, et accepte n'importe quelle résolution en interpolant.
        """
        return usa_terrain.resolution_native(bbox_wgs84, journal=journal)

    def resolution_de_travail(self, demandee, bbox_wgs84, journal=None):
        """Borne la résolution demandée à ce que le service possède vraiment.

        3DEP ne refuse JAMAIS une résolution trop fine : il interpole et rend du
        faux relief plausible — mesuré, 16 Mo pour 0,25 m demandé sur une zone à
        1 m. Le bornage est donc obligatoire, et il se dit au journal : deux ZIV
        d'apparence identique ne doivent pas reposer sur des terrains vingt fois
        différents en finesse sans qu'on le sache.
        """
        native = self.resolution_native(bbox_wgs84, journal=journal)
        return usa_terrain.borner_resolution(demandee, native, journal)

    def fournit_terrain(self):
        return True

    def terrain(self, bbox, resolution, epsg, dossier, journal=None, est_annule=None):
        """Le raster d'élévation de l'emprise, découpé, téléchargé, assemblé.

        `bbox` est dans `epsg`. La résolution doit déjà avoir été bornée au
        natif par l'appelant — c'est lui qui connaît l'emprise en WGS84 et donc
        peut interroger l'index.

        Réemploie le cache de la chaîne française par ses deux points
        d'injection : reprises, fichier `.partiel`, trois tentatives,
        annulation, et surtout le reniflement de signature GeoTIFF, sans lequel
        une erreur de service en HTTP 200 passerait pour une dalle.
        """
        import os

        from ..import_dalles.mosaique import construire_mosaique, statistiques
        from ..import_dalles.telechargement import telecharger_dalles

        journal = journal or usa_terrain.JournalMuet()
        plafond = usa_terrain.plafond_image(journal=journal)
        while True:
            liste = usa_terrain.dalles(bbox, resolution, epsg, plafond=plafond,
                                       journal=journal)
            try:
                chemins = telecharger_dalles(
                    liste, dossier, resolution, journal=journal,
                    est_annule=est_annule,
                    url_a_la_resolution=usa_terrain.url_a_la_resolution,
                    apres_arrivee=usa_terrain.poser_sans_donnee)
                break
            except Exception as erreur:
                # Un refus CÔTÉ SERVEUR (5xx) sur un morceau se règle en
                # découpant plus fin, pas en réessayant à l'identique — les
                # trois tentatives espacées viennent d'échouer pour cela. Un
                # 4xx ou une coupure réseau ne se répare pas en découpant.
                suivant = plafond // 2
                if "HTTP 5" not in str(erreur) or suivant < usa_terrain.PLAFOND_MINIMAL:
                    raise
                journal.warning(
                    "3DEP : le service refuse des morceaux de %d px (%s). "
                    "Redécoupage à %d px." % (plafond, erreur, suivant))
                plafond = suivant
        if not chemins:
            raise usa_terrain.ErreurTerrain(
                "Aucun morceau de terrain téléchargé : rien à assembler.")

        sortie = os.path.join(dossier, "terrain_3dep_%gm.tif" % resolution)
        construire_mosaique([str(c) for c in chemins], sortie,
                            resolution_cible=resolution, emprise=bbox,
                            journal=journal, epsg=epsg)
        infos = statistiques(sortie, epsg=epsg)
        journal.info(
            "Terrain 3DEP : %d x %d cellules de %g m, altitudes de %.1f à "
            "%.1f m, SCR %s."
            % (infos["colonnes"], infos["lignes"], infos["pixel"], infos["min"],
               infos["max"], "conforme" if infos["scr_conforme"] else "NON CONFORME"))
        if not infos["scr_conforme"]:
            journal.warning("Le terrain produit n'est pas exactement en EPSG:%d : "
                            "à vérifier avant la ZIV." % epsg)
        return sortie

    def occulte(self):
        # Vrai : le terrain est nu, la surface occultante se grave.
        return True

    def graver_occultation(self, chemin_terrain, bbox, epsg, dossier, journal=None):
        """Reconstruit la surface occultante sur le terrain nu, EN PLACE.

        **Une seule gravure, sur le MAXIMUM des deux hauteurs.** La hauteur
        occultante d'une cellule est le sommet de ce qui s'y trouve, pas la
        somme : une maison sous les arbres occulte la hauteur des arbres, non
        celle des arbres plus la sienne. Additionner les deux gonflerait
        systématiquement l'occultation en zone boisée habitée — et le cas est
        loin d'être marginal, puisqu'à la maille de 30 m de LANDFIRE beaucoup
        d'empreintes de bâti tombent dans des cellules classées forêt, celles
        précisément que le CH remplit à 19-27 m.

        Le bâti est écrit sous **le nom que la chaîne française emploie déjà**
        (`masquage.NOM_BATIMENTS`). Ce n'est pas de la coquetterie : l'étape des
        points de vue photo relit ce fichier s'il existe, et lui donner le même
        nom lui épargne un branchement et un second téléchargement.

        Rend un dict de statistiques, destiné au journal et au cartouche. Ne
        lève pas sur un échec partiel : une surface incomplète vaut mieux qu'un
        calcul avorté, à condition de le DIRE.
        """
        import os

        import numpy as np

        from .. import masquage
        from . import usa_vecteurs as vecteurs
        from . import usa_vegetation as vegetation

        journal = journal or usa_terrain.JournalMuet()
        stats = {"vegetation_max": 0.0, "bati": None, "provenance": {}}
        hauteurs = []

        # --- vegetation ---
        try:
            chemin_evh, chemin_ch = vegetation.rasters(bbox, epsg, dossier,
                                                       journal=journal)
            chemin_surface, provenance = vegetation.surface_sur_disque(
                chemin_evh, chemin_ch, dossier, journal)
            hauteurs.append(vegetation.hauteur_sur_grille(chemin_terrain,
                                                          chemin_surface))
            stats["provenance"] = provenance
            # Gardes pour le masquage du resultat, plus tard dans la chaine.
            stats["evh"] = chemin_evh
            stats["surface"] = chemin_surface
        except Exception as erreur:
            journal.warning(
                "Occultation végétale impossible (%s) — la végétation n'occulte "
                "pas, donc la visibilité sera SURÉVALUÉE. À dire au cartouche."
                % erreur)

        # --- bati ---
        try:
            chemin_bati = os.path.join(dossier, masquage.NOM_BATIMENTS)
            resume = vecteurs.telecharger_bati(bbox, epsg, chemin_bati,
                                               journal=journal)
            if resume:
                hauteurs.append(vecteurs.hauteur_bati_sur_grille(chemin_terrain,
                                                                 chemin_bati))
                stats["bati"] = resume
        except Exception as erreur:
            journal.warning("Occultation du bâti impossible (%s) — le bâti "
                            "n'occulte pas." % erreur)

        if not hauteurs:
            journal.warning("Aucune occultation gravée : le terrain reste NU. "
                            "La ZIV surestimera la visibilité.")
            return stats

        surface = hauteurs[0]
        for autre in hauteurs[1:]:
            surface = np.maximum(surface, autre)
        _nombre, maximum = vegetation.graver_hauteur(
            chemin_terrain, surface, journal,
            libelle="Gravure de la surface occultante")
        stats["vegetation_max"] = maximum
        stats["occultation_max"] = maximum
        return stats

    def masquer_resultat(self, chemin_ziv, chemin_demande, dossier,
                         journal=None):
        """Force « hors visibilité » là où personne ne peut se tenir.

        Deux masques, de natures différentes et c'est assumé : la forêt par
        **seuil sur le raster** de hauteur déjà rapatrié, le bâti par la
        **gravure vectorielle** existante. Le premier évite de vectoriser un
        raster pour rien ; le second réemploie `graver_dans_raster` tel quel.

        La cohérence forêt est acquise par construction : on occulte et on
        masque avec le même tableau de hauteur, donc la surface et le masque ne
        peuvent pas se contredire en lisière.

        Rend le chemin du raster masqué, ou celui de la ZIV d'origine si rien
        n'a pu être appliqué. Ne lève jamais.
        """
        import os

        from .. import donnees_ign, masquage
        from . import usa_vecteurs as vecteurs
        from . import usa_vegetation as vegetation

        journal = journal or usa_terrain.JournalMuet()
        source = str(chemin_ziv)
        if not os.path.isfile(source):
            return source

        try:
            # Reemploi du garde-fou de `masquage.py` : quand l'utilisateur a
            # demande un chemin de sortie explicite, le modele a DEJA ecrit la
            # ZIV a ce chemin. Copier la source sur elle-meme leve SameFileError,
            # et l'ancienne version l'attrapait en rendant la ZIV NON masquee,
            # silencieusement, sur tous les livrables exportes.
            destination, sur_place = masquage._preparer_destination(
                source, chemin_demande, journal)
            if destination is None:
                return source
            if sur_place:
                journal.info("Masquage : gravure directe dans la ZIV demandée (%s)."
                             % os.path.basename(str(destination)))

            masquees = 0

            # --- foret, par seuil raster ---
            chemin_evh = os.path.join(dossier, "landfire_evh_%s.tif" % vegetation.MILLESIME)
            chemin_surface = os.path.join(dossier, "surface_vegetale_30m.tif")
            if os.path.isfile(chemin_evh) and os.path.isfile(chemin_surface):
                masquees += vegetation.masquer_par_raster(
                    str(destination), chemin_evh, chemin_surface, journal=journal)
            else:
                journal.warning(
                    "Masquage forêt : rasters de végétation absents du dossier de "
                    "travail — l'occultation a-t-elle échoué ? Masquage sauté.")

            # --- bati, par gravure vectorielle ---
            chemin_bati = os.path.join(dossier, masquage.NOM_BATIMENTS)
            if os.path.isfile(chemin_bati):
                if not donnees_ign.graver_dans_raster(
                        str(destination), chemin_bati,
                        valeur=masquage.HORS_VISIBILITE, ajouter=False,
                        journal=journal):
                    journal.warning("Masquage : gravure du bâti échouée.")
            else:
                journal.warning("Masquage bâti : %s absent du dossier de travail."
                                % masquage.NOM_BATIMENTS)

            journal.info("Masquage forêt/bâti appliqué (gravure %s) -> %s"
                         % ("sur place" if sur_place else "sur copie", destination))
            return str(destination)
        except Exception as erreur:
            journal.warning("Masquage forêt/bâti échoué (%s) — ZIV inchangée." % erreur)
            return source

    def routes(self, bbox, epsg, chemin, journal=None):
        """Les routes de l'emprise, pour placer les points de vue photo.

        TIGER/Line du Census, trois couches réunies : axes primaires, routes
        secondaires et voirie locale. C'est la voirie locale qui compte — un
        photographe accède par une petite route bien plus souvent que par une
        autoroute.
        """
        from . import usa_vecteurs as vecteurs

        try:
            return vecteurs.telecharger_routes(bbox, epsg, str(chemin),
                                               journal=journal)
        except Exception as erreur:
            if journal is not None:
                journal.warning("Routes TIGER indisponibles (%s) — les points de "
                                "vue seront placés sans elles." % erreur)
            return False
