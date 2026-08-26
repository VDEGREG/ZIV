"""
ZIV_prime - IGN LiDAR HD download and ZIV Processing algorithm.

Part of ZIV_prime, a GNU GPL v3 (or later) licensed QGIS plugin by G. DELSIGNE.

Third-party origin
------------------
The IGN LiDAR HD tile discovery and download logic in this module is derived
from the open-source QGIS plugin "PointCloudFR" by Samy KHELIL
(https://github.com/sameeeyy/PointCloudFR), released under the GNU General
Public License v2 or later. This notice is preserved as required by the GPL.

The visibility engine used downstream is derived from "Visibility Analysis" by
Zoran Cuckovic (GNU GPL v3) - see ``algorithms/README.txt``.

See ``LICENSE`` and ``README.md`` at the root of the plugin for the full terms
and the complete credits.
"""
import concurrent.futures
import contextlib
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import processing
import requests

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsFillSymbol,
    QgsGeometry,
    QgsMessageLog,
    QgsPointCloudClassifiedRenderer,
    QgsPointCloudLayer,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingModelAlgorithm,
    QgsProcessingOutputFile,
    QgsProcessingOutputFolder,
    QgsProcessingOutputString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterDestination,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsRectangle,
    QgsSingleBandPseudoColorRenderer,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.PyQt.QtGui import QColor, QFont
from qgis.PyQt.QtWidgets import QMessageBox
from requests.adapters import HTTPAdapter, Retry


# Nom affiche a l'utilisateur (journal, fenetres, titre de carte).
PLUGIN_DISPLAY_NAME = "Zones d'influence visuelle (ZIV)"


class DownloadProgressTracker:
    """Simplified thread-safe progress tracker counting completed files."""

    def __init__(self, feedback):
        self.feedback = feedback
        self.total_files = 0
        self.completed_files = 0
        self._lock = threading.RLock()

    def set_total_files(self, total: int):
        """Set the total number of files to download."""
        with self._lock:
            self.total_files = total
            self.completed_files = 0
            self._update_progress()

    def mark_file_completed(self):
        """Mark one file as completed and update progress."""
        with self._lock:
            self.completed_files += 1
            self._update_progress()

    def _update_progress(self):
        """Update the progress bar based on completed files."""
        if self.total_files > 0:
            progress = min((self.completed_files / self.total_files) * 100, 100)
            self.feedback.setProgress(int(progress))

    def get_progress_info(self) -> str:
        """Get current progress information as a string."""
        with self._lock:
            return f"{self.completed_files}/{self.total_files} files completed"


class LidarLogger:
    """Custom logger for LiDAR operations"""

    def __init__(self, feedback, log_to_file: bool = True):
        self.feedback = feedback
        self.log_to_file = log_to_file
        self.log_file = None
        if log_to_file:
            # Use the proper QGIS3 settings dir (audit fix M-2). Fall back to
            # the OS tempdir if QgsApplication is not initialised yet.
            try:
                from qgis.core import QgsApplication

                base_dir = Path(QgsApplication.qgisSettingsDirPath() or "")
            except Exception:
                base_dir = Path()
            if not base_dir or not str(base_dir):
                import tempfile as _tmp

                base_dir = Path(_tmp.gettempdir())
            log_dir = base_dir / "generation_ziv_logs"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                log_dir = Path()
            if log_dir and log_dir.exists():
                self.log_file = (
                    log_dir
                    / f'genziv_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
                )
                # Rotate: keep only the 30 most recent .log files (audit fix M-1)
                try:
                    olds = sorted(
                        log_dir.glob("genziv_*.log"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    for old in olds[30:]:
                        try:
                            old.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass

    def info(self, message: str):
        """Log info message"""
        self.feedback.pushInfo(message)
        QgsMessageLog.logMessage(message, PLUGIN_DISPLAY_NAME, Qgis.Info)
        self._write_to_file("INFO", message)

    def error(self, message: str):
        """Log error message"""
        self.feedback.reportError(message)
        QgsMessageLog.logMessage(message, PLUGIN_DISPLAY_NAME, Qgis.Critical)
        self._write_to_file("ERROR", message)

    def warning(self, message: str):
        """Log warning message"""
        self.feedback.pushWarning(message)
        QgsMessageLog.logMessage(message, PLUGIN_DISPLAY_NAME, Qgis.Warning)
        self._write_to_file("WARNING", message)

    def _write_to_file(self, level: str, message: str):
        """Write log message to file if enabled"""
        if self.log_to_file and self.log_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(f"{timestamp} [{level}] {message}\n")
            except Exception as e:
                self.feedback.reportError(f"Failed to write to log file: {str(e)}")



class LidarDownloaderAlgorithm(QgsProcessingAlgorithm):
    """QGIS Processing algorithm for downloading LiDAR tiles using WFS service."""

    # Parameter keys (must match what the user-facing form exposes)
    INPUT = "INPUT"
    DATA_TYPE = "DATA_TYPE"
    FORCE_DOWNLOAD = "FORCE_DOWNLOAD"
    LOAD_LAYER = "LOAD_LAYER"
    MERGE_TILES = "MERGE_TILES"
    CREATE_ZIV = "CREATE_ZIV"
    ZIV_RADIUS = "ZIV_RADIUS"
    ZIV_PANEL_HEIGHT = "ZIV_PANEL_HEIGHT"
    ZIV_OUTPUT = "ZIV_OUTPUT"
    ZIV_SYMBOLOGY = "ZIV_SYMBOLOGY"
    # v3.11.0/v3.13.0: photo viewpoints - opt-in checkbox + number of
    # camera locations generated on the most visible sectors of the ZIV.
    SUGGEST_VIEWPOINTS = "SUGGEST_VIEWPOINTS"
    NB_VIEWPOINTS = "NB_VIEWPOINTS"

    # ZIV symbology options (cf. audit V7)
    ZIV_SYMBOLOGY_OPTIONS = [
        "Spotlight nuit (défaut) — non visible en noir 80 %, visible transparent",
        "Dégradé chaud — blanc → jaune → orange → rouge (style v1.3.x)",
        "Rouge translucide — visible en rouge 30 %, non visible transparent",
    ]
    # Symbology codes used internally by _apply_ziv_color_ramp
    SYMB_SPOTLIGHT_NIGHT = 0
    SYMB_WARM_GRADIENT = 1
    SYMB_TRANSLUCENT_RED = 2

    # Memory and file size limits (in bytes)
    MIN_DISK_SPACE_MB = 1024  # 1GB minimum free space
    MAX_TILES_RECOMMENDED = 50  # Recommended maximum tiles per download

    # Target resolution (m) the merged DEM is resampled to before the
    # viewshed runs. Lower = finer ZIV but quadratically slower (cells
    # scale with 1/res²). 5 m → 2 m multiplies the cell count by ~6.25×.
    ZIV_TARGET_RES_M = 2.0

    # Options for data types
    DATA_TYPE_OPTIONS = [
        "MNT (Digital Terrain Model)",
        "MNS (Digital Surface Model)",
        "MNH (Digital Height Model)",
        "LIDAR (Point Cloud)",
    ]

    # Mapping to WFS codes (Updated based on user input and IGN Géoplateforme)
    DATA_TYPE_CODES = {
        0: "IGNF_MNT-LIDAR-HD:dalle",  # MNT
        1: "IGNF_MNS-LIDAR-HD:dalle",  # MNS
        2: "IGNF_MNH-LIDAR-HD:dalle",  # MNH
        3: "IGNF_NUAGES-DE-POINTS-LIDAR-HD:dalle",  # LIDAR (Confirmed correct name)
    }

    def __init__(self):
        super().__init__()
        self.logger = None
        self._temp_files = set()  # Track temporary files for cleanup

    def tr(self, string):
        """Returns a translatable string with the self.tr() function."""
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return LidarDownloaderAlgorithm()

    def name(self):
        return "generate"

    def displayName(self):
        return self.tr("ZIV")

    def shortHelpString(self):
        return self.tr(
            """
ZIV_prime

Télécharge les dalles LiDAR HD de l'IGN (MNT/MNS/MNH/LIDAR) qui intersectent
votre emprise (AOI) puis génère automatiquement la Zone d'Influence Visuelle
(ZIV).

Types de données :
- MNT : Modèle Numérique de Terrain (sol nu)
- MNS : Modèle Numérique de Surface (végétation et bâti compris)
- MNH : Modèle Numérique de Hauteur (objets au-dessus du sol)
- LIDAR : Nuage de points classifiés brut

Rayon d'analyse (m) :
- Valeur unique servant à la fois de buffer autour de l'AOI pour le
  téléchargement ET de rayon d'analyse pour le calcul de la ZIV.
- Un shapefile du buffer est généré et affiché avec un contour
  rouge en pointillés (remplissage transparent).

Fusionner les dalles :
- Force la fusion des dalles MNT/MNS/MNH téléchargées en un raster unique.

Créer la ZIV :
- Exécute le modèle de traitement « Création ZIV » embarqué sur le MNT
  fusionné. Les algorithmes de visibilité sont intégrés dans le plugin —
  aucune dépendance externe n'est nécessaire.
- Le raster de sortie est stylisé en "effet nuit" : les zones non visibles
  (valeurs 0 et NoData) sont peintes en noir à 80 % d'opacité ; l'empreinte
  projet (0.1) et les zones réellement visibles (1+) restent transparentes
  pour laisser apparaître le fond de carte.
- Masquage automatique : les cellules tombant dans une forêt (BD Forêt IGN)
  ou sur un bâtiment (BDTOPO) sont forcées à "hors visibilité" — on ne voit
  pas le projet depuis une canopée ou un toit. Les empreintes sont
  téléchargées via le WFS IGN ; si indisponible, la ZIV sort inchangée.
"""
        )

    def initAlgorithm(self, config=None):
        """Initialize the algorithm parameters."""
        # --- 1) Projet & emprise & rayon -----------------------------------
        # ZIP selection (input project polygon)
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr("Sélection de la ZIP"),
                [QgsProcessing.TypeVectorAnyGeometry],
            )
        )

        # Analysis radius - used BOTH for tile download buffer AND ZIV.
        # Placed next to the ZIP because it drives the WFS query upstream.
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ZIV_RADIUS,
                self.tr(
                    "Rayon d'analyse (mètres) - utilisé pour le buffer de "
                    "téléchargement et la ZIV"
                ),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=300.0,
                minValue=1.0,
                maxValue=50000.0,
                optional=False,
            )
        )

        # --- 2) Téléchargement IGN -----------------------------------------
        # Data type selection - MNS by default (Digital Surface Model, includes
        # vegetation + buildings → correct height reference for visibility studies)
        self.addParameter(
            QgsProcessingParameterEnum(
                self.DATA_TYPE,
                self.tr("Type de données à télécharger"),
                options=self.DATA_TYPE_OPTIONS,
                defaultValue=1,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.FORCE_DOWNLOAD,
                self.tr(
                    "Forcer le re-téléchargement (supprime et retélécharge "
                    "les dalles déjà présentes localement)"
                ),
                defaultValue=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MERGE_TILES,
                self.tr("Fusionner les dalles téléchargées (MNT/MNS/MNH)"),
                defaultValue=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.LOAD_LAYER,
                self.tr("Charger les données après le téléchargement"),
                defaultValue=True,
            )
        )

        # --- 3) ZIV ---------------------------------------------------------
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CREATE_ZIV,
                self.tr(
                    "Créer la Zone d'Influence Visuelle (ZIV) à partir du MNT fusionné"
                ),
                defaultValue=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.ZIV_PANEL_HEIGHT,
                self.tr("ZIV : hauteur de l'observateur (en mètres)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.7,
                minValue=0.1,
                maxValue=500.0,
                optional=False,
            )
        )

        # Symbology choice for the ZIV raster (audit V7).
        # Default sourced from plugin preferences (audit T3), falling back
        # to spotlight-night when preferences are unavailable.
        try:
            from .preferences_dialog import get as _pref_get

            default_symb = int(
                _pref_get("ziv_symbology_default")
                if _pref_get("ziv_symbology_default") is not None
                else self.SYMB_SPOTLIGHT_NIGHT
            )
        except Exception:
            default_symb = self.SYMB_SPOTLIGHT_NIGHT
        self.addParameter(
            QgsProcessingParameterEnum(
                self.ZIV_SYMBOLOGY,
                self.tr("ZIV : symbologie"),
                options=self.ZIV_SYMBOLOGY_OPTIONS,
                defaultValue=default_symb,
            )
        )

        # Photo viewpoints (v3.11.0, opt-in checkbox since v3.13.0) -
        # camera locations on the most visible sectors, restricted to
        # roads + dwelling surroundings, spread over 3 distance rings.
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SUGGEST_VIEWPOINTS,
                self.tr(
                    "Suggérer des points de vue photo (photomontages) "
                    "sur les secteurs les plus visibles"
                ),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.NB_VIEWPOINTS,
                self.tr(
                    "Points de vue photo : nombre à générer "
                    "(réparti proche / moyen / éloigné)"
                ),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=6,
                minValue=1,
                maxValue=30,
            )
        )

        # --- 4) Sorties optionnelles ---------------------------------------
        # ZIV - output raster
        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.ZIV_OUTPUT,
                self.tr("Sortie ZIV (raster) - laisser vide pour temporaire"),
                optional=True,
                createByDefault=False,
            )
        )

        # Add output definitions
        self.addOutput(
            QgsProcessingOutputFolder(
                "OUTPUT_DIRECTORY", self.tr("Répértoire de téléchargement")
            )
        )

        self.addOutput(QgsProcessingOutputFile("OUTPUT_FILE", self.tr("Data file")))

        # Add multi-file output for iteration
        self.addOutput(
            QgsProcessingOutputString(
                "OUTPUT_FILES",
                self.tr("Data files for iteration (semicolon separated)"),
            )
        )

    def _cleanup_temp_files(self):
        """Clean up all tracked temporary files."""
        for temp_file in self._temp_files.copy():
            try:
                if temp_file.exists():
                    temp_file.unlink()
                self._temp_files.discard(temp_file)
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Failed to cleanup temp file {temp_file}: {e}")

    @contextlib.contextmanager
    def _create_temp_file(self, directory: Path, prefix: str = "temp_"):
        """Context manager for temporary file creation with automatic cleanup."""
        temp_file = directory / f"{prefix}{uuid.uuid4().hex}"
        self._temp_files.add(temp_file)
        try:
            yield temp_file
        finally:
            try:
                if temp_file.exists():
                    temp_file.unlink()
                self._temp_files.discard(temp_file)
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Failed to cleanup temp file {temp_file}: {e}")

    def _check_disk_space(self, directory: Path, required_mb: int) -> bool:
        """Check available disk space before download using cross-platform method."""
        try:
            # Use shutil.disk_usage() for cross-platform compatibility
            total, used, free = shutil.disk_usage(directory)
            available_mb = free / (1024 * 1024)
            if available_mb < required_mb:
                self.logger.error(
                    f"Insufficient disk space. Required: {required_mb}MB, "
                    f"Available: {available_mb:.1f}MB"
                )
                return False
            self.logger.info(
                f"Disk space OK: {available_mb:.0f}MB free for "
                f"{required_mb}MB required."
            )
            return True
        except Exception as e:
            # Non-fatal: log loudly so the user understands that the safety
            # net is off, but keep True - blocking on a stat() failure would
            # be worse than running with a possibly-full disk (audit fix I-10).
            self.logger.warning(
                f"Disk-space check failed ({e}). Proceeding anyway - if the "
                "download fails with 'no space left', this is the cause."
            )
            return True

    def _validate_file_integrity(
        self, file_path: Path, expected_min_size: int = 1024
    ) -> bool:
        """Validate downloaded file integrity."""
        try:
            if not file_path.exists():
                self.logger.error(f"File does not exist: {file_path}")
                return False

            file_size = file_path.stat().st_size
            if file_size < expected_min_size:
                self.logger.error(f"File too small ({file_size} bytes): {file_path}")
                return False

            # For ZIP files, try to open them
            if file_path.suffix.lower() == ".zip":
                try:
                    with zipfile.ZipFile(file_path, "r") as zip_ref:
                        # Test the ZIP file integrity
                        zip_ref.testzip()
                except zipfile.BadZipFile:
                    self.logger.error(f"Corrupted ZIP file: {file_path}")
                    return False

            return True
        except Exception as e:
            self.logger.error(f"Error validating file {file_path}: {e}")
            return False

    def _safe_remove_file(self, file_path: Path) -> bool:
        """Safely remove a file with proper error handling."""
        try:
            if file_path.exists():
                # Sur Windows, parfois le fichier peut être verrouillé
                if os.name == "nt":  # Windows
                    for attempt in range(3):
                        try:
                            file_path.unlink()
                            return True
                        except PermissionError:
                            if attempt < 2:
                                time.sleep(0.1)  # Attendre un peu avant de réessayer
                                continue
                            else:
                                raise
                else:
                    file_path.unlink()
                    return True
            return True
        except Exception as e:
            self.logger.error(f"Failed to remove file {file_path}: {e}")
            return False

    def load_point_cloud_layer(self, file_path: str) -> bool:
        """Load a point cloud layer into QGIS project with classified renderer."""
        try:
            layer_name = Path(file_path).stem
            self.logger.info(f"Loading point cloud layer: {layer_name}")

            options = QgsPointCloudLayer.LayerOptions()
            options.skipIndexGeneration = True
            options.skipStatisticsCalculation = True

            layer = QgsPointCloudLayer(file_path, layer_name, "copc", options)
            if not layer.isValid():
                self.logger.error(f"Failed to create valid layer from {file_path}")
                return False

            # Set up classified renderer
            renderer = QgsPointCloudClassifiedRenderer("Classification")
            renderer.setCategories(QgsPointCloudClassifiedRenderer.defaultCategories())
            layer.setRenderer(renderer)

            # Add to project
            QgsProject.instance().addMapLayer(layer)
            self.logger.info(f"Successfully loaded point cloud layer: {layer_name}")
            return True
        except Exception as e:
            self.logger.error(f"Error loading point cloud layer: {str(e)}")
            return False

    def load_raster_layer(self, file_path: str, data_type: int) -> bool:
        """Load a raster layer into QGIS project with appropriate styling."""
        try:
            layer_name = Path(file_path).stem
            self.logger.info(f"Loading raster layer: {layer_name}")

            # Create raster layer
            layer = QgsRasterLayer(file_path, layer_name)
            if not layer.isValid():
                self.logger.error(f"Failed to create valid layer from {file_path}")
                return False

            # Add to project
            QgsProject.instance().addMapLayer(layer)
            self.logger.info(f"Successfully loaded raster layer: {layer_name}")
            return True
        except Exception as e:
            self.logger.error(f"Error loading raster layer: {str(e)}")
            return False

    def _save_buffer_shapefile(
        self, geom: QgsGeometry, output_dir: Path, radius: float
    ) -> str:
        """Save the analysis buffer geometry as a Shapefile in EPSG:2154.

        Uses a unique timestamped filename so successive runs never collide
        with a shapefile still locked by QGIS from a previous execution
        (this is the most common cause of "shapefile not generated" on
        Windows: GDAL cannot overwrite a file currently locked by an open
        layer in the project).

        Returns the output path on success, '' otherwise.
        """
        writer = None
        output_path = None
        try:
            crs = QgsCoordinateReferenceSystem("EPSG:2154")
            fields = QgsFields()
            fields.append(QgsField("name", QVariant.String, len=50))
            fields.append(QgsField("radius_m", QVariant.Double))

            # Unique filename for this run -> no collision with a still-open
            # layer from a previous execution.
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = (
                output_dir / f"rayon_analyse_{int(round(radius))}m_{timestamp}.shp"
            )

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "ESRI Shapefile"
            options.fileEncoding = "UTF-8"

            writer = QgsVectorFileWriter.create(
                str(output_path),
                fields,
                QgsWkbTypes.Polygon,
                crs,
                QgsCoordinateTransformContext(),
                options,
            )
            if writer is None:
                self.logger.error(
                    f"QgsVectorFileWriter returned None for: {output_path}"
                )
                return ""
            if writer.hasError() != QgsVectorFileWriter.NoError:
                self.logger.error(
                    f"Could not create buffer SHP at {output_path}: "
                    f"{writer.errorMessage()}"
                )
                return ""

            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            feat.setAttribute("name", "Rayon d'analyse")
            feat.setAttribute("radius_m", float(radius))
            ok = writer.addFeature(feat)
            if not ok:
                self.logger.error(
                    f"Failed to add feature to buffer SHP: "
                    f"{writer.errorMessage()}"
                )

            # Final sanity check
            if not output_path.exists() or output_path.stat().st_size == 0:
                self.logger.error(
                    f"Buffer SHP missing or empty after write: {output_path}"
                )
                return ""

            self.logger.info(f"Analysis buffer saved to: {output_path}")
            return str(output_path)
        except Exception as e:
            self.logger.error(f"Error saving buffer shapefile: {e}")
            import traceback

            self.logger.error(traceback.format_exc())
            return ""
        finally:
            # Ensure the writer is always flushed/destroyed, even on exception
            # (audit fix M-7).
            try:
                del writer
            except Exception:
                pass

    def _apply_red_dashed_style(self, layer: QgsVectorLayer) -> None:
        """Apply a red-dashed outline / transparent fill style to a polygon layer."""
        try:
            symbol = QgsFillSymbol.createSimple(
                {
                    "color": "0,0,0,0",
                    "outline_color": "255,0,0,255",
                    "outline_width": "0.8",
                    "outline_style": "dash",
                }
            )
            layer.renderer().setSymbol(symbol)
            layer.triggerRepaint()
        except Exception as e:
            self.logger.warning(f"Could not apply red-dashed style: {e}")

    def _load_buffer_layer(self, shp_path: str) -> None:
        """Load the analysis buffer shapefile in QGIS with red-dashed style."""
        try:
            layer = QgsVectorLayer(shp_path, "Rayon d'analyse", "ogr")
            if not layer.isValid():
                self.logger.warning(
                    f"Buffer shapefile loaded but layer invalid: {shp_path}"
                )
                return
            self._apply_red_dashed_style(layer)
            QgsProject.instance().addMapLayer(layer)
        except Exception as e:
            self.logger.warning(f"Could not load buffer layer: {e}")

    def _apply_ziv_color_ramp(self, layer: QgsRasterLayer) -> None:
        """Apply the ZIV symbology - dispatched on `self._ziv_symbology`.

        Pixel value meaning:
          - NoData / 0  : cell outside the analysis area (no info / burn)
          - 0.1         : project footprint rasterized at 0.1
          - 1+          : viewshed count = nb observers seeing the cell

        Renderers (audit V7):
          - 0 SYMB_SPOTLIGHT_NIGHT : night-black 80% on non-visible cells,
                                     transparent over visible (default).
          - 1 SYMB_WARM_GRADIENT   : classic white → yellow → orange → red
                                     ramp on visible cells, non-visible
                                     transparent (style v1.3.x).
          - 2 SYMB_TRANSLUCENT_RED : uniform red 30% on visible cells,
                                     non-visible transparent.
        """
        symb = getattr(self, "_ziv_symbology", self.SYMB_SPOTLIGHT_NIGHT)
        try:
            if symb == self.SYMB_WARM_GRADIENT:
                self._apply_ramp_warm_gradient(layer)
            elif symb == self.SYMB_TRANSLUCENT_RED:
                self._apply_ramp_translucent_red(layer)
            else:
                self._apply_ramp_spotlight_night(layer)
        except Exception as e:
            self.logger.warning(f"Could not apply ZIV color ramp: {e}")

    def _apply_ramp_spotlight_night(self, layer: QgsRasterLayer) -> None:
        """Default spotlight-night renderer (v1.6.0 → present)."""
        provider = layer.dataProvider()
        night = QColor(0, 0, 0, 204)
        invisible = QColor(0, 0, 0, 0)

        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Interpolated)
        color_ramp.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(-1e6, night, "Hors visibilité"),
                QgsColorRampShader.ColorRampItem(0.0999, night, "Hors visibilité"),
                QgsColorRampShader.ColorRampItem(
                    0.1, invisible, "Empreinte projet (0.1)"
                ),
                QgsColorRampShader.ColorRampItem(1e6, invisible, "Visible"),
            ]
        )
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(color_ramp)
        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        renderer.setClassificationMin(-1e6)
        renderer.setClassificationMax(1e6)
        renderer.setOpacity(1.0)
        try:
            renderer.setNodataColor(night)
        except AttributeError:
            pass
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        self.logger.info("ZIV symbology: spotlight night (defaut)")

    def _apply_ramp_warm_gradient(self, layer: QgsRasterLayer) -> None:
        """Classic white → yellow → orange → red gradient (v1.3.x)."""
        provider = layer.dataProvider()
        invisible = QColor(0, 0, 0, 0)

        # Discrete ramp: 0/0.1 transparent, then white at 1, yellow at 4,
        # orange at 8, red at 16+. The exact thresholds are conservative
        # so the colours read correctly even on dense viewsheds.
        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Interpolated)
        color_ramp.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(-1e6, invisible, "Hors visibilité"),
                QgsColorRampShader.ColorRampItem(0.0999, invisible, "Hors visibilité"),
                QgsColorRampShader.ColorRampItem(
                    0.1, invisible, "Empreinte projet (0.1)"
                ),
                QgsColorRampShader.ColorRampItem(
                    1.0, QColor(255, 255, 255, 180), "1 observateur"
                ),
                QgsColorRampShader.ColorRampItem(
                    4.0, QColor(255, 235, 100, 200), "≥ 4"
                ),
                QgsColorRampShader.ColorRampItem(
                    8.0, QColor(255, 150, 30, 220), "≥ 8"
                ),
                QgsColorRampShader.ColorRampItem(
                    16.0, QColor(200, 20, 30, 235), "≥ 16"
                ),
            ]
        )
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(color_ramp)
        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        renderer.setClassificationMin(-1e6)
        renderer.setClassificationMax(16.0)
        renderer.setOpacity(1.0)
        try:
            renderer.setNodataColor(invisible)
        except AttributeError:
            pass
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        self.logger.info("ZIV symbology: warm gradient (style v1.3.x)")

    def _apply_ramp_translucent_red(self, layer: QgsRasterLayer) -> None:
        """Uniform translucent red wherever the viewshed sees the cell."""
        provider = layer.dataProvider()
        invisible = QColor(0, 0, 0, 0)
        red = QColor(200, 20, 30, 100)  # ~40% red

        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Discrete)
        color_ramp.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(0.0999, invisible, "Hors visibilité"),
                QgsColorRampShader.ColorRampItem(0.1, invisible, "Empreinte (0.1)"),
                QgsColorRampShader.ColorRampItem(1e6, red, "Visible"),
            ]
        )
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(color_ramp)
        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        renderer.setClassificationMin(-1e6)
        renderer.setClassificationMax(1e6)
        renderer.setOpacity(1.0)
        try:
            renderer.setNodataColor(invisible)
        except AttributeError:
            pass
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        self.logger.info("ZIV symbology: translucent red (uniform)")

    def _run_ziv_model(
        self,
        dem_path: str,
        zip_source,
        ziv_radius: float,
        ziv_panel_height: float,
        ziv_output_path: str,
    ) -> str:
        """Run the 'Création ZIV' processing model with the merged DEM.

        Returns the path of the generated ZIV raster on success, '' otherwise.
        """
        try:
            model_path = Path(__file__).parent / "models" / "creation_ziv.model3"
            if not model_path.exists():
                self.logger.error(
                    f"ZIV model file not found at {model_path}"
                )
                return ""

            self.logger.info(f"Loading ZIV model from: {model_path}")
            model = QgsProcessingModelAlgorithm()
            if not model.fromFile(str(model_path)):
                self.logger.error("Failed to load 'Création ZIV' model file")
                return ""
            model.initAlgorithm()

            # If no explicit output requested, generate a TEMPORARY_OUTPUT
            ziv_out = ziv_output_path if ziv_output_path else "TEMPORARY_OUTPUT"

            params = {
                "couche_topographique_lidar": dem_path,
                "zip": zip_source,
                "taille_du_rayon_danalyse_en_mtres": ziv_radius,
                "hauteur_des_panneaux_en_mtres": ziv_panel_height,
                "rayon_danalyse": "TEMPORARY_OUTPUT",
                "zone_dinfluence_visuelle_ziv": ziv_out,
            }

            self.logger.info(
                f"Running ZIV model (radius={ziv_radius} m, "
                f"observer_height={ziv_panel_height} m)"
            )
            result = processing.run(model, params, feedback=self.feedback)

            ziv_result_path = result.get("zone_dinfluence_visuelle_ziv", "")
            if ziv_result_path:
                self.logger.info(f"ZIV created successfully: {ziv_result_path}")
                # Mask out forest canopy + building footprints: a viewshed
                # over an MNS counts treetops and rooftops as cells that
                # "see" the panels, but nobody stands there. Same principle
                # as the project-footprint elimination baked into the model.
                ziv_result_path = self._mask_ziv_landcover(
                    ziv_result_path, ziv_output_path
                )
                # Try to auto-load the produced ZIV raster with custom styling.
                try:
                    layer = QgsRasterLayer(
                        ziv_result_path, "Zone d'Influence Visuelle (ZIV)"
                    )
                    if layer.isValid():
                        self._apply_ziv_color_ramp(layer)
                        QgsProject.instance().addMapLayer(layer)
                except Exception as load_err:
                    self.logger.warning(
                        f"Could not auto-load ZIV layer: {load_err}"
                    )
            else:
                self.logger.warning("ZIV model returned no output path")
            return ziv_result_path

        except Exception as e:
            self.logger.error(f"Error running ZIV model: {str(e)}")
            import traceback

            tb = traceback.format_exc()
            self.logger.error(tb)
            # Show a persistent popup so the user can actually read the error
            # rather than seeing it flash by in the Processing dialog log.
            self._show_persistent_error(
                "Erreur lors du calcul de la ZIV",
                f"{e}\n\n--- Détail technique ---\n{tb}",
            )
            return ""

    def _show_persistent_error(self, title: str, message: str) -> None:
        """Display a modal QMessageBox.critical so the error doesn't vanish
        when the Processing dialog auto-closes."""
        try:
            main_win = None
            try:
                from qgis.utils import iface as _iface

                if _iface is not None:
                    main_win = _iface.mainWindow()
            except Exception:
                pass
            box = QMessageBox(main_win)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle(f"{PLUGIN_DISPLAY_NAME} - {title}")
            box.setText(title)
            # Show only the short message in the main area, the full
            # traceback goes into the detailed-text section so the dialog
            # stays readable.
            short = message.split("\n\n--- Détail technique ---", 1)[0]
            box.setInformativeText(short)
            if "Détail technique" in message:
                box.setDetailedText(message)
            box.setStandardButtons(QMessageBox.Ok)
            box.exec()
        except Exception as e:
            # Last resort - never crash the plugin because of error display
            try:
                self.logger.error(f"Could not show error popup: {e}")
            except Exception:
                pass

    def _finalize_result(self, result_dict: dict) -> dict:
        # Persist the tile manifest as the very first thing - even if the
        # ZIV step fails later, the tiles we've downloaded are still
        # valid and worth remembering for next time (audit V2).
        try:
            self._save_tile_manifest()
        except Exception:
            pass

        """Post-download orchestration, executed in this strict order:

        1. Load merged DEM in QGIS + ask the user to validate the merge
           (catches missing tiles before the long ZIV computation runs)
        2. ZIV computation (if requested and validated)
        3. Toggle the merged DEM visibility OFF (not removed!) so the
           basemap shows through instead of the grey DEM
        4. Load the analysis buffer LAST so it sits on top of all layers
        """
        ziv_path = ""
        dem_layer_id = None  # kept for symmetry, never populated since v2.1.6
        dem_path = result_dict.get("OUTPUT_FILE", "")
        dem_is_valid_raster = (
            dem_path
            and str(dem_path).lower().endswith((".tif", ".tiff", ".asc"))
        )

        # NB: the previous "load MNS + user validation popup" step was
        # removed AGAIN in v3.0.9 after being re-introduced in v3.0.6.
        # Each attempt (v1.5.0 / v3.0.6) ended up destabilising the
        # plugin on real workflows - either freezing QGIS, popping
        # ghost dialogs, or crashing on the next launch. The plugin
        # now runs straight through: if the user notices a hole in the
        # final ZIV he can simply relaunch with "Forcer le re-
        # téléchargement". See changelog of v3.0.9 for the full history.

        # ---- ZIV step ----
        if getattr(self, "_ziv_requested", False):
            if not dem_path:
                self.logger.warning(
                    "ZIV requested but no output file available - skipping ZIV"
                )
            elif str(dem_path).lower().endswith(
                (".laz", ".las", ".copc.laz")
            ):
                self.logger.warning(
                    "ZIV requires a DEM raster (MNT/MNS/MNH) - "
                    "cannot use point cloud output"
                )
            elif not dem_is_valid_raster:
                self.logger.warning(
                    f"ZIV step skipped - unsupported DEM format: {dem_path}"
                )
            else:
                ziv_path = self._run_ziv_model(
                    dem_path,
                    self._ziv_zip_source,
                    self._ziv_radius,
                    self._ziv_panel_height,
                    self._ziv_output_path,
                )
                if ziv_path:
                    result_dict[self.ZIV_OUTPUT] = ziv_path

        # ---- 3) Hide (do NOT delete) the merged DEM once ZIV is done ----
        if ziv_path and dem_layer_id:
            try:
                root = QgsProject.instance().layerTreeRoot()
                node = root.findLayer(dem_layer_id)
                if node is not None:
                    node.setItemVisibilityChecked(False)
                    self.logger.info(
                        "Merged DEM hidden in the layer tree "
                        "(kept on disk, kept in the project)."
                    )
            except Exception as hide_err:
                self.logger.warning(
                    f"Could not hide merged DEM layer: {hide_err}"
                )

        # ---- 3) Buffer layer LAST (so it appears on top) ----
        buffer_layer = None
        buffer_shp = getattr(self, "_buffer_shp_path", "")
        radius = getattr(self, "_ziv_radius", 0.0) or 0.0
        if buffer_shp:
            # Layer name carries the radius the user typed, e.g.
            # "Rayon d'analyse - 300 m" - also stored as field `radius_m`.
            try:
                layer_label = f"Rayon d'analyse - {int(round(radius))} m"
            except Exception:
                layer_label = "Rayon d'analyse"
            buffer_layer = QgsVectorLayer(buffer_shp, layer_label, "ogr")
            if buffer_layer.isValid():
                self._apply_red_dashed_style(buffer_layer)
                QgsProject.instance().addMapLayer(buffer_layer)
            else:
                self.logger.warning(
                    f"Buffer shapefile loaded but layer invalid: {buffer_shp}"
                )
                buffer_layer = None

        # ---- 3bis) Photo viewpoints on the most visible sectors ----
        # Loaded after the buffer so the red stars sit on top of every
        # other layer. Best-effort: never blocks the pipeline.
        if ziv_path:
            self._generate_photo_viewpoints(ziv_path)

        return result_dict

    def _downsample_dem(
        self, input_path: str, downloads_dir: Path
    ) -> str:
        """Downsample the merged DEM to `ZIV_TARGET_RES_M` resolution
        using `gdal:warpreproject` with the Average resampling method
        (speeds up the subsequent ZIV computation on raw 0.5/1 m
        LiDAR HD).

        The Average method preserves the mean elevation across the
        aggregation window, which is the appropriate choice for a DEM
        used by a viewshed (Nearest would create staircase artefacts,
        Bilinear would smooth out ridge lines). The operation is
        wrapped in a try/except so a failure here never blocks the
        rest of the pipeline - we just fall back to the input raster.
        """
        try:
            import processing

            res = self.ZIV_TARGET_RES_M
            res_tag = f"{res:g}m".replace(".", "_")
            src = Path(input_path)
            if not src.exists():
                self.logger.warning(
                    f"Downsample: source DEM missing ({src}) - skipping."
                )
                return input_path
            output_path = (
                Path(downloads_dir) / f"{src.stem}_{res_tag}{src.suffix}"
            )
            # If the file was downsampled before, just return the existing
            # result (idempotent across runs).
            if output_path.exists() and output_path.stat().st_size > 1024:
                self.logger.info(
                    f"Downsample: existing {res:g} m DEM reused ({output_path})."
                )
                return str(output_path)
            self.logger.info(
                f"Downsample: rééchantillonnage du DEM à {res:g} m via "
                f"gdal:warpreproject (Average) -> {output_path}"
            )
            processing.run(
                "gdal:warpreproject",
                {
                    "INPUT": str(src),
                    # Same CRS in and out - we only change the cell size
                    "SOURCE_CRS": None,
                    "TARGET_CRS": None,
                    "RESAMPLING": 5,  # 5 = Average / moyenne
                    "NODATA": None,
                    "TARGET_RESOLUTION": res,
                    "OPTIONS": "COMPRESS=DEFLATE|PREDICTOR=2",
                    "DATA_TYPE": 0,  # 0 = use input data type
                    "TARGET_EXTENT": None,
                    "TARGET_EXTENT_CRS": None,
                    "MULTITHREADING": False,
                    "EXTRA": "",
                    "OUTPUT": str(output_path),
                },
            )
            if not output_path.exists() or output_path.stat().st_size < 1024:
                self.logger.warning(
                    "Downsample: warpreproject produit un fichier vide - "
                    "utilisation du DEM d'origine."
                )
                return input_path
            return str(output_path)
        except Exception as e:
            self.logger.warning(
                f"Downsample: rééchantillonnage à {self.ZIV_TARGET_RES_M:g} m "
                f"a échoué ({e}) - utilisation du DEM d'origine."
            )
            return input_path

    def _mask_ziv_landcover(
        self, ziv_path: str, requested_output: str = ""
    ) -> str:
        """Burn 0 ("hors visibilité") into the ZIV wherever a forest
        (BD Forêt) or a building (BDTOPO batiment) footprint sits.

        Rationale: with an MNS the viewshed treats tree canopy and roof
        tops as elevated surfaces that "see" the panels, producing
        visibility artefacts over places where no observer ever stands.

        Method (v3.9.3): we work on a COPY of the ZIV and use
        `gdal:rasterize_over_fixed_value` to burn the value 0 directly
        into that raster, on its own native grid. This deliberately
        avoids the previous mask-raster + gdal:rastercalculator approach,
        which kept turning the whole ZIV to NoData: gdal:rasterize always
        flags 0 as NoData on the mask, and rastercalculator then
        propagated that NoData over the entire output. Burning in place
        sidesteps NoData, grid-alignment and dimension issues entirely.

        Best-effort: any failure (offline, no coverage, GDAL error)
        logs a warning and returns the unmodified ZIV path.

        If `requested_output` is set, the masked raster is produced there
        so the output contract is honoured (the burn is in place, so we
        copy the ZIV to that path first and burn into it).
        """
        try:
            import processing
            from . import ign_data

            src = Path(ziv_path)
            if not src.exists():
                return ziv_path
            ziv = QgsRasterLayer(ziv_path, "_ziv_mask_src")
            if not ziv.isValid():
                self.logger.warning(
                    "Masquage forêt/bâti : raster ZIV invalide - ignoré."
                )
                return ziv_path
            extent = ziv.extent()
            work_dir = src.parent

            # Burn happens IN PLACE, so always operate on a copy - never
            # the original ZIV temp. Put it at the user-requested path if
            # one was given.
            masked_out = (
                Path(requested_output)
                if requested_output
                else work_dir / f"{src.stem}_masque.tif"
            )
            try:
                shutil.copyfile(str(src), str(masked_out))
            except Exception as cp_err:
                self.logger.warning(
                    f"Masquage : copie de la ZIV échouée ({cp_err}) - "
                    "ZIV inchangée."
                )
                return ziv_path

            targets = [
                (ign_data.WFS_BUILDINGS, "batiments"),
                (ign_data.WFS_FOREST, "foret"),
            ]
            burned = 0
            for type_name, stem in targets:
                geojson = work_dir / f"_ziv_mask_{stem}.geojson"
                if not ign_data._download_wfs_layer(
                    extent, type_name, geojson, self.logger
                ):
                    continue
                vec = QgsVectorLayer(str(geojson), f"_fp_{stem}", "ogr")
                if not vec.isValid() or vec.featureCount() == 0:
                    continue
                # Burn 0 ("hors visibilité") into the ZIV copy via the GDAL
                # Python API (NOT the QGIS gdal:rasterize_over* wrapper,
                # which crashed QGIS in the Processing worker thread).
                if ign_data.burn_into_raster_inplace(
                    str(masked_out), str(geojson),
                    burn_value=0.0, add=False, logger=self.logger,
                ):
                    burned += 1
                else:
                    self.logger.warning(
                        f"Masquage : burn {stem} échoué."
                    )

            if burned == 0:
                self.logger.info(
                    "Masquage forêt/bâti : aucune empreinte appliquée "
                    "(hors couverture ou WFS indisponible)."
                )
                # masked_out is a plain copy of the ZIV. Keep it only if
                # the caller asked for a specific output path.
                return str(masked_out) if requested_output else ziv_path

            self.logger.info(
                f"Masquage forêt/bâti appliqué ({burned} couche(s), "
                f"burn 0 sur place) -> {masked_out}"
            )
            return str(masked_out)
        except Exception as e:
            self.logger.warning(
                f"Masquage forêt/bâti échoué ({e}) - ZIV inchangée."
            )
            return ziv_path

    def _generate_photo_viewpoints(self, ziv_path: str):
        """Generate camera locations ("points de vue photo") on the most
        visible sectors of the final ZIV (v3.11.0, audit V6).

        Method:
          1. Read the masked ZIV; keep only truly visible cells (value
             >= 1, which excludes NoData, masked cells and the 0.1
             project footprint).
          2. Build a candidate mask on the ZIV grid (in-memory GDAL
             raster): BDTOPO road segments (allTouched) + a 50 m buffer
             around BDTOPO buildings - photos must be shot from
             accessible ground: roads, or dwelling surroundings when
             the project is highly visible from them. If no candidate
             is visible (or the WFS fails), fall back to the raw ZIV.
          3. Split the analysis area into three distance rings measured
             from the ZIP polygon itself (gdal.ComputeProximity):
             "proche" (<= 100 m), "moyen" (100 m -> 2/3 of the analysis
             radius) and "eloigne" (beyond). The requested count is
             split evenly across the rings (6 -> 2/2/2; remainder goes
             to the closest rings first). Unfillable quotas (ring with
             no visible candidate) are redistributed to the other rings.
          4. Within each ring: greedy peak-picking with non-maximum
             suppression (disk of 25 % of the analysis radius, shared
             across rings) so points land on distinct sectors.
          5. Save as a styled point Shapefile (red stars + "PV n"
             labels) with fields id / visib / dist_m (distance to the
             ZIP) / azimut / categorie, and load it into the project.

        Best-effort: returns the loaded QgsVectorLayer, or None (never
        raises - the ZIV pipeline must not fail because of this step).
        """
        count = int(getattr(self, "_viewpoints_count", 0) or 0)
        if count <= 0 or not ziv_path:
            return None
        try:
            import math

            import numpy as np
            from osgeo import gdal, ogr

            from . import ign_data

            gdal.UseExceptions()
            ds = gdal.Open(str(ziv_path))
            if ds is None:
                return None
            band = ds.GetRasterBand(1)
            arr = band.ReadAsArray().astype(float)
            nod = band.GetNoDataValue()
            if nod is not None:
                arr[arr == nod] = 0.0
            arr[~np.isfinite(arr)] = 0.0
            arr[arr < 1.0] = 0.0  # keep real visibility counts only
            if float(arr.max()) < 1.0:
                self.logger.info(
                    "Points de vue photo : aucune cellule visible - ignoré."
                )
                return None

            rows, cols = arr.shape
            gt = ds.GetGeoTransform()
            pix = abs(gt[1]) or 1.0
            xmin = gt[0]
            ymax = gt[3]
            extent = QgsRectangle(
                xmin, ymax + rows * gt[5], xmin + cols * gt[1], ymax
            )
            work_dir = Path(
                str(getattr(self, "_working_dir", "") or "")
            )
            if not work_dir or not work_dir.exists():
                work_dir = Path(ziv_path).parent

            # ---- Candidate mask: roads + buffered buildings ----------
            cand_sources = []
            roads_geojson = work_dir / "_pv_routes.geojson"
            if ign_data._download_wfs_layer(
                extent, ign_data.WFS_ROADS, roads_geojson, self.logger
            ):
                cand_sources.append(str(roads_geojson))
            # Buildings: reuse the footprints already downloaded by the
            # landcover masking step when present, else download.
            bati_geojson = Path(ziv_path).parent / "_ziv_mask_batiments.geojson"
            if not bati_geojson.exists():
                bati_geojson = work_dir / "_pv_batiments.geojson"
                if not ign_data._download_wfs_layer(
                    extent, ign_data.WFS_BUILDINGS, bati_geojson,
                    self.logger,
                ):
                    bati_geojson = None
            if bati_geojson is not None and bati_geojson.exists():
                buf_path = work_dir / "_pv_batiments_buffer.geojson"
                if self._buffer_vector_ogr(bati_geojson, buf_path, 50.0):
                    cand_sources.append(str(buf_path))

            # STRICT: a viewpoint may ONLY sit on a road cell or near a
            # building (dwelling surroundings). We NEVER fall back to the
            # raw ZIV - a point in open ground with no road/building is
            # useless for a photomontage (user requirement). If no such
            # candidate is visible we generate nothing rather than an
            # off-road point. Unfilled ring quotas are redistributed to
            # the most-visible remaining road/building cell of the îlot
            # (i.e. "un peu plus proche"), never to open ground.
            cand = None
            if cand_sources:
                mem = gdal.GetDriverByName("MEM").Create(
                    "", cols, rows, 1, gdal.GDT_Byte
                )
                mem.SetGeoTransform(gt)
                mem.SetProjection(ds.GetProjection())
                for src in cand_sources:
                    try:
                        gdal.Rasterize(
                            mem, src,
                            options=gdal.RasterizeOptions(
                                bands=[1], burnValues=[1], allTouched=True
                            ),
                        )
                    except Exception as rz_err:
                        self.logger.warning(
                            f"Points de vue : rasterize candidat échoué "
                            f"({rz_err})."
                        )
                mask = mem.ReadAsArray()
                masked = arr * (mask == 1)
                if float(masked.max()) >= 1.0:
                    cand = masked
            if cand is None:
                self.logger.warning(
                    "Points de vue : aucune cellule visible sur une route "
                    "ou aux abords d'un bâtiment - aucun point généré "
                    "(consigne : points uniquement route/bâti)."
                )
                return None

            # ---- Per-îlot selection ----------------------------------
            # Split the ZIP into its disjoint parts (îlots) so a project
            # with several parcels far apart does NOT concentrate every
            # viewpoint on the single most-visible îlot. The requested
            # count is split equitably across îlots (remainder to the
            # most visible), and each îlot keeps its own proche/moyen/
            # éloigné rings (distance measured to THAT îlot).
            radius = float(getattr(self, "_ziv_radius", 0.0) or 0.0)
            NEAR_MAX = 100.0
            aoi_geom = getattr(self, "_aoi_geom", None)
            ilots = []
            if aoi_geom is not None:
                try:
                    ilots = [
                        g for g in aoi_geom.asGeometryCollection()
                        if g is not None and not g.isEmpty()
                    ]
                except Exception:
                    ilots = []
                if not ilots:
                    ilots = [aoi_geom]

            def _proximity_for(geom):
                """Metric distance-to-geom array on the ZIV grid, or None."""
                try:
                    import json as _json

                    gj = {
                        "type": "FeatureCollection",
                        "crs": {"type": "name", "properties": {
                            "name": "urn:ogc:def:crs:EPSG::2154"}},
                        "features": [{"type": "Feature", "properties": {},
                                      "geometry": _json.loads(geom.asJson())}],
                    }
                    gjp = work_dir / "_pv_ilot.geojson"
                    gjp.write_text(_json.dumps(gj), encoding="utf-8")
                    zmem = gdal.GetDriverByName("MEM").Create(
                        "", cols, rows, 1, gdal.GDT_Byte)
                    zmem.SetGeoTransform(gt)
                    zmem.SetProjection(ds.GetProjection())
                    gdal.Rasterize(zmem, str(gjp), options=gdal.RasterizeOptions(
                        bands=[1], burnValues=[1], allTouched=True))
                    pmem = gdal.GetDriverByName("MEM").Create(
                        "", cols, rows, 1, gdal.GDT_Float32)
                    pmem.SetGeoTransform(gt)
                    pmem.SetProjection(ds.GetProjection())
                    gdal.ComputeProximity(
                        zmem.GetRasterBand(1), pmem.GetRasterBand(1),
                        ["VALUES=1", "DISTUNITS=GEO"])
                    return pmem.ReadAsArray().astype(float)
                except Exception as e:
                    self.logger.warning(
                        f"Points de vue : proximité îlot échouée ({e}).")
                    return None

            prox_list = [p for p in (_proximity_for(g) for g in ilots)
                         if p is not None]

            # NMS setup (shared across îlots so no two PM ever collide)
            sep = max(5, int(round(0.25 * radius / pix))) if radius else 25
            yy, xx = np.ogrid[-sep: sep + 1, -sep: sep + 1]
            disk = (yy * yy + xx * xx) <= sep * sep
            work = cand.copy()
            picked = []  # (x, y, val, categorie, dist)

            def _suppress(y, x):
                y0, y1 = max(0, y - sep), min(rows, y + sep + 1)
                x0, x1 = max(0, x - sep), min(cols, x + sep + 1)
                work[y0:y1, x0:x1][
                    disk[y0 - (y - sep): y1 - (y - sep),
                         x0 - (x - sep): x1 - (x - sep)]
                ] = 0.0

            def _ring_of(d, mid):
                return ("proche" if d <= NEAR_MAX
                        else "moyen" if d <= mid else "eloigne")

            if prox_list:
                n_ilots = len(prox_list)
                # Each cell belongs to its NEAREST îlot -> territories.
                nearest = np.argmin(np.stack(prox_list, axis=0), axis=0)
                vis_by_ilot = [
                    float(np.where(nearest == i, cand, 0.0).max())
                    for i in range(n_ilots)
                ]
                order = sorted(range(n_ilots),
                               key=lambda i: vis_by_ilot[i], reverse=True)
                # Equitable split of `count` across îlots (remainder to
                # the most visible îlots first).
                base, rem = divmod(count, n_ilots)
                quota = {i: base for i in range(n_ilots)}
                for k in range(rem):
                    quota[order[k]] += 1

                for i in order:
                    q = quota[i]
                    if q <= 0:
                        continue
                    di = prox_list[i]
                    terr = (nearest == i)
                    R_i = radius if radius > 0 else float(
                        di[terr].max() if terr.any() else di.max())
                    mid_i = max(NEAR_MAX, R_i * (2.0 / 3.0))
                    rings = [
                        ("proche", terr & (di <= NEAR_MAX)),
                        ("moyen", terr & (di > NEAR_MAX) & (di <= mid_i)),
                        ("eloigne", terr & (di > mid_i)),
                    ]
                    b2, r2 = divmod(q, 3)
                    quotas = [b2 + (1 if j < r2 else 0) for j in range(3)]
                    got = 0
                    for (cat, mask), qr in zip(rings, quotas):
                        for _ in range(qr):
                            rw = np.where(mask, work, 0.0)
                            idx = int(np.argmax(rw))
                            val = float(rw.flat[idx])
                            if val < 1.0:
                                break
                            y, x = divmod(idx, cols)
                            picked.append((x, y, val, cat, float(di[y, x])))
                            _suppress(y, x)
                            got += 1
                    # Fill this îlot's unspent quota from its territory.
                    while got < q:
                        rw = np.where(terr, work, 0.0)
                        idx = int(np.argmax(rw))
                        val = float(rw.flat[idx])
                        if val < 1.0:
                            break
                        y, x = divmod(idx, cols)
                        d = float(di[y, x])
                        picked.append((x, y, val, _ring_of(d, mid_i), d))
                        _suppress(y, x)
                        got += 1

            # Global fill for any leftover (no proximity, or all îlots
            # exhausted before reaching the requested count).
            while len(picked) < count:
                idx = int(np.argmax(work))
                val = float(work.flat[idx])
                if val < 1.0:
                    break
                y, x = divmod(idx, cols)
                d = None
                cat = ""
                if prox_list:
                    d = float(min(p[y, x] for p in prox_list))
                    mid = max(NEAR_MAX, (radius if radius > 0 else d) * (2.0 / 3.0))
                    cat = _ring_of(d, mid)
                picked.append((x, y, val, cat, d))
                _suppress(y, x)
            if not picked:
                return None
            self.logger.info(
                f"Points de vue : {len(picked)} PM sur "
                f"{len(prox_list) or 1} îlot(s) ; catégories "
                + ", ".join(
                    f"{c}={sum(1 for p in picked if p[3] == c)}"
                    for c in ("proche", "moyen", "eloigne")
                )
            )

            # ---- Build features (id, visib, dist_m, azimut, cat) -----
            centroid = getattr(self, "_aoi_centroid", None)
            cards = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
            feats = []
            for i, (x, y, val, cat, dist) in enumerate(picked, start=1):
                gx = gt[0] + (x + 0.5) * gt[1]
                gy = gt[3] + (y + 0.5) * gt[5]
                # `dist` is the distance to the owning îlot (proximity
                # raster). Fall back to the centroid distance if unknown.
                if dist is None and centroid is not None:
                    dist = math.hypot(
                        gx - centroid.x(), gy - centroid.y()
                    )
                azim = None
                if centroid is not None:
                    dx = gx - centroid.x()
                    dy = gy - centroid.y()
                    brg = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
                    azim = (
                        f"{cards[int(((brg + 22.5) % 360) // 45)]} "
                        f"({int(round(brg))}°)"
                    )
                feats.append((gx, gy, int(round(val)), dist, azim, cat))

            # ---- Write the Shapefile ---------------------------------
            crs = QgsCoordinateReferenceSystem("EPSG:2154")
            fields = QgsFields()
            fields.append(QgsField("id", QVariant.Int))
            fields.append(QgsField("visib", QVariant.Int))
            fields.append(QgsField("dist_m", QVariant.Double))
            fields.append(QgsField("azimut", QVariant.String, len=12))
            fields.append(QgsField("categorie", QVariant.String, len=8))
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shp = work_dir / f"points_vue_photo_{stamp}.shp"
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "ESRI Shapefile"
            options.fileEncoding = "UTF-8"
            writer = QgsVectorFileWriter.create(
                str(shp), fields, QgsWkbTypes.Point, crs,
                QgsCoordinateTransformContext(), options,
            )
            if writer is None or writer.hasError() != QgsVectorFileWriter.NoError:
                self.logger.warning(
                    "Points de vue : création du shapefile impossible."
                )
                return None
            for i, (gx, gy, vis, dist, azim, cat) in enumerate(
                feats, start=1
            ):
                f = QgsFeature(fields)
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(gx, gy)))
                f.setAttribute("id", i)
                f.setAttribute("visib", vis)
                if dist is not None:
                    f.setAttribute("dist_m", round(dist, 1))
                if azim is not None:
                    f.setAttribute("azimut", azim)
                if cat:
                    f.setAttribute("categorie", cat)
                writer.addFeature(f)
            del writer

            # ---- Load + style (red stars, "PV n" labels) -------------
            layer = QgsVectorLayer(
                str(shp), "Points de vue photo", "ogr"
            )
            if not layer.isValid():
                self.logger.warning(
                    "Points de vue : shapefile écrit mais couche invalide."
                )
                return None
            try:
                from qgis.core import QgsMarkerSymbol

                symbol = QgsMarkerSymbol.createSimple(
                    {
                        "name": "star",
                        "color": "230,30,45,255",
                        "outline_color": "255,255,255,255",
                        "outline_width": "0.4",
                        "size": "5",
                    }
                )
                layer.renderer().setSymbol(symbol)
            except Exception:
                pass
            try:
                from qgis.core import (
                    QgsPalLayerSettings,
                    QgsTextBufferSettings,
                    QgsTextFormat,
                    QgsVectorLayerSimpleLabeling,
                )

                pal = QgsPalLayerSettings()
                pal.fieldName = "concat('PV ', \"id\")"
                pal.isExpression = True
                fmt = QgsTextFormat()
                fmt.setFont(QFont("Arial", 9))
                fmt.setSize(9)
                buf = QgsTextBufferSettings()
                buf.setEnabled(True)
                buf.setSize(1.0)
                buf.setColor(QColor(255, 255, 255))
                fmt.setBuffer(buf)
                pal.setFormat(fmt)
                layer.setLabelsEnabled(True)
                layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
            except Exception:
                pass
            QgsProject.instance().addMapLayer(layer)
            self.logger.info(
                f"Points de vue photo : {len(feats)} point(s) généré(s) "
                f"-> {shp}"
            )
            return layer
        except Exception as e:
            self.logger.warning(
                f"Points de vue photo : génération échouée ({e})."
            )
            return None

    def _buffer_vector_ogr(
        self, src_geojson: Path, dst_geojson: Path, dist_m: float
    ) -> bool:
        """Buffer every feature of a GeoJSON by `dist_m` metres into a
        new GeoJSON (OGR API - no QGIS processing wrapper, cf. the
        v3.10.x thread-worker crash lessons). Returns True on success."""
        try:
            from osgeo import ogr

            src = ogr.Open(str(src_geojson))
            if src is None:
                return False
            lyr = src.GetLayer(0)
            drv = ogr.GetDriverByName("GeoJSON")
            if dst_geojson.exists():
                try:
                    drv.DeleteDataSource(str(dst_geojson))
                except Exception:
                    dst_geojson.unlink(missing_ok=True)
            dst = drv.CreateDataSource(str(dst_geojson))
            out = dst.CreateLayer(
                "buffered", lyr.GetSpatialRef(), ogr.wkbPolygon
            )
            n = 0
            for f in lyr:
                g = f.GetGeometryRef()
                if g is None:
                    continue
                try:
                    b = g.Buffer(float(dist_m))
                except Exception:
                    continue
                nf = ogr.Feature(out.GetLayerDefn())
                nf.SetGeometry(b)
                out.CreateFeature(nf)
                nf = None
                n += 1
            dst = None
            src = None
            return n > 0 and dst_geojson.exists()
        except Exception as e:
            self.logger.warning(f"Points de vue : buffer OGR échoué ({e}).")
            return False

    def merge_rasters_gdal(
        self,
        raster_files: List[str],
        output_folder: Path,
        output_filename: str = "merged_output.tif",
        target_res: float = None,
    ) -> str:
        """Merge raster .tif files using the GDAL Python API.

        When `target_res` is provided, the mosaic is resampled to that
        cell size in the SAME pass (Average). This avoids materialising
        the full-resolution 0.5/1 m mosaic and reading it back for a
        separate downsample - the dominant cost on large LiDAR HD AOIs.
        A 4×4 km block at 0.5 m is ~64 M px; the previous flow wrote it
        with ZLEVEL=9 (max compression, ~16 min) THEN re-read it. Merging
        straight to the target resolution reads the heavy data once and
        writes only ~4 M px.
        """
        try:
            from osgeo import gdal

            output_path = output_folder / output_filename

            # Light compression: this raster is consumed immediately by
            # the ZIV step, so ZLEVEL=9 (max) only wasted CPU.
            warp_kwargs = dict(
                format="GTiff",
                creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2"],
                outputType=gdal.GDT_Float32,
            )
            if target_res:
                # Merge + downsample in one streaming pass (Average is the
                # right choice for a viewshed DEM - same method as the
                # standalone _downsample_dem step it replaces here).
                warp_kwargs["xRes"] = target_res
                warp_kwargs["yRes"] = target_res
                warp_kwargs["resampleAlg"] = "average"

            gdal.Warp(
                str(output_path),
                raster_files,
                options=gdal.WarpOptions(**warp_kwargs),
            )

            if output_path.exists():
                res_note = (
                    f" (rééchantillonné à {target_res:g} m)" if target_res else ""
                )
                self.logger.info(
                    f"Successfully merged {len(raster_files)} raster "
                    f"files{res_note} to: {output_path}"
                )
                return str(output_path)
            else:
                self.logger.error("GDAL merge completed but output file not found")
                return ""

        except Exception as e:
            self.logger.error(f"Error during raster merge: {str(e)}")
            return ""

    def download_file(
        self,
        url: str,
        output_path: str,
        progress_tracker: DownloadProgressTracker,
        force_download: bool = False,
        tile_name: str = None,  # Added to fix filename issues
    ) -> Tuple[bool, str]:
        """Download file with proper cancellation, force download handling and naming."""
        output_path = Path(output_path)
        session = None

        try:
            # Vérifier l'annulation avant de commencer
            if self.feedback.isCanceled():
                return False, ""

            # Create session with retry strategy
            session = requests.Session()
            retry_strategy = Retry(
                total=3,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS"],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("http://", adapter)
            session.mount("https://", adapter)

            # --- FILENAME LOGIC START ---
            filename = None

            # 1. Use the provided tile name (Unique ID from IGN)
            if tile_name:
                filename = self._sanitize_filename(tile_name)

            # 2. Fallback: Extract from URL if name is missing
            if not filename:
                try:
                    url_path = url.split("?")[0]
                    filename = url_path.split("/")[-1]
                except Exception:
                    pass

            # 3. Last resort: Generate UUID
            if not filename or filename == "wfs" or filename == "ows":
                filename = f"tile_{uuid.uuid4().hex[:8]}"

            # 4. Handle Extensions Logic
            # Check the URL to see if it's a point cloud (.laz)
            url_lower = url.split("?")[0].lower()

            if url_lower.endswith((".laz", ".las")):
                # If URL is LiDAR, ensure filename ends in .laz
                if not filename.lower().endswith((".laz", ".las")):
                    filename += ".laz"
            else:
                # If URL is not explicitly LiDAR, and filename has no extension, assume TIF (for MNT/MNS/MNH)
                if not filename.lower().endswith(
                    (".tif", ".tiff", ".laz", ".las", ".asc")
                ):
                    filename += ".tif"
            # --- FILENAME LOGIC END ---

            output_file = output_path / filename

            # Check if file already exists and is valid
            if output_file.exists():
                if force_download:
                    self.logger.info(
                        f"Force download enabled - removing existing file: {output_file}"
                    )
                    self._forget_tile_in_manifest(output_file)
                    if not self._safe_remove_file(output_file):
                        return False, ""
                else:
                    # Audit V2: skip the costly integrity check when the
                    # tile is already listed in the manifest with matching
                    # size + mtime - a previous run validated it.
                    if self._tile_in_manifest(output_file):
                        self.logger.info(
                            f"Using cached tile (manifest hit): {output_file.name}"
                        )
                        return True, str(output_file)
                    if self._validate_file_integrity(output_file):
                        self.logger.info(f"Using existing valid file: {output_file}")
                        self._record_tile_in_manifest(output_file)
                        return True, str(output_file)

            # Vérifier l'annulation avant le téléchargement
            if self.feedback.isCanceled():
                return False, ""

            # Check disk space
            estimated_size = 100 * 1024 * 1024  # 100MB minimum estimate
            required_space_mb = (estimated_size / (1024 * 1024)) + 100
            if not self._check_disk_space(output_path, required_space_mb):
                return False, ""

            # Download with temporary file
            # NB: SSL verification is enabled now (audit fix I-4). Larger chunk
            # size (256 KiB) reduces overhead vs. the previous 8 KiB.
            # NO QCoreApplication.processEvents() here: this code runs inside
            # a ThreadPoolExecutor worker, calling processEvents() from a
            # non-Qt-main thread is undefined behaviour (audit fix C-5).
            with self._create_temp_file(output_path, "download_") as temp_file_path:
                with open(temp_file_path, "wb") as temp_file:
                    with session.get(
                        url, stream=True, timeout=(15, 60)
                    ) as response:
                        response.raise_for_status()
                        for data in response.iter_content(chunk_size=262144):
                            if self.feedback.isCanceled():
                                raise InterruptedError("Operation canceled by user")
                            if data:
                                temp_file.write(data)

                # Vérifier l'annulation avant la validation
                if self.feedback.isCanceled():
                    return False, ""

                # Validate downloaded file
                if not self._validate_file_integrity(temp_file_path):
                    return False, ""

                # Move temp file to final location
                try:
                    temp_file_path.rename(output_file)
                    self.logger.info(f"Successfully downloaded: {output_file}")
                    # Audit V2: record in manifest so the next run skips
                    # the slow integrity check on this tile.
                    self._record_tile_in_manifest(output_file)
                    return True, str(output_file)
                except Exception as e:
                    self.logger.error(
                        f"Failed to rename temp file to {output_file}: {e}"
                    )
                    return False, ""

        except InterruptedError:
            # Annulation demandée
            return False, ""
        except Exception as e:
            self.logger.error(f"Error downloading {url}: {str(e)}")
            return False, ""
        finally:
            if session:
                session.close()

    # =================================================== Tile manifest (V2)

    # Filename used to persist the download cache next to the tiles.
    TILE_MANIFEST_NAME = "_tile_manifest.json"

    def _init_tile_manifest(self, downloads_dir: Path) -> None:
        """Load the per-tile cache manifest (if any) into self._tile_manifest.

        Manifest schema (JSON):
            {
                "tile_filename.tif": {"size": 12345, "mtime": 1701000000.0},
                ...
            }

        Audit V2: when a tile is listed in the manifest AND its size+mtime
        on disk still match, we skip the (otherwise quite slow on big tiles)
        `_validate_file_integrity` call. The Force-download checkbox bypasses
        the cache entirely. Concurrent access from download workers is
        guarded by `self._tile_manifest_lock`.
        """
        try:
            import threading

            self._tile_manifest_lock = threading.Lock()
            self._tile_manifest_path = downloads_dir / self.TILE_MANIFEST_NAME
            self._tile_manifest = {}
            if self._tile_manifest_path.exists():
                try:
                    raw = self._tile_manifest_path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        self._tile_manifest = {
                            k: v
                            for k, v in data.items()
                            if isinstance(v, dict)
                            and "size" in v
                            and "mtime" in v
                        }
                    self.logger.info(
                        f"Tile manifest loaded: {len(self._tile_manifest)} "
                        "cached entries."
                    )
                except Exception as e:
                    self.logger.warning(
                        f"Tile manifest unreadable ({e}); starting fresh."
                    )
                    self._tile_manifest = {}
        except Exception as e:
            self.logger.warning(f"Could not init tile manifest: {e}")
            self._tile_manifest = {}
            self._tile_manifest_lock = None
            self._tile_manifest_path = None

    def _tile_in_manifest(self, file_path: Path) -> bool:
        """Return True if `file_path` is in the manifest AND the on-disk
        size + mtime still match. Cheap check that skips the slower
        integrity validation on subsequent runs."""
        try:
            entry = (getattr(self, "_tile_manifest", {}) or {}).get(
                file_path.name
            )
            if not entry:
                return False
            stat = file_path.stat()
            if int(entry.get("size", -1)) != stat.st_size:
                return False
            # Allow 1 s tolerance on mtime (filesystems often round).
            if abs(float(entry.get("mtime", 0.0)) - stat.st_mtime) > 1.0:
                return False
            return True
        except Exception:
            return False

    def _record_tile_in_manifest(self, file_path: Path) -> None:
        """Add or update the manifest entry for `file_path`."""
        try:
            stat = file_path.stat()
            entry = {"size": int(stat.st_size), "mtime": float(stat.st_mtime)}
            lock = getattr(self, "_tile_manifest_lock", None)
            if lock is not None:
                with lock:
                    self._tile_manifest[file_path.name] = entry
            else:
                self._tile_manifest[file_path.name] = entry
        except Exception as e:
            self.logger.warning(
                f"Could not update tile manifest for {file_path.name}: {e}"
            )

    def _forget_tile_in_manifest(self, file_path: Path) -> None:
        """Drop an entry (e.g. when force-redownload nukes the file)."""
        try:
            lock = getattr(self, "_tile_manifest_lock", None)
            if lock is not None:
                with lock:
                    self._tile_manifest.pop(file_path.name, None)
            else:
                self._tile_manifest.pop(file_path.name, None)
        except Exception:
            pass

    def _save_tile_manifest(self) -> None:
        """Persist the manifest to disk. Called once at the end of the run."""
        try:
            path = getattr(self, "_tile_manifest_path", None)
            if path is None:
                return
            lock = getattr(self, "_tile_manifest_lock", None)
            data = (
                dict(self._tile_manifest)
                if lock is None
                else (lambda: (lock.acquire(), dict(self._tile_manifest), lock.release())[1])()
            )
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.logger.info(
                f"Tile manifest saved: {len(data)} entries → {path.name}"
            )
        except Exception as e:
            self.logger.warning(f"Could not save tile manifest: {e}")

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for Windows compatibility."""
        # Remove or replace invalid Windows filename characters
        invalid_chars = '<>:"/\\|?*&'
        for char in invalid_chars:
            filename = filename.replace(char, "_")

        # Remove control characters
        filename = "".join(c for c in filename if ord(c) >= 32)

        # Limit filename length
        if len(filename) > 200:
            name, ext = os.path.splitext(filename)
            filename = name[: 200 - len(ext)] + ext

        return filename

    def _query_wfs_tiles(
        self, aoi_geometry: QgsGeometry, data_type_code: str
    ) -> List[dict]:
        """Query WFS service with strict EPSG:2154 projection."""
        try:
            self.logger.info(f"Querying WFS for data type: {data_type_code}")

            # URL du service WFS de la Géoplateforme IGN
            wfs_url = "https://data.geopf.fr/wfs/ows"

            # The AOI is always re-projected to EPSG:2154 by processAlgorithm
            # BEFORE this method is called. QgsGeometry has no sourceCrs()
            # method so the previous defensive reprojection block was dead
            # code (audit fix I-3). We just compute the bbox.
            bbox = aoi_geometry.boundingBox()

            # 3. Construct parameters with explicit coordinate reference system
            # The URN 'urn:ogc:def:crs:EPSG::2154' is crucial for WFS 2.0
            params = {
                "SERVICE": "WFS",
                "VERSION": "2.0.0",
                "REQUEST": "GetFeature",
                "TYPENAME": data_type_code,
                "OUTPUTFORMAT": "application/json",
                "SRSNAME": "EPSG:2154",  # Explicitly ask for response in Lambert-93
            }

            # BBOX format: minx,miny,maxx,maxy,CRS_URN
            params["BBOX"] = (
                f"{bbox.xMinimum()},{bbox.yMinimum()},"
                f"{bbox.xMaximum()},{bbox.yMaximum()},"
                "urn:ogc:def:crs:EPSG::2154"
            )

            self.logger.info(f"WFS Query URL: {wfs_url}")
            self.logger.info(f"BBOX: {params['BBOX']}")

            try:
                response = requests.get(
                    wfs_url, params=params, timeout=120
                )
                response.raise_for_status()

                # Parse GeoJSON response
                geojson_data = response.json()
                if "features" not in geojson_data:
                    self.logger.warning(
                        "WFS returned valid response but 0 features found."
                    )
                    return []

                tiles = []
                for feature in geojson_data["features"]:
                    if "properties" in feature:
                        properties = feature["properties"]
                        # The new platform uses 'url' and 'nom' or 'name'
                        # We check both just in case
                        url = properties.get("url")
                        name = properties.get("name", properties.get("nom"))

                        if url and name:
                            tiles.append(
                                {
                                    "url": url,
                                    "name": name,
                                    "geometry": feature.get("geometry"),
                                    "properties": properties,
                                }
                            )

                self.logger.info(f"Found {len(tiles)} tiles intersecting the envelope")
                return tiles

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 400:
                    self.logger.error(
                        f"WFS 400 Error. The server rejected the request. "
                        f"Check if layer '{data_type_code}' exists."
                    )
                else:
                    self.logger.error(f"HTTP Error during WFS request: {str(e)}")
                return []

            except requests.exceptions.RequestException as e:
                self.logger.error(f"Connection error during WFS request: {str(e)}")
                return []
            except (ValueError, KeyError) as e:
                self.logger.error(f"Error parsing WFS response: {str(e)}")
                if "response" in locals():
                    self.logger.error(f"Response snippet: {response.text[:200]}")
                return []

        except Exception as e:
            self.logger.error(f"Critical error querying WFS: {str(e)}")
            return []

    def _filter_intersecting_tiles(
        self, tiles: List[dict], aoi_geometry: QgsGeometry
    ) -> List[dict]:
        """Filter tiles that actually intersect with AOI geometry.

        Robust against both `Polygon` and `MultiPolygon` GeoJSON tile
        geometries (audit fix I-2).
        """
        try:
            intersecting_tiles = []
            for tile in tiles:
                geom_json = tile.get("geometry")
                if not geom_json:
                    continue
                try:
                    tile_geom = self._geojson_to_qgs_geometry(geom_json)
                    if tile_geom is None or tile_geom.isEmpty():
                        # If we couldn't parse, keep the tile (better safe)
                        intersecting_tiles.append(tile)
                        continue
                    if tile_geom.intersects(aoi_geometry):
                        intersecting_tiles.append(tile)
                except Exception as e:
                    self.logger.warning(f"Error processing tile geometry: {e}")
                    intersecting_tiles.append(tile)

            self.logger.info(
                f"Filtered to {len(intersecting_tiles)} intersecting tiles"
            )
            return intersecting_tiles
        except Exception as e:
            self.logger.error(f"Error filtering intersecting tiles: {str(e)}")
            return tiles  # Return all tiles if filtering fails

    @staticmethod
    def _geojson_to_qgs_geometry(geom_json: dict):
        """Best-effort GeoJSON geometry -> QgsGeometry. Handles Polygon and
        MultiPolygon (audit fix I-2)."""
        gtype = (geom_json.get("type") or "").lower()
        coords = geom_json.get("coordinates") or []
        if gtype == "polygon":
            rings = [
                [QgsPointXY(c[0], c[1]) for c in ring] for ring in coords
            ]
            return QgsGeometry.fromPolygonXY(rings)
        if gtype == "multipolygon":
            polys = [
                [[QgsPointXY(c[0], c[1]) for c in ring] for ring in poly]
                for poly in coords
            ]
            return QgsGeometry.fromMultiPolygonXY(polys)
        return None

    def _select_best_tiles(self, tiles: List[dict]) -> List[dict]:
        """Return all intersecting tiles (audit fix I-5: simplified after the
        removal of the "Use Most Coverage" strategy)."""
        if not tiles:
            self.logger.warning("No tiles provided for selection")
            return []
        if len(tiles) == 1:
            self.logger.info("Single tile found - no selection needed")
            return tiles
        self.logger.info(f"Using all {len(tiles)} intersecting tiles")
        return tiles

    def processAlgorithm(self, parameters, context, feedback):
        """Main processing algorithm with WFS integration."""
        try:
            self.feedback = feedback
            self.logger = LidarLogger(feedback)

            self.logger.info("Starting data download process...")

            # Get and validate parameters
            source = self.parameterAsSource(parameters, self.INPUT, context)
            data_type = self.parameterAsEnum(parameters, self.DATA_TYPE, context)
            force_download = self.parameterAsBool(
                parameters, self.FORCE_DOWNLOAD, context
            )
            load_layer = self.parameterAsBool(parameters, self.LOAD_LAYER, context)
            merge_tiles_flag = self.parameterAsBool(
                parameters, self.MERGE_TILES, context
            )
            create_ziv = self.parameterAsBool(parameters, self.CREATE_ZIV, context)
            ziv_radius = self.parameterAsDouble(parameters, self.ZIV_RADIUS, context)
            ziv_panel_height = self.parameterAsDouble(
                parameters, self.ZIV_PANEL_HEIGHT, context
            )
            ziv_output_param = self.parameterAsOutputLayer(
                parameters, self.ZIV_OUTPUT, context
            )

            # ---- Derive working directory (the old "Output folder") ----
            # Priority order:
            #   1) the directory of the requested ZIV output raster, when set
            #   2) a temporary directory unique to this run
            self._is_temporary_output = not bool(ziv_output_param)
            if ziv_output_param:
                output_folder = Path(ziv_output_param).parent
            else:
                import tempfile as _tmp

                output_folder = Path(_tmp.gettempdir()) / (
                    f"ZIV_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
            try:
                output_folder.mkdir(parents=True, exist_ok=True)
            except Exception as mk_err:
                self.logger.error(
                    f"Cannot create working directory {output_folder}: {mk_err}"
                )
                return {}
            self._working_dir = output_folder

            # The analysis radius is used both for buffering the AOI before WFS
            # query AND for the ZIV analysis radius - single user input.
            buffer_distance = ziv_radius

            # Merge strategy is now driven only by the checkbox (no enum exposed)
            merge_strategy = 1 if (merge_tiles_flag or create_ziv) else 0

            # No concurrency limit exposed to the user - cap to a sane value to
            # avoid creating thousands of threads on huge tile sets.
            max_downloads = 32

            # Sanity check: the embedded visibility provider should be loaded
            # at this point (it is registered alongside the main provider).
            if create_ziv:
                try:
                    reg = QgsApplication.processingRegistry()
                    if reg.algorithmById("pcfr_vis:viewshed") is None:
                        self.logger.warning(
                            "Embedded visibility provider not found - "
                            "ZIV computation may fail."
                        )
                except Exception:
                    pass

            # Store ZIV-related state for the finalize step
            self._ziv_requested = create_ziv
            self._ziv_radius = ziv_radius
            self._ziv_panel_height = ziv_panel_height
            self._ziv_output_path = ziv_output_param or ""
            # ZIV symbology choice (audit V7). Default = spotlight night.
            try:
                self._ziv_symbology = self.parameterAsEnum(
                    parameters, self.ZIV_SYMBOLOGY, context
                )
            except Exception:
                self._ziv_symbology = self.SYMB_SPOTLIGHT_NIGHT
            # Photo viewpoints (v3.13.0): generated only when the user
            # ticks the "Suggérer des points de vue photo" checkbox.
            try:
                suggest_vp = self.parameterAsBool(
                    parameters, self.SUGGEST_VIEWPOINTS, context
                )
                self._viewpoints_count = (
                    self.parameterAsInt(
                        parameters, self.NB_VIEWPOINTS, context
                    )
                    if suggest_vp else 0
                )
            except Exception:
                self._viewpoints_count = 0
            # Pass the AOI to the ZIV model AS-IS (path, layer id or layer
            # object - QGIS's processing.run knows how to resolve all of
            # these for a "vector" parameter). The previous, more elaborate
            # FeatureSourceDefinition wrapping introduced regressions on
            # some QGIS versions and was reverted in v2.1.1.
            self._ziv_zip_source = parameters.get(self.INPUT)
            # Also resolve the ZIP as a concrete layer (for the map legend
            # "ZIP / ZE - zone d'étude" entry). None if the input is not a
            # resolvable project layer.
            try:
                self._ziv_zip_layer = self.parameterAsVectorLayer(
                    parameters, self.INPUT, context
                )
            except Exception:
                self._ziv_zip_layer = None

            # Get data type code
            data_type_code = self.DATA_TYPE_CODES.get(data_type)
            if not data_type_code:
                self.logger.error(f"Invalid data type: {data_type}")
                return {}

            # Log processing parameters
            self.logger.info(f"Processing parameters:")
            self.logger.info(f"- Data type: {self.DATA_TYPE_OPTIONS[data_type]}")
            self.logger.info(f"- WFS code: {data_type_code}")
            self.logger.info(f"- Working directory: {output_folder}")
            if self._is_temporary_output:
                self.logger.info(
                    "  (no output path supplied - using a temporary directory)"
                )
            self.logger.info(f"- Force download: {force_download}")
            self.logger.info(
                f"- Merge tiles: {'yes' if merge_strategy == 1 else 'no'}"
            )
            self.logger.info(f"- Load layer after download: {load_layer}")
            self.logger.info(f"- Buffer distance: {buffer_distance} m")
            self.logger.info(f"- Merge tiles: {merge_tiles_flag}")
            self.logger.info(f"- Create ZIV: {create_ziv}")
            if create_ziv:
                self.logger.info(f"- ZIV radius: {ziv_radius} m")
                self.logger.info(f"- ZIV observer height: {ziv_panel_height} m")

            # Create directory structure
            downloads_dir = output_folder / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"Created directory: {downloads_dir}")

            # Init tile cache manifest (audit V2). Must come BEFORE the
            # download loop so _tile_in_manifest() works during downloads.
            self._init_tile_manifest(downloads_dir)

            # Check initial disk space
            if not self._check_disk_space(output_folder, self.MIN_DISK_SPACE_MB):
                return {}

            # Process AOI
            features = list(source.getFeatures())
            if not features:
                self.logger.error("No features found in input layer")
                return {}

            # Handle AOI geometry and CRS
            # Merge all features into a single geometry to support multi-feature
            # ZIPs (audit fix I-9). For a single-feature layer this is a no-op.
            if len(features) > 1:
                self.logger.info(
                    f"Multi-feature ZIP detected ({len(features)} features) - "
                    "merging into a single geometry with unaryUnion()."
                )
                aoi_geometry = QgsGeometry.unaryUnion(
                    [f.geometry() for f in features if not f.geometry().isEmpty()]
                )
            else:
                aoi_geometry = features[0].geometry()
            source_crs = source.sourceCrs()

            # Transform to Lambert-93 if needed
            if source_crs.authid() != "EPSG:2154":
                self.logger.info(
                    f"Transforming geometry from {source_crs.authid()} to EPSG:2154"
                )
                transform = QgsCoordinateTransform(
                    source_crs,
                    QgsCoordinateReferenceSystem("EPSG:2154"),
                    QgsProject.instance(),
                )
                aoi_geometry.transform(transform)

            # Sanity check: warn early if the AOI clearly sits outside the
            # French metropolitan footprint (Lambert-93 bbox approx).
            # IGN's LiDAR HD covers France + DROM-COM, but a rough metropole
            # bbox is enough to surface the most common mistake: AOI in
            # WGS84 longitude/latitude or in another country (audit fix M-15).
            METRO_BBOX = QgsRectangle(0.0, 6.0e6, 1.3e6, 7.2e6)
            try:
                if not aoi_geometry.boundingBox().intersects(METRO_BBOX):
                    self.logger.warning(
                        "L'emprise ne semble PAS recouvrir la France "
                        "métropolitaine en Lambert-93 - vérifiez le SCR "
                        "source de votre ZIP. La requête WFS pourrait "
                        "ne renvoyer aucune dalle."
                    )
            except Exception:
                pass

            # Keep the project centroid AND geometry (pre-buffer,
            # EPSG:2154) - used by the photo-viewpoints generator for
            # the distance rings and the azimuth field.
            try:
                self._aoi_centroid = aoi_geometry.centroid().asPoint()
            except Exception:
                self._aoi_centroid = None
            try:
                self._aoi_geom = QgsGeometry(aoi_geometry)
            except Exception:
                self._aoi_geom = None

            # Apply buffer around AOI (in meters, native unit of EPSG:2154)
            buffer_geom = None
            if buffer_distance and buffer_distance > 0:
                self.logger.info(
                    f"Applying {buffer_distance} m buffer around the AOI (EPSG:2154)"
                )
                buffered = aoi_geometry.buffer(buffer_distance, 64)
                if buffered is None or buffered.isEmpty():
                    self.logger.warning(
                        "Buffer operation returned empty geometry - "
                        "falling back to original AOI"
                    )
                else:
                    aoi_geometry = buffered
                    buffer_geom = QgsGeometry(buffered)
                    self.logger.info(
                        f"Buffered AOI bbox: {aoi_geometry.boundingBox().toString()}"
                    )

            # Persist the analysis buffer as a Shapefile *next to the ZIV
            # output* (so the user finds it in the same destination folder).
            # The layer itself is loaded LAST (at the very end of processing)
            # so it ends up on top of every other layer in the QGIS layer tree.
            self._buffer_shp_path = ""
            if buffer_geom is not None:
                shp_path = self._save_buffer_shapefile(
                    buffer_geom, self._working_dir, buffer_distance
                )
                if shp_path and load_layer:
                    self._buffer_shp_path = shp_path

            # Query WFS for tiles
            self.logger.info("Querying WFS service for available tiles...")
            wfs_tiles = self._query_wfs_tiles(aoi_geometry, data_type_code)

            # No usable tile: LiDAR HD covers ~80 % of mainland France.
            # The experimental "MNS artificiel" fallback (RGE ALTI +
            # sursol BDTOPO, v3.8.0-v3.12.0) was REMOVED in v3.13.0 - it
            # never ran reliably and was judged not worth hardening. The
            # plugin now simply reports the lack of coverage.
            no_tiles = not wfs_tiles
            if wfs_tiles:
                intersecting_tiles_preview = self._filter_intersecting_tiles(
                    wfs_tiles, aoi_geometry
                )
                if not intersecting_tiles_preview:
                    no_tiles = True
                else:
                    intersecting_tiles = intersecting_tiles_preview
            if no_tiles:
                self.logger.warning(
                    "Aucune dalle LiDAR HD sur l'AOI - zone non couverte "
                    "par le LiDAR HD IGN (couverture ~80 % de la métropole)."
                )
                try:
                    from qgis.utils import iface as _iface

                    if _iface is not None:
                        _iface.messageBar().pushMessage(
                            PLUGIN_DISPLAY_NAME,
                            "Aucune dalle LiDAR HD ne couvre cette zone - "
                            "impossible de calculer la ZIV ici.",
                            level=Qgis.Warning,
                            duration=10,
                        )
                except Exception:
                    pass
                return {"OUTPUT_DIRECTORY": str(downloads_dir), "OUTPUT_FILES": ""}

            # Warn (don't block) when the user is about to download a lot of tiles
            if len(intersecting_tiles) > self.MAX_TILES_RECOMMENDED:
                self.logger.warning(
                    f"Found {len(intersecting_tiles)} tiles - this exceeds "
                    f"the recommended limit of {self.MAX_TILES_RECOMMENDED}. "
                    "Consider splitting your AOI into smaller chunks."
                )

            # All intersecting tiles are downloaded (no "best tile" mode)
            selected_tiles = self._select_best_tiles(intersecting_tiles)

            # No user-facing concurrency limit - one worker per tile, capped
            effective_workers = max(1, min(len(selected_tiles), max_downloads))
            self.logger.info(
                f"Using {effective_workers} parallel download workers "
                f"for {len(selected_tiles)} tiles"
            )

            # Global disk-space estimate (audit fix M-12). Each LiDAR HD tile
            # weighs ~500 MB max, MNS/MNT tiles ~5-20 MB. Estimate optimistic.
            # We multiply by tile count instead of per-worker count.
            per_tile_mb = 500 if data_type == 3 else 30
            required_mb = per_tile_mb * len(selected_tiles) + 200
            if not self._check_disk_space(downloads_dir, required_mb):
                self.logger.error(
                    f"Estimated download size = {required_mb} MB - "
                    "not enough free space in the working directory."
                )
                return {}

            # Initialize progress tracker
            progress_tracker = DownloadProgressTracker(feedback)
            progress_tracker.set_total_files(len(selected_tiles))

            # Download selected tiles with proper resource management and cancellation
            total_files = len(selected_tiles)
            self.logger.info(f"Starting download of {total_files} tiles...")

            downloaded_files = []
            try:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=effective_workers
                ) as executor:
                    futures = [
                        executor.submit(
                            self.download_file,
                            tile["url"],
                            str(downloads_dir),
                            progress_tracker,
                            force_download,
                            tile["name"],  # Pass the unique name here
                        )
                        for tile in selected_tiles
                    ]

                    for future in concurrent.futures.as_completed(futures):

                        if self.feedback.isCanceled():
                            self.logger.info(
                                "Cancellation requested - stopping all downloads..."
                            )

                            cancelled_count = 0
                            for f in futures:
                                if not f.done():
                                    if f.cancel():
                                        cancelled_count += 1

                            self.logger.info(
                                f"Cancelled {cancelled_count} pending downloads"
                            )

                            # Arrêter l'executor et sortir de la boucle
                            executor.shutdown(wait=False)
                            break

                        try:
                            success, file_path = future.result()
                            if success and file_path:
                                downloaded_files.append(file_path)

                            # Mark file as completed regardless of success/failure
                            progress_tracker.mark_file_completed()
                            self.logger.info(
                                f"({progress_tracker.completed_files}/{progress_tracker.total_files})"
                            )

                        except Exception as e:
                            self.logger.error(
                                f"Error processing download result: {str(e)}"
                            )
                            progress_tracker.mark_file_completed()
                            continue

                # Log final status
                if self.feedback.isCanceled():
                    self.logger.info(
                        f"Download cancelled by user. Downloaded {len(downloaded_files)} files before cancellation."
                    )
                else:
                    self.logger.info(
                        f"Download completed: ({progress_tracker.completed_files}/{progress_tracker.total_files})"
                    )

            except Exception as e:
                self.logger.error(f"Error during concurrent downloads: {str(e)}")
            finally:
                # Clean up any remaining temp files
                self._cleanup_temp_files()

            if not downloaded_files:
                self.logger.warning("No files were successfully downloaded")
                return {"OUTPUT_DIRECTORY": str(downloads_dir), "OUTPUT_FILES": ""}

            # Process output based on strategy
            if merge_strategy == 0:  # Download All (No Merge)
                self.logger.info(
                    f"Strategy: Download All - Returning {len(downloaded_files)} files"
                )
                # Load layers if requested
                if load_layer:
                    if data_type == 3:  # LIDAR - use point cloud loader
                        for file_path in downloaded_files:
                            self.load_point_cloud_layer(file_path)
                    else:  # MNT, MNS, MNH - use raster loader
                        for file_path in downloaded_files:
                            self.load_raster_layer(file_path, data_type)
                return self._finalize_result({
                    "OUTPUT_DIRECTORY": str(downloads_dir),
                    "OUTPUT_FILE": downloaded_files[0] if downloaded_files else "",
                    "OUTPUT_FILES": ";".join(downloaded_files),
                })

            elif (
                merge_strategy == 1 and len(downloaded_files) > 1
            ):  # Merge All Intersecting
                self.logger.info(
                    f"Strategy: Merge All - Merging {len(downloaded_files)} files"
                )

                # Unique filename per run avoids the Windows file-lock issue
                # when QGIS still references a previous merged_output (audit
                # fix I-7).
                merge_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

                if data_type == 3:  # LIDAR point clouds
                    merged_output = str(
                        downloads_dir / f"merged_output_{merge_stamp}.laz"
                    )
                    try:
                        result = processing.run(
                            "pdal:merge",
                            {
                                "LAYERS": [
                                    f"copc://{path}" for path in downloaded_files
                                ],
                                "FILTER_EXPRESSION": "",
                                "FILTER_EXTENT": None,
                                "OUTPUT": merged_output,
                            },
                            feedback=self.feedback,
                        )

                        if result and "OUTPUT" in result:
                            self.logger.info(
                                f"Successfully merged files to: {result['OUTPUT']}"
                            )
                            self.logger.warning(
                                "Note: Auto-loading is disabled for merged files. "
                                "To visualize, manually drag and drop the file into QGIS."
                            )
                            return self._finalize_result({
                                "OUTPUT_DIRECTORY": str(downloads_dir),
                                "OUTPUT_FILE": result["OUTPUT"],
                            })
                        else:
                            self.logger.warning(
                                "Merge operation failed - using first file as fallback"
                            )
                            return self._finalize_result({
                                "OUTPUT_DIRECTORY": str(downloads_dir),
                                "OUTPUT_FILE": downloaded_files[0],
                            })

                    except Exception as e:
                        self.logger.error(f"Error during merge operation: {str(e)}")
                        self.logger.info("Using first downloaded file as fallback")
                        return self._finalize_result({
                            "OUTPUT_DIRECTORY": str(downloads_dir),
                            "OUTPUT_FILE": downloaded_files[0],
                        })

                else:
                    # For raster data (MNT, MNS, MNH), use GDAL merge.
                    # Merge AND resample to ZIV_TARGET_RES_M in one pass so
                    # the viewshed runs faster on raw 0.5/1 m LiDAR HD data
                    # without materialising / re-reading the heavy full-res
                    # mosaic (see merge_rasters_gdal docstring).
                    self.logger.info(f"Merging {len(downloaded_files)} rasters")
                    merged_output = self.merge_rasters_gdal(
                        downloaded_files,
                        downloads_dir,
                        f"merged_output_{merge_stamp}.tif",
                        target_res=self.ZIV_TARGET_RES_M,
                    )

                    if merged_output:
                        # The merged DEM is intentionally NOT auto-loaded:
                        # it is grey, visually noisy and would hide the
                        # basemap. It remains available on disk for the ZIV
                        # step. It is already at
                        # ZIV_TARGET_RES_M, so no separate downsample.
                        final_dem = merged_output
                        return self._finalize_result({
                            "OUTPUT_DIRECTORY": str(downloads_dir),
                            "OUTPUT_FILE": final_dem,
                            "OUTPUT_FILES": ";".join(downloaded_files),
                        })
                    else:
                        self.logger.warning(
                            "Raster merge operation failed - using first file as fallback"
                        )
                        final_dem = self._downsample_dem(
                            downloaded_files[0], downloads_dir
                        )
                        return self._finalize_result({
                            "OUTPUT_DIRECTORY": str(downloads_dir),
                            "OUTPUT_FILE": final_dem,
                            "OUTPUT_FILES": ";".join(downloaded_files),
                        })

            # Single-file or fallback case (typically: merge requested but
            # only one tile intersects the AOI -> nothing to merge, the lone
            # tile IS our DEM). Logged explicitly (audit fix M-11).
            if downloaded_files:
                self.logger.info(
                    f"Single intersecting tile detected - using it directly "
                    f"as the DEM: {downloaded_files[0]}"
                )
            # Same downsample step as in the merged case
            single_dem = downloaded_files[0] if downloaded_files else ""
            if single_dem:
                single_dem = self._downsample_dem(
                    single_dem, downloads_dir
                )
            return self._finalize_result({
                "OUTPUT_DIRECTORY": str(downloads_dir),
                "OUTPUT_FILE": single_dem,
                "OUTPUT_FILES": ";".join(downloaded_files),
            })

        except Exception as e:
            self.logger.error(f"Error in main processing: {str(e)}")
            import traceback

            tb = traceback.format_exc()
            self.logger.error(tb)
            # Persistent popup - won't disappear with the Processing dialog
            self._show_persistent_error(
                "Erreur pendant le traitement",
                f"{e}\n\n--- Détail technique ---\n{tb}",
            )
            return {}
        finally:
            # Final cleanup
            self._cleanup_temp_files()
