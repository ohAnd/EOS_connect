# pylint: disable=redefined-outer-name,protected-access,unused-argument
# pylint: disable=too-few-public-methods
"""
Tests for the data-directory persistence check.

A standalone Docker container without a volume on /app/data loses the settings
database on every recreate, then resurrects pre-database values from the config.yaml
that *did* survive (#287).  These tests pin the classifier that detects it, and in
particular that it fails closed: anything it cannot classify comes back "unknown",
never "ephemeral".

Note: redefined-outer-name is disabled because pytest fixtures with the same names
are a standard pattern, protected-access because the parsing helpers are private, and
unused-argument because several fixtures are requested purely for their side effect.
"""

import os

import pytest

from src import config as config_module
from src.config import (
    ConfigManager,
    check_data_dir_persistent,
    _fstype_for_path,
    _in_container,
)


# A container: overlay root, an ext4 bind mount on /app/data, a tmpfs elsewhere.
MOUNTINFO_CONTAINER = """\
1234 1233 0:78 / / rw,relatime - overlay overlay rw,lowerdir=/l,upperdir=/u,workdir=/w
1240 1234 0:79 / /proc rw,nosuid - proc proc rw
1250 1234 259:1 /srv/eos/data /app/data rw,relatime - ext4 /dev/nvme0n1p1 rw
1260 1234 0:80 / /app/cache rw,nosuid - tmpfs tmpfs rw
"""

# A plain host: single ext4 root, nothing container-like.
MOUNTINFO_HOST = "26 1 259:1 / / rw,relatime - ext4 /dev/nvme0n1p1 rw\n"


@pytest.fixture
def mountinfo(tmp_path, monkeypatch):
    """Point the parser at a fixture file instead of the real /proc."""

    def _write(text):
        path = tmp_path / "mountinfo"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(config_module, "_MOUNTINFO_PATH", str(path))
        return path

    return _write


@pytest.fixture
def no_mountinfo(tmp_path, monkeypatch):
    """Simulate a platform with no /proc at all."""
    monkeypatch.setattr(
        config_module, "_MOUNTINFO_PATH", str(tmp_path / "does-not-exist")
    )


def _fake_stat(monkeypatch, same_device):
    """Override only the device id reported by os.stat, keeping existence real.

    os.stat backs os.path.exists, so a fake that invents results for every path
    also makes /data/options.json "exist" and flips is_ha_addon. Delegating to
    the real call first keeps existence honest and rewrites nothing but st_dev.
    """
    real_stat = os.stat

    class _Result:
        def __init__(self, source, dev):
            self._source = source
            self.st_dev = dev

        def __getattr__(self, name):
            return getattr(self._source, name)

    def fake(path, *args, **kwargs):
        real = real_stat(path, *args, **kwargs)  # raises if genuinely missing
        if os.path.abspath(str(path)) == "/":
            return _Result(real, 1)
        return _Result(real, 1 if same_device else 2)

    monkeypatch.setattr(config_module.os, "stat", fake)


# -----------------------------------------------------------------------
# mountinfo parsing
# -----------------------------------------------------------------------

class TestFstypeForPath:
    """Longest-prefix matching over mountinfo."""

    def test_exact_mount_point(self, mountinfo):
        """A directory that is itself a mount point."""
        mountinfo(MOUNTINFO_CONTAINER)
        assert _fstype_for_path("/app/data") == "ext4"

    def test_tmpfs_mount_point(self, mountinfo):
        """Reports the real fs type, whatever it is."""
        mountinfo(MOUNTINFO_CONTAINER)
        assert _fstype_for_path("/app/cache") == "tmpfs"

    def test_root_is_overlay(self, mountinfo):
        """The container image layer."""
        mountinfo(MOUNTINFO_CONTAINER)
        assert _fstype_for_path("/") == "overlay"

    def test_unmounted_path_falls_back_to_root(self, mountinfo):
        """/app has no mount of its own, so it belongs to the overlay root."""
        mountinfo(MOUNTINFO_CONTAINER)
        assert _fstype_for_path("/app") == "overlay"

    def test_longest_prefix_wins_not_first_match(self, mountinfo):
        """A file under /app/data must not be attributed to /."""
        mountinfo(MOUNTINFO_CONTAINER)
        assert _fstype_for_path("/app/data/eos_connect.db") == "ext4"

    def test_prefix_must_be_a_path_component(self, mountinfo):
        """/app/database must not match the /app/data mount by string prefix."""
        mountinfo(MOUNTINFO_CONTAINER)
        assert _fstype_for_path("/app/database") == "overlay"

    def test_octal_escapes_in_mount_point(self, mountinfo):
        """mountinfo writes a space as \\040."""
        mountinfo(
            MOUNTINFO_CONTAINER
            + "1270 1234 259:1 / /app/my\\040data rw - xfs /dev/sdb rw\n"
        )
        assert _fstype_for_path("/app/my data") == "xfs"

    def test_last_shadowed_mount_wins(self, mountinfo):
        """Two mounts on one point: the later entry is the effective one."""
        mountinfo(
            MOUNTINFO_CONTAINER
            + "1280 1234 259:9 / /app/data rw - btrfs /dev/sdc rw\n"
        )
        assert _fstype_for_path("/app/data") == "btrfs"

    def test_garbage_lines_are_skipped(self, mountinfo):
        """Truncated and separator-less lines must not hide a later valid one."""
        mountinfo(
            "garbage\n"
            "1 2 3 4 5 6 7 8\n"
            "1234 1233 0:78 / / rw - overlay overlay rw\n"
            "1250 1234 259:1 / /app/data rw,relatime - ext4 /dev/nvme0n1p1 rw\n"
        )
        assert _fstype_for_path("/app/data") == "ext4"

    def test_separator_as_last_field_is_skipped(self, mountinfo):
        """A line with no fs type after the separator."""
        mountinfo("1250 1234 259:1 / /app/data rw,relatime shared:1 -\n")
        assert _fstype_for_path("/app/data") is None

    def test_returns_none_without_proc(self, no_mountinfo):
        """No /proc means no opinion, not a crash."""
        assert _fstype_for_path("/app/data") is None


# -----------------------------------------------------------------------
# Container detection
# -----------------------------------------------------------------------

class TestInContainer:
    """Three OR-ed signals, failing closed."""

    def _no_markers(self, monkeypatch):
        monkeypatch.delenv("container", raising=False)
        monkeypatch.setattr(config_module.os.path, "exists", lambda p: False)

    def test_detected_via_dockerenv(self, monkeypatch, no_mountinfo):
        """The signal Docker and compose always set."""
        monkeypatch.delenv("container", raising=False)
        monkeypatch.setattr(
            config_module.os.path, "exists", lambda p: p == "/.dockerenv"
        )
        assert _in_container() is True

    def test_detected_via_container_env(self, monkeypatch, no_mountinfo):
        """Podman and systemd-nspawn set this instead."""
        monkeypatch.setattr(config_module.os.path, "exists", lambda p: False)
        monkeypatch.setenv("container", "podman")
        assert _in_container() is True

    def test_detected_via_overlay_root(self, monkeypatch, mountinfo):
        """containerd and nerdctl set neither marker; the overlay root gives it away."""
        mountinfo(MOUNTINFO_CONTAINER)
        self._no_markers(monkeypatch)
        assert _in_container() is True

    def test_not_detected_on_bare_host(self, monkeypatch, mountinfo):
        """An ext4 root with no markers is a plain host."""
        mountinfo(MOUNTINFO_HOST)
        self._no_markers(monkeypatch)
        assert _in_container() is False

    def test_not_detected_without_proc(self, monkeypatch, no_mountinfo):
        """No /proc and no markers must mean "not a container", not a crash."""
        self._no_markers(monkeypatch)
        assert _in_container() is False


# -----------------------------------------------------------------------
# The verdict
# -----------------------------------------------------------------------

class TestCheckDataDirPersistent:
    """st_dev is the verdict; mountinfo only enriches and vetoes tmpfs.

    The mount points here are real directories under tmp_path, because the
    verdict walks up to the nearest existing path — synthetic /app/data would
    resolve all the way to / and test nothing.
    """

    @pytest.fixture
    def container_fs(self, tmp_path, mountinfo):
        """Real data/cache dirs plus a mountinfo that describes them."""
        data = tmp_path / "app" / "data"
        cache = tmp_path / "app" / "cache"
        data.mkdir(parents=True)
        cache.mkdir(parents=True)
        mountinfo(
            "1234 1233 0:78 / / rw - overlay overlay rw,lowerdir=/l\n"
            f"1250 1234 259:1 /srv {data} rw,relatime - ext4 /dev/nvme0n1p1 rw\n"
            f"1260 1234 0:80 / {cache} rw,nosuid - tmpfs tmpfs rw\n"
        )
        return data, cache

    def test_same_device_as_root_is_not_persistent(self, monkeypatch, container_fs):
        """The volume-less container from #287."""
        data, _ = container_fs
        _fake_stat(monkeypatch, same_device=True)
        verdict, detail = check_data_dir_persistent(str(data))
        assert verdict is False
        assert "container image layer" in detail

    def test_own_mount_is_persistent(self, monkeypatch, container_fs):
        """Bind mount, named volume and anonymous volume are all this one path."""
        data, _ = container_fs
        _fake_stat(monkeypatch, same_device=False)
        verdict, detail = check_data_dir_persistent(str(data))
        assert verdict is True
        assert "ext4" in detail

    def test_ram_backed_mount_is_still_persistent_but_flagged(
        self, monkeypatch, container_fs
    ):
        """tmpfs must NOT flip the verdict — it would false-alarm on bind mounts.

        From inside a container, `--tmpfs /app/data` and `-v /tmp/eos:/app/data`
        look identical, and systemd puts /tmp on tmpfs on many distros. The bind
        mount survives a container recreate, so the verdict stays persistent and
        the volatility is reported in the detail instead.
        """
        _, cache = container_fs
        _fake_stat(monkeypatch, same_device=False)
        verdict, detail = check_data_dir_persistent(str(cache))
        assert verdict is True
        assert "RAM-backed" in detail
        assert "host reboots" in detail

    def test_unknown_when_stat_fails(self, monkeypatch, container_fs):
        """An unreadable directory must not become a verdict."""
        data, _ = container_fs

        def boom(*_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(config_module.os, "stat", boom)
        verdict, _ = check_data_dir_persistent(str(data))
        assert verdict is None

    def test_verdict_without_mountinfo_still_works(
        self, monkeypatch, tmp_path, no_mountinfo
    ):
        """No /proc degrades the message, not the verdict."""
        _fake_stat(monkeypatch, same_device=True)
        verdict, detail = check_data_dir_persistent(str(tmp_path))
        assert verdict is False
        assert "unknown fs" in detail

    def test_missing_dir_is_unknown_not_ephemeral(self, tmp_path):
        """Fails closed. ConfigStore.open() creates the directory before this runs,
        so a missing one means something unexpected — never assume the worst."""
        verdict, detail = check_data_dir_persistent(str(tmp_path / "nope"))
        assert verdict is None
        assert "cannot stat" in detail


# -----------------------------------------------------------------------
# ConfigManager.data_dir_persistence
# -----------------------------------------------------------------------

class TestConfigManagerPersistenceState:
    """The tri-state wrapper the rest of the app consumes."""

    def _make_cm(self, tmp_path, monkeypatch):
        """ConfigManager whose data dir exists, as ConfigStore.open() leaves it.

        start_db() calls ConfigStore.open() — which does os.makedirs — well before
        the persistence check, so a present directory is the real precondition.
        """
        for var in ("HASSIO", "HASSIO_TOKEN", "EOS_DATA_PATH"):
            monkeypatch.delenv(var, raising=False)
        (tmp_path / "config.yaml").write_text("time_zone: UTC\n", encoding="utf-8")
        (tmp_path / "data").mkdir(exist_ok=True)
        return ConfigManager(str(tmp_path))

    def test_ha_addon_is_always_persistent(self, tmp_path, monkeypatch):
        """Supervisor owns /data; never second-guess it."""
        (tmp_path / "config.yaml").write_text("time_zone: UTC\n", encoding="utf-8")
        monkeypatch.setenv("HASSIO", "1")
        cm = ConfigManager(str(tmp_path))
        state, detail = cm.data_dir_persistence()
        assert state == "persistent"
        assert "Supervisor" in detail

    def test_unknown_outside_a_container(self, tmp_path, monkeypatch, mountinfo):
        """A bare Python install has no volume to mount and must not be warned."""
        mountinfo(MOUNTINFO_HOST)
        cm = self._make_cm(tmp_path, monkeypatch)
        monkeypatch.delenv("container", raising=False)
        monkeypatch.setattr(config_module.os.path, "exists", lambda p: False)
        state, detail = cm.data_dir_persistence()
        assert state == "unknown"
        assert "not running in a container" in detail

    def test_ephemeral_in_container_without_a_volume(
        self, tmp_path, monkeypatch, mountinfo
    ):
        """The one actionable state."""
        mountinfo(MOUNTINFO_CONTAINER)
        cm = self._make_cm(tmp_path, monkeypatch)
        monkeypatch.setenv("container", "docker")
        _fake_stat(monkeypatch, same_device=True)
        state, _ = cm.data_dir_persistence()
        assert state == "ephemeral"

    def test_persistent_in_container_with_a_volume(
        self, tmp_path, monkeypatch, mountinfo
    ):
        """A mounted volume is the fixed state."""
        mountinfo(MOUNTINFO_CONTAINER)
        cm = self._make_cm(tmp_path, monkeypatch)
        monkeypatch.setenv("container", "docker")
        _fake_stat(monkeypatch, same_device=False)
        state, _ = cm.data_dir_persistence()
        assert state == "persistent"

    def test_unknown_when_undeterminable_in_container(
        self, tmp_path, monkeypatch, mountinfo
    ):
        """Undeterminable is never ephemeral."""
        mountinfo(MOUNTINFO_CONTAINER)
        cm = self._make_cm(tmp_path, monkeypatch)
        monkeypatch.setenv("container", "docker")

        def boom(*_args, **_kwargs):
            raise OSError("nope")

        monkeypatch.setattr(config_module.os, "stat", boom)
        state, _ = cm.data_dir_persistence()
        assert state == "unknown"

    def test_never_raises_on_this_host(self, tmp_path, monkeypatch):
        """Whatever the real machine looks like, the call must return a state."""
        cm = self._make_cm(tmp_path, monkeypatch)
        state, detail = cm.data_dir_persistence()
        assert state in {"persistent", "ephemeral", "unknown"}
        assert isinstance(detail, str) and detail
