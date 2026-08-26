# Zones d'influence visuelle (ZIV)

> Package name: `ZIV_prime`

QGIS plugin computing **Visual Influence Zones** (in French *zones d'influence
visuelle*, ZIV) from the French IGN **LiDAR HD** elevation data.

Typical use: assessing how far, and from where, a planned installation — solar
farm, wind turbines, buildings — would be visible in the surrounding landscape.

- **Author:** G. DELSIGNE
- **License:** GNU General Public License v3 or later (see [LICENSE](LICENSE))
- **QGIS:** 3.34 → 4.x (Qt5 and Qt6)

## What it does

From an area of interest, the plugin:

1. queries the IGN Géoplateforme WFS for the LiDAR HD tiles that intersect the
   AOI (terrain model, surface model, canopy height model or raw point clouds);
2. downloads them in parallel, with an on-disk cache and a JSON manifest;
3. merges and resamples them into a single elevation raster;
4. computes the **viewshed** over that terrain;
5. masks out woodland (IGN BD Forêt) and buildings (IGN BD TOPO);
6. optionally suggests **photo viewpoints** for photomontages, spread across
   disjoint visible patches and across distance rings, placed on roads or near
   buildings rather than in the middle of a field.

The visibility algorithms ship with the plugin as an internal Processing
provider (`pcfr_vis`), so no companion plugin has to be installed. The only
Python dependencies are `requests` and `numpy`, both already bundled with QGIS.

## Installation

Install from the QGIS plugin manager, or download the ZIP and use
*Plugins → Manage and Install Plugins → Install from ZIP*.

## Credits

**This plugin stands on two open-source QGIS plugins and would not exist
without them.** Both are GPL-licensed and their notices are preserved here as
required by the license.

| Component | Upstream plugin | Author | License |
|---|---|---|---|
| Visibility engine (`createviewpoints`, `viewshed`) | [Visibility Analysis](https://github.com/zoran-cuckovic/QGIS-visibility-analysis) | **Zoran Cuckovic** | GNU GPL v3 |
| IGN LiDAR HD tile discovery and download | [PointCloudFR](https://github.com/sameeeyy/PointCloudFR) | **Samy KHELIL** | GNU GPL v2 or later |

Modifications made for ZIV_prime are listed in
[`algorithms/README.txt`](algorithms/README.txt) for the visibility part, and
in the module headers of `lidar_algorithm.py` / `ign_data.py` for the download
part.

Elevation and landcover data © IGN — *Licence Ouverte / Open Licence*.

## License

ZIV_prime is free software, released under the **GNU General Public License
v3 or later**. Combining GPL-3.0 code (Visibility Analysis) with
GPL-2.0-or-later code (PointCloudFR) results in a GPL-3.0-or-later work.
See [LICENSE](LICENSE) for the full terms.
