# -*- coding: utf-8 -*-
"""Journal du greffon : une même ligne part vers trois destinations.

Un message écrit ici arrive dans le retour d'exécution de Processing (ce que
l'utilisateur voit dérouler), dans le journal de messages de QGIS (ce qu'il
retrouve après coup) et dans un fichier daté sur disque (ce qu'il peut nous
envoyer quand quelque chose s'est mal passé).

Trois choses non négociables, et une corrigée :

  * le fichier vit dans le **répertoire de réglages QGIS**, sous
    `generation_ziv_logs`, pas dans le dossier temporaire du système — un
    journal qu'un nettoyage de disque efface ne sert à rien le lendemain ;
  * **rétention de 30 fichiers** : au-delà, les plus anciens partent ;
  * **aucune exception ne remonte jamais**. Un problème d'écriture de journal
    ne doit pas interrompre un calcul de deux heures. Toute la chaîne de replis
    est silencieuse, et si rien n'est écrivable la journalisation fichier
    s'éteint sans un mot de plus.

Corrigé ici : la préférence « dossier des journaux » que la fenêtre de réglages
propose est désormais **réellement lue**. Elle existait dans l'interface et
n'avait aucun effet.
"""

import contextlib
from datetime import datetime
from pathlib import Path

# Nom affiché dans le journal de messages de QGIS.
NOM_AFFICHE = "Zones d'influence visuelle (ZIV)"

SOUS_DOSSIER = "generation_ziv_logs"
PREFIXE = "genziv_"
FORMAT_HORODATAGE = "%Y%m%d_%H%M%S"
MOTIF = PREFIXE + "*.log"
FICHIERS_CONSERVES = 30

INFO, ATTENTION, ERREUR = "INFO", "WARNING", "ERROR"


class JournalZiv:
    """Journal à trois sorties : Processing, QGIS, fichier."""

    def __init__(self, feedback=None, vers_fichier=True):
        self.feedback = feedback
        self.vers_fichier = bool(vers_fichier)
        self.fichier = None
        if self.vers_fichier:
            self.fichier = self._preparer_fichier()

    # ------------------------------------------------------------- écriture

    def info(self, message):
        self._ecrire(INFO, message)

    def warning(self, message):
        self._ecrire(ATTENTION, message)

    def error(self, message):
        self._ecrire(ERREUR, message)

    def _ecrire(self, niveau, message):
        texte = str(message)
        self._vers_processing(niveau, texte)
        self._vers_qgis(niveau, texte)
        self._vers_disque(niveau, texte)

    def _vers_processing(self, niveau, texte):
        if self.feedback is None:
            return
        with contextlib.suppress(Exception):
            if niveau == ERREUR:
                self.feedback.reportError(texte)
            elif niveau == ATTENTION:
                self.feedback.pushWarning(texte)
            else:
                self.feedback.pushInfo(texte)

    def _vers_qgis(self, niveau, texte):
        with contextlib.suppress(Exception):
            from qgis.core import Qgis, QgsMessageLog

            niveaux = {
                INFO: Qgis.MessageLevel.Info,
                ATTENTION: Qgis.MessageLevel.Warning,
                ERREUR: Qgis.MessageLevel.Critical,
            }
            QgsMessageLog.logMessage(texte, NOM_AFFICHE, niveaux[niveau])

    def _vers_disque(self, niveau, texte):
        if self.fichier is None:
            return
        # Ouverture, écriture, fermeture à chaque ligne : un calcul interrompu
        # par un plantage laisse quand même son journal complet sur le disque.
        with contextlib.suppress(Exception):
            horodatage = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.fichier, "a", encoding="utf-8") as flux:
                flux.write("%s [%s] %s\n" % (horodatage, niveau, texte))

    # ------------------------------------------------------- mise en place

    def _preparer_fichier(self):
        dossier = self._dossier_des_journaux()
        if dossier is None:
            return None
        self._faire_le_menage(dossier)
        return dossier / ("%s%s.log" % (PREFIXE, datetime.now().strftime(FORMAT_HORODATAGE)))

    def _dossier_des_journaux(self):
        """Dossier où écrire, ou None si rien n'est écrivable.

        Ordre : le dossier choisi par l'utilisateur dans les réglages, sinon le
        répertoire de réglages QGIS, sinon le dossier temporaire du système.
        """
        for candidat in (self._dossier_demande(), self._dossier_qgis(), self._dossier_temporaire()):
            if candidat is None:
                continue
            with contextlib.suppress(Exception):
                candidat.mkdir(parents=True, exist_ok=True)
                if candidat.is_dir():
                    return candidat
        return None

    @staticmethod
    def _dossier_demande():
        """La préférence de la fenêtre de réglages, si elle est renseignée."""
        with contextlib.suppress(Exception):
            from . import preferences_dialog

            choisi = str(preferences_dialog.get("logs_dir_override") or "").strip()
            if choisi:
                return Path(choisi)
        return None

    @staticmethod
    def _dossier_qgis():
        with contextlib.suppress(Exception):
            from qgis.core import QgsApplication

            racine = str(QgsApplication.qgisSettingsDirPath() or "").strip()
            if racine:
                return Path(racine) / SOUS_DOSSIER
        return None

    @staticmethod
    def _dossier_temporaire():
        with contextlib.suppress(Exception):
            import tempfile

            return Path(tempfile.gettempdir()) / SOUS_DOSSIER
        return None

    @staticmethod
    def _faire_le_menage(dossier, conserves=FICHIERS_CONSERVES):
        """Ne garde que les `conserves` journaux les plus récents.

        Un fichier verrouillé par un autre processus Windows est laissé en
        place sans bruit : la rotation est un service, pas une exigence.
        """
        with contextlib.suppress(Exception):
            fichiers = sorted(dossier.glob(MOTIF), key=lambda f: f.stat().st_mtime, reverse=True)
            for vieux in fichiers[conserves:]:
                with contextlib.suppress(Exception):
                    vieux.unlink()
