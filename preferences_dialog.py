# -*- coding: utf-8 -*-
"""
ZIV_prime — global preferences dialog.

Lets the user override a handful of plugin-wide defaults from a single
place. Values are stored under the `plugins/ZIV_prime/preferences/`
namespace of QgsSettings, so they survive QGIS restarts.

Each setting has a hard-coded fallback so the rest of the plugin keeps
working even when QgsSettings is empty or corrupted.
"""
from __future__ import annotations

from qgis.core import QgsSettings
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


SETTINGS_NS = "plugins/ZIV_prime/preferences/"

# Fallback defaults - duplicated in `defaults()` so the rest of the plugin
# can read them without instantiating the dialog.
_DEFAULTS = {
    "ziv_symbology_default": 0,  # 0=spotlight, 1=warm gradient, 2=red translucent
    "logs_dir_override": "",  # empty = QGIS profile default
}

_SYMBOLOGY_LABELS = [
    "Spotlight nuit (défaut)",
    "Dégradé chaud (blanc → rouge)",
    "Rouge translucide uniforme",
]


def get(name: str):
    """Read one preference value, falling back to the hard-coded default."""
    s = QgsSettings()
    default = _DEFAULTS.get(name, "")
    raw = s.value(SETTINGS_NS + name, default)
    if name == "ziv_symbology_default":
        try:
            return int(raw)
        except Exception:
            return _DEFAULTS[name]
    return raw if raw is not None else default


def set_value(name: str, value) -> None:
    QgsSettings().setValue(SETTINGS_NS + name, value)


def defaults() -> dict:
    """Return a copy of the hard-coded defaults dict."""
    return dict(_DEFAULTS)


class PreferencesDialog(QDialog):
    """Modal dialog to edit ZIV_prime preferences."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Zones d'influence visuelle (ZIV) — Préférences")
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ----- ZIV group ----
        ziv_grp = QGroupBox("ZIV")
        ziv_form = QFormLayout(ziv_grp)
        self._symb_combo = QComboBox()
        for label in _SYMBOLOGY_LABELS:
            self._symb_combo.addItem(label)
        try:
            self._symb_combo.setCurrentIndex(int(get("ziv_symbology_default")))
        except Exception:
            self._symb_combo.setCurrentIndex(0)
        ziv_form.addRow(QLabel("Symbologie ZIV par défaut :"), self._symb_combo)
        root.addWidget(ziv_grp)

        # ----- Logs ----
        logs_grp = QGroupBox("Logs")
        logs_form = QFormLayout(logs_grp)
        self._logs_widget = QgsFileWidget()
        self._logs_widget.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self._logs_widget.setFilePath(str(get("logs_dir_override") or ""))
        logs_form.addRow(
            QLabel(
                "Dossier des logs ZIV (laisser vide pour le défaut QGIS) :"
            ),
            self._logs_widget,
        )
        root.addWidget(logs_grp)

        # ----- Buttons ----
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Enregistrer")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)

        reset_btn = QPushButton("Restaurer les défauts")
        reset_btn.clicked.connect(self._on_reset)
        btns.addButton(reset_btn, QDialogButtonBox.ButtonRole.ResetRole)

        root.addWidget(btns)

    def _on_save(self):
        set_value("ziv_symbology_default", int(self._symb_combo.currentIndex()))
        set_value("logs_dir_override", self._logs_widget.filePath() or "")
        self.accept()

    def _on_reset(self):
        d = defaults()
        self._symb_combo.setCurrentIndex(int(d["ziv_symbology_default"]))
        self._logs_widget.setFilePath("")
