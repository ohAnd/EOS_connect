"""
How large the text in the dashboard's top tiles is.

Only a browser can answer this: the size is chosen by CSS from the tile's own width and
the row's height, and whether it is *right* is a question about what fits -- whether a
label wraps to a second line, whether a tile has to scroll its own content, and whether
the text is as large as the tile could actually carry.

The last of those is the point of this file. Sizing the tiles by viewport width instead
had the row 25% smaller than it needed to be on a 4:3 screen and a third smaller with
five tiles configured, and nothing noticed, because "too small" breaks no assertion.
``test_the_text_is_nearly_as_large_as_the_tile_allows`` measures the ceiling in the
browser and asserts the rendered size is near it.
"""

import pytest

import tests.web.wizard_driver as wz  # noqa: F401  (keeps the shared skip behaviour)

from tests.web.test_managed_loads_ui import TWO_LOADS


# Sizes people actually use, and the aspect ratios that pull in different directions:
# 16:9 is short, so the row's height binds; 4:3 and 16:10 are tall, so the tile's width
# does; a phone is one tile per row and neither.
VIEWPORTS = [
    ("1280x720", 1280, 720),
    ("1280x800", 1280, 800),
    ("1280x1024", 1280, 1024),
    ("1366x768", 1366, 768),
    ("1440x900", 1440, 900),
    ("1600x900", 1600, 900),
    ("1600x1200", 1600, 1200),
    ("1680x1050", 1680, 1050),
    ("1920x1080", 1920, 1080),
    ("2560x1440", 2560, 1440),
    ("3440x1440", 3440, 1440),
    ("phone-large", 430, 932),
    ("phone", 390, 844),
    ("phone-small", 360, 800),
    ("phone-se", 320, 568),
    # 768 is inside the phone media query, so a 4:3 tablet in portrait stacks like one.
    ("tablet-portrait-4-3", 768, 1024),
]

# Between the phone breakpoint and about 1300px, five tiles side by side get under 250px
# each, which no font size fits -- see test_the_row_cannot_be_shared_below_250px_a_tile.
TABLET_BAND = [
    ("1024x768", 1024, 768),
    ("tablet-portrait", 820, 1180),
]

# The markup ships placeholders ("initializing...", "... next charge time") that are
# wider than any value the dashboard ever shows, so measuring against them would size
# the row for text no user sees.
LIVE_VALUES = """
() => {
    const put = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
    put('control_overall', 'discharge allowed');
    put('control_ac_charge', '0 %');
    put('control_dc_charge', '100 %');
    put('control_discharge_allowed', 'true');
    put('next_charge_time', '02:00');
    put('next_charge_amount', '12.4 kWh');
    put('next_charge_sum_price', '3.21 \\u20ac*');
    put('next_charge_avg_price', '25.9 ct/kWh');
    put('current_max_charge_dyn', '4200 W');
    put('expense_summary', '4.56 \\u20ac*');
    put('income_summary', '1.23 \\u20ac*');
    put('feed_in_summary', '8.4 kWh');
    for (const el of document.querySelectorAll('.top-box td, .top-box th, .header_notification')) {
        if (el.textContent.trim().startsWith('...')) { el.textContent = '13:00'; }
    }
}
"""

# The upper end of the content clamp in style.css. A stacked tile on a tablet is 728px
# wide and could carry 39px before anything wrapped; the cap is a deliberate stop, so the
# fill assertion below measures against whichever of the two comes first.
MAX_CONTENT_PX = 28

# Wrapped cells and tiles that scroll. Line boxes are measured over a range across the
# whole cell, so nested markup counts: the italic hint in Battery State is an <i>, and a
# check that only reads direct text nodes cannot see it wrap.
FITS = """
() => {
    const wrapped = [], scrolling = [];
    for (const box of document.querySelectorAll('.top-box')) {
        if (box.offsetParent === null) { continue; }
        const content = box.querySelector('.content');
        if (content.scrollHeight > content.clientHeight + 1) {
            scrolling.push((box.id || 'tile') + ' by '
                + (content.scrollHeight - content.clientHeight) + 'px');
        }
        for (const cell of content.querySelectorAll('td, th')) {
            const r = document.createRange();
            r.selectNodeContents(cell);
            const rects = [...r.getClientRects()].filter(x => x.width > 0.5);
            if (new Set(rects.map(x => Math.round(x.top))).size > 1) {
                wrapped.push(cell.textContent.trim().replace(/\\s+/g, ' ').slice(0, 30));
            }
        }
    }
    return {wrapped, scrolling};
}
"""

# Overrides the content font so the ceiling can be searched for. The header keeps its own
# CSS size, which is what makes the search honest: a taller header leaves the table less
# room, and that is part of what the answer depends on.
OVERRIDE = """
(px) => {
    let el = document.getElementById('sizing-probe');
    if (!el) {
        el = document.createElement('style');
        el.id = 'sizing-probe';
        document.head.appendChild(el);
    }
    el.textContent = px === null ? ''
        : `.top-box > .content { font-size: ${px}px !important; }`;
}
"""


def _show(page, width, height, loads=TWO_LOADS):
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("(l) => controlsManager.updateManagedLoads(l)", loads)
    page.evaluate(LIVE_VALUES)
    page.wait_for_timeout(90)


def _content_font(page):
    return page.evaluate(
        """() => {
            const box = [...document.querySelectorAll('.top-box')].find(b => b.offsetParent);
            return parseFloat(getComputedStyle(box.querySelector('.content')).fontSize);
        }"""
    )


def _ceiling(page):
    """The largest content font at which no tile wraps a line or scrolls."""
    low, high, best = 4.0, 44.0, None
    for _ in range(13):
        mid = (low + high) / 2
        page.evaluate(OVERRIDE, mid)
        page.wait_for_timeout(30)
        fits = page.evaluate(FITS)
        if fits["wrapped"] or fits["scrolling"]:
            high = mid
        else:
            low, best = mid, mid
    page.evaluate(OVERRIDE, None)
    return best


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_nothing_in_a_tile_wraps_or_scrolls(page, label, width, height):
    _show(page, width, height)

    fits = page.evaluate(FITS)
    assert fits["wrapped"] == [], f"{label}: wrapped {fits['wrapped']}"
    assert fits["scrolling"] == [], f"{label}: scrolling {fits['scrolling']}"


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_the_baseline_fits_too(page, label, width, height):
    """The same, with no managed loads: four tiles rather than five."""
    _show(page, width, height, loads=[])

    fits = page.evaluate(FITS)
    assert fits["wrapped"] == [], f"{label}: wrapped {fits['wrapped']}"
    assert fits["scrolling"] == [], f"{label}: scrolling {fits['scrolling']}"


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_the_text_is_nearly_as_large_as_the_tile_allows(page, label, width, height):
    """
    The tiles should be reading the room they have, not a fixed fraction of the viewport.

    A loose bound, deliberately. The ceiling is measured in whatever font the machine
    running the tests falls back to -- the stylesheet asks for Segoe UI, which exists on
    none of them -- and that moves it by more than 10%: the same viewport measures 13.6px
    against DejaVu Sans and 15.2px on a GitHub runner, while the CSS produces one size for
    both. The coefficients are fitted to the widest of those, so a narrower font shows up
    here as headroom that cannot be claimed without wrapping somewhere else.

    So this catches a size that is wrong by a lot, which is what it was written for: the
    viewport-scaled sizing it replaced scored 53-59% of the ceiling. For the exact
    coefficients see test_the_size_is_the_one_the_stylesheet_documents, which does not
    depend on a font at all.
    """
    _show(page, width, height)

    actual = _content_font(page)
    ceiling = _ceiling(page)
    assert ceiling is not None, f"{label}: no font fits at all"
    target = min(ceiling, MAX_CONTENT_PX)
    assert actual >= 0.75 * target, (
        f"{label}: text is {actual:.1f}px where {ceiling:.1f}px fits "
        f"({100 * actual / ceiling:.0f}% of what the tile allows)"
    )


# The clamps in style.css, as (floor, cqi coefficient, px offset, cap). The height term
# is 1.75vh - 2.4px for both, and applies only in the side-by-side layout.
CONTENT_CLAMP = (9.0, 5.4, 2.4, 28.0)
HEADER_CLAMP = (10.0, 6.0, 2.4, 30.0)
HEIGHT_TERM = (1.75, 2.4)
STACKED_BELOW = 769


def _expected(clamp, tile_width, viewport_height):
    floor, cqi, offset, cap = clamp
    size = cqi * tile_width / 100 - offset
    if viewport_height is not None:
        vh, vh_offset = HEIGHT_TERM
        size = min(size, vh * viewport_height / 100 - vh_offset)
    return min(max(size, floor), cap)


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
@pytest.mark.parametrize("tiles", [4, 5], ids=["four-tiles", "five-tiles"])
def test_the_size_is_the_one_the_stylesheet_documents(page, label, width, height, tiles):
    """
    The text is a function of the tile's own inline size, and of the row's height where
    the tiles share one -- not of the viewport's width, and not of how many tiles there
    are except through the width each one gets.

    No font is involved in this, so it says the same thing on every machine. It is the
    test that fails if the sizing goes back to viewport units, if a per-tile-count
    correction reappears, or if a coefficient is edited without the stylesheet's
    measurements being redone.
    """
    _show(page, width, height, loads=TWO_LOADS if tiles == 5 else [])

    measured = page.evaluate(
        """() => {
            const box = [...document.querySelectorAll('.top-box')].find(b => b.offsetParent);
            const cs = getComputedStyle(box);
            return {
                tile: box.getBoundingClientRect().width
                    - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
                header: parseFloat(getComputedStyle(box.querySelector('.header')).fontSize),
                content: parseFloat(getComputedStyle(box.querySelector('.content')).fontSize),
            };
        }"""
    )
    # Stacked one per row, the tiles are as tall as their content and the height term is
    # scoped out of the stylesheet.
    row_height = height if width >= STACKED_BELOW else None

    for tier, clamp in (("content", CONTENT_CLAMP), ("header", HEADER_CLAMP)):
        expected = _expected(clamp, measured["tile"], row_height)
        assert abs(measured[tier] - expected) < 0.6, (
            f"{label} {tiles} tiles: {tier} is {measured[tier]:.2f}px, "
            f"expected {expected:.2f}px for a {measured['tile']:.0f}px tile"
        )


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_the_header_chips_stay_inside_their_headers(page, label, width, height):
    """
    The chips are positioned against the header, whose padding scales with its text, so
    a chip pinned a fixed distance from the top outgrows it on a short screen: at
    1280x720 the header is 21px tall and a 16px chip hung 5px out the bottom.
    """
    _show(page, width, height)

    spilling = page.evaluate(
        """() => {
            const out = [];
            for (const box of document.querySelectorAll('.top-box')) {
                if (box.offsetParent === null) { continue; }
                const h = box.querySelector('.header').getBoundingClientRect();
                for (const chip of box.querySelectorAll('.header_notification')) {
                    const r = chip.getBoundingClientRect();
                    const out_by = Math.round(Math.max(r.bottom - h.bottom, h.top - r.top));
                    if (out_by > 0) {
                        out.push(`${box.id || 'tile'}: chip ${out_by}px outside a `
                            + `${Math.round(h.height)}px header`);
                    }
                }
            }
            return out;
        }"""
    )
    assert spilling == [], f"{label}: {spilling}"


@pytest.mark.parametrize(
    "label,width,height",
    [pytest.param(*v, marks=pytest.mark.xfail(strict=True, reason="see the docstring"))
     for v in TABLET_BAND],
)
def test_the_row_cannot_be_shared_below_250px_a_tile(page, label, width, height):
    """
    Documents a limit of the row rather than of the type size.

    Between the phone breakpoint (768px) and about 1300px, four or five tiles side by
    side get 124px to 255px each, and the row's widest pairing -- "Needed Energy" against
    "12.4 kWh / 3.21 EUR" -- does not fit any legible size in that: the ceiling at 196px
    is 8.9px, and at 124px it is 4.8px. Shrinking type is the wrong answer at that point;
    the row has to stack or wrap, which is a layout change and not this one.

    Strict xfail: whoever makes that change should see this test start passing and delete
    the exclusion above.
    """
    _show(page, width, height)

    fits = page.evaluate(FITS)
    assert fits["wrapped"] == [], f"{label}: wrapped {fits['wrapped']}"
