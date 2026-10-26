# bgs_viewer.js: wiring notes

Wired on 2026-10-06: routes and cache in `_viewer.py` plus the BlueprintPlugin routes in `__init__.py`, `bgs_viewer.js`
in `get_assets`, `hideGCodeTab` default False with a settings v9 migration, and a `viewerNormalize` toggle in settings.
The notes below are the original plan; re-find code by the quoted text, not by line number.

## 1. Asset registration (`get_assets`, `__init__.py` ~695-703)
Add `'js/bgs_viewer.js'` to the `js=[...]` list. No css or less is needed. The file registers a view model with
`elements: []` (no DOM binding), depending on `printerProfilesViewModel` and `settingsViewModel`.

## 2. The route it calls (not yet written; spec section 2 of viewer-design.md)
Both are same-origin GETs under `/plugin/bettergrblsupport/`, using the BlueprintPlugin mixin:
- `GET viewer/<storage>/<path:filename>`: normalised text, with header `X-Original-Content-Length` = normalised byte size
  (the stock worker takes its progress denominator from it, worker.js:317).
- `GET viewer-map/<storage>/<path:filename>`: JSON `{version, orig_size, norm_size, orig:[...], norm:[...]}`.
  A 404 or any failure makes the JS use an identity map, which is the stock behaviour.
The JS builds `BASEURL + "plugin/bettergrblsupport/viewer/" + encodeURIComponent(storage) + "/" + path` with each path
segment `encodeURIComponent`-ed, and requests the map through `OctoPrint.get("plugin/bettergrblsupport/viewer-map/...")`.
The JS acts only when storage is `local` (1.11 passes none and is treated as `local`) and the current printer profile id is `_bgs`.

## 3. `hideGCodeTab` change (runtime wrap needs the built-in plugin enabled)
- Default: `self.hideGCodeTab = True` (~60) and `hideGCodeTab = True,` in `get_settings_defaults` (~211) must become False.
- Branch in `on_settings_load`/migration (~415-428, text `# disable the gcodeviewer plugin`): with False it already removes
  `"gcodeviewer"` from disabled plugins and `"plugin_gcodeviewer"` from disabled tabs, setting `restart_required`. Existing installs
  that stored True keep the viewer disabled until the stored value is reset; migration is part of the later wiring.
- `_bgs.py` ~136-140 (uninstall) already re-enables both; no change needed.
- Optional setting `viewerNormalize` (default True). The JS reads `settings.plugins.bettergrblsupport.viewerNormalize()`
  and treats a missing observable as on.
- A restart is needed for the plugin enable/disable and for the new blueprint.

## 4. How the wrap behaves (verified by the headless check)
- `GCODE.gCodeReader.loadFile(reader)`: only `reader.url` is replaced; `path`, `size`, `skipUntil`, `storage` are untouched, so the
  viewer's job-equality check and skipUntil keep working.
- `GCODE.gCodeReader.getCmdIndexForPercentage(p)`: `p` (percent of original bytes) is translated to percent of normalised bytes.
- Missing `GCODE`, `loadFile` or `getCmdIndexForPercentage` (checked at view-model construction and again at `onStartupComplete`):
  nothing is installed and one `console.info` is written once.
- Seams: 2.0.0rc5 reader.js:211 / 303, gcodeviewer.js:546; 1.11.8 reader.js:211 / 302, gcodeviewer.js:532.
