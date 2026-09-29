"""
The thermal arithmetic behind every storage-type managed load.

Pure functions, no I/O and no state, so the numbers can be checked against a textbook
rather than against a running installation. A pool, a sauna, a hot water tank and a
buffer tank differ only in the values fed to these - which is why they share one model.
"""

import math

# Energy to lift one cubic metre of water by one kelvin:
#   1000 kg x 4186 J/(kg K) = 4.186 MJ/K = 1163 Wh/K
# Saunas heat air rather than water, so they configure a small effective volume that
# reproduces their measured heat-up time. That is a fit, not a lie about physics, and
# the calibrator corrects it from observation anyway.
WH_PER_M3_PER_K = 1163.0

# A store that reports a COP outside this band is reporting a sensor fault. The bounds
# also stop a divide-by-almost-zero turning a small thermal demand into a gigawatt-hour
# of electrical demand.
#
# The floor sits below 1.0 on purpose: not every stored-heat appliance is a heat pump.
# A sauna and many hot water tanks are resistive, so their true COP is 1.0, and a floor
# of 1.5 would have quietly under-forecast every one of them by a third.
COP_MIN = 0.8
COP_MAX = 8.0

# Ambient temperature the nominal COP is quoted at. Air-to-water pool heat pumps are
# rated around 26 C air / 26 C water, which is the standard most datasheets use.
COP_REFERENCE_AMBIENT_C = 26.0


def energy_to_raise_wh(volume_m3, delta_k):
    """Thermal energy to lift *volume_m3* of water by *delta_k*. Never negative."""
    if volume_m3 <= 0 or delta_k <= 0:
        return 0.0
    return WH_PER_M3_PER_K * volume_m3 * delta_k


def loss_power_w(loss_coefficient, surface_m2, medium_c, ambient_c, cover_factor=1.0):
    """
    Heat leaving the store, in watts.

    One lumped coefficient stands in for evaporation, convection and radiation together.
    Splitting them would need wind speed and humidity that nobody has a sensor for, and
    the calibrator recovers the combined figure from the observed cooling rate anyway.

    A negative result - ambient warmer than the water - is returned as a gain, because
    on a hot afternoon an uncovered pool genuinely heats itself.
    """
    if surface_m2 <= 0 or loss_coefficient <= 0:
        return 0.0
    return loss_coefficient * surface_m2 * (medium_c - ambient_c) * cover_factor


def cop_at(ambient_c, cop_nominal, air_coefficient, reference_c=COP_REFERENCE_AMBIENT_C):
    """
    Coefficient of performance as a linear function of ambient temperature.

    A linear fit is crude next to a real compressor map, but it captures the effect that
    actually matters for planning - a pool pump is markedly less efficient at 8 C than at
    25 C - with two parameters that can be recovered from ordinary operating data.
    """
    if not math.isfinite(ambient_c):
        ambient_c = reference_c
    value = cop_nominal * (1.0 + air_coefficient * (ambient_c - reference_c))
    return max(COP_MIN, min(COP_MAX, value))


def thermal_to_electrical_wh(thermal_wh, cop):
    """Electrical energy needed to move *thermal_wh* of heat at *cop*."""
    if thermal_wh <= 0:
        return 0.0
    return thermal_wh / max(COP_MIN, cop)


def observed_loss_coefficient(volume_m3, surface_m2, delta_temp_k, hours,
                              medium_c, ambient_c, cover_factor=1.0):
    """
    Recover the loss coefficient from a stretch of cooling with the pump off.

    ``delta_temp_k`` is the observed temperature change over ``hours`` - negative while
    cooling. Returns None when the sample cannot say anything useful: no time elapsed,
    no temperature difference to drive the loss, or the water warming up (sun on the
    surface, which this model does not attempt to separate out).
    """
    if hours <= 0 or volume_m3 <= 0 or surface_m2 <= 0 or cover_factor <= 0:
        return None
    driving_delta = medium_c - ambient_c
    if driving_delta <= 1.0:
        return None
    if delta_temp_k >= 0:
        return None

    loss_w = -delta_temp_k / hours * WH_PER_M3_PER_K * volume_m3
    return loss_w / (surface_m2 * driving_delta * cover_factor)


def observed_cop(volume_m3, delta_temp_k, hours, electrical_w, loss_w):
    """
    Recover the COP from a stretch of heating.

    Thermal output is what went into the water plus what leaked out while it did, so a
    badly insulated store does not read as an inefficient heat pump. ``loss_w`` is
    signed and used as given: when ambient is warmer than the store the term is negative
    and the heat gained from the air is subtracted, because crediting it to the heat
    pump would report a COP that rises with the weather.
    """
    if hours <= 0 or volume_m3 <= 0 or electrical_w <= 0:
        return None
    stored_w = delta_temp_k / hours * WH_PER_M3_PER_K * volume_m3
    thermal_w = stored_w + loss_w
    if thermal_w <= 0:
        return None
    return thermal_w / electrical_w
