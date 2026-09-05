/*
 * Renders the complete configuration reference from the exported schema.
 *
 * docs/assets/data/config_schema.json is generated from the application's single
 * point of truth (src/config_web/schema.py) by scripts/export_config_schema.py.
 * Every parameter table on the configuration page comes from it, so a new field
 * appears in the docs as soon as the schema is re-exported — no hand-written
 * table to forget.
 *
 * Fields are grouped by their help_url anchor, which guarantees that every
 * anchor the application links to (src/web/js/config.js) exists on this page.
 */
(function () {
    "use strict";

    var LEVEL_ORDER = { getting_started: 0, standard: 1, expert: 2 };
    var LEVEL_LABEL = {
        getting_started: "Getting Started",
        standard: "Standard",
        expert: "Expert"
    };

    /* Human titles for the anchors the schema's help_url values point at. Any
     * anchor missing here still renders, using the section label as a fallback. */
    var ANCHOR_TITLE = {
        "data-source": "Data Source",
        "load": "Load",
        "eos": "Optimizer",
        "price": "Price",
        "price-sources": "Price Sources",
        "energyforecast": "Smart Price Prediction",
        "battery": "Battery",
        "battery-price": "Battery Energy Pricing",
        "pv-forecast": "PV Installations",
        "pv-forecast-sources": "PV Source",
        "pv-forecast-evcc": "PV Forecast via EVCC",
        "pv-autoscaling": "PV Auto-Scaling",
        "inverter": "Inverter",
        "evcc": "EVCC",
        "mqtt": "MQTT",
        "system": "System"
    };

    var BADGE = {
        restart_required: { cls: "badge-restart", icon: "fa-rotate", text: "restart" },
        deprecated: { cls: "badge-deprecated", icon: "fa-triangle-exclamation", text: "deprecated" },
        experimental: { cls: "badge-experimental", icon: "fa-flask", text: "experimental" }
    };

    function esc(value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function anchorOf(field) {
        var url = field.help_url || "";
        var hash = url.indexOf("#");
        return hash === -1 ? "" : url.slice(hash + 1);
    }

    function formatDefault(field) {
        if (field.type === "password") { return "••••"; }
        if (field.default === "" || field.default === null) { return "<em>empty</em>"; }
        if (typeof field.default === "boolean") { return field.default ? "true" : "false"; }
        return "<code>" + esc(field.default) + "</code>";
    }

    function constraints(field) {
        var v = field.validation || {};
        var parts = [];
        if (v.choices) {
            // A few fields accept "" to mean "inherit the global setting"; an
            // empty <code> would render as a stray comma.
            parts.push("one of " + v.choices.map(function (c) {
                return c === "" ? "<em>empty</em>" : "<code>" + esc(c) + "</code>";
            }).join(", "));
        }
        if (v.min !== undefined && v.max !== undefined) {
            parts.push("range " + esc(v.min) + "–" + esc(v.max));
        } else if (v.min !== undefined) {
            parts.push("minimum " + esc(v.min));
        } else if (v.max !== undefined) {
            parts.push("maximum " + esc(v.max));
        }
        if (field.depends_on) {
            var conds = Object.keys(field.depends_on).map(function (k) {
                var vals = field.depends_on[k];
                return "<code>" + esc(k) + "</code> is " +
                    (Array.isArray(vals) ? vals : [vals]).map(function (x) {
                        return "<code>" + esc(x) + "</code>";
                    }).join(" or ");
            });
            parts.push("only when " + conds.join(" and "));
        }
        return parts.length
            ? "<div class=\"param-desc\" style=\"font-size:0.85em\">" + parts.join(" &middot; ") + "</div>"
            : "";
    }

    function badges(field) {
        var out = (field.labels || []).map(function (label) {
            var b = BADGE[label];
            if (!b) { return ""; }
            return " <span class=\"badge " + b.cls + "\"><i class=\"fas " + b.icon +
                "\" aria-hidden=\"true\"></i>" + b.text + "</span>";
        }).join("");
        if (field.hot_reload) {
            out += " <span class=\"badge badge-hot\" title=\"Applies without a restart\">" +
                "<i class=\"fas fa-bolt\" aria-hidden=\"true\"></i>live</span>";
        }
        return out;
    }

    function row(field) {
        return "<tr>" +
            "<td class=\"param-key\"><code>" + esc(field.key) + "</code>" + badges(field) + "</td>" +
            "<td data-label=\"Type\">" + esc(field.type) + "</td>" +
            "<td data-label=\"Default\">" + formatDefault(field) + "</td>" +
            "<td data-label=\"Level\"><span class=\"badge badge-level\">" +
            esc(LEVEL_LABEL[field.level] || field.level) + "</span></td>" +
            "<td data-label=\"What it does\" class=\"param-desc\">" +
            esc(field.description) + constraints(field) + "</td>" +
            "</tr>";
    }

    function table(fields) {
        return "<div class=\"scroll-x\"><table class=\"param-table\">" +
            "<thead><tr><th>Parameter</th><th>Type</th><th>Default</th>" +
            "<th>Level</th><th>What it does</th></tr></thead><tbody>" +
            fields.map(row).join("") +
            "</tbody></table></div>";
    }

    function render(schema, level) {
        var maxLevel = LEVEL_ORDER[level] === undefined ? 2 : LEVEL_ORDER[level];
        var sectionMeta = schema.sections || {};
        var fields = schema.fields || [];

        // Preserve schema order for both sections and the anchor groups inside
        // them, so the page reads in the same order as the app's config UI.
        var sections = [];
        var bySection = {};
        fields.forEach(function (f) {
            if (!bySection[f.section]) {
                bySection[f.section] = { anchors: [], byAnchor: {} };
                sections.push(f.section);
            }
            var group = bySection[f.section];
            var anchor = anchorOf(f) || f.section;
            if (!group.byAnchor[anchor]) {
                group.byAnchor[anchor] = [];
                group.anchors.push(anchor);
            }
            group.byAnchor[anchor].push(f);
        });

        var html = "";
        var shown = 0;
        var hidden = 0;

        sections.forEach(function (name) {
            var group = bySection[name];
            var meta = sectionMeta[name] || {};
            var inner = "";

            // One anchor group usually just restates the section name ("Battery"
            // inside "Battery"). That anchor moves onto the section heading
            // instead, so the reader does not see the same word twice.
            var primary = null;
            group.anchors.forEach(function (a) {
                if (primary === null && (ANCHOR_TITLE[a] || a) === (meta.label || name)) {
                    primary = a;
                }
            });

            group.anchors.forEach(function (anchor) {
                var all = group.byAnchor[anchor];
                var visible = all.filter(function (f) {
                    return (LEVEL_ORDER[f.level] === undefined ? 2 : LEVEL_ORDER[f.level]) <= maxLevel;
                });
                shown += visible.length;
                hidden += all.length - visible.length;

                // The heading is rendered even when every field in it is above
                // the current level, so the anchor the app links to always
                // resolves. Only the table is dropped.
                var title = ANCHOR_TITLE[anchor] || meta.label || anchor;
                if (anchor !== primary) {
                    inner += "<h3 id=\"" + esc(anchor) + "\">" + esc(title) + "</h3>";
                }
                inner += visible.length
                    ? table(visible)
                    : "<p class=\"level-note\">" + all.length + " setting" +
                      (all.length === 1 ? "" : "s") +
                      " here are above your current detail level. Switch to " +
                      "Expert to see them.</p>";
            });

            // The heading carries the primary anchor where there is one; otherwise
            // a prefixed id that cannot collide with a help_url anchor.
            html += "<section class=\"param-section\">" +
                "<h2 id=\"" + esc(primary || ("ref-" + name)) + "\">" +
                "<i class=\"fas " + esc(meta.icon || "fa-cog") + "\" aria-hidden=\"true\"></i> " +
                esc(meta.label || name) + "</h2>" + inner + "</section>";
        });

        var legend =
            "<p class=\"param-legend\">" +
            "<span><span class=\"badge badge-restart\"><i class=\"fas fa-rotate\"></i>restart</span> needs a restart</span>" +
            "<span><span class=\"badge badge-hot\"><i class=\"fas fa-bolt\"></i>live</span> applies immediately</span>" +
            "<span><span class=\"badge badge-experimental\"><i class=\"fas fa-flask\"></i>experimental</span> may change</span>" +
            "<span><span class=\"badge badge-deprecated\"><i class=\"fas fa-triangle-exclamation\"></i>deprecated</span> do not use for new setups</span>" +
            "</p>";

        var summary = "<p class=\"level-note\">Showing " + shown + " of " +
            (shown + hidden) + " settings" +
            (hidden ? " — " + hidden + " more at a higher detail level." : ".") + "</p>";

        return summary + legend + html;
    }

    function mount() {
        var container = document.getElementById("schema-reference");
        if (!container) { return; }

        fetch("../assets/data/config_schema.json")
            .then(function (res) {
                if (!res.ok) { throw new Error("HTTP " + res.status); }
                return res.json();
            })
            .then(function (schema) {
                var drawn = false;
                var draw = function () {
                    var level = document.body.getAttribute("data-active-level") || "standard";
                    container.innerHTML = render(schema, level);
                    if (window.EOSDocs && window.EOSDocs.buildTOC) {
                        window.EOSDocs.buildTOC();
                    }
                    // The application deep-links to anchors that live in here, and
                    // they do not exist until this first render finishes — so the
                    // browser's own jump has already failed by now. Redo it once.
                    if (!drawn) {
                        drawn = true;
                        var hash = window.location.hash.slice(1);
                        if (hash) {
                            var target = document.getElementById(decodeURIComponent(hash));
                            if (target) { target.scrollIntoView(); }
                        }
                    }
                };
                draw();
                // Re-render whenever the reader changes the detail level.
                new MutationObserver(function (records) {
                    if (records.some(function (r) { return r.attributeName === "data-active-level"; })) {
                        draw();
                    }
                }).observe(document.body, { attributes: true, attributeFilter: ["data-active-level"] });
            })
            .catch(function (err) {
                container.innerHTML =
                    "<div class=\"alert alert-warning\"><p>The parameter reference could not be " +
                    "loaded (" + esc(err.message) + "). It is generated from " +
                    "<code>assets/data/config_schema.json</code>; when reading these pages from " +
                    "disk rather than over HTTP, your browser will block that request.</p></div>";
            });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mount);
    } else {
        mount();
    }
}());
