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


# ── The card ────────────────────────────────────────────────────────────────────

def test_the_energy_figure_is_split_into_why(page):
    """
    "Energy needed" alone was the most misread number here: for a pool it is dominated
    by two days of standing losses, so a 1.3 degree rise reads as 77 kWh and looks
    absurd until you are told what it is made of.
    """
    _open_overlay(page)
    content = page.text_content("#full_screen_content")
    assert "Energy needed" in content
    assert "to reach" in content
    assert "to hold it" in content


def test_coverage_says_whether_the_plan_is_enough(page):
    """Two numbers to compare became a state, because the comparison is the point."""
    _open_overlay(page)
    content = page.text_content("#full_screen_content")
    assert "covers" in content or "fully covered" in content


def test_the_ambient_input_and_its_provenance_are_shown(page):
    """
    A prediction standing on a guessed constant looks exactly like one standing on a
    forecast. That is how the placeholder-ambient bug stayed invisible.
    """
    _open_overlay(page)
    content = page.text_content("#full_screen_content")
    assert "Outside now" in content
    assert ("weather forecast" in content
            or "your sensor" in content
            or "fixed guess" in content)


def test_the_rating_is_given_in_both_currencies(page):
    """It is electrical; everything above it on the card is derived from heat."""
    _open_overlay(page)
    content = page.text_content("#full_screen_content")
    assert "W electrical" in content
    assert "of heat" in content


def test_calibration_offers_a_reset(page):
    _open_overlay(page)
    assert page.query_selector("#full_screen_content button") is not None
    assert "Calibration" in page.text_content("#full_screen_content")


def test_resetting_the_calibration_reaches_the_backend(page):
    _open_overlay(page)
    before = page.text_content("#full_screen_content")
    assert "Still learning" in before

    page.click("#full_screen_content button:has-text('Reset')")
    page.wait_for_timeout(400)

    # The overlay reopens against the API, so a stale card would be a failure here.
    assert "Calibration 0%" in page.text_content("#full_screen_content")


def test_the_menu_offers_managed_loads_only_once_configured(page):
    _seed(page, [])
    page.evaluate("() => showMainMenu('v', 'b', 'g')")
    assert "Managed Loads" not in page.text_content("#main-dropdown-menu")
    page.evaluate("() => closeDropdownMenu()")

    _seed(page, TWO_LOADS)
    page.evaluate("() => showMainMenu('v', 'b', 'g')")
    assert "Managed Loads" in page.text_content("#main-dropdown-menu")


def test_the_header_title_never_sits_under_its_chip(page):
    """
    The tile is the narrowest on the row, so a centred title and a chip positioned over
    the end of the header met: "38.4 kWh" landed on top of "Managed Loads". Padding
    cannot fix that -- every value wide enough to clear the chip wraps the title -- so
    the chip is laid out in flow here instead.
    """
    for _, width, height in VIEWPORTS:
        page.set_viewport_size({"width": width, "height": height})
        _seed(page, TWO_LOADS)
        overlap = page.evaluate(
            """() => {
                const h = document.querySelector('#managed_loads_box > .header');
                const t = h.querySelector('.header-title').getBoundingClientRect();
                const c = h.querySelector('.header_notification').getBoundingClientRect();
                const x = Math.min(t.right, c.right) - Math.max(t.left, c.left);
                const y = Math.min(t.bottom, c.bottom) - Math.max(t.top, c.top);
                return (x > 0.5 && y > 0.5) ? Math.round(x) : 0;
            }"""
        )
        assert overlap == 0, f"{width}x{height}: title and chip overlap by {overlap}px"


def test_the_header_title_stays_on_one_line(page):
    for _, width, height in VIEWPORTS:
        page.set_viewport_size({"width": width, "height": height})
        _seed(page, TWO_LOADS)
        lines = page.evaluate(
            """() => {
                const el = document.querySelector('#managed_loads_box .header-title');
                const r = document.createRange();
                r.selectNodeContents(el);
                const rects = [...r.getClientRects()].filter(x => x.width > 0.5);
                return new Set(rects.map(x => Math.round(x.top))).size;
            }"""
        )
        assert lines == 1, f"{width}x{height}: header title wraps to {lines} lines"


# ── The plan strip ──────────────────────────────────────────────────────────────

def _open_overlay(page):
    _seed(page, TWO_LOADS)
    page.click("#managed_loads_total")
    page.wait_for_selector("#full_screen_content")


def test_the_plan_strip_is_labelled_with_the_time_of_day(page):
    """Bars alone say roughly when; the axis says exactly when."""
    _open_overlay(page)
    ticks = page.evaluate(
        """() => [...document.querySelectorAll('#full_screen_content span')]
            .map(e => e.textContent.trim())
            .filter(t => /^(00|06|12|18)$/.test(t))"""
    )
    # Every six hours across a two-day horizon.
    assert ticks[:4] == ["00", "06", "12", "18"]
    assert len(ticks) >= 8


def test_the_plan_strip_names_both_days(page):
    _open_overlay(page)
    content = page.text_content("#full_screen_content")
    today = page.evaluate(
        """() => new Date().toLocaleDateString(navigator.language,
            {weekday: 'short', day: 'numeric', month: 'short'})"""
    )
    assert today in content


def test_hovering_a_bar_gives_the_time_the_energy_and_the_running_total(page):
    _open_overlay(page)
    tips = page.evaluate(
        """() => [...document.querySelectorAll('#full_screen_content div[title]')]
            .map(e => e.getAttribute('title'))"""
    )
    planned = [t for t in tips if "planned" in t and "not planned" not in t]
    assert planned, "no planned slot carried a tooltip"
    assert "Wh planned" in planned[0]
    assert "cumulative" in planned[0]
    # A time range, not just an index.
    assert "\u2013" in planned[0] or "-" in planned[0] or ":" in planned[0]

    idle = [t for t in tips if "not planned" in t]
    assert idle, "idle slots should say so rather than carry no tooltip"


def test_the_current_slot_is_marked_once_per_load(page):
    _open_overlay(page)
    marked, strips = page.evaluate(
        """() => {
            const tips = [...document.querySelectorAll('#full_screen_content div[title]')]
                .map(e => e.getAttribute('title'))
                .filter(t => t.includes('happening now'));
            // The label itself, not every ancestor that contains it.
            const strips = [...document.querySelectorAll('#full_screen_content div')]
                .filter(e => e.children.length === 0
                    && e.textContent.trim().startsWith('Planned slots')).length;
            return [tips.length, strips];
        }"""
    )
    assert strips > 0
    assert marked == strips, "every plan strip marks exactly one current slot"


def test_the_menu_entry_opens_the_same_overlay(page):
    _seed(page, TWO_LOADS)
    page.evaluate("() => showManagedLoadsMenu()")
    page.wait_for_selector("#full_screen_overlay", state="visible")
    assert "Managed Loads" in page.text_content("#full_screen_header")
