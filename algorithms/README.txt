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
  * Help text: the two <img> tags pointing at kofi2.webp and ukraine.png were
    removed. Neither file ships in this package, so both rendered as empty
    boxes. The Ko-fi image was the sole content of its anchor, so the link
    itself was invisible; it is now a text link, which makes the upstream
    author's donation link visible rather than removing it. The peace message
    text is kept. An unclosed <b> and an invalid </font size> were fixed.
  * Code-quality pass required by the plugin repository scan, verified to
    change no numeric result (the viewshed rasters of a reference run are
    byte-identical before and after):
      - 21 bare `except:` handlers became `except Exception:` (Flake8 E722).
        This is the one behavioural difference: KeyboardInterrupt and
        SystemExit are no longer swallowed, so a running analysis can be
        cancelled instead of the cancellation being ignored.
      - `azimuth_1 != None` became `is not None` in modules/Raster.py (E711).
      - three variables named `l` were renamed to `missing`, `line` and
        `row` (E741).
      - tab indentation on 19 whitespace-only or help-string lines was
        converted to spaces (E101 / W191).
  * modules/visibility.py: initialised `min_index` before the loop that
    reads it. Upstream reads it before any assignment, which raises
    NameError as soon as the first pixel of a run carries an error >= 1
    (Flake8 F821). Behaviour is unchanged on the normal path.

This directory and its contents are distributed under the same GNU GPL v3
license. See LICENSE for the full terms.
