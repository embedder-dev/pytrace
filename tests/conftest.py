"""Shared fixtures, and the guard that keeps the suite honest.

Nothing here needs a probe. The one external input is the oracle firmware, and
it is committed under ``fixtures/firmware/`` so a clean checkout runs the whole
suite -- see that directory's README for what makes it an oracle.

Two tests do need the SEGGER software pack installed, because what they check
is that this SDK's prototypes match the real library. They carry the
``requires_jlink_software`` marker and skip without it. Every other skip is a
defect: a skip is how thirty tests, including every test of the hand-written
ELF and DWARF parsers, once retired themselves silently when a fixture moved.
``--fail-on-skip`` turns any unmarked skip into a failure, and CI passes it.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ORACLE_ELF = Path(__file__).parent / "fixtures" / "firmware" / "coverage_demo.elf"


@pytest.fixture
def oracle_elf() -> Path:
    """The known-count demo firmware the ELF, DWARF and capture tests use.

    Raises rather than skips when it is missing. It is a committed fixture, so
    its absence is a broken checkout -- and a skip here would quietly retire
    around a seventh of the suite, including every test of the hand-written ELF
    and DWARF parsers.
    """
    if not ORACLE_ELF.is_file():
        raise FileNotFoundError(
            f"Oracle firmware missing at {ORACLE_ELF}. It is committed to the "
            f"repository; restore it with `git checkout -- tests/fixtures/`."
        )
    return ORACLE_ELF


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--fail-on-skip",
        action="store_true",
        help=(
            "Treat any skip that is not marked requires_jlink_software as a "
            "failure. CI passes this so a vanished fixture is loud."
        ),
    )


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_jlink_software: needs the SEGGER J-Link software pack "
        "installed; skipped without it, and exempt from --fail-on-skip",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Convert an unexpected skip into a failure when asked.

    Reported as a failure rather than an error so it lands in the same summary
    a reader is already looking at, and names the marker that would legitimise
    it if the skip turns out to be intended.
    """
    outcome = yield
    report = outcome.get_result()
    if (
        report.skipped
        and item.config.getoption("--fail-on-skip")
        and item.get_closest_marker("requires_jlink_software") is None
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} was skipped, and --fail-on-skip is set.\n"
            f"A skip here means a fixture went missing, not that the test is "
            f"optional. If the skip is intended, mark the test "
            f"requires_jlink_software."
        )
