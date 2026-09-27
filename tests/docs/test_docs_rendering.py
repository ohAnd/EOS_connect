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

PAGES = [
    "index.html",
    "what-is/index.html",
    "user-guide/index.html",
    "user-guide/configuration.html",
    "advanced/index.html",
    "developer/index.html",
]

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
        page.get_by_role("button", name=label, exact=True).click()
        page.wait_for_timeout(250)
        rows = page.locator("#schema-reference tbody tr").count()
        assert rows == expected[level], (
            f"{label} shows {rows} parameters, schema says {expected[level]}"
        )


def test_level_persists_across_pages(page, docs_url):
    """A reader should not have to re-pick their depth on every page."""
    _open(page, docs_url, "user-guide/configuration.html")
    page.get_by_role("button", name="Expert", exact=True).click()
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
    anchors = sorted({
        f["help_url"].split("#", 1)[1]
        for f in schema["fields"] if "#" in (f.get("help_url") or "")
    })

    _open(page, docs_url, "user-guide/configuration.html", level="expert")
    page.wait_for_selector("#schema-reference h2")
    rendered = set(page.evaluate("() => [...document.querySelectorAll('[id]')].map(e => e.id)"))

    missing = [a for a in anchors if a not in rendered]
    assert not missing, f"help_url anchors that do not exist: {missing}"


def test_deep_link_into_hidden_content_raises_the_level(page, docs_url):
    """A link into a deeper level must reveal its target, not a blank page.

    src/interfaces/timeseries_normalizer.py points error messages at
    #timeseries-templates, which lives in a Standard-level block.
    """
    _open(page, docs_url, "user-guide/configuration.html", level="getting_started")
    page.goto(
        docs_url + "user-guide/configuration.html?level=getting_started#timeseries-templates",
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
    """At rest the banner is the topmost chrome and the header sits clear below it.

    The header is *not* sticky on this site despite its ``position: sticky``:
    ``#site-nav`` wraps it at exactly its own height, so it has no travel. That is
    pre-existing and deliberately left alone, which is why the banner holds its own
    position (``position: fixed``) instead of relying on the header.
    """
    _open(page, preview_docs_url, "user-guide/configuration.html")

    banner_box = page.locator(".preview-banner").bounding_box()
    header_box = page.locator(".nav-header").bounding_box()

    assert banner_box["y"] == pytest.approx(0, abs=1)
    assert header_box["y"] >= banner_box["y"] + banner_box["height"] - 1, (
        "the banner covers the top of the navigation header"
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
        + "user-guide/configuration.html?level=expert#timeseries-templates",
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
    page.locator(".level-option[data-level-value='expert']").click()
    assert page.locator("body[data-active-level='expert']").count() == 1
    assert page.locator(".toc-link").count() > 1
