# lidar_provider.py
from pathlib import Path

from qgis.core import Qgis, QgsMessageLog, QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .lidar_algorithm import LidarDownloaderAlgorithm as ZivAlgorithm

# Nom affiche a l'utilisateur. L'identifiant du provider (id()) reste "genziv".
PLUGIN_DISPLAY_NAME = "Zones d'influence visuelle (ZIV)"


class LidarProcessingProvider(QgsProcessingProvider):
    def __init__(self):
        QgsProcessingProvider.__init__(self)
        QgsMessageLog.logMessage("Provider initialized", PLUGIN_DISPLAY_NAME, Qgis.MessageLevel.Info)
        self.refreshAlgorithms()

    def load(self):
        QgsMessageLog.logMessage("Provider load called", PLUGIN_DISPLAY_NAME, Qgis.MessageLevel.Info)
        self.refreshAlgorithms()
        return True

    def icon(self):
        """Returns the provider icon."""
        return QIcon(str(Path(__file__).parent / "icon.png"))

    def loadAlgorithms(self):
        QgsMessageLog.logMessage("Loading algorithms", PLUGIN_DISPLAY_NAME, Qgis.MessageLevel.Info)
        try:
            # Only the ZIV algorithm is exposed via this provider.
            self.addAlgorithm(ZivAlgorithm())
            QgsMessageLog.logMessage(
                "Algorithm added successfully (ZIV)",
                PLUGIN_DISPLAY_NAME,
                Qgis.MessageLevel.Info,
            )
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error adding algorithm: {str(e)}", PLUGIN_DISPLAY_NAME, Qgis.MessageLevel.Critical
            )

    def id(self):
        """The unique provider id"""
        return "genziv"

    def name(self):
        """The provider name"""
        return self.tr("Zones d'influence visuelle (ZIV)")

    def longName(self):
        """The provider full name"""
        return self.name()
