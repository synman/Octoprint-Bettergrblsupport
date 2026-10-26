/*
 * Better Grbl Support: Grbl-aware built-in G-code viewer.
 *
 * Wraps the built-in viewer's GCODE.gCodeReader at runtime. No OctoPrint file is changed.
 *   - loadFile(reader): only reader.url is rewritten, to a Bettergrbl route that streams a
 *     normalised copy of the stored file (absolute G0/G1 in mm, cuts marked with E).
 *   - getCmdIndexForPercentage(p): print progress arrives as a percentage of the ORIGINAL file's
 *     bytes; the stored command percentages are bytes of the normalised stream, so p is translated
 *     through the position map served by /viewer-map/.
 * If the seam is missing the built-in viewer is left untouched. Nothing here throws.
 *
 * Seams (OctoPrint 2.0.0rc5 / 1.11.8):
 *   viewer/reader.js:211 loadFile, :303 / :302 getCmdIndexForPercentage
 *   gcodeviewer.js:546 / :532 caller looks up GCODE.gCodeReader.loadFile on every call
 */
$(function () {
    function BgsViewerViewModel(params) {
        var self = this;
        var profiles = params[0];
        var settings = params[1];
        var map = null;
        var mapFor = null;
        var noted = false;

        function note(msg) {
            if (noted) return;
            noted = true;
            try {
                console.info("BGS viewer: " + msg);
            } catch (e) {}
        }

        function active() {
            var s =
                settings &&
                settings.settings &&
                settings.settings.plugins &&
                settings.settings.plugins.bettergrblsupport;
            if (s && typeof s.viewerNormalize === "function" && s.viewerNormalize() === false) {
                return false;
            }
            return !!profiles && typeof profiles.currentProfile === "function" && profiles.currentProfile() === "_bgs";
        }

        function encodePath(p) {
            return String(p).split("/").map(encodeURIComponent).join("/");
        }

        // orig% -> norm%, by binary search over the position map with linear interpolation.
        function translate(m, p) {
            var a = m.orig;
            var b = m.norm;
            var hi = a.length - 1;
            var lo = 0;
            if (hi < 0 || !m.orig_size || !m.norm_size) return p;
            var pos = (p * m.orig_size) / 100;
            if (pos <= a[0]) return (100 * b[0] * (a[0] ? pos / a[0] : 0)) / m.norm_size;
            if (pos >= a[hi]) return (100 * b[hi]) / m.norm_size;
            while (hi - lo > 1) {
                var mid = (lo + hi) >> 1;
                if (a[mid] <= pos) lo = mid;
                else hi = mid;
            }
            var f = (pos - a[lo]) / (a[hi] - a[lo]);
            return (100 * (b[lo] + f * (b[hi] - b[lo]))) / m.norm_size;
        }

        // Grow the drawn bed so it is always a little bigger than the model (never smaller than the profile's).
        function fitBed(bounds) {
            try {
                var p = profiles.currentProfileData && profiles.currentProfileData();
                var v = p && p.volume;
                if (!v || !bounds || v.formFactor() === "circular") return;
                var centered = v.origin && v.origin() === "center";
                function size(profile, lo, hi) {
                    var extent = centered ? 2 * Math.max(Math.abs(lo), Math.abs(hi)) : Math.max(hi, 0);
                    var margin = Math.max(10, extent * 0.1);
                    return Math.max(profile, Math.ceil((extent + margin) / 10) * 10);
                }
                var bed = {
                    x: size(v.width(), bounds[0], bounds[2]),
                    y: size(v.depth(), bounds[1], bounds[3]),
                    r: undefined,
                    circular: false,
                    centeredOrigin: centered
                };
                GCODE.ui.updateOptions({renderer: {bed: bed}, reader: {bed: bed}});
            } catch (e) {
                note("bed resize failed: " + e);
            }
        }

        // A single-layer job has nothing to step through: hide the layer slider and the previous/next layer
        // buttons for it (a class on <body>; multi-layer jobs and the stock viewer keep them).
        function hideLayerControls(on) {
            try {
                if (!document.getElementById("bgs-viewer-style")) {
                    $("<style id='bgs-viewer-style'>" +
                        "body.bgs-viewer #gcode #gcode_layer_slider, body.bgs-viewer #gcode .layer-buttons {display: none !important;}" +
                        "</style>").appendTo("head");
                }
                $("body").toggleClass("bgs-viewer", !!on);
            } catch (e) {
                note("layer control toggle failed: " + e);
            }
        }

        function install(final) {
            var r = window.GCODE && window.GCODE.gCodeReader;
            if (!r || typeof r.loadFile !== "function" || typeof r.getCmdIndexForPercentage !== "function") {
                if (final) note("viewer seam missing; built-in viewer left untouched");
                return;
            }
            if (r.__bgsWrapped) return;

            var origLoad = r.loadFile;
            var origPct = r.getCmdIndexForPercentage;

            r.loadFile = function (reader) {
                var arg = reader;
                map = null;
                mapFor = null;
                hideLayerControls(false);
                try {
                    var storage = (reader && reader.storage) || "local"; // 1.11 passes no storage
                    if (reader && reader.path && storage === "local" && active()) {
                        var rel = "plugin/bettergrblsupport/";
                        var tail = encodeURIComponent(storage) + "/" + encodePath(reader.path);
                        var path = reader.path;
                        // skipUntil matches text in the original file, never in the normalised copy
                        arg = $.extend({}, reader, {url: BASEURL + rel + "viewer/" + tail, skipUntil: undefined});
                        mapFor = path;
                        var that = this;
                        // the map carries the model bounds, and the reader takes the bed size at load time,
                        // so wait for it before loading
                        OctoPrint.get(rel + "viewer-map/" + tail)
                            .done(function (m) {
                                if (mapFor === path) {
                                    map = m;
                                    fitBed(m && m.bounds);
                                    hideLayerControls(!!m && m.layers <= 1);
                                }
                            })
                            .fail(function () {
                                if (mapFor === path) {
                                    map = null; // passthrough -> identity
                                    fitBed([0, 0, 0, 0]); // back to the profile's bed
                                }
                            })
                            .always(function () {
                                if (mapFor === path) origLoad.call(that, arg);
                            });
                        return;
                    }
                } catch (e) {
                    arg = reader;
                    mapFor = null;
                    note("loadFile redirect failed: " + e);
                }
                return origLoad.call(this, arg);
            };

            r.getCmdIndexForPercentage = function (p) {
                var q = p;
                try {
                    if (map) q = translate(map, p);
                } catch (e) {
                    q = p;
                    note("progress translation failed: " + e);
                }
                return origPct.call(this, q);
            };

            r.__bgsWrapped = true;
        }

        install(false); // every asset script is loaded before view models are built
        self.onStartupComplete = function () {
            install(true); // second try, idempotent via __bgsWrapped
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: BgsViewerViewModel,
        dependencies: ["printerProfilesViewModel", "settingsViewModel"],
        elements: []
    });
});
