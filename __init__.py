"""
ZIV_prime - plugin entry point (classFactory, toolbar, dependency check).

Part of ZIV_prime, a GNU GPL v3 (or later) licensed QGIS plugin by G. DELSIGNE.

This package covers TWO countries: France (IGN LiDAR HD) and the contiguous
United States (USGS 3DEP, LANDFIRE, USA Structures, TIGER/Line). It grew as its own
package from version 1.5.3 of this plugin, and was folded back in on
2026-09-17 under the original identities, so that the people already using it
receive an update rather than meeting a stranger.

The tile import (``import_dalles/``), the visibility engine
(``moteur_visibilite/``) and the two Processing algorithms (``algorithmes_ziv/``)
are this plugin's own code - no third-party code remains in this package.

See ``LICENSE`` and ``README.md`` at the root of the plugin for the full terms.
"""
import contextlib
import importlib.util
import shutil
import tempfile
from pathlib import Path

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QToolButton

# Plugin version
VERSION = "1.7.0"
# Nom affiche a l'utilisateur (fenetres, menu, journal).
PLUGIN_NAME = "Zones d'influence visuelle (ZIV)"
# Nom de paquet : dossier installe, espace QgsSettings, dossier temporaire
# et contexte de traduction Qt. NE JAMAIS renommer : cela orphelinerait les
# preferences stockees et changerait le chemin d'installation. La ligne
# bi-pays a eu le sien un temps ; elle a repris celui-ci le 2026-09-17, ce qui
# rend aux utilisateurs leurs reglages d'origine.
PLUGIN_PACKAGE = "ZIV_prime"
PLUGIN_DIR = Path(__file__).parent


class PluginSettings:
    """Class to manage plugin settings."""

    def __init__(self):
        self.settings = QgsSettings()
        self.settings_prefix = f"plugins/{PLUGIN_PACKAGE}/"

    def get(self, key: str, default=None):
        """Get a setting value."""
        return self.settings.value(f"{self.settings_prefix}{key}", default)

    def set(self, key: str, value):
        """Set a setting value."""
        self.settings.setValue(f"{self.settings_prefix}{key}", value)

    def remove(self, key: str):
        """Remove a setting."""
        self.settings.remove(f"{self.settings_prefix}{key}")


class DependencyChecker:
    """Verifie que les deux dependances Python du greffon sont importables.

    `requests` et `numpy` sont livres avec QGIS : cette verification ne
    devrait jamais echouer.

    Historique : jusqu'a la 1.2.0 le greffon installait lui-meme ces paquets,
    en lancant un script batch livre dans le paquet, qui appelait
    l'installateur de paquets Python.
    plugins.qgis.org refuse cela sur deux regles critiques a la fois -
    FILE_SUSPICIOUS (un greffon ne livre pas de script executable) et B603 /
    B607 (execution de processus). Installer des paquets dans
    l'environnement Python de QGIS a la place de l'utilisateur n'etait de
    toute facon pas acceptable : on se contente desormais de signaler.
    """

    REQUIRED = ("requests", "numpy")

    def missing(self) -> list:
        """Noms des paquets requis qui ne sont pas importables."""
        absent = []
        for name in self.REQUIRED:
            try:
                if importlib.util.find_spec(name) is None:
                    absent.append(name)
            except (ImportError, ValueError):
                absent.append(name)
        return absent


def show_error_message(message: str, title: str = "Error"):
    """Show error message to user."""
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Critical)
    msg.setText(title)
    msg.setInformativeText(message)
    msg.setWindowTitle(f"{PLUGIN_NAME} - Erreur")
    msg.exec()


def show_info_message(message: str, title: str = "Information"):
    """Show information message to user."""
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Information)
    msg.setText(title)
    msg.setInformativeText(message)
    msg.setWindowTitle(PLUGIN_NAME)
    msg.exec()


class LidarPlugin:
    """Main plugin class."""

    @staticmethod
    def tr(message: str) -> str:
        """Translate a string with Qt's translator."""
        return QCoreApplication.translate(PLUGIN_PACKAGE, message)

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.visibility_provider = None
        self.action_ziv = None       # toolbar/menu action for ZIV
        self.action_prefs = None     # plugin preferences
        self.tool_button = None      # toolbar button (ZIV)
        self.tool_button_action = None
        self.menu_name = self.tr("&Zones d'influence visuelle (ZIV)")
        self.settings = PluginSettings()

        # Initialize plugin directory
        self.plugin_dir = Path(__file__).parent

        # Langue : le reglage du greffon l'emporte sur celui de QGIS quand
        # l'utilisateur a clique, sinon on suit QGIS. Le choix vit dans
        # `langue.py`, partage avec les boutons FR/EN du formulaire — deux
        # facons de choisir la langue finiraient par diverger.
        from . import langue as langue_module

        langue_module.appliquer(langue_module.retenue())

        QgsMessageLog.logMessage(
            f"{PLUGIN_NAME} plugin initialized", PLUGIN_NAME, Qgis.MessageLevel.Info
        )

    def _register_provider_safe(self, attr_name, provider_class, registry):
        """Add a Processing provider, recovering from common failure modes:

          * `wrapped C/C++ object deleted` on the Python reference -
            happens when a previous unload() partially failed and left a
            stale Python reference. We detect it by touching any
            C++-backed attribute and reset to None on RuntimeError.
          * A stale provider with the same id() still in the registry -
            remove it before adding ours.

        Each sub-step is in its own try/except: a single failing call
        cannot prevent the rest of the plugin from loading (cf. v3.5.5
        fix for the install popup blocking new users).
        """
        # 1) Is the existing Python reference still alive?
        existing = getattr(self, attr_name, None)
        if existing is not None:
            try:
                _ = existing.id()
                QgsMessageLog.logMessage(
                    f"{attr_name} already registered; skipping.",
                    PLUGIN_NAME,
                    Qgis.MessageLevel.Info,
                )
                return existing
            except (RuntimeError, AttributeError) as e:
                QgsMessageLog.logMessage(
                    f"{attr_name} stale (wrapped C/C++ deleted: {e}) - "
                    "rebuilding.",
                    PLUGIN_NAME,
                    Qgis.MessageLevel.Warning,
                )
                setattr(self, attr_name, None)

        # 2) Instantiate
        try:
            instance = provider_class()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Could not instantiate {provider_class.__name__}: {e}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Critical,
            )
            return None

        # 3) Remove any stale provider with the same id (left over from
        # a broken unload of an older install)
        with contextlib.suppress(Exception):
            provider_id = instance.id()
            stale = registry.providerById(provider_id)
            if stale is not None and stale is not instance:
                QgsMessageLog.logMessage(
                    f"Removing stale provider '{provider_id}' from registry.",
                    PLUGIN_NAME,
                    Qgis.MessageLevel.Warning,
                )
                with contextlib.suppress(Exception):
                    registry.removeProvider(stale)

        # 4) Register the fresh instance
        try:
            registry.addProvider(instance)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"addProvider({attr_name}) failed: {e}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Warning,
            )
            return None

        # 5) Refresh algorithms - never let this break the load
        try:
            instance.refreshAlgorithms()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"refreshAlgorithms({attr_name}) failed silently: {e}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Warning,
            )

        setattr(self, attr_name, instance)
        QgsMessageLog.logMessage(
            f"{attr_name} registered successfully.", PLUGIN_NAME, Qgis.MessageLevel.Info
        )
        return instance

    def initGui(self):
        """Initialize plugin GUI.

        Defensive load: each major step (visibility provider, main
        provider, toolbar, menu actions) is wrapped in its own try/except
        so a partial failure cannot prevent the plugin from appearing.
        No error popups - everything goes to the QGIS message log under
        the plugin name tag.
        """
        QgsMessageLog.logMessage(
            f"Initializing {PLUGIN_NAME} GUI", PLUGIN_NAME, Qgis.MessageLevel.Info
        )
        try:
            registry = QgsApplication.processingRegistry()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Could not reach Processing registry: {e}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Critical,
            )
            registry = None

        # ---- Visibility provider (registered FIRST so the main model can
        # resolve `pcfr_vis:*` algorithm ids when it is loaded) ----
        if registry is not None:
            try:
                from .visibility_provider import ZivVisibilityProvider

                self._register_provider_safe(
                    "visibility_provider", ZivVisibilityProvider, registry
                )
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"Visibility provider init skipped: {e}",
                    PLUGIN_NAME,
                    Qgis.MessageLevel.Warning,
                )

        # ---- Main ZIV_prime provider ----
        if registry is not None:
            try:
                from .lidar_provider import LidarProcessingProvider

                self._register_provider_safe(
                    "provider", LidarProcessingProvider, registry
                )
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"Main provider init skipped: {e}",
                    PLUGIN_NAME,
                    Qgis.MessageLevel.Warning,
                )

        # ---- Toolbar button (ZIV) ----
        try:
            if self.tool_button is None:
                icon = QIcon(str(self.plugin_dir / "icon.png"))

                # ZIV action (clic direct sur le bouton)
                self.action_ziv = QAction(
                    icon, self.tr("ZIV"), self.iface.mainWindow()
                )
                self.action_ziv.setToolTip(
                    self.tr(
                        "Zones d'influence visuelle (ZIV): downloads national LiDAR tiles and computes visibility"
                    )
                )
                self.action_ziv.triggered.connect(self.run_ziv)

                # QToolButton attaché à la barre d'outils Plugins
                self.tool_button = QToolButton(self.iface.mainWindow())
                self.tool_button.setIcon(icon)
                self.tool_button.setDefaultAction(self.action_ziv)

                self.tool_button_action = self.iface.addToolBarWidget(
                    self.tool_button
                )

                # Entrée dans Extensions -> ZIV_prime
                self.iface.addPluginToMenu(self.menu_name, self.action_ziv)

                # Préférences globales (menu uniquement, pas d'icône barre
                # d'outils pour ne pas l'encombrer).
                self.action_prefs = QAction(
                    self.tr('⚙ Preferences…'), self.iface.mainWindow()
                )
                self.action_prefs.setToolTip(
                    self.tr(
                        'Configure the plugin defaults: viewshed symbology and log folder.'
                    )
                )
                self.action_prefs.triggered.connect(self.run_preferences)
                self.iface.addPluginToMenu(self.menu_name, self.action_prefs)

        except Exception as e:
            # Toolbar setup failure: log only, never popup. The provider
            # is still registered, so the user can still launch the algos
            # from the Processing toolbox even without our toolbar button.
            QgsMessageLog.logMessage(
                f"Toolbar init failed (algos still available via "
                f"Processing toolbox): {e}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Warning,
            )

    def _open_algorithm_dialog(self, algorithm_id: str, label: str):
        """Generic helper to open a Processing algorithm dialog by id."""
        try:
            import processing

            processing.execAlgorithmDialog(algorithm_id, {})
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Could not open '{label}' dialog: {e}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Critical,
            )
            show_error_message(
                f"Impossible d'ouvrir la boîte de dialogue '{label}' :\n{e}"
            )

    def run_ziv(self):
        """Open the ZIV (download + viewshed) algorithm dialog."""
        self._open_algorithm_dialog("genziv:generate", "ZIV")

    def run_preferences(self):
        """Open the ZIV_prime preferences dialog."""
        try:
            from .preferences_dialog import PreferencesDialog

            dlg = PreferencesDialog(parent=self.iface.mainWindow())
            dlg.exec()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Could not open Preferences dialog: {e}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Critical,
            )
            show_error_message(f"Préférences indisponibles : {e}")

    def unload(self):
        """Unload the plugin.

        Every sub-step is isolated in its own try/except, so a failing
        close never leaves the Processing providers in the registry -
        that would cause a 'wrapped C/C++ object deleted' error on the
        next install.
        """
        QgsMessageLog.logMessage(
            f"Unloading {PLUGIN_NAME} plugin", PLUGIN_NAME, Qgis.MessageLevel.Info
        )

        # 1) Remove toolbar / menu entries
        try:
            for act in (self.action_ziv, self.action_prefs):
                if act is None:
                    continue
                with contextlib.suppress(Exception):
                    self.iface.removePluginMenu(self.menu_name, act)
            if self.tool_button_action is not None:
                with contextlib.suppress(Exception):
                    self.iface.removeToolBarIcon(self.tool_button_action)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"unload(toolbar) failed silently: {e}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Warning,
            )
        finally:
            self.action_ziv = None
            self.action_prefs = None
            self.tool_button = None
            self.tool_button_action = None

        # 2) Remove Processing providers - THE CRITICAL STEP. Each
        # provider is removed in its own try/except so a stale main
        # provider does not prevent the visibility provider from being
        # cleaned (or vice-versa).
        try:
            registry = QgsApplication.processingRegistry()
        except Exception:
            registry = None
        if registry is not None:
            for attr_name in ("provider", "visibility_provider"):
                provider = getattr(self, attr_name, None)
                if provider is None:
                    continue
                try:
                    registry.removeProvider(provider)
                    QgsMessageLog.logMessage(
                        f"{attr_name} removed from registry.",
                        PLUGIN_NAME,
                        Qgis.MessageLevel.Info,
                    )
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"removeProvider({attr_name}) failed silently: {e}",
                        PLUGIN_NAME,
                        Qgis.MessageLevel.Warning,
                    )
                finally:
                    setattr(self, attr_name, None)

        # 3) Clear version setting (but keep ever_installed flag)
        with contextlib.suppress(Exception):
            self.settings.remove("version")

        # 4) Cleanup any temporary files
        try:
            temp_dir = Path(tempfile.gettempdir()) / PLUGIN_PACKAGE
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error cleaning temporary files: {str(e)}",
                PLUGIN_NAME,
                Qgis.MessageLevel.Warning,
            )


def classFactory(iface):
    """Load the ZIV_prime plugin.

    Dependency installation is NON-BLOCKING. The two required packages
    (`requests`, `numpy`) ship with QGIS by default, so 99 % of installs
    find nothing to do. If pip really cannot run (no network, no admin
    rights…) we still load the plugin - the user can re-install missing
    packages later and reload QGIS.
    """
    try:
        settings = PluginSettings()
        ever_installed = settings.get("ever_installed", False)
    except Exception:
        settings = None
        ever_installed = False

    # Report missing dependencies - never install anything, never block the load.
    try:
        absent = DependencyChecker().missing()
        if absent:
            QgsMessageLog.logMessage(
                f"Missing Python packages: {', '.join(absent)}. They normally "
                "ship with QGIS; install them in the QGIS Python environment "
                "if a feature fails at runtime. The plugin loads anyway.",
                PLUGIN_NAME,
                Qgis.MessageLevel.Warning,
            )
    except Exception as e:
        QgsMessageLog.logMessage(
            f"Dependency check failed ({e}); loading plugin anyway.",
            PLUGIN_NAME,
            Qgis.MessageLevel.Warning,
        )

    # Show welcome message only on first ever installation
    with contextlib.suppress(Exception):
        if settings is not None and not ever_installed:
            show_info_message(
                f"Bienvenue dans {PLUGIN_NAME} v{VERSION} !\n\n"
                "Cliquez sur l'icône de la barre d'outils ou retrouvez l'outil "
                "dans la Boîte à outils de traitement sous "
                "« Zones d'influence visuelle (ZIV) ».",
                "Bienvenue",
            )
            settings.set("ever_installed", True)
            settings.set("version", VERSION)

    # Initialize plugin - if THIS step crashes there is nothing we can do
    try:
        return LidarPlugin(iface)
    except Exception as e:
        QgsMessageLog.logMessage(
            f"Error loading plugin: {str(e)}", PLUGIN_NAME, Qgis.MessageLevel.Critical
        )
        return None
