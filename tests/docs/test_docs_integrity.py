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

    src/web/js/config.js turns help_url into a link to the published docs,
    prefixing ``user-guide/`` and nothing else, so the value names a page beside
    configuration.html. When an anchor goes missing the button silently lands at
    the top of whichever page it named.
    """
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    generated = _generated_anchors()

    ids_for = {}
    missing_pages = {}
    missing = {}
    for field in schema["fields"]:
        url = field.get("help_url") or ""
        if "#" not in url:
            continue
        page, anchor = url.split("#", 1)
        if page not in ids_for:
            path = DOCS / "user-guide" / page
            ids_for[page] = _ids(path.read_text(encoding="utf-8")) if path.exists() else None
        if ids_for[page] is None:
            missing_pages.setdefault(page, []).append(field["key"])
            continue
        if anchor not in ids_for[page] and anchor not in generated:
            missing.setdefault(url, []).append(field["key"])

    assert not missing_pages, "help_url names a page that does not exist:\n  " + "\n  ".join(
        f"user-guide/{p} ({len(k)} field(s), e.g. {k[0]})"
        for p, k in sorted(missing_pages.items())
    )
    assert not missing, "help_url anchors with no matching element:\n  " + "\n  ".join(
        f"{u} ({len(k)} field(s), e.g. {k[0]})" for u, k in sorted(missing.items())
    )


def test_every_field_has_a_help_url():
    """A field with no help_url renders no "Learn more" button at all.

    Six did, and they were the ones that most needed one: the alternative source
    a load or a battery can be read from, with its URL and token. The button is
    the only route from a setting to its explanation, so every setting gets one.
    """
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    bare = [f["key"] for f in schema["fields"] if "#" not in (f.get("help_url") or "")]
    assert not bare, f"fields with no help_url anchor: {bare}"


def test_help_urls_reach_prose_not_the_generated_reference():
    """Every anchor the application links to must be written into a page.

    ``_generated_anchors()`` counts any help_url anchor as present, because the
    A-Z reference on the overview renders a heading for each one. That check
    cannot tell an explained setting from an unexplained one, which is how 75% of
    the fields came to point at a bare table heading. This one can: the anchor has
    to exist as a hand-written id on the page the field names.
    """
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))

    written = {}
    missing = {}
    for field in schema["fields"]:
        url = field.get("help_url") or ""
        if "#" not in url:
            continue
        page, anchor = url.split("#", 1)
        if page not in written:
            path = DOCS / "user-guide" / page
            written[page] = _ids(path.read_text(encoding="utf-8")) if path.exists() else set()
        if anchor not in written[page]:
            missing.setdefault(url, []).append(field["key"])

    assert not missing, (
        "help_url anchors with no heading of their own:\n  " +
        "\n  ".join(f"{u} ({len(k)} field(s), e.g. {k[0]})"
                    for u, k in sorted(missing.items()))
    )


def test_every_managed_load_type_has_a_profile_section():
    """Each type the schema offers needs its own explanation and parameter table.

    The tables are mounted by ``data-managed-load-profile`` and filled from the
    schema, so a type added to presets.py without a section here would simply have
    no documentation - and nothing else would notice.
    """
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    types = set(schema["managed_load_presets"])
    html = (DOCS / "user-guide/config-managed-loads.html").read_text(encoding="utf-8")
    mounted = set(re.findall(r'data-managed-load-profile="([^"]+)"', html))

    assert mounted == types, (
        f"undocumented type(s): {sorted(types - mounted)}; "
        f"documented but unknown: {sorted(mounted - types)}"
    )


def test_schema_section_mounts_name_real_sections():
    """``data-schema-section`` renders nothing for a section that does not exist.

    It would fail quietly - an empty div where a parameter table should be - so
    the mounts are checked against the schema instead.
    """
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    known = {f["section"] for f in schema["fields"]}
    anchors_of = {}
    for field in schema["fields"]:
        url = field.get("help_url") or ""
        if "#" in url:
            anchors_of.setdefault(field["section"], set()).add(url.split("#", 1)[1])

    bad = []
    for page in _published_pages():
        html = page.read_text(encoding="utf-8")
        for mount in re.findall(r"<div ([^>]*data-schema-section=[^>]*)>", html):
            section = re.search(r'data-schema-section="([^"]+)"', mount).group(1)
            anchor = re.search(r'data-schema-anchor="([^"]+)"', mount)
            if section not in known:
                bad.append(f"{page.name}: unknown section {section!r}")
            elif anchor and anchor.group(1) not in anchors_of.get(section, set()):
                bad.append(
                    f"{page.name}: no field in {section!r} uses anchor "
                    f"{anchor.group(1)!r}"
                )
    assert not bad, "schema mounts that would render nothing:\n  " + "\n  ".join(bad)


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


# --------------------------------------------------------------- develop preview

# The site is published twice by .github/workflows/pages.yml: the released copy at
# the root, the develop copy under /develop/. Everything that distinguishes the two
# is added at publish time or derived from the URL at runtime — nothing marking a
# preview may be committed to docs/, or it would travel to main on the next merge
# and mark the released site instead.


@pytest.mark.parametrize("page", _published_pages(), ids=lambda p: p.name)
def test_no_page_hand_writes_the_preview_banner(page):
    """The banner comes from site.js, which raises it from the URL path."""
    html = page.read_text(encoding="utf-8")
    assert "preview-banner" not in html, (
        f"{page.relative_to(REPO)} contains preview-banner markup; the banner is "
        "injected by docs/assets/js/site.js and must never be written into a page"
    )


@pytest.mark.parametrize("page", _published_pages(), ids=lambda p: p.name)
def test_no_page_hand_writes_a_robots_meta(page):
    """noindex is injected into the develop copy by the publish workflow.

    Committed to a page it would reach main and de-index the real documentation.
    """
    html = page.read_text(encoding="utf-8")
    assert not re.search(r'<meta[^>]+name=["\']robots["\']', html, re.I), (
        f"{page.relative_to(REPO)} carries a robots meta; noindex belongs in "
        ".github/workflows/pages.yml, which applies it to the develop copy only"
    )


def test_banner_segment_matches_the_published_path():
    """site.js keys the banner off a URL segment; the workflow creates that path.

    If the two ever disagree the preview would publish with no banner at all — the
    exact failure this whole mechanism exists to prevent.
    """
    site_js = (DOCS / "assets/js/site.js").read_text(encoding="utf-8")
    segment = re.search(r'var PREVIEW_SEGMENT = "([^"]+)"', site_js).group(1)

    workflow = (REPO / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    published = re.search(r"^\s*PREVIEW_DIR:\s*(\S+)\s*$", workflow, re.M).group(1)

    assert segment == published, (
        f"site.js raises the banner on '/{segment}/' but pages.yml publishes the "
        f"preview to '/{published}/'"
    )


def test_banner_offset_is_a_no_op_off_the_preview_path():
    """--banner-h must default to zero, or the released site shifts too."""
    css = (DOCS / "assets/css/style.css").read_text(encoding="utf-8")
    root_block = re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1)
    default = re.search(r"--banner-h:\s*([^;]+);", root_block).group(1).strip()
    assert default in ("0", "0rem", "0px"), (
        f"--banner-h defaults to {default!r}; it must be zero so the released site "
        "renders exactly as before"
    )


# ------------------------------------------------------------------ navigation

SITE_JS = DOCS / "assets/js/site.js"


def _nav_children():
    """The href of every second-level nav entry in site.js, across all sections."""
    js = SITE_JS.read_text(encoding="utf-8")
    blocks = re.findall(r"children:\s*\[(.*?)\]", js, re.S)
    assert blocks, "site.js has no nav children array"
    return [h for block in blocks for h in re.findall(r'href:\s*"([^"]+)"', block)]


def test_every_nav_child_is_a_real_page():
    """A mistyped href in NAV is a 404 the moment someone clicks it.

    The nav is built in JavaScript from string literals, so nothing else would
    catch it - not the link check either, which only reads hrefs out of HTML.
    """
    missing = [h for h in _nav_children() if not (DOCS / h).exists()]
    assert not missing, f"nav points at pages that do not exist: {missing}"


def test_every_sub_page_is_in_the_nav():
    """A page nobody links to is a page nobody reads.

    The second-level row is how a reader moves between the pages of a section, so
    one that is not listed there is reachable only by knowing its URL.
    """
    listed = {h.split("/")[-1] for h in _nav_children() if h.startswith("user-guide/")}
    on_disk = {p.name for p in (DOCS / "user-guide").glob("*.html")}
    assert on_disk == listed, (
        f"not in the nav: {sorted(on_disk - listed)}; "
        f"in the nav but not on disk: {sorted(listed - on_disk)}"
    )


def test_readme_deep_links_resolve():
    """README links into the published docs by absolute URL.

    Nothing else checks them: they are absolute, so the internal-link check skips
    them, and they only break for a reader, never for a test. Moving a section
    between pages is exactly when they break.
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    prefix = "https://ohAnd.github.io/EOS_connect/"

    # Deliberately not falling back on _generated_anchors(): an anchor that the
    # A-Z reference happens to mint on some other page is not an explanation, and
    # a README link that lands on one is exactly the failure this catches.
    broken = []
    for url in re.findall(r"https://ohAnd\.github\.io/EOS_connect/[^)\]\s]+", readme):
        rel = url[len(prefix):]
        path, _, anchor = rel.partition("#")
        dest = DOCS / path
        if not dest.exists():
            broken.append(f"{url} (no such page)")
            continue
        if anchor and anchor not in _ids(dest.read_text(encoding="utf-8")):
            broken.append(f"{url} (no such anchor)")

    assert not broken, "README links into the docs that go nowhere:\n  " + "\n  ".join(broken)


def test_pages_reruns_after_the_image_builds():
    """The preview banner names the build it documents, and only a rerun makes
    that true.

    ``src/version.py`` is rewritten after the push that started the build, by the
    ``[AUTO]`` commit the image workflows push with ``GITHUB_TOKEN`` - and GitHub
    starts no workflow run from an event that token created, so the path filter on
    that file never fires. A ``workflow_run`` trigger is what actually reruns the
    publish once the version is current.

    ``workflow_run`` matches on the *name* of the other workflow, so a rename
    there silently stops the rerun and the banner quietly goes a build stale.
    """
    workflows = REPO / ".github/workflows"
    pages = workflows / "pages.yml"
    listed = re.search(
        r"workflow_run:\s*\n\s*workflows:\s*\[([^\]]*)\]", pages.read_text(encoding="utf-8")
    )
    assert listed, "pages.yml has no workflow_run trigger; the banner would stay stale"
    named = {n.strip().strip('"\'') for n in listed.group(1).split(",") if n.strip()}

    building = {
        re.search(r"^name:\s*(.+)$", (workflows / f).read_text(encoding="utf-8"), re.M)
        .group(1)
        .strip()
        for f in ("docker_main.yml", "docker_develop.yml")
    }
    assert named == building, (
        f"pages.yml waits for {sorted(named)}, the image workflows are called "
        f"{sorted(building)}"
    )


def test_the_version_bump_cannot_trigger_the_publish():
    """The comments above rest on the bump being pushed with GITHUB_TOKEN.

    Swap in a personal access token there and the [AUTO] commit starts triggering
    workflows - which would make docker_develop.yml, triggered by every push to
    develop with no path filter, run itself in a loop.
    """
    for name in ("docker_main.yml", "docker_develop.yml"):
        text = (REPO / ".github/workflows" / name).read_text(encoding="utf-8")
        step = re.search(r"Commit version file and push changes(.*?)\n\n", text, re.S)
        assert step, f"{name} no longer has the version commit step"
        assert "secrets.GITHUB_TOKEN" in step.group(1), (
            f"{name} pushes the version bump with something other than GITHUB_TOKEN; "
            "that makes [AUTO] commits trigger workflows, and docker_develop.yml "
            "has no path filter to stop it looping"
        )
