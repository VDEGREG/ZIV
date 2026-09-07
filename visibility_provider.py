# -*- coding: utf-8 -*-
"""
Embedded visibility provider for ZIV_prime.

This file is part of the GNU GPL v3 licensed ZIV_prime plugin.
Exposes the two visibility algorithms (createviewpoints + viewshed) used by
the "Création ZIV" processing model so the plugin remains self-contained
and never collides with a separately-installed visibility provider.

Les deux algorithmes sont propres à ce greffon (``algorithmes_ziv/``, sur le
moteur ``moteur_visibilite/``). Leur interface, leurs noms de champs et leurs
conventions de sortie sont ceux de la version d'origine du greffon, pour que
``creation_ziv.model3`` fonctionne sans retouche. L'identifiant ``pcfr_vis``
est conservé pour la même raison : c'est une identité technique sur laquelle
des modèles Processing enregistrés peuvent compter.
"""

from pathlib import Path

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithmes_ziv.points_de_vue import ViewshedPoints
from .algorithmes_ziv.visibilite_raster import ViewshedRaster


class ZivVisibilityProvider(QgsProcessingProvider):
    """Internal provider for the visibility algorithms used by the
    ZIV_prime plugin.
    """

    def id(self):
        return "pcfr_vis"

    def name(self):
        return "Zones d'influence visuelle (ZIV) — Visibilité (interne)"

    def longName(self):
        return self.name()

    def icon(self):
        return QIcon(str(Path(__file__).parent / "icon.png"))

    def loadAlgorithms(self):
        for alg in (ViewshedPoints(), ViewshedRaster()):
            self.addAlgorithm(alg)
