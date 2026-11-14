"""
Starting values for a house, from what its owner actually knows about it.

Nobody knows their house's heat loss coefficient or its heat capacity. They know its
living area, roughly when it was built or last insulated, whether it is a detached
house or a flat, and whether it has floor heating or radiators. This module turns those
into the numbers the thermal model starts from - the same water-equivalent volume,
surface and loss coefficient a pool starts from - and the calibrator takes over from
there, exactly as it does for a pool.

The tables are rough on purpose. They are a starting point that is close enough for the
first plan, not an energy certificate, and each value was checked against two weeks of
measurements from one house (1960s, massive, renovated, floor heating in screed, 170 m2)
rather than taken from a standard alone:

- what the calibration converged to from both a good and a poor guess, within two to
  three days;
- how far off a 24-hour prediction of the room temperature then was.

What that showed is why the capacity is set heavy. Guessing it too small hurt far more
than guessing it too large: half the fitted value gave 0.6 K of error over a day, one and
a half times it gave 0.2 K. A screed floor and masonry walls hold more heat than the air
temperature suggests, and a model that thinks the house is light expects it to respond
faster than it does.

Pure functions, no state, so the arithmetic can be checked by hand.
"""

from .thermal_physics import WH_PER_M3_PER_K

# Heat stored per kelvin and square metre of living area, Wh/(K m2). Masonry and
# concrete hold roughly twice what a timber frame does; a screed floor with pipes in it
# adds its own layer on top.
CAPACITY_WH_PER_K_M2 = {
    "massive": 90.0,
    "light": 45.0,
}
SCREED_WH_PER_K_M2 = 35.0

# Envelope area per square metre of living area: walls, roof and floor that lose heat to
# the outside or the ground. A detached house has all four walls outside; a flat has
# one or two, and neighbours above and below.
ENVELOPE_PER_M2 = {
    "detached": 2.4,
    "semi_detached": 1.9,
    "terraced": 1.4,
    "apartment": 1.0,
}

# Effective heat loss per square metre of envelope, W/(m2 K). Effective, not the
# transmission value from a certificate: the calibration has no term for the heat that
# people, appliances and the hot water cylinder give off, so that heat ends up netted
# against the losses. These are therefore about half the textbook values, which is also
# what the measured house showed (0.25 against a design value of about 0.45).
LOSS_W_PER_M2_K = {
    "before_1979": 1.0,
    "1979_1994": 0.65,
    "1995_2015": 0.4,
    "renovated": 0.28,
    "new_build": 0.2,
}

# Coefficient of performance at the 26 C reference and its slope, by heat emitter. Floor
# heating runs at about 30-35 C flow, radiators at 45-55 C, and the flow temperature is
# what decides how hard the compressor works. The floor value reproduces the measured
# house (3.4 at 0 C, 4.0 at 7 C, from its heat meter); the radiator value is the same
# curve about a quarter lower.
COP_BY_EMITTER = {
    "floor": (5.6, 0.0155),
    "radiators": (4.2, 0.0155),
}

DEFAULT_INPUTS = {
    "living_area_m2": 150.0,
    "construction": "massive",
    "emitter": "floor",
    "building_form": "detached",
    "insulation": "renovated",
}

# What the derived values are called in a config entry - the thermal model's own keys,
# so a house is configured exactly like every other store once these are filled in.
DERIVED_KEYS = (
    "volume_m3",
    "surface_m2",
    "heat_loss_w_per_m2_k",
    "cop_nominal",
    "cop_air_coeff",
)


def _choice(entry, key, table):
    """The entry's value for *key* if the table knows it, else the default input."""
    value = str(entry.get(key) or "").strip().lower()
    return value if value in table else DEFAULT_INPUTS[key]


def _area(entry):
    try:
        area = float(entry.get("living_area_m2") or 0)
    except (TypeError, ValueError):
        area = 0.0
    return area if area > 0 else DEFAULT_INPUTS["living_area_m2"]


def capacity_wh_per_k(entry):
    """Heat the house stores per kelvin, in Wh/K."""
    per_m2 = CAPACITY_WH_PER_K_M2[_choice(entry, "construction", CAPACITY_WH_PER_K_M2)]
    if _choice(entry, "emitter", COP_BY_EMITTER) == "floor":
        per_m2 += SCREED_WH_PER_K_M2
    return per_m2 * _area(entry)


def starting_values(entry):
    """
    The thermal model's starting values for a house described by *entry*.

    Reads ``living_area_m2``, ``construction``, ``emitter``, ``building_form`` and
    ``insulation``; anything missing or unrecognised falls back to `DEFAULT_INPUTS`.
    Returns the keys in `DERIVED_KEYS`.
    """
    area = _area(entry)
    cop_nominal, cop_air_coeff = COP_BY_EMITTER[
        _choice(entry, "emitter", COP_BY_EMITTER)
    ]
    return {
        # Expressed as water, because that is the unit the thermal model and its
        # calibration already work in - the sauna's water equivalent is the precedent.
        "volume_m3": round(capacity_wh_per_k(entry) / WH_PER_M3_PER_K, 2),
        "surface_m2": round(
            ENVELOPE_PER_M2[_choice(entry, "building_form", ENVELOPE_PER_M2)] * area, 1
        ),
        "heat_loss_w_per_m2_k": LOSS_W_PER_M2_K[
            _choice(entry, "insulation", LOSS_W_PER_M2_K)
        ],
        "cop_nominal": cop_nominal,
        "cop_air_coeff": cop_air_coeff,
    }
