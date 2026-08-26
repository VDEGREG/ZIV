# -*- coding: utf-8 -*-
"""
ZIV_prime — shared IGN Géoplateforme data helpers.

Utilities shared by the ZIV landcover masking and the photo-viewpoints
generator:

  * `_download_wfs_layer`      : bbox-filtered WFS GetFeature -> GeoJSON
  * `burn_into_raster_inplace` : GDAL-API vector burn into an existing
                                 raster (never the QGIS gdal:rasterize_over*
                                 wrappers, which segfault in the Processing
                                 worker thread - cf. v3.10.x lessons)

History: this module is the surviving core of the former `fallback_dem.py`
(experimental "MNS artificiel" mode, removed in v3.13.0 - it never worked
reliably and was judged not useful enough to harden).

Third-party origin: the IGN download logic these helpers support is
derived from the open-source QGIS plugin "PointCloudFR" by Samy KHELIL
(https://github.com/sameeeyy/PointCloudFR), GNU GPL v2 or later. See
LICENSE and README.md at the root of the plugin.

All Géoplateforme codes were verified live via WFS DescribeFeatureType /
GetFeature requests on 2026-06-26 / 2026-06-30.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from qgis.core import QgsRectangle


# -------------------------------------------------------------------- #
# Géoplateforme endpoint + layer codes (verified live)
# -------------------------------------------------------------------- #

WFS_BASE_URL = "https://data.geopf.fr/wfs/ows"

WFS_BUILDINGS = "BDTOPO_V3:batiment"             # `hauteur` xsd:double
WFS_FOREST = "LANDCOVER.FORESTINVENTORY.V2:formation_vegetale"
# BDTOPO road segments (LineString) - used by the photo-viewpoints
# generator to keep candidate points on accessible ground.
WFS_ROADS = "BDTOPO_V3:troncon_de_route"


# -------------------------------------------------------------------- #
# WFS layer download (single bbox call -> GeoJSON file)
# -------------------------------------------------------------------- #


def _download_wfs_layer(
    bbox: QgsRectangle,
    type_name: str,
    output_path: Path,
    logger,
) -> bool:
    """Download a WFS FeatureCollection limited to the bbox as GeoJSON.

    Géoplateforme WFS accepts the standard WFS 2.0.0 GetFeature with
    BBOX filter. We request `application/json` so the file can be
    loaded back into QGIS without an XSD.
    """
    import requests

    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": type_name,
        "SRSNAME": "EPSG:2154",
        "BBOX": (
            f"{bbox.xMinimum()},{bbox.yMinimum()},"
            f"{bbox.xMaximum()},{bbox.yMaximum()},EPSG:2154"
        ),
        "OUTPUTFORMAT": "application/json",
        "COUNT": "10000",
    }
    try:
        resp = requests.get(WFS_BASE_URL, params=params, timeout=(15, 120))
        if resp.status_code != 200:
            logger.warning(
                f"WFS IGN: {type_name} HTTP {resp.status_code}"
            )
            return False
        # Quick sanity check: must be JSON with at least an empty
        # features list, not an XML ExceptionReport.
        content = resp.text or ""
        if not content.lstrip().startswith("{"):
            logger.warning(
                f"WFS IGN: {type_name} returned non-JSON "
                f"(first 200 chars: {content[:200]!r})"
            )
            return False
        output_path.write_text(content, encoding="utf-8")
        # Sanity check the result has at least 1 feature
        if '"features"' in content and '"features":[]' in content.replace(
            " ", ""
        ):
            logger.warning(
                f"WFS IGN: {type_name} returned 0 features."
            )
            return False
        logger.info(
            f"WFS IGN: {type_name} -> "
            f"{output_path.name} ({len(content)} bytes)"
        )
        return True
    except Exception as e:
        logger.warning(f"WFS IGN: {type_name} exception: {e}")
        return False


# -------------------------------------------------------------------- #
# GDAL-API in-place vector burn
# -------------------------------------------------------------------- #


def burn_into_raster_inplace(
    raster_path: str,
    vector_path: str,
    burn_value: Optional[float] = None,
    field: Optional[str] = None,
    add: bool = True,
    logger=None,
) -> bool:
    """Burn a vector into an existing raster IN PLACE via the GDAL Python
    API (`gdal.Rasterize` on a GA_Update dataset, band 1).

    Used instead of the QGIS `gdal:rasterize_over*` processing wrappers,
    which crashed QGIS when run inside the Processing worker thread (the
    wrapper loads INPUT_RASTER as a QgsRasterLayer; on rasters with a
    non-standard CRS WKT this segfaulted). The bare GDAL call matches the
    `gdal_rasterize` CLI, which was verified to work on the exact same
    files (2026-06-30).

    Provide either `burn_value` (fixed value) or `field` (per-feature
    attribute). `add=True` adds to the existing pixel value; `add=False`
    overwrites it. Returns True on success.
    """
    try:
        from osgeo import gdal
    except Exception as e:
        if logger:
            logger.warning(f"burn_into_raster: osgeo indisponible ({e}).")
        return False
    try:
        gdal.UseExceptions()
        opt_kwargs = dict(bands=[1], add=bool(add))
        if field is not None:
            opt_kwargs["attribute"] = field
        else:
            opt_kwargs["burnValues"] = [float(burn_value)]
        opts = gdal.RasterizeOptions(**opt_kwargs)
        ds = gdal.Open(raster_path, gdal.GA_Update)
        if ds is None:
            if logger:
                logger.warning(
                    f"burn_into_raster: ouverture update impossible "
                    f"({raster_path})."
                )
            return False
        try:
            gdal.Rasterize(ds, vector_path, options=opts)
            ds.FlushCache()
        finally:
            ds = None
        return True
    except Exception as e:
        if logger:
            logger.warning(f"burn_into_raster: échec ({e}).")
        return False
