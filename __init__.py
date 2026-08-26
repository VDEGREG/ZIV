"""
ZIV_prime - plugin entry point (classFactory, toolbar, dependency check).

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
import importlib.metadata
import importlib.util
import locale
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import QCoreApplication, QTranslator
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QToolButton

# Plugin version
VERSION = "1.1.0"
# Nom affiche a l'utilisateur (fenetres, menu, journal).
PLUGIN_NAME = "Zones d'influence visuelle (ZIV)"
# Nom de paquet : dossier installe, espace QgsSettings, dossier temporaire
# et contexte de traduction Qt. NE JAMAIS renommer : cela orphelinerait les
# preferences stockees et changerait le chemin d'installation.
PLUGIN_PACKAGE = "ZIV_prime"
PLUGIN_DIR = Path(__file__).parent


import re

# PEP 440 numeric prefix : extracts major.minor.patch from "1.26.4", "2.0.0rc1",
# "1.26.4+computecanada", "0.14.1.post1"... and ignores trailing suffixes.
_VERSION_NUMERIC_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _parse_version_tuple(version_str: str):
    """Tolerant PEP 440 → (major, minor, patch) tuple. Returns (0, 0, 0) on
    parse failure, never raises."""
    if not version_str:
        return (0, 0, 0)
    m = _VERSION_NUMERIC_RE.match(version_str)
    if not m:
        return (0, 0, 0)
    return tuple(int(g) if g else 0 for g in m.groups())


def check_package_version(package_name: str, required_version: str = None) -> bool:
    """
    Check if a package is installed and optionally verify its version.
    Uses modern importlib.metadata instead of deprecated pkg_resources.
    Tolerant to PEP 440 suffixes (rc, post, local, etc.) - audit fix C-4.
    """
    try:
        # First try to import the module to verify it's actually available
        spec = importlib.util.find_spec(package_name)
        if spec is None:
            return False

        if required_version is None:
            return True

        # Get installed version
        try:
            installed_version = importlib.metadata.version(package_name)
            if not installed_version:
                return False
        except importlib.metadata.PackageNotFoundError:
            return False

        # Tolerant numeric comparison
        return _parse_version_tuple(installed_version) >= _parse_version_tuple(
            required_version
        )

    except Exception as e:
        QgsMessageLog.logMessage(
            f"Error checking {package_name}: {str(e)}", PLUGIN_NAME, Qgis.Warning
        )
        return False


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


class DependencyInstaller:
    """Class to manage plugin dependencies using batch files."""

    def __init__(self):
        self.plugin_path = PLUGIN_DIR
        self.requirements_path = self.plugin_path / "requirements.txt"
        self.py3_env_path = self.plugin_path / "py3-env.bat"
        self.install_script_path = self.plugin_path / "install_pip_packages.bat"
        self.settings = PluginSettings()

    def check_dependencies(self):
        """Check if required packages are installed with version verification."""
        try:
            if not self.requirements_path.exists():
                QgsMessageLog.logMessage(
                    "requirements.txt not found", PLUGIN_NAME, Qgis.Critical
                )
                return None

            # Read requirements with proper encoding handling
            try:
                with open(self.requirements_path, encoding="utf-8") as f:
                    requirements = [
                        line.strip()
                        for line in f
                        if line.strip() and not line.startswith("#")
                    ]
            except UnicodeDecodeError:
                with open(
                    self.requirements_path, encoding=locale.getpreferredencoding()
                ) as f:
                    requirements = [
                        line.strip()
                        for line in f
                        if line.strip() and not line.startswith("#")
                    ]

            missing = []
            for requirement in requirements:
                package_name = (
                    requirement.split(">=")[0] if ">=" in requirement else requirement
                )
                required_version = (
                    requirement.split(">=")[1] if ">=" in requirement else None
                )

                if not check_package_version(package_name, required_version):
                    missing.append(requirement)
                    QgsMessageLog.logMessage(
                        f"Package {requirement} needs installation",
                        PLUGIN_NAME,
                        Qgis.Info,
                    )
                else:
                    QgsMessageLog.logMessage(
                        f"Package {package_name} is installed", PLUGIN_NAME, Qgis.Info
                    )

            return missing

        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error checking dependencies: {str(e)}", PLUGIN_NAME, Qgis.Critical
            )
            return None

    def verify_batch_files(self):
        """Verify that required batch files exist."""
        if platform.system() != "Windows":
            QgsMessageLog.logMessage(
                "Not on Windows - batch files not needed", PLUGIN_NAME, Qgis.Info
            )
            return True

        if not self.py3_env_path.exists():
            QgsMessageLog.logMessage(
                "py3-env.bat not found", PLUGIN_NAME, Qgis.Critical
            )
            return False

        if not self.install_script_path.exists():
            QgsMessageLog.logMessage(
                "install_pip_packages.bat not found", PLUGIN_NAME, Qgis.Critical
            )
            return False

        return True

    def install(self):
        """Execute dependency installation."""
        try:
            # Check dependencies first
            missing = self.check_dependencies()
            if missing is None:
                # check_dependencies could not even read requirements.txt -
                # proceed conservatively with an empty list so we never
                # crash on `', '.join(None)` later (audit fix C-2).
                QgsMessageLog.logMessage(
                    "Proceeding with full installation", PLUGIN_NAME, Qgis.Warning
                )
                missing = []
            elif not missing:
                QgsMessageLog.logMessage(
                    "All dependencies installed", PLUGIN_NAME, Qgis.Success
                )
                return True

            # Verify batch files exist for Windows
            if not self.verify_batch_files():
                return False

            # Show installation prompt - use real newlines, not escaped
            missing_str = ", ".join(missing) if missing else "(unknown)"
            message = (
                f"The following Python packages are required:\n\n{missing_str}\n\n"
                "Would you like to install them now? "
                "After installation please restart QGIS."
            )
            reply = QMessageBox.question(
                None,
                "Missing Dependencies",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if reply == QMessageBox.StandardButton.No:
                return False

            try:
                if platform.system() == "Windows":
                    # Use batch file on Windows. shell=True is NOT required:
                    # .bat scripts are launched via cmd.exe by the OS as long
                    # as we point to the absolute path (audit fix sec).
                    process = subprocess.Popen(
                        ["cmd.exe", "/c", str(self.install_script_path)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                else:
                    # Direct pip installation on Unix
                    pip_command = "pip3" if shutil.which("pip3") else "pip"
                    process = subprocess.Popen(
                        [pip_command, "install", "-r", str(self.requirements_path)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )

                stdout, stderr = process.communicate(timeout=300)  # 5 minutes timeout

                if process.returncode != 0:
                    QgsMessageLog.logMessage(
                        f"Installation error: {stderr.decode()}",
                        PLUGIN_NAME,
                        Qgis.Critical,
                    )
                    return False

            except subprocess.TimeoutExpired:
                process.kill()
                QgsMessageLog.logMessage(
                    "Installation timeout", PLUGIN_NAME, Qgis.Critical
                )
                return False
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"Installation error: {str(e)}", PLUGIN_NAME, Qgis.Critical
                )
                return False

            # Verify installation
            missing_after = self.check_dependencies()
            if missing_after:
                QgsMessageLog.logMessage(
                    f"Dependencies still missing: {', '.join(missing_after)}",
                    PLUGIN_NAME,
                    Qgis.Critical,
                )
                return False

            return True

        except Exception as e:
            QgsMessageLog.logMessage(
                f"Installation error: {str(e)}", PLUGIN_NAME, Qgis.Critical
            )
            return False


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

        # Initialize locale
        locale_path = self.plugin_dir / "i18n"
        if locale_path.exists():
            self.translator = QTranslator()
            # Defensive: QgsSettings may return None on a fresh profile (audit fix C-3)
            locale = (QgsSettings().value("locale/userLocale") or "fr")[0:2]
            locale_file = f"{PLUGIN_PACKAGE}_{locale}.qm"
            if (locale_path / locale_file).exists():
                self.translator.load(str(locale_path / locale_file))
                QCoreApplication.installTranslator(self.translator)

        QgsMessageLog.logMessage(
            f"{PLUGIN_NAME} plugin initialized", PLUGIN_NAME, Qgis.Info
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
                    Qgis.Info,
                )
                return existing
            except (RuntimeError, AttributeError) as e:
                QgsMessageLog.logMessage(
                    f"{attr_name} stale (wrapped C/C++ deleted: {e}) - "
                    "rebuilding.",
                    PLUGIN_NAME,
                    Qgis.Warning,
                )
                setattr(self, attr_name, None)

        # 2) Instantiate
        try:
            instance = provider_class()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Could not instantiate {provider_class.__name__}: {e}",
                PLUGIN_NAME,
                Qgis.Critical,
            )
            return None

        # 3) Remove any stale provider with the same id (left over from
        # a broken unload of an older install)
        try:
            provider_id = instance.id()
            stale = registry.providerById(provider_id)
            if stale is not None and stale is not instance:
                QgsMessageLog.logMessage(
                    f"Removing stale provider '{provider_id}' from registry.",
                    PLUGIN_NAME,
                    Qgis.Warning,
                )
                try:
                    registry.removeProvider(stale)
                except Exception:
                    pass
        except Exception:
            pass

        # 4) Register the fresh instance
        try:
            registry.addProvider(instance)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"addProvider({attr_name}) failed: {e}",
                PLUGIN_NAME,
                Qgis.Warning,
            )
            return None

        # 5) Refresh algorithms - never let this break the load
        try:
            instance.refreshAlgorithms()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"refreshAlgorithms({attr_name}) failed silently: {e}",
                PLUGIN_NAME,
                Qgis.Warning,
            )

        setattr(self, attr_name, instance)
        QgsMessageLog.logMessage(
            f"{attr_name} registered successfully.", PLUGIN_NAME, Qgis.Info
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
            f"Initializing {PLUGIN_NAME} GUI", PLUGIN_NAME, Qgis.Info
        )
        try:
            registry = QgsApplication.processingRegistry()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Could not reach Processing registry: {e}",
                PLUGIN_NAME,
                Qgis.Critical,
            )
            registry = None

        # ---- Visibility provider (registered FIRST so the main model can
        # resolve `pcfr_vis:*` algorithm ids when it is loaded) ----
        if registry is not None:
            try:
                from .visibility_provider import PcfrVisibilityProvider

                self._register_provider_safe(
                    "visibility_provider", PcfrVisibilityProvider, registry
                )
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"Visibility provider init skipped: {e}",
                    PLUGIN_NAME,
                    Qgis.Warning,
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
                    Qgis.Warning,
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
                        "Zones d'influence visuelle (ZIV) : téléchargement des dalles "
                        "LiDAR HD de l'IGN et calcul de la visibilité"
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
                    self.tr("⚙ Préférences…"), self.iface.mainWindow()
                )
                self.action_prefs.setToolTip(
                    self.tr(
                        "Configurer les défauts du greffon : symbologie "
                        "ZIV et dossier de logs."
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
                Qgis.Warning,
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
                Qgis.Critical,
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
                Qgis.Critical,
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
            f"Unloading {PLUGIN_NAME} plugin", PLUGIN_NAME, Qgis.Info
        )

        # 1) Remove toolbar / menu entries
        try:
            for act in (self.action_ziv, self.action_prefs):
                if act is None:
                    continue
                try:
                    self.iface.removePluginMenu(self.menu_name, act)
                except Exception:
                    pass
            if self.tool_button_action is not None:
                try:
                    self.iface.removeToolBarIcon(self.tool_button_action)
                except Exception:
                    pass
        except Exception as e:
            QgsMessageLog.logMessage(
                f"unload(toolbar) failed silently: {e}",
                PLUGIN_NAME,
                Qgis.Warning,
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
                        Qgis.Info,
                    )
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"removeProvider({attr_name}) failed silently: {e}",
                        PLUGIN_NAME,
                        Qgis.Warning,
                    )
                finally:
                    setattr(self, attr_name, None)

        # 3) Clear version setting (but keep ever_installed flag)
        try:
            self.settings.remove("version")
        except Exception:
            pass

        # 4) Cleanup any temporary files
        try:
            temp_dir = Path(tempfile.gettempdir()) / PLUGIN_PACKAGE
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error cleaning temporary files: {str(e)}",
                PLUGIN_NAME,
                Qgis.Warning,
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

    # Try to install required deps - but never block the load on failure.
    try:
        installer = DependencyInstaller()
        if not installer.install():
            QgsMessageLog.logMessage(
                "Some optional dependencies could not be installed; the "
                "plugin will load anyway. If features fail at runtime, "
                "install 'requests' and 'numpy' manually in the QGIS "
                "Python environment.",
                PLUGIN_NAME,
                Qgis.Warning,
            )
    except Exception as e:
        QgsMessageLog.logMessage(
            f"DependencyInstaller crashed silently: {e}; loading plugin "
            "anyway.",
            PLUGIN_NAME,
            Qgis.Warning,
        )

    # Show welcome message only on first ever installation
    try:
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
    except Exception:
        pass

    # Initialize plugin - if THIS step crashes there is nothing we can do
    try:
        return LidarPlugin(iface)
    except Exception as e:
        QgsMessageLog.logMessage(
            f"Error loading plugin: {str(e)}", PLUGIN_NAME, Qgis.Critical
        )
        return None
