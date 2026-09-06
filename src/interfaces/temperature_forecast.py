"""
Outside-temperature forecast, independent of who provides it.

The curve used to come from ``api.akkudoktor.net/forecast`` and nowhere else. That
endpoint proxies a weather service which rate-limits it, and when the upstream refuses,
the API relays a 429 to everyone regardless of their own request rate - so a forecast
that is only as available as one host is not available enough for something a heat pump
plans against.

This module knows how to ask more than one provider for the same 48 hourly numbers, so
the choice is a setting rather than a rewrite. It deliberately holds no state and does
no caching or backing off: `PvInterface` owns all of that and applies it whichever
provider answered.
"""

import logging

import requests

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


class TemperatureForecastError(Exception):
    """A provider could not supply a usable curve. The message names the provider."""


def fetch_openmeteo_temperature(latitude, longitude, timezone="UTC", hours=48,
                                request_timeout=10):
    """
    Hourly air temperature in degrees Celsius, starting at local midnight today.

    Open-Meteo returns whole local days when a timezone is given, which is exactly the
    convention the rest of EOS Connect indexes forecasts by - slot 0 is local midnight -
    so no realignment is needed.

    Args:
        latitude: Site latitude.
        longitude: Site longitude.
        timezone: IANA zone name, which decides where the days are cut.
        hours: How many hourly values are wanted; rounded up to whole days when asked.
        request_timeout: Per-request timeout in seconds.

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

    try:
        response = requests.get(OPENMETEO_URL, params=params, timeout=request_timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as exc:
        raise TemperatureForecastError(f"Open-Meteo is unreachable: {exc}") from exc
    except ValueError as exc:
        raise TemperatureForecastError("Open-Meteo returned something that is not JSON") from exc

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

    logger.debug(
        "[TEMP-FC] Open-Meteo returned %d hourly values for %s, %s",
        len(numbers), latitude, longitude,
    )
    return numbers
