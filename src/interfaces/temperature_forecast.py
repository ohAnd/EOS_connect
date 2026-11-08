"""
Outside-temperature forecast, independent of who provides it.

The curve used to come from ``api.akkudoktor.net/forecast`` and nowhere else. That
endpoint proxies a weather service which rate-limits it, and when the upstream refuses,
the API relays a 429 to everyone regardless of their own request rate - so a forecast
that is only as available as one host is not available enough for something a heat pump
plans against.

This module knows how to ask more than one provider for the same 48 hourly numbers, so
the choice is a setting rather than a rewrite. It deliberately holds no state: the
cache, the failure counting and the rate-limit hold-offs are `PvInterface`'s, and it
applies them whichever provider answered. The retry ladder below is not state - it is
the same stateless transport every other public forecast API here goes through.
"""

import logging

import requests

try:  # running from src/ — src/ is on sys.path
    from interfaces.http_fetch import escalating_timeouts, retry_request
except ImportError:  # imported as src.interfaces (tests)
    from .http_fetch import escalating_timeouts, retry_request

logger = logging.getLogger("__main__")

OPENMETEO = "openmeteo"
AKKUDOKTOR = "akkudoktor"

#: Providers a user may pick. Open-Meteo first: it needs no key, publishes the value
#: directly rather than deriving it from a PV forecast query, and is not currently
#: refusing every request. Mirrored by ``TEMPERATURE_SOURCES`` in the config schema.
TEMPERATURE_PROVIDERS = (OPENMETEO, AKKUDOKTOR)

DEFAULT_TEMPERATURE_PROVIDER = OPENMETEO

OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"

# Anything outside this is not weather. A PV figure leaking into the temperature array
# is the failure this catches - it looks like a number and ruins everything downstream.
PLAUSIBLE_MIN_C = -60.0
PLAUSIBLE_MAX_C = 60.0

# Retry policy for the temperature request, whichever provider serves it.  Deliberately
# shorter than the PV one: the cache is the real recovery strategy here (a held forecast
# is a good answer, a held PV array is not), and every retry blocks the shared update
# loop.  Lives here rather than in pv_interface so the akkudoktor route and the
# Open-Meteo route below cannot drift onto two different policies.
TEMP_MAX_RETRIES = 2
TEMP_RETRY_DELAY_S = 2

# Each attempt waits longer than the last, so a provider that is merely slow is ridden
# out instead of being asked the same impossible question twice - see
# `escalating_timeouts` and issue #358.
TEMPERATURE_TIMEOUTS = escalating_timeouts(TEMP_MAX_RETRIES)


class TemperatureForecastError(Exception):
    """A provider could not supply a usable curve. The message names the provider."""


def fetch_openmeteo_temperature(latitude, longitude, timezone="UTC", hours=48):
    """
    Hourly air temperature in degrees Celsius, starting at local midnight today.

    Open-Meteo returns whole local days when a timezone is given, which is exactly the
    convention the rest of EOS Connect indexes forecasts by - slot 0 is local midnight -
    so no realignment is needed.

    The request and its decoding are retried on `TEMPERATURE_TIMEOUTS` - a proxy's
    HTML error page is as transient as a timeout. A series that did decode is judged
    once: missing, non-numeric or implausible says the same thing however often it is
    asked for, and asking again only costs a request.

    Args:
        latitude: Site latitude.
        longitude: Site longitude.
        timezone: IANA zone name, which decides where the days are cut.
        hours: How many hourly values are wanted; rounded up to whole days when asked.

    Returns:
        A list of *hours* floats.

    Raises:
        TemperatureForecastError: Unreachable, malformed, or implausible.
    """
    days = max(1, -(-int(hours) // 24))
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "temperature_2m",
        "forecast_days": days,
        "timezone": timezone or "UTC",
    }

    def request_and_parse(timeout):
        response = requests.get(OPENMETEO_URL, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def error_handler(kind, exc):
        # Raising rather than returning: this function's contract is a curve or a
        # TemperatureForecastError, and the caller counts the failure either way.
        if kind == "timeout":
            raise TemperatureForecastError(
                "Open-Meteo did not answer within "
                + "/".join(str(t) for t in TEMPERATURE_TIMEOUTS)
                + " s"
            ) from exc
        if kind == "invalid_json":
            raise TemperatureForecastError(
                "Open-Meteo returned something that is not JSON"
            ) from exc
        raise TemperatureForecastError(f"Open-Meteo is unreachable: {exc}") from exc

    payload = retry_request(
        request_and_parse,
        error_handler,
        delay=TEMP_RETRY_DELAY_S,
        timeouts=TEMPERATURE_TIMEOUTS,
    )

    values = (payload.get("hourly") or {}).get("temperature_2m")
    if not isinstance(values, list) or not values:
        raise TemperatureForecastError("Open-Meteo returned no temperature series")

    numbers = []
    for raw in values[:hours]:
        try:
            numbers.append(float(raw))
        except (TypeError, ValueError) as exc:
            raise TemperatureForecastError(
                f"Open-Meteo returned a non-numeric temperature: {raw!r}"
            ) from exc

    if any(v < PLAUSIBLE_MIN_C or v > PLAUSIBLE_MAX_C for v in numbers):
        raise TemperatureForecastError(
            "Open-Meteo returned temperatures outside the plausible range"
        )

    # A provider that answers with a short series is not an error - a day boundary or a
    # trimmed forecast horizon does it - but the array has a fixed length downstream.
    while len(numbers) < hours:
        numbers.append(numbers[-1])

    # Without the coordinates: they are the user's home address, and debug logs get
    # pasted into bug reports. The count is what this line was ever for.
    logger.debug("[TEMP-FC] Open-Meteo returned %d hourly values", len(numbers))
    return numbers
