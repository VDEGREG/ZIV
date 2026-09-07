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

1. lists the IGN LiDAR HD tiles that intersect the AOI (terrain model, surface
   model, canopy height model or raw point clouds) and downloads them, at the
   working resolution, directly from the IGN Géoplateforme services;
2. assembles them into a single elevation raster, with an on-disk cache and a
   JSON manifest;
3. computes the **viewshed** over that terrain with the embedded visibility
   engine (horizon propagation by sectors, each cell visited exactly once);
4. masks out woodland (IGN BD Forêt) and buildings (IGN BD TOPO);
5. optionally suggests **photo viewpoints** for photomontages, spread across
   disjoint visible patches and across distance rings, placed on roads or near
   buildings rather than in the middle of a field.

The two Processing algorithms (`createviewpoints`, `viewshed`) ship with the
plugin through an internal Processing provider, so no companion plugin has to
be installed. The only Python dependencies are `requests` and `numpy`, both
already bundled with QGIS.

## Layout

| Path | Role |
|---|---|
| `lidar_algorithm.py` | The main Processing algorithm (`genziv:generate`): orchestration of the whole chain |
| `import_dalles/` | IGN LiDAR HD tile discovery, download and mosaicking |
| `moteur_visibilite/` | The visibility engine (horizon propagation) |
| `algorithmes_ziv/` | The two Processing algorithms built on the engine |
| `donnees_ign.py` | Géoplateforme vector layers (WFS) and raster burning (masking) |
| `emprise_ziv.py` | AOI alignment, analysis buffer, buffer shapefile |
| `masquage.py` | Woodland / building masking of the ZIV |
| `journal_ziv.py` | Logging: Processing output, QGIS message log, dated file on disk |
| `models/creation_ziv.model3` | The Processing model wiring the chain |
| `preferences_dialog.py` | Plugin preferences |

## History

The plugin began as an extraction of the ZIV tool from an earlier internal
plugin. Since **1.3.0** the visibility engine, since **1.4.0** the LiDAR HD
tile import, and since **1.5.0** the two Processing algorithms are original
code written for this plugin — **1.5.1 removed the last third-party mentions**:
the package now contains no third-party code at all, while keeping the same
behaviour, algorithm ids, parameter names and output conventions throughout.

## Installation

Install from the QGIS plugin manager, or download the ZIP and use
*Plugins → Manage and Install Plugins → Install from ZIP*.

## Technical identities kept stable

Some identifiers are part of the plugin's technical identity and are kept
unchanged so that existing Processing models keep working:

- package folder `ZIV_prime`;
- main provider id `genziv`, algorithm `genziv:generate`;
- internal visibility provider id `pcfr_vis` (class `ZivVisibilityProvider`),
  algorithms `pcfr_vis:createviewpoints` and `pcfr_vis:viewshed`;
- QgsSettings namespace `plugins/ZIV_prime/`.

Elevation and landcover data © IGN — *Licence Ouverte / Open Licence*.

## License

ZIV_prime is free software, released under the **GNU General Public License
v3 or later**.
See [LICENSE](LICENSE) for the full terms.
