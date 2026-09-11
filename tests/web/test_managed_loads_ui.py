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


# Every line box under the tiles, nested markup included. LINE_BOXES above only reads
# direct text nodes, which is how the italic hint in Battery State -- an <i> inside its
# cell -- stayed invisible to it while it wrapped.
ALL_LINE_BOXES = """
() => {
    const out = [];
    for (const box of document.querySelectorAll('.top-box')) {
        if (box.offsetParent === null) { continue; }
        for (const cell of box.querySelectorAll('.content td, .content th')) {
            const r = document.createRange();
            r.selectNodeContents(cell);
            const rects = [...r.getClientRects()].filter(x => x.width > 0.5);
            if (new Set(rects.map(x => Math.round(x.top))).size > 1) {
                out.push((box.id || 'tile') + ': ' + cell.textContent.trim().slice(0, 34));
            }
        }
    }
    return out;
}
"""


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_the_fifth_tile_wraps_nothing_that_four_did_not(page, label, width, height):
    """
    The tile takes an equal share of the row rather than a capped one, so the four others
    are narrower than they were. Whatever fitted on one line without it has to still fit
    with it -- the text is sized from each tile's own width, so a fifth share makes every
    tile's text smaller rather than pushing a neighbour's label into a second line.

    A delta, not an absolute: this compares the row against itself with and without the
    tile, and still measures the placeholder text the markup ships. Whether the row fits
    at all, against the values a running install shows, is test_tile_sizing_ui.py.
    """
    page.set_viewport_size({"width": width, "height": height})

    _seed(page, [])
    before = page.evaluate(ALL_LINE_BOXES, None)
    _seed(page, TWO_LOADS)
    after = page.evaluate(ALL_LINE_BOXES, None)

    added = [cell for cell in after if cell not in before]
    assert added == [], f"{label}: the fifth tile wrapped {added}"


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


def test_a_shortfall_names_the_setting_behind_it(page):
    """
    It used to read "widen its window or raise the daily cap" whatever the cause. On a
    real install a 22 ct/kWh cap ruled out four fifths of the horizon while the window
    stood open from 06:00 to 23:00, so the page sent the user to the wrong setting.
    """
    _open_overlay(page)
    page.evaluate(
        """() => {
            const load = {id: 'pool', type: 'pool_heatpump', enabled: true,
                reason: 'below target', energy_needed_wh: 24000, planned_wh: 4800,
                plan: [], plan_reasons: [], model: {}, release: null, detail: {},
                plan_summary: {slots: 192, planned: 12, limited_by: 'above price cap',
                               limited_slots: 150, counts: {}}};
            document.getElementById('full_screen_content').innerHTML =
                controlsManager._managedLoadCard(load, 900, 0);
        }"""
    )
    shown = page.text_content("#full_screen_content")
    assert "above price cap" in shown
    assert "150 of 192" in shown
    assert "raise or clear the price cap" in shown
    assert "widen the allowed window" not in shown


def test_each_limit_suggests_its_own_remedy(page):
    _open_overlay(page)
    for limit, remedy in [
        ("outside allowed hours", "widen the allowed window"),
        ("too cold to run", "lower the minimum outside temperature"),
        ("daily runtime cap", "raise the daily runtime cap"),
    ]:
        page.evaluate(
            """(limit) => {
                const load = {id: 'pool', type: 'pool_heatpump', enabled: true,
                    reason: 'below target', energy_needed_wh: 24000, planned_wh: 4800,
                    plan: [], plan_reasons: [], model: {}, release: null, detail: {},
                    plan_summary: {slots: 192, planned: 12, limited_by: limit,
                                   limited_slots: 100, counts: {}}};
                document.getElementById('full_screen_content').innerHTML =
                    controlsManager._managedLoadCard(load, 900, 0);
            }""",
            limit,
        )
        assert remedy in page.text_content("#full_screen_content")


def test_a_covered_plan_blames_nothing(page):
    _open_overlay(page)
    page.evaluate(
        """() => {
            const load = {id: 'pool', type: 'pool_heatpump', enabled: true,
                reason: 'covering losses', energy_needed_wh: 5000, planned_wh: 5000,
                plan: [], plan_reasons: [], model: {}, release: null, detail: {},
                plan_summary: {slots: 192, planned: 40, limited_by: null,
                               limited_slots: 0, counts: {}}};
            document.getElementById('full_screen_content').innerHTML =
                controlsManager._managedLoadCard(load, 900, 0);
        }"""
    )
    shown = page.text_content("#full_screen_content")
    assert "fully covered" in shown
    assert "Limited by" not in shown


def test_a_bar_says_why_it_is_empty(page):
    """The strip already had tooltips; they said "not planned" and nothing more."""
    _open_overlay(page)
    page.evaluate(
        """() => {
            const load = {id: 'pool', type: 'pool_heatpump', enabled: true,
                reason: 'below target', energy_needed_wh: 24000, planned_wh: 1600,
                plan: [1600, 0, 0, 0], plan_reasons: ['planned', 'above price cap',
                    'outside allowed hours', 'too cold to run'],
                model: {}, release: null, detail: {}};
            document.getElementById('full_screen_content').innerHTML =
                controlsManager._managedLoadCard(load, 3600, 0);
        }"""
    )
    tips = page.evaluate(
        """() => [...document.querySelectorAll('#full_screen_content div[title]')]
            .map(e => e.getAttribute('title'))"""
    )
    assert any("above price cap" in t for t in tips)
    assert any("too cold to run" in t for t in tips)


def _render_strip(page, plan, reasons):
    page.evaluate(
        """([plan, reasons]) => {
            const load = {id: 'pool', type: 'pool_heatpump', enabled: true,
                reason: 'below target', energy_needed_wh: 9000, planned_wh: 1600,
                plan: plan, plan_reasons: reasons, model: {}, release: null, detail: {}};
            document.getElementById('full_screen_content').innerHTML =
                controlsManager._managedLoadCard(load, 3600, 0);
        }""",
        [plan, reasons],
    )


def _bar_colours(page):
    return page.evaluate(
        """() => [...document.querySelectorAll('#full_screen_content div[title]')]
            .map(e => getComputedStyle(e).backgroundColor)"""
    )


def test_the_strip_colours_a_capped_slot_differently_from_a_disallowed_one(page):
    """
    Blue says it will run. A slot that could have run but was rationed, and one that
    was never allowed to, are different problems with different remedies -- so they
    cannot look the same as each other or as an empty one.
    """
    _open_overlay(page)
    _render_strip(page,
                  [1600, 0, 0, 0],
                  ['planned', 'above price cap', 'too cold to run', 'not needed'])

    colours = _bar_colours(page)
    assert len(set(colours)) == 4, colours


def test_every_capped_reason_shares_one_colour(page):
    """Three hues is the safe limit when any two states can sit side by side."""
    _open_overlay(page)
    _render_strip(page,
                  [0, 0, 0],
                  ['above price cap', 'daily runtime cap', 'shared power budget'])
    assert len(set(_bar_colours(page))) == 1


def test_every_not_allowed_reason_shares_one_colour(page):
    _open_overlay(page)
    _render_strip(page,
                  [0, 0, 0, 0],
                  ['outside allowed hours', 'out of season', 'too cold to run',
                   'after deadline'])
    assert len(set(_bar_colours(page))) == 1


def test_only_the_running_bars_carry_height(page):
    """A colour must never be readable as a quantity; only the blue bars are."""
    _open_overlay(page)
    _render_strip(page, [1600, 800, 0, 0],
                  ['planned', 'planned', 'above price cap', 'too cold to run'])

    heights = page.evaluate(
        """() => [...document.querySelectorAll('#full_screen_content div[title]')]
            .map(e => e.getBoundingClientRect().height)"""
    )
    assert heights[0] > heights[1] > heights[2]      # energy orders the blue bars
    assert abs(heights[2] - heights[3]) < 1.0        # both blocked bands, same height


def test_the_strip_names_the_states_it_shows(page):
    """Identity is never colour alone."""
    _open_overlay(page)
    _render_strip(page, [1600, 0, 0],
                  ['planned', 'above price cap', 'too cold to run'])

    shown = page.text_content("#full_screen_content")
    assert "Will run" in shown
    assert "Capped" in shown
    assert "Not allowed then" in shown


def test_a_state_that_is_absent_is_not_in_the_legend(page):
    _open_overlay(page)
    _render_strip(page, [1600, 0], ['planned', 'above price cap'])
    shown = page.text_content("#full_screen_content")
    assert "Capped" in shown
    assert "Not allowed then" not in shown


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


def test_a_disagreement_between_sensor_and_forecast_is_shown(page):
    """
    "Outside now" promises a measurement. Showing a regional forecast under that label
    while the thermometer at the site reads 3 K lower hides a quarter of the pool's
    standing loss.
    """
    _open_overlay(page)
    content = page.text_content("#full_screen_content")
    # The harness has no ambient sensor, so it shows the single figure and its source.
    assert "Outside now" in content

    # With a sensor that disagrees, both numbers appear.
    page.evaluate(
        """() => {
            const load = {id: 'pool', type: 'pool_heatpump', enabled: true,
                reason: 'below target', energy_needed_wh: 1000, planned_wh: 1000,
                plan: [], model: {}, release: null,
                detail: {ambient_now_c: 17.9, ambient_measured_c: 14.9,
                         ambient_source: 'forecast_corrected'}};
            const html = controlsManager._managedLoadCard(load, 3600, 0);
            document.getElementById('full_screen_content').innerHTML = html;
        }"""
    )
    shown = page.text_content("#full_screen_content")
    assert "14.9" in shown and "measured" in shown
    assert "17.9" in shown and "model using" in shown
    assert "corrected to your sensor" in shown


def test_agreeing_values_are_not_shown_twice(page):
    _open_overlay(page)
    page.evaluate(
        """() => {
            const load = {id: 'pool', type: 'pool_heatpump', enabled: true,
                reason: 'below target', energy_needed_wh: 1000, planned_wh: 1000,
                plan: [], model: {}, release: null,
                detail: {ambient_now_c: 15.0, ambient_measured_c: 15.1,
                         ambient_source: 'forecast_corrected'}};
            document.getElementById('full_screen_content').innerHTML =
                controlsManager._managedLoadCard(load, 3600, 0);
        }"""
    )
    assert "model using" not in page.text_content("#full_screen_content")


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


def _menu_entries(page):
    page.evaluate("() => showMainMenu('v', 'b', 'g')")
    entries = page.evaluate(
        r"""() => [...document.querySelectorAll('#main-dropdown-menu > div')]
            .map(d => d.textContent.replace(/\s+/g, ' ').trim())"""
    )
    page.evaluate("() => closeDropdownMenu()")
    return entries


def test_the_menu_groups_managed_loads_with_override_controls(page):
    """
    Both are "what is being done to a load right now", so they belong together at the
    top. Managed Loads was below Alarms, which is where diagnostics live.
    """
    _seed(page, TWO_LOADS)
    entries = _menu_entries(page)

    at = entries.index("Override Controls")
    assert entries[at + 1:at + 3] == ["Managed Loads", "PV Auto-Scaling"]


def test_pv_autoscaling_is_offered_even_with_no_managed_loads(page):
    """It is not conditional on anything: its overlay is how a user checks it is running."""
    _seed(page, [])
    entries = _menu_entries(page)

    at = entries.index("Override Controls")
    assert entries[at + 1] == "PV Auto-Scaling"


# ── The tile in its row ────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_the_tile_is_as_wide_as_its_neighbours(page, label, width, height):
    """
    It was capped narrower than the four full tiles to protect their labels, and a short
    fifth tile in a row of four equal ones read as unfinished. The labels are protected
    by the row's font size now, so every tile takes the same share.
    """
    page.set_viewport_size({"width": width, "height": height})
    _seed(page, TWO_LOADS)

    widths = page.evaluate(
        """() => [...document.querySelectorAll('.top-box')]
            .filter(b => b.offsetParent !== null)
            .map(b => [b.id || 'tile', Math.round(b.getBoundingClientRect().width)])"""
    )
    measured = [w for _, w in widths]
    assert max(measured) - min(measured) <= 1, f"{label}: uneven tiles {widths}"


@pytest.mark.parametrize("label,width,height", VIEWPORTS)
def test_the_chip_sits_where_every_other_tile_puts_its_own(page, label, width, height):
    """
    The chip was laid out in flow next to the title, so it floated in the middle of the
    header while every other tile's sits against the right edge.
    """
    page.set_viewport_size({"width": width, "height": height})
    _seed(page, TWO_LOADS)

    insets = page.evaluate(
        """() => [...document.querySelectorAll('.top-box')]
            .filter(b => b.offsetParent !== null)
            .map(b => {
                const header = b.querySelector('.header');
                const chips = [...header.querySelectorAll('.header_notification')];
                const right = chips[chips.length - 1].getBoundingClientRect().right;
                return [b.id || 'tile',
                        Math.round(header.getBoundingClientRect().right - right)];
            })"""
    )
    measured = [inset for _, inset in insets]
    assert max(measured) - min(measured) <= 1, f"{label}: chips misaligned {insets}"


def test_the_header_title_never_sits_under_its_chip(page):
    """
    "Managed Loads" plus an energy value is the longest title-and-chip pairing on the
    row, and with the chip positioned over the end of the header the two met: "38.4 kWh"
    landed on top of the title. Padding wide enough to clear it wraps the title instead,
    so this header is a grid: an icon column, the centred title, and the chip.
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

    # An idle slot names why it is idle. It used to say only "not planned", which is
    # the one thing the reader could already see from the bar being empty.
    idle = [t for t in tips if "Wh planned" not in t]
    assert idle, "idle slots should carry a tooltip too"
    assert all(t.count("\u00b7") >= 1 for t in idle), idle[:3]


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


def test_a_wholly_blocked_plan_still_draws_its_strip(page):
    """
    The strip used to be skipped whenever nothing was planned -- hiding it in the one
    case where it explains the most: a price cap that ruled out the entire horizon.
    """
    _open_overlay(page)
    _render_strip(page, [0, 0, 0, 0],
                  ['above price cap'] * 4)

    assert len(_bar_colours(page)) == 4
    assert "Capped" in page.text_content("#full_screen_content")


def test_a_plan_with_nothing_to_say_draws_no_strip(page):
    """An idle, satisfied load should not sprout an empty chart."""
    _open_overlay(page)
    _render_strip(page, [0, 0, 0], ['not needed', 'past', 'not needed'])
    assert _bar_colours(page) == []


def test_an_undersized_appliance_is_not_blamed_on_a_setting(page):
    """
    When nothing can close the gap, naming the biggest blocker is true and useless:
    the user raises the price cap and the pool still never reaches target.
    """
    _open_overlay(page)
    page.evaluate(
        """() => {
            const load = {id: 'pool', type: 'pool_heatpump', enabled: true,
                reason: 'below target', energy_needed_wh: 36600, planned_wh: 12400,
                plan: [], plan_reasons: [], model: {}, release: null, detail: {},
                plan_summary: {slots: 192, planned: 31, limited_by: 'above price cap',
                               limited_slots: 43, counts: {}, over_committed: true,
                               reachable_wh: 29600, shortfall_wh: 24200}};
            document.getElementById('full_screen_content').innerHTML =
                controlsManager._managedLoadCard(load, 900, 0);
        }"""
    )
    # Normalised: the sentence wraps across source lines in the template.
    shown = " ".join(page.text_content("#full_screen_content").split())
    assert "29.6 kWh" in shown
    assert "No setting closes that gap" in shown
    assert "raise or clear the price cap" not in shown


def test_a_measured_cover_is_told_apart_from_an_assumed_one(page):
    """
    The cover factor is now an output, not a setting, and the card must say which:
    "it cuts losses by 65%" and "we still assume it cuts losses by 65%" are different
    claims, and only one of them is evidence.
    """
    _open_overlay(page)
    for identified, expected in [
        (True, "measures out at 65% off"),
        (False, "still assumed to cut the heat loss by 65%"),
    ]:
        page.evaluate(
            """(identified) => {
                const model = {confidence: 0.8, loss_samples: 40, cop_samples: 20,
                               fit_quality: 0.9, cover_loss_factor: 0.35,
                               cover_identified: identified};
                document.getElementById('full_screen_content').innerHTML =
                    controlsManager._managedLoadCalibration({id: 'pool'}, model);
            }""",
            identified,
        )
        shown = " ".join(page.text_content("#full_screen_content").split())
        assert expected in shown


def test_a_store_with_no_cover_says_nothing_about_one(page):
    _open_overlay(page)
    page.evaluate(
        """() => {
            const model = {confidence: 0.8, loss_samples: 40, cop_samples: 20,
                           fit_quality: 0.9, cover_loss_factor: 1.0};
            document.getElementById('full_screen_content').innerHTML =
                controlsManager._managedLoadCalibration({id: 'tank'}, model);
        }"""
    )
    assert "cover" not in page.text_content("#full_screen_content").lower()
