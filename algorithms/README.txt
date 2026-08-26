ZIV_prime — visibility algorithms (third-party origin)
============================================================

The two algorithms shipped in this directory (createviewpoints / viewshed)
and their supporting modules (modules/Points.py, modules/Raster.py,
modules/visibility.py) are derived from the open-source QGIS plugin
"Visibility Analysis" (originally authored by Z. Cuckovic, released under
the GNU General Public License v3).

This GPL notice is preserved as required by section 5 of the GPL v3
license (see LICENSE in this directory for the full license text).

Modifications made for the ZIV_prime plugin by G. DELSIGNE:
  * Qt5/Qt6 compatibility through the QGIS PyQt abstraction layer.
  * Removed external ProcessingConfig dependency (inlined a sensible
    default memory buffer size).
  * Registered under provider id `pcfr_vis` instead of `visibility` to
    avoid collisions when a third-party visibility plugin is installed
    in parallel.
  * Stripped unused algorithms not required by the ZIV_prime
    processing model.

This directory and its contents are distributed under the same GNU GPL v3
license. See LICENSE for the full terms.
