"""
The managed loads tile and its overlay.

Two things are checked here that only a browser can answer: that the tile does not cost
its four neighbours their layout, and that the header chip and the menu actually open
something. The first cut did neither -- the tile pushed every other tile below the width
its labels need, and the chip was styled as clickable while being wired to nothing.
"""

import json

import pytest

import tests.web.wizard_driver as wz  # noqa: F401  (keeps the shared skip behaviour)


# Widths users actually have, including the 4:3 case that scaled the font by height and
# so produced the largest text in the narrowest tiles.
VIEWPORTS = [
    ("1920x1080", 1920, 1080),
    ("1680x1200", 1680, 1200),
    ("1440x900", 1440, 900),
    ("1280x800", 1280, 800),
    ("phone-portrait", 390, 844),
]

TWO_LOADS = [
    {
        "id": "pool", "type": "pool_heatpump", "reason": "below target temperature",
        "energy_needed_wh": 18400, "planned_wh": 18400, "released": True,
        "next_release_start": "2026-09-06T13:00:00+02:00",
        "temperature_c": 24.2, "target_temperature_c": 28.0,
    },
    {
        "id": "sauna", "type": "sauna", "reason": "at target temperature",
        "energy_needed_wh": 0, "planned_wh": 0, "released": False,
        "next_release_start": None, "temperature_c": 88.5, "target_temperature_c": 90.0,
    },
]

# Line boxes a text node really occupies. Header height is no use — the notification
# chips live in there and inflate it.
LINE_BOXES = """
(sel) => {
    const lines = (el) => {
        let max = 0;
        for (const node of el.childNodes) {
            if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) { continue; }
            const r = document.createRange();
            r.selectNodeContents(node);
            const rects = [...r.getClientRects()].filter(x => x.width > 0.5);
            max = Math.max(max, new Set(rects.map(x => Math.round(x.top))).size);
        }
        return max;
    };
    const out = [];
    for (const box of document.querySelectorAll('.top-box')) {
        if (box.offsetParent === null) { continue; }
        for (const cell of box.querySelectorAll('.content td, .content th')) {
            const n = lines(cell);
            if (n > 1) { out.push((box.id || 'tile') + ': ' + cell.textContent.trim().slice(0, 30)); }
        }
    }
    return out;
}
"""


def _seed(page, loads):
    """Put loads on the dashboard and replace the markup's placeholder text.

    The placeholders ("... next charge time") never appear at runtime, and measuring
    them reports wrapping that no user ever sees.
    """
    page.evaluate(
        """(loads) => {
            controlsManager.updateManagedLoads(loads);
            for (const el of document.querySelectorAll('.top-box td, .top-box th, .header_notification')) {
                if (el.textContent.trim().startsWith('...')) { el.textContent = '13:00'; }
            }
        }""",
        loads,
    )
    page.wait_for_timeout(80)


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_no_tile_wraps_once_managed_loads_are_configured(page, label, width, height):
    """
    The regression this exists for: a fifth tile took every other one from 400px to
    316px at 1680x1200, and Statistics and Battery State started wrapping.
    """
    page.set_viewport_size({"width": width, "height": height})
    _seed(page, TWO_LOADS)

    wrapped = page.evaluate(LINE_BOXES, None)
    assert wrapped == [], f"{label}: wrapped labels {wrapped}"


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_the_baseline_stays_clean_too(page, label, width, height):
    """The same check with no managed loads, so a fix here cannot break what was fine."""
    page.set_viewport_size({"width": width, "height": height})
    _seed(page, [])

    wrapped = page.evaluate(LINE_BOXES, None)
    assert wrapped == [], f"{label}: wrapped labels {wrapped}"


def test_the_tile_is_hidden_until_something_is_configured(page):
    _seed(page, [])
    assert page.evaluate(
        "() => document.getElementById('managed_loads_box').offsetParent === null"
    )


def test_the_tile_appears_with_one_row_per_load(page):
    _seed(page, TWO_LOADS)
    names = page.evaluate(
        "() => [...document.querySelectorAll('#managed_loads_rows .managed-load-name')]"
        ".map(e => e.textContent.trim())"
    )
    assert names == ["pool", "sauna"]


def test_long_names_are_clipped_rather_than_wrapped(page):
    """A wrapped name doubles the tile's height for no information."""
    page.set_viewport_size({"width": 1680, "height": 1200})
    _seed(page, [dict(TWO_LOADS[0], id="pool_heat_pump_south_terrace_upper")])

    overflow = page.evaluate(
        """() => {
            const el = document.querySelector('#managed_loads_rows .managed-load-name');
            return {clipped: el.scrollWidth > el.clientWidth,
                    style: getComputedStyle(el).textOverflow};
        }"""
    )
    assert overflow["style"] == "ellipsis"
    assert page.evaluate(LINE_BOXES, None) == []


def test_more_than_four_loads_collapse_into_a_count(page):
    """Otherwise the tile grows taller than its neighbours."""
    many = [dict(TWO_LOADS[0], id=f"load{i}") for i in range(7)]
    _seed(page, many)

    rows = page.evaluate(
        "() => document.querySelectorAll('#managed_loads_rows tr').length"
    )
    more = page.evaluate(
        "() => (document.querySelector('.managed-load-more') || {}).textContent || ''"
    )
    assert rows == 5           # four loads plus the summary row
    assert "+3 more" in more


def test_the_header_chip_sums_the_planned_energy(page):
    _seed(page, TWO_LOADS)
    text = page.evaluate(
        "() => document.getElementById('managed_loads_total').textContent.trim()"
    )
    assert text.startswith("18.4")


# ── The overlay ─────────────────────────────────────────────────────────────────

def test_the_header_chip_opens_the_overlay(page):
    """It was styled as clickable and wired to nothing."""
    _seed(page, TWO_LOADS)
    page.click("#managed_loads_total")
    page.wait_for_selector("#full_screen_overlay", state="visible")

    header = page.text_content("#full_screen_header")
    assert "Managed Loads" in header


def test_the_overlay_shows_every_configured_load(page):
    _seed(page, TWO_LOADS)
    page.click("#managed_loads_total")
    page.wait_for_selector("#full_screen_content")

    content = page.text_content("#full_screen_content")
    # Straight from the API, not from the tile — the overlay refetches for the detail.
    assert "pool" in content
    assert "Energy needed" in content


def test_the_menu_offers_managed_loads_only_once_configured(page):
    _seed(page, [])
    page.evaluate("() => showMainMenu('v', 'b', 'g')")
    assert "Managed Loads" not in page.text_content("#main-dropdown-menu")
    page.evaluate("() => closeDropdownMenu()")

    _seed(page, TWO_LOADS)
    page.evaluate("() => showMainMenu('v', 'b', 'g')")
    assert "Managed Loads" in page.text_content("#main-dropdown-menu")


def test_the_menu_entry_opens_the_same_overlay(page):
    _seed(page, TWO_LOADS)
    page.evaluate("() => showManagedLoadsMenu()")
    page.wait_for_selector("#full_screen_overlay", state="visible")
    assert "Managed Loads" in page.text_content("#full_screen_header")
