"""The parts of the connection layer that can be exercised without a probe.

Mostly the lookups and guards that would otherwise only be discovered on a
bench, where the feedback loop costs minutes rather than milliseconds.
"""

import ctypes

import pytest

from jtrace.constants import INTERFACE_BY_NAME, Interface
from jtrace.errors import JLinkError, describe_code
from jtrace.link import JLink, Register, _register_aliases, close_open_link
from jtrace.loader import _PROTOTYPES, find_library
from jtrace.structs import (
    DataEvent,
    EmuConnectInfo,
    TraceRegionPropsEx,
)


# -- register names --------------------------------------------------------


def test_alias_expansion_splits_the_parenthesised_form():
    assert _register_aliases("R15 (PC)") == {"r15 (pc)", "r15", "pc"}
    assert _register_aliases("R0") == {"r0"}
    assert _register_aliases("XPSR") == {"xpsr"}


class _FakeLink:
    """Just enough JLink to exercise the name lookup."""

    _registers = [
        Register(0, "R0"),
        Register(13, "R13 (SP)"),
        Register(14, "R14"),
        Register(15, "R15 (PC)"),
        Register(16, "XPSR"),
    ]

    def registers(self):
        return self._registers

    _register_index = JLink._register_index


def test_lookup_prefers_an_exact_name():
    assert _FakeLink()._register_index("R14") == 14
    assert _FakeLink()._register_index("xpsr") == 16


def test_lookup_accepts_the_short_alias():
    # The single most likely thing a caller passes, and the one an
    # exact-match-only lookup rejects.
    assert _FakeLink()._register_index("PC") == 15
    assert _FakeLink()._register_index("sp") == 13


def test_lookup_passes_an_integer_straight_through():
    assert _FakeLink()._register_index(7) == 7


def test_unknown_register_names_what_is_available():
    with pytest.raises(JLinkError, match="Available: R0"):
        _FakeLink()._register_index("Q7")


# -- structs ---------------------------------------------------------------


def test_region_props_ex_is_exactly_the_size_the_dll_demands():
    # The DLL rejects SizeofStruct < 32 and > 256, in its own words. 32 on the
    # nose is the corroboration that the layout is right.
    assert ctypes.sizeof(TraceRegionPropsEx) == 32
    props = TraceRegionPropsEx.new(region_index=3)
    assert props.SizeofStruct == 32
    assert props.RegionIndex == 3


def test_sized_structs_stamp_their_own_size():
    event = DataEvent.new()
    assert event.SizeOfStruct == ctypes.sizeof(DataEvent)


def test_emu_connect_info_layout_is_stable():
    # A wrong size here means enumeration reads neighbouring entries as
    # garbage rather than failing, so it is worth pinning.
    assert ctypes.sizeof(EmuConnectInfo) == 264
    assert EmuConnectInfo.acProduct.offset == 50
    assert EmuConnectInfo.acNickName.offset == 82


# -- constants -------------------------------------------------------------


def test_interface_names_map_to_the_dll_values():
    assert INTERFACE_BY_NAME["SWD"] == Interface.SWD == 1
    assert INTERFACE_BY_NAME["JTAG"] == Interface.JTAG == 0


def test_error_codes_describe_themselves():
    assert "not connected" in describe_code(-2)
    assert describe_code(-9999) == "code -9999"


# -- the library -----------------------------------------------------------


@pytest.mark.requires_jlink_software
def test_every_prototype_binds_against_the_installed_dll():
    """The DLL is the source of truth; a typo in a name is caught here."""
    if find_library() is None:
        pytest.skip("J-Link software not installed")
    from jtrace.loader import load

    library = load()
    assert library.missing == set(), sorted(library.missing)
    assert len(library.bound) == len(_PROTOTYPES)


@pytest.mark.requires_jlink_software
def test_bound_functions_are_reachable_not_just_present():
    """The bug this pins: prototypes bound onto the CDLL rather than onto the
    wrapper made every call raise "not exported", including the 237 that were."""
    if find_library() is None:
        pytest.skip("J-Link software not installed")
    from jtrace.loader import load

    library = load()
    assert callable(library.JLINKARM_EMU_GetList)
    assert callable(library.JLINK_STRACE_Read)
    assert callable(library.JLINKARM_TRACE_Control)


def test_missing_library_path_is_a_clear_error():
    from jtrace.errors import LibraryNotFoundError
    from jtrace.loader import RawLibrary

    with pytest.raises(LibraryNotFoundError):
        RawLibrary("/nonexistent/libjlinkarm.dylib")


# -- leaked-handle recovery -------------------------------------------------


class _ClosableLink:
    """Enough of JLink for close_open_link; the real one needs a probe."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True
        JLink._open_instance = None


def test_close_open_link_is_a_noop_when_nothing_is_open():
    assert JLink._open_instance is None
    assert close_open_link() is False


def test_close_open_link_closes_a_leaked_handle():
    leaked = _ClosableLink()
    JLink._open_instance = leaked
    try:
        assert close_open_link() is True
        assert leaked.closed is True
        # The point of the helper: the process global is clear afterwards, so a
        # later JLink() opens instead of raising "already open".
        assert JLink._open_instance is None
    finally:
        JLink._open_instance = None
