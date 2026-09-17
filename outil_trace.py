# -*- coding: utf-8 -*-
"""Tracer l'emprise a la souris, depuis le formulaire de l'algorithme.

Pourquoi. On ouvre la ZIV, et on s'apercoit qu'on n'a pas de couche d'emprise :
il fallait jusqu'ici sortir du formulaire, creer une couche, la numeriser, la
sauver. Le bouton pose ici tient tout cela en trois clics.

Ce que l'on trace. Un POLYGONE — l'emprise du projet — ou un POINT, qui vaut
un observateur unique portant un objet de la hauteur saisie : utile pour une
eolienne, un pylone, un mat, tout projet ponctuel. La chaine accepte les deux
sans rien changer au modele : `native:pointsalonglines`, qui fabrique les
observateurs a partir de l'emprise, rend 1 point pour un point et N points
pour un contour (mesure du 2026-09-14).

Ou va la geometrie. Dans une couche MEMOIRE ajoutee au projet, que l'on
COMPLETE a chaque trace plutot que de la remplacer : un projet en plusieurs
ilots se trace en plusieurs fois, et l'algorithme unit deja les entites de son
entree. Couche temporaire : QGIS previent a la fermeture du projet, et une
emprise non enregistree est perdue — c'est le prix de la souplesse, assume au
moment du choix.

Le SCR. Celui du projet, parce que c'est dans ce systeme que le canevas rend
les coordonnees de la souris. `_caler_emprise_sur_le_mnt`, dans l'algorithme,
reprojettera ensuite sur le terrain : n'importe quel SCR de projet convient
donc, et forcer celui du terrain ici serait une erreur.

Le motif d'interaction est celui de `hedge_tool.py` du meme auteur, eprouve en
production : clic pour un sommet, double-clic ou clic droit pour terminer,
Echap pour tout effacer. Y compris son piege — le double-clic ajoute un sommet
en doublon, qu'il faut retirer avant de construire la geometrie.
"""

import contextlib
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer,
                       QgsWkbTypes)
from qgis.gui import QgsMapTool, QgsRubberBand

# Un nom par type de geometrie : une couche memoire ne porte qu'un type, et
# melanger emprise et observateur dans la meme serait un piege a lecture.
NOM_POLYGONE = "Emprise tracee"
NOM_POINT = "Observateur trace"


def _traduire(texte):
    # `QCoreApplication.translate` et non `self.tr` : en PyQt6 `tr` n'accepte
    # plus l'instance en premier argument, et le greffon vise Qt5 comme Qt6.
    return QCoreApplication.translate("ZIV_prime", texte)


class OutilTrace(QgsMapTool):
    """Trace un polygone ou un point, et rend la couche a l'appelant.

    `au_termine(couche)` est appele une fois la geometrie posee — c'est par la
    que le formulaire se remontre et selectionne la couche dans son champ
    d'entree.
    """

    def __init__(self, iface, ponctuel=False, au_termine=None):
        self.iface = iface
        self.canevas = iface.mapCanvas()
        super().__init__(self.canevas)
        self.ponctuel = bool(ponctuel)
        self._au_termine = au_termine
        self._sommets = []
        genre = (QgsWkbTypes.PointGeometry if self.ponctuel
                 else QgsWkbTypes.PolygonGeometry)
        self._bande = QgsRubberBand(self.canevas, genre)
        self._bande.setColor(QColor(255, 0, 0, 90))
        self._bande.setWidth(2)
        # Un curseur est un confort : son absence ne justifie rien.
        with contextlib.suppress(Exception):
            self.setCursor(Qt.CursorShape.CrossCursor)

    # ---- la couche d'accueil ------------------------------------------
    def couche(self):
        """La couche memoire du bon type, creee au premier usage."""
        projet = QgsProject.instance()
        nom = NOM_POINT if self.ponctuel else NOM_POLYGONE
        for couche in projet.mapLayersByName(_traduire(nom)):
            if isinstance(couche, QgsVectorLayer) and couche.isValid():
                return couche
        # Le SCR du projet : c'est celui des coordonnees que rend le canevas.
        scr = projet.crs().authid() or "EPSG:4326"
        couche = QgsVectorLayer(
            "%s?crs=%s&field=id:integer"
            % ("Point" if self.ponctuel else "Polygon", scr),
            _traduire(nom), "memory")
        with contextlib.suppress(Exception):
            symbole = couche.renderer().symbol()
            symbole.setColor(QColor(255, 0, 0, 60))
        projet.addMapLayer(couche)
        return couche

    # ---- l'apercu elastique -------------------------------------------
    def _peindre(self, en_cours=None):
        genre = (QgsWkbTypes.PointGeometry if self.ponctuel
                 else QgsWkbTypes.PolygonGeometry)
        self._bande.reset(genre)
        points = self._sommets + ([en_cours] if en_cours is not None else [])
        dernier = len(points) - 1
        for rang, point in enumerate(points):
            self._bande.addPoint(point, rang == dernier)

    # ---- les evenements -----------------------------------------------
    def canvasMoveEvent(self, event):
        if self._sommets:
            self._peindre(self.toMapCoordinates(event.pos()))

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._terminer()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._sommets.append(self.toMapCoordinates(event.pos()))
        self._peindre()
        # Un point n'a qu'un sommet : le premier clic suffit, inutile de
        # demander un double-clic pour une geometrie deja complete.
        if self.ponctuel:
            self._terminer()

    def canvasDoubleClickEvent(self, event):
        self._terminer()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._effacer()

    # ---- terminer, ou renoncer ----------------------------------------
    def _terminer(self):
        # Le double-clic ajoute un sommet en DOUBLON du precedent : le retirer
        # avant de construire, sinon un triangle se croit quadrilatere et la
        # geometrie sort invalide.
        propres = []
        for point in self._sommets:
            if (not propres
                    or (point.x(), point.y()) != (propres[-1].x(), propres[-1].y())):
                propres.append(point)

        if self.ponctuel:
            if not propres:
                self._effacer()
                return
            geometrie = QgsGeometry.fromPointXY(propres[0])
        else:
            # Trois sommets distincts au minimum : en deca il n'y a pas de
            # surface, donc pas d'emprise. Renoncer en silence serait pire —
            # l'utilisateur croirait avoir trace.
            if len(propres) < 3:
                self._avertir(_traduire(
                    "Emprise abandonnee : il faut au moins trois sommets "
                    "distincts pour delimiter une surface."))
                self._effacer()
                return
            geometrie = QgsGeometry.fromPolygonXY([propres])
            if not geometrie.isGeosValid():
                # Un contour qui se croise donne une surface indefinie : la
                # reparation de GEOS rend le polygone valide le plus proche.
                reparee = geometrie.makeValid()
                if reparee is not None and not reparee.isEmpty():
                    geometrie = reparee
                else:
                    self._avertir(_traduire(
                        "Emprise abandonnee : le contour se recoupe et n'a pas "
                        "pu etre repare."))
                    self._effacer()
                    return

        couche = self.couche()
        entite = QgsFeature(couche.fields())
        entite.setGeometry(geometrie)
        with contextlib.suppress(Exception):
            entite.setAttribute("id", couche.featureCount() + 1)
        couche.dataProvider().addFeatures([entite])
        couche.updateExtents()
        couche.triggerRepaint()
        self._effacer()

        if callable(self._au_termine):
            try:
                self._au_termine(couche)
            except Exception as erreur:
                self._avertir(_traduire(
                    "Trace enregistre, mais le formulaire n'a pas pu etre "
                    "remis au premier plan (%s).") % erreur)

    def _effacer(self):
        self._sommets = []
        genre = (QgsWkbTypes.PointGeometry if self.ponctuel
                 else QgsWkbTypes.PolygonGeometry)
        self._bande.reset(genre)

    def _avertir(self, message):
        with contextlib.suppress(Exception):
            self.iface.messageBar().pushWarning("ZIV", message)

    def deactivate(self):
        # Sans cet effacement, la bande elastique reste peinte sur la carte
        # apres qu'on a change d'outil : c'est le defaut « bandeau orphelin »
        # deja rencontre ailleurs dans ce depot.
        self._effacer()
        with contextlib.suppress(Exception):
            super().deactivate()
