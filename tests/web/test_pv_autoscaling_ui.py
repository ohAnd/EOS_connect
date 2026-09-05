"""
Browser tests for the PV Autoscaling overlay.

The numbers in this panel are covered by ``tests/interfaces`` and the REST layer by the
API tests.  What is checked here is the part only a browser can see: that the panel fits
the screen it is opened on.  It was built entirely from inline styles with hard-coded
column counts, so on a phone in portrait the Forecast Comparison card overflowed its box
by 18px, every timeframe cell wrapped onto four lines, and the day list was a second
scrollbox nested inside the first.

The overflow assertions walk *every* descendant, not just ``#full_screen_content``.  The
equivalent test in ``test_backup_ui.py`` checks the root only, which is exactly why the
overflow inside the Forecast Comparison card went unseen.
"""

import json

import pytest

# isMobile() and the stylesheet both break at 768px.  The landscape entry matters on its
# own: 874x402 is wider than the breakpoint, so only the max-height arm of the media
# query catches it.
PHONE_SE = {"width": 320, "height": 568}
PHONE = {"width": 402, "height": 874}
PHONE_LANDSCAPE = {"width": 874, "height": 402}
TABLET = {"width": 820, "height": 1180}
DESKTOP = {"width": 1920, "height": 1080}


def _forecast_array():
    """
    Two days of 15-minute slots, Wh each, shaped like a clear day.

    192 values at ``used_time_frame_base: 900`` is what a real install publishes.  The
    panel derives its slot width from that base rather than the array length, so the
    length has to stay consistent with it or "today" and "tomorrow" both read wrong.
    """
    out = []
    for _ in range(2):
        for slot in range(96):
            hour = slot / 4
            if 6 <= hour <= 20:
                # Crude parabola peaking at 13:00, ~2.5 kW at noon.
                out.append(round(max(0.0, 620.0 - 12.0 * (hour - 13) ** 2), 1))
            else:
                out.append(0.0)
    return out


_RAW = _forecast_array()
_SCALED = [round(v * 0.56, 1) for v in _RAW]

# One day carries origin "imported": a restored day renders an extra badge next to the
# date, which is the widest that header row ever gets.
_STATUS = {
    "pv_autoscaling": {
        "enabled": True,
        "running": True,
        "restored_hours": 165,
        "sensor_entity_id": "sensor.openhab_mqtt_pv_system_pv_ertrag_gesamt",
        "min_data_hours_required": 24,
        "retention_days": 7,
        "total_hours_recorded": 167,
        "last_reading_timestamp": "2026-09-04T08:00:00+00:00",
        "last_error": None,
        "consecutive_failures": 0,
        "scale_factors": {"1": 0.64, "2": 0.465, "3": 0.55, "4": 0.738},
        "timeframe_bounds": [
            {"id": 1, "start": 0, "end": 8, "label": "00:00 - 07:59"},
            {"id": 2, "start": 8, "end": 12, "label": "08:00 - 11:59"},
            {"id": 3, "start": 12, "end": 16, "label": "12:00 - 15:59"},
            {"id": 4, "start": 16, "end": 24, "label": "16:00 - 23:59"},
        ],
        "todays_partial_data": {
            "date": "2026-09-04",
            "hours_collected": 11,
            "collected_timeframes": [1, 2],
            "actual_kwh": {"1": 5.153, "2": 1.337, "3": 0.0, "4": 0.0},
            "forecast_kwh": {"1": 0.0, "2": 3.218, "3": 0.0, "4": 0.0},
        },
        "aggregated_history": {
            "days": [
                {
                    "date": f"2026-08-{31 - offset:02d}",
                    "hours_collected": 24,
                    "origin": "imported" if offset % 2 else "measured",
                    "actual_kwh": {"1": 0.26, "2": 3.87, "3": 12.74, "4": 5.15},
                    "forecast_kwh": {"1": 0.26, "2": 9.03, "3": 18.32, "4": 4.60},
                }
                for offset in range(7)
            ]
        },
        "current_forecast_array_raw": _RAW,
        "current_forecast_array_scaled": _SCALED,
        "current_forecast_array_unit": "Wh per forecast slot",
        "used_time_frame_base": 900,
    }
}


def _open_overlay(page, viewport):
    """Size the window first: the panel's layout is chosen by media query, at paint."""
    page.route(
        "**/api/pv_autoscaling/status*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_STATUS)
        ),
    )
    page.set_viewport_size(viewport)
    # Not a click on ``#pv_autoscaling_open``: that trigger is a FontAwesome glyph, and
    # the harness blocks the CDN, so the icon has no box and Playwright will not click
    # it.  Whether the icon opens the panel is not what these tests are about.
    page.wait_for_function("typeof statisticsManager !== 'undefined' && statisticsManager")
    page.evaluate("() => statisticsManager.showPvAutoscalingOverlay()")
    page.wait_for_selector(".pv-scale-tiles")
    # The historical list is the last thing built; without this the measurements can run
    # against a half-laid-out grid.
    page.wait_for_selector(".pv-scale-history")


def _overflowing(page):
    """Every descendant of the overlay content whose own content is wider than it is."""
    return page.evaluate(
        """() => [...document.getElementById('full_screen_content').querySelectorAll('*')]
              .filter(e => e.scrollWidth > e.clientWidth + 1)
              .map(e => (e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60))"""
    )


def _live_scrollers(page):
    """Descendants that are actually scrolling, ignoring ones that merely could."""
    return page.evaluate(
        """() => [...document.getElementById('full_screen_content').querySelectorAll('*')]
              .filter(e => {
                  const s = getComputedStyle(e);
                  return (s.overflowY === 'auto' || s.overflowY === 'scroll')
                      && e.scrollHeight > e.clientHeight + 1;
              }).length"""
    )


@pytest.mark.parametrize(
    "viewport",
    [PHONE_SE, PHONE, PHONE_LANDSCAPE, TABLET, DESKTOP],
    ids=["phone-se", "phone", "phone-landscape", "tablet", "desktop"],
)
def test_nothing_overflows_horizontally(page, viewport):
    """Guards the 18px the Forecast Comparison card used to spill on a phone."""
    _open_overlay(page, viewport)

    assert _overflowing(page) == []
    assert page.eval_on_selector(
        "#full_screen_content", "e => e.scrollWidth <= e.clientWidth + 1"
    )


def test_the_overlay_is_the_only_scroller_on_a_phone(page):
    """Three stacked scroll areas gave a touch three places to land, and two clipped."""
    _open_overlay(page, PHONE)

    assert _live_scrollers(page) == 0


def test_the_day_list_keeps_its_own_scrollbox_on_desktop(page):
    """The nested list is a deliberate desktop affordance; only phones lose it."""
    _open_overlay(page, DESKTOP)

    assert page.eval_on_selector(
        ".pv-scale-history", "e => e.scrollHeight > e.clientHeight + 1"
    )


def test_forecast_cards_stack_on_a_phone(page):
    """Two kWh totals side by side in a 147px column is what broke the card."""
    _open_overlay(page, PHONE)

    today, tomorrow = page.eval_on_selector_all(
        ".pv-scale-forecast-grid > div", "els => els.map(e => e.getBoundingClientRect())"
    )
    assert tomorrow["top"] > today["top"] + 40
    assert abs(tomorrow["left"] - today["left"]) < 2


def test_forecast_cards_sit_side_by_side_on_desktop(page):
    """The wide layout is unchanged."""
    _open_overlay(page, DESKTOP)

    today, tomorrow = page.eval_on_selector_all(
        ".pv-scale-forecast-grid > div", "els => els.map(e => e.getBoundingClientRect())"
    )
    assert tomorrow["left"] > today["left"] + 300
    assert abs(tomorrow["top"] - today["top"]) < 5


def test_whole_day_tile_spans_the_row_on_a_phone(page):
    """Two columns leave it alone on the last row; a half-width orphan looked broken."""
    _open_overlay(page, PHONE)

    grid = page.eval_on_selector(".pv-scale-tiles", "e => e.clientWidth")
    tile = page.eval_on_selector(".pv-scale-tile-total", "e => e.getBoundingClientRect().width")
    assert tile > grid * 0.95


def test_timeframe_cells_hold_a_reading_on_one_line(page):
    """"R: 5.15 kWh" across four phone columns wrapped onto four lines."""
    _open_overlay(page, PHONE)

    # The R line of every cell in the day list, measured against its own line-height.
    lines = page.evaluate(
        """() => [...document.querySelectorAll('.pv-scale-history .pv-scale-tf-grid > div')]
              .map(cell => {
                  const r = cell.children[1];
                  const lh = parseFloat(getComputedStyle(r).lineHeight) || 14;
                  return Math.round(r.getBoundingClientRect().height / lh);
              })"""
    )
    assert lines, "no timeframe cells rendered"
    assert max(lines) == 1


def test_the_panel_reflows_when_the_device_is_turned(page):
    """
    The layout used to be frozen at render time.

    ``showFullScreenOverlay`` read ``isMobile()`` once and baked the answer into inline
    styles, so an overlay opened in portrait kept phone paddings in landscape and vice
    versa.  Nothing re-renders on rotate, so the rules have to be media queries.
    """
    _open_overlay(page, PHONE)
    assert _overflowing(page) == []

    page.set_viewport_size(PHONE_LANDSCAPE)
    page.wait_for_timeout(150)
    assert _overflowing(page) == []

    page.set_viewport_size(PHONE)
    page.wait_for_timeout(150)
    assert _overflowing(page) == []

# ----------------------------------------------------------------------
# Status banners
# ----------------------------------------------------------------------

# Three states the live install never reaches once it is collecting, each with its own
# banner: a stalled collector, a fresh install, and one part-way to its first factors.
_BANNERS = {
    "error": {
        "last_error": "HTTPError: 401 Unauthorized",
        "consecutive_failures": 3,
    },
    "initializing": {
        "total_hours_recorded": 0,
        "todays_partial_data": {},
        "aggregated_history": {"days": []},
    },
    "collecting": {
        "total_hours_recorded": 9,
        "aggregated_history": {"days": []},
    },
}


def _open_with(page, viewport, overrides):
    """The overlay against a status payload patched into one of the banner states."""
    payload = json.loads(json.dumps(_STATUS))
    payload["pv_autoscaling"].update(overrides)
    page.route(
        "**/api/pv_autoscaling/status*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        ),
    )
    page.set_viewport_size(viewport)
    page.wait_for_function("typeof statisticsManager !== 'undefined' && statisticsManager")
    page.evaluate("() => statisticsManager.showPvAutoscalingOverlay()")
    page.wait_for_selector(".pv-scale-tiles")


@pytest.mark.parametrize("state", sorted(_BANNERS), ids=sorted(_BANNERS))
def test_banners_use_fontawesome_not_emoji(page, state):
    """
    ``.github/copilot-instructions.md``: FontAwesome everywhere, never emoji.

    An emoji also renders at whatever size and colour the platform picks, which is why
    the warning triangle came out yellow inside a red banner on some phones.
    """
    _open_with(page, PHONE, _BANNERS[state])

    body = page.inner_text("#full_screen_content")
    assert not set(body) & set("\u26a0\U0001f504\u23f3"), f"emoji left in the {state} banner"
    assert page.eval_on_selector_all(
        ".pv-scale-root i[class*='fa-']", "els => els.length"
    ) > 0


@pytest.mark.parametrize("state", sorted(_BANNERS), ids=sorted(_BANNERS))
def test_banners_fit_a_phone(page, state):
    """The error banner carries a sensor entity id in a <code>, 45 characters with no spaces."""
    _open_with(page, PHONE, _BANNERS[state])

    assert _overflowing(page) == []


def test_the_error_banner_breaks_a_long_entity_id(page):
    """Without an explicit break it is one unbreakable 45-character word."""
    _open_with(page, PHONE, _BANNERS["error"])

    assert page.eval_on_selector_all(
        ".pv-scale-root code", "els => els.every(e => e.scrollWidth <= e.clientWidth + 1)"
    )
