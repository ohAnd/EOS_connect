"""
Does the PV Source screen admit that the temperature provider has nowhere to ask?

The outside-temperature forecast needs a coordinate. A location-based PV source
carries one, so for most installs the question never arises - but EVCC, Victron,
Solcast and timeseries supply no installation at all, and then the only source is
Latitude and Longitude under System. Both default to zero, which is how a numeric
field says "unset", so an install of that shape picks a provider, saves, and quietly
gets a flat 15 C curve with nothing on screen to explain it.

The setting that fixes it lives in another section. That is exactly why the notice
belongs on this one: this is the screen where the expectation is formed.
"""

import tests.web.wizard_driver as wz  # noqa: F401  (keeps the shared skip behaviour)

NOTICE = "No coordinates to ask with"
FIELD = "pv_forecast_source.temperature_source"


def _open_pv_source(page, latitude=0, longitude=0, pv_coords=False, source="openmeteo"):
    page.evaluate("() => showConfigurationMenu()")
    page.wait_for_selector(".config-field")
    page.evaluate(
        """([lat, lon, pv, src]) => {
            const v = configurationManager.values;
            Object.keys(v).filter(k => /^pv_forecast\\.\\d+\\.(lat|lon)$/.test(k))
                .forEach(k => { v[k] = pv ? 48.8 : 0; });
            v.latitude = lat;
            v.longitude = lon;
            v['pv_forecast_source.source'] = src;
            configurationManager._selectSection('pv_forecast_source');
        }""",
        [latitude, longitude, pv_coords, source],
    )
    page.wait_for_selector(f'[data-key="{FIELD}"]')
    return page.evaluate("() => document.body.innerText")


def test_nothing_supplies_coordinates_so_the_screen_says_so(page):
    assert NOTICE in _open_pv_source(page)


def test_a_site_location_settles_it(page):
    """Latitude and Longitude under System are the answer for a sourceless install."""
    assert NOTICE not in _open_pv_source(page, latitude=48.8, longitude=8.9)


def test_a_pv_installation_settles_it_too(page):
    """
    A location-based source carries its own pair and is preferred over the site one,
    so the notice must not nag an install that was already fine.
    """
    assert NOTICE not in _open_pv_source(page, pv_coords=True)


def test_an_installation_hidden_behind_a_non_location_source_does_not_count(page):
    """
    The story that prompted this. Configure OpenMeteo with installations, switch to
    EVCC, and the PV Installations section is replaced by "not needed for evcc" - but
    the entries stay in the database and go on quietly supplying the coordinates the
    temperature provider asks with. The reader sees no installation, sets no site
    location, and is told nothing.

    Config nobody can see must not be what answers the question.
    """
    text = _open_pv_source(page, pv_coords=True, source="evcc")
    assert "still stored" in text, text
    assert "Latitude" in text and "Longitude" in text


def test_a_visible_installation_still_settles_it(page):
    """Under a location-based source the entries are on screen and editable."""
    assert NOTICE not in _open_pv_source(page, pv_coords=True, source="openmeteo")
    assert "still stored" not in _open_pv_source(page, pv_coords=True,
                                                 source="openmeteo")


def test_a_site_location_settles_the_hidden_case_too(page):
    text = _open_pv_source(page, latitude=48.8, longitude=8.9, pv_coords=True,
                           source="evcc")
    assert NOTICE not in text
    assert "still stored" not in text


def test_the_notice_sits_with_the_provider_it_is_about(page):
    """Beside the setting that raised the expectation, not at the foot of the form."""
    _open_pv_source(page)
    gap = page.evaluate(
        """([field, notice]) => {
            const f = document.querySelector(`[data-key="${field}"]`);
            const all = [...document.querySelectorAll('.config-field')];
            const n = all.find(e => e.textContent.includes(notice));
            if (!f || !n) { return null; }
            return n.getBoundingClientRect().top - f.getBoundingClientRect().bottom;
        }""",
        [FIELD, NOTICE],
    )
    assert gap is not None, "the notice was not rendered as a field row"
    assert -5 < gap < 40, f"notice is {gap}px from its field"
