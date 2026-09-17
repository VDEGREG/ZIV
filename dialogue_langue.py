# -*- coding: utf-8 -*-
"""Deux boutons FR / EN DANS le formulaire de l'algorithme.

Pourquoi ici et pas dans la barre d'outils : l'utilisateur change de langue en
regardant le formulaire, pas une icone perdue sous la carte.

Comment on s'accroche. `processing.createAlgorithmDialog()` appelle
`alg.createCustomParametersWidget(parent)` AVANT de construire son dialogue
par defaut, et ne retombe sur celui-ci que si l'on rend `None` — constate dans
`processing/tools/general.py` des deux versions visees (3.44 l. 177, 4.2
l. 178), et c'est par la que passent aussi la boite a outils, le localisateur
et les menus. Les algorithmes GDAL de QGIS utilisent exactement ce point pour
ajouter leur panneau « GDAL/OGR console call » : on suit leur patron, on ne
detourne rien.

La classe de dialogue, elle, a change de nom et de module entre les deux
versions — `AlgorithmDialog` en 3.44, `AlgorithmWidget` en 4.2 — d'ou l'import
tolerant. Si aucune des deux n'est la, on rend `None` et QGIS ouvre son
formulaire habituel, sans boutons : le greffon perd une commodite, pas une
fonction.

Le rafraichissement. Reinstaller le traducteur ne repeint PAS les widgets deja
construits : les libelles ont ete poses a l'ouverture. On rouvre donc le
formulaire, en lui repassant les valeurs deja saisies.
"""

import contextlib
from qgis.PyQt.QtCore import QCoreApplication, QTimer
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from . import langue as langue_module

# La classe de dialogue de CETTE version de QGIS.
_BASE = None
_PANNEAU = None
try:
    from processing.gui.ParametersPanel import ParametersPanel as _PANNEAU
except Exception:
    _PANNEAU = None
for _module, _nom in (("processing.gui.AlgorithmDialog", "AlgorithmDialog"),
                      ("processing.gui.algorithm_widget", "AlgorithmWidget")):
    with contextlib.suppress(Exception):
        _BASE = getattr(__import__(_module, fromlist=[_nom]), _nom)
        break


def disponible():
    """Peut-on habiller le formulaire sur cette installation de QGIS ?"""
    return _BASE is not None and _PANNEAU is not None


def construire(algorithme, parent):
    """Rend le formulaire habille, ou None pour laisser QGIS faire le sien."""
    if not disponible():
        return None
    try:
        return _DialogueAvecLangue(algorithme, parent=parent)
    except Exception:
        return None


if disponible():

    class _BarreDeLangue(QWidget):
        """« Langue : [FR] [EN] », a poser en tete du formulaire."""

        def __init__(self, au_clic, parent=None):
            super().__init__(parent)
            disposition = QHBoxLayout()
            disposition.setContentsMargins(0, 0, 0, 4)
            disposition.setSpacing(4)
            self.etiquette = QLabel()
            disposition.addWidget(self.etiquette)
            disposition.addStretch(1)

            self.boutons = {}
            for code, texte in (("fr", "FR"), ("en", "EN")):
                bouton = QPushButton(texte)
                bouton.setCheckable(True)
                bouton.setMaximumWidth(44)
                bouton.clicked.connect(
                    lambda _coche=False, c=code: au_clic(c))
                disposition.addWidget(bouton)
                self.boutons[code] = bouton
            self.setLayout(disposition)
            self.retraduire()

        def retraduire(self):
            # `QCoreApplication.translate` plutot que `self.tr()` : en PyQt6,
            # `tr` n'accepte plus l'instance en premier argument, et le
            # greffon vise Qt5 comme Qt6. Le contexte est nomme explicitement,
            # comme partout ailleurs dans le greffon.
            self.etiquette.setText(
                QCoreApplication.translate("ZIV_prime", "Language / Langue :"))

        def cocher(self, langue):
            for code, bouton in self.boutons.items():
                bouton.blockSignals(True)
                bouton.setChecked(code == langue)
                bouton.blockSignals(False)

    class _BarreDeTrace(QWidget):
        """« Ou tracer : [Emprise] [Point d'observation] », sous le champ ZIP.

        Sous le champ, et non en tete du formulaire : un bouton se lit a cote
        de ce qu'il remplit. C'est le choix arrete le 2026-09-14.
        """

        def __init__(self, au_clic, parent=None):
            super().__init__(parent)
            disposition = QHBoxLayout()
            disposition.setContentsMargins(0, 0, 0, 6)
            disposition.setSpacing(4)
            self.etiquette = QLabel()
            disposition.addWidget(self.etiquette)
            disposition.addStretch(1)

            self.bouton_emprise = QPushButton()
            self.bouton_emprise.clicked.connect(
                lambda _coche=False: au_clic(False))
            disposition.addWidget(self.bouton_emprise)

            self.bouton_point = QPushButton()
            self.bouton_point.clicked.connect(
                lambda _coche=False: au_clic(True))
            disposition.addWidget(self.bouton_point)

            self.setLayout(disposition)
            self.retraduire()

        def retraduire(self):
            self.etiquette.setText(
                QCoreApplication.translate("ZIV_prime", "or draw it:"))
            self.bouton_emprise.setText(
                QCoreApplication.translate("ZIV_prime", "Draw an area"))
            self.bouton_point.setText(
                QCoreApplication.translate("ZIV_prime", "Draw a viewpoint"))
            self.bouton_emprise.setToolTip(QCoreApplication.translate(
                "ZIV_prime",
                "Click to add a vertex, double-click to finish, Esc to clear. "
                "The area is written to a temporary layer of the project."))
            self.bouton_point.setToolTip(QCoreApplication.translate(
                "ZIV_prime",
                "One click is enough. The point stands for a single observer "
                "carrying an object of the height set below."))

    class _PanneauAvecLangue(_PANNEAU):
        """Le panneau des parametres, habille de deux barres.

        `addExtraWidget` vient de `QgsProcessingParametersWidget` et existe
        dans les deux versions — c'est ce que GDAL emploie. Il ajoute en BAS ;
        on deplace donc ensuite chaque widget a sa place : la langue en tete,
        parce qu'elle se choisit avant de remplir un formulaire ; le trace
        juste sous le champ d'emprise, parce qu'il le remplit.
        """

        def __init__(self, parent, algorithme):
            super().__init__(parent, algorithme)
            self.dialogue_parent = parent
            self.barre = _BarreDeLangue(self._demander_langue, self)
            self.addExtraWidget(self.barre)
            self.barre.cocher(langue_module.retenue())
            self._remonter_en_tete()

            # L'outil de trace, GARDE ici : sans reference Python, Qt le
            # libere et le canevas se retrouve avec un outil mort.
            self._outil_trace = None
            # L'outil qu'avait le canevas avant le trace, pour le lui rendre
            # tel quel — `unsetMapTool` ne fait que revenir au defaut.
            self._outil_precedent = None
            self.barre_trace = _BarreDeTrace(self._tracer, self)
            self.addExtraWidget(self.barre_trace)
            self._placer_sous_lentree(self.barre_trace)

        def _remonter_en_tete(self):
            # Restee en bas, la barre reste cliquable : ce n'est qu'une
            # question de place.
            with contextlib.suppress(Exception):
                disposition = self.barre.parentWidget().layout()
                if disposition is not None:
                    disposition.removeWidget(self.barre)
                    disposition.insertWidget(0, self.barre)

        def _placer_sous_lentree(self, widget):
            """Deplace `widget` juste apres le widget du parametre INPUT.

            L'index n'est PAS code en dur : on demande au wrapper du parametre
            quel widget il porte, puis sa position dans le layout. Un
            parametre ajoute ou retire plus tard ne deplacera donc rien.
            Sonde du 2026-09-14 : le panneau expose `wrappers`, un dictionnaire
            nom de parametre -> wrapper, et `wrappedWidget()` rend le widget.
            """
            # Reste en bas du formulaire : deplace ou non, le bouton
            # trace. Mieux vaut mal place que pas la.
            with contextlib.suppress(Exception):
                disposition = widget.parentWidget().layout()
                if disposition is None:
                    return
                wrapper = (getattr(self, "wrappers", None) or {}).get("INPUT")
                cible = wrapper.wrappedWidget() if wrapper is not None else None
                rang = disposition.indexOf(cible) if cible is not None else -1
                disposition.removeWidget(widget)
                disposition.insertWidget(rang + 1 if rang >= 0 else 0, widget)

        # ---- le trace a la souris -------------------------------------
        def _tracer(self, ponctuel):
            """Reduit le formulaire, arme l'outil, et attend la geometrie.

            `showMinimized` et surtout PAS `hide` : le formulaire de
            Processing est ouvert par `exec()`, et `QDialog.setVisible(False)`
            QUITTE la boucle modale. Le cacher terminait donc le dialogue, et
            `execAlgorithmDialog` remettait dans la foulee l'outil de carte
            precedent (3.44, `tools/general.py` l. 199-204) — d'ou le defaut
            observe : la fenetre disparait, la main revient, et rien ne se
            trace. Mesure du 2026-09-16 : `exec()` rend 106 ms apres `hide()`,
            et tient jusqu'a la fermeture avec `showMinimized()` ; un clic
            injecte sur la carte est bien recu pendant que le dialogue modal
            est reduit. C'est le motif que QGIS emploie lui-meme pour
            « dessiner sur le canevas » (`gui/PointSelectionPanel.py`).
            """
            from qgis.utils import iface

            from . import outil_trace

            if iface is None:
                return
            fenetre = None
            try:
                fenetre = self.dialogue_parent.window()
            except Exception:
                fenetre = None

            def termine(couche):
                self.desactiver_trace()
                if fenetre is not None:
                    with contextlib.suppress(Exception):
                        fenetre.showNormal()
                        fenetre.raise_()
                        fenetre.activateWindow()
                self._choisir_la_couche(couche)

            self.desactiver_trace()
            canevas = iface.mapCanvas()
            self._outil_precedent = canevas.mapTool()
            self._outil_trace = outil_trace.OutilTrace(
                iface, ponctuel=ponctuel, au_termine=termine)
            canevas.setMapTool(self._outil_trace)
            if fenetre is not None:
                with contextlib.suppress(Exception):
                    fenetre.showMinimized()

        def _choisir_la_couche(self, couche):
            """Pose la couche tracee dans le champ d'entree du formulaire."""
            # La couche est dans le projet : l'utilisateur la choisira
            # d'un clic. Un confort manque, pas une fonction.
            with contextlib.suppress(Exception):
                from qgis.core import QgsProcessingContext

                wrapper = (getattr(self, "wrappers", None) or {}).get("INPUT")
                if wrapper is None or couche is None:
                    return
                contexte = QgsProcessingContext()
                wrapper.setParameterValue(couche, contexte)

        def desactiver_trace(self):
            """Rend le canevas a son outil d'avant. Toujours appelable.

            Appelee a la fin d'un trace ET a la fermeture du formulaire :
            laisser un outil de dessin arme sur un formulaire disparu est le
            defaut « bandeau orphelin » deja rencontre dans ce depot.
            """
            outil = getattr(self, "_outil_trace", None)
            if outil is None:
                return
            self._outil_trace = None
            precedent = getattr(self, "_outil_precedent", None)
            self._outil_precedent = None
            with contextlib.suppress(Exception):
                from qgis.utils import iface

                if iface is None:
                    return
                canevas = iface.mapCanvas()
                # Rendre l'outil D'AVANT plutot que de retirer le notre :
                # `unsetMapTool` laisse le canevas sur son outil par defaut,
                # et l'utilisateur perdrait celui qu'il avait en main.
                if precedent is not None:
                    canevas.setMapTool(precedent)
                else:
                    canevas.unsetMapTool(outil)

        def _demander_langue(self, code):
            if code == langue_module.retenue():
                self.barre.cocher(code)
                return
            dialogue = self.dialogue_parent
            valeurs = _valeurs_saisies(dialogue)
            langue_module.appliquer(code, memoriser=True)
            langue_module.rafraichir_fournisseurs()
            _rouvrir(dialogue, valeurs)

    class _DialogueAvecLangue(_BASE):
        def __init__(self, algorithme, parent=None):
            super().__init__(algorithme, parent=parent)
            self._panneau_ziv = None

        def getParametersPanel(self, algorithme, parent):
            self._panneau_ziv = _PanneauAvecLangue(parent, algorithme)
            return self._panneau_ziv

        def closeEvent(self, event):
            # Fermer le formulaire pendant un trace laissait l'outil arme sur
            # le canevas : le clic suivant de l'utilisateur ajoutait un sommet
            # a une emprise dont plus rien ne parlait. On rend la main au
            # canevas avant de disparaitre.
            panneau = getattr(self, "_panneau_ziv", None)
            if panneau is not None:
                with contextlib.suppress(Exception):
                    panneau.desactiver_trace()
            try:
                super().closeEvent(event)
            except Exception:
                event.accept()


def _valeurs_saisies(dialogue):
    """Ce que l'utilisateur a deja rempli, pour ne pas le lui redemander.

    `createProcessingParameters()` refuse un formulaire incomplet : c'est
    normal a ce stade, et ce n'est pas une raison pour perdre le reste. On
    rend alors ce qu'on peut, quitte a rouvrir un formulaire vide.
    """
    for methode in ("createProcessingParameters", "getParameterValues"):
        with contextlib.suppress(Exception):
            fonction = getattr(dialogue, methode, None)
            if fonction is None:
                continue
            valeurs = fonction()
            if isinstance(valeurs, dict):
                return valeurs
    return {}


def _rouvrir(dialogue, valeurs):
    """Ferme le formulaire et le rouvre traduit, avec les memes valeurs.

    Differe d'un tour de boucle : rouvrir depuis l'interieur de la boucle
    modale du dialogue qu'on ferme ne fonctionne pas.
    """
    identifiant = "genziv:generate"
    with contextlib.suppress(Exception):
        algorithme = dialogue.algorithm()
        if algorithme is not None:
            identifiant = algorithme.id()

    def ouvrir():
        try:
            import processing

            processing.execAlgorithmDialog(identifiant, valeurs or {})
        except Exception:
            with contextlib.suppress(Exception):
                import processing

                processing.execAlgorithmDialog(identifiant, {})

    with contextlib.suppress(Exception):
        dialogue.close()
    QTimer.singleShot(0, ouvrir)
