# -*- coding: utf-8 -*-
"""« Create viewpoints » — réécrit pour ce greffon, sur le moteur `moteur_visibilite`.

Cet algorithme remplace celui du greffon *Visibility Analysis* de Zoran
Čučković, qui a été retiré du paquet. **L'interface est conservée à
l'identique** — identifiant `createviewpoints`, mêmes noms de paramètres,
mêmes noms de champs produits — pour que `creation_ziv.model3` et les couches
déjà produites continuent de fonctionner sans retouche.

Champs écrits, dans l'ordre attendu par l'algorithme de visibilité :

  * `ID`         — identifiant du point (texte, comme la version d'origine) ;
  * `observ_hgt` — hauteur de l'observateur au-dessus du sol ;
  * `radius`     — rayon d'analyse ;
  * puis, seulement si les champs correspondants ont été désignés :
    `targ_hgt`, `radius_in`, `azim_1`, `azim_2`, `angle_up`, `angle_down`.

Les points sont calés au centre de la cellule qui les contient, et ceux qui
tombent hors du modèle numérique de terrain sont écartés et signalés.
"""

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QMetaType

from . import commun


class ViewshedPoints(QgsProcessingAlgorithm):
    """Prépare des points d'observation pour l'analyse de visibilité."""

    OUTPUT = "OUTPUT"
    OBSERVER_POINTS = "OBSERVER_POINTS"
    DEM = "DEM"
    OBSERVER_ID = "OBSERVER_ID"
    RADIUS = "RADIUS"
    RADIUS_FIELD = "RADIUS_FIELD"
    RADIUS_IN_FIELD = "RADIUS_IN_FIELD"
    OBS_HEIGHT = "OBS_HEIGHT"
    OBS_HEIGHT_FIELD = "OBS_HEIGHT_FIELD"
    TARGET_HEIGHT = "TARGET_HEIGHT"
    TARGET_HEIGHT_FIELD = "TARGET_HEIGHT_FIELD"
    AZIM_1_FIELD = "AZIM_1_FIELD"
    AZIM_2_FIELD = "AZIM_2_FIELD"
    ANGLE_UP_FIELD = "ANGLE_UP_FIELD"
    ANGLE_DOWN_FIELD = "ANGLE_DOWN_FIELD"

    def tr(self, texte):
        return QCoreApplication.translate("Processing", texte)

    def createInstance(self):
        return ViewshedPoints()

    def name(self):
        return "createviewpoints"

    def displayName(self):
        return self.tr("Create viewpoints")

    def group(self):
        return self.tr("Visibilité")

    def groupId(self):
        return "visibilite"

    def shortHelpString(self):
        return self.tr(
            "<p>Prépare une couche de points d'observation pour l'analyse de "
            "visibilité : chaque point est reprojeté dans le système du modèle "
            "numérique de terrain, <b>calé au centre de la cellule</b> qui le "
            "contient, et reçoit ses paramètres d'analyse.</p>"
            "<p>Pour chaque paramètre, un champ de la couche l'emporte sur la "
            "valeur saisie : c'est ainsi qu'on traite d'un coup des mâts de "
            "hauteurs différentes.</p>"
            "<p>Les points hors emprise du terrain sont écartés et signalés.</p>"
        )

    def initAlgorithm(self, config=None):
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
            QgsProcessingParameterField(
                self.OBSERVER_ID, self.tr("Observer ids (leave unchanged to use feature ids)"),
                None, self.OBSERVER_POINTS,
                QgsProcessingParameterField.DataType.Numeric, False, True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.RADIUS, self.tr("Radius of analysis, meters"),
                QgsProcessingParameterNumber.Type.Double, 5000, False, 0,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.RADIUS_FIELD, self.tr("Radius, data field"), None,
                self.OBSERVER_POINTS, QgsProcessingParameterField.DataType.Numeric, False, True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.OBS_HEIGHT, self.tr("Observer height, meters"),
                QgsProcessingParameterNumber.Type.Double, 1.6, False, 0,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.OBS_HEIGHT_FIELD, self.tr("Observer height, data field"), None,
                self.OBSERVER_POINTS, QgsProcessingParameterField.DataType.Numeric, False, True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TARGET_HEIGHT, self.tr("Target height, meters"),
                QgsProcessingParameterNumber.Type.Double, 0.0, False, 0,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.TARGET_HEIGHT_FIELD, self.tr("Target height, data field"), None,
                self.OBSERVER_POINTS, QgsProcessingParameterField.DataType.Numeric, False, True,
            )
        )
        for nom, libelle in (
            (self.RADIUS_IN_FIELD, self.tr("Inner radius, data field")),
            (self.AZIM_1_FIELD, self.tr("Azimuth: minimum, data field")),
            (self.AZIM_2_FIELD, self.tr("Azimuth: maximum, data field")),
            (self.ANGLE_UP_FIELD, self.tr("Vertical angle: maximum, data field")),
            (self.ANGLE_DOWN_FIELD, self.tr("Vertical angle: minimum, data field")),
        ):
            self.addParameter(
                QgsProcessingParameterField(
                    nom, libelle, None, self.OBSERVER_POINTS,
                    QgsProcessingParameterField.DataType.Numeric, False, True,
                )
            )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT, self.tr("Viewpoints"), QgsProcessing.SourceType.TypeVectorPoint
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.OBSERVER_POINTS, context)
        couche_mnt = self.parameterAsRasterLayer(parameters, self.DEM, context)
        if source is None or couche_mnt is None:
            raise QgsProcessingException("Points d'observation ou terrain manquant.")
        mnt = commun.charger_mnt(couche_mnt)

        champ_id = self.parameterAsString(parameters, self.OBSERVER_ID, context)
        champs_optionnels = [
            (commun.CHAMP_TARGET, self.parameterAsString(parameters, self.TARGET_HEIGHT_FIELD, context)),
            (commun.CHAMP_RAYON_IN, self.parameterAsString(parameters, self.RADIUS_IN_FIELD, context)),
            (commun.CHAMP_AZIM_1, self.parameterAsString(parameters, self.AZIM_1_FIELD, context)),
            (commun.CHAMP_AZIM_2, self.parameterAsString(parameters, self.AZIM_2_FIELD, context)),
            (commun.CHAMP_ANGLE_UP, self.parameterAsString(parameters, self.ANGLE_UP_FIELD, context)),
            (commun.CHAMP_ANGLE_DOWN, self.parameterAsString(parameters, self.ANGLE_DOWN_FIELD, context)),
        ]
        hauteur_cible = self.parameterAsDouble(parameters, self.TARGET_HEIGHT, context)
        champ_hauteur = self.parameterAsString(parameters, self.OBS_HEIGHT_FIELD, context)
        champ_rayon = self.parameterAsString(parameters, self.RADIUS_FIELD, context)
        hauteur = self.parameterAsDouble(parameters, self.OBS_HEIGHT, context)
        rayon = self.parameterAsDouble(parameters, self.RADIUS, context)

        champs = QgsFields()
        champs.append(QgsField(commun.CHAMP_ID, QMetaType.Type.QString))
        champs.append(QgsField(commun.CHAMP_HAUTEUR, QMetaType.Type.Double))
        champs.append(QgsField(commun.CHAMP_RAYON, QMetaType.Type.Double))
        retenus = [(nom, champ) for nom, champ in champs_optionnels if champ]
        # La hauteur de cible n'est stockée que si elle sert : la version
        # d'origine ne créait pas ce champ pour une cible au sol.
        if hauteur_cible and not any(nom == commun.CHAMP_TARGET for nom, _ in retenus):
            retenus.insert(0, (commun.CHAMP_TARGET, None))
        for nom, _champ in retenus:
            champs.append(QgsField(nom, QMetaType.Type.Double))

        sink, identifiant = self.parameterAsSink(
            parameters, self.OUTPUT, context, champs,
            QgsWkbTypes.Type.Point, couche_mnt.crs(),
        )

        transformation = QgsCoordinateTransform(
            source.sourceCrs(), couche_mnt.crs(), context.transformContext()
        )
        index = {nom.lower(): i for i, nom in enumerate(source.fields().names())}
        hors_emprise, conserves = [], 0

        for numero, entite in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            geometrie = entite.geometry()
            if geometrie.isEmpty():
                continue
            geometrie.transform(transformation)
            point = (geometrie.asMultiPoint()[0] if geometrie.isMultipart()
                     else geometrie.asPoint())
            cellule = mnt.cellule(point.x(), point.y())
            identite = commun.valeur_champ(entite, index, champ_id, numero + 1)
            if cellule is None:
                hors_emprise.append(str(identite))
                continue
            x, y = mnt.pixel_vers_monde(cellule[1], cellule[0])

            sortie = QgsFeature(champs)
            sortie.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))
            valeurs = [
                str(identite),
                float(commun.valeur_champ(entite, index, champ_hauteur, hauteur)),
                float(commun.valeur_champ(entite, index, champ_rayon, rayon)),
            ]
            for nom, champ in retenus:
                defaut = hauteur_cible if nom == commun.CHAMP_TARGET else 0.0
                valeurs.append(float(commun.valeur_champ(entite, index, champ, defaut)))
            sortie.setAttributes(valeurs)
            sink.addFeature(sortie)
            conserves += 1

        if hors_emprise:
            feedback.pushWarning(
                "%d point(s) hors de l'emprise du terrain, ecarte(s) : %s"
                % (len(hors_emprise), ", ".join(hors_emprise[:8])
                   + (", ..." if len(hors_emprise) > 8 else ""))
            )
        if conserves == 0:
            raise QgsProcessingException(
                "Aucun point d'observation ne tombe sur le terrain fourni."
            )
        feedback.pushInfo(
            "%d point(s) prepare(s), cales sur la grille du terrain (%.2f m au plus)."
            % (conserves, 0.5 * float((mnt.dx ** 2 + mnt.dy ** 2) ** 0.5))
        )
        return {self.OUTPUT: identifiant}
