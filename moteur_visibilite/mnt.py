# -*- coding: utf-8 -*-
"""Accès au modèle numérique de terrain.

Une seule classe, `Mnt`, qui porte la grille d'altitudes et sa
géoréférence. Deux façons de la construire : depuis un fichier lisible par
GDAL, ou depuis un tableau numpy (c'est ce dont les bancs de contrôle ont
besoin, et cela garde le moteur indépendant de GDAL).

Convention interne : les cellules sans donnée valent `nan`. Le moteur s'appuie
dessus (`np.fmax` propage l'horizon à travers un trou au lieu de le contaminer).
"""

import numpy as np


class Mnt:
    """Grille d'altitudes régulière, orientée nord (pas de rotation).

    `gt` suit la convention GDAL : (x_origine, taille_x, 0, y_origine, 0, -taille_y),
    l'origine étant le **coin** supérieur gauche de la première cellule.
    """

    def __init__(self, z, gt, crs_wkt=""):
        z = np.asarray(z, dtype=np.float32)
        if z.ndim != 2:
            raise ValueError("Le MNT doit être un tableau à deux dimensions.")
        if gt[2] != 0.0 or gt[4] != 0.0:
            raise ValueError(
                "Grille pivotée (termes de rotation non nuls) : ce moteur ne "
                "traite que les grilles orientées nord. Reprojeter le MNT d'abord."
            )
        if gt[1] <= 0 or gt[5] >= 0:
            raise ValueError(
                "Géoréférence inattendue : la taille de cellule en X doit être "
                "positive et celle en Y négative (origine en haut à gauche)."
            )
        self.z = z
        self.gt = tuple(float(v) for v in gt)
        self.crs_wkt = crs_wkt

    # ------------------------------------------------------------------ infos

    @property
    def ny(self):
        return self.z.shape[0]

    @property
    def nx(self):
        return self.z.shape[1]

    @property
    def dx(self):
        return self.gt[1]

    @property
    def dy(self):
        return -self.gt[5]

    @property
    def etendue(self):
        """(xmin, ymin, xmax, ymax) de l'emprise, bords des cellules."""
        xmin = self.gt[0]
        ymax = self.gt[3]
        return (xmin, ymax - self.ny * self.dy, xmin + self.nx * self.dx, ymax)

    # ------------------------------------------------------- construction I/O

    @classmethod
    def depuis_fichier(cls, chemin, bande=1):
        from osgeo import gdal

        gdal.UseExceptions()
        source = gdal.Open(str(chemin), gdal.GA_ReadOnly)
        if source is None:
            raise IOError("MNT illisible : %s" % chemin)
        b = source.GetRasterBand(bande)
        z = b.ReadAsArray().astype(np.float32)
        sans_donnee = b.GetNoDataValue()
        if sans_donnee is not None:
            z[z == np.float32(sans_donnee)] = np.nan
        # Les rasters produits par certains logiciels codent l'absence par une
        # valeur sentinelle sans la déclarer ; on ne devine pas, on se contente
        # de neutraliser les infinis, qui casseraient les comparaisons d'angle.
        z[~np.isfinite(z)] = np.nan
        objet = cls(z, source.GetGeoTransform(), source.GetProjection())
        source = None
        return objet

    @classmethod
    def depuis_tableau(cls, z, origine_x=0.0, origine_y=None, taille=1.0, taille_y=None):
        """Construit un MNT synthétique, origine au coin supérieur gauche."""
        z = np.asarray(z, dtype=np.float32)
        ty = taille if taille_y is None else taille_y
        if origine_y is None:
            origine_y = z.shape[0] * ty
        return cls(z, (origine_x, taille, 0.0, origine_y, 0.0, -ty))

    # ------------------------------------------------------------- géométrie

    def monde_vers_pixel(self, x, y):
        """Coordonnées de cellule fractionnaires (colonne, ligne), centres entiers."""
        col = (np.asarray(x, dtype=float) - self.gt[0]) / self.dx - 0.5
        lig = (self.gt[3] - np.asarray(y, dtype=float)) / self.dy - 0.5
        return col, lig

    def pixel_vers_monde(self, col, lig):
        """Centre de la cellule (colonne, ligne)."""
        x = self.gt[0] + (np.asarray(col, dtype=float) + 0.5) * self.dx
        y = self.gt[3] - (np.asarray(lig, dtype=float) + 0.5) * self.dy
        return x, y

    def cellule(self, x, y):
        """(ligne, colonne) entières contenant (x, y), ou None si hors emprise."""
        col, lig = self.monde_vers_pixel(x, y)
        c = int(np.floor(col + 0.5))
        r = int(np.floor(lig + 0.5))
        if 0 <= r < self.ny and 0 <= c < self.nx:
            return r, c
        return None

    def altitude(self, x, y):
        """Altitude de la cellule contenant (x, y) ; `nan` si hors emprise."""
        rc = self.cellule(x, y)
        if rc is None:
            return float("nan")
        return float(self.z[rc])

    def altitude_interpolee(self, x, y):
        """Altitude interpolée bilinéairement — utilisée par la ligne de vue."""
        col, lig = self.monde_vers_pixel(x, y)
        c0 = np.floor(col).astype(int)
        r0 = np.floor(lig).astype(int)
        fc = col - c0
        fr = lig - r0
        c0 = np.clip(c0, 0, self.nx - 1)
        r0 = np.clip(r0, 0, self.ny - 1)
        c1 = np.clip(c0 + 1, 0, self.nx - 1)
        r1 = np.clip(r0 + 1, 0, self.ny - 1)
        z = self.z
        haut = z[r0, c0] * (1 - fc) + z[r0, c1] * fc
        bas = z[r1, c0] * (1 - fc) + z[r1, c1] * fc
        return haut * (1 - fr) + bas * fr

    def fenetre_disque(self, x, y, rayon):
        """Fenêtre (lig0, col0, n_lig, n_col) englobant le disque, coupée au MNT."""
        col, lig = self.monde_vers_pixel(x, y)
        demi_c = int(np.ceil(rayon / self.dx)) + 1
        demi_l = int(np.ceil(rayon / self.dy)) + 1
        c_centre = int(np.floor(col + 0.5))
        l_centre = int(np.floor(lig + 0.5))
        col0 = max(0, c_centre - demi_c)
        lig0 = max(0, l_centre - demi_l)
        col1 = min(self.nx, c_centre + demi_c + 1)
        lig1 = min(self.ny, l_centre + demi_l + 1)
        if col1 <= col0 or lig1 <= lig0:
            return None
        return lig0, col0, lig1 - lig0, col1 - col0
