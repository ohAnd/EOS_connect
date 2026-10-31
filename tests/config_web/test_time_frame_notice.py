"""
Quarter-hourly planning and the one optimizer that cannot do it.

``eos.time_frame`` offers 900 and 3600 whatever the optimizer is, but the
Akkudoktor EOS server plans in hourly slots only: EOS Connect drives it through
``/optimize``, whose GENETIC0 algorithm exposes ``interval_sec`` as a computed
constant of 3600 - no setting changes it. The form used to accept 900 there and
``eos_connect.py`` dropped it back to 3600 at startup with nothing but a log
line, so the setting looked as if it had reverted on its own (issue #353).

The notice has to reach the form, and the startup fallback has to keep naming
the server as the reason.
"""

import re
from pathlib import Path

from src.config_web.api import _conditions_met
from src.config_web.schema import ConfigSchema

SCHEMA = ConfigSchema()
EOS_CONNECT = Path(__file__).resolve().parents[2] / "src" / "eos_connect.py"


def _notice_shows_for(source):
    """Whether the quarter-hourly notice is displayed for an optimizer source."""
    field = SCHEMA.get("eos.time_frame")
    assert field.notice_when, "eos.time_frame carries no notice"
    return _conditions_met(
        field.notice_when["when"], {"eos.source": source}, {}, "eos.time_frame"
    )


def test_both_resolutions_stay_selectable():
    """The choices are not narrowed - 900 is valid config, just not everywhere."""
    assert SCHEMA.get("eos.time_frame").validation["choices"] == [900, 3600]


def test_the_notice_shows_for_the_eos_server():
    """The one source where 900 does not survive a restart."""
    assert _notice_shows_for("eos_server") is True


def test_it_stays_out_of_the_way_for_the_others():
    """EVopt and Local EVopt plan quarter-hourly - no warning belongs there."""
    for source in ("evopt", "local_evopt"):
        assert _notice_shows_for(source) is False, source


def test_the_notice_names_the_server_and_the_way_out():
    """A notice that only says "no" leaves the reader guessing."""
    text = SCHEMA.get("eos.time_frame").notice_when["text"]
    assert "EOS server" in text
    assert "hourly" in text
    assert "EVopt" in text, "say which optimizer does support it"


def test_the_startup_fallback_blames_the_server_not_the_setting():
    """
    The log is the only place a user running on config.yaml sees this, so it
    has to carry the reason too - not just "defaulting to 3600".
    """
    source = EOS_CONNECT.read_text(encoding="utf-8")
    block = re.search(
        r"elif time_frame_base == 900 and eos_source not in.*?time_frame_base = 3600",
        source,
        re.DOTALL,
    )
    assert block, "the 15-min fallback is gone - update this test with it"
    warning = block.group(0)
    assert "Akkudoktor EOS" in warning, "name the optimizer that cannot do it"
    assert "hourly" in warning
    assert "EVopt" in warning, "name the optimizers that can"
