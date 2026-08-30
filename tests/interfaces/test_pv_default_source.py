"""
The built-in ("default") PV source — the one a fresh install now starts on.

It is meant to be configuration-free: no provider, no API key, and no installation, so
the setup wizard can finish without asking a new user for their roof. It is also the
only source that contacts nothing, synthesizing a fixed bell curve instead.

Both halves of that were broken. The wizard, correctly, saves no ``pv_forecast`` entry
for a source that needs none — but the summarizer only ever reached the curve through
``for config_entry in self.config``, so an empty list produced an empty forecast while
the configuration still validated as *valid*: the optimizer silently ran with no solar
at all. And an installation added by hand was rejected unless it carried a latitude and
longitude that this source never reads.
"""

import pytest

from src.interfaces.pv_interface import DEFAULT_PV_NOMINAL_POWER_W, PvInterface

# Peak of the curve, as a fraction of the array's nominal power, at midday.
PEAK_FRACTION = 0.7


@pytest.fixture(autouse=True)
def no_update_thread(monkeypatch):
    """The forecast is derived on demand here; the background fetcher is noise."""
    monkeypatch.setattr(
        PvInterface, "_PvInterface__start_update_service", lambda self: None
    )


def _pv(entries, time_frame_base=3600):
    return PvInterface(
        config_source={"source": "default"},
        config=entries,
        time_frame_base=time_frame_base,
        config_special={},
        timezone="Europe/Berlin",
    )


# ----------------------------------------------------------------------
# No installation — what the wizard actually saves
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "time_frame_base, slots",
    [(3600, 48), (900, 192)],
    ids=["hourly", "quarter-hourly"],
)
def test_it_forecasts_without_an_installation(time_frame_base, slots):
    """
    The whole point of the source. This returned ``[]`` before, which reads downstream
    as "the sun never shines" rather than as a missing configuration.
    """
    forecast = _pv([], time_frame_base).get_summarized_pv_forecast()

    assert len(forecast) == slots
    assert max(forecast) == pytest.approx(PEAK_FRACTION * DEFAULT_PV_NOMINAL_POWER_W)


def test_the_assumed_array_is_a_plausible_home():
    """
    A shed-sized array would make the demo look broken rather than illustrative, and
    the figure is load-bearing for what a new user sees on their first dashboard.
    """
    assert DEFAULT_PV_NOMINAL_POWER_W == 4000


def test_an_install_with_no_installation_is_still_valid():
    """It must not land in DEGRADED mode for having nothing to configure."""
    pv = _pv([])

    assert pv.configuration_valid is True
    assert pv.configuration_state == "valid"


# ----------------------------------------------------------------------
# Shape of the curve — the contract every consumer relies on
# ----------------------------------------------------------------------


def test_the_curve_is_anchored_to_midnight():
    """
    Index 0 is 00:00 local, as it is for every real provider: the EOS request builder
    slices from ``seconds_since_midnight``, so a now-anchored array would be read as
    the wrong time of day.
    """
    forecast = _pv([]).get_summarized_pv_forecast()

    assert forecast[0] == 0.0  # 00:00
    assert forecast[3] == 0.0  # 03:00, still dark
    assert forecast[12] == max(forecast)  # midday peak


def test_it_covers_two_days():
    """The optimizer looks a day ahead, so a 24 h array would run short."""
    forecast = _pv([]).get_summarized_pv_forecast()

    assert forecast[:24] == forecast[24:]


def test_the_sun_sets():
    """A flat or always-on curve would let the optimizer charge from imaginary solar."""
    forecast = _pv([]).get_summarized_pv_forecast()

    assert forecast[:6] == [0.0] * 6
    assert forecast[19:24] == [0.0] * 5


# ----------------------------------------------------------------------
# With installations — the source is config-free, not config-hostile
# ----------------------------------------------------------------------


def test_an_installation_only_needs_its_size():
    """
    Nothing here reads coordinates, so demanding them put a perfectly serviceable
    config into DEGRADED mode and zeroed the forecast.
    """
    pv = _pv([{"name": "roof", "power": 5000}])

    assert pv.configuration_valid is True
    assert max(pv.get_summarized_pv_forecast()) == pytest.approx(PEAK_FRACTION * 5000)


def test_installations_are_summed():
    """
    Someone who has described their roof should see the curve scaled to it rather than
    to the assumed default — and to all of it, not just the first array.
    """
    pv = _pv([{"name": "east", "power": 3000}, {"name": "west", "power": 2000}])

    assert max(pv.get_summarized_pv_forecast()) == pytest.approx(PEAK_FRACTION * 5000)


def test_an_installation_without_a_size_falls_back():
    """
    ``power`` is no longer guaranteed by validation for this source, and the curve used
    to index it directly — a KeyError on the update thread, which fetches nothing else.
    """
    pv = _pv([{"name": "roof"}])

    forecast = pv.get_summarized_pv_forecast()
    assert max(forecast) == pytest.approx(PEAK_FRACTION * DEFAULT_PV_NOMINAL_POWER_W)
