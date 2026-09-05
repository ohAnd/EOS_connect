"""Integrity checks for the published GitHub Pages documentation.

These guard the failure modes the docs actually hit, all of which were live at
some point and none of which any test caught:

* links between pages pointing at anchors that do not exist;
* ``help_url`` values in the config schema pointing at anchors the docs never
  rendered, which turned 57 of 127 in-app "Learn more" buttons into dead ends;
* the version badge drifting away from ``src/version.py``;
* editing artefacts (draft banners, placeholder comments) reaching production.

The parameter reference is rendered in the browser from ``config_schema.json``,
so anchor checks that involve it need a real page load. Those live in
``test_docs_rendering.py``; everything here is static and needs no browser.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
PAGES = sorted(DOCS.glob("*.html")) + sorted(DOCS.glob("*/*.html"))

# Anchors that only exist after config-reference.js has run. Static checks treat
# them as present; test_docs_rendering.py proves they really are.
SCHEMA_JSON = DOCS / "assets/data/config_schema.json"


def _published_pages():
    """Pages actually served — the template is a reference, not a page."""
    return [p for p in PAGES if p.name != "PAGE_TEMPLATE.html"]


def _ids(html):
    return set(re.findall(r'id="([^"]+)"', html))


def _generated_anchors():
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    out = {"ref-" + f["section"] for f in schema["fields"]}
    out |= {
        f["help_url"].split("#", 1)[1]
        for f in schema["fields"]
        if "#" in (f.get("help_url") or "")
    }
    return out


@pytest.mark.parametrize("page", _published_pages(), ids=lambda p: p.name)
def test_no_duplicate_ids(page):
    """A repeated id breaks both anchor links and the generated contents."""
    found = re.findall(r'id="([^"]+)"', page.read_text(encoding="utf-8"))
    dupes = sorted({i for i in found if found.count(i) > 1})
    assert not dupes, f"{page.relative_to(REPO)} repeats id(s): {dupes}"


def test_internal_links_resolve():
    """Every relative link between docs pages must land somewhere real."""
    ids = {p: _ids(p.read_text(encoding="utf-8")) for p in _published_pages()}
    generated = _generated_anchors()
    broken = []

    for page in _published_pages():
        for href in re.findall(r'href="([^"]+)"', page.read_text(encoding="utf-8")):
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            target, _, anchor = href.partition("#")
            if not target:
                dest = page
            else:
                dest = (page.parent / target).resolve()
                if not dest.exists():
                    broken.append(f"{page.relative_to(REPO)} -> {href} (no such file)")
                    continue
                if dest not in ids:
                    continue  # an asset, not a page
            if anchor and anchor not in ids[dest] and anchor not in generated:
                broken.append(f"{page.relative_to(REPO)} -> {href} (no such anchor)")

    assert not broken, "broken internal links:\n  " + "\n  ".join(broken)


def test_schema_help_urls_resolve():
    """Each field's in-app "Learn more" button must reach a real anchor.

    src/web/js/config.js turns help_url into a link to the published docs. When
    an anchor goes missing the button silently lands at the top of the page.
    """
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    config_page = DOCS / "user-guide/configuration.html"
    available = _ids(config_page.read_text(encoding="utf-8")) | _generated_anchors()

    missing = {}
    for field in schema["fields"]:
        url = field.get("help_url") or ""
        if "#" not in url:
            continue
        page, anchor = url.split("#", 1)
        assert page == "configuration.html", (
            f"{field['key']} points help_url at {page!r}; only configuration.html "
            "is handled by this check"
        )
        if anchor not in available:
            missing.setdefault(anchor, []).append(field["key"])

    assert not missing, "help_url anchors with no matching element:\n  " + "\n  ".join(
        f"#{a} ({len(k)} field(s), e.g. {k[0]})" for a, k in sorted(missing.items())
    )


def test_version_badge_matches_release_prefix():
    """site.js holds the one version string the whole site renders.

    It is not compared against ``src/version.py``: that file is rewritten by the
    build (docker_develop.yml) and on a development branch still carries the
    previous release. The badge tracks ``VERSION_PREFIX`` instead, which is what
    the automated bump moves in step with the docs.
    """
    workflow = (REPO / ".github/workflows/docker_develop.yml").read_text(encoding="utf-8")
    prefix = re.search(r"VERSION_PREFIX:\s*([0-9.]+?)\.?\s*$", workflow, re.M).group(1)

    site_js = (DOCS / "assets/js/site.js").read_text(encoding="utf-8")
    shown = re.search(r'var VERSION = "([^"]+)"', site_js).group(1)

    assert shown == prefix, (
        f"docs advertise v{shown} but docker_develop.yml builds {prefix}.*; "
        "update VERSION in docs/assets/js/site.js"
    )


def test_current_version_is_not_hand_written():
    """The badge used to be copied into six pages and drift between them.

    Only the *current* version is forbidden; a page may still refer to an older
    release as history ("compose files written before v0.3.34").
    """
    site_js = (DOCS / "assets/js/site.js").read_text(encoding="utf-8")
    current = re.search(r'var VERSION = "([^"]+)"', site_js).group(1)

    offenders = [
        p.relative_to(REPO)
        for p in _published_pages()
        if current in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"v{current} is hand-written into {offenders}; site.js renders it instead"
    )


@pytest.mark.parametrize("page", _published_pages(), ids=lambda p: p.name)
def test_no_editing_artifacts(page):
    """Draft banners and placeholder notes must not reach the published site."""
    html = page.read_text(encoding="utf-8")
    for artifact in ("draft-banner", "DRAFT DOCUMENTATION", "placeholder for now", "TODO:"):
        assert artifact not in html, (
            f"{page.relative_to(REPO)} still contains {artifact!r}"
        )


@pytest.mark.parametrize("page", _published_pages(), ids=lambda p: p.name)
def test_no_inline_styles(page):
    """Inline styles beat every media query, which is what broke mobile before."""
    html = page.read_text(encoding="utf-8")
    assert 'style="' not in html, (
        f"{page.relative_to(REPO)} uses inline style attributes; put the rule in "
        "assets/css/style.css instead"
    )
    assert "<style" not in html, f"{page.relative_to(REPO)} has a <style> block"


@pytest.mark.parametrize("page", _published_pages(), ids=lambda p: p.name)
def test_shared_chrome_is_not_duplicated(page):
    """Nav, footer and behaviour live in site.js, not copied into each page."""
    html = page.read_text(encoding="utf-8")
    assert '<div id="site-nav">' in html, f"{page.relative_to(REPO)} has no nav mount"
    assert '<div id="site-footer">' in html, f"{page.relative_to(REPO)} has no footer mount"

    scripts = re.findall(r"<script[^>]*src=\"([^\"]+)\"", html)
    inline = len(re.findall(r"<script(?![^>]*\bsrc=)", html))
    assert inline == 0, f"{page.relative_to(REPO)} has {inline} inline <script> block(s)"
    assert any(s.endswith("site.js") for s in scripts), (
        f"{page.relative_to(REPO)} does not load site.js"
    )


@pytest.mark.parametrize("page", _published_pages(), ids=lambda p: p.name)
def test_page_declares_its_identity(page):
    """site.js needs data-page and data-root to render nav and asset paths."""
    html = page.read_text(encoding="utf-8")
    body = re.search(r"<body([^>]*)>", html).group(1)
    assert "data-page=" in body, f"{page.relative_to(REPO)} has no data-page"
    assert "data-root=" in body, f"{page.relative_to(REPO)} has no data-root"

    expected = "" if page.parent == DOCS else "../"
    root = re.search(r'data-root="([^"]*)"', body).group(1)
    assert root == expected, (
        f"{page.relative_to(REPO)} declares data-root={root!r}, expected {expected!r}"
    )


def test_flow_diagram_copies_agree():
    """The inline and standalone flow diagrams must say the same thing.

    docs/what-is/index.html carries the diagram inline so it can inherit the
    page's colours through ``currentColor``; the standalone SVG exists because
    README.md embeds it as an image and cannot inherit anything. Two copies
    drift, so compare the labels.
    """
    inline = (DOCS / "what-is/index.html").read_text(encoding="utf-8")
    standalone = (DOCS / "assets/images/eos_connect_flow.svg").read_text(encoding="utf-8")

    svg = re.search(r'<svg class="diagram".*?</svg>', inline, re.S)
    assert svg, "the inline flow diagram is missing from what-is/index.html"

    def labels(markup):
        return {
            re.sub(r"\s+", " ", t).strip()
            for t in re.findall(r"<text[^>]*>(.*?)</text>", markup, re.S)
        }

    only_inline = labels(svg.group(0)) - labels(standalone)
    only_standalone = labels(standalone) - labels(svg.group(0))
    assert not (only_inline or only_standalone), (
        "the two flow diagrams have diverged.\n"
        f"  only inline:     {sorted(only_inline)}\n"
        f"  only standalone: {sorted(only_standalone)}"
    )


def test_referenced_images_exist():
    """A renamed or removed image should fail here, not on the live site."""
    missing = []
    for page in _published_pages():
        for src in re.findall(r'<img[^>]*src="([^"]+)"', page.read_text(encoding="utf-8")):
            if src.startswith(("http://", "https://", "data:")):
                continue
            if not (page.parent / src).resolve().exists():
                missing.append(f"{page.relative_to(REPO)} -> {src}")
    assert not missing, "missing images:\n  " + "\n  ".join(missing)
