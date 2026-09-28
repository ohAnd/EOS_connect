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
 *
 * The same data drives the per-profile tables in the Managed Loads section. A
 * managed load's fields already carry depends_on: {type: [...]}, so which settings
 * a sauna has is a fact the schema knows - there is no second list to maintain.
 * Their starting values come from managed_load_presets, exported alongside the
 * fields from src/loads/presets.py, because a FieldDef holds one default for a
 * field four appliance types share and that default is the pool's.
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

    /* Every string the per-profile tables render. Collected rather than scattered
     * through the template literals below, so translating this file later is one
     * map to replace. */
    var PROFILE_TEXT = {
        noLimit: "no limit",
        startsAt: "Starts at",
        aboveLevel: function (n, label) {
            return n + " more setting" + (n === 1 ? "" : "s") + " for this profile " +
                "at " + label + " level.";
        },
        unknown: function (type) {
            return "No profile named \u201c" + type + "\u201d in the schema.";
        }
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

    /* Ids handed out by the render in progress. Reset by render(); null outside
     * one, so a stray call cannot accumulate. */
    var claimed = null;

    /* An anchor may only be written once on the page. Two sources would otherwise
     * claim the same one, and two elements with one id put the section twice in
     * the contents and send every deep link to whichever came first:
     *
     *   - a prose section above already explains the setting and owns its anchor,
     *     so the table down here yields and is reached through its "ref-" id;
     *   - two schema sections can share a help_url (pv_forecast_source and
     *     pv_forecast both use #pv-forecast), and only one may carry it.
     *
     * Returns an id attribute for the first free candidate, or none at all. */
    function idAttr(anchor, fallback) {
        var candidates = [anchor, fallback];
        for (var i = 0; i < candidates.length; i++) {
            var id = candidates[i];
            if (!id || document.getElementById(id) || (claimed && claimed[id])) {
                continue;
            }
            if (claimed) { claimed[id] = true; }
            return " id=\"" + esc(id) + "\"";
        }
        return "";
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

    /* A preset value, where null is meaningful rather than absent: a sauna's
     * window_start is null because a sauna has no allowed window, which is not the
     * same as the field being left blank. */
    function formatPreset(field, value) {
        if (field.type === "password") { return "••••"; }
        if (value === null) { return "<em>" + PROFILE_TEXT.noLimit + "</em>"; }
        if (value === "") { return "<em>empty</em>"; }
        if (typeof value === "boolean") { return value ? "true" : "false"; }
        return "<code>" + esc(value) + "</code>";
    }

    function constraints(field, skip) {
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
            var keys = Object.keys(field.depends_on).filter(function (k) {
                return (skip || []).indexOf(k) === -1;
            });
            var conds = keys.map(function (k) {
                var vals = field.depends_on[k];
                return "<code>" + esc(k) + "</code> is " +
                    (Array.isArray(vals) ? vals : [vals]).map(function (x) {
                        return "<code>" + esc(x) + "</code>";
                    }).join(" or ");
            });
            if (conds.length) {
                parts.push("only when " + conds.join(" and "));
            }
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

    function row(field, opts) {
        var o = opts || {};
        return "<tr>" +
            "<td class=\"param-key\"><code>" + esc(o.key || field.key) + "</code>" +
            badges(field) + "</td>" +
            "<td data-label=\"Type\">" + esc(field.type) + "</td>" +
            "<td data-label=\"" + esc(o.defaultLabel || "Default") + "\">" +
            (o.defaultHtml || formatDefault(field)) + "</td>" +
            "<td data-label=\"Level\"><span class=\"badge badge-level\">" +
            esc(LEVEL_LABEL[field.level] || field.level) + "</span></td>" +
            "<td data-label=\"What it does\" class=\"param-desc\">" +
            esc(field.description) + constraints(field, o.skip) + "</td>" +
            "</tr>";
    }

    function table(fields, rowFor, defaultLabel) {
        return "<div class=\"scroll-x\"><table class=\"param-table\">" +
            "<thead><tr><th>Parameter</th><th>Type</th><th>" +
            esc(defaultLabel || "Default") + "</th>" +
            "<th>Level</th><th>What it does</th></tr></thead><tbody>" +
            fields.map(rowFor || function (f) { return row(f); }).join("") +
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

        // One anchor group usually just restates the section name ("Battery"
        // inside "Battery"). That anchor moves onto the section heading instead,
        // so the reader does not see the same word twice.
        sections.forEach(function (name) {
            var group = bySection[name];
            var label = (sectionMeta[name] || {}).label || name;
            group.anchors.forEach(function (a) {
                if (group.primary === undefined && (ANCHOR_TITLE[a] || a) === label) {
                    group.primary = a;
                }
            });
            if (group.primary === undefined) { group.primary = null; }
        });

        // Section headings claim their ids before any group heading does. Where
        // two sections share an anchor, it belongs on the one the anchor is named
        // after - #pv-forecast on "PV Installations", not on a subheading of
        // "PV Source" that merely happens to carry the same help_url.
        claimed = {};
        var sectionId = {};
        sections.forEach(function (name) {
            sectionId[name] = idAttr(bySection[name].primary || ("ref-" + name),
                                     "ref-" + name);
        });

        var html = "";
        var shown = 0;
        var hidden = 0;

        sections.forEach(function (name) {
            var group = bySection[name];
            var meta = sectionMeta[name] || {};
            var inner = "";
            var primary = group.primary;

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
                    inner += "<h3" + idAttr(anchor) + ">" + esc(title) + "</h3>";
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
                "<h2" + sectionId[name] + ">" +
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

        claimed = null;

        var summary = "<p class=\"level-note\">Showing " + shown + " of " +
            (shown + hidden) + " settings" +
            (hidden ? " — " + hidden + " more at a higher detail level." : ".") + "</p>";

        return summary + legend + html;
    }

    /* ------------------------------------------------ per-profile parameters */

    /* Which settings a profile has. depends_on.type is the schema's own answer -
     * it is what the application's config form filters on - so this cannot drift
     * from what the UI shows. A field with no type condition applies to all six. */
    function fieldsForProfile(schema, type) {
        return (schema.fields || []).filter(function (f) {
            if (f.section !== "managed_loads") { return false; }
            var dep = f.depends_on && f.depends_on.type;
            if (!dep) { return true; }
            return (Array.isArray(dep) ? dep : [dep]).indexOf(type) !== -1;
        });
    }

    function presetValue(schema, type, field) {
        var dot = field.key.indexOf(".");
        var sub = dot === -1 ? field.key : field.key.slice(dot + 1);
        // No preset names the type - it is the key they are looked up by. In this
        // table it is not open anyway: the schema default is the pool's, and
        // printing that against a sauna would be simply wrong.
        if (sub === "type") { return type; }
        var preset = (schema.managed_load_presets || {})[type];
        if (!preset || !preset.defaults) { return undefined; }
        return Object.prototype.hasOwnProperty.call(preset.defaults, sub)
            ? preset.defaults[sub]
            : undefined;
    }

    function renderProfile(schema, type, maxLevel) {
        var fields = fieldsForProfile(schema, type);
        if (!fields.length) {
            return "<p class=\"level-note\">" + esc(PROFILE_TEXT.unknown(type)) + "</p>";
        }

        // Group by display_group, in the order the schema lists them, so the tables
        // read in the same order as the cards in the application's settings form.
        var order = [];
        var byGroup = {};
        fields.forEach(function (f) {
            var g = f.display_group || "";
            if (!byGroup[g]) { byGroup[g] = []; order.push(g); }
            byGroup[g].push(f);
        });

        var html = "";
        var hidden = 0;
        order.forEach(function (group) {
            var all = byGroup[group];
            var visible = all.filter(function (f) {
                return (LEVEL_ORDER[f.level] === undefined ? 2 : LEVEL_ORDER[f.level]) <= maxLevel;
            });
            hidden += all.length - visible.length;
            if (!visible.length) { return; }

            html += "<h4 class=\"param-group\">" + esc(group) + "</h4>" +
                table(visible, function (f) {
                    var preset = presetValue(schema, type, f);
                    var dot = f.key.indexOf(".");
                    return row(f, {
                        // The stored key is managed_loads.<n>.<field>; the index is
                        // the entry's position and says nothing useful here.
                        key: dot === -1 ? f.key : f.key.slice(dot + 1),
                        defaultLabel: PROFILE_TEXT.startsAt,
                        defaultHtml: preset === undefined
                            ? formatDefault(f)
                            : formatPreset(f, preset),
                        // The table is already this profile's, so repeating
                        // "only when type is sauna" on every row says nothing.
                        skip: ["type"]
                    });
                }, PROFILE_TEXT.startsAt);
        });

        if (hidden) {
            html += "<p class=\"level-note\">" +
                esc(PROFILE_TEXT.aboveLevel(hidden, LEVEL_LABEL.expert)) + "</p>";
        }
        return html;
    }

    function renderProfiles(schema, level) {
        var maxLevel = LEVEL_ORDER[level] === undefined ? 2 : LEVEL_ORDER[level];
        var mounts = document.querySelectorAll("[data-managed-load-profile]");
        Array.prototype.forEach.call(mounts, function (node) {
            node.innerHTML = renderProfile(
                schema, node.getAttribute("data-managed-load-profile"), maxLevel
            );
        });
    }

    function mount() {
        var container = document.getElementById("schema-reference");
        var profiles = document.querySelectorAll("[data-managed-load-profile]");
        if (!container && !profiles.length) { return; }

        fetch("../assets/data/config_schema.json")
            .then(function (res) {
                if (!res.ok) { throw new Error("HTTP " + res.status); }
                return res.json();
            })
            .then(function (schema) {
                var drawn = false;
                var draw = function () {
                    var level = document.body.getAttribute("data-active-level") || "standard";
                    renderProfiles(schema, level);
                    if (container) {
                        // Cleared first: idAttr() asks the document whether an anchor
                        // is already taken, and the previous render's own headings
                        // would answer yes to their own ids.
                        container.innerHTML = "";
                        container.innerHTML = render(schema, level);
                    }
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
                var warning =
                    "<div class=\"alert alert-warning\"><p>The parameter reference could not be " +
                    "loaded (" + esc(err.message) + "). It is generated from " +
                    "<code>assets/data/config_schema.json</code>; when reading these pages from " +
                    "disk rather than over HTTP, your browser will block that request.</p></div>";
                if (container) { container.innerHTML = warning; }
                Array.prototype.forEach.call(profiles, function (node) {
                    node.innerHTML = warning;
                });
            });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mount);
    } else {
        mount();
    }
}());
