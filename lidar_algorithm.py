"""
ZIV_prime - IGN LiDAR HD import and ZIV Processing algorithm.

Part of ZIV_prime, a GNU GPL v3 (or later) licensed QGIS plugin by G. DELSIGNE.

The tile import (discovery, download, mosaicking) is this plugin's own code, in
``import_dalles/``; so are the visibility engine and the two Processing
algorithms, in ``moteur_visibilite/`` and ``algorithmes_ziv/`` - no third-party
code remains in this package.

Carried over UNCHANGED from ZIV_prime 1.5.3. The United States chain is added
in the next version, as a parallel branch selected by the area of interest; the
France path below is not meant to be refactored on the way.

See ``LICENSE`` and ``README.md`` at the root of the plugin for the full terms.
"""
import concurrent.futures
import contextlib
import glob
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
    QgsProcessingException,
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
    QgsProcessingParameterVectorDestination,
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
from requests.adapters import HTTPAdapter, Retry

from .import_dalles import adaptateur as import_dalles


# Nom affiche a l'utilisateur (journal, fenetres, titre de carte).
PLUGIN_DISPLAY_NAME = "Zones d'influence visuelle (ZIV)"

# Hauteur de l'observateur pour la ZIV : intitulé et valeur par défaut.
# Ce sont des constantes de module, et non des littéraux dans initAlgorithm,
# pour qu'un greffon hôte puisse les adapter à son usage sans toucher à ce
# fichier — par exemple remplacer le libellé par « Hauteur des panneaux en
# (m) » et la valeur par défaut par 4,3 m.
ZIV_HAUTEUR_LIBELLE = 'Viewshed: observer height (metres)'
ZIV_HAUTEUR_DEFAUT = 1.7

# Ce que disent les valeurs de la ZIV, une seule definition pour les trois
# legendes. « Hors visibilite » melait trois etats sous un meme mot : non vu,
# masque sous foret ou bati, et jamais calcule. Le dernier porte desormais le
# sans-donnee (`SANS_DONNEE_ZIV`, pose hors du rayon d'analyse).
NON_VU = "Non vu (forêt et bâti compris)"
EMPRISE = "Emprise (0.1)"
SANS_DONNEE_ZIV = -9999.0


# Memoire du calcul de visibilite, par cellule de la maille de travail.
# Mesure du 2026-10-02 (tracemalloc, rayons de 2 et 4 km, loi en R2 tenue) :
# 8 octets pour le terrain et le cumul, 22,9 au pic d'un point d'observation.
# A 2 m : 4,5 Go a 12 km, 12 Go a 20 km — et 77 Go aux 50 km que le
# formulaire accepte. On refuse un rayon qui depasse ce que le poste peut
# donner, plutot qu'une limite fixe qui brimerait les machines bien dotees.
OCTETS_PAR_CELLULE = 31.0
PART_MEMOIRE_ADMISE = 0.75


def _memoire_physique():
    """Memoire vive totale du poste, en octets ; None si on ne sait pas la lire."""
    try:
        if os.name == "nt":
            import ctypes

            class _Etat(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            etat = _Etat()
            etat.dwLength = ctypes.sizeof(_Etat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(etat)):
                return int(etat.ullTotalPhys)
            return None
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return None


def _version_du_greffon():
    """La version publiee, lue dans metadata.txt : une seule source de verite.

    `RawConfigParser`, sans interpolation : un « % » du changelog ne doit pas
    faire perdre la version (lessons.md, 2026-09-03).
    """
    import configparser

    lecteur = configparser.RawConfigParser()
    try:
        lecteur.read(str(Path(__file__).with_name("metadata.txt")), encoding="utf-8")
        return lecteur.get("general", "version")
    except Exception:
        return "?"


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
        # Tout avertissement ou erreur de l'execution, dans l'ordre : c'est le
        # registre des degradations que la fiche de provenance reporte. Un
        # repli qui ne se dit qu'au fil du journal se perd des la fermeture
        # de la fenetre.
        self.avertissements = []
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
                with contextlib.suppress(Exception):
                    olds = sorted(
                        log_dir.glob("genziv_*.log"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    for old in olds[30:]:
                        with contextlib.suppress(Exception):
                            old.unlink()

    def info(self, message: str):
        """Log info message"""
        self.feedback.pushInfo(message)
        QgsMessageLog.logMessage(message, PLUGIN_DISPLAY_NAME, Qgis.MessageLevel.Info)
        self._write_to_file("INFO", message)

    def error(self, message: str):
        """Log error message"""
        self.avertissements.append("ERREUR : %s" % message)
        self.feedback.reportError(message)
        QgsMessageLog.logMessage(message, PLUGIN_DISPLAY_NAME, Qgis.MessageLevel.Critical)
        self._write_to_file("ERROR", message)

    def warning(self, message: str):
        """Log warning message"""
        self.avertissements.append(message)
        self.feedback.pushWarning(message)
        QgsMessageLog.logMessage(message, PLUGIN_DISPLAY_NAME, Qgis.MessageLevel.Warning)
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
    PAYS = "PAYS"
    DATA_TYPE = "DATA_TYPE"
    LOAD_LAYER = "LOAD_LAYER"
    MERGE_TILES = "MERGE_TILES"
    COURBES_NIVEAU = "COURBES_NIVEAU"
    ZIV_RADIUS = "ZIV_RADIUS"
    ZIV_PANEL_HEIGHT = "ZIV_PANEL_HEIGHT"
    ZIV_OUTPUT = "ZIV_OUTPUT"
    RADIUS_OUTPUT = "RADIUS_OUTPUT"
    ZIV_SYMBOLOGY = "ZIV_SYMBOLOGY"
    # v3.11.0/v3.13.0: photo viewpoints - opt-in checkbox + number of
    # camera locations generated on the most visible sectors of the ZIV.
    SUGGEST_VIEWPOINTS = "SUGGEST_VIEWPOINTS"
    NB_VIEWPOINTS = "NB_VIEWPOINTS"

    # ZIV symbology options (cf. audit V7)
    ZIV_SYMBOLOGY_OPTIONS = [
        'Night spotlight (default) — hidden in 80% black, visible transparent',
        'Warm gradient — white → yellow → orange → red (v1.3.x style)',
        'Translucent red — visible in 30% red, hidden transparent',
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
    # Maille de travail de la ZIV, la meme pour tout le monde : le choix
    # offert au formulaire a ete retire le 2026-09-16 (une question de plus a
    # l'ouverture, pour un reglage que personne n'a a arbitrer). Tout le code
    # en aval lit `self.ZIV_TARGET_RES_M`.
    # Ce que coute cette valeur unique, mesure le 2026-09-10 sur une plaine de
    # l'Illinois : a 2 m la ZIV suit le micro-relief agricole (sigma 20 cm) et
    # rend 53 000 m de lisiere par km2, soit des plages de 24 m — illisible ;
    # a 10 m, 8 200 m de lisiere, des plages de 170 m et un raster 25 fois
    # plus leger. C'est en terrain PLAT que la finesse se paie.
    ZIV_TARGET_RES_M = 2.0
    # Le terrain d'ou l'on tire les COURBES n'a pas a etre aussi fin que la
    # ZIV : au metre il porte encore le micro-relief agricole, et le trace se
    # fragmente. Mesure du 2026-09-14 sur une emprise francaise, equidistance
    # imposee de 5 m : le MNS rend 3 433 troncons, le MNT a 2 m encore 4 744
    # avant ecartement des coquilles. Une courbe topographique se lit a la
    # dizaine de metres — c'est aussi l'echelle des cartes au 1:25 000.
    RESOLUTION_COURBES = 10.0

    # Pays et source de donnees. « Automatique » deduit le pays du centroide de
    # l'emprise, reprojete en WGS84 : les deux couvertures ne se recoupent pas,
    # donc la detection est sans ambiguite. Les deux autres valeurs IMPOSENT le
    # fournisseur — utile pour qu'un modele Processing enregistre porte le pays
    # noir sur blanc, ce qui se defend mieux dans une etude d'impact qu'un
    # modele qui commute selon la geometrie.
    PAYS_OPTIONS = [
        'Automatic (from the area of interest)',
        "France — IGN LiDAR HD",
        'United States (CONUS) — USGS 3DEP',
    ]
    # Meme ordre que PAYS_OPTIONS. None signifie « laisser la detection faire ».
    PAYS_IDS = [None, "france_ign", "usa_3dep"]

    # Options for data types
    DATA_TYPE_OPTIONS = [
        'DTM — Digital Terrain Model (bare earth)',
        'DSM — Digital Surface Model (vegetation and buildings included)',
        'DHM — Digital Height Model (objects above ground)',
        'LiDAR — classified point cloud',
    ]

    # Mapping to WFS codes (Updated based on user input and IGN Géoplateforme)
    # Ce ne sont plus des noms de couches WFS mais les PROPRIETES qui portent
    # l'URL de telechargement. L'IGN a retire ses quatre couches de dalles lors
    # de la migration vers cartes.gouv.fr et les a fusionnees en une seule,
    # `IGNF_LIDAR-HD_METADONNEE:metadata`, qui expose les quatre URL en
    # attributs. Constate le 2026-09-09 : l'ancien nom fait repondre
    # « Unknown namespace [IGNF_MNS-LIDAR-HD] ».
    DATA_TYPE_CODES = {
        0: "url_mnt",  # MNT
        1: "url_mns",  # MNS
        2: "url_mnh",  # MNH
        3: "url_npl",  # LIDAR, nuage de points brut
    }

    def __init__(self):
        super().__init__()
        self.logger = None
        self._temp_files = set()  # Track temporary files for cleanup
        # Le terrain mis de cote avant la gravure de la surface occultante,
        # d'ou se tirent les courbes de niveau. Remis a vide ici et pas
        # seulement par `getattr` : si une instance servait deux fois, des
        # courbes francaises se tireraient du terrain americain precedent.
        self._terrain_nu = ""
        # Vrai quand le fichier de travail est DEJA un terrain : il n'y a alors
        # ni terrain nu a part, ni raison d'avertir.
        self._courbes_sur_le_terrain = False

    def tr(self, string):
        """Returns a translatable string with the self.tr() function."""
        return QCoreApplication.translate("Processing", string)

    def createCustomParametersWidget(self, parent):
        """Le formulaire, habille de deux boutons FR / EN.

        QGIS appelle cette methode avant de construire son propre formulaire —
        c'est le point d'accroche officiel, celui qu'emploient les algorithmes
        GDAL. Rendre None fait retomber QGIS sur son formulaire habituel : le
        greffon perd les boutons, jamais l'algorithme.
        """
        try:
            from .dialogue_langue import construire

            return construire(self, parent)
        except Exception:
            return None

    def createInstance(self):
        return LidarDownloaderAlgorithm()

    # ---- Choix du pays ---------------------------------------------------

    def _choisir_fournisseur(self, aoi_geometry, source_crs):
        """Choisit le fournisseur de donnees d'apres l'emprise, et le memorise.

        A appeler UNE fois, dans `processAlgorithm`, avant toute reprojection :
        c'est le seul instant ou l'emprise est encore dans son systeme d'origine.
        Trois valeurs sont posees sur l'instance :

          * `_fournisseur`     — l'objet fournisseur, interroge en aval ;
          * `_epsg_travail`    — le SCR projete metrique de tout le calcul ;
          * `_aoi_bbox_wgs84`  — l'emprise en degres, que `resolution_native`
            est seul a savoir consommer et qui n'est plus reconstituable une
            fois la geometrie reprojetee.

        Rien n'est pose au niveau de la classe ni dans `initAlgorithm` :
        `createInstance()` rend une instance neuve a chaque execution, et un
        etat de pays partage entre executions serait imprevisible.
        """
        from . import fournisseurs

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        # Sur une COPIE : `transform()` mute la geometrie en place, et l'emprise
        # d'origine est encore necessaire a tout ce qui suit.
        en_degres = QgsGeometry(aoi_geometry)
        if source_crs.authid() != wgs84.authid():
            en_degres.transform(QgsCoordinateTransform(
                source_crs, wgs84, QgsProject.instance()))
        boite = en_degres.boundingBox()
        centre = en_degres.centroid().asPoint()

        # `processAlgorithm` pose le journal avant d'arriver ici, mais cette
        # methode doit rester appelable sans lui : un appel de journal ne casse
        # jamais un calcul dans ce greffon.
        journal = self.logger
        impose = getattr(self, "_pays_impose", None)
        self._fournisseur = fournisseurs.choisir(
            centre.x(), centre.y(), force=impose, journal=journal)
        self._epsg_travail = int(self._fournisseur.crs_travail(
            centre.x(), centre.y()))
        self._aoi_bbox_wgs84 = (boite.xMinimum(), boite.yMinimum(),
                                boite.xMaximum(), boite.yMaximum())
        if journal is not None:
            journal.info(
                "Systeme de travail : EPSG:%d (%s)."
                % (self._epsg_travail, self._fournisseur.NOM))
        return self._fournisseur

    def name(self):
        return "generate"

    def displayName(self):
        return self.tr('Visual influence zone (ZIV)')

    def shortHelpString(self):
        return self.tr(
            '\nZIV_prime\n\nDownloads the national LiDAR elevation tiles that intersect your project area\nand derives the visual influence zone (viewshed) from them.\n\nCountries:\n- France: IGN LiDAR HD (DTM / DSM / DHM / raw point cloud).\n- Contiguous United States: USGS 3DEP for the terrain, LANDFIRE for vegetation\n  height, USA Structures for buildings, TIGER/Line for roads.\n- The country is deduced from the area of interest; the "Country" field forces\n  it, which a saved model should do so that it states its source plainly.\n\nData types:\n- DTM: bare earth.\n- DSM: vegetation and buildings included.\n- DHM: height of the objects above the ground.\n- LiDAR: raw classified point cloud.\n\nAnalysis radius (m):\n- One value, used both as the buffer around the area of interest for the\n  download AND as the analysis radius of the viewshed.\n- The buffer is written as a layer and drawn with a red dashed outline.\n- "Analysis radius output" says where to write it; left empty it follows the\n  working folder, which is temporary when the viewshed output is.\n\nCreate the viewshed:\n- Runs the embedded "Creation ZIV" processing model over the merged terrain.\n  The visibility algorithms ship with the plugin: no companion plugin needed.\n- The output raster is styled as a night effect: hidden areas (0 and NoData)\n  are painted 80% black, while the project footprint (0.1) and the genuinely\n  visible cells (1 and above) stay transparent over the basemap.\n- Masking: cells falling under woodland or on a building are forced out of\n  visibility, because nobody stands on a canopy or a roof. France draws those\n  footprints from BD Foret and BDTOPO, the United States from LANDFIRE and USA\n  Structures. If a service is unavailable the viewshed comes out unmasked\n  rather than not at all.\n\nWhere no 1-metre LiDAR covers the area, the terrain is served at about 10 m and\nthe working resolution is raised to match, rather than interpolated finer than\nthe data really is. The log says so.\n'
        )

    def flags(self):
        """Cet algorithme doit tourner dans le FIL PRINCIPAL.

        Il ne se contente pas de produire des fichiers : il cree des couches,
        les habille et les ajoute au projet. Or un objet Qt cree dans un fil
        de travail n'appartient pas a l'interface qui va l'afficher — les
        couches se dessinent une fois, puis disparaissent au premier
        rafraichissement du canevas. C'est ce qu'un utilisateur a rapporte le
        2026-09-14 : « quand je deplace le fond de carte, les couches
        nouvellement creees disparaissent ».

        Le prix est connu et assume : l'interface se fige pendant le calcul,
        faute de fil separe. Des couches stables valent mieux qu'une barre de
        progression fluide au bout de laquelle il ne reste rien.

        L'enum a change de maison entre Qt5 et Qt6 ; les deux formes existent
        sur les deux cibles (sonde du 2026-09-14), la moderne d'abord.
        """
        base = super().flags()
        try:
            from qgis.core import Qgis

            return base | Qgis.ProcessingAlgorithmFlag.NoThreading
        except Exception:
            drapeau = getattr(QgsProcessingAlgorithm, "FlagNoThreading", None)
            return base | drapeau if drapeau is not None else base

    def checkParameterValues(self, parameters, context):
        """Le rayon d'analyse est obligatoire, bien que declare optionnel.

        Le champ part vide pour que personne ne lance une etude sur un defaut
        qu'il n'a pas choisi, et seul un parametre optionnel s'affiche vide
        (mesure du 2026-09-16 : obligatoire et sans defaut, le widget retombe
        sur son minimum et propose « 1,000000 »). L'obligation se porte donc
        ici : QGIS affiche le message dans le formulaire et ne lance rien.
        """
        valeur = parameters.get(self.ZIV_RADIUS)
        if valeur is None or str(valeur).strip() in ("", "NULL"):
            return False, self.tr(
                "Enter an analysis radius, in metres: it sets both the area "
                "of terrain downloaded and the extent of the viewshed."
            )
        # Un rayon que le poste ne peut pas calculer : le refuser ici, avant
        # tout telechargement, plutot qu'un plantage par manque de memoire.
        # Borne basse : la fenetre d'un point, sans l'emprise elle-meme.
        rayon = self.parameterAsDouble(parameters, self.ZIV_RADIUS, context)
        besoin = (2.0 * rayon / self.ZIV_TARGET_RES_M) ** 2 * OCTETS_PAR_CELLULE
        totale = _memoire_physique()
        if totale and besoin > PART_MEMOIRE_ADMISE * totale:
            return False, self.tr(
                "At a radius of %.0f m the viewshed needs about %.1f GB of "
                "memory, more than this computer can give it (%.1f GB in "
                "total). Reduce the radius."
            ) % (rayon, besoin / 2**30, totale / 2**30)
        # Deux combinaisons qui ne donnaient PAS de ZIV juste, sans le dire :
        # un nuage de points ne se calcule pas (des centaines de Mo pour rien),
        # et sans fusion la ZIV ne portait que sur la premiere dalle.
        if self.parameterAsEnum(parameters, self.DATA_TYPE, context) == 3:
            return False, self.tr(
                "A point cloud cannot be used to compute a viewshed: choose "
                "the DSM (recommended), the DTM or the DHM."
            )
        if not self.parameterAsBool(parameters, self.MERGE_TILES, context):
            return False, self.tr(
                "Keep the tiles merged: the viewshed is computed on a single "
                "terrain, and without merging only the first tile would be used."
            )
        return super().checkParameterValues(parameters, context)

    def initAlgorithm(self, config=None):
        """Initialize the algorithm parameters."""
        # --- 1) Projet & emprise & rayon -----------------------------------
        # ZIP selection (input project polygon)
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Project area (AOI)'),
                [QgsProcessing.SourceType.TypeVectorAnyGeometry],
            )
        )

        # Pays et source de donnees. Place juste apres la ZIP parce que le pays
        # en est DEDUIT : le combo n'est qu'une surcharge, et son defaut
        # « Automatique » convient a tout le monde. Il reste visible plutot que
        # range dans les parametres avances, pour qu'on voie d'un coup d'oeil
        # que le greffon ne couvre pas que la France.
        self.addParameter(
            QgsProcessingParameterEnum(
                self.PAYS,
                self.tr('Country / data source'),
                options=[self.tr(o) for o in self.PAYS_OPTIONS],
                defaultValue=0,
            )
        )

        # Analysis radius - used BOTH for tile download buffer AND ZIV.
        # Placed next to the ZIP because it drives the WFS query upstream.
        # Le champ part VIDE : un rayon depend du projet, et un defaut de
        # 300 m se lancait tel quel sans qu'on y pense. « Optionnel » est ici
        # une mecanique d'affichage, pas une permission — mesure du
        # 2026-09-16 : un parametre obligatoire sans defaut retombe sur son
        # minimum et afficherait « 1,000000 ». L'obligation est portee par
        # `checkParameterValues`.
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ZIV_RADIUS,
                self.tr(
                    'Analysis radius (metres) - used both for the download buffer and the viewshed'
                ),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=None,
                minValue=1.0,
                maxValue=50000.0,
                optional=True,
            )
        )

        # --- 2) Téléchargement IGN -----------------------------------------
        # Data type selection - MNS by default (Digital Surface Model, includes
        # vegetation + buildings → correct height reference for visibility studies)
        self.addParameter(
            QgsProcessingParameterEnum(
                self.DATA_TYPE,
                self.tr('Data type to download'),
                options=[self.tr(o) for o in self.DATA_TYPE_OPTIONS],
                defaultValue=1,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MERGE_TILES,
                self.tr('Merge the downloaded tiles (DTM/DSM/DHM)'),
                defaultValue=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.LOAD_LAYER,
                # Le libelle dit ce que la case fait : les dalles ne sont jamais
                # chargees, la ZIV et ses couches le sont toujours ; seule la
                # couche du rayon en depend (audit du 2026-10-02).
                self.tr('Load the analysis radius layer into the project'),
                defaultValue=True,
            )
        )

        # --- 3) ZIV ---------------------------------------------------------
        # Les courbes de niveau prennent la place de l'ancienne case « creer la
        # ZIV » : celle-ci decrivait le principe meme du greffon. Croisees avec
        # la ZIV, elles disent POURQUOI une zone voit ou ne voit pas.
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.COURBES_NIVEAU,
                self.tr("Also produce the contour lines of the terrain"),
                defaultValue=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.ZIV_PANEL_HEIGHT,
                self.tr(ZIV_HAUTEUR_LIBELLE),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=ZIV_HAUTEUR_DEFAUT,
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
                self.tr('Viewshed: symbology'),
                options=[self.tr(o) for o in self.ZIV_SYMBOLOGY_OPTIONS],
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
                    'Suggest photo viewpoints (photomontages) on the most visible sectors'
                ),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.NB_VIEWPOINTS,
                self.tr(
                    'Photo viewpoints: how many to generate (spread near / medium / far)'
                ),
                type=QgsProcessingParameterNumber.Type.Integer,
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
                self.tr('Viewshed output (raster) - leave empty for a temporary file'),
                optional=True,
                createByDefault=False,
            )
        )

        # Rayon d'analyse - couche vecteur du tampon. Sans chemin explicite le
        # fichier suit le dossier de travail, qui est un dossier temporaire du
        # systeme quand la ZIV elle-meme est temporaire : l'utilisateur ne le
        # retrouvait pas. Ce parametre lui laisse le choix de la destination.
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.RADIUS_OUTPUT,
                self.tr(
                    'Analysis radius output (vector) - leave empty for the working folder'
                ),
                type=QgsProcessing.SourceType.TypeVectorPolygon,
                optional=True,
                createByDefault=False,
            )
        )

        # Add output definitions
        self.addOutput(
            QgsProcessingOutputFolder(
                "OUTPUT_DIRECTORY", self.tr('Download directory')
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

        # Les courbes de niveau : declarees pour de bon. Une cle ajoutee au
        # dictionnaire de resultat sans `addOutput` n'existe pas pour
        # Processing, qui n'enumere que `outputDefinitions()` — le fichier
        # etait produit mais invisible au panneau de resultats et inchaînable
        # dans un modele.
        self.addOutput(
            QgsProcessingOutputFile(
                "COURBES_NIVEAU", self.tr("Contour lines")
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
        self,
        geom: QgsGeometry,
        output_dir: Path,
        radius: float,
        chemin_demande: str = "",
    ) -> str:
        """Save the analysis buffer geometry as a Shapefile in the working CRS.

        Uses a unique timestamped filename so successive runs never collide
        with a shapefile still locked by QGIS from a previous execution
        (this is the most common cause of "shapefile not generated" on
        Windows: GDAL cannot overwrite a file currently locked by an open
        layer in the project).

        ``chemin_demande`` (parametre RADIUS_OUTPUT) court-circuite ce nom
        horodate : l'utilisateur choisit sa destination, donc il choisit
        d'ecraser. Le pilote OGR est deduit de l'extension qu'il a saisie.
        Si l'ecriture echoue, c'est presque toujours ce verrou Windows qu'il
        faut nommer au journal, sinon le message reste incomprehensible.

        Returns the output path on success, '' otherwise.
        """
        writer = None
        output_path = None
        # Un chemin explicite ecrase le fichier existant, ce que GDAL refuse
        # sous Windows tant que la couche reste ouverte dans QGIS.
        indice_verrou = (
            "Rayon d'analyse : fichier probablement verrouille par une couche "
            "encore ouverte dans QGIS. La retirer du projet, ou laisser la "
            "sortie vide pour obtenir un nom horodate."
        )
        try:
            # Le defaut a 2154 n'est pas decoratif : cette methode est
            # appelable par un chemin qui n'a pas traverse processAlgorithm, et
            # doit alors se comporter comme la chaine francaise d'origine.
            crs = QgsCoordinateReferenceSystem(
                "EPSG:%d" % getattr(self, "_epsg_travail", 2154))
            fields = QgsFields()
            fields.append(QgsField("name", QVariant.String, len=50))
            fields.append(QgsField("radius_m", QVariant.Double))

            if chemin_demande:
                output_path = Path(chemin_demande)
                pilote = QgsVectorFileWriter.driverForExtension(output_path.suffix)
                if not pilote:
                    pilote = "ESRI Shapefile"
                    self.logger.warning(
                        "Extension '%s' non reconnue pour le rayon d'analyse : "
                        "ecriture en ESRI Shapefile vers %s."
                        % (output_path.suffix, output_path.with_suffix(".shp"))
                    )
                    output_path = output_path.with_suffix(".shp")
                with contextlib.suppress(Exception):
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                # Un chemin explicite ecrase le fichier du tir precedent. Si la
                # couche de ce tir est encore chargee, GDAL detruit d'abord les
                # fichiers annexes du shapefile (.shx, .prj, .cpg) PUIS echoue
                # sur le verrou Windows : l'utilisateur perdait le fichier qu'il
                # avait deja obtenu, et n'obtenait pas le nouveau. Le nom
                # horodate de la branche « sans chemin » evite ce cas par
                # construction ; ici il faut liberer le verrou nous-memes.
                self._liberer_couche_du_projet(output_path)
            else:
                # Unique filename for this run -> no collision with a still-open
                # layer from a previous execution.
                pilote = "ESRI Shapefile"
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = (
                    output_dir / f"rayon_analyse_{int(round(radius))}m_{timestamp}.shp"
                )

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = pilote
            options.fileEncoding = "UTF-8"

            writer = QgsVectorFileWriter.create(
                str(output_path),
                fields,
                QgsWkbTypes.Type.Polygon,
                crs,
                QgsCoordinateTransformContext(),
                options,
            )
            if writer is None:
                self.logger.error(
                    f"QgsVectorFileWriter returned None for: {output_path}"
                )
                if chemin_demande:
                    self.logger.error(indice_verrou)
                return ""
            if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
                self.logger.error(
                    f"Could not create buffer SHP at {output_path}: "
                    f"{writer.errorMessage()}"
                )
                if chemin_demande:
                    self.logger.error(indice_verrou)
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

            # Le tampon d'OGR n'atteint le disque qu'a la DESTRUCTION du
            # graveur. Tant que cette fonction n'ecrivait qu'en ESRI Shapefile,
            # verifier avant la destruction tenait par accident : le .shp porte
            # son en-tete de 100 octets des la creation. RADIUS_OUTPUT ouvre la
            # destination aux extensions du selecteur de Processing, dont une
            # douzaine (.kml, .fgb, .xlsx, .parquet, .tab, .ods...) sont servies
            # par des pilotes qui n'ecrivent rien avant la fermeture : le
            # controle rendait '' sur une ecriture parfaitement reussie.
            del writer
            writer = None

            output_path = self._fichier_rayon_produit(output_path)
            if output_path is None:
                return ""

            self.logger.info(f"Analysis buffer saved to: {output_path}")
            return str(output_path)
        except Exception as e:
            self.logger.error(f"Error saving buffer shapefile: {e}")
            if chemin_demande:
                self.logger.error(indice_verrou)
            import traceback

            self.logger.error(traceback.format_exc())
            return ""
        finally:
            # Ensure the writer is always flushed/destroyed, even on exception
            # (audit fix M-7).
            with contextlib.suppress(Exception):
                del writer

    def _liberer_couche_du_projet(self, chemin: Path) -> None:
        """Retire du projet les couches dont la source est `chemin`.

        Appelee juste avant de reecrire a un chemin choisi par l'utilisateur.
        Sans elle, GDAL detruit les fichiers annexes du shapefile precedent
        avant d'echouer sur le verrou que pose la couche encore chargee : le
        livrable deja obtenu devient illisible, sans SCR, et aucun nouveau n'est
        produit. Meilleur effort : un echec ici ne doit rien empecher.
        """
        try:
            cible = os.path.normcase(os.path.abspath(str(chemin)))
            a_retirer = []
            for identifiant, couche in QgsProject.instance().mapLayers().items():
                # Une source GPKG porte « chemin|layername=... » : seul le
                # chemin nous interesse pour reconnaitre le fichier verrouille.
                source = (couche.source() or "").split("|", 1)[0]
                if not source:
                    continue
                with contextlib.suppress(Exception):
                    if os.path.normcase(os.path.abspath(source)) == cible:
                        a_retirer.append((identifiant, couche.name()))
            for identifiant, nom in a_retirer:
                QgsProject.instance().removeMapLayer(identifiant)
                self.logger.info(
                    f"Rayon d'analyse : couche « {nom} » retirée du projet pour "
                    f"libérer {chemin.name} avant réécriture."
                )
        except Exception as e:
            self.logger.warning(
                f"Could not release layers holding {chemin}: {e}"
            )

    def _fichier_rayon_produit(self, demande: Path):
        """Rend le fichier reellement ecrit, ou None si rien ne l'a ete.

        QGIS ajoute l'extension canonique du pilote quand celle saisie n'en est
        pas une : « rayon.json » est reconnu par `driverForExtension` comme du
        GeoJSON — donc pas de repli sur le Shapefile — mais le fichier produit
        s'appelle « rayon.json.geojson ». Verifier le chemin demande rendait
        alors '' sur une ecriture reussie, et le journal citait un chemin
        inexistant sans jamais nommer le fichier obtenu.
        """
        with contextlib.suppress(OSError):
            if demande.exists() and demande.stat().st_size > 0:
                return demande

        # Le pilote a renomme la cible : chercher « <nom demande>.<canonique> ».
        candidats = []
        with contextlib.suppress(OSError):
            candidats = [
                c for c in demande.parent.glob(demande.name + ".*")
                if c.is_file() and c.stat().st_size > 0
            ]
        if candidats:
            produit = max(candidats, key=lambda c: c.stat().st_size)
            self.logger.info(
                f"Rayon d'analyse : le pilote a écrit {produit.name} "
                f"(extension canonique ajoutée à {demande.name})."
            )
            return produit

        self.logger.error(f"Buffer SHP missing or empty after write: {demande}")
        return None

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
          - NoData      : outside the analysis radius - never computed
          - 0           : not seen, including the cells forced there by the
                          forest / building masking
          - 0.1         : footprint of the area of interest
          - 1+          : number of contour points (one every 50 m along the
                          area of interest) that see the cell

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
        color_ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
        color_ramp.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(-1e6, night, NON_VU),
                QgsColorRampShader.ColorRampItem(0.0999, night, NON_VU),
                QgsColorRampShader.ColorRampItem(0.1, invisible, EMPRISE),
                QgsColorRampShader.ColorRampItem(1e6, invisible, "Visible"),
            ]
        )
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(color_ramp)
        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        renderer.setClassificationMin(-1e6)
        renderer.setClassificationMax(1e6)
        renderer.setOpacity(1.0)
        with contextlib.suppress(AttributeError):
            renderer.setNodataColor(night)
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
        color_ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
        color_ramp.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(-1e6, invisible, NON_VU),
                QgsColorRampShader.ColorRampItem(0.0999, invisible, NON_VU),
                QgsColorRampShader.ColorRampItem(0.1, invisible, EMPRISE),
                # Ce qu'on compte, ce sont des POINTS du contour (un tous les
                # 50 m), pas des personnes : « 1 observateur » le faisait lire.
                QgsColorRampShader.ColorRampItem(
                    1.0, QColor(255, 255, 255, 180), "Vu depuis 1 point du contour"
                ),
                QgsColorRampShader.ColorRampItem(
                    4.0, QColor(255, 235, 100, 200), "≥ 4 points"
                ),
                QgsColorRampShader.ColorRampItem(
                    8.0, QColor(255, 150, 30, 220), "≥ 8 points"
                ),
                QgsColorRampShader.ColorRampItem(
                    16.0, QColor(200, 20, 30, 235), "≥ 16 points"
                ),
            ]
        )
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(color_ramp)
        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        renderer.setClassificationMin(-1e6)
        renderer.setClassificationMax(16.0)
        renderer.setOpacity(1.0)
        with contextlib.suppress(AttributeError):
            renderer.setNodataColor(invisible)
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        self.logger.info("ZIV symbology: warm gradient (style v1.3.x)")

    def _apply_ramp_translucent_red(self, layer: QgsRasterLayer) -> None:
        """Uniform translucent red wherever the viewshed sees the cell."""
        provider = layer.dataProvider()
        invisible = QColor(0, 0, 0, 0)
        red = QColor(200, 20, 30, 100)  # ~40% red

        color_ramp = QgsColorRampShader()
        color_ramp.setColorRampType(QgsColorRampShader.Type.Discrete)
        color_ramp.setColorRampItemList(
            [
                QgsColorRampShader.ColorRampItem(0.0999, invisible, NON_VU),
                QgsColorRampShader.ColorRampItem(0.1, invisible, EMPRISE),
                QgsColorRampShader.ColorRampItem(1e6, red, "Visible"),
            ]
        )
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(color_ramp)
        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        renderer.setClassificationMin(-1e6)
        renderer.setClassificationMax(1e6)
        renderer.setOpacity(1.0)
        with contextlib.suppress(AttributeError):
            renderer.setNodataColor(invisible)
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        self.logger.info("ZIV symbology: translucent red (uniform)")

    def _resoudre_emprise(self, zip_source):
        """Rend la ZIP comme QgsVectorLayer valide, ou None.

        `parameters[INPUT]` n'est une couche que lorsque l'algorithme est
        appele depuis Python. Depuis le FORMULAIRE, c'est un identifiant de
        couche ou un QgsProcessingFeatureSourceDefinition, et
        `QgsVectorLayer(str(...), ..., "ogr")` rend alors une couche invalide.
        Le calage etait donc saute a chaque execution par l'interface — sans
        consequence en France, ou la ZIP et le MNS partagent le Lambert 93,
        mais fatal des que le SCR de travail differe de celui du projet.
        `parameterAsVectorLayer` a deja resolu la couche pour la legende :
        c'est elle qui fait foi.
        """
        from qgis.core import QgsProcessingUtils

        if isinstance(zip_source, QgsVectorLayer) and zip_source.isValid():
            return zip_source

        deja_resolue = getattr(self, "_ziv_zip_layer", None)
        if isinstance(deja_resolue, QgsVectorLayer) and deja_resolue.isValid():
            return deja_resolue

        # Un QgsProcessingFeatureSourceDefinition porte la source dans
        # `source`, elle-meme une QgsProperty : dérouler avant de resoudre.
        candidat = zip_source
        interne = getattr(candidat, "source", None)
        if interne is not None:
            candidat = getattr(interne, "staticValue", lambda: interne)()

        contexte = getattr(self, "_contexte_processing", None)
        if contexte is not None:
            with contextlib.suppress(Exception):
                trouvee = QgsProcessingUtils.mapLayerFromString(
                    str(candidat), contexte)
                if isinstance(trouvee, QgsVectorLayer) and trouvee.isValid():
                    return trouvee

        # Dernier recours sans etat : un identifiant de couche se resout sur
        # le projet courant. Sans cela, la resolution dependrait de ce que
        # `processAlgorithm` a bien voulu retenir avant — un contrat trop
        # fragile pour un garde-fou.
        du_projet = QgsProject.instance().mapLayer(str(candidat))
        if isinstance(du_projet, QgsVectorLayer) and du_projet.isValid():
            return du_projet

        depuis_fichier = QgsVectorLayer(str(candidat), "emprise_projet", "ogr")
        return depuis_fichier if depuis_fichier.isValid() else None

    def _donner_une_surface_aux_points(self, couche, dem_path):
        """Remplace chaque entite PONCTUELLE par un carre d'une cellule.

        Pourquoi c'est necessaire. Le modele fabrique ses observateurs avec
        `native:pointsalonglines`, applique a l'emprise. Sur une entree
        ponctuelle, cet algorithme rend bien UNE entite — mais sa geometrie est
        VIDE (mesure du 2026-09-16). Le modele echoue alors sur « Aucun point
        d'observation ne tombe sur le terrain fourni », et l'utilisateur qui
        vient de pointer un mat ou une eolienne n'obtient rien.

        On donne donc au point le plus petit contour qui ait un sens : la
        cellule du terrain ou il se trouve. Mesure a l'appui, un carre de 2 m
        rend exactement UN observateur, a 1 m du point d'origine — moins d'une
        cellule, donc invisible dans le resultat. Un carre de 20 m en rendrait
        deux : la taille n'est pas decorative, elle tient au pas de 50 m que le
        modele emploie le long du contour.

        Les lignes et les polygones passent inchanges : eux rendent des points
        aux bonnes coordonnees.
        """
        try:
            ponctuelles = [entite for entite in couche.getFeatures()
                           if entite.geometry() is not None
                           and not entite.geometry().isEmpty()
                           and entite.geometry().length() == 0
                           and entite.geometry().area() == 0]
            if not ponctuelles:
                return couche

            cellule = float(getattr(self, "_resolution_terrain", 0) or 0)
            if cellule <= 0:
                jeu = QgsRasterLayer(str(dem_path), "mns_maille")
                cellule = (jeu.rasterUnitsPerPixelX()
                           if jeu.isValid() else self.ZIV_TARGET_RES_M)
            demi = max(float(cellule), 0.5) / 2.0

            surface = QgsVectorLayer(
                "Polygon?crs=" + couche.crs().authid(),
                "emprise_projet_ponctuelle", "memory")
            surface.dataProvider().addAttributes(couche.fields())
            surface.updateFields()
            copies = []
            for entite in couche.getFeatures():
                geometrie = entite.geometry()
                if geometrie is None or geometrie.isEmpty():
                    continue
                if geometrie.length() == 0 and geometrie.area() == 0:
                    centre = geometrie.centroid().asPoint()
                    geometrie = QgsGeometry.fromRect(QgsRectangle(
                        centre.x() - demi, centre.y() - demi,
                        centre.x() + demi, centre.y() + demi))
                copie = QgsFeature(surface.fields())
                copie.setGeometry(geometrie)
                copie.setAttributes(entite.attributes())
                copies.append(copie)
            if not copies:
                return couche
            surface.dataProvider().addFeatures(copies)
            surface.updateExtents()
            self.logger.info(
                "Emprise ponctuelle : %d point(s) etendu(s) a la cellule du "
                "terrain (%.1f m de cote). Chacun vaut UN observateur portant "
                "un objet de la hauteur demandee ; le resultat se lit « d'ou "
                "voit-on cet objet »." % (len(ponctuelles), 2.0 * demi))
            return surface
        except Exception as erreur:
            self.logger.warning(
                "Emprise ponctuelle non etendue (%s) : le modele risque de ne "
                "trouver aucun observateur." % erreur)
            return couche

    def _caler_emprise_sur_le_mnt(self, zip_source, dem_path):
        """Reprojette l'emprise projet dans le systeme du MNS et verifie le recouvrement.

        Renvoie une couche prete pour le modele, ou None si l'emprise ne touche
        pas le MNS — auquel cas il n'y a rien a calculer, et le dire vaut mieux
        que produire un raster vide de plusieurs dizaines de gigaoctets.
        """
        try:
            mnt = QgsRasterLayer(str(dem_path), "mns_controle")
            if not mnt.isValid():
                self.logger.warning(
                    "MNS illisible pour le calage de l'emprise : le modele recoit "
                    "l'emprise telle quelle."
                )
                return zip_source

            couche = self._resoudre_emprise(zip_source)
            if couche is None:
                # Passer l'emprise telle quelle a deja coute cher : depuis le
                # formulaire, la ZIP repartait dans son SCR d'origine pendant
                # que le viewshed sortait dans celui du MNS, et gdal:merge
                # faisait l'union de deux rasters distants de milliers de
                # kilometres. Mieux vaut ne rien rendre que rendre cela.
                self.logger.error(
                    "Emprise projet impossible a resoudre en couche : le calage "
                    "sur le MNS ne peut pas etre verifie. Rien n'est calcule, "
                    "car une emprise non calee produit une ZIV de plusieurs "
                    "dizaines de gigaoctets. Charger la ZIP comme une couche "
                    "vecteur du projet."
                )
                return None

            scr_mnt = mnt.crs()
            if couche.crs() != scr_mnt:
                self.logger.info(
                    "Emprise projet en %s, MNS en %s : reprojection avant la ZIV."
                    % (couche.crs().authid(), scr_mnt.authid())
                )
                transformation = QgsCoordinateTransform(
                    couche.crs(), scr_mnt, QgsProject.instance()
                )
                # Le type de la SOURCE, jamais « Polygon » code en dur : une
                # couche memoire REFUSE une geometrie d'un autre type, et le
                # refus de `addFeatures` ne leve pas. Un observateur ponctuel
                # disparaissait donc ici — mesure du 2026-09-17 : addFeatures
                # rend False, la couche calee sort VIDE, et l'emprise nulle
                # faisait echouer le controle de recoupement deux lignes plus
                # bas, sur un message qui accusait le SCR de l'utilisateur.
                # Invisible en France (projet et MNT en Lambert 93, donc pas
                # de reprojection), systematique aux Etats-Unis, ou le calcul
                # se fait en UTM.
                calee = QgsVectorLayer(
                    "%s?crs=%s" % (QgsWkbTypes.displayString(couche.wkbType()),
                                   scr_mnt.authid()),
                    "emprise_projet_calee", "memory"
                )
                calee.dataProvider().addAttributes(couche.fields())
                calee.updateFields()
                entites = []
                for entite in couche.getFeatures():
                    geometrie = entite.geometry()
                    if geometrie.isEmpty():
                        continue
                    geometrie.transform(transformation)
                    copie = QgsFeature(calee.fields())
                    copie.setGeometry(geometrie)
                    copie.setAttributes(entite.attributes())
                    entites.append(copie)
                if not entites:
                    self.logger.error("Emprise projet vide apres reprojection.")
                    return None
                calee.dataProvider().addFeatures(entites)
                calee.updateExtents()
                # Le fournisseur memoire ecarte EN SILENCE ce qu'il n'accepte
                # pas. Compter ce qui est entre evite de poursuivre avec une
                # couche vide, dont l'echec serait impute au SCR.
                if calee.featureCount() != len(entites):
                    self.logger.error(
                        "Emprise projet : %d entite(s) sur %d ont ete refusees "
                        "par la couche reprojetee (type %s). Rien n'est "
                        "calcule."
                        % (len(entites) - calee.featureCount(), len(entites),
                           QgsWkbTypes.displayString(couche.wkbType()))
                    )
                    return None
                couche = calee

            if not couche.extent().intersects(mnt.extent()):
                self.logger.error(
                    "L'emprise projet (%s) ne recoupe pas le MNS (%s), dans le meme "
                    "systeme de coordonnees. Rien a calculer : verifier le systeme de "
                    "coordonnees de la couche d'emprise."
                    % (couche.extent().toString(0), mnt.extent().toString(0))
                )
                return None
            return couche
        except Exception as erreur:
            self.logger.warning(
                "Calage de l'emprise impossible (%s) : passee telle quelle." % erreur
            )
            return zip_source

    # Valeur de l'empreinte du projet dans la ZIV. La chaine lit 0 comme
    # « non vu », 0,1 comme « c'est le projet lui-meme » et 1 et plus comme
    # du visible. Le modele la grave ; le masquage l'efface ; on la rend.
    EMPREINTE_PROJET = 0.1

    # Au-dessous de cette surface, une tache « non vue » isolee au milieu du
    # visible est tenue pour un artefact et comblee. 24 m2, soit un carre de
    # 5 m de cote — un abri de jardin, pas une maison.
    #
    # Le seuil a fait l'aller-retour le 2026-09-17 : 24, puis 100 sur « le
    # moins possible » de mouchetis, puis 24 de nouveau. Mesure sur la ZIV
    # americaine, en cumule : 24 m2 comble 97 cellules, 50 m2 en comble 165,
    # 100 m2 en comble 218, 400 m2 en comble 693. Le choix se lit donc ainsi :
    # a 24 m2 on n'efface que du bruit, a 100 on emporte une maison isolee, et
    # au-dela un bois de 20 m de cote s'en irait sans laisser de trace.
    SURFACE_MINIMALE_TACHE_M2 = 24.0

    def _gommer_le_mouchetis(self, chemin_ziv):
        """Comble les petites taches « non vues » isolees dans du visible.

        Pourquoi seulement dans ce sens. Un tamis ordinaire agit des deux
        cotes : il retirerait aussi les ilots VISIBLES isoles. Mesure du
        2026-09-17 sur une ZIV francaise, ou le visible est rare et fragmente
        (0,5 % des cellules) : un tamis a 36 m2 y emporte 829 des 5 456
        cellules vues, soit 15 % de toute la visibilite. Or une trouee d'ou
        l'on voit le projet est precisement ce qu'une etude d'impact doit
        montrer. On ne retire donc JAMAIS de visibilite : on n'en ajoute un
        peu, ce qui est majorant, donc du bon cote.

        Comment. `gdal.SieveFilter` — livre avec GDAL, aucune dependance
        nouvelle — sur un masque binaire vu / non vu ; on ne garde que les
        cellules qu'il fait passer de non vu a vu. Chacune recoit ensuite la
        PLUS PETITE valeur vue parmi ses voisins : une cellule comblee ne
        pretend pas voir mieux que le moins bien loti de ses voisins.

        L'empreinte du projet et le sans-donnee sont laisses de cote : la
        premiere est regravee juste apres, le second n'est pas un trou mais
        l'absence de calcul.

        Meilleur effort : rend le nombre de cellules comblees, 0 si rien n'a
        pu etre fait. Un rendu mouchete vaut mieux que pas de ZIV.
        """
        if not chemin_ziv:
            return 0
        try:
            import numpy as np
            from osgeo import gdal

            gdal.UseExceptions()
            jeu = gdal.Open(str(chemin_ziv), gdal.GA_Update)
            if jeu is None:
                return 0
            try:
                grille = jeu.GetGeoTransform()
                cote = abs(grille[1]) or self.ZIV_TARGET_RES_M
                seuil = int(round(self.SURFACE_MINIMALE_TACHE_M2
                                  / (cote * cote)))
                if seuil < 2:
                    # Une seule cellule fait deja plus que la surface tenue
                    # pour negligeable : il n'y a pas d'artefact a gommer.
                    self.logger.info(
                        "Mouchetis : maille de %g m, une cellule vaut deja "
                        "%g m2 — rien a gommer." % (cote, cote * cote))
                    return 0

                bande = jeu.GetRasterBand(1)
                valeurs = np.asarray(bande.ReadAsArray(), dtype="float32")
                sans_donnee = bande.GetNoDataValue()
                absente = (np.isclose(valeurs, sans_donnee)
                           if sans_donnee is not None
                           else np.zeros(valeurs.shape, dtype=bool))
                vu = (valeurs >= 1.0) & ~absente

                memoire = gdal.GetDriverByName("MEM")
                lignes, colonnes = valeurs.shape
                entree = memoire.Create("", colonnes, lignes, 1, gdal.GDT_Byte)
                entree.GetRasterBand(1).WriteArray(vu.astype("uint8"))
                sortie = memoire.Create("", colonnes, lignes, 1, gdal.GDT_Byte)
                gdal.SieveFilter(entree.GetRasterBand(1), None,
                                 sortie.GetRasterBand(1), seuil, 8)
                tamise = np.asarray(sortie.GetRasterBand(1).ReadAsArray()) > 0
                entree = None
                sortie = None

                # UNIQUEMENT les trous combles. Ce que le tamis voudrait
                # retirer par ailleurs est ignore, c'est tout le propos.
                a_combler = tamise & ~vu & ~absente & (valeurs
                                                       != self.EMPREINTE_PROJET)
                nombre = int(a_combler.sum())
                if nombre == 0:
                    self.logger.info(
                        "Mouchetis : aucune tache non vue de moins de %g m2 "
                        "au milieu du visible." % self.SURFACE_MINIMALE_TACHE_M2)
                    return 0

                # Une tache de `seuil` cellules a un rayon de quelques
                # cellules : quatre tours de voisinage suffisent, et la
                # boucle sort des qu'il n'y a plus rien a remplir.
                comble = valeurs.copy()
                vus = vu.copy()
                for _ in range(4):
                    reste = a_combler & ~vus
                    if not reste.any():
                        break
                    voisin = self._plus_petit_voisin_vu(comble, vus)
                    prets = reste & np.isfinite(voisin)
                    if not prets.any():
                        break
                    comble[prets] = voisin[prets]
                    vus |= prets

                bande.WriteArray(comble)
                bande.ComputeStatistics(False)
                jeu.FlushCache()
                self.logger.info(
                    "Mouchetis gomme : %d cellule(s) non vues isolees comblees "
                    "(taches de moins de %g m2, soit %d cellule(s) a la maille "
                    "de %g m). Aucune visibilite n'a ete retiree."
                    % (nombre, self.SURFACE_MINIMALE_TACHE_M2, seuil, cote))
                return nombre
            finally:
                # Sous Windows le fichier reste verrouille tant que la
                # derniere reference vit.
                jeu = None
        except Exception as erreur:
            self.logger.warning(
                "Mouchetis non gomme (%s) : la ZIV est rendue telle quelle."
                % erreur)
            return 0

    @staticmethod
    def _plus_petit_voisin_vu(valeurs, vu):
        """Pour chaque cellule, la plus petite valeur vue de ses 8 voisins.

        `inf` la ou aucun voisin n'est vu — l'appelant s'en sert pour savoir
        ce qu'il peut encore remplir.
        """
        import numpy as np

        lignes, colonnes = valeurs.shape
        grand = np.full((lignes + 2, colonnes + 2), np.inf, dtype="float32")
        grand[1:-1, 1:-1] = np.where(vu, valeurs, np.inf)
        decales = [grand[1 + dy:1 + dy + lignes, 1 + dx:1 + dx + colonnes]
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                   if (dy, dx) != (0, 0)]
        return np.minimum.reduce(decales)

    def _rendre_lempreinte_du_projet(self, chemin_ziv, emprise):
        """Regrave l'emprise du projet a 0,1, APRES le masquage.

        Pourquoi c'est necessaire. Le masquage force « hors visibilite » tout
        ce que couvrent les bois et le bati, sans regarder ce qu'il efface :
        l'empreinte du projet y passe comme le reste. Mesure du 2026-09-17 sur
        une ZIV synthetique — un bois mordant sur l'emprise emporte 600 des
        1 600 cellules d'empreinte. A l'ecran, la ZIV « deborde » sur la zone
        de projet, alors que la convention veut qu'elle y soit uniformement a
        0,1. Le defaut ne tient PAS au mode de saisie de l'emprise : il se
        produit avec une couche du projet comme avec un trace a la souris, dans
        les deux pays, des que l'emprise porte du couvert.

        Rendre l'empreinte en dernier plutot que d'apprendre au masquage a
        l'epargner : il y a deux masquages, l'un vectoriel (France) et l'autre
        matriciel (Etats-Unis), et un seul endroit ou reposer la valeur.

        Meilleur effort : sans emprise ou sans GDAL, la ZIV est rendue telle
        quelle — une empreinte entamee vaut mieux que pas de ZIV.
        """
        if not chemin_ziv or emprise is None:
            return False
        try:
            from osgeo import ogr, osr

            dossier = Path(str(getattr(self, "_working_dir", "")
                               or Path(chemin_ziv).parent))
            chemin = dossier / "_ziv_empreinte_projet.geojson"
            if chemin.exists():
                chemin.unlink()

            systeme = osr.SpatialReference()
            systeme.ImportFromWkt(emprise.crs().toWkt())
            pilote = ogr.GetDriverByName("GeoJSON")
            source = pilote.CreateDataSource(str(chemin))
            couche = source.CreateLayer("empreinte", systeme, ogr.wkbUnknown)
            definition = couche.GetLayerDefn()
            nombre = 0
            for entite in emprise.getFeatures():
                geometrie = entite.geometry()
                if geometrie is None or geometrie.isEmpty():
                    continue
                copie = ogr.Feature(definition)
                copie.SetGeometry(ogr.CreateGeometryFromWkt(geometrie.asWkt()))
                couche.CreateFeature(copie)
                nombre += 1
            source = None
            if nombre == 0:
                return False

            from . import donnees_ign

            if not donnees_ign.graver_dans_raster(
                    str(chemin_ziv), str(chemin),
                    valeur=self.EMPREINTE_PROJET, ajouter=False,
                    journal=self.logger):
                return False
            self.logger.info(
                "Empreinte du projet rendue a %g apres le masquage (%d "
                "entite(s)) : la ZIV ne deborde pas sur la zone de projet."
                % (self.EMPREINTE_PROJET, nombre))
            return True
        except Exception as erreur:
            self.logger.warning(
                "Empreinte du projet non rendue apres le masquage (%s) : la "
                "ZIV peut mordre sur la zone de projet." % erreur)
            return False

    def _poser_sans_donnee_hors_du_rayon(self, chemin_ziv, emprise, rayon):
        """Les cellules hors du rayon d'analyse passent en sans-donnee.

        Aucun observateur ne les a calculees : le viewshed part d'un fond a 0
        et `gdal:merge` assemble sans sans-donnee. Elles valaient donc 0, comme
        une cellule regardee et NON VUE — dans tous les coins du raster. Les
        marquer laisse a 0 ce qui a ete regarde sans etre vu, et permet a toute
        analyse en aval (part visible, surfaces) de compter juste.

        `allTouched` : une cellule que le bord du rayon effleure reste
        calculee. Meilleur effort : en cas d'echec, la ZIV reste telle quelle.
        """
        if not chemin_ziv or emprise is None or not rayon or float(rayon) <= 0:
            return 0
        jeu = masque = None
        chemin = None
        try:
            import numpy as np
            from osgeo import gdal, ogr, osr

            geometries = [e.geometry() for e in emprise.getFeatures()
                          if e.hasGeometry() and not e.geometry().isEmpty()]
            if not geometries:
                return 0
            zone = QgsGeometry.unaryUnion(geometries).buffer(float(rayon), 32)
            if zone is None or zone.isEmpty():
                return 0

            dossier = Path(str(getattr(self, "_working_dir", "")
                               or Path(chemin_ziv).parent))
            chemin = dossier / "_ziv_rayon_analyse.geojson"
            if chemin.exists():
                chemin.unlink()
            systeme = osr.SpatialReference()
            systeme.ImportFromWkt(emprise.crs().toWkt())
            source = ogr.GetDriverByName("GeoJSON").CreateDataSource(str(chemin))
            couche = source.CreateLayer("rayon", systeme, ogr.wkbUnknown)
            entite = ogr.Feature(couche.GetLayerDefn())
            entite.SetGeometry(ogr.CreateGeometryFromWkt(zone.asWkt()))
            couche.CreateFeature(entite)
            source = None

            gdal.UseExceptions()
            jeu = gdal.Open(str(chemin_ziv), gdal.GA_Update)
            masque = gdal.GetDriverByName("MEM").Create(
                "", jeu.RasterXSize, jeu.RasterYSize, 1, gdal.GDT_Byte)
            masque.SetGeoTransform(jeu.GetGeoTransform())
            masque.SetProjection(jeu.GetProjection())
            gdal.Rasterize(masque, str(chemin), options=gdal.RasterizeOptions(
                burnValues=[1], allTouched=True))
            dehors = np.asarray(masque.GetRasterBand(1).ReadAsArray()) == 0

            bande = jeu.GetRasterBand(1)
            valeurs = np.asarray(bande.ReadAsArray(), dtype="float32")
            ancienne = bande.GetNoDataValue()
            if ancienne is not None:
                dehors |= np.isclose(valeurs, ancienne)
            nombre = int(dehors.sum())
            if nombre:
                valeurs[dehors] = SANS_DONNEE_ZIV
                bande.WriteArray(valeurs)
            bande.SetNoDataValue(SANS_DONNEE_ZIV)
            bande.ComputeStatistics(False)
            jeu.FlushCache()
            self.logger.info(
                "ZIV : %d cellule(s) hors du rayon d'analyse marquees "
                "sans-donnee — jamais calculees, elles ne se lisent plus "
                "« non vu »." % nombre)
            return nombre
        except Exception as erreur:
            self.logger.warning(
                "Sans-donnee hors du rayon non pose (%s) : les cellules jamais "
                "calculees restent a 0, comme du non vu." % erreur)
            return 0
        finally:
            # Sous Windows un fichier reste verrouille tant que vit la
            # derniere reference : liberer avant de rendre la main.
            jeu = masque = None
            if chemin is not None:
                with contextlib.suppress(OSError):
                    chemin.unlink()

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
            # L'emprise projet DOIT etre dans le systeme du MNS. Le modele ne la
            # reprojette pas : le tampon reste dans son systeme d'origine, le
            # rasterise aussi, et gdal:merge fait ensuite l'union de deux rasters
            # distants de centaines de kilometres. Mesure : une emprise en Web
            # Mercator au lieu du Lambert 93 produit une ZIV de 94 638 x 320 342
            # cellules, soit 113 Go — pour un calcul de 6 x 6 km.
            emprise_calee = self._caler_emprise_sur_le_mnt(zip_source, dem_path)
            if emprise_calee is None:
                raise QgsProcessingException(
                    "Calcul de la ZIV impossible : l'emprise n'a pas pu etre "
                    "calee sur le terrain (voir les messages ci-dessus).")
            # Une emprise PONCTUELLE n'a pas de contour, et c'est le contour
            # que le modele parcourt pour poser ses observateurs.
            emprise_calee = self._donner_une_surface_aux_points(
                emprise_calee, dem_path)
            params["zip"] = emprise_calee

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
                # Le mouchetis se gomme APRES le masquage — c'est lui qui en
                # produit une partie, un batiment isole faisant une tache de
                # quelques cellules — et AVANT que l'empreinte ne soit
                # reposee, pour qu'elle sorte intacte quoi qu'il arrive.
                self._gommer_le_mouchetis(ziv_result_path)
                # Le masquage grave sans regarder ce qu'il efface : l'emprise
                # du projet y passe comme le reste. On la rend EN DERNIER.
                self._rendre_lempreinte_du_projet(
                    ziv_result_path, emprise_calee)
                # Ce que personne n'a calcule ne doit pas se lire « non vu ».
                self._poser_sans_donnee_hors_du_rayon(
                    ziv_result_path, emprise_calee, ziv_radius)
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

        except QgsProcessingException:
            raise
        except Exception as e:
            # Une annulation pendant le modele n'est pas un echec : la seconde
            # garde de _finalize_result retire ce qui a deja ete charge.
            retour = getattr(self, "feedback", None)
            if retour is not None and retour.isCanceled():
                self.logger.info("Calcul de la ZIV interrompu par l'annulation.")
                return ""
            self.logger.error(f"Error running ZIV model: {str(e)}")
            import traceback

            self.logger.error(traceback.format_exc())
            # Plus de boite modale : une exception suffit. Processing garde sa
            # fenetre ouverte sur un echec (`keepOpen = not successful`, dans
            # les deux QGIS) et l'affiche en rouge ; la boite, elle, ne servait
            # qu'a rattraper un faux succes, et bloquait modeles et bancs.
            raise QgsProcessingException("Calcul de la ZIV impossible : %s" % e) from e

    def _terrain_par_fournisseur(self, aoi_geometry, dossier, charger_couches):
        """Terrain et surface occultante par le fournisseur, puis chaînage ZIV.

        Remplace, pour un fournisseur qui fournit son terrain, tout le bloc de
        découverte de dalles : le service américain est transparent, il n'y a
        rien à découvrir. Le contrat de sortie est celui de la chaîne française
        — le dictionnaire que `_finalize_result` consomme — de sorte que la
        suite (ZIV, symbologie, masquage, points de vue, cartouche) ne voit
        aucune différence.

        Là où les États-Unis ne livrent qu'un terrain nu, la surface occultante
        est **reconstruite** ici : végétation et bâti gravés en une fois, sur le
        maximum des deux hauteurs. Un échec de gravure n'arrête pas le calcul —
        mais il est DIT, parce qu'une ZIV sur terrain nu surestime la visibilité
        sur le fond, pas dans la marge.
        """
        fournisseur = self._fournisseur
        emprise = aoi_geometry.boundingBox()
        boite = (emprise.xMinimum(), emprise.yMinimum(),
                 emprise.xMaximum(), emprise.yMaximum())

        resolution = fournisseur.resolution_de_travail(
            self.ZIV_TARGET_RES_M, self._aoi_bbox_wgs84, journal=self.logger)
        self._resolution_terrain = resolution

        est_annule = getattr(self.feedback, "isCanceled", None)
        try:
            chemin = fournisseur.terrain(
                boite, resolution, self._epsg_travail, str(dossier),
                journal=self.logger, est_annule=est_annule)
        except Exception as erreur:
            # Une exception, pas un resultat vide : rendre un dictionnaire ici
            # faisait dire a QGIS « finished » sur un calcul qui n'avait rien
            # produit, et l'utilisateur cherchait sa ZIV dans un projet vide.
            self.logger.error("Terrain indisponible : %s" % erreur)
            raise QgsProcessingException(
                "Terrain indisponible, la ZIV n'a pas ete calculee : %s" % erreur)

        if fournisseur.occulte():
            # Les courbes de niveau se tirent du TERRAIN, jamais de la surface.
            # La gravure ci-dessous ecrit EN PLACE : passe ce point, le terrain
            # nu n'existe plus. Or des contours calcules sur la canopee
            # LANDFIRE, dont la maille est de 30 m, donnent exactement ce qu'un
            # utilisateur a rapporte le 2026-09-11 — des creneaux carres en
            # zone boisee et une nuee de micro-polygones le long des lignes.
            # La copie coute un fichier ; la recalculer couterait un second
            # telechargement du terrain.
            self._terrain_nu = self._garder_terrain_nu(chemin)
            # Le dossier de travail, et non celui des telechargements : le
            # masquage du resultat relira ces memes fichiers plus tard, et
            # `masquage.py` les cherche a cote de la ZIV.
            self._occultation = fournisseur.graver_occultation(
                chemin, boite, self._epsg_travail, str(self._working_dir),
                journal=self.logger)

        self._avertir_scr_du_projet()
        self._journaliser_provenance(resolution)

        # Les dalles ne sont jamais chargees dans le projet : ce sont des
        # intermediaires. Seul le resultat merite d'etre affiche.
        _ = charger_couches
        return self._finalize_result({
            "OUTPUT_DIRECTORY": str(dossier),
            "OUTPUT_FILE": str(chemin),
            "OUTPUT_FILES": "",
        })

    def _livrer_dans_le_scr_du_projet(self, result_dict):
        """Reprojette les couches produites dans le systeme du projet.

        Le CALCUL reste dans le fuseau deduit de l'emprise : c'est lui qui
        donne des distances justes, et en changer fausserait le viewshed. Seul
        le LIVRABLE change de systeme, une fois tout calcule — les points de
        vue et les courbes sont donc etablis sur des coordonnees exactes, puis
        traduits.

        Ce que cela supprime : l'utilisateur recevait des couches dans un
        systeme autre que le sien, et son reflexe — leur imposer le SCR du
        projet — les envoyait a des centaines de kilometres, dans l'ocean pour
        une zone cotiere. Un avertissement au journal n'y suffisait pas.

        Rien ici n'est bloquant. Si une reprojection echoue, la couche reste
        dans le fuseau de calcul, ce qui est juste, et le journal le dit.
        """
        travail = int(getattr(self, "_epsg_travail", 0) or 0)
        if not travail:
            return
        scr_projet = QgsProject.instance().crs()
        if not scr_projet.isValid() or scr_projet.authid() == ("EPSG:%d" % travail):
            return
        if scr_projet.isGeographic():
            # Reprojeter un raster metrique en degres donne des pixels qui ne
            # sont plus carres : le viewshed cesserait d'etre lisible a la
            # regle. On garde le fuseau et on le dit.
            self.logger.warning(
                "Le projet est en %s, un systeme en degres : les couches "
                "restent en EPSG:%d pour garder des pixels carres. QGIS les "
                "reprojette a l'affichage ; ne pas leur imposer le SCR du "
                "projet." % (scr_projet.authid(), travail))
            return

        livrables = self._fichiers_livrables(result_dict)
        if not livrables:
            return

        from osgeo import gdal

        gdal.UseExceptions()
        traduites, echouees = [], []
        for couche, chemin in livrables:
            nom, source = couche.name(), str(chemin)
            rendu = etiquetage = None
            with contextlib.suppress(Exception):
                rendu = couche.renderer().clone()
            with contextlib.suppress(Exception):
                if couche.labelsEnabled():
                    etiquetage = couche.labeling().clone()
            est_raster = isinstance(couche, QgsRasterLayer)
            try:
                # La couche doit lacher le fichier AVANT qu'on le remplace :
                # sous Windows un raster ouvert par QGIS reste verrouille, et
                # `os.replace` echouerait en WinError 32.
                QgsProject.instance().removeMapLayer(couche.id())
                temporaire = "%s.reproj%s" % os.path.splitext(source)
                if est_raster:
                    # Plus proche voisin : les valeurs de la ZIV sont des
                    # classes de visibilite, pas des mesures continues. Les
                    # interpoler inventerait des degres qui n'existent pas.
                    sortie = gdal.Warp(temporaire, source,
                                       dstSRS=scr_projet.authid(),
                                       resampleAlg="near")
                    # `gdal.Warp` rend lui aussi un jeu de donnees OUVERT. Sans
                    # cette fermeture, un handle reste sur le raster remplace —
                    # et un raster a demi tenu se dessine de facon erratique.
                    sortie = None
                else:
                    jeu = gdal.VectorTranslate(temporaire, source,
                                               dstSRS=scr_projet.authid(),
                                               reproject=True)
                    # VectorTranslate rend un jeu de donnees OUVERT : sans
                    # cette fermeture, le fichier reste verrouille et le
                    # remplacement echoue sous Windows.
                    jeu = None
                self._remplacer_fichier_et_freres(temporaire, source)
                traduites.append(nom)
            except Exception as erreur:
                echouees.append("%s (%s)" % (nom, str(erreur)[:60]))
                with contextlib.suppress(Exception):
                    for reste in glob.glob(
                            "%s.*" % os.path.splitext(temporaire)[0]):
                        os.remove(reste)
            # Rechargee dans tous les cas : la couche a ete retiree du projet,
            # la laisser disparaitre serait pire que de la laisser en 32617.
            rechargee = (QgsRasterLayer(source, nom) if est_raster
                         else QgsVectorLayer(source, nom, "ogr"))
            if rechargee.isValid():
                with contextlib.suppress(Exception):
                    if rendu is not None:
                        rechargee.setRenderer(rendu)
                    if etiquetage is not None:
                        rechargee.setLabeling(etiquetage)
                        rechargee.setLabelsEnabled(True)
                QgsProject.instance().addMapLayer(rechargee)
            else:
                self.logger.warning(
                    "Couche « %s » non rechargeable apres reprojection." % nom)

        if traduites:
            self.logger.info(
                "Couches livrees en %s, le systeme du projet : %s. Le calcul, "
                "lui, s'est fait en EPSG:%d — le fuseau de l'emprise, qui seul "
                "donne des distances justes."
                % (scr_projet.authid(), ", ".join(traduites), travail))
        if echouees:
            self.logger.warning(
                "Reprojection impossible pour %s : ces couches restent en "
                "EPSG:%d. Elles sont justes ; ne pas leur imposer le SCR du "
                "projet." % ("; ".join(echouees), travail))

    @staticmethod
    def _remplacer_fichier_et_freres(temporaire, source):
        """Remplace `source` par `temporaire`, FRERES COMPRIS.

        Un Shapefile n'est pas un fichier mais une fratrie : `.shp`, `.shx`,
        `.dbf`, `.prj`. Ne remplacer que le `.shp` laissait le `.prj` d'origine
        en place — la couche continuait d'annoncer l'ancien systeme tout en
        portant des coordonnees nouvelles, ce qui est le pire des deux mondes.
        Un GeoPackage ou un GeoTIFF n'a qu'un fichier : la meme boucle les
        traite sans cas particulier.
        """
        racine_temporaire = os.path.splitext(temporaire)[0]
        racine_source = os.path.splitext(source)[0]
        produits = glob.glob("%s.*" % racine_temporaire)
        if not produits:
            raise OSError("la reprojection n'a produit aucun fichier")
        for produit in produits:
            os.replace(produit, racine_source + os.path.splitext(produit)[1])

    def _couches_du_calcul(self, result_dict):
        """Les couches du projet qui sortent de CE calcul, et leur fichier.

        On ne touche qu'a ce que l'on vient de produire : une couche est
        retenue si son fichier est l'un de ceux que l'on rend, ou se trouve
        dans le dossier de travail. Les couches de l'utilisateur, elles, ne
        sont pas notre affaire.
        """
        attendus = {os.path.normcase(os.path.abspath(str(v)))
                    for v in result_dict.values()
                    if isinstance(v, str) and v and os.path.isfile(str(v))}
        dossier = str(getattr(self, "_working_dir", "") or "")
        dossier = os.path.normcase(os.path.abspath(dossier)) if dossier else ""

        retenues = []
        for couche in list(QgsProject.instance().mapLayers().values()):
            source = str(couche.source()).split("|")[0]
            if not source or not os.path.isfile(source):
                continue
            absolu = os.path.normcase(os.path.abspath(source))
            if absolu in attendus or (dossier and absolu.startswith(dossier)):
                retenues.append((couche, source))
        return retenues

    def _fichiers_livrables(self, result_dict):
        """Celles des couches du calcul qui sont encore dans le fuseau de
        travail — les seules qu'il reste a traduire."""
        travail = "EPSG:%d" % int(getattr(self, "_epsg_travail", 0) or 0)
        return [(couche, source)
                for couche, source in self._couches_du_calcul(result_dict)
                if couche.crs().authid() == travail]

    def _retirer_les_couches_du_calcul(self, result_dict):
        """Retire du projet les couches produites par ce calcul. Rend leur nombre.

        Appelee a l'annulation : les FICHIERS restent sur le disque — le cache
        a ete paye, une relance doit pouvoir s'en servir — mais le projet
        redevient ce qu'il etait. Une couche rescapee d'un calcul interrompu
        ne se distingue en rien d'un resultat complet, une fois affichee.
        """
        retirees = 0
        try:
            for couche, _source in self._couches_du_calcul(result_dict):
                QgsProject.instance().removeMapLayer(couche.id())
                retirees += 1
        except Exception as erreur:
            self.logger.warning(
                "Couches non retirees apres annulation (%s)." % erreur)
        return retirees

    def _ordonner_les_couches(self, result_dict):
        """Remet les couches produites dans l'ordre ou on les lit.

        Points, puis lignes, puis polygones, puis rasters : l'ordre
        cartographique usuel, ou ce qui est le plus fin passe au-dessus de ce
        qui est le plus large. En pratique le rayon d'analyse doit se voir
        PAR-DESSUS la ZIV — un contour de 300 m masque par un raster ne sert a
        rien — et c'est ce qu'un utilisateur a demande le 2026-09-14.

        Le faire explicitement, et non en comptant sur l'ordre d'ajout : la
        traduction des couches dans le systeme du projet (1.5.0) les retire et
        les recharge, donc l'ordre d'arrivee ne veut plus rien dire.

        Chaque couche reste dans le groupe ou QGIS l'a rangee. Une couche
        ajoutee au projet va au point d'insertion, qui suit la selection du
        panneau : dans un groupe quand la couche selectionnee y est. Or
        `removeChildNode` n'otait que les enfants DIRECTS de la racine — la
        copie montait en tete et l'original restait dans le groupe, si bien
        que ZIV, rayon et points de vue s'affichaient deux fois (signale le
        2026-10-13, sur une emprise rangee dans un groupe).
        """
        try:
            racine = QgsProject.instance().layerTreeRoot()

            def _rang(couche):
                if isinstance(couche, QgsRasterLayer):
                    return 3
                try:
                    # 0 point, 1 ligne, 2 polygone — meme numerotation en Qt5
                    # et en Qt6, seule la maison de l'enum a change.
                    return {0: 0, 1: 1, 2: 2}.get(int(couche.geometryType()), 2)
                except Exception:
                    return 2

            couches = [c for c, _s in self._couches_du_calcul(result_dict)]
            if len(couches) < 2:
                return
            # Du bas vers le haut : chaque insertion en tete repousse la
            # precedente d'un cran, donc on pose le raster d'abord.
            for couche in sorted(couches, key=_rang, reverse=True):
                noeud = racine.findLayer(couche.id())
                if noeud is None:
                    continue
                groupe = noeud.parent()
                copie = noeud.clone()
                groupe.insertChildNode(0, copie)
                groupe.removeChildNode(noeud)
        except Exception as erreur:
            # Un ordre de legende ne justifie pas de faire echouer un calcul.
            self.logger.warning(
                "Ordre des couches non applique (%s)." % erreur)

    def _habiller_courbes(self, couche):
        """Trait fin brun, et l'altitude ecrite le long de la ligne.

        Sans etiquette, une courbe de niveau ne dit pas dans quel sens le
        terrain monte : c'est le chiffre qui la rend lisible, pas le trait.

        Les enums de placement ont change de maison entre Qt5 et Qt6 — d'ou
        l'acces tolerant : `Qgis.LabelPlacement` d'abord, present depuis 3.26
        et sur les deux cibles du greffon, l'ancien attribut de classe ensuite,
        et le defaut de QGIS si les deux manquent. Le style, lui, passe par
        `createSimple` et ses chaines, qui n'ont jamais bouge.
        """
        try:
            from qgis.core import (QgsLineSymbol, QgsPalLayerSettings,
                                   QgsTextFormat, QgsVectorLayerSimpleLabeling)

            symbole = QgsLineSymbol.createSimple({
                "color": "150,90,50,255",
                "width": "0.25",
            })
            couche.renderer().setSymbol(symbole)

            reglages = QgsPalLayerSettings()
            reglages.fieldName = "altitude"
            # Un chiffre par ligne suffit ; repete tous les 60 mm de trace, il
            # reste lisible sans transformer la carte en liste de nombres.
            reglages.repeatDistance = 60.0

            placement, millimetre = None, None
            try:
                from qgis.core import Qgis
                placement = Qgis.LabelPlacement.Line
                millimetre = Qgis.RenderUnit.Millimeters
            except Exception:
                placement = getattr(QgsPalLayerSettings, "Line", None)
                try:
                    from qgis.core import QgsUnitTypes
                    millimetre = QgsUnitTypes.RenderUnit.RenderMillimeters
                except Exception:
                    millimetre = None
            if placement is not None:
                reglages.placement = placement
            if millimetre is not None:
                reglages.repeatDistanceUnit = millimetre

            format_texte = QgsTextFormat()
            format_texte.setSize(7.0)
            reglages.setFormat(format_texte)

            couche.setLabeling(QgsVectorLayerSimpleLabeling(reglages))
            couche.setLabelsEnabled(True)
            couche.triggerRepaint()
        except Exception as erreur:
            # Une couche sans habillage reste une couche juste.
            self.logger.warning(
                "Courbes de niveau : habillage non applique (%s)." % erreur)

    def _mnt_pour_les_courbes(self, aoi_geometry, dalles_choisies, dossier,
                              type_donnee):
        """Le MNT des memes dalles, pour en tirer les courbes de niveau.

        Rend le chemin d'une mosaique de TERRAIN, ou "" s'il n'y a rien a
        faire ou si quoi que ce soit echoue — les courbes retombent alors sur
        le produit de travail, en le DISANT au journal.

        Trois faits mesures le 2026-09-14 sur le service de l'IGN, et qui
        rendent l'operation peu couteuse :

          * la decouverte est la MEME requete WFS pour les quatre produits,
            une seule couche les servant tous : demander le MNT ne coute pas
            une recherche de plus, seulement la lecture d'une autre propriete ;
          * les trois produits couvrent les memes dalles — MNT, MNS et MNH
            rendus tous les trois sur l'emprise d'essai ;
          * le nom de dalle est prefixe par le produit (`mnt_1050-6846` contre
            `mns_1050-6846`), donc les deux cohabitent au cache sans que l'un
            se relise pour l'autre. C'est ce prefixe, pose en 1.1.0, qui rend
            ce correctif sur.

        On ne rejoue PAS la selection geometrique : les dalles MNT portent le
        meme identifiant que celles deja retenues, au prefixe pres. Filtrer
        sur cet identifiant donne exactement la meme emprise, sans dupliquer
        une logique de selection qui vit ailleurs.
        """
        if not getattr(self, "_courbes_demandees", False):
            return ""
        resolution = max(self.ZIV_TARGET_RES_M, self.RESOLUTION_COURBES)
        # Le produit demande EST le terrain, et il est deja assez grossier :
        # le fichier de travail fait l'affaire, rien a telecharger. On le
        # SIGNALE, faute de quoi `_produire_courbes` croirait travailler sur
        # une surface et avertirait a tort.
        if type_donnee == 0 and self.ZIV_TARGET_RES_M >= self.RESOLUTION_COURBES:
            self._courbes_sur_le_terrain = True
            return ""

        try:
            # `import_dalles` designe l'ADAPTATEUR, lie au niveau module
            # (`from .import_dalles import adaptateur as import_dalles`). Un
            # `from . import import_dalles` local pointerait le PAQUET et le
            # masquerait : meme nom, autre objet.
            identifiants = {str(d.get("name", "")).split("_", 1)[-1]
                            for d in dalles_choisies if d.get("name")}
            if not identifiants:
                return ""

            bbox = aoi_geometry.boundingBox()
            emprise = (bbox.xMinimum(), bbox.yMinimum(),
                       bbox.xMaximum(), bbox.yMaximum())
            toutes = import_dalles.dalles_geoplateforme(
                emprise, "url_mnt", journal=self.logger)
            dalles_mnt = [d for d in toutes
                          if str(d.get("name", "")).split("_", 1)[-1] in identifiants]
            if not dalles_mnt:
                self.logger.warning(
                    "Courbes de niveau : aucune dalle MNT sur l'emprise alors "
                    "que %d dalle(s) du produit de travail y sont. Les courbes "
                    "suivront la surface." % len(identifiants))
                return ""
            if len(dalles_mnt) < len(identifiants):
                # Dit, et non tu : une couverture partielle troue le terrain,
                # et un trou de terrain se lit comme un replat sur la carte.
                self.logger.warning(
                    "Courbes de niveau : seulement %d dalle(s) MNT pour %d "
                    "dalle(s) de travail — le terrain sera incomplet sur les "
                    "bords." % (len(dalles_mnt), len(identifiants)))

            # Un dossier a part : `importer_raster` nomme sa mosaique a la
            # SECONDE (`merged_output_<horodatage>.tif`), et deux appels dans
            # la meme seconde se marcheraient dessus.
            dossier_mnt = Path(str(dossier)) / "mnt_courbes"
            dossier_mnt.mkdir(parents=True, exist_ok=True)
            # Une courbe de niveau ne gagne rien a la finesse de la ZIV : au
            # metre, le MNT porte encore le micro-relief — sillons, talus de
            # bord de champ — et le trace se fragmente. Mesure du 2026-09-14 :
            # a 2 m, 4 744 troncons engendres dont 3 111 ecartes comme
            # coquilles. On demande donc le terrain a RESOLUTION_COURBES au
            # plus fin, ce qui allege aussi le telechargement d'un facteur 24
            # (978 Ko contre 40 Ko par dalle). Le cache distingue bien les deux
            # resolutions : le nom de fichier les porte (`..._2m.tif`).
            resultat = import_dalles.importer_raster(
                self, dalles_mnt, str(dossier_mnt), 0, resolution,
                fusionner=True, charger_couches=False)
            chemin = str(resultat.get("OUTPUT_FILE") or "")
            if not chemin or not os.path.isfile(chemin):
                return ""
            # Volontairement PAS dans `_temp_files` : GDAL tient encore le
            # fichier quand le nettoyage passe, et l'echec de suppression
            # produisait un avertissement pour rien. Le terrain reste donc a
            # cote de la ZIV, comme il le fait deja cote americain.
            self.logger.info(
                "Courbes de niveau : MNT telecharge a part sur %d dalle(s) a "
                "%g m — les isolignes decriront le relief nu, pas la canopee."
                % (len(dalles_mnt), resolution))
            return chemin
        except Exception as erreur:
            self.logger.warning(
                "Courbes de niveau : MNT indisponible (%s). Elles seront "
                "tirees du produit de travail et suivront donc la vegetation "
                "et le bati." % erreur)
            return ""

    def _garder_terrain_nu(self, chemin_terrain):
        """Met le terrain de cote AVANT que la surface occultante ne l'ecrase.

        Rend le chemin de la copie, ou "" si elle a echoue — auquel cas les
        courbes retomberont sur la surface, avec un mot au journal. Une couche
        d'agrement ne justifie pas de faire echouer un calcul.

        La copie est RAMENEE a `RESOLUTION_COURBES` quand le terrain est plus
        fin, pour la meme raison qu'en France : une courbe de niveau se lit a
        l'echelle d'une carte topographique, et au metre le terrain porte
        encore le micro-relief agricole. Mesure du 2026-09-14 sur une plaine
        de l'Illinois, equidistance automatique de 2 m : 2 378 troncons
        engendres a 2 m contre 871 a 10 m, soit 819 livres contre 333. Sur un
        relief marque l'ecart est mince (81 contre 47 a Asheville) — c'est en
        terrain plat que la finesse se paie. Aucun reseau ici : le terrain est
        deja sur le disque, on le reechantillonne.
        """
        try:
            from osgeo import gdal

            gdal.UseExceptions()
            source = Path(str(chemin_terrain))
            copie = source.with_name("%s_nu%s" % (source.stem, source.suffix))
            jeu = gdal.Open(str(source))
            pixel = abs(jeu.GetGeoTransform()[1]) if jeu is not None else 0.0
            jeu = None
            if pixel and pixel < self.RESOLUTION_COURBES:
                # `average` et non le plus proche voisin : on veut lisser le
                # micro-relief, pas en echantillonner un point sur cinq.
                sortie = gdal.Warp(
                    str(copie), str(source),
                    xRes=self.RESOLUTION_COURBES, yRes=self.RESOLUTION_COURBES,
                    resampleAlg="average")
                # `gdal.Warp` rend un jeu de donnees OUVERT : sans cette
                # fermeture le fichier reste verrouille sous Windows.
                sortie = None
            else:
                shutil.copyfile(str(source), str(copie))
            # Un Path, pas une chaine : `_cleanup_temp_files` appelle
            # `.exists()` sur ce qu'il trouve, et une chaine le faisait echouer
            # a chaque passage — « Failed to cleanup temp file », pour rien.
            self._temp_files.add(copie)
            return str(copie)
        except Exception as erreur:
            self.logger.warning(
                "Terrain nu non conserve (%s) : les courbes de niveau suivront "
                "la surface occultante, donc la canopee." % erreur)
            return ""

    def _avertir_scr_du_projet(self):
        """Dire que les couches ne sortiront pas dans le SCR du projet.

        Le fuseau de travail se deduit de l'emprise, pas du projet : c'est ce
        qui garantit des distances justes. Mais l'utilisateur, lui, voit
        arriver des couches dans un autre systeme que le sien — et son
        reflexe, leur imposer le SCR du projet, les envoie a des centaines de
        kilometres. Signale le 2026-09-11 : projet en UTM 18N, zone en 17N,
        ZIV « dans l'ocean » apres correction manuelle du SCR.
        """
        # Un avertissement de confort ne fait echouer aucun calcul.
        with contextlib.suppress(Exception):
            travail = int(getattr(self, "_epsg_travail", 0) or 0)
            if not travail:
                return
            scr_projet = QgsProject.instance().crs()
            if not scr_projet.isValid():
                return
            code = scr_projet.authid()
            if code == ("EPSG:%d" % travail):
                return
            self.logger.warning(
                "Le calcul se fait en EPSG:%d, deduit de l'emprise, alors que "
                "le projet est en %s. Les couches produites porteront "
                "EPSG:%d : QGIS les reprojette a l'affichage, il ne faut PAS "
                "leur imposer le SCR du projet — elles se retrouveraient a "
                "des centaines de kilometres." % (travail, code, travail))

    def _journaliser_provenance(self, resolution):
        """Écrit au journal SUR QUOI le résultat repose, en clair.

        Le greffon n'a plus de mise en page cartographique depuis la 1.5.x : le
        journal est donc le seul endroit où la provenance est consignée — et
        `journal_ziv.py` l'écrit aussi dans un fichier daté sur le disque, qui
        peut être joint en annexe d'une étude.

        Ce bloc existe pour une raison précise. Deux ZIV d'apparence identique
        peuvent reposer sur des terrains dix fois différents en finesse, et sur
        un bâti dont la hauteur est mesurée ou seulement supposée. Ne pas le
        dire serait la dégradation silencieuse que `tasks/lessons.md` documente
        sous toutes ses formes.
        """
        stats = getattr(self, "_occultation", None) or {}
        fournisseur = getattr(self, "_fournisseur", None)
        lignes = [
            "----- Provenance du résultat -----",
            "Fournisseur      : %s" % (fournisseur.NOM if fournisseur else "?"),
            "Système de calcul: EPSG:%d" % getattr(self, "_epsg_travail", 0),
        ]

        # Une resolution de travail plus grossiere que celle demandee signifie
        # que le service ne possedait pas plus fin : c'est le mode replie.
        if resolution > self.ZIV_TARGET_RES_M + 1e-6:
            lignes.append(
                "Terrain          : %g m — le LiDAR fin NE COUVRE PAS cette "
                "emprise, le relief est lissé et les talus de moins de %g m "
                "disparaissent. ZIV indicative." % (resolution, resolution))
        else:
            lignes.append("Terrain          : %g m" % resolution)

        provenance = stats.get("provenance") or {}
        total = sum(v for c, v in provenance.items() if c != "sans_occultation")
        if provenance and total:
            part = lambda cle: 100.0 * provenance.get(cle, 0) / total  # noqa: E731
            forfaits = sum(v for c, v in provenance.items()
                           if c.startswith("forfait"))
            lignes.append(
                "Végétation       : LANDFIRE 30 m — %.0f %% de hauteur EVH "
                "directe, %.0f %% comblés par le CH, %.0f %% au forfait ; "
                "jusqu'à %.1f m."
                % (part("evh"), part("ch"), 100.0 * forfaits / total,
                   stats.get("occultation_max", 0.0)))
        else:
            lignes.append(
                "Végétation       : ABSENTE — la canopée n'occulte pas, "
                "la visibilité est SURÉVALUÉE.")

        bati = stats.get("bati")
        if bati:
            lignes.append(
                "Bâti             : %d bâtiment(s), %.0f %% à hauteur mesurée, "
                "%.0f %% au forfait de classe ; moyenne %.1f m."
                % (bati["ecrites"], bati["part_mesuree"],
                   100.0 - bati["part_mesuree"], bati["hauteur_moyenne"]))
            if bati.get("tronque"):
                lignes.append(
                    "                   /!\\ garde-fou de volume atteint : le "
                    "bâti est TRONQUÉ sur cette emprise.")
        else:
            lignes.append(
                "Bâti             : ABSENT — les toitures n'occultent pas.")

        for ligne in lignes:
            self.logger.info(ligne)

        from .fournisseurs import usa_vegetation

        self._fiche().setdefault("sources", {}).update({
            "terrain": {"service": "USGS 3DEP", "resolution_m": resolution,
                        "mode_replie": resolution > self.ZIV_TARGET_RES_M + 1e-6},
            "vegetation": {"service": "LANDFIRE %s (EVH, CH)" % usa_vegetation.MILLESIME,
                           "cellules_par_provenance": provenance or None},
            "bati": bati or None,
            "resume": lignes[1:],
            "attribution": ("USGS 3DEP ; LANDFIRE ; FEMA/ORNL USA Structures ; "
                            "US Census TIGER/Line"),
        })

    def _fiche(self):
        """La fiche de provenance de l'execution, creee au besoin."""
        fiche = getattr(self, "_provenance", None)
        if fiche is None:
            fiche = self._provenance = {}
        return fiche

    def _ouvrir_provenance(self, data_type, rayon, hauteur):
        """Commence la fiche de provenance : quel greffon, quand, quels reglages.

        Deux ZIV d'apparence identique peuvent reposer sur des hypotheses et
        des donnees tres differentes. La fiche, ecrite a cote du raster, le
        dit une fois pour toutes ; le journal, lui, se perd avec la fenetre.
        """
        options = self.DATA_TYPE_OPTIONS
        self._provenance = {
            "greffon": {"nom": PLUGIN_DISPLAY_NAME,
                        "version": _version_du_greffon(),
                        "algorithme": "genziv:generate"},
            "date": datetime.now().isoformat(timespec="seconds"),
            "journal": str(getattr(self.logger, "log_file", "") or ""),
            "parametres": {
                "rayon_analyse_m": rayon,
                "hauteur_saisie_m": hauteur,
                "type_donnees": (options[data_type] if 0 <= data_type < len(options)
                                 else data_type),
                "maille_m": self.ZIV_TARGET_RES_M,
                "symbologie": getattr(self, "_ziv_symbology", None),
                "points_de_vue_demandes": getattr(self, "_viewpoints_count", 0),
                "courbes_de_niveau": bool(getattr(self, "_courbes_demandees", False)),
            },
            "hypotheses": self._hypotheses_du_modele(),
            "sources": {},
        }

    def _hypotheses_du_modele(self):
        """Ce que le modele de ZIV suppose, LU dans le modele et non recopie.

        La fiche dit ce qui a reellement tourne, et restera vraie le jour ou
        ces reglages changeront : hauteur de la cible, courbure, refraction,
        espacement des points poses sur le contour de l'emprise.
        """
        hypotheses = {
            "hauteur_saisie_appliquee_a": "points poses sur le contour de "
                                          "l'emprise (observateurs du calcul)",
        }

        def nombre(valeur):
            try:
                return float(valeur)
            except (TypeError, ValueError):
                return valeur

        try:
            import xml.etree.ElementTree as ElementTree

            racine = ElementTree.parse(
                str(Path(__file__).parent / "models" / "creation_ziv.model3")).getroot()
            for noeud in racine.iter("Option"):
                enfants = noeud.findall("Option")
                algorithme = next((o.get("value") for o in enfants
                                   if o.get("name") == "alg_id"), None)
                params = next((o for o in enfants if o.get("name") == "params"), None)
                if not algorithme or params is None:
                    continue
                statiques = {}
                for liste in params.findall("Option"):
                    for carte in liste.findall("Option"):
                        for valeur in carte.findall("Option"):
                            if valeur.get("name") == "static_value":
                                statiques[liste.get("name")] = valeur.get("value")
                if algorithme.endswith("pointsalonglines") and "DISTANCE" in statiques:
                    hypotheses["espacement_points_contour_m"] = nombre(statiques["DISTANCE"])
                if "TARGET_HEIGHT" in statiques:
                    hypotheses["hauteur_cible_m"] = nombre(statiques["TARGET_HEIGHT"])
                if "USE_CURVATURE" in statiques:
                    hypotheses["courbure_terrestre"] = statiques["USE_CURVATURE"] == "true"
                if "REFRACTION" in statiques:
                    hypotheses["refraction"] = nombre(statiques["REFRACTION"])
        except Exception as erreur:
            hypotheses["modele_illisible"] = str(erreur)
        return hypotheses

    def _noter_sources_france(self, dalles, propriete_url):
        """Les sources de la chaine francaise : dalles, editions, masquage."""
        from . import donnees_ign
        from .import_dalles import geoplateforme

        editions = sorted({str((d.get("properties") or {}).get("timestamp") or "")
                           for d in dalles} - {""})
        self._fiche().setdefault("sources", {}).update({
            "terrain": {"service": "IGN Geoplateforme - LiDAR HD",
                        "couche": geoplateforme.TYPENAME_DALLES,
                        "produit": propriete_url, "dalles": len(dalles),
                        "editions": [editions[0], editions[-1]] if editions else []},
            "masquage": {"foret": donnees_ign.COUCHE_FORET,
                         "bati": donnees_ign.COUCHE_BATIMENTS},
            "attribution": "IGN - LiDAR HD, BD TOPO, BD Foret V2 - Licence "
                           "Ouverte 2.0 (Etalab)",
        })

    def _ecrire_provenance(self, result_dict):
        """Ecrit la fiche a cote de la ZIV, en JSON. Rend son chemin, ou "".

        Meilleur effort : une fiche manquante ne retire rien au resultat.
        """
        fiche = getattr(self, "_provenance", None)
        ziv = result_dict.get(self.ZIV_OUTPUT, "") if result_dict else ""
        if not fiche or not ziv:
            return ""
        jeu = None
        try:
            import numpy as np
            from osgeo import gdal

            fournisseur = getattr(self, "_fournisseur", None)
            fiche["fournisseur"] = {
                "id": getattr(fournisseur, "ID", ""),
                "nom": getattr(fournisseur, "NOM", ""),
                "epsg_calcul": getattr(self, "_epsg_travail", None),
            }
            # Par blocs de lignes : la ZIV d'un grand rayon pese des centaines
            # de Mo, la fiche n'a pas a les tenir en memoire d'un coup.
            gdal.UseExceptions()
            jeu = gdal.Open(str(ziv))
            bande = jeu.GetRasterBand(1)
            sans_donnee = bande.GetNoDataValue()
            calculees = vues = 0
            for debut in range(0, jeu.RasterYSize, 1024):
                hauteur = min(1024, jeu.RasterYSize - debut)
                bloc = np.asarray(bande.ReadAsArray(0, debut, jeu.RasterXSize, hauteur))
                valides = np.isfinite(bloc)
                if sans_donnee is not None:
                    valides &= ~np.isclose(bloc, sans_donnee)
                calculees += int(valides.sum())
                vues += int((valides & (bloc >= 1.0)).sum())
            fiche["resultat"] = {
                "ziv": str(ziv),
                "cellules_calculees": calculees,
                "cellules_vues": vues,
                "part_vue_pct": round(100.0 * vues / calculees, 2) if calculees else None,
                "valeurs": {
                    "sans-donnee": "hors du rayon d'analyse, jamais calcule",
                    "0": "non vu, y compris sous foret et bati masques",
                    "0.1": "emprise",
                    ">= 1": "nombre de points du contour qui voient la cellule",
                },
                "sorties": {cle: valeur for cle, valeur in result_dict.items()
                            if isinstance(valeur, str) and valeur},
            }
            fiche["avertissements"] = list(getattr(self.logger, "avertissements", []))
            chemin = Path(str(ziv)).with_name(Path(str(ziv)).stem + "_provenance.json")
            chemin.write_text(json.dumps(fiche, indent=1, ensure_ascii=False, default=str),
                              encoding="utf-8")
            self.logger.info("Fiche de provenance : %s" % chemin)
            return str(chemin)
        except Exception as erreur:
            self.logger.warning("Fiche de provenance non ecrite (%s)." % erreur)
            return ""
        finally:
            jeu = None

    def _finalize_result(self, result_dict: dict) -> dict:
        # Persist the tile manifest as the very first thing - even if the
        # ZIV step fails later, the tiles we've downloaded are still
        # valid and worth remembering for next time (audit V2).
        with contextlib.suppress(Exception):
            self._save_tile_manifest()

        """Post-download orchestration, executed in this strict order:

        1. Load merged DEM in QGIS + ask the user to validate the merge
           (catches missing tiles before the long ZIV computation runs)
        2. ZIV computation (if requested and validated)
        3. Toggle the merged DEM visibility OFF (not removed!) so the
           basemap shows through instead of the grey DEM
        4. Load the analysis buffer LAST so it sits on top of all layers
        """
        # ---- 0) Annulation : on s'arrete AVANT de produire quoi que ce soit --
        # Les gardes posees plus haut arretent bien les telechargements, mais
        # la chaine continuait jusqu'ici et fabriquait la ZIV, le rayon
        # d'analyse et les courbes de niveau — sur un terrain incomplet, et
        # sans que rien ne le dise. Un utilisateur l'a rapporte le 2026-09-14.
        # Une ZIV partielle ne se distingue pas d'une ZIV complete sur une
        # carte : mieux vaut ne rien rendre du tout.
        # Le dossier de travail est tout de meme nomme, pour que les dalles
        # deja telechargees restent trouvables — relancer ne doit pas tout
        # recommencer.
        if self.feedback.isCanceled():
            self.logger.info(
                "Annulation demandee : ni ZIV, ni rayon d'analyse, ni courbes "
                "de niveau ne sont produits. Les dalles deja telechargees "
                "restent en cache pour une prochaine execution.")
            return {"OUTPUT_DIRECTORY": result_dict.get("OUTPUT_DIRECTORY", "")}

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
        # Chaque sortie sans ZIV est un ECHEC : rendre le dictionnaire valait
        # « termine » pour Processing, avec un rayon et des courbes charges
        # autour d'une ZIV absente (lessons.md, 2026-09-11, regle 4).
        if getattr(self, "_ziv_requested", False):
            if not dem_path:
                raise QgsProcessingException(
                    "Aucun terrain produit : la ZIV ne peut pas etre calculee.")
            elif str(dem_path).lower().endswith(
                (".laz", ".las", ".copc.laz")
            ):
                raise QgsProcessingException(
                    "ZIV requires a DEM raster (MNT/MNS/MNH) - "
                    "cannot use point cloud output")
            elif not dem_is_valid_raster:
                raise QgsProcessingException(
                    f"ZIV step skipped - unsupported DEM format: {dem_path}")
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
                elif not self.feedback.isCanceled():
                    raise QgsProcessingException(
                        "Le modele de ZIV n'a rendu aucun raster "
                        "(voir les messages ci-dessus).")

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

        # Seconde garde : la ZIV est le poste long de la chaine, et c'est
        # pendant elle qu'on annule le plus souvent. Tout ce qui suit — rayon,
        # points de vue, courbes — decrit un resultat qui n'existe pas.
        # La ZIV, elle, a pu etre CHARGEE avant qu'on arrive ici : on la
        # retire, sinon une couche rescapee d'un calcul annule resterait au
        # projet sans que rien ne la distingue d'un resultat complet.
        if self.feedback.isCanceled():
            retirees = self._retirer_les_couches_du_calcul(result_dict)
            self.logger.info(
                "Annulation demandee pendant le calcul de la ZIV : ni rayon "
                "d'analyse, ni points de vue, ni courbes de niveau%s."
                % (", et %d couche(s) deja chargee(s) retiree(s)" % retirees
                   if retirees else ""))
            return {"OUTPUT_DIRECTORY": result_dict.get("OUTPUT_DIRECTORY", "")}

        # ---- 3) Buffer layer LAST (so it appears on top) ----
        buffer_layer = None
        buffer_shp = getattr(self, "_buffer_shp_path", "")
        radius = getattr(self, "_ziv_radius", 0.0) or 0.0
        # Sortie declaree, pour que Processing et les modeles voient le fichier.
        radius_written = getattr(self, "_radius_written_path", "")
        if radius_written:
            result_dict[self.RADIUS_OUTPUT] = radius_written
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

        # ---- 3ter) Courbes de niveau, si demandees ----
        # Croisees avec la ZIV, elles disent POURQUOI une zone voit ou ne voit
        # pas : sur un terrain plat, une ondulation d'un metre suffit a tout
        # decider, et sans courbes le resultat semble arbitraire.
        if getattr(self, "_courbes_demandees", False) and dem_path:
            chemin = self._produire_courbes(dem_path)
            if chemin:
                result_dict["COURBES_NIVEAU"] = chemin

        # ---- 4) Les livrables passent dans le systeme du projet -------------
        # En DERNIER, et pas avant : points de vue et courbes viennent d'etre
        # etablis sur les coordonnees du fuseau de calcul, qui sont les justes.
        self._livrer_dans_le_scr_du_projet(result_dict)

        # ---- 5) Et l'ordre de la legende, une fois tout en place ------------
        self._ordonner_les_couches(result_dict)

        # ---- 6) La fiche de provenance, sur le livrable definitif -----------
        self._ecrire_provenance(result_dict)

        return result_dict

    @staticmethod
    def _equidistance(amplitude):
        """Un beau nombre visant une quinzaine de courbes sur l'emprise.

        Une equidistance fixe ne convient pas : 5 m donnerait deux courbes sur
        une plaine et un plat de spaghettis en montagne. On vise 15 courbes,
        puis on arrondit au pas cartographique le plus proche par en dessous.
        """
        if not amplitude or amplitude <= 0:
            return 0.0
        PAS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 200.0)
        vise = amplitude / 15.0
        retenus = [p for p in PAS if p <= vise]
        if retenus:
            return retenus[-1]
        # Sous le plus petit pas cartographique, il n y a pas de courbe a
        # tracer : le dire vaut mieux que livrer un fichier vide.
        return PAS[0] if amplitude >= PAS[0] else 0.0

    def _produire_courbes(self, dem_path):
        """Ecrit les courbes de niveau du TERRAIN et les charge. Best-effort.

        Rien ici n'est bloquant : une ZIV sans courbes reste exploitable, une
        execution avortee pour une couche d'agrement ne le serait pas.

        Sur QUOI elles sont tirees decide de tout. Une courbe de niveau decrit
        le RELIEF ; la tirer d'une surface qui porte la canopee et les toits
        donne les creneaux carres et la nuee de micro-polygones rapportes le
        2026-09-11 — aux Etats-Unis la surface vegetale vient de LANDFIRE, dont
        la maille de 30 m se lit directement dans le dessin des lignes. On
        prefere donc le terrain nu mis de cote avant la gravure, et on DIT au
        journal sur quoi on a travaille quand ce n'est pas lui.
        """
        try:
            import numpy as np
            from osgeo import gdal, ogr, osr

            gdal.UseExceptions()
            terrain_nu = getattr(self, "_terrain_nu", "") or ""
            if terrain_nu and os.path.isfile(terrain_nu):
                base, origine = terrain_nu, "le terrain nu"
            elif getattr(self, "_courbes_sur_le_terrain", False):
                # Le produit de travail EST un terrain : rien a telecharger,
                # et surtout rien a signaler — avertir ici reviendrait a
                # accuser de canopee un fichier qui n'en porte pas.
                base = str(dem_path)
                origine = "le terrain demande au formulaire"
            else:
                base = str(dem_path)
                origine = ("la surface de travail (relief + ce que le produit "
                           "choisi contient : canopee, toitures)")
                self.logger.warning(
                    "Courbes de niveau tirees de %s, faute de terrain nu : "
                    "elles suivront la vegetation et le bati. Pour un trace "
                    "franc, demander le MNT comme type de donnees." % origine)

            jeu = gdal.Open(str(base))
            if jeu is None:
                self.logger.warning("Courbes de niveau : terrain illisible.")
                return ""
            bande = jeu.GetRasterBand(1)
            valeurs = bande.ReadAsArray().astype("float64")
            sans_donnee = bande.GetNoDataValue()
            if sans_donnee is not None:
                valeurs[valeurs == sans_donnee] = np.nan
            finies = valeurs[np.isfinite(valeurs)]
            if finies.size == 0:
                self.logger.warning("Courbes de niveau : terrain sans altitude.")
                return ""
            bas, haut = float(finies.min()), float(finies.max())
            pas = self._equidistance(haut - bas)
            if pas <= 0:
                self.logger.warning(
                    "Courbes de niveau : terrain trop plat (%.2f m d'amplitude)."
                    % (haut - bas))
                return ""

            dossier = Path(str(getattr(self, "_working_dir", "") or ""))
            if not str(dossier) or not dossier.exists():
                dossier = Path(dem_path).parent
            horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
            chemin = dossier / ("courbes_niveau_%gm_%s.gpkg" % (pas, horodatage))

            reference = osr.SpatialReference()
            reference.ImportFromWkt(jeu.GetProjection())

            # On genere d'abord EN MEMOIRE : le tri des coquilles se fait ainsi
            # a l'ecriture, sans supprimer d'entites dans le fichier livre.
            brouillon = ogr.GetDriverByName("Memory").CreateDataSource("courbes")
            provisoire = brouillon.CreateLayer("courbes", reference,
                                               ogr.wkbLineString)
            provisoire.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
            provisoire.CreateField(ogr.FieldDefn("altitude", ogr.OFTReal))
            gdal.ContourGenerate(bande, pas, 0.0, [], 1 if sans_donnee is not None
                                 else 0, sans_donnee or 0.0, provisoire, 0, 1)
            engendrees = provisoire.GetFeatureCount()

            # Le seuil des « coquilles » : ces boucles de quelques pixels qui
            # naissent du bruit du raster et encombrent le dessin sans rien
            # dire du relief. Il est exprime en PIXELS, seule echelle ou il
            # garde un sens d'une resolution a l'autre : en dessous de dix
            # cotes de cellule, un contour ne decrit plus une forme du terrain.
            pixel = abs(jeu.GetGeoTransform()[1]) or 1.0
            seuil = 10.0 * pixel

            pilote = ogr.GetDriverByName("GPKG")
            source = pilote.CreateDataSource(str(chemin))
            couche = source.CreateLayer("courbes", reference, ogr.wkbLineString)
            couche.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
            couche.CreateField(ogr.FieldDefn("altitude", ogr.OFTReal))
            definition = couche.GetLayerDefn()
            nombre = 0
            provisoire.ResetReading()
            for entite in provisoire:
                geometrie = entite.GetGeometryRef()
                if geometrie is None or geometrie.Length() < seuil:
                    continue
                gardee = ogr.Feature(definition)
                gardee.SetGeometry(geometrie.Clone())
                gardee.SetField("id", nombre)
                gardee.SetField("altitude", entite.GetField("altitude"))
                couche.CreateFeature(gardee)
                gardee = None
                nombre += 1
            provisoire = None
            brouillon = None
            couche = None
            source = None
            jeu = None

            self.logger.info(
                "Courbes de niveau : %d courbe(s) tous les %g m sur %s "
                "(amplitude %.1f m), %d coquille(s) de moins de %.0f m "
                "ecartee(s) -> %s"
                % (nombre, pas, origine, haut - bas, engendrees - nombre,
                   seuil, chemin.name))

            couche_qgis = QgsVectorLayer(str(chemin), "Courbes de niveau (%g m)"
                                         % pas, "ogr")
            if couche_qgis.isValid():
                self._habiller_courbes(couche_qgis)
                QgsProject.instance().addMapLayer(couche_qgis)
            else:
                self.logger.warning("Courbes de niveau : couche non chargeable.")
            return str(chemin)
        except Exception as erreur:
            self.logger.warning("Courbes de niveau : echec (%s)." % erreur)
            return ""

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
        """Force « hors visibilité » sous les forêts et les bâtiments.

        Délégué à `masquage.py`, écrit pour ce greffon. Sur un MNS, la canopée
        et les toitures « voient » le projet alors que personne ne s'y tient :
        ces cellules sont des artefacts.

        Meilleur effort : hors couverture, WFS indisponible ou erreur GDAL, on
        prévient et la ZIV est rendue telle quelle.

        Deux natures de masque, et c'est assumé. La France masque par des
        empreintes **vectorielles** (BD Forêt, BDTOPO) ; les États-Unis par un
        **seuil sur le raster** de hauteur déjà rapatrié pour l'occultation, ce
        qui évite de vectoriser un raster pour rien et garantit par
        construction que surface et masque ne se contredisent pas en lisière.
        """
        fournisseur = getattr(self, "_fournisseur", None)
        if fournisseur is not None and fournisseur.occulte():
            return fournisseur.masquer_resultat(
                ziv_path, requested_output,
                str(getattr(self, "_working_dir", "")), journal=self.logger)

        from . import masquage

        return masquage.masquer_foret_et_bati(
            ziv_path, requested_output, self.logger
        )

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

            from . import donnees_ign

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
            # Hors de France, routes et bati viennent du fournisseur : TIGER/Line
            # du Census et USA Structures. Le WFS de la Geoplateforme ne rendrait
            # rien sur une emprise americaine.
            fournisseur = getattr(self, "_fournisseur", None)
            par_fournisseur = fournisseur is not None and fournisseur.occulte()

            cand_sources = []
            roads_geojson = work_dir / "_pv_routes.geojson"
            if par_fournisseur:
                routes_obtenues = fournisseur.routes(
                    (extent.xMinimum(), extent.yMinimum(),
                     extent.xMaximum(), extent.yMaximum()),
                    getattr(self, "_epsg_travail", 2154),
                    roads_geojson, journal=self.logger)
            else:
                routes_obtenues = donnees_ign.telecharger_couche(
                    extent, donnees_ign.COUCHE_ROUTES, roads_geojson, self.logger
                )
            if routes_obtenues:
                cand_sources.append(str(roads_geojson))
            # Buildings: reuse the footprints already downloaded by the
            # landcover masking step when present, else download.
            bati_geojson = Path(ziv_path).parent / "_ziv_mask_batiments.geojson"
            if not bati_geojson.exists():
                if par_fournisseur:
                    # Le fournisseur a ecrit le bati dans le dossier de travail,
                    # sous ce meme nom. S'il n'y est pas, l'occultation a echoue :
                    # on s'en passe plutot que d'interroger un service qui ne
                    # couvre pas ce pays.
                    candidat = work_dir / "_ziv_mask_batiments.geojson"
                    bati_geojson = candidat if candidat.exists() else None
                else:
                    bati_geojson = work_dir / "_pv_batiments.geojson"
                    if not donnees_ign.telecharger_couche(
                        extent, donnees_ign.COUCHE_BATIMENTS, bati_geojson,
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

                    # Le membre `crs` est indispensable : sans lui OGR lit ce
                    # GeoJSON en EPSG:4326 et la rasterisation ne touche AUCUNE
                    # cellule, tout en rendant True.
                    gj = {
                        "type": "FeatureCollection",
                        "crs": {"type": "name", "properties": {
                            "name": "urn:ogc:def:crs:EPSG::%d"
                                    % getattr(self, "_epsg_travail", 2154)}},
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
            crs = QgsCoordinateReferenceSystem(
                "EPSG:%d" % getattr(self, "_epsg_travail", 2154))
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
                str(shp), fields, QgsWkbTypes.Type.Point, crs,
                QgsCoordinateTransformContext(), options,
            )
            if writer is None or writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
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
            with contextlib.suppress(Exception):
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
            with contextlib.suppress(Exception):
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
            skipped = 0
            for f in lyr:
                g = f.GetGeometryRef()
                if g is None:
                    continue
                try:
                    b = g.Buffer(float(dist_m))
                except Exception:
                    skipped += 1
                    continue
                nf = ogr.Feature(out.GetLayerDefn())
                nf.SetGeometry(b)
                out.CreateFeature(nf)
                nf = None
                n += 1
            dst = None
            src = None
            if skipped:
                self.logger.warning(
                    f"Points de vue : {skipped} entite(s) non tamponnee(s) par OGR."
                )
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
                with contextlib.suppress(Exception):
                    url_path = url.split("?")[0]
                    filename = url_path.split("/")[-1]

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
        with contextlib.suppress(Exception):
            lock = getattr(self, "_tile_manifest_lock", None)
            if lock is not None:
                with lock:
                    self._tile_manifest.pop(file_path.name, None)
            else:
                self._tile_manifest.pop(file_path.name, None)

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
        """Découverte des dalles — déléguée au moteur d'import embarqué.

        L'AOI est déjà reprojetée dans le SCR de travail par processAlgorithm —
        Lambert-93 en France, UTM aux États-Unis. Le contrat
        de sortie est inchangé (`url` / `name` / `geometry` / `properties`), et
        l'erreur reste non bloquante : une liste vide, et le motif au journal.
        """
        bbox = aoi_geometry.boundingBox()
        emprise = (bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum())
        # Remis a zero a chaque decouverte : un motif garde d'une execution
        # precedente etiquetterait a tort un vrai trou de couverture en panne.
        self._echec_wfs = ""
        try:
            return import_dalles.dalles_geoplateforme(
                emprise, data_type_code, journal=self.logger
            )
        except Exception as e:
            # Le motif est RETENU, pas seulement journalise : sans lui, une
            # panne du service est annoncee a l'utilisateur comme une « zone
            # non couverte ». C'est ce message trompeur qui a masque le retrait
            # des couches de dalles par l'IGN le 2026-09-09 : le greffon publie
            # accusait la couverture LiDAR alors que le WFS repondait HTTP 400.
            self._echec_wfs = str(e)
            self.logger.error("Interrogation du WFS impossible : %s" % e)
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
            # Lu ICI, bien avant `_choisir_fournisseur` : c'est cette valeur qui
            # decide si la detection s'applique ou si elle est outrepassee.
            indice_pays = self.parameterAsEnum(parameters, self.PAYS, context)
            self._pays_impose = (
                self.PAYS_IDS[indice_pays]
                if 0 <= indice_pays < len(self.PAYS_IDS) else None
            )
            data_type = self.parameterAsEnum(parameters, self.DATA_TYPE, context)
            load_layer = self.parameterAsBool(parameters, self.LOAD_LAYER, context)
            merge_tiles_flag = self.parameterAsBool(
                parameters, self.MERGE_TILES, context
            )
            # La ZIV est la raison d'etre du greffon : elle n'est plus
            # optionnelle.
            create_ziv = True
            self._courbes_demandees = self.parameterAsBool(
                parameters, self.COURBES_NIVEAU, context)
            self.logger.info("Resolution de travail de la ZIV : %g m"
                             % self.ZIV_TARGET_RES_M)
            ziv_radius = self.parameterAsDouble(parameters, self.ZIV_RADIUS, context)
            ziv_panel_height = self.parameterAsDouble(
                parameters, self.ZIV_PANEL_HEIGHT, context
            )
            ziv_output_param = self.parameterAsOutputLayer(
                parameters, self.ZIV_OUTPUT, context
            )
            # Destination choisie pour la couche du rayon d'analyse. Vide : on
            # retombe sur le dossier de travail et le nom horodate.
            self._radius_output_path = (
                self.parameterAsOutputLayer(parameters, self.RADIUS_OUTPUT, context)
                or ""
            )

            # ---- Derive working directory (the old "Output folder") ----
            # Priority order:
            #   1) the directory of the requested ZIV output raster, when set
            #   2) a temporary directory unique to this run
            self._is_temporary_output = not bool(ziv_output_param)
            if ziv_output_param:
                output_folder = Path(ziv_output_param).parent
            else:
                # Le dossier temporaire de Processing, et non %TEMP% : QGIS le
                # vide lui-meme a la fermeture, comme pour toute sortie
                # temporaire. Sous %TEMP%, chaque calcul laissait son dossier
                # ZIV_* — 38 dossiers et 901 Mo le 2026-10-02 sur un poste.
                from qgis.core import QgsProcessingUtils

                output_folder = Path(QgsProcessingUtils.tempFolder()) / (
                    f"ZIV_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
            try:
                output_folder.mkdir(parents=True, exist_ok=True)
            except Exception as mk_err:
                raise QgsProcessingException(
                    f"Cannot create working directory {output_folder}: {mk_err}"
                ) from mk_err
            self._working_dir = output_folder

            # The analysis radius is used both for buffering the AOI before WFS
            # query AND for the ZIV analysis radius - single user input.
            buffer_distance = ziv_radius

            # Merge strategy is now driven only by the checkbox (no enum exposed)
            # La case le decide seule. Y ajouter `create_ziv`, devenu
            # toujours vrai depuis que la ZIV n'est plus optionnelle, rendait
            # la case inoperante et le journal menteur.
            merge_strategy = 1 if merge_tiles_flag else 0

            # No concurrency limit exposed to the user - cap to a sane value to
            # avoid creating thousands of threads on huge tile sets.
            max_downloads = 32

            # Sanity check: the embedded visibility provider should be loaded
            # at this point (it is registered alongside the main provider).
            if create_ziv:
                with contextlib.suppress(Exception):
                    reg = QgsApplication.processingRegistry()
                    if reg.algorithmById("pcfr_vis:viewshed") is None:
                        self.logger.warning(
                            "Embedded visibility provider not found - "
                            "ZIV computation may fail."
                        )

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
            self._ouvrir_provenance(data_type, ziv_radius, ziv_panel_height)
            # Pass the AOI to the ZIV model AS-IS (path, layer id or layer
            # object - QGIS's processing.run knows how to resolve all of
            # these for a "vector" parameter). The previous, more elaborate
            # FeatureSourceDefinition wrapping introduced regressions on
            # some QGIS versions and was reverted in v2.1.1.
            self._ziv_zip_source = parameters.get(self.INPUT)
            # Retenu pour resoudre la ZIP en couche au moment du calage.
            self._contexte_processing = context
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
                raise QgsProcessingException(f"Invalid data type: {data_type}")

            # Log processing parameters
            self.logger.info(f"Processing parameters:")
            self.logger.info(f"- Data type: {self.DATA_TYPE_OPTIONS[data_type]}")
            self.logger.info(f"- WFS code: {data_type_code}")
            self.logger.info(f"- Working directory: {output_folder}")
            if self._is_temporary_output:
                self.logger.info(
                    "  (no output path supplied - using a temporary directory)"
                )
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
                raise QgsProcessingException(
                    "Espace disque insuffisant dans %s : il faut au moins %d Mo "
                    "libres." % (output_folder, self.MIN_DISK_SPACE_MB))

            # Process AOI
            features = list(source.getFeatures())
            if not features:
                raise QgsProcessingException("No features found in input layer")

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

            # ---- Choix du pays ---------------------------------------
            # ICI et pas ailleurs : c'est le DERNIER instant ou l'emprise est
            # encore dans son systeme d'origine, et le premier ou l'on connait
            # a la fois la geometrie et ce systeme. Deux lignes plus bas,
            # `transform()` mute la geometrie EN PLACE et l'emprise en degres
            # n'est plus reconstituable.
            #
            # Le centroide est calcule sur une COPIE, pour la meme raison.
            self._choisir_fournisseur(aoi_geometry, source_crs)
            # Un fournisseur qui reconstruit son terrain ne livre qu'une
            # SURFACE : terrain nu, vegetation et bati graves. Lui demander un
            # MNT, un MNH ou un nuage de points rendait en silence cette
            # surface sous le nom demande (audit du 2026-10-02).
            if self._fournisseur.fournit_terrain() and data_type != 1:
                raise QgsProcessingException(
                    "%s : seul le type MNS est disponible ici (le terrain y "
                    "est reconstruit avec la vegetation et le bati). Choisir "
                    "le MNS." % self._fournisseur.NOM)
            scr_travail = QgsCoordinateReferenceSystem(
                "EPSG:%d" % self._epsg_travail)

            # Transform to the provider's working CRS if needed
            if source_crs.authid() != scr_travail.authid():
                self.logger.info(
                    f"Transforming geometry from {source_crs.authid()} "
                    f"to {scr_travail.authid()}"
                )
                transform = QgsCoordinateTransform(
                    source_crs,
                    scr_travail,
                    QgsProject.instance(),
                )
                aoi_geometry.transform(transform)

            # Sanity check: warn early if the AOI clearly sits outside the
            # French metropolitan footprint (Lambert-93 bbox approx).
            # IGN's LiDAR HD covers France + DROM-COM, but a rough metropole
            # bbox is enough to surface the most common mistake: AOI in
            # WGS84 longitude/latitude or in another country (audit fix M-15).
            #
            # Ne vaut QUE pour la France : cette boite est en Lambert-93, et une
            # emprise americaine en UTM n'y tomberait jamais. Hors de France, le
            # refus est deja venu de `choisir()`, qui enumere les couvertures
            # testees — un message plus utile que celui-ci.
            if self._epsg_travail == 2154:
                METRO_BBOX = QgsRectangle(0.0, 6.0e6, 1.3e6, 7.2e6)
                with contextlib.suppress(Exception):
                    if not aoi_geometry.boundingBox().intersects(METRO_BBOX):
                        self.logger.warning(
                            "L'emprise ne semble PAS recouvrir la France "
                            "métropolitaine en Lambert-93 - vérifiez le SCR "
                            "source de votre ZIP. La requête WFS pourrait "
                            "ne renvoyer aucune dalle."
                        )

            # Keep the project centroid AND geometry (pre-buffer, in the
            # working CRS) - used by the photo-viewpoints generator for
            # the distance rings and the azimuth field.
            try:
                self._aoi_centroid = aoi_geometry.centroid().asPoint()
            except Exception:
                self._aoi_centroid = None
            try:
                self._aoi_geom = QgsGeometry(aoi_geometry)
            except Exception:
                self._aoi_geom = None

            # Apply buffer around AOI (in meters, native unit of the working CRS)
            buffer_geom = None
            if buffer_distance and buffer_distance > 0:
                self.logger.info(
                    f"Applying {buffer_distance} m buffer around the AOI "
                    f"(EPSG:{self._epsg_travail})"
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
            # Chemin reellement ecrit, distinct de _buffer_shp_path qui, lui,
            # ne sert qu'au chargement dans le projet : la sortie declaree doit
            # etre rendue meme quand le chargement des couches est decoche.
            self._radius_written_path = ""
            if buffer_geom is not None:
                shp_path = self._save_buffer_shapefile(
                    buffer_geom,
                    self._working_dir,
                    buffer_distance,
                    getattr(self, "_radius_output_path", ""),
                )
                self._radius_written_path = shp_path
                if shp_path and load_layer:
                    self._buffer_shp_path = shp_path

            # ---- Branche du fournisseur -----------------------------------
            # Un fournisseur qui fournit son terrain remplace TOUT le bloc de
            # decouverte ci-dessous — listage WFS, filtrage geometrique,
            # avertissements de volume, import — par un appel unique. C'est le
            # cas des Etats-Unis : le service 3DEP est transparent, il n'y a
            # aucune dalle a decouvrir.
            #
            # La branche est posee ICI, avant la premiere requete, pour que la
            # chaine francaise qui suit reste inchangee a l'octet : on ne la
            # traverse simplement pas.
            if self._fournisseur.fournit_terrain():
                return self._terrain_par_fournisseur(
                    aoi_geometry, downloads_dir, load_layer)

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
                echec = getattr(self, "_echec_wfs", "")
                if echec:
                    detail_journal = (
                        "Le service de dalles n'a pas repondu la liste : %s. "
                        "Ce n'est PAS un defaut de couverture : verifier le "
                        "reseau, puis que les codes de couche sont toujours ceux "
                        "que le service annonce (scripts/veiller_couches_ign.py)."
                        % echec
                    )
                    detail_barre = (
                        "Le service de dalles n'a pas repondu - impossible de "
                        "savoir si la zone est couverte."
                    )
                else:
                    detail_journal = (
                        "Aucune dalle LiDAR HD sur l'AOI - zone non couverte "
                        "par le LiDAR HD IGN (couverture ~80 % de la métropole)."
                    )
                    detail_barre = (
                        "Aucune dalle LiDAR HD ne couvre cette zone - "
                        "impossible de calculer la ZIV ici."
                    )
                with contextlib.suppress(Exception):
                    from qgis.utils import iface as _iface

                    if _iface is not None:
                        _iface.messageBar().pushMessage(
                            PLUGIN_DISPLAY_NAME,
                            detail_barre,
                            level=Qgis.MessageLevel.Warning,
                            duration=10,
                        )
                # Zone non couverte ou service muet : dans les deux cas, pas de
                # ZIV — un echec, chacun avec SON motif (lessons.md, 2026-09-10).
                raise QgsProcessingException(detail_journal)

            # Warn (don't block) when the user is about to download a lot of tiles
            if len(intersecting_tiles) > self.MAX_TILES_RECOMMENDED:
                self.logger.warning(
                    f"Found {len(intersecting_tiles)} tiles - this exceeds "
                    f"the recommended limit of {self.MAX_TILES_RECOMMENDED}. "
                    "Consider splitting your AOI into smaller chunks."
                )

            # All intersecting tiles are downloaded (no "best tile" mode)
            selected_tiles = self._select_best_tiles(intersecting_tiles)
            self._noter_sources_france(selected_tiles, data_type_code)

            # --- Import des produits raster : moteur embarque ----------------
            # MNT / MNS / MNH passent par import_dalles : dalles demandees au serveur
            # a la resolution de travail (16 fois moins de volume a 2 m),
            # georeferencement corrige, mosaique. Le nuage de points (type 3)
            # garde le chemin d'origine : il ne s'assemble pas comme un raster.
            if import_dalles.est_raster(data_type):
                try:
                    resultat = import_dalles.importer_raster(
                        self,
                        selected_tiles,
                        downloads_dir,
                        data_type,
                        self.ZIV_TARGET_RES_M,
                        fusionner=merge_strategy == 1,
                        charger_couches=load_layer,
                    )
                    # Les courbes de niveau se tirent du TERRAIN. Quand le
                    # produit demande est une SURFACE, on va chercher le MNT
                    # des memes dalles — sans quoi les isolignes epousent la
                    # canopee et les toits. Cote americain le terrain nu est
                    # simplement mis de cote avant la gravure ; ici il faut le
                    # demander, la chaine francaise ne telechargeant que le
                    # produit choisi.
                    self._terrain_nu = self._mnt_pour_les_courbes(
                        aoi_geometry, selected_tiles, downloads_dir, data_type)
                except Exception as e:
                    raise QgsProcessingException(
                        "Import des dalles impossible : %s" % e) from e
                # Hors du `try` : un echec de la ZIV n'est pas un echec de
                # l'import, et ne doit pas en emprunter le message.
                return self._finalize_result(resultat)


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
                raise QgsProcessingException(
                    f"Estimated download size = {required_mb} MB - "
                    "not enough free space in the working directory.")

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
                            # Le formulaire ne propose plus de forcer le
                            # re-telechargement : une dalle deja tenue et
                            # saine est reprise du cache.
                            False,
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
                raise QgsProcessingException("No files were successfully downloaded")

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

        except QgsProcessingException as e:
            # Levee volontairement, deja journalisee la ou elle est nee : QGIS
            # l'affiche en rouge et marque l'execution en echec. La convertir en
            # dictionnaire vide, comme ci-dessous, la ferait passer pour reussie.
            self.logger.error("Traitement interrompu : %s" % e)
            raise
        except Exception as e:
            self.logger.error(f"Error in main processing: {str(e)}")
            import traceback

            self.logger.error(traceback.format_exc())
            # Rendre un dictionnaire ici valait « termine avec succes » pour
            # Processing : le greffon affichait « finished » sur un calcul qui
            # n'avait rien produit. Seule une exception marque l'echec.
            raise QgsProcessingException(str(e)) from e
        finally:
            # Final cleanup
            self._cleanup_temp_files()
