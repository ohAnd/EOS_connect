"""
Browser tests for the Setup Wizard — the first thing a new install shows.

``tests/config_web`` covers what the REST layer does with a payload.  What it cannot
see is whether the user was ever given a way to produce that payload, and on a fresh
install the answer was often no: a step that rendered four fields and showed none of
them, a review page listing a section with no rows, a Finish button that reported
success for a save the server had refused.

These run against ``fresh_page`` — a real first install, wizard already open.
"""

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
