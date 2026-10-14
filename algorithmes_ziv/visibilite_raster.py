# -*- coding: utf-8 -*-
"""« Viewshed » — réécrit pour ce greffon, sur le moteur `moteur_visibilite`.

Remplace l'algorithme du greffon *Visibility Analysis* de Zoran Čučković, retiré
du paquet. **L'interface et les conventions de sortie sont conservées à
l'identique**, pour que `creation_ziv.model3` et la symbologie « effet nuit »
fonctionnent sans retouche :

  * identifiant `viewshed`, mêmes paramètres, mêmes intitulés ;
  * raster de sortie sur **toute l'emprise du terrain**, `Float32`,
    valeur sans-donnée `nan` ;
  * en mode *Addition*, le fond vaut **0** (non vu) et non sans-donnée : c'est
    ce que la chaîne ZIV additionne puis stylise ;
  * en *Minimum* / *Maximum*, le fond vaut `nan`, faute de quoi le zéro du fond
    l'emporterait sur toute valeur réelle ;
  * « Depth below horizon » est exprimé en **mètres**, valant 0 là où la cible
    est vue — la convention d'origine.
"""

import numpy as np
from osgeo import gdal, osr
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from ..moteur_visibilite.moteur import Observateur, ObservateurHorsMnt, calculer, horizon_visible
from . import commun

BINAIRE, PROFONDEUR, HORIZON = 0, 1, 2
ADDITION, MINIMUM, MAXIMUM = 0, 1, 2

# Colonnes recalculées exactement par le moteur avant de reprendre sa mémoire
# longue. 8 : réglage par défaut, mesuré à 99,63 % d'accord avec un oracle.
RECUL = 8


class ViewshedRaster(QgsProcessingAlgorithm):
    """Carte de visibilité depuis un ou plusieurs points d'observation."""

    DEM = "DEM"
    OBSERVER_POINTS = "OBSERVER_POINTS"
    USE_CURVATURE = "USE_CURVATURE"
    REFRACTION = "REFRACTION"
    ANALYSIS_TYPE = "ANALYSIS_TYPE"
    OPERATOR = "OPERATOR"
    OUTPUT = "OUTPUT"

    TYPES = ["Binary viewshed", "Depth below horizon", "Horizon"]
    OPERATORS = ["Addition", "Minimum", "Maximum"]

    def tr(self, texte):
        return QCoreApplication.translate("Processing", texte)

    def createInstance(self):
        return ViewshedRaster()

    def name(self):
        return "viewshed"

    def displayName(self):
        return self.tr("Viewshed")

    def group(self):
        return self.tr('Visibility')

    def groupId(self):
        return "visibilite"

    def shortHelpString(self):
        return self.tr(
            '<p>Produces a visibility map from the observer points, over a digital terrain model.</p><ul><li><b>Binary viewshed</b> — 1 where the cell is seen, 0 otherwise.</li><li><b>Depth below horizon</b> — 0 where the target is seen, otherwise how many <b>metres</b> would have to be added for it to appear.</li><li><b>Horizon</b> — the ridge cells that close the landscape.</li></ul><p>With several points, <i>Addition</i> counts how many of them see each cell: that is the visual exposure map the viewshed chain styles.</p><p>The point layer must carry the fields <code>observ_hgt</code> and <code>radius</code>, which Create viewpoints produces. The optional fields <code>targ_hgt</code>, <code>radius_in</code>, <code>azim_1</code>, <code>azim_2</code>, <code>angle_up</code> and <code>angle_down</code> are honoured when present.</p>'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterEnum(
                self.ANALYSIS_TYPE, self.tr("Analysis type"), self.TYPES, False, 0
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.OBSERVER_POINTS, self.tr("Observer location(s)"),
                [QgsProcessing.SourceType.TypeVectorPoint],
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterLayer(self.DEM, self.tr("Digital elevation model"))
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.USE_CURVATURE, self.tr("Take in account Earth curvature"), False
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.REFRACTION, self.tr("Atmospheric refraction"),
                QgsProcessingParameterNumber.Type.Double, 0.13, False, 0, 1,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.OPERATOR, self.tr("Combining multiple outputs"), self.OPERATORS, False, 0
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterDestination(self.OUTPUT, self.tr("Output file"))
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.OBSERVER_POINTS, context)
        couche_mnt = self.parameterAsRasterLayer(parameters, self.DEM, context)
        if source is None or couche_mnt is None:
            raise QgsProcessingException("Points d'observation ou terrain manquant.")

        manquants = commun.champs_manquants(source.fields())
        if manquants:
            raise QgsProcessingException(
                "Champs absents de la couche de points : %s. Passer d'abord par "
                "« Create viewpoints »." % ", ".join(manquants)
            )

        mnt = commun.charger_mnt(couche_mnt)
        type_analyse = self.parameterAsEnum(parameters, self.ANALYSIS_TYPE, context)
        operateur = self.parameterAsEnum(parameters, self.OPERATOR, context)
        courbure = self.parameterAsBool(parameters, self.USE_CURVATURE, context)
        refraction = self.parameterAsDouble(parameters, self.REFRACTION, context)
        chemin = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)

        fond = 0.0 if operateur == ADDITION else np.nan
        cumul = np.full((mnt.ny, mnt.nx), fond, dtype=np.float32)

        index = {nom.lower(): i for i, nom in enumerate(source.fields().names())}
        entites = list(source.getFeatures())
        if not entites:
            raise QgsProcessingException("La couche de points d'observation est vide.")

        traites, ignores = 0, []
        for numero, entite in enumerate(entites):
            if feedback.isCanceled():
                return {}
            geometrie = entite.geometry()
            if geometrie.isEmpty():
                continue
            point = (geometrie.asMultiPoint()[0] if geometrie.isMultipart()
                     else geometrie.asPoint())
            observateur = Observateur(
                x=point.x(), y=point.y(),
                h_obs=float(commun.valeur_champ(entite, index, commun.CHAMP_HAUTEUR, 1.6)),
                h_cible=float(commun.valeur_champ(entite, index, commun.CHAMP_TARGET, 0.0)),
                rayon=float(commun.valeur_champ(entite, index, commun.CHAMP_RAYON, 5000.0)),
                rayon_min=float(commun.valeur_champ(entite, index, commun.CHAMP_RAYON_IN, 0.0)),
                azimut_min=float(commun.valeur_champ(entite, index, commun.CHAMP_AZIM_1, 0.0)),
                azimut_max=float(commun.valeur_champ(entite, index, commun.CHAMP_AZIM_2, 360.0)),
                identifiant=commun.valeur_champ(entite, index, commun.CHAMP_ID, numero + 1),
            )
            try:
                resultat = calculer(
                    mnt, observateur, courbure=courbure, refraction=refraction,
                    recul=RECUL,
                    unite_marge="profondeur_m" if type_analyse == PROFONDEUR else "degres",
                    # Seule la profondeur sous l'horizon lit la marge : ailleurs
                    # on s'epargne un tableau de la fenetre et deux arc-tangentes
                    # par cellule.
                    calcul_marge=type_analyse == PROFONDEUR,
                )
            except ObservateurHorsMnt as erreur:
                ignores.append(str(erreur))
                continue

            contribution = self._contribution(type_analyse, resultat)
            self._masquer_angles(contribution, resultat, entite, index, mnt, observateur)

            lignes = slice(resultat.lig0, resultat.lig0 + contribution.shape[0])
            colonnes = slice(resultat.col0, resultat.col0 + contribution.shape[1])
            if operateur == ADDITION:
                cumul[lignes, colonnes] += np.nan_to_num(contribution, nan=0.0)
            elif operateur == MINIMUM:
                cumul[lignes, colonnes] = np.fmin(cumul[lignes, colonnes], contribution)
            else:
                cumul[lignes, colonnes] = np.fmax(cumul[lignes, colonnes], contribution)

            traites += 1
            feedback.setProgress(100.0 * (numero + 1) / len(entites))

        for message in ignores:
            feedback.pushWarning(message)
        if traites == 0:
            raise QgsProcessingException(
                "Aucun point d'observation exploitable : tous sont hors du terrain."
            )

        self._ecrire(chemin, cumul, mnt, couche_mnt, feedback)
        vues = int(np.count_nonzero(np.nan_to_num(cumul, nan=0.0) > 0))
        feedback.pushInfo(
            "%d point(s) traite(s). %d cellule(s) a valeur non nulle sur %d "
            "(%.1f %%), valeurs de %.3f a %.3f."
            % (traites, vues, cumul.size, 100.0 * vues / cumul.size,
               float(np.nanmin(cumul)), float(np.nanmax(cumul)))
        )
        return {self.OUTPUT: chemin}

    @staticmethod
    def _contribution(type_analyse, resultat):
        """Valeur apportée par un observateur, sur sa fenêtre."""
        if type_analyse == PROFONDEUR:
            # Déjà en mètres, 0 là où c'est vu : convention d'origine.
            valeurs = np.where(resultat.valide, resultat.marge, 0.0)
            return np.nan_to_num(valeurs, nan=0.0).astype(np.float32)
        if type_analyse == HORIZON:
            return horizon_visible(resultat).astype(np.float32)
        return resultat.visible.astype(np.float32)

    @staticmethod
    def _masquer_angles(contribution, resultat, entite, index, mnt, observateur):
        """Retire ce qui sort des angles verticaux, si les champs existent.

        Sans les champs `angle_up` / `angle_down`, il n'y a rien à masquer : on
        ne fabrique pas de limite que l'utilisateur n'a pas demandée.
        """
        haut = commun.valeur_champ(entite, index, commun.CHAMP_ANGLE_UP, None)
        bas = commun.valeur_champ(entite, index, commun.CHAMP_ANGLE_DOWN, None)
        if haut is None and bas is None:
            return
        n_lig, n_col = contribution.shape
        ecart_x = (np.arange(n_col, dtype=np.float32)
                   + (resultat.col0 - resultat.col_obs)) * np.float32(mnt.dx)
        ecart_y = (np.float32(resultat.lig_obs - resultat.lig0)
                   - np.arange(n_lig, dtype=np.float32)) * np.float32(mnt.dy)
        distance = np.hypot(ecart_y[:, None], ecart_x[None, :])
        distance[distance == 0] = np.nan
        fenetre = mnt.z[resultat.lig0:resultat.lig0 + n_lig,
                        resultat.col0:resultat.col0 + n_col]
        with np.errstate(invalid="ignore"):
            tangente = (fenetre - np.float32(resultat.z_obs)) / distance
            if haut is not None:
                contribution[tangente > np.tan(np.radians(float(haut)))] = 0
            if bas is not None:
                contribution[tangente < np.tan(np.radians(float(bas)))] = 0

    @staticmethod
    def _ecrire(chemin, tableau, mnt, couche_mnt, feedback=None):
        gdal.UseExceptions()
        pilote = gdal.GetDriverByName("GTiff")
        jeu = pilote.Create(str(chemin), mnt.nx, mnt.ny, 1, gdal.GDT_Float32,
                            ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES",
                             "BIGTIFF=IF_SAFER"])
        jeu.SetGeoTransform(mnt.gt)
        wkt = couche_mnt.crs().toWkt() if couche_mnt.crs().isValid() else mnt.crs_wkt
        if wkt:
            reference = osr.SpatialReference()
            reference.ImportFromWkt(wkt)
            jeu.SetProjection(reference.ExportToWkt())
        elif feedback is not None:
            # Un raster sans projection ne se voit pas : il s'affiche, il se
            # mesure, et c'est bien plus loin que la chaine casse. Mesure du
            # 2026-09-14 : `gdal:merge` prend le systeme de sa PREMIERE entree
            # et rend, sans un mot, une ZIV sans projection ; le masquage
            # americain recoit alors un `-t_srs` vide et echoue sur
            # « Invalid SRS for -t_srs », a trois etapes de la vraie cause.
            feedback.pushWarning(
                "Le terrain ne declare AUCUN systeme de coordonnees : la "
                "visibilite sort donc sans projection, et tout ce qui la suit "
                "— fusion, masquage, chargement — travaillera a l'aveugle. "
                "Verifier le raster de terrain fourni.")
        bande = jeu.GetRasterBand(1)
        bande.SetNoDataValue(float("nan"))
        bande.WriteArray(tableau)
        bande.FlushCache()
        bande.ComputeStatistics(False)
        jeu = None
