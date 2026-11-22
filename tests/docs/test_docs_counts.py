"""Numbers the documentation states about the code, held against the code.

Every other docs test guards something an edit can break. These guard the
opposite: sentences nobody edited, which a change somewhere else made untrue.

They all drifted that way at once. The schema grew from 173 fields to 198 and
from 12 sections to 13; the settings needing a restart went from sixty-six to
ninety-five; space heating became the seventh appliance type while the prose
still counted six. No test could notice, because nothing about those sentences
had changed - only the code they describe.

So: a number about the code may appear in the docs only if it is pinned here.
Otherwise leave the number out and name the command that prints it.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
SCHEMA_JSON = DOCS / "assets/data/config_schema.json"

_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def spelled_number(word):
    """``"Ninety-five"`` -> 95, so the prose may keep writing numbers as words.

    Only what the docs actually use: units, teens, tens, and a tens-unit pair.
    Anything else raises rather than guessing, because a silently wrong parse
    would turn this whole module into a test that always passes.
    """
    parts = word.strip().lower().replace("–", "-").split("-")
    if len(parts) == 1:
        single = parts[0]
        if single in _UNITS:
            return _UNITS[single]
        if single in _TENS:
            return _TENS[single]
    elif len(parts) == 2 and parts[0] in _TENS and parts[1] in _UNITS:
        return _TENS[parts[0]] + _UNITS[parts[1]]
    raise ValueError(f"cannot read {word!r} as a number")


@pytest.fixture(name="schema", scope="module")
def _schema():
    return json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))


def _text(relative):
    return (REPO / relative).read_text(encoding="utf-8")


def _claim(relative, pattern):
    """The one capture of *pattern* in that file, or a failure naming the file."""
    found = re.findall(pattern, _text(relative), re.S)
    assert len(found) == 1, (
        f"{relative}: expected exactly one sentence matching {pattern!r}, "
        f"found {len(found)}. The wording moved - move this test with it."
    )
    return found[0]


# ---------------------------------------------------------------- the schema


def test_the_field_and_section_count_match_the_schema(schema):
    """`src/config_web/schema.py` is the single point of truth, and says so."""
    fields, sections = _claim(
        "docs/developer/index.html",
        r"It holds (\d+) <code>FieldDef</code> entries across (\d+)\s",
    )
    assert int(fields) == len(schema["fields"])
    assert int(sections) == len(schema["sections"])


def test_every_field_having_a_help_url_is_counted_right(schema):
    """The claim is that all of them have one; the number says how many that is."""
    claimed = _claim("docs/developer/index.html", r"All (\d+) fields have one\.")
    assert int(claimed) == len(schema["fields"])
    assert all(entry.get("help_url") for entry in schema["fields"])


def test_the_hot_reload_count_matches_the_schema(schema):
    """A field gaining hot reload moves a number on a page nobody opened."""
    claimed = _claim("docs/developer/index.html", r"True for (\d+) fields\.")
    assert int(claimed) == sum(
        1 for entry in schema["fields"] if entry.get("hot_reload") is True
    )


def test_the_quoted_export_output_matches_what_the_script_prints(schema):
    """The page shows the script's own line; it has to be the line it prints."""
    fields, presets = _claim(
        "docs/developer/index.html",
        r"# Exported (\d+) fields and (\d+) managed-load presets",
    )
    assert int(fields) == len(schema["fields"])
    assert int(presets) == len(schema["managed_load_presets"])


def test_the_settings_reference_counts_every_field(schema):
    """The reference page renders the schema, so its own count has to match."""
    claimed = _claim(
        "docs/user-guide/configuration.html", r"All (\d+) settings in one list"
    )
    assert int(claimed) == len(schema["fields"])


# ------------------------------------------------------- settings and restart


def test_the_restart_count_matches_the_labelled_fields(schema):
    """Stated twice, in the user guide and in the module that acts on it.

    Both are prose nobody touches when a field gains the label, which is how
    they sat at sixty-six while the schema had grown to ninety-five.
    """
    expected = sum(
        1
        for entry in schema["fields"]
        if "restart_required" in (entry.get("labels") or [])
    )
    in_guide = _claim(
        "docs/user-guide/configuration.html", r"([A-Za-z-]+) settings need a restart"
    )
    in_module = _claim(
        "src/restart.py", r"([A-Za-z-]+) settings are marked ``restart_required``"
    )
    assert spelled_number(in_guide) == expected
    assert spelled_number(in_module) == expected


# ------------------------------------------------------------ appliance types


def test_the_appliance_type_counts_match_the_presets(schema):
    """Seven types, five of them the same heated-store model.

    Space heating arrived as the seventh and the fifth, and three sentences
    went on counting six and four.
    """
    types = len(schema["managed_load_presets"])
    stores = len(schema["managed_load_groups"]["thermal"])

    page = "docs/user-guide/config-managed-loads.html"
    not_separate, of_the = _claim(
        page, r"It is not ([a-z]+) separate implementations: ([a-z]+) of the"
    )
    assert spelled_number(not_separate) == types
    assert spelled_number(of_the) == stores

    shared = _claim(page, r"a field ([a-z]+) appliance types share")
    assert spelled_number(shared) == stores

    scheduled = _claim(page, r"the ([a-z]+) heated stores and the")
    assert spelled_number(scheduled) == stores
