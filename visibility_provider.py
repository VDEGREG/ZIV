# -*- coding: utf-8 -*-
"""
Embedded visibility provider for ZIV_prime.

This file is part of the GNU GPL v3 licensed ZIV_prime plugin.
Exposes the two visibility algorithms (createviewpoints + viewshed) used by
the "Création ZIV" processing model so the plugin remains self-contained
and never collides with a separately-installed visibility provider.

See ``algorithms/LICENSE`` and ``algorithms/README.txt`` for the original
third-party copyright notice and the full GPL v3 license terms.
"""

from pathlib import Path

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms.viewshed_points import ViewshedPoints
from .algorithms.viewshed_raster import ViewshedRaster


class PcfrVisibilityProvider(QgsProcessingProvider):
    """Internal provider for the visibility algorithms used by the
    ZIV_prime plugin.
    """

    def id(self):
        return "pcfr_vis"

    def name(self):
        return "ZIV_prime — Visibilité (interne)"

    def longName(self):
        return self.name()

    def icon(self):
        return QIcon(str(Path(__file__).parent / "icon.png"))

    def loadAlgorithms(self):
        for alg in (ViewshedPoints(), ViewshedRaster()):
            self.addAlgorithm(alg)
