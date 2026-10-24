"""
What an EVCC-managed field looks like, and when it stops being one.

With EVCC as the data source, the names EOS Connect reads are EVCC's own and
the user has nothing to type: those fields read out instead of asking, with a
"from EVCC" badge. The value shown is derived per merge and never stored, so it
arrives over ``/api/config/resolved`` rather than in the exported settings.

The part worth a browser test is the *exception*. EVCC reports a battery's SoC
limits only where the integration implements the capability for it, so "EVCC
manages this" is a fact about the site, not about the field. Rendering the row
read-only regardless left a field the user had to fill and could not.
"""

import json
import re

import pytest

import tests.web.wizard_driver as wz  # noqa: F401  (keeps the shared skip behaviour)

EVCC_SETTINGS = {
    "data_source.type": "evcc",
    "data_source.url": "http://evcc.local:7070",
    # The sample config pins these; clear them so the sections inherit EVCC.
    "load.source": "default",
    "battery.source": "default",
}


def _css_key(key):
    return key.replace(".", "-")


def _open_evcc_config(page, server, monkeypatch, parameters=None):
    """Put the site on EVCC, decide what EVCC answers, open the overlay.

    The server runs in this process, so patching the module attribute reaches
    the merge the page is about to trigger. Without it the merge would really
    try to reach ``evcc.local``.
    """
    monkeypatch.setattr(
        "src.interfaces.evcc_parameters.fetch_evcc_parameters",
        lambda *args, **kwargs: parameters,
    )
    server.store.set_batch(EVCC_SETTINGS)
    page.evaluate("() => showConfigurationMenu()")
    page.wait_for_selector(".config-field")
    return page


@pytest.fixture(name="evcc_page")
def evcc_page_fixture(page, server, monkeypatch):
    """The config overlay on a site whose battery reports no SoC limits."""
    return _open_evcc_config(page, server, monkeypatch)


def _field(page, key):
    return page.evaluate(
        """(key) => {
            const el = document.querySelector(`#cfg-field-${key}`);
            if (!el) { return null; }
            const input = el.querySelector('input');
            return {
                text: el.innerText,
                readonly: input ? input.hasAttribute('readonly') : null,
                value: input ? input.value : null,
            };
        }""",
        _css_key(key),
    )


def _select(page, section):
    page.evaluate(f"() => configurationManager._selectSection('{section}')")
    page.wait_for_selector(f'[data-key^="{section}."]')


def test_a_derived_sensor_reads_out_instead_of_asking(evcc_page):
    """group:home is EVCC's household meter; there is nothing to type."""
    _select(evcc_page, "load")
    field = _field(evcc_page, "load.load_sensor")

    assert field is not None, "the load sensor field is rendered"
    assert field["readonly"] is True
    assert field["value"] == "group:home"
    assert "from EVCC" in field["text"]


def test_the_derived_value_never_lands_in_the_stored_settings(evcc_page):
    """It is derived per merge, so the export - and the backup - stay clean.

    The store still holds the Home Assistant name this site was set up with;
    the EVCC reading is shown over it without replacing it. Writing it down
    was what pinned stale values and put derived data into backups.
    """
    exported = json.loads(
        evcc_page.evaluate(
            "async () => JSON.stringify(await (await fetch('api/config/export')).json())"
        )
    )
    resolved = json.loads(
        evcc_page.evaluate(
            "async () => JSON.stringify(await (await fetch('api/config/resolved')).json())"
        )
    )
    assert resolved["load.load_sensor"] == "group:home"
    assert exported["load.load_sensor"] != resolved["load.load_sensor"]
    assert exported["load.load_sensor"] == "sensor.power", "the stored value is intact"


def test_a_field_evcc_cannot_answer_stays_an_input(evcc_page):
    """The battery here reports no SoC limits, so the range is the user's to set.

    This is the gate: without it the row rendered read-only and said "not
    provided by EVCC" at a field that is required to have a value.
    """
    _select(evcc_page, "battery")
    field = _field(evcc_page, "battery.min_soc_percentage")

    assert field is not None
    assert field["readonly"] is False
    assert "from EVCC" not in field["text"]


def test_the_evcc_page_steps_down_while_it_is_the_data_source(evcc_page):
    """One address, one input. Its only field is the URL, typed on Data Source.

    The section then has nothing left to answer and drops out of the nav
    entirely, rather than offering an empty page.
    """
    sections = evcc_page.evaluate(
        "() => Array.from(document.querySelectorAll('.config-nav-item'))"
        ".map(el => el.textContent.trim())"
    )
    assert sections, "the nav rendered"
    assert not any(s.lower() == "evcc" for s in sections), sections


def test_the_same_field_reads_out_once_evcc_reports_its_limits(
    page, server, monkeypatch
):
    """The counterpart: a battery that does report them hands them over.

    Same field, opposite rendering - which is the whole point of gating on
    whether a value was actually resolved rather than on the field's label.
    """
    _open_evcc_config(
        page,
        server,
        monkeypatch,
        parameters={"min_soc_percentage": 12, "max_soc_percentage": 96},
    )
    _select(page, "battery")
    field = _field(page, "battery.min_soc_percentage")

    assert field is not None
    assert field["readonly"] is True
    assert field["value"] == "12"
    assert "from EVCC" in field["text"]


def test_the_wizard_applies_the_same_negative_dependency(page):
    """``"!evcc"`` is implemented three times and has to mean one thing.

    The settings page, the setup wizard and ``api._dependency_met`` each parse
    ``depends_on`` themselves. The wizard's copy is the one no test reached: a
    user setting the site up from scratch would have been asked for the EVCC
    address twice, on two different steps.
    """
    page.evaluate("() => showSetupWizard()")
    page.wait_for_function("typeof setupWizard !== 'undefined' && !!setupWizard")

    verdicts = page.evaluate("""() => {
            const field = {depends_on: {"data_source.type": "!evcc"}};
            const notEmpty = {depends_on: {"load.additional_load_1_sensor": "!empty"}};
            const out = {};
            setupWizard.values["data_source.type"] = "evcc";
            out.hiddenOnEvcc = setupWizard._isDependencyMet(field);
            setupWizard.values["data_source.type"] = "homeassistant";
            out.shownOnHomeAssistant = setupWizard._isDependencyMet(field);
            // "!empty" keeps its own meaning, not "anything but 'empty'".
            setupWizard.values["load.additional_load_1_sensor"] = "";
            out.notEmptyRejectsBlank = setupWizard._isDependencyMet(notEmpty);
            setupWizard.values["load.additional_load_1_sensor"] = "group:consumer:X";
            out.notEmptyAcceptsValue = setupWizard._isDependencyMet(notEmpty);
            return out;
        }""")

    assert verdicts == {
        "hiddenOnEvcc": False,
        "shownOnHomeAssistant": True,
        "notEmptyRejectsBlank": False,
        "notEmptyAcceptsValue": True,
    }


def _computed(page, selector, prop):
    return page.evaluate(
        """([sel, prop]) => {
            const el = document.querySelector(sel);
            return el ? getComputedStyle(el).getPropertyValue(prop) : null;
        }""",
        [selector, prop],
    )


def test_a_derived_field_is_styled_by_the_theme_not_the_browser(evcc_page):
    """It rendered without the .config-input base, so it came out a white box.

    The class is the contract, but a class with no rule behind it would satisfy
    that and still render light - hence the computed colour. Dark rather than an
    exact value: the palette is a design decision, "not the browser default" is
    the regression.
    """
    _select(evcc_page, "load")
    sel = f"#cfg-field-{_css_key('load.load_sensor')} input"

    classes = evcc_page.evaluate("(sel) => document.querySelector(sel).className", sel)
    assert "config-input" in classes
    assert "config-input-readonly" in classes

    background = _computed(evcc_page, sel, "background-color")
    channels = [int(n) for n in re.findall(r"\d+", background)[:3]]
    assert channels, f"no colour parsed from {background!r}"
    assert all(c < 100 for c in channels), f"{background} is not a dark ground"


def test_the_badge_uses_the_accent_colour_class(evcc_page):
    """Same reason: the badge carried its colours inline instead of a class."""
    _select(evcc_page, "load")
    badge = evcc_page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            return el ? el.className : null;
        }""",
        f"#cfg-field-{_css_key('load.load_sensor')} .config-badge",
    )
    assert badge is not None, "the from-EVCC badge is rendered"
    assert "config-badge-evcc" in badge
