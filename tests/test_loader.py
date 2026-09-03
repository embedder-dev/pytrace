"""Finding the J-Link library.

There were no tests here, and the gap showed: on ARM Linux the SEGGER software
normally arrives as a tarball you unpack and run in place, which leaves nothing
in `/opt` and nothing on `PATH`. Discovery missed it entirely, so
`JLINK_LIBRARY` was mandatory on a perfectly ordinary install.

Everything below is hermetic -- a synthesized directory tree, a patched `$HOME`
and a patched platform. Nothing here touches a real install, because CI runs
with `--fail-on-skip` and a test that quietly needs a probe is a test that
retires itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jtrace import loader


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """A Linux machine with no packaged install and nothing on PATH."""
    monkeypatch.setattr(loader.sys, "platform", "linux")
    monkeypatch.setattr(loader, "_SEARCH_DIRS", {"linux": []})
    monkeypatch.setattr(loader.shutil, "which", lambda _name: None)
    monkeypatch.delenv("JLINK_LIBRARY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(loader.Path, "home", classmethod(lambda _cls: home))
    return home


def unpack(home: Path, name: str, library: str = "libjlinkarm.so") -> Path:
    """Lay out a directory the way SEGGER's tarball does."""
    directory = home / name
    directory.mkdir()
    (directory / library).write_bytes(b"")
    (directory / "JLinkExe").write_bytes(b"")
    return directory / library


# -- the gap this closes ---------------------------------------------------


def test_an_unpacked_tarball_under_home_is_found(isolated):
    """The whole point: no /opt, no PATH, still found."""
    expected = unpack(isolated, "JLink_Linux_V972_arm64")
    assert loader.find_library() == expected


def test_lowercase_directories_are_found_too(isolated):
    expected = unpack(isolated, "jlink")
    assert loader.find_library() == expected


def test_a_versioned_soname_is_found_when_there_is_no_bare_one(isolated):
    expected = unpack(isolated, "JLink_Linux_V972_arm64", "libjlinkarm.so.9.72.0")
    assert loader.find_library() == expected


def test_nothing_unpacked_still_returns_none(isolated):
    (isolated / "JLink_Linux_V972_arm64").mkdir()
    assert loader.find_library() is None


# -- precedence ------------------------------------------------------------


def test_a_packaged_install_wins_over_an_unpacked_one(monkeypatch, isolated, tmp_path):
    """A tarball someone left in their home directory must not silently
    outrank the system install a debug session on the same machine would use."""
    packaged = tmp_path / "opt"
    packaged.mkdir()
    (packaged / "libjlinkarm.so").write_bytes(b"")
    monkeypatch.setattr(loader, "_SEARCH_DIRS", {"linux": [str(packaged)]})
    unpack(isolated, "JLink_Linux_V972_arm64")

    assert loader.find_library() == packaged / "libjlinkarm.so"


def test_the_newer_of_two_unpacked_versions_wins(isolated):
    """Lexical, not semantic -- but V972 beating V918 is the case that occurs,
    and JLINK_LIBRARY is still how you pin one exactly."""
    unpack(isolated, "JLink_Linux_V918_arm64")
    newer = unpack(isolated, "JLink_Linux_V972_arm64")
    assert loader.find_library() == newer


def test_the_env_override_still_beats_everything(monkeypatch, isolated, tmp_path):
    unpack(isolated, "JLink_Linux_V972_arm64")
    pinned = tmp_path / "pinned.so"
    pinned.write_bytes(b"")
    monkeypatch.setenv("JLINK_LIBRARY", str(pinned))
    assert loader.find_library() == pinned


def test_an_override_pointing_nowhere_finds_nothing(monkeypatch, isolated):
    """Rather than silently falling back: if you pinned a version, binding a
    different one is worse than failing."""
    unpack(isolated, "JLink_Linux_V972_arm64")
    monkeypatch.setenv("JLINK_LIBRARY", "/nonexistent/libjlinkarm.so")
    assert loader.find_library() is None


# -- the search must not widen more than intended --------------------------


def test_only_one_level_below_home_is_searched(isolated):
    """A recursive walk of $HOME would be slow on every lookup and a way to
    bind something that merely looks like a J-Link library."""
    nested = isolated / "projects" / "JLink_Linux_V972_arm64"
    nested.mkdir(parents=True)
    (nested / "libjlinkarm.so").write_bytes(b"")
    assert loader.find_library() is None


def test_unrelated_home_directories_are_ignored(isolated):
    for name in ("Documents", "src", "jetbrains", "linky"):
        directory = isolated / name
        directory.mkdir()
        (directory / "libjlinkarm.so").write_bytes(b"")
    assert loader.find_library() is None


def test_a_file_named_like_the_directory_is_not_searched(isolated):
    (isolated / "JLink_notes.txt").write_bytes(b"")
    assert loader.find_library() is None


def test_windows_does_not_search_home(monkeypatch, tmp_path):
    """The glob is for tarball platforms; Windows has an installer and a
    registry key, and scanning a user profile there buys nothing."""
    monkeypatch.setattr(loader.sys, "platform", "win32")
    home = tmp_path / "home"
    (home / "JLink").mkdir(parents=True)
    (home / "JLink" / "JLinkARM.dll").write_bytes(b"")
    monkeypatch.setattr(loader.Path, "home", classmethod(lambda _cls: home))
    assert loader._unpacked_install_dirs() == []


def test_a_missing_home_is_not_an_error(monkeypatch):
    def explode(_cls):
        raise RuntimeError("no home directory")

    monkeypatch.setattr(loader.sys, "platform", "linux")
    monkeypatch.setattr(loader.Path, "home", classmethod(explode))
    assert loader._unpacked_install_dirs() == []


def test_is_available_agrees_with_find_library(isolated):
    assert loader.is_available() is False
    unpack(isolated, "JLink_Linux_V972_arm64")
    assert loader.is_available() is True
