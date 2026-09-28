/*
 * EOS Connect documentation — shared page chrome.
 *
 * Every page used to carry its own copy of the nav, the footer, the version
 * badge and three <script> blocks. This module owns all of it, so a nav change
 * is one edit instead of six and the scripts cannot drift apart.
 *
 * Each page declares its identity on <body>:
 *
 *   <body data-page="user-guide" data-root="../" data-levels>
 *
 *   data-page    key from NAV below; marks the current item
 *   data-root    relative path back to docs/ ("" at the root, "../" one down)
 *   data-levels  present if this page offers the disclosure-level switcher
 *
 * and provides mount points: #site-nav, #site-toc, #site-footer.
 */
(function () {
    "use strict";

    /* Single source of truth. The [AUTO] version bump rewrites this one line
     * instead of a badge in each of six pages. */
    var VERSION = "0.3.39";

    /* A nav entry may carry `children`, which renders as a second row under the
     * header while the reader is inside that entry. The pages in it declare the
     * PARENT's key in data-page and their own in data-subpage, so the first row
     * still marks exactly one item and only the second row moves. */
    var NAV = [
        { key: "home", href: "index.html", label: "Home" },
        { key: "what-is", href: "what-is/index.html", label: "What Is" },
        {
            key: "user-guide",
            href: "user-guide/index.html",
            label: "User Guide",
            children: [
                { key: "overview", href: "user-guide/index.html", label: "Overview" },
                { key: "install", href: "user-guide/install.html", label: "Install" },
                { key: "first-run", href: "user-guide/first-run.html", label: "First run" },
                { key: "daily-use", href: "user-guide/daily-use.html", label: "Living with it" },
                { key: "troubleshooting", href: "user-guide/troubleshooting.html", label: "Troubleshooting" }
            ]
        },
        {
            key: "configuration",
            href: "user-guide/configuration.html",
            label: "Configuration",
            children: [
                { key: "overview", href: "user-guide/configuration.html", label: "Overview" },
                { key: "data", href: "user-guide/config-data.html", label: "Data & sensors" },
                { key: "battery", href: "user-guide/config-battery.html", label: "Battery & inverter" },
                { key: "price", href: "user-guide/config-price.html", label: "Prices" },
                { key: "solar", href: "user-guide/config-solar.html", label: "Solar forecast" },
                { key: "optimizer", href: "user-guide/config-optimizer.html", label: "Optimizer" },
                { key: "managed-loads", href: "user-guide/config-managed-loads.html", label: "Managed loads" },
                { key: "system", href: "user-guide/config-system.html", label: "System & MQTT" }
            ]
        },
        { key: "advanced", href: "advanced/index.html", label: "Advanced" },
        { key: "developer", href: "developer/index.html", label: "Developer" }
    ];

    /* Mirrors LEVEL_ORDER in src/web/js/config.js:21 and
     * ConfigSchema.get_by_level() in src/config_web/schema.py:121-128.
     * Cumulative: expert shows standard and getting-started content too. */
    var LEVELS = [
        { val: "getting_started", label: "Getting Started" },
        { val: "standard", label: "Standard" },
        { val: "expert", label: "Expert" }
    ];
    var LEVEL_KEY = "config_level";
    var DEFAULT_LEVEL = "standard";

    /* The develop copy of this site is published under /EOS_connect/develop/, the
     * released copy at the root. Deriving that from the URL rather than from a build
     * flag is what keeps this file byte-identical on both branches: a docs PR from
     * develop to main carries no banner state anyone has to remember to strip.
     * .github/workflows/pages.yml owns the other half — it is what puts the develop
     * copy under that path, and what writes build-info.json beside it. */
    var PREVIEW_SEGMENT = "develop";
    var STABLE_DOCS_URL = "https://ohand.github.io/EOS_connect/";

    var body = document.body;

    /* data-root is one of exactly two values across the whole site. Comparing it
     * and assigning a literal keeps DOM text from reaching the innerHTML calls
     * below — the flow CodeQL reports as js/html-constructed-from-input — and
     * makes a mistyped attribute fall back to a working root instead of
     * producing a broken path. */
    var root = body.getAttribute("data-root") === "../" ? "../" : "";

    /* Only ever compared against NAV keys, never written into markup. */
    var page = body.getAttribute("data-page") || "";
    var subpage = body.getAttribute("data-subpage") || "";

    function escapeHtml(text) {
        return String(text === null || text === undefined ? "" : text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function el(tag, attrs, html) {
        var node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (k) {
                if (attrs[k] !== null && attrs[k] !== undefined) {
                    node.setAttribute(k, attrs[k]);
                }
            });
        }
        if (html !== undefined) { node.innerHTML = html; }
        return node;
    }

    /* -------------------------------------------------------- preview banner */

    function isPreviewLocation() {
        return window.location.pathname.split("/").indexOf(PREVIEW_SEGMENT) !== -1;
    }

    function renderPreviewBanner() {
        if (!isPreviewLocation()) { return; }
        var mount = document.getElementById("site-nav");
        if (!mount) { return; }

        /* Drives --banner-h, which offsets the sticky header, the sticky TOC and the
         * anchor scroll margin. Without the class every one of those is a no-op. */
        body.classList.add("docs-preview");

        /* Inserted after renderNav(), which assigns innerHTML and would otherwise
         * wipe it. The skip-link therefore stays ahead of the banner in DOM order,
         * so tabbing still reaches the content first. */
        mount.insertAdjacentHTML("afterbegin",
            "<div class=\"preview-banner\" role=\"status\">" +
            "<span class=\"preview-banner-text\">" +
            "<i class=\"fas fa-flask\" aria-hidden=\"true\"></i> " +
            "<strong>Development preview</strong> &mdash; documents the unreleased " +
            "develop build<span class=\"preview-build\"></span></span>" +
            "<a class=\"preview-banner-link\" href=\"" + STABLE_DOCS_URL + "\">" +
            "Released documentation</a>" +
            "</div>");

        var banner = mount.querySelector(".preview-banner");
        syncBannerHeight(banner);
        window.addEventListener("resize", function () { syncBannerHeight(banner); });

        fillBuildStamp(mount.querySelector(".preview-build"), banner);
    }

    /* The banner wraps to two lines on a phone, and the sticky header is offset by
     * --banner-h. A fixed token would therefore let the header ride up over the
     * second line, so measure what the banner actually occupies. The CSS default is
     * only what applies for the frame before this runs. */
    function syncBannerHeight(banner) {
        if (!banner) { return; }
        body.style.setProperty("--banner-h", banner.offsetHeight + "px");
    }

    /* build-info.json is written by the publish workflow, so it is absent whenever
     * the docs are opened from a checkout or the test server. Every failure path has
     * to leave the banner standing and say nothing. */
    function fillBuildStamp(slot, banner) {
        if (!slot || !window.fetch) { return; }
        fetch(root + "build-info.json", { cache: "no-store" })
            .then(function (res) { return res.ok ? res.json() : null; })
            .then(function (info) {
                if (!info || !info.version) { return; }
                var parts = [info.version];
                if (info.commit) { parts.push(info.commit); }
                if (info.built) { parts.push(String(info.built).slice(0, 10)); }
                /* textContent, not innerHTML: this is fetched data, the sink CodeQL
                 * reports as js/html-constructed-from-input. */
                slot.textContent = " \u00b7 " + parts.join(" \u00b7 ");
                syncBannerHeight(banner);
            })
            .catch(function () { /* no stamp; the banner still says what matters */ });
    }

    /* ------------------------------------------------------------ navigation */

    function renderNav() {
        var mount = document.getElementById("site-nav");
        if (!mount) { return; }

        var current = null;
        var items = NAV.map(function (item) {
            var active = item.key === page;
            if (active) { current = item; }
            return "<li><a href=\"" + root + item.href + "\"" +
                (active ? " class=\"active\" aria-current=\"page\"" : "") +
                ">" + item.label + "</a></li>";
        }).join("");

        /* The second row exists only inside a section that has one, and carries
         * its own classes: site.js takes the FIRST .nav-menu for the hamburger,
         * and a second one would hand that button the wrong element. It is not
         * sticky either - the header never actually sticks (#site-nav wraps it at
         * its own height), so this row scrolls away with it and costs nothing
         * from --scroll-offset. */
        var subnav = "";
        if (current && current.children) {
            var subItems = current.children.map(function (child) {
                var active = child.key === subpage;
                return "<li><a href=\"" + root + child.href + "\"" +
                    (active ? " class=\"active\" aria-current=\"page\"" : "") +
                    ">" + child.label + "</a></li>";
            }).join("");
            subnav = "<nav class=\"subnav\" aria-label=\"" + escapeHtml(current.label) +
                " sections\"><div class=\"subnav-container\">" +
                "<ul class=\"subnav-menu\">" + subItems + "</ul></div></nav>";
        }

        var external =
            "<li><a href=\"https://github.com/ohAnd/EOS_connect\" target=\"_blank\" rel=\"noopener\">" +
            "<i class=\"fab fa-github\" aria-hidden=\"true\"></i> GitHub</a></li>" +
            "<li><a href=\"https://github.com/sponsors/ohAnd\" target=\"_blank\" rel=\"noopener\" " +
            "data-sponsor title=\"Sponsor the project\" aria-label=\"Sponsor the project\">" +
            "<i class=\"fas fa-mug-hot\" aria-hidden=\"true\"></i></a></li>";

        mount.innerHTML =
            "<a class=\"skip-link\" href=\"#main\">Skip to content</a>" +
            "<nav class=\"nav-header\" aria-label=\"Main\">" +
            "<div class=\"nav-container\">" +
            "<a href=\"" + root + "index.html\" class=\"nav-logo\">" +
            "<img src=\"" + root + "assets/images/icon.png\" alt=\"\" width=\"32\" height=\"32\">" +
            "<span>EOS Connect</span>" +
            "<span class=\"version-badge\">v" + VERSION + "</span></a>" +
            // A literal glyph, not a Font Awesome icon: this is the only control
            // that makes the site unnavigable on a phone if the icon CDN fails.
            "<button class=\"mobile-menu-toggle\" type=\"button\" aria-expanded=\"false\" " +
            "aria-controls=\"nav-menu\" aria-label=\"Toggle navigation\">☰</button>" +
            "<ul class=\"nav-menu\" id=\"nav-menu\">" + items + external + "</ul>" +
            "</div>" + subnav + "</nav>";

        var toggle = mount.querySelector(".mobile-menu-toggle");
        var menu = mount.querySelector(".nav-menu");

        toggle.addEventListener("click", function (e) {
            e.stopPropagation();
            var open = menu.classList.toggle("mobile-active");
            toggle.setAttribute("aria-expanded", open ? "true" : "false");
        });

        document.addEventListener("click", function (e) {
            if (menu.classList.contains("mobile-active") && !menu.contains(e.target)) {
                menu.classList.remove("mobile-active");
                toggle.setAttribute("aria-expanded", "false");
            }
        });

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && menu.classList.contains("mobile-active")) {
                menu.classList.remove("mobile-active");
                toggle.setAttribute("aria-expanded", "false");
                toggle.focus();
            }
        });
    }

    function renderFooter() {
        var mount = document.getElementById("site-footer");
        if (!mount) { return; }
        mount.innerHTML =
            "<footer class=\"footer\"><p>EOS Connect v" + VERSION + " &middot; " +
            "<a href=\"" + root + "index.html\">Documentation home</a> &middot; " +
            "<a href=\"https://github.com/ohAnd/EOS_connect\" target=\"_blank\" rel=\"noopener\">GitHub</a> &middot; " +
            "<a href=\"https://github.com/ohAnd/EOS_connect/discussions\" target=\"_blank\" rel=\"noopener\">Discussions</a> &middot; " +
            "<a href=\"https://github.com/sponsors/ohAnd\" target=\"_blank\" rel=\"noopener\">Sponsor</a></p></footer>";
    }

    /* ------------------------------------------------------ disclosure level */

    function readLevel() {
        // The app links here with ?level=… because it cannot share localStorage
        // across origins (src/web/js/config.js). An explicit level in the URL
        // therefore wins, and is remembered for later visits.
        var fromUrl = new URLSearchParams(window.location.search).get("level");
        if (fromUrl && LEVELS.some(function (l) { return l.val === fromUrl; })) {
            storeLevel(fromUrl);
            return fromUrl;
        }
        try {
            var saved = window.localStorage.getItem(LEVEL_KEY);
            if (saved && LEVELS.some(function (l) { return l.val === saved; })) {
                return saved;
            }
        } catch (err) {
            /* private mode or blocked storage — fall through to the default */
        }
        return DEFAULT_LEVEL;
    }

    function storeLevel(level) {
        try {
            window.localStorage.setItem(LEVEL_KEY, level);
        } catch (err) {
            /* nothing to do; the level still applies for this page view */
        }
    }

    function applyLevel(level) {
        body.setAttribute("data-active-level", level);
        var buttons = document.querySelectorAll(".level-option");
        Array.prototype.forEach.call(buttons, function (b) {
            b.setAttribute("aria-pressed", b.getAttribute("data-level-value") === level ? "true" : "false");
        });
    }

    function renderLevelSwitcher() {
        if (!body.hasAttribute("data-levels")) {
            // Pages without a switcher still need a level set, or everything
            // tagged data-level would stay hidden.
            body.setAttribute("data-active-level", "expert");
            return;
        }
        var mount = document.getElementById("site-level");
        if (!mount) { return; }

        var opts = LEVELS.map(function (l) {
            return "<button type=\"button\" class=\"level-option\" data-level-value=\"" +
                l.val + "\" aria-pressed=\"false\">" + l.label + "</button>";
        }).join("");

        mount.innerHTML =
            "<div class=\"level-switcher\">" +
            "<span class=\"level-switcher-label\" id=\"level-label\">" +
            "<i class=\"fas fa-layer-group\" aria-hidden=\"true\"></i> " +
            "How much detail do you want?</span>" +
            "<div class=\"level-options\" role=\"group\" aria-labelledby=\"level-label\">" +
            opts + "</div></div>";

        mount.addEventListener("click", function (e) {
            var btn = e.target.closest(".level-option");
            if (!btn) { return; }
            var level = btn.getAttribute("data-level-value");
            storeLevel(level);
            applyLevel(level);
            buildTOC();
        });
    }

    /* ------------------------------------------------ table of contents */

    function buildTOC() {
        var mount = document.getElementById("site-toc");
        var main = document.getElementById("main");
        if (!mount || !main) { return; }

        // Only headings that are actually visible at the current level — a link
        // to a hidden section is a dead end.
        var headings = Array.prototype.filter.call(
            main.querySelectorAll("h2[id], h3[id]"),
            function (h) { return h.offsetParent !== null || h.getClientRects().length > 0; }
        );

        if (headings.length < 2) {
            mount.innerHTML = "";
            return;
        }

        var links = headings.map(function (h) {
            var sub = h.tagName === "H3" ? " toc-sub" : "";
            // Heading text and ids are page-authored, so they reach innerHTML as
            // DOM text; a heading like "Backup &amp; Restore" would otherwise
            // render wrong here, and the pattern is the same sink CodeQL flags.
            return "<a class=\"toc-link" + sub + "\" href=\"#" + escapeHtml(h.id) + "\">" +
                escapeHtml((h.textContent || "").trim()) + "</a>";
        }).join("");

        // A disclosure on phones so the page opens on content; CSS forces it
        // open and strips the summary once there is room for a sidebar.
        mount.innerHTML =
            "<details class=\"toc-details\"" + (window.innerWidth >= 1100 ? " open" : "") + ">" +
            "<summary><i class=\"fas fa-list\" aria-hidden=\"true\"></i> On this page</summary>" +
            "<nav class=\"toc-nav\" aria-label=\"On this page\">" + links + "</nav>" +
            "</details>";

        spy();
    }

    var tocLinks = [];
    var spyTargets = [];

    function collectSpy() {
        var main = document.getElementById("main");
        if (!main) { return; }
        tocLinks = Array.prototype.slice.call(document.querySelectorAll(".toc-link"));
        spyTargets = tocLinks.map(function (a) {
            return document.getElementById(decodeURIComponent(a.getAttribute("href").slice(1)));
        });
    }

    function spy() {
        collectSpy();
        highlight();
    }

    function highlight() {
        if (!tocLinks.length) { return; }
        var offset = 96;
        var current = -1;
        for (var i = 0; i < spyTargets.length; i++) {
            var t = spyTargets[i];
            if (t && t.getBoundingClientRect().top <= offset) { current = i; }
        }
        tocLinks.forEach(function (a, i) {
            a.classList.toggle("active", i === current);
        });
    }

    /* ------------------------------------------------ deep links into depth */

    /* A link can point at content that is hidden at the reader's current level
     * — the application's error messages link straight to
     * #timeseries-templates, for instance, which lives in a Standard block.
     * Landing on a blank page would be worse than useless, so raise the level
     * just far enough to reveal what was linked to. */
    function revealHashTarget() {
        var hash = window.location.hash.slice(1);
        if (!hash) { return; }
        var target = document.getElementById(decodeURIComponent(hash));
        if (!target) { return; }

        var needed = null;
        for (var node = target; node && node !== document.body; node = node.parentElement) {
            var lvl = node.getAttribute && node.getAttribute("data-level");
            if (lvl && (needed === null || LEVEL_RANK[lvl] > LEVEL_RANK[needed])) {
                needed = lvl;
            }
        }
        if (!needed) { return; }

        var current = body.getAttribute("data-active-level") || DEFAULT_LEVEL;
        if (LEVEL_RANK[needed] > LEVEL_RANK[current]) {
            applyLevel(needed);
            buildTOC();
            // The anchor moved as content above it appeared, so jump again.
            window.requestAnimationFrame(function () {
                var el2 = document.getElementById(decodeURIComponent(hash));
                if (el2) { el2.scrollIntoView(); }
            });
        }
    }

    var LEVEL_RANK = { getting_started: 0, standard: 1, expert: 2 };

    /* --------------------------------------------------------- back to top */

    function renderBackToTop() {
        var btn = el("button", {
            "class": "back-to-top",
            "type": "button",
            "aria-label": "Back to top"
        }, "<i class=\"fas fa-chevron-up\" aria-hidden=\"true\"></i>");
        document.body.appendChild(btn);

        btn.addEventListener("click", function () {
            window.scrollTo({ top: 0, behavior: "smooth" });
        });

        return function () {
            btn.classList.toggle("visible", window.scrollY > 300);
        };
    }

    /* ------------------------------------------------------------- startup */

    function init() {
        renderNav();
        renderPreviewBanner();
        renderFooter();
        renderLevelSwitcher();
        applyLevel(readLevel());
        buildTOC();
        revealHashTarget();
        window.addEventListener("hashchange", revealHashTarget);

        var toggleTop = renderBackToTop();
        var ticking = false;
        window.addEventListener("scroll", function () {
            if (ticking) { return; }
            ticking = true;
            window.requestAnimationFrame(function () {
                highlight();
                toggleTop();
                ticking = false;
            });
        }, { passive: true });

        var resizeTimer;
        window.addEventListener("resize", function () {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(buildTOC, 200);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    // Let the schema-driven reference rebuild the TOC once its sections exist.
    window.EOSDocs = {
        buildTOC: buildTOC,
        applyLevel: applyLevel,
        revealHashTarget: revealHashTarget
    };
}());
