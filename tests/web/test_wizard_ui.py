"""
Browser tests for the Setup Wizard — the first thing a new install shows.

``tests/config_web`` covers what the REST layer does with a payload.  What it cannot
see is whether the user was ever given a way to produce that payload, and on a fresh
install the answer was often no: a step that rendered four fields and showed none of
them, a review page listing a section with no rows, a Finish button that reported
success for a save the server had refused.

These run against ``fresh_page`` — a real first install, wizard already open.
"""

import json

import pytest

from tests.web import wizard_driver as wz


# ----------------------------------------------------------------------
# Shape of the walk
# ----------------------------------------------------------------------


def test_the_wizard_opens_by_itself_on_a_fresh_install(fresh_page):
    """No menu, no click: an empty store is what summons it."""
    assert fresh_page.is_visible(".wizard-container")
    assert wz.current_step_id(fresh_page) == "welcome"


def test_it_hides_the_startup_overlay(fresh_page):
    """The loading overlay sits on top of everything, including the wizard."""
    assert fresh_page.eval_on_selector(
        "#overlay", "e => getComputedStyle(e).display"
    ) == "none"


def test_data_source_is_asked_before_inverter(fresh_page):
    """
    The Home Assistant inverter type needs the connection Data Source establishes.
    Asking for the inverter first meant the answer could not be acted on yet.
    """
    order = []
    while True:
        order.append(wz.current_step_id(fresh_page))
        if order[-1] == "review":
            break
        wz.next_step(fresh_page)

    assert order == wz.STEP_IDS
    assert order.index("data_source") < order.index("inverter")


# ----------------------------------------------------------------------
# Every step has to show the user something
# ----------------------------------------------------------------------


def test_no_step_is_silently_blank(fresh_page):
    """
    The invariant the Inverter step broke: a step either offers a field or says why
    it does not.  It rendered four fields, collapsed every one of them to
    ``max-height: 0``, and left a title above blank space.
    """
    blank = []
    while True:
        step = wz.current_step_id(fresh_page)
        if step not in ("welcome", "review"):
            has_field = bool(wz.visible_fields(fresh_page))
            has_notice = fresh_page.is_visible(".wizard-step-empty")
            if not has_field and not has_notice:
                blank.append(step)
        if step == "review":
            break
        wz.next_step(fresh_page)

    assert not blank, f"steps rendered nothing at all: {blank}"


def test_the_inverter_step_offers_the_type(fresh_page):
    """It was gated on the HA data source, which a fresh install does not use."""
    while wz.current_step_id(fresh_page) != "inverter":
        wz.next_step(fresh_page)

    assert "inverter.type" in wz.visible_fields(fresh_page)
    assert [c for c, _ in wz.choices(fresh_page, "inverter.type")] == [
        "fronius_gen24", "fronius_gen24_legacy", "victron",
        "evcc", "homeassistant", "default",
    ]


def test_the_inverter_step_survives_openhab(fresh_page):
    """The old dependency made this step permanently empty for OpenHAB users."""
    while wz.current_step_id(fresh_page) != "data_source":
        wz.next_step(fresh_page)
    wz.set_field(fresh_page, "data_source.type", "openhab")
    wz.set_field(fresh_page, "data_source.url", "http://openhab.local:8080")
    wz.next_step(fresh_page)

    assert wz.current_step_id(fresh_page) == "inverter"
    assert "inverter.type" in wz.visible_fields(fresh_page)


def test_a_step_with_nothing_visible_says_so(fresh_page):
    """
    No shipped step can currently empty out — every one of them leads with a field
    that has no dependency.  This drives the state directly to keep the notice honest,
    because the way it failed before was silence, not an error.
    """
    while wz.current_step_id(fresh_page) != "inverter":
        wz.next_step(fresh_page)

    # Make the step's only visible field depend on something that is not true.
    fresh_page.evaluate(
        """() => {
            const f = setupWizard.schema.find(x => x.key === 'inverter.type');
            f.depends_on = {'data_source.type': ['nothing-selects-this']};
            setupWizard._render();
        }"""
    )

    assert wz.visible_fields(fresh_page) == []
    assert fresh_page.is_visible(".wizard-step-empty")
    # The heading stays: the user still needs to know which step they are on.
    assert wz.step_title(fresh_page) == "Inverter"


# ----------------------------------------------------------------------
# Review
# ----------------------------------------------------------------------


def test_review_has_no_hollow_sections(fresh_page):
    """
    ``Inverter`` used to appear as a heading with zero rows beneath it, because the
    review counted emitted fields rather than shown ones.
    """
    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)

    sections = wz.review_rows(fresh_page)
    assert sections, "review listed nothing at all"
    empty = [name for name, rows in sections.items() if not rows]
    assert not empty, f"sections with no rows: {empty}"


def test_review_reports_what_was_entered(fresh_page):
    """A summary that does not match the answers is worse than no summary."""
    while wz.current_step_id(fresh_page) != "battery":
        wz.next_step(fresh_page)
    wz.set_field(fresh_page, "battery.capacity_wh", 20000)
    wz.set_field(fresh_page, "battery.min_soc_percentage", 15)

    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)

    battery = wz.review_rows(fresh_page)["Battery"]
    assert battery["Capacity Wh"] == "20000"
    assert battery["Min Soc Percentage"] == "15"


def test_review_omits_fields_the_user_never_saw(fresh_page):
    """
    With the default price source there is no token to show.  Listing the schema
    placeholder would read as a setting the user had chosen.
    """
    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)

    pricing = wz.review_rows(fresh_page)["Pricing"]
    assert "Token" not in pricing


# ----------------------------------------------------------------------
# Finishing — the personas
# ----------------------------------------------------------------------

# Eight first-time-user journeys across the axes the wizard asks about.  Each is
# {step id: {schema key: value}}, plus the steps that persona skips.
PERSONAS = {
    "defaults": ({}, []),
    "home_assistant_fronius_tibber": (
        {
            "data_source": {
                "data_source.type": "homeassistant",
                "data_source.url": "http://homeassistant.local:8123",
                "data_source.access_token": "ha-token",
            },
            "inverter": {
                "inverter.type": "fronius_gen24",
                "inverter.address": "192.168.1.50",
                "inverter.user": "customer",
                "inverter.password": "secret",
            },
            "price": {"price.source": "tibber", "price.token": "tibber-token"},
        },
        ["evcc"],
    ),
    "evcc_everywhere": (
        {
            "evcc": {"evcc.url": "http://evcc.local:7070"},
            "inverter": {"inverter.type": "evcc"},
            "price": {"price.source": "evcc"},
            "pv": {"pv_forecast_source.source": "evcc"},
        },
        [],
    ),
    "openhab_fixed_price": (
        {
            "data_source": {
                "data_source.type": "openhab",
                "data_source.url": "http://openhab.local:8080",
            },
            "price": {"price.source": "fixed_24h"},
            "pv": {"pv_forecast_source.source": "openmeteo"},
        },
        ["evcc"],
    ),
    "external_eos_server": (
        {
            "eos": {"eos.source": "eos_server", "eos.server": "10.0.0.5", "eos.port": 8503},
            "data_source": {
                "data_source.type": "homeassistant",
                "data_source.url": "http://homeassistant.local:8123",
                "data_source.access_token": "ha-token",
            },
            "inverter": {"inverter.type": "homeassistant"},
            "price": {"price.source": "smartenergy_at"},
        },
        ["evcc"],
    ),
    "timeseries_from_home_assistant": (
        {
            "data_source": {
                "data_source.type": "homeassistant",
                "data_source.url": "http://homeassistant.local:8123",
                "data_source.access_token": "ha-token",
            },
            "price": {
                "price.source": "timeseries",
                "price.data_url": "http://homeassistant.local:8123/api/states/sensor.prices",
            },
            "pv": {"pv_forecast_source.source": "default"},
        },
        ["evcc"],
    ),
    "victron": (
        {
            "inverter": {"inverter.type": "victron", "inverter.address": "192.168.1.60"},
            "pv": {"pv_forecast_source.source": "openmeteo_local"},
        },
        ["evcc"],
    ),
    "forecast_solar": (
        {"pv": {"pv_forecast_source.source": "forecast_solar",
                "pv_forecast.lat": 52.5, "pv_forecast.lon": 13.4,
                "pv_forecast.power": 8200}},
        ["evcc"],
    ),
}


def _run_persona(page, name):
    """Walk the whole wizard as *name* would, and press Finish."""
    values, skips = PERSONAS[name]
    while True:
        step = wz.current_step_id(page)
        for key, value in values.get(step, {}).items():
            wz.set_field(page, key, value)
        if step == "review":
            break
        if step in skips and page.is_visible("#wiz-skip"):
            wz.skip_step(page)
        else:
            wz.next_step(page)
    wz.finish(page)


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_every_persona_can_finish(fresh_page, offline_timeseries, persona):
    """
    The headline failure: on a fresh install the final save came back 422 and the
    wizard could not be completed by anybody, whatever they chose.  Two fields no step
    displayed were in the payload — an empty ``data_source.url`` that fails its own URL
    pattern, and ``pv_autoscaling.sensor_entity_id``, which is marked required.
    """
    _run_persona(fresh_page, persona)

    assert wz.finished_successfully(fresh_page), wz.save_error_text(fresh_page)


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_finishing_marks_the_wizard_done(fresh_page, offline_timeseries, persona):
    """Otherwise it reopens on the next page load and the user starts over."""
    _run_persona(fresh_page, persona)

    status = fresh_page.evaluate(
        "() => fetch('api/config/wizard-status').then(r => r.json())"
    )
    assert status["pending"] is False
    assert status["completed"] is True


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_it_stores_nothing_the_user_never_saw(fresh_page, offline_timeseries, fresh_server, persona):
    """
    The payload was every ``getting_started`` field in the schema, not the ones the
    steps had shown.  Sections the wizard does not cover rode along, and so did the
    schema's placeholder secrets for fields that were collapsed at the time.
    """
    _run_persona(fresh_page, persona)
    stored = fresh_server.store.get_all()

    assert "time_zone" not in stored, "a bootstrap key belongs in config.yaml"
    assert not [k for k in stored if k.startswith("pv_autoscaling.")], (
        "no wizard step covers PV auto-scaling"
    )
    assert "tibberBearerToken" not in stored.values()
    assert "abc123" not in stored.values()


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_installations_are_only_stored_for_sources_that_use_them(
    fresh_page, offline_timeseries, fresh_server, persona
):
    """
    Unindexed ``pv_forecast.*`` keys are what the merger turns into a phantom
    installation at 47.5/8.5 — an evcc user would end up with a solar array in
    Switzerland they never configured.
    """
    _run_persona(fresh_page, persona)
    stored = fresh_server.store.get_all()

    unindexed = [
        k for k in stored
        if k.startswith("pv_forecast.") and not k.split(".")[1].isdigit()
    ]
    assert not unindexed, f"template keys reached the store: {unindexed}"


def test_a_source_without_installations_stores_none(fresh_page, fresh_server):
    """EVCC brings its own forecast; there is nothing to place on a map."""
    _run_persona(fresh_page, "evcc_everywhere")
    stored = fresh_server.store.get_all()

    assert not [k for k in stored if k.startswith("pv_forecast.")]
    assert stored["pv_forecast_source.source"] == "evcc"


def test_a_location_source_stores_one_indexed_installation(fresh_page, fresh_server):
    """And a source that does need one gets it at index 0, ready for the merger."""
    _run_persona(fresh_page, "forecast_solar")
    stored = fresh_server.store.get_all()

    assert stored["pv_forecast.0.lat"] == 52.5
    assert stored["pv_forecast.0.lon"] == 13.4
    assert stored["pv_forecast.0.power"] == 8200


def test_the_answers_are_what_gets_stored(fresh_page, fresh_server):
    """The whole point: what the user typed is what the install runs on."""
    _run_persona(fresh_page, "home_assistant_fronius_tibber")
    stored = fresh_server.store.get_all()

    assert stored["data_source.type"] == "homeassistant"
    assert stored["data_source.url"] == "http://homeassistant.local:8123"
    assert stored["data_source.access_token"] == "ha-token"
    assert stored["inverter.type"] == "fronius_gen24"
    assert stored["inverter.address"] == "192.168.1.50"
    assert stored["price.source"] == "tibber"
    assert stored["price.token"] == "tibber-token"


def test_a_skipped_step_is_not_saved(fresh_page, fresh_server):
    """Skipping EVCC means declining it, not accepting the placeholder URL."""
    _run_persona(fresh_page, "openhab_fixed_price")

    assert "evcc.url" not in fresh_server.store.get_all()


def test_going_back_into_a_skipped_step_un_skips_it(fresh_page, fresh_server):
    """Changing your mind has to work, or the answer is silently dropped."""
    while wz.current_step_id(fresh_page) != "evcc":
        wz.next_step(fresh_page)
    wz.skip_step(fresh_page)
    wz.back_step(fresh_page)
    wz.set_field(fresh_page, "evcc.url", "http://evcc.local:7070")
    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)
    wz.finish(fresh_page)

    assert fresh_server.store.get("evcc.url") == "http://evcc.local:7070"


# ----------------------------------------------------------------------
# A refused save has to look refused
# ----------------------------------------------------------------------


def _refuse_the_save(page, body=None):
    """
    Answer the wizard's PUT the way the server does when a dependency is unmet.

    Every route into this from the UI is closed — all three selects that can choose
    EVCC disable the option until a URL is configured, and required fields are caught
    at the step they are on. That is the right design, and it leaves the response
    handler with no way to be exercised through the UI, so the response is supplied
    here instead. The shape is what ``update_config`` really returns.
    """
    payload = body or {
        "success": False,
        "unmet_dependencies": [{
            "field": "price.source",
            "reason": "EVCC selected as price source but EVCC URL is not configured",
            "requires": "evcc.url",
            "blocking": True,
        }],
        "message": "Cannot save: required dependencies not configured",
    }
    page.route(
        "**/api/config/",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        ) if route.request.method == "PUT" else route.continue_(),
    )


def test_a_refused_save_does_not_claim_success(fresh_page):
    """
    The server answers an unmet dependency with HTTP 200 and ``success: false``.
    Checking only ``res.ok`` showed "Setup Complete!" over a configuration that was
    never stored.
    """
    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)
    _refuse_the_save(fresh_page)
    wz.finish(fresh_page)

    assert not wz.finished_successfully(fresh_page)


def test_the_refusal_says_which_field_is_at_fault(fresh_page):
    """"Save failed: 200" told the user nothing they could act on."""
    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)
    _refuse_the_save(fresh_page)
    wz.finish(fresh_page)

    assert "EVCC URL is not configured" in wz.save_error_text(fresh_page)


def test_a_refused_save_leaves_the_wizard_pending(fresh_page):
    """
    Nothing was stored, so the setup is not done. Marking it complete anyway would
    mean the wizard never reappears and the user has to find the Settings page.
    """
    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)
    _refuse_the_save(fresh_page)
    wz.finish(fresh_page)

    status = fresh_page.evaluate(
        "() => fetch('api/config/wizard-status').then(r => r.json())"
    )
    assert status["pending"] is True


def test_a_field_level_rejection_names_the_field(fresh_page):
    """A 422 carries per-key errors; they were collapsed to "Save failed: 422"."""
    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)
    fresh_page.route(
        "**/api/config/",
        lambda route: route.fulfill(
            status=422,
            content_type="application/json",
            body=json.dumps({"errors": [
                {"key": "battery.capacity_wh", "error": "Must be at least 100"},
            ]}),
        ) if route.request.method == "PUT" else route.continue_(),
    )
    wz.finish(fresh_page)

    message = wz.save_error_text(fresh_page)
    assert "Capacity Wh" in message
    assert "Must be at least 100" in message


def test_the_finish_button_comes_back_after_a_failure(fresh_page):
    """A dead Saving… button would strand the user on the last step."""
    while wz.current_step_id(fresh_page) != "review":
        wz.next_step(fresh_page)
    _refuse_the_save(fresh_page)
    wz.finish(fresh_page)

    assert fresh_page.is_enabled("#wiz-next")
    assert "Finish" in fresh_page.inner_text("#wiz-next")


# ----------------------------------------------------------------------
# Required fields
# ----------------------------------------------------------------------


def test_solcast_asks_for_the_resource_id(fresh_page):
    """
    A Solcast install cannot be saved without one — the API refuses it. The field was
    "standard" level, which the wizard does not render, so those users answered every
    question they were given and were then told about one they were not.
    """
    while wz.current_step_id(fresh_page) != "pv":
        wz.next_step(fresh_page)
    wz.set_field(fresh_page, "pv_forecast_source.source", "solcast")

    assert "pv_forecast_source.resource_id" in wz.visible_fields(fresh_page)


def test_it_will_not_move_on_without_a_required_answer(fresh_page):
    """The asterisk has to mean something."""
    while wz.current_step_id(fresh_page) != "pv":
        wz.next_step(fresh_page)
    wz.set_field(fresh_page, "pv_forecast_source.source", "solcast")
    wz.set_field(fresh_page, "pv_forecast_source.api_key", "an-api-key")
    wz.set_field(fresh_page, "pv_forecast_source.resource_id", "")

    assert wz.next_step_expecting_block(fresh_page)
    assert wz.field_error(fresh_page, "pv_forecast_source.resource_id") == (
        "This field is required"
    )


def test_a_solcast_install_completes_once_it_is_answered(fresh_page, fresh_server):
    """And then it goes through, rather than failing at the last step."""
    while wz.current_step_id(fresh_page) != "pv":
        wz.next_step(fresh_page)
    wz.set_field(fresh_page, "pv_forecast_source.source", "solcast")
    wz.set_field(fresh_page, "pv_forecast_source.api_key", "an-api-key")
    wz.set_field(fresh_page, "pv_forecast_source.resource_id", "site-1,site-2")
    wz.next_step(fresh_page)
    wz.finish(fresh_page)

    assert wz.finished_successfully(fresh_page), wz.save_error_text(fresh_page)
    assert fresh_server.store.get("pv_forecast_source.resource_id") == "site-1,site-2"
    assert not [k for k in fresh_server.store.get_all() if k.startswith("pv_forecast.")]


def test_an_optional_empty_field_is_not_treated_as_malformed(fresh_page):
    """
    Home Assistant's token has a pattern-free schema entry and may legitimately be
    left blank on a hosted instance; blank must not read as "invalid format".
    """
    while wz.current_step_id(fresh_page) != "data_source":
        wz.next_step(fresh_page)
    wz.set_field(fresh_page, "data_source.type", "homeassistant")
    wz.set_field(fresh_page, "data_source.url", "http://homeassistant.local:8123")
    wz.set_field(fresh_page, "data_source.access_token", "")
    wz.next_step(fresh_page)

    assert wz.current_step_id(fresh_page) == "inverter"
