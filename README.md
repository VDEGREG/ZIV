# Zones d'influence visuelle (ZIV)

> Package name: `ZIV_prime`

QGIS plugin computing **visual influence zones** — viewsheds, also called *ZTV*
(zone of theoretical visibility) or *ZVI* (zone of visual influence) in landscape
impact assessment — from national high-resolution LiDAR elevation data.

Typical use: assessing how far, and from where, a planned installation — solar
farm, wind turbines, buildings — would be visible in the surrounding landscape.

- **Author:** G. DELSIGNE
- **License:** GNU General Public License v3 or later (see [LICENSE](LICENSE))
- **QGIS:** 3.34 → 4.x (Qt5 and Qt6)

## Status

**Version 1.0.0 is marked experimental, on purpose.** The point of this line is
support for **more than one country**, and only the France chain is in it so far:
functionally, this version is the same plugin as its parent `ZIV_prime` 1.5.3.

| Country | Elevation | Status |
|---|---|---|
| France | IGN LiDAR HD (MNT / MNS / MNH / point clouds) | shipped, unchanged from `ZIV_prime` 1.5.3 |
| United States (contiguous) | USGS 3DEP, LANDFIRE, USA Structures, TIGER/Line | **next version** — services measured, chain not written |

The five American services have been surveyed against the live endpoints and
measured, not guessed: see `scripts/mesurer_services_us.py` in the project tree,
which prints twenty-one facts — endpoints, record caps, units, coordinate systems,
error behaviour — and stays there as a regression check.

## One plugin, two countries

This was a separate line for a while. The two-country chain grew as its own
package from version 1.5.3 of this plugin, with renamed technical identities so
that both could be installed side by side while the American side was built.

On 2026-09-17 it was folded back in: the identities below are those of the
published plugin again, so the people already using it receive an **update**
rather than meeting a stranger — and find their stored preferences where they
left them.

| Element | Value |
|---|---|
| Package folder | `ZIV_prime` |
| Main provider | `genziv` |
| Main algorithm | `genziv:generate` |
| Visibility provider | `pcfr_vis` |
| QgsSettings namespace | `plugins/ZIV_prime/` |
| Log file prefix | `genziv_` |

Every fix shipped in the single-country line up to 1.5.6 is carried here,
checked one by one: the IGN tile-layer withdrawal, a service outage no longer
reported as a gap in coverage, and spaced-out download retries.

Field names of the produced layers (`azimut`, `categorie`, `observ_hgt`…) are
**unchanged** — existing Processing models depend on them.

## What it does

From an area of interest, the plugin:

1. lists the elevation tiles that intersect the AOI and downloads them, at the
   working resolution, directly from the national elevation service;
2. assembles them into a single elevation raster, with an on-disk cache and a
   JSON manifest;
3. computes the **viewshed** over that terrain with the embedded visibility
   engine (horizon propagation by sectors, each cell visited exactly once);
4. masks out woodland and buildings;
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
| `import_dalles/` | Tile discovery, download and mosaicking |
| `moteur_visibilite/` | The visibility engine (horizon propagation) |
| `algorithmes_ziv/` | The two Processing algorithms built on the engine |
| `donnees_ign.py` | Géoplateforme vector layers (WFS) and raster burning (masking) |
| `emprise_ziv.py` | AOI alignment, analysis buffer, buffer shapefile |
| `masquage.py` | Woodland / building masking of the result |
| `journal_ziv.py` | Logging: Processing output, QGIS message log, dated file on disk |
| `models/creation_ziv.model3` | The Processing model wiring the chain |
| `preferences_dialog.py` | Plugin preferences |

## Language

The plugin's **display name** is in English; the rest of the interface is still
in French. The next version internationalises it properly — English source
strings with a French translation shipped as a `.qm`, so nothing is lost for
existing users.

## Installation

Download the ZIP and use *Plugins → Manage and Install Plugins → Install from
ZIP*. Experimental plugins are hidden in the plugin manager unless you enable
*Show also experimental plugins* in its settings.

## License

`ZIV_prime` is free software, released under the **GNU General Public License
v3 or later**. See [LICENSE](LICENSE) for the full terms.

Elevation and landcover data for France © IGN — *Licence Ouverte / Open Licence*.
