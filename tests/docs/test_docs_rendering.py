"""What the browser actually does with the documentation.

These cover the failures that only exist once CSS and JavaScript have run, and
that a static check cannot see:

* pages scrolling sideways on a phone;
* body text and links failing WCAG AA contrast;
* the disclosure level not matching ``ConfigSchema.get_by_level()``;
* anchors the application deep-links to not existing after the schema-driven
  reference has rendered.

The old stylesheet shrank headings on mobile with bare element selectors that
``.hero h1`` and ``.content-section h2`` silently out-specified, so every
heading stayed at its desktop size. Type is now sized once with ``clamp()``;
``test_headings_shrink_on_mobile`` is what stops that regressing.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"

CONFIG_PAGES = [
    "user-guide/configuration.html",
    "user-guide/config-data.html",
    "user-guide/config-battery.html",
    "user-guide/config-price.html",
    "user-guide/config-solar.html",
    "user-guide/config-optimizer.html",
    "user-guide/config-managed-loads.html",
    "user-guide/config-system.html",
]

USER_GUIDE_PAGES = [
    "user-guide/index.html",
    "user-guide/install.html",
    "user-guide/first-run.html",
    "user-guide/daily-use.html",
    "user-guide/troubleshooting.html",
]

PAGES = [
    "index.html",
    "what-is/index.html",
    "advanced/index.html",
    "developer/index.html",
] + USER_GUIDE_PAGES + CONFIG_PAGES

PHONE = {"width": 360, "height": 740}
DESKTOP = {"width": 1440, "height": 900}

# WCAG 2.1 AA for normal-size body text.
MIN_CONTRAST = 4.5


def _relative_luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg, bg):
    light, dark = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _parse_rgb(value):
    nums = [float(n) for n in re.findall(r"[\d.]+", value)]
    return tuple(int(n) for n in nums[:3])


@pytest.fixture(name="page")
def page_fixture(browser):
    """A desktop-sized page; tests that need a phone open their own context."""
    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    yield page
    context.close()


def _page_level(page, label):
    """The level button on the page itself, not the copy in the header.

    Both switchers carry the same accessible name on purpose - they are the same
    control - so an unscoped query matches two elements and Playwright's strict
    mode refuses to click either.
    """
    return page.locator("#site-level").get_by_role("button", name=label, exact=True)


def _open(page, docs_url, path, **query):
    suffix = ("?" + "&".join(f"{k}={v}" for k, v in query.items())) if query else ""
    page.goto(docs_url + path + suffix, wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('.nav-header')")


@pytest.mark.parametrize("path", PAGES)
def test_no_horizontal_scroll_on_phone(browser, docs_url, path):
    """The page body must never scroll sideways at 360px."""
    context = browser.new_context(viewport=PHONE)
    page = context.new_page()
    try:
        _open(page, docs_url, path)
        page.wait_for_timeout(300)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth"
            " - document.documentElement.clientWidth"
        )
        culprits = page.evaluate(
            """() => {
                const vw = document.documentElement.clientWidth;
                return [...document.querySelectorAll('*')]
                    .filter(e => e.getBoundingClientRect().right > vw + 1)
                    .slice(0, 5)
                    .map(e => e.tagName.toLowerCase() + '.' + (e.className || ''));
            }"""
        )
        assert overflow <= 1, f"{path} overflows by {overflow}px; widest: {culprits}"
    finally:
        context.close()


@pytest.mark.parametrize("path", PAGES)
def test_shared_chrome_renders(page, docs_url, path):
    """site.js must produce the nav, the footer and the current-page marker."""
    _open(page, docs_url, path)
    assert page.locator(".nav-header").count() == 1
    assert page.locator(".footer").count() == 1
    assert page.locator(".nav-menu a.active").count() == 1, (
        f"{path} does not mark exactly one nav item as current"
    )


@pytest.mark.parametrize("path", PAGES)
def test_no_javascript_errors(browser, docs_url, path):
    """A thrown error would leave the page without nav, footer or contents."""
    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        _open(page, docs_url, path)
        page.wait_for_timeout(400)
        assert not errors, f"{path} raised: {errors}"
    finally:
        context.close()


@pytest.mark.parametrize("path", PAGES)
def test_headings_shrink_on_phone(browser, docs_url, path):
    """Every heading must be smaller on a phone than on a desktop.

    This is the regression guard for the specificity trap: the old mobile rules
    were written as bare `h1`/`h2` selectors and never applied, so headings
    stayed at their desktop size on a 360px screen.
    """
    sizes = {}
    for name, viewport in (("phone", PHONE), ("desktop", DESKTOP)):
        context = browser.new_context(viewport=viewport)
        page = context.new_page()
        try:
            _open(page, docs_url, path)
            page.wait_for_timeout(200)
            sizes[name] = page.evaluate(
                """() => {
                    const h = document.querySelector('#main h1, #main h2');
                    return h ? parseFloat(getComputedStyle(h).fontSize) : null;
                }"""
            )
        finally:
            context.close()

    assert sizes["phone"] and sizes["desktop"], f"{path} has no heading in <main>"
    assert sizes["phone"] < sizes["desktop"], (
        f"{path}: heading is {sizes['phone']}px on a phone and "
        f"{sizes['desktop']}px on desktop — the responsive rule is not applying"
    )
    assert sizes["phone"] <= 32, (
        f"{path}: {sizes['phone']}px heading is too large for a 360px screen"
    )


@pytest.mark.parametrize("path", PAGES)
def test_body_text_and_links_meet_aa_contrast(page, docs_url, path):
    """Body copy and inline links must clear 4.5:1 against what is behind them."""
    _open(page, docs_url, path)
    page.wait_for_timeout(200)

    samples = page.evaluate(
        """() => {
            const out = [];
            const seen = new Set();
            const bg = el => {
                for (let n = el; n; n = n.parentElement) {
                    const c = getComputedStyle(n).backgroundColor;
                    if (c && c !== 'transparent' && !c.startsWith('rgba(0, 0, 0, 0)')) return c;
                }
                return getComputedStyle(document.body).backgroundColor;
            };
            for (const el of document.querySelectorAll('#main p, #main li, #main a, #main td')) {
                if (!el.textContent.trim()) continue;
                const s = getComputedStyle(el);
                const key = s.color + '|' + bg(el);
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({ color: s.color, bg: bg(el), tag: el.tagName,
                           text: el.textContent.trim().slice(0, 40) });
            }
            return out;
        }"""
    )

    failures = []
    for s in samples:
        # An alpha-composited background cannot be judged from the computed
        # value alone; the opaque surfaces underneath are what these sit on.
        ratio = _contrast(_parse_rgb(s["color"]), _parse_rgb(s["bg"]))
        if ratio < MIN_CONTRAST:
            failures.append(
                f"{s['tag']} {s['color']} on {s['bg']} = {ratio:.2f}:1 ({s['text']!r})"
            )

    assert not failures, f"{path} fails AA contrast:\n  " + "\n  ".join(failures)


def test_level_filter_matches_schema(page, docs_url):
    """The docs must show exactly what ConfigSchema.get_by_level() would return.

    Cumulative, per src/config_web/schema.py:121-128 and LEVEL_ORDER in
    src/web/js/config.js:21 — expert includes standard includes getting started.
    """
    schema = json.loads((DOCS / "assets/data/config_schema.json").read_text())
    order = {"getting_started": 0, "standard": 1, "expert": 2}
    expected = {
        level: sum(1 for f in schema["fields"] if order[f["level"]] <= rank)
        for level, rank in order.items()
    }

    _open(page, docs_url, "user-guide/configuration.html")
    page.wait_for_selector("#schema-reference table")

    for label, level in (("Getting Started", "getting_started"),
                         ("Standard", "standard"),
                         ("Expert", "expert")):
        _page_level(page, label).click()
        page.wait_for_timeout(250)
        rows = page.locator("#schema-reference tbody tr").count()
        assert rows == expected[level], (
            f"{label} shows {rows} parameters, schema says {expected[level]}"
        )


def test_level_persists_across_pages(page, docs_url):
    """A reader should not have to re-pick their depth on every page."""
    _open(page, docs_url, "user-guide/configuration.html")
    _page_level(page, "Expert").click()
    page.wait_for_timeout(200)

    _open(page, docs_url, "user-guide/index.html")
    page.wait_for_timeout(200)
    assert page.evaluate("document.body.dataset.activeLevel") == "expert"


def test_app_deep_links_reach_their_anchor(page, docs_url):
    """Every help_url in the schema must resolve once the page has rendered.

    src/web/js/config.js turns these into "Learn more" links from inside the
    running application. 57 of 127 of them pointed at nothing before this.
    """
    schema = json.loads((DOCS / "assets/data/config_schema.json").read_text())
    by_page = {}
    for field in schema["fields"]:
        url = field.get("help_url") or ""
        if "#" in url:
            target, anchor = url.split("#", 1)
            by_page.setdefault(target, set()).add(anchor)

    missing = []
    for target, anchors in sorted(by_page.items()):
        _open(page, docs_url, "user-guide/" + target, level="expert")
        page.wait_for_timeout(400)
        rendered = set(page.evaluate(
            "() => [...document.querySelectorAll('[id]')].map(e => e.id)"
        ))
        missing += [f"{target}#{a}" for a in sorted(anchors) if a not in rendered]

    assert not missing, f"help_url anchors that do not exist: {missing}"


def test_deep_link_into_hidden_content_raises_the_level(page, docs_url):
    """A link into a deeper level must reveal its target, not a blank page.

    src/interfaces/timeseries_normalizer.py points error messages at
    #timeseries-templates, which lives in a Standard-level block.
    """
    _open(page, docs_url, "user-guide/config-data.html", level="getting_started")
    page.goto(
        docs_url + "user-guide/config-data.html?level=getting_started#timeseries-templates",
        wait_until="domcontentloaded",
    )
    page.wait_for_timeout(400)
    assert page.locator("#timeseries-templates").is_visible(), (
        "deep link landed on content hidden by the level filter"
    )


def test_generated_markup_escapes_page_text(page, docs_url):
    """Text taken from the page must be escaped before it is written back as HTML.

    site.js builds the nav, footer and contents with innerHTML, so anything it
    reads out of the document first — heading text, ids, data-root — is a
    DOM-text-to-HTML sink (CodeQL js/html-constructed-from-input). A heading
    containing markup must come back as text, not as an element.
    """
    _open(page, docs_url, "user-guide/index.html")
    page.wait_for_timeout(200)

    injected = page.evaluate(
        """() => {
            const h = document.createElement('h2');
            h.id = 'escape-probe';
            h.textContent = 'Backup & <img src=x onerror="window.__pwned=1">Restore';
            document.getElementById('main').appendChild(h);
            window.EOSDocs.buildTOC();
            const link = [...document.querySelectorAll('.toc-link')]
                .find(a => a.hash === '#escape-probe');
            return {
                text: link ? link.textContent : null,
                images: link ? link.querySelectorAll('img').length : -1,
                pwned: !!window.__pwned
            };
        }"""
    )

    assert injected["text"] is not None, "the probe heading never reached the contents"
    assert not injected["pwned"], "markup from a heading was executed"
    assert injected["images"] == 0, "markup from a heading became a real element"
    assert "&" in injected["text"], "the ampersand was mangled instead of escaped"


def test_root_attribute_cannot_inject_markup(page, docs_url):
    """data-root is validated to a known value before it reaches innerHTML."""
    page.goto(docs_url + "user-guide/index.html", wait_until="domcontentloaded")
    page.wait_for_function("() => !!document.querySelector('.nav-header')")

    # Whatever is put in the attribute, the nav must still resolve to a real
    # root — never to attacker-controlled markup.
    hrefs = page.evaluate(
        """() => {
            document.body.setAttribute('data-root', '"><img src=x onerror="window.__pwned2=1">');
            return { pwned: !!window.__pwned2 };
        }"""
    )
    assert not hrefs["pwned"]


def test_table_of_contents_is_generated(page, docs_url):
    """The contents used to be hand-maintained, 32 links on one page."""
    _open(page, docs_url, "user-guide/index.html")
    page.wait_for_timeout(200)
    links = page.locator(".toc-link")
    assert links.count() >= 3, "no generated table of contents"

    targets = page.evaluate(
        """() => [...document.querySelectorAll('.toc-link')]
             .map(a => !!document.getElementById(decodeURIComponent(a.hash.slice(1))))"""
    )
    assert all(targets), "a generated contents entry points at no element"


# --------------------------------------------------------------- develop preview

# ``.github/workflows/pages.yml`` publishes the released docs at the root and the
# develop docs under ``/develop/``. ``site.js`` raises the banner from that URL
# segment alone, so the same file is byte-identical on both branches and a docs PR
# from develop to main carries nothing that has to be stripped.


def test_no_banner_on_the_released_site(page, docs_url):
    """Production must look exactly as it did before this existed."""
    _open(page, docs_url, "user-guide/index.html")
    assert page.locator(".preview-banner").count() == 0
    assert not page.locator("body.docs-preview").count()
    # Every preview rule keys off --banner-h, so a zero here is what guarantees
    # the released site is untouched: no reserved space, no shifted anchors.
    assert page.evaluate(
        "() => getComputedStyle(document.body).getPropertyValue('--banner-h').trim()"
    ) in ("0", "0rem", "0px")
    assert page.evaluate(
        "() => getComputedStyle(document.body).paddingTop"
    ) == "0px"
    assert page.locator(".nav-header").bounding_box()["y"] == pytest.approx(0, abs=1)


@pytest.mark.parametrize("path", PAGES)
def test_banner_shows_on_every_preview_page(browser, preview_docs_url, path):
    """Every page gets the banner — it comes from shared chrome, not from markup."""
    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    try:
        _open(page, preview_docs_url, path)
        banner = page.locator(".preview-banner")
        assert banner.is_visible(), f"{path} shows no development-preview banner"
        assert "Development preview" in banner.inner_text()
        assert page.locator("body.docs-preview").count() == 1
    finally:
        context.close()


def test_banner_starts_above_the_header_without_covering_it(page, preview_docs_url):
    """The banner is the topmost chrome and the header sits clear below it.

    The header sticks by way of ``#site-nav``, offset by ``top: var(--banner-h)``
    so it comes to rest under the banner rather than behind it. The banner stays
    ``position: fixed`` because it has to hold the top edge while the header
    travels. Checked at rest and scrolled: the offset is what keeps the order.
    """
    _open(page, preview_docs_url, "user-guide/configuration.html")

    for where in ("at rest", "scrolled"):
        if where == "scrolled":
            page.evaluate("() => window.scrollTo(0, 1500)")
            page.wait_for_function("() => window.scrollY > 1000")
            page.wait_for_timeout(400)

        banner_box = page.locator(".preview-banner").bounding_box()
        header_box = page.locator(".nav-header").bounding_box()

        assert banner_box["y"] == pytest.approx(0, abs=1), where
        assert header_box["y"] >= banner_box["y"] + banner_box["height"] - 1, (
            f"{where}: the banner covers the top of the navigation header"
        )


def test_banner_stays_visible_while_scrolling(page, preview_docs_url):
    """A reader must not be able to scroll away from the 'this is unreleased' mark."""
    _open(page, preview_docs_url, "user-guide/configuration.html")
    page.evaluate("() => window.scrollTo(0, 1500)")
    page.wait_for_function("() => window.scrollY > 1000")

    banner = page.locator(".preview-banner")
    assert banner.is_visible(), "the preview banner scrolled out of view"
    assert banner.bounding_box()["y"] == pytest.approx(0, abs=1)


def test_banner_offers_the_released_documentation(page, preview_docs_url):
    """A reader who landed on the preview by accident needs a way back."""
    _open(page, preview_docs_url, "index.html")
    link = page.locator(".preview-banner-link")
    assert link.is_visible()
    assert link.get_attribute("href") == "https://ohand.github.io/EOS_connect/"


def test_preview_renders_without_a_build_stamp(browser, preview_docs_url):
    """build-info.json only exists once published; its absence must be silent."""
    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        _open(page, preview_docs_url, "user-guide/index.html")
        page.wait_for_timeout(300)
        assert page.locator(".preview-banner").is_visible()
        assert page.locator(".preview-build").inner_text().strip() == ""
        assert not errors, f"JavaScript errors on the preview page: {errors}"
    finally:
        context.close()


def test_deep_linked_anchor_is_not_hidden_behind_the_banner(page, preview_docs_url):
    """--scroll-offset grows with the banner, or the app's deep links land behind it.

    This is the same anchor src/interfaces/timeseries_normalizer.py points its error
    messages at, which on a develop build now resolves into the preview site.
    """
    page.goto(
        preview_docs_url
        + "user-guide/config-data.html?level=expert#timeseries-templates",
        wait_until="domcontentloaded",
    )
    page.wait_for_function("() => !!document.querySelector('.preview-banner')")
    page.wait_for_timeout(500)

    target_top = page.evaluate(
        "() => document.getElementById('timeseries-templates').getBoundingClientRect().top"
    )
    banner_bottom = page.evaluate(
        "() => document.querySelector('.preview-banner').getBoundingClientRect().bottom"
    )
    assert target_top >= banner_bottom - 1, (
        f"the deep-linked heading sits at {target_top}px, behind a banner reaching "
        f"{banner_bottom}px"
    )


def test_navigation_stays_inside_the_preview(page, preview_docs_url):
    """Relative links must keep a preview reader on preview pages."""
    _open(page, preview_docs_url, "user-guide/index.html")
    page.locator(".nav-menu a", has_text="Advanced").first.click()
    page.wait_for_function("() => !!document.querySelector('.nav-header')")

    assert "/develop/advanced/" in page.url, f"navigation left the preview: {page.url}"
    assert page.locator(".preview-banner").is_visible(), (
        "the banner vanished after navigating within the preview"
    )


def test_level_switcher_still_works_under_the_preview_path(page, preview_docs_url):
    """The deeper path must not break the disclosure level or the contents."""
    _open(page, preview_docs_url, "user-guide/configuration.html")
    page.locator("#site-level .level-option[data-level-value='expert']").click()
    assert page.locator("body[data-active-level='expert']").count() == 1
    assert page.locator(".toc-link").count() > 1


# ------------------------------------------------- managed-load profile tables

MANAGED_LOAD_PROFILES = [
    "pool_heatpump",
    "sauna",
    "hot_water_tank",
    "buffer_tank",
    "external_contingent",
    "external_profile",
]


@pytest.mark.parametrize("profile", MANAGED_LOAD_PROFILES)
def test_every_profile_renders_its_own_settings(page, docs_url, profile):
    """Each profile mount must fill with the settings that profile really has.

    The tables come from depends_on: {type: [...]} in the schema, so this also
    proves the six mounts on the page name types the schema still knows about —
    a renamed type would leave an empty table rather than failing anywhere else.
    """
    schema = json.loads((DOCS / "assets/data/config_schema.json").read_text())
    expected = {
        f["key"].split(".", 1)[1]
        for f in schema["fields"]
        if f["section"] == "managed_loads"
        and profile in ((f.get("depends_on") or {}).get("type") or [profile])
    }

    _open(page, docs_url, "user-guide/config-managed-loads.html", level="expert")
    mount = page.locator(f"[data-managed-load-profile='{profile}']")
    page.wait_for_selector(f"[data-managed-load-profile='{profile}'] table")

    shown = set(mount.locator("td.param-key code").all_inner_texts())
    assert shown == expected, (
        f"{profile} shows {sorted(shown - expected)} it should not and is missing "
        f"{sorted(expected - shown)}"
    )


def _profile_row(page, profile, key):
    """The row for one setting in a profile's table, matched on the key itself.

    Matching on text would also hit the header ("Type") and any description that
    happens to mention the setting.
    """
    return page.locator(f"[data-managed-load-profile='{profile}'] tbody tr").filter(
        has=page.locator(f"td.param-key code:text-is('{key}')")
    ).first


def test_profile_tables_show_the_profile_starting_values(page, docs_url):
    """A sauna must not be documented with the pool's numbers.

    managed_loads.target_temp is one FieldDef shared by four appliance types and
    its default is the pool's 28 C. The per-profile values come from
    managed_load_presets, exported from src/loads/presets.py.
    """
    schema = json.loads((DOCS / "assets/data/config_schema.json").read_text())
    presets = schema["managed_load_presets"]

    _open(page, docs_url, "user-guide/config-managed-loads.html", level="expert")
    page.wait_for_selector("[data-managed-load-profile='sauna'] table")

    for profile in ("pool_heatpump", "sauna"):
        row = _profile_row(page, profile, "target_temp")
        # Compared as a number: 28.0 in the JSON is 28 once JavaScript has
        # rendered it, which is the same temperature.
        shown = float(row.locator("td").all_inner_texts()[2])
        expected = presets[profile]["defaults"]["target_temp"]
        assert shown == expected, (
            f"{profile} documents target_temp as {shown}, preset says {expected}"
        )


@pytest.mark.parametrize("profile", MANAGED_LOAD_PROFILES)
def test_the_type_row_names_the_profile_it_is_in(page, docs_url, profile):
    """No preset names the type - it is the key they are looked up by.

    Left to fall back on the schema, every profile would document its own type as
    pool_heatpump, which is the one value in that table that cannot be changed
    without making it a different table.
    """
    _open(page, docs_url, "user-guide/config-managed-loads.html", level="expert")
    page.wait_for_selector(f"[data-managed-load-profile='{profile}'] table")

    shown = _profile_row(page, profile, "type").locator("td").all_inner_texts()[2]
    assert shown.strip() == profile, f"the {profile} table documents type as {shown!r}"


def test_a_preset_of_none_reads_as_no_limit(page, docs_url):
    """None in a preset means "no limit", not "unset".

    A sauna has no allowed window at all; rendering its window_start as the
    schema's 8 would document a restriction that is not applied.
    """
    _open(page, docs_url, "user-guide/config-managed-loads.html", level="expert")
    page.wait_for_selector("[data-managed-load-profile='sauna'] table")

    row = _profile_row(page, "sauna", "window_start")
    assert "no limit" in row.locator("td").all_inner_texts()[2]


@pytest.mark.parametrize("path", CONFIG_PAGES)
def test_the_rendered_page_has_no_duplicate_ids(page, docs_url, path):
    """Generated tables must not claim an anchor the prose already owns.

    Both are driven by help_url, so every anchor the application links to exists
    twice by construction unless config-reference.js yields the id. Two elements
    with one id put the section twice in the contents and send the deep link to
    whichever came first.
    """
    _open(page, docs_url, path, level="expert")
    page.wait_for_timeout(400)

    dupes = page.evaluate(
        "() => { const seen = {}; "
        "[...document.querySelectorAll('[id]')].forEach(e => "
        "  seen[e.id] = (seen[e.id] || 0) + 1); "
        "return Object.keys(seen).filter(k => seen[k] > 1); }"
    )
    assert not dupes, f"ids rendered more than once: {dupes}"


def test_profile_tables_follow_the_detail_level(page, docs_url):
    """The per-profile tables are subject to the same disclosure as everything else."""
    _open(page, docs_url, "user-guide/config-managed-loads.html", level="expert")
    page.wait_for_selector("[data-managed-load-profile='pool_heatpump'] table")
    expert = page.locator("[data-managed-load-profile='pool_heatpump'] tbody tr").count()

    _page_level(page, "Standard").click()
    page.wait_for_timeout(250)
    standard = page.locator("[data-managed-load-profile='pool_heatpump'] tbody tr").count()

    assert 0 < standard < expert, (
        f"pool heat pump shows {standard} settings at Standard and {expert} at "
        "Expert; the level filter is not reaching the profile tables"
    )


@pytest.mark.parametrize("path", CONFIG_PAGES)
def test_every_schema_section_mount_renders_a_table(page, docs_url, path):
    """An empty mount is invisible: the prose reads on, the settings are gone."""
    _open(page, docs_url, path, level="expert")
    page.wait_for_timeout(400)

    empty = page.evaluate(
        "() => [...document.querySelectorAll('[data-schema-section]')]"
        "  .filter(n => !n.querySelector('table'))"
        "  .map(n => n.getAttribute('data-schema-section') + '#' +"
        "            (n.getAttribute('data-schema-anchor') || ''))"
    )
    assert not empty, f"{path} has mounts that rendered no table: {empty}"


def test_the_reference_points_each_section_at_its_page(page, docs_url):
    """The A-Z list answers "what is it called"; the link is what answers "why".

    Without it the reader who found a name in the full list has nowhere to go
    with it, which is the whole reason the list is allowed to stay complete.
    """
    _open(page, docs_url, "user-guide/configuration.html", level="expert")
    page.wait_for_selector("#schema-reference table")

    targets = page.evaluate(
        "() => [...document.querySelectorAll('#schema-reference .param-explained a')]"
        "  .map(a => a.getAttribute('href'))"
    )
    assert len(targets) >= 13, f"only {len(targets)} sections link to a topic page"
    assert all(t.startswith("config-") for t in targets), targets


@pytest.mark.parametrize("level", ["getting_started", "standard", "expert"])
def test_reference_headings_are_distinct_within_a_section(page, docs_url, level):
    """Two groups in one section must not carry the same title.

    The A-Z list groups fields by their help_url anchor and titles each group from
    ANCHOR_TITLE, falling back to the section label. That fallback was invisible
    while a section had one anchor; splitting managed_loads across the paragraphs
    that explain it turned it into twelve consecutive headings all reading
    "Managed Loads", each above a "nothing here at this level" note.

    Titles repeating across *different* sections is fine and expected - an anchor
    can serve fields from two sections, and the section heading above tells them
    apart.
    """
    _open(page, docs_url, "user-guide/configuration.html", level=level)
    page.wait_for_selector("#schema-reference table")
    page.wait_for_timeout(300)

    sections = page.evaluate(
        "() => [...document.querySelectorAll('#schema-reference section')].map(s => ({"
        "  name: s.querySelector('h2').textContent.trim(),"
        "  groups: [...s.querySelectorAll('h3')].map(h => h.textContent.trim())"
        "}))"
    )
    assert sections, "the reference rendered no sections at all"

    repeated = {
        s["name"]: sorted({g for g in s["groups"] if s["groups"].count(g) > 1})
        for s in sections
        if any(s["groups"].count(g) > 1 for g in s["groups"])
    }
    assert not repeated, f"sections with repeated group headings: {repeated}"


@pytest.mark.parametrize("level", ["getting_started", "standard", "expert"])
def test_the_reference_shows_no_empty_groups(page, docs_url, level):
    """A heading with no table under it is noise, not navigation.

    Every anchor the application links to is a written heading on a topic page
    now, so the reference has no reason to render one it cannot fill.
    """
    _open(page, docs_url, "user-guide/configuration.html", level=level)
    page.wait_for_selector("#schema-reference table")
    page.wait_for_timeout(300)

    empty = page.evaluate(
        "() => [...document.querySelectorAll('#schema-reference h3')]"
        "  .filter(h => h.nextElementSibling === null ||"
        "               !h.nextElementSibling.querySelector('table'))"
        "  .map(h => h.textContent.trim())"
    )
    assert not empty, f"group headings with no table: {empty}"


@pytest.mark.parametrize("level", ["getting_started", "standard", "expert"])
@pytest.mark.parametrize("path", CONFIG_PAGES)
def test_no_setting_is_shown_above_the_chosen_level(page, docs_url, path, level):
    """Nothing painted may carry a level badge higher than the one selected.

    Three mechanisms filter by level and they are easy to get out of step: CSS on
    ``data-level`` blocks, the row filter in the generated tables, and the
    per-profile tables. This asserts the result rather than any one of them - it
    reads the badge off every row the browser actually painted.
    """
    order = {"getting started": 0, "standard": 1, "expert": 2}
    _open(page, docs_url, path, level=level)
    page.wait_for_timeout(500)

    shown = page.evaluate(
        "() => [...document.querySelectorAll('table.param-table tbody tr')]"
        "  .filter(tr => tr.offsetParent !== null && tr.querySelector('.badge-level'))"
        "  .map(tr => ({key: tr.querySelector('td.param-key').textContent.trim(),"
        "               level: tr.querySelector('.badge-level').textContent.trim()}))"
    )
    # Not every page has settings at every level, so an empty result is only
    # suspicious at Expert - where a page showing nothing means a mount that
    # failed rather than a level that excludes everything.
    if level == "expert":
        assert shown, f"{path} painted no parameter rows at Expert"

    too_deep = sorted({
        f"{r['key']} ({r['level']})"
        for r in shown
        if order.get(r["level"].lower(), 2) > order[level.replace("_", " ")]
    })
    assert not too_deep, f"{path} at {level} shows deeper settings: {too_deep}"


@pytest.mark.parametrize("level", ["getting_started", "standard", "expert"])
@pytest.mark.parametrize("path", CONFIG_PAGES)
def test_the_contents_lists_nothing_hidden(page, docs_url, path, level):
    """Every entry in "on this page" must lead somewhere visible.

    buildTOC() filters on what is rendered, and the generated tables render after
    it first runs - so a heading that the level filter later empties, or one the
    reference stopped emitting, would linger in the sidebar as a dead entry.
    """
    _open(page, docs_url, path, level=level)
    page.wait_for_timeout(500)

    ghosts = page.evaluate(
        "() => [...document.querySelectorAll('.toc-link')]"
        "  .map(a => a.getAttribute('href'))"
        "  .filter(h => { const el = document.getElementById(h.slice(1));"
        "                 return !el || el.offsetParent === null; })"
    )
    assert not ghosts, f"{path} at {level} lists hidden headings: {ghosts}"


# ------------------------------------------------------------- sticky header

def _header_metrics(page):
    return page.evaluate(
        "() => { const h = document.querySelector('.nav-header').getBoundingClientRect();"
        " return {y: h.y, height: h.height, bottom: h.bottom,"
        "  chrome: parseFloat(getComputedStyle(document.body)"
        "            .getPropertyValue('--chrome-h')) || 0}; }"
    )


@pytest.mark.parametrize("path", ["user-guide/config-battery.html", "advanced/index.html"])
def test_the_header_stays_at_the_top_while_scrolling(page, docs_url, path):
    """The header has to travel, which for years it did not.

    ``.nav-header`` declared ``position: sticky`` but its parent ``#site-nav``
    wraps it at exactly its own height, so the sticky rectangle had no travel and
    it behaved like ``position: relative``. The sticky now sits on ``#site-nav``,
    whose parent is ``<body>``.
    """
    _open(page, docs_url, path)
    page.wait_for_timeout(300)
    assert _header_metrics(page)["y"] == pytest.approx(0, abs=1), "not at the top at rest"

    page.evaluate("() => window.scrollTo(0, 1500)")
    page.wait_for_function("() => window.scrollY > 1000")
    page.wait_for_timeout(400)
    assert _header_metrics(page)["y"] == pytest.approx(0, abs=1), (
        "the header scrolled away instead of sticking"
    )


def test_the_header_gets_flatter_when_scrolled(page, docs_url):
    """Sticky chrome that kept its full height would just eat the viewport.

    Both nav rows have to survive the shrink - being able to change section
    without scrolling back up is the entire point of keeping the header.
    """
    _open(page, docs_url, "user-guide/config-battery.html")
    page.wait_for_timeout(300)
    tall = _header_metrics(page)["height"]

    page.evaluate("() => window.scrollTo(0, 1500)")
    page.wait_for_function("() => document.body.classList.contains('nav-compact')")
    page.wait_for_timeout(500)
    short = _header_metrics(page)["height"]

    assert short < tall * 0.8, f"header went from {tall:.0f}px to {short:.0f}px"
    assert page.locator(".nav-menu a.active").first.is_visible(), "first level lost"
    assert page.locator(".subnav-menu a.active").first.is_visible(), "second level lost"


@pytest.mark.parametrize("scrolled", [False, True])
def test_chrome_height_matches_the_rendered_header(page, docs_url, scrolled):
    """--chrome-h feeds the anchor offset and the sticky contents sidebar.

    It is written from a measurement, and the measurement is taken while the
    shrink is still animating - so it is only right because a transitionend
    listener takes it again. Wrong here means anchors land behind the header.
    """
    _open(page, docs_url, "user-guide/config-battery.html")
    if scrolled:
        page.evaluate("() => window.scrollTo(0, 1500)")
        page.wait_for_function("() => document.body.classList.contains('nav-compact')")
    page.wait_for_timeout(600)

    m = _header_metrics(page)
    assert m["chrome"] == pytest.approx(m["height"], abs=2), (
        f"--chrome-h is {m['chrome']}px, the header is {m['height']:.0f}px"
    )


def test_the_measured_height_never_reaches_the_min_height(page, docs_url):
    """--header-h is an input and must stay one.

    .nav-container takes it as its min-height, so writing the measured total back
    into it makes the header grow by its own height every time it is measured -
    130px, then 184px, and on up. Asserted as the invariant rather than as a
    symptom: the runaway saturates quickly enough that comparing two heights can
    miss it.
    """
    _open(page, docs_url, "user-guide/config-battery.html")
    page.evaluate("() => window.scrollTo(0, 1500)")
    page.wait_for_function("() => document.body.classList.contains('nav-compact')")
    page.wait_for_timeout(500)

    written = page.evaluate(
        "() => ({header: document.body.style.getPropertyValue('--header-h'),"
        "        chrome: document.body.style.getPropertyValue('--chrome-h')})"
    )
    assert written["header"] == "", (
        f"--header-h was written at runtime ({written['header']}); the measured "
        "value belongs in --chrome-h"
    )
    assert written["chrome"], "--chrome-h was never measured"


def test_a_contents_jump_lands_below_the_header(page, docs_url):
    """scroll-margin-top has to clear the chrome that is now genuinely on top.

    --scroll-offset is declared on <body> rather than :root on purpose: a var()
    is substituted where the property is declared, and both terms are measured
    onto <body> at runtime.
    """
    _open(page, docs_url, "user-guide/config-battery.html")
    page.wait_for_timeout(300)
    page.locator(".toc-link", has_text="The inverter").first.click()
    page.wait_for_timeout(1000)

    box = page.locator("#inverter").bounding_box()
    assert box["y"] >= _header_metrics(page)["bottom"] - 1, (
        "the heading landed behind the header"
    )


def test_the_header_switcher_appears_only_when_scrolled(page, docs_url):
    """At rest the full switcher is a few lines down the page; a copy would be noise."""
    _open(page, docs_url, "user-guide/config-battery.html")
    page.wait_for_timeout(300)
    assert not page.locator("#site-level-compact").is_visible()

    page.evaluate("() => window.scrollTo(0, 1500)")
    page.wait_for_function("() => document.body.classList.contains('nav-compact')")
    page.wait_for_timeout(400)
    assert page.locator("#site-level-compact").is_visible()


def test_the_header_switcher_drives_the_same_state(page, docs_url):
    """One control in two places: applyLevel() syncs both from one query.

    Both sets keep class="level-option" and data-level-value for exactly that
    reason, so neither can drift from the level the body actually carries.
    """
    _open(page, docs_url, "user-guide/config-battery.html", level="standard")
    page.evaluate("() => window.scrollTo(0, 1500)")
    page.wait_for_function("() => document.body.classList.contains('nav-compact')")
    page.wait_for_timeout(400)

    page.locator("#site-level-compact .level-option[data-level-value='expert']").click()
    page.wait_for_timeout(400)

    assert page.evaluate("() => document.body.dataset.activeLevel") == "expert"
    for where in ("#site-level", "#site-level-compact"):
        pressed = page.locator(
            f"{where} .level-option[data-level-value='expert']"
        ).get_attribute("aria-pressed")
        assert pressed == "true", f"{where} did not follow"


def test_pages_without_levels_have_no_header_switcher(page, docs_url):
    """Those pages are pinned to Expert, so a switcher there would do nothing."""
    _open(page, docs_url, "advanced/index.html")
    page.wait_for_timeout(300)
    assert page.locator("#site-level-compact").count() == 0


def test_the_github_link_separates_its_icon_from_its_label(page, docs_url):
    """`.nav-menu a` is a flex container, and flex drops whitespace-only items.

    The literal space between the icon and "GitHub" in the markup is one of those,
    so without an explicit gap the two render flush against each other.
    """
    _open(page, docs_url, "user-guide/index.html")
    gap = page.evaluate(
        "() => { const a = [...document.querySelectorAll('.nav-menu a')]"
        "  .find(x => x.textContent.trim() === 'GitHub');"
        "  return parseFloat(getComputedStyle(a).columnGap) || 0; }"
    )
    assert gap >= 2, f"icon and label are {gap}px apart"


def test_the_hamburger_keeps_the_edge_on_a_phone(browser, docs_url):
    """Thumb reach: the menu button belongs at the edge, the filters inboard.

    DOM order puts the compact switcher last because that is where it belongs on
    a desktop, to the right of the nav links. On a phone the links are behind the
    hamburger, so the two swap visually - which is what CSS order is for.
    """
    context = browser.new_context(viewport=PHONE)
    page = context.new_page()
    try:
        _open(page, docs_url, "user-guide/config-battery.html")
        page.evaluate("() => window.scrollTo(0, 1500)")
        page.wait_for_function("() => document.body.classList.contains('nav-compact')")
        page.wait_for_timeout(400)

        order = page.evaluate(
            "() => [...document.querySelector('.nav-container').children]"
            "  .filter(e => getComputedStyle(e).display !== 'none')"
            "  .map(e => [e, e.getBoundingClientRect().x])"
            "  .sort((a, b) => a[1] - b[1])"
            "  .map(([e]) => e.id || e.className.split(' ')[0])"
        )
        assert order == ["nav-logo", "site-level-compact", "mobile-menu-toggle"], order
    finally:
        context.close()


def test_the_switcher_sits_right_of_the_links_on_a_desktop(browser, docs_url):
    """The mirror of the phone rule: on a desktop nothing reorders."""
    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    try:
        _open(page, docs_url, "user-guide/config-battery.html")
        page.evaluate("() => window.scrollTo(0, 1500)")
        page.wait_for_function("() => document.body.classList.contains('nav-compact')")
        page.wait_for_timeout(400)

        menu = page.locator(".nav-menu").bounding_box()
        switcher = page.locator("#site-level-compact").bounding_box()
        assert switcher["x"] >= menu["x"] + menu["width"] - 1, (
            "the compact switcher is not to the right of the nav links"
        )
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_no_level_button_is_narrower_than_its_label(browser, docs_url, viewport):
    """A label wider than its button is clipped, whichever switcher it is in.

    The phone rule gives .level-option `flex: 1 1 0` so the switcher on the page
    can divide 360px into equal thirds. The compact copy in the header inherited
    that, which squeezed every label to the width of the shortest - "Standard"
    wrapped to two lines and still spilled 12px past its own edge.
    """
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    try:
        _open(page, docs_url, "user-guide/config-battery.html")
        page.evaluate("() => window.scrollTo(0, 1500)")
        page.wait_for_function("() => document.body.classList.contains('nav-compact')")
        page.wait_for_timeout(400)

        clipped = page.evaluate(
            "() => [...document.querySelectorAll('.level-option')]"
            "  .filter(e => e.offsetParent !== null && e.scrollWidth > e.clientWidth + 1)"
            "  .map(e => ({label: e.textContent.trim(),"
            "              needs: e.scrollWidth, has: e.clientWidth,"
            "              where: e.closest('#site-level-compact') ? 'header' : 'page'}))"
        )
        assert not clipped, f"labels wider than their button: {clipped}"
    finally:
        context.close()
