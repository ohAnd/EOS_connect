"""
Browser tests for the Backup & Restore panel.

The REST layer is covered in ``tests/config_web/test_backup.py``.  What is checked here
is the part no API test can see: that the panel opens from the menu, that a restore
really does show a preview before it writes anything, and that the warnings the user is
supposed to read are actually on screen.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest


def _seed_hours(server, days_ago=1, hours=(8, 12)):
    """Record measured hours so the panel has history to talk about."""
    base = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for hour in hours:
        when = base.replace(hour=hour, minute=0, second=0, microsecond=0)
        server.pv_store.insert_hourly_record(
            timestamp=when.isoformat(),
            date=when.strftime("%Y-%m-%d"),
            hour=hour,
            timeframe_id=(hour // 6) + 1,
            real_counter_kwh=1042.5,
            real_delta_kwh=0.83,
            forecast_kwh=0.91,
            local_date=when.strftime("%Y-%m-%d"),
            local_hour=hour,
            local_offset_minutes=0,
        )


def _open_menu(page):
    """
    Open the main dropdown.

    Called directly rather than by clicking the header icon: that icon's listener is
    attached while the dashboard boots from real optimizer output, which this harness
    does not serve.  The dropdown itself is the real one from ``ui.js`` either way,
    which is where the menu entry under test lives.
    """
    page.evaluate("() => showMainMenu('1.2.3', 'local_evopt', 3600)")
    page.wait_for_selector("#main-dropdown-menu")


def _open_panel(page):
    """Open Backup & Restore the way a user does: from the main menu."""
    _open_menu(page)
    page.click("#main-dropdown-menu span:text-is('Backup & Restore')")
    page.wait_for_selector("text=Create a backup")


def _write_backup(tmp_path, page, name="backup.json"):
    """Fetch a backup through the running app and drop it on disk for the file input."""
    data = page.evaluate("() => fetch('api/backup/export').then(r => r.json())")
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path, data


def _choose_file(page, path):
    page.set_input_files("#backup-restore-file", str(path))
    page.wait_for_selector("text=Review before restoring")


# ----------------------------------------------------------------------
# Reaching the feature
# ----------------------------------------------------------------------


def test_menu_offers_backup_and_restore(page):
    """It has to be findable where the other tools live."""
    _open_menu(page)

    assert page.is_visible("#main-dropdown-menu span:text-is('Backup & Restore')")
    # Sits with Configuration rather than off among the external links.
    entries = page.eval_on_selector_all(
        "#main-dropdown-menu span", "els => els.map(e => e.textContent.trim())"
    )
    assert entries.index("Backup & Restore") == entries.index("Configuration") + 1


def test_panel_opens_from_the_menu(page):
    _open_panel(page)

    assert page.is_visible("text=Create a backup")
    assert page.is_visible("text=Restore from a backup")


def test_panel_reports_what_the_install_holds(server, page):
    _seed_hours(server, hours=(8, 12, 16))
    _open_panel(page)

    body = page.inner_text("#full_screen_content")
    assert "3 hours" in body
    assert "settings" in body


def test_panel_warns_that_the_file_holds_secrets(page):
    """The warning is the whole reason the export is allowed to be unmasked."""
    _open_panel(page)

    body = page.inner_text("#full_screen_content")
    assert "plain text" in body
    assert "password" in body.lower()


def test_history_shows_as_unavailable_when_there_is_none(page):
    _open_panel(page)

    assert "nothing recorded yet" in page.inner_text("#full_screen_content")


# ----------------------------------------------------------------------
# Backing up
# ----------------------------------------------------------------------


def test_download_produces_a_backup_file(server, page):
    _seed_hours(server)
    _open_panel(page)

    with page.expect_download() as info:
        page.click("text=Download backup")
    download = info.value

    assert download.suggested_filename.startswith("eos_connect_backup_")
    assert download.suggested_filename.endswith(".json")


def test_downloaded_file_carries_both_datasets(server, page, tmp_path):
    _seed_hours(server)
    _open_panel(page)

    with page.expect_download() as info:
        page.click("text=Download backup")
    path = tmp_path / "downloaded.json"
    info.value.save_as(str(path))
    data = json.loads(path.read_text())

    assert data["_format"] == "eos-connect-backup"
    assert data["battery.capacity_wh"] == 10000
    assert len(data["pv_yield_history"]) == 2
    assert "real_counter_kwh" not in data["pv_yield_history"][0]


def test_deselecting_a_dataset_leaves_it_out_of_the_download(server, page, tmp_path):
    _seed_hours(server)
    _open_panel(page)
    page.uncheck("#ds-backup-pv_yield_history")

    with page.expect_download() as info:
        page.click("text=Download backup")
    path = tmp_path / "settings_only.json"
    info.value.save_as(str(path))
    data = json.loads(path.read_text())

    assert data["_datasets"] == ["settings"]
    assert "pv_yield_history" not in data


# ----------------------------------------------------------------------
# Restoring — the preview is the point
# ----------------------------------------------------------------------


def test_choosing_a_file_previews_instead_of_applying(server, page, tmp_path):
    """Nothing may reach the database before the user confirms."""
    path, _ = _write_backup(tmp_path, page)
    server.store.set("battery.capacity_wh", 99999)

    _open_panel(page)
    _choose_file(page, path)

    assert page.is_visible("text=Restore now")
    assert server.store.get("battery.capacity_wh") == 99999


def test_preview_names_the_settings_it_would_remove(server, page, tmp_path):
    """A restore that silently dropped settings would be the worst kind of surprise."""
    _, data = _write_backup(tmp_path, page)
    del data["eos.port"]
    path = tmp_path / "missing_key.json"
    path.write_text(json.dumps(data))

    _open_panel(page)
    _choose_file(page, path)

    body = page.inner_text("#full_screen_content")
    assert "will be removed" in body
    assert "eos.port" in body
    assert server.store.has_key("eos.port")


def test_confirming_applies_the_restore(server, page, tmp_path):
    path, _ = _write_backup(tmp_path, page)
    server.store.set("battery.capacity_wh", 99999)

    _open_panel(page)
    _choose_file(page, path)
    page.click("text=Restore now")
    page.wait_for_selector("text=Restore complete")

    assert server.store.get("battery.capacity_wh") == 10000


def test_restore_result_reports_what_happened(server, page, tmp_path):
    _seed_hours(server)
    path, _ = _write_backup(tmp_path, page)
    server.store.execute("DELETE FROM pv_yield_history")

    _open_panel(page)
    _choose_file(page, path)
    page.click("text=Restore now")
    page.wait_for_selector("text=Restore complete")

    body = page.inner_text("#full_screen_content")
    assert "Yield hours restored" in body
    assert len(server.pv_store.get_all_history()) == 2


def test_cancel_discards_the_pending_restore(server, page, tmp_path):
    path, _ = _write_backup(tmp_path, page)
    server.store.set("battery.capacity_wh", 99999)

    _open_panel(page)
    _choose_file(page, path)
    page.click("text=Cancel")
    page.wait_for_selector("text=Choose backup file…")

    assert server.store.get("battery.capacity_wh") == 99999


def test_merge_mode_keeps_settings_the_file_lacks(server, page, tmp_path):
    _, data = _write_backup(tmp_path, page)
    del data["eos.port"]
    path = tmp_path / "merge.json"
    path.write_text(json.dumps(data))

    _open_panel(page)
    _choose_file(page, path)
    page.check("#backup-mode-merge")
    page.wait_for_function(
        "() => !document.getElementById('full_screen_content')"
        ".innerText.includes('will be removed')"
    )
    page.click("text=Restore now")
    page.wait_for_selector("text=Restore complete")

    assert server.store.has_key("eos.port")


def test_restore_button_is_disabled_when_nothing_is_selected(page, tmp_path):
    """Asking for nothing must not quietly restore everything."""
    path, _ = _write_backup(tmp_path, page)

    _open_panel(page)
    _choose_file(page, path)
    page.uncheck("#ds-restore-settings")
    page.uncheck("#ds-restore-pv_yield_history")
    page.wait_for_selector("text=Nothing is selected")

    assert page.is_disabled("button:has-text('Restore now')")


def test_backup_and_restore_selections_are_independent(server, page, tmp_path):
    """Unticking a dataset under Restore must not change what Download writes."""
    _seed_hours(server)
    path, _ = _write_backup(tmp_path, page)

    _open_panel(page)
    _choose_file(page, path)
    page.uncheck("#ds-restore-pv_yield_history")
    page.wait_for_selector("#ds-restore-pv_yield_history:not(:checked)")

    page.click("text=Cancel")
    page.wait_for_selector("text=Choose backup file…")

    assert page.is_checked("#ds-backup-pv_yield_history")
    assert page.evaluate("() => backupManager.forRestore.pv_yield_history") is False
    assert page.evaluate("() => backupManager.forBackup.pv_yield_history") is True


def test_preview_replaces_the_backup_card(server, page, tmp_path):
    """Mid-restore the Confirm button must not sit below an irrelevant card."""
    path, _ = _write_backup(tmp_path, page)

    _open_panel(page)
    assert page.is_visible("text=Create a backup")

    _choose_file(page, path)

    assert not page.is_visible("text=Create a backup")
    assert page.is_visible("button:has-text('Restore now')")


# ----------------------------------------------------------------------
# Time-shifted history
# ----------------------------------------------------------------------


def _stale_backup(page, tmp_path, days_ago=25):
    """A backup whose history predates the retention window."""
    base = datetime.now(timezone.utc) - timedelta(days=days_ago)
    rows = []
    for hour in (8, 12, 16):
        when = base.replace(hour=hour, minute=0, second=0, microsecond=0)
        rows.append(
            {
                "timestamp": when.isoformat(),
                "date": when.strftime("%Y-%m-%d"),
                "hour": hour,
                "timeframe_id": (hour // 6) + 1,
                "real_delta_kwh": 0.83,
                "forecast_kwh": 0.91,
                "local_date": when.strftime("%Y-%m-%d"),
                "local_hour": hour,
                "local_offset_minutes": 0,
            }
        )
    data = page.evaluate("() => fetch('api/backup/export').then(r => r.json())")
    data["pv_yield_history"] = rows
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(data))
    return path


def test_stale_backup_preselects_time_shifting(page, tmp_path):
    path = _stale_backup(page, tmp_path)

    _open_panel(page)
    _choose_file(page, path)

    assert page.is_checked("#backup-history-seed")
    body = page.inner_text("#full_screen_content")
    assert "recommended" in body
    assert "past the" in body  # the explanation of why it is offered


def test_fresh_backup_does_not_preselect_time_shifting(server, page, tmp_path):
    _seed_hours(server)
    path, _ = _write_backup(tmp_path, page)

    _open_panel(page)
    _choose_file(page, path)

    assert page.is_checked("#backup-history-as-is")
    assert "Not recommended for this file" in page.inner_text("#full_screen_content")


def test_seeded_restore_marks_the_rows(server, page, tmp_path):
    path = _stale_backup(page, tmp_path)

    _open_panel(page)
    _choose_file(page, path)
    page.click("text=Restore now")
    page.wait_for_selector("text=Restore complete")

    body = page.inner_text("#full_screen_content")
    assert "History shifted" in body
    stored = server.pv_store.get_all_history()
    assert len(stored) == 3
    assert {row["origin"] for row in stored} == {"seeded"}
    assert all(row["real_counter_kwh"] is None for row in stored)


# ----------------------------------------------------------------------
# Config panel stays config-scoped
# ----------------------------------------------------------------------


def test_config_import_says_it_ignored_the_yield_history(server, page, tmp_path):
    """A full backup dropped into the config importer must not lose it silently."""
    _seed_hours(server)
    result = page.evaluate(
        """() => fetch('api/backup/export')
            .then(r => r.json())
            .then(b => fetch('api/config/import', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(b),
            }))
            .then(r => r.json())"""
    )

    assert result["ignored_datasets"] == ["pv_yield_history"]
    assert result["imported"] > 0


def test_preview_explains_hours_it_will_not_write(server, page, tmp_path):
    """"2 of 6 hours" has to say where the other four went."""
    _seed_hours(server, days_ago=1, hours=(7, 8, 9, 10, 11, 12))
    path = _stale_backup(page, tmp_path)

    _open_panel(page)
    _choose_file(page, path)

    body = page.inner_text("#full_screen_content")
    assert "Hours already measured here" in body
    assert "left untouched" in body


# ----------------------------------------------------------------------
# Layout and responsiveness
# ----------------------------------------------------------------------

# Wide enough for two columns, and a phone in portrait. isMobile() switches at 768.
DESKTOP = {"width": 1920, "height": 1080}
PHONE = {"width": 390, "height": 844}


def test_cards_sit_side_by_side_on_a_wide_screen(server, page):
    """A narrow centred column in a 1920px overlay is not how the other panels look."""
    _seed_hours(server)
    page.set_viewport_size(DESKTOP)
    _open_panel(page)

    backup = page.eval_on_selector("#ds-backup-settings", "e => e.getBoundingClientRect()")
    restore_btn = page.eval_on_selector(
        "#backup-restore-file + button", "e => e.getBoundingClientRect()"
    )

    # The restore action starts to the right of the backup action, not beneath it.
    assert restore_btn["left"] > backup["left"] + 300
    assert abs(restore_btn["top"] - backup["top"]) < 120


def test_cards_stack_on_a_phone(server, page):
    _seed_hours(server)
    page.set_viewport_size(PHONE)
    _open_panel(page)

    backup = page.eval_on_selector("#ds-backup-settings", "e => e.getBoundingClientRect()")
    restore_btn = page.eval_on_selector(
        "#backup-restore-file + button", "e => e.getBoundingClientRect()"
    )

    assert restore_btn["top"] > backup["top"] + 100
    assert abs(restore_btn["left"] - backup["left"]) < 40


def test_panel_uses_the_full_overlay_width(server, page):
    """The other overlays fill the width; a centred 820px column looked out of place."""
    _seed_hours(server)
    page.set_viewport_size(DESKTOP)
    _open_panel(page)

    content = page.eval_on_selector("#full_screen_content", "e => e.clientWidth")
    widest = page.eval_on_selector_all(
        "#full_screen_content > *", "els => Math.max(...els.map(e => e.offsetWidth))"
    )

    assert widest > content * 0.9


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE, {"width": 820, "height": 1180}])
def test_nothing_overflows_horizontally(server, page, tmp_path, viewport):
    """Guards the squeeze that pushed the dataset labels into a two-character ribbon."""
    _seed_hours(server)
    path, _ = _write_backup(tmp_path, page)
    page.set_viewport_size(viewport)

    _open_panel(page)
    assert page.eval_on_selector(
        "#full_screen_content", "e => e.scrollWidth <= e.clientWidth + 1"
    )

    _choose_file(page, path)
    assert page.eval_on_selector(
        "#full_screen_content", "e => e.scrollWidth <= e.clientWidth + 1"
    )


def test_explainer_is_present_when_idle_and_hidden_mid_restore(server, page, tmp_path):
    """It fills the desktop dead space, but must not compete with the review."""
    path, _ = _write_backup(tmp_path, page)
    page.set_viewport_size(DESKTOP)

    _open_panel(page)
    assert "How it works:" in page.inner_text("#full_screen_content")

    _choose_file(page, path)
    assert "How it works:" not in page.inner_text("#full_screen_content")


def test_result_counts_are_laid_out_as_a_row_on_a_wide_screen(server, page, tmp_path):
    _seed_hours(server)
    path, _ = _write_backup(tmp_path, page)
    server.store.execute("DELETE FROM pv_yield_history")
    page.set_viewport_size(DESKTOP)

    _open_panel(page)
    _choose_file(page, path)
    page.click("text=Restore now")
    page.wait_for_selector("text=Restore complete")

    tops = page.eval_on_selector_all(
        "#full_screen_content div:has(> div + div)",
        """els => els
            .filter(e => /^\\d/.test(
                e.querySelectorAll(':scope > div')[1]?.textContent?.trim() || ''))
            .map(e => e.offsetTop)""",
    )
    assert len(tops) >= 2
    assert len(set(tops)) == 1, "count tiles should share one row"
