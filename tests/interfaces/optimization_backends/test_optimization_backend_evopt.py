# pylint: disable=protected-access
"""
Unit tests for the DST-safe ``normalize()`` helper inside
``EVOptBackend._transform_request_from_eos_to_evopt``.

The ``normalize()`` closure ensures that every time-series array sent to the
EVopt server has exactly *n* floats regardless of the input length:

- Empty arrays are padded to *n* zeros.
- Arrays shorter than *n* are extended with the last element.
- Arrays longer than *n* are truncated.

Three DST scenarios (normal, spring-forward, fall-back) are covered
for both hourly (time_frame_base=3600) and 15-minute (time_frame_base=900)
modes.

Usage:
    pytest tests/interfaces/optimization_backends/test_optimization_backend_evopt.py -v
"""

import pytest
import pytz
from datetime import datetime as _real_datetime
from unittest.mock import patch

from src.interfaces.optimization_backends.optimization_backend_evopt import EVOptBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="berlin_timezone")
def fixture_berlin_timezone():
    """
    Provides a pytz timezone for Europe/Berlin.

    Returns:
        pytz.timezone: Timezone object for Europe/Berlin.
    """
    return pytz.timezone("Europe/Berlin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(time_frame_base=3600, tz=None):
    """
    Create an EVOptBackend instance without any network access.

    Args:
        time_frame_base: Slot duration in seconds (3600 for hourly, 900 for 15-min).
        tz: pytz timezone; defaults to Europe/Berlin if not supplied.

    Returns:
        EVOptBackend: Configured backend instance.
    """
    if tz is None:
        tz = pytz.timezone("Europe/Berlin")
    return EVOptBackend("http://localhost:8502", time_frame_base, tz)


def _make_eos_request(pv=None, price=None, feed=None, load=None):
    """
    Build a minimal EOS-format request dict with the supplied time-series lists.

    Any argument left as *None* defaults to a 48-element list of representative
    values.

    Args:
        pv: PV forecast list (Wh).
        price: Grid price list (€/Wh).
        feed: Feed-in tariff list (€/Wh).
        load: Household load list (Wh).

    Returns:
        dict: Minimal EOS request payload.
    """
    return {
        "ems": {
            "pv_prognose_wh": pv if pv is not None else [10.0] * 48,
            "strompreis_euro_pro_wh": price if price is not None else [0.0003] * 48,
            "einspeiseverguetung_euro_pro_wh": (
                feed if feed is not None else [0.00008] * 48
            ),
            "gesamtlast": load if load is not None else [400.0] * 48,
        }
    }


def _midnight_mock(tz, year=2026, month=3, day=1):
    """
    Return a ``datetime`` subclass whose ``now()`` is fixed at midnight of the
    given date in *tz*.  Constructor behaviour is identical to the real class.

    This is used to pin ``datetime.now()`` in the EVopt module so that
    ``current_hour`` is always 0 (no array slicing from current-hour offset).

    Args:
        tz: pytz timezone for the fixed midnight.
        year, month, day: Date to use; default is a normal (non-DST) weekday.

    Returns:
        type: A subclass of ``datetime`` with overridden ``now()`` classmethod.
    """
    fixed = tz.localize(_real_datetime(year, month, day, 0, 0, 0))

    class _FixedMidnight(_real_datetime):
        """Datetime subclass returning a fixed midnight."""

        @classmethod
        def now(cls, tz_arg=None):
            """Return the fixed midnight optionally converted to *tz_arg*."""
            return fixed.astimezone(tz_arg) if tz_arg is not None else fixed

    return _FixedMidnight


def _transform(eos_request, time_frame_base=3600, tz=None, year=2026, month=3, day=1):
    """
    Run ``_transform_request_from_eos_to_evopt`` with ``datetime.now`` pinned
    at midnight so that ``current_hour == 0`` and no per-hour slicing occurs.

    Args:
        eos_request: EOS-format request dict.
        time_frame_base: Slot duration in seconds.
        tz: pytz timezone (defaults to Europe/Berlin).
        year, month, day: Date for the pinned midnight.

    Returns:
        tuple: (evopt dict, errors list) from the transformation.
    """
    if tz is None:
        tz = pytz.timezone("Europe/Berlin")
    backend = _make_backend(time_frame_base=time_frame_base, tz=tz)
    mock_dt = _midnight_mock(tz, year=year, month=month, day=day)
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_evopt.datetime",
        mock_dt,
    ):
        return backend._transform_request_from_eos_to_evopt(eos_request)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNormalizeConsistentLengths:
    """
    Verify that all ``time_series`` arrays produced by
    ``_transform_request_from_eos_to_evopt`` always share the same length,
    covering three calendar scenarios (normal, spring-forward, fall-back)
    for both hourly and 15-minute modes.
    """

    def test_normal_day_hourly_all_arrays_same_length(self, berlin_timezone):
        """
        Normal day, hourly mode: all ``time_series`` arrays must have the same
        length *n* (48 when called at midnight with 48-element inputs).

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        req = _make_eos_request()
        evopt, _ = _transform(req, time_frame_base=3600)
        ts = evopt["time_series"]
        lengths = {key: len(val) for key, val in ts.items()}
        assert (
            len(set(lengths.values())) == 1
        ), f"Inconsistent time_series lengths on normal day: {lengths}"

    def test_spring_forward_day_hourly_all_arrays_same_length(self, berlin_timezone):
        """
        Spring-forward day (March 29, 2026), hourly mode: sources deliver
        47-element arrays (one wall-clock hour missing); all ``time_series``
        arrays must still share a single consistent length.

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        req = _make_eos_request(
            pv=[10.0] * 47,
            price=[0.0003] * 47,
            feed=[0.00008] * 47,
            load=[400.0] * 47,
        )
        evopt, _ = _transform(req, time_frame_base=3600, year=2026, month=3, day=29)
        ts = evopt["time_series"]
        lengths = {key: len(val) for key, val in ts.items()}
        assert (
            len(set(lengths.values())) == 1
        ), f"Inconsistent time_series lengths on spring-forward day: {lengths}"

    def test_fall_back_day_hourly_all_arrays_same_length(self, berlin_timezone):
        """
        Fall-back day (October 25, 2026), hourly mode: sources deliver
        49-element arrays (one extra wall-clock hour); all ``time_series``
        arrays must share a single consistent length.

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        req = _make_eos_request(
            pv=[10.0] * 49,
            price=[0.0003] * 49,
            feed=[0.00008] * 49,
            load=[400.0] * 49,
        )
        evopt, _ = _transform(req, time_frame_base=3600, year=2026, month=10, day=25)
        ts = evopt["time_series"]
        lengths = {key: len(val) for key, val in ts.items()}
        assert (
            len(set(lengths.values())) == 1
        ), f"Inconsistent time_series lengths on fall-back day: {lengths}"

    def test_normal_day_15min_all_arrays_same_length_192(self, berlin_timezone):
        """
        Normal day, 15-minute mode: ``n`` is fixed at 192; all ``time_series``
        arrays must have exactly 192 elements even when inputs have 48 elements.

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        req = _make_eos_request()  # 48-element arrays
        evopt, _ = _transform(req, time_frame_base=900)
        ts = evopt["time_series"]
        for key, val in ts.items():
            assert len(val) == 192, f"'{key}' should have 192 elements in 15-min mode"


class TestNormalizePaddingBehavior:
    """
    Verify the specific padding/truncation behavior of ``normalize()``
    in edge and DST scenarios.
    """

    def test_empty_pv_series_padded_with_zeros(self, berlin_timezone):
        """
        An empty PV array must be padded to *n* zeros so ``time_series['ft']``
        has the same length as the other arrays and every element is 0.0.

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        req = _make_eos_request(pv=[])
        evopt, _ = _transform(req, time_frame_base=3600)
        ts = evopt["time_series"]
        n = len(ts["dt"])
        assert len(ts["ft"]) == n, "Empty PV array must be padded to length n"
        assert all(
            v == 0.0 for v in ts["ft"]
        ), "Padded PV values must be 0.0 when source array was empty"

    def test_short_load_series_padded_with_last_value(self, berlin_timezone):
        """
        A load array shorter than *n* must be padded with the last element,
        not with zeros.  Use a distinctive sentinel value to verify this.

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        sentinel = 999.0
        short_load = [400.0] * 30 + [sentinel]  # 31 elements
        req = _make_eos_request(load=short_load)
        evopt, _ = _transform(req, time_frame_base=3600)
        ts = evopt["time_series"]
        n = len(ts["dt"])
        assert len(ts["gt"]) == n, "Short load array must be padded to length n"
        # Every element beyond position 30 must equal the sentinel
        assert all(
            ts["gt"][i] == sentinel for i in range(31, n)
        ), "Padding must repeat the last element of the short load array"

    def test_short_15min_array_padded_to_192(self, berlin_timezone):
        """
        In 15-min mode *n* is fixed at 192.  An input array with only 47
        elements (as could occur on a spring-forward day) must be padded to
        exactly 192 using the last value.

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        last_val = 777.0
        short_arr = [100.0] * 46 + [last_val]  # 47 elements
        req = _make_eos_request(
            pv=short_arr, price=[0.0003] * 47, feed=[0.00008] * 47, load=short_arr
        )
        evopt, _ = _transform(req, time_frame_base=900)
        ts = evopt["time_series"]
        for key in ("dt", "gt", "ft", "p_N", "p_E"):
            assert len(ts[key]) == 192, f"'{key}' must have 192 elements in 15-min mode"
        # Verify padding used the last element  (index 46 is the sentinel)
        assert (
            ts["ft"][191] == last_val
        ), "PV padding in 15-min mode must use the last input value"
        assert (
            ts["gt"][191] == last_val
        ), "Load padding in 15-min mode must use the last input value"

    def test_long_arrays_truncated_to_n(self, berlin_timezone):
        """
        An array longer than *n* must be truncated: only the first *n*
        elements are kept and no extra elements appear.

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        sentinel_long = 1234.0
        long_load = [400.0] * 48 + [sentinel_long]  # 49 elements
        req = _make_eos_request(load=long_load)
        evopt, _ = _transform(req, time_frame_base=3600)
        ts = evopt["time_series"]
        n = len(ts["dt"])
        assert len(ts["gt"]) == n, "Long load array must be truncated to length n"
        assert (
            sentinel_long not in ts["gt"]
        ), "The extra element beyond n must be discarded"

    def test_all_empty_series_produce_consistent_lengths(self, berlin_timezone):
        """
        When all four input series are empty, ``n`` falls to 1 (the default
        guard).  All ``time_series`` arrays must still have the same length.

        Args:
            berlin_timezone: Europe/Berlin timezone fixture.
        """
        req = _make_eos_request(pv=[], price=[], feed=[], load=[])
        evopt, _ = _transform(req, time_frame_base=3600)
        ts = evopt["time_series"]
        lengths = {key: len(val) for key, val in ts.items()}
        assert (
            len(set(lengths.values())) == 1
        ), f"All-empty input must still produce consistent lengths: {lengths}"
