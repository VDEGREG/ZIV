# lidar_provider.py
from pathlib import Path

from qgis.core import Qgis, QgsMessageLog, QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .lidar_algorithm import LidarDownloaderAlgorithm as ZivAlgorithm


class LidarProcessingProvider(QgsProcessingProvider):
    def __init__(self):
        QgsProcessingProvider.__init__(self)
        QgsMessageLog.logMessage("Provider initialized", "ZIV_prime", Qgis.Info)
        self.refreshAlgorithms()

    def load(self):
        QgsMessageLog.logMessage("Provider load called", "ZIV_prime", Qgis.Info)
        self.refreshAlgorithms()
        return True

    def icon(self):
        """Returns the provider icon."""
        return QIcon(str(Path(__file__).parent / "icon.png"))

    def loadAlgorithms(self):
        QgsMessageLog.logMessage("Loading algorithms", "ZIV_prime", Qgis.Info)
        try:
            # Only the ZIV algorithm is exposed via this provider.
            self.addAlgorithm(ZivAlgorithm())
            QgsMessageLog.logMessage(
                "Algorithm added successfully (ZIV)",
                "ZIV_prime",
                Qgis.Info,
            )
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error adding algorithm: {str(e)}", "ZIV_prime", Qgis.Critical
            )

    def id(self):
        """The unique provider id"""
        return "genziv"

    def name(self):
        """The provider name"""
        return self.tr("ZIV_prime")

    def longName(self):
        """The provider full name"""
        return self.name()
