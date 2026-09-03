"""Constants for the SEGGER J-Link DLL.

Provenance is recorded per group, because it varies:

``observed``
    Not published in a header. Recovered by inspecting the shipped library and
    corroborated against a live probe. Treat these as specific to the DLL
    versions they were checked on rather than as a stable contract.

``sdk``
    From SEGGER's public headers. Stable across releases and, where the values
    overlap something this SDK drives today, corroborated by working code.

``empirical``
    Measured against known-count firmware on real hardware.

Anything undocumented says so in its own comment. Guesses are not in here.
"""

from __future__ import annotations

from enum import IntEnum

# --------------------------------------------------------------------------
# Target interface (sdk)
# --------------------------------------------------------------------------


class Interface(IntEnum):
    """Values accepted by ``JLINKARM_TIF_Select``."""

    JTAG = 0
    SWD = 1
    BDM3 = 2
    FINE = 3
    ICSP = 4
    SPI = 5
    C2 = 6
    CJTAG = 7


INTERFACE_BY_NAME = {
    "JTAG": Interface.JTAG,
    "SWD": Interface.SWD,
    "FINE": Interface.FINE,
    "ICSP": Interface.ICSP,
    "SPI": Interface.SPI,
    "C2": Interface.C2,
    "CJTAG": Interface.CJTAG,
}

# --------------------------------------------------------------------------
# Host interfaces for probe enumeration (sdk)
# --------------------------------------------------------------------------

HOST_IF_USB = 1
HOST_IF_IP = 2
HOST_IF_ALL = HOST_IF_USB | HOST_IF_IP


# --------------------------------------------------------------------------
# Reset (sdk)
# --------------------------------------------------------------------------


class ResetType(IntEnum):
    NORMAL = 0
    CORE = 1
    RESET_PIN = 2
    CONNECT_UNDER_RESET = 3
    HALT_AFTER_BTL = 4
    HALT_BEFORE_BTL = 5
    KINETIS = 6
    ADI_HALT_AFTER_KERNEL = 7
    CORE_AND_PERIPHERALS = 8
    LPC1200 = 9
    S3FN60D = 10


# --------------------------------------------------------------------------
# Speed (sdk)
# --------------------------------------------------------------------------

SPEED_AUTO = 0
SPEED_ADAPTIVE = 0xFFFE
SPEED_INVALID = 0xFFFF


# --------------------------------------------------------------------------
# Breakpoint type flags (sdk)
#
# The low nibble selects the implementation, the high bits the mode. ANY lets
# the DLL choose, which is what nearly every caller wants.
# --------------------------------------------------------------------------


class BreakpointType(IntEnum):
    ARM = 0x00000001
    THUMB = 0x00000002
    SW_RAM = 0x00000010
    SW_FLASH = 0x00000020
    SW = 0x000000F0
    HW = 0xFFFFFF00
    ANY = 0xFFFFFFF0


# --------------------------------------------------------------------------
# Watchpoint / data-event access flags (sdk)
# --------------------------------------------------------------------------


class DataEventType(IntEnum):
    BP_DATA = 0x00000001
    """Halt when the access matches."""

    TRACE_START = 0x00000002
    TRACE_STOP = 0x00000004


class AccessSize(IntEnum):
    SIZE_8 = 0x00000000
    SIZE_16 = 0x00000001
    SIZE_32 = 0x00000002


ACCESS_DIR_READ = 0x00000000
ACCESS_DIR_WRITE = 0x00000001
ACCESS_PRIV = 0x00000010


# --------------------------------------------------------------------------
# STRACE -- selective/instruction trace.
#
# Command values are `sdk`, and SET_BUFFER_SIZE is additionally `empirical`:
# it is the command a working capture path uses to size the probe-side ring.
# The DLL rejects any command above 3, so this is the whole set.
# --------------------------------------------------------------------------


class StraceCmd(IntEnum):
    TRACE_EVENT_SET = 0
    TRACE_EVENT_CLR = 1
    TRACE_EVENT_CLR_ALL = 2
    SET_BUFFER_SIZE = 3


class StraceEventType(IntEnum):
    CODE_FETCH = 0
    DATA_ACCESS = 1
    DATA_LOAD = 2
    DATA_STORE = 3


class StraceOperation(IntEnum):
    TRACE_START = 0
    TRACE_STOP = 1
    TRACE_INCLUDE_RANGE = 2
    TRACE_EXCLUDE_RANGE = 3


# ``Type`` argument of JLINK_STRACE_GetInstStats (empirical).
#
# The function is absent from the public headers. The layout below was
# recovered from the library and confirmed against firmware with known
# execution counts: an item is 8 bytes, NumItems counts 2-byte slots, and item
# i describes the address Addr + i*2.
class InstStatsType(IntEnum):
    EXEC_COUNT = 0
    """Per-halfword-slot execution counts. This is what coverage is built on."""

    AGGREGATE = 4
    """One total for the whole capture, not per address."""


INST_STATS_ITEM_BYTES = 8
"""``{U32 count; U32 reserved}``."""

STRACE_TIMESTAMP_ITEM_BYTES = 16
"""``sizeof(JLINK_STRACE_TIMESTAMP_INFO)`` (observed).

The record is sixteen bytes; the field layout is in
:class:`jtrace.structs.StraceTimestampInfo`. The cadence at which the DLL emits
stamps is a different question and is not known -- it has to be measured on
hardware.
"""

STRACE_READEX_FLAGS_NONE = 0
"""The ``Flags`` argument of ``JLINK_STRACE_ReadEx`` (observed).
No other value has been seen in use, and none is documented."""

MAX_STRACE_ITEMS = 0x10000
"""The DLL's hard clamp on a single ``JLINK_STRACE_Read``.

Asking for more silently returns this many: the read path is backed by a
fixed 256 KiB buffer, which is 65,536 x 4 bytes. It is a per-call ceiling, not
a hardware or buffer limit -- see :mod:`jtrace.tracebuf` for the offset-cursor
API that has no such clamp.
"""


# --------------------------------------------------------------------------
# TRACE -- the general trace buffer API.
#
# All values `observed`. The DLL rejects any command outside this set, and
# every entry below has been exercised against a probe. Unlike STRACE this API
# has a real cursor (TRACE_Read takes an Offset) and capacity control, so it is
# not subject to the 65,536-item clamp above.
# --------------------------------------------------------------------------


class TraceCmd(IntEnum):
    START = 0x00
    STOP = 0x01
    FLUSH = 0x02
    GET_NUM_SAMPLES = 0x10
    GET_CONF_CAPACITY = 0x11
    SET_CAPACITY = 0x12
    GET_MIN_CAPACITY = 0x13
    GET_MAX_CAPACITY = 0x14
    SET_FORMAT = 0x20
    GET_FORMAT = 0x21
    GET_NUM_REGIONS = 0x30
    GET_REGION_PROPS = 0x31
    GET_REGION_PROPS_EX = 0x32


class TraceFormat(IntEnum):
    """Bit flags for ``TraceCmd.SET_FORMAT`` (sdk)."""

    FORMAT_4BIT = 0x0001
    FORMAT_8BIT = 0x0002
    FORMAT_16BIT = 0x0004
    FORMAT_MULTIPLEXED = 0x0008
    FORMAT_DEMULTIPLEXED = 0x0010
    FORMAT_DOUBLE_EDGE = 0x0020
    FORMAT_ETM7_9 = 0x0040
    FORMAT_ETM10 = 0x0080
    FORMAT_1BIT = 0x0100
    FORMAT_2BIT = 0x0200


# --------------------------------------------------------------------------
# RAWTRACE (observed)
#
# The DLL rejects any command above 4, so this is the whole set.
# --------------------------------------------------------------------------


class RawTraceCmd(IntEnum):
    START = 0
    STOP = 1
    GET_TRACE_FREQ = 2
    SET_BUFF_SIZE = 3
    GET_CAPS = 4


# --------------------------------------------------------------------------
# SWO / ITM (observed)
#
# The DLL rejects any command above 0x15. Command 0 is also what an
# unrecognised value falls through to, so it is the effective default.
# --------------------------------------------------------------------------


class SwoCmd(IntEnum):
    START = 0x00
    STOP = 0x01
    FLUSH = 0x02
    GET_SPEED_INFO = 0x03
    GET_NUM_BYTES = 0x0A
    SET_BUFFERSIZE_HOST = 0x14
    SET_BUFFERSIZE_EMU = 0x15


class SwoInterface(IntEnum):
    UART = 0
    MANCHESTER = 1


# --------------------------------------------------------------------------
# POWERTRACE (observed)
#
# The DLL rejects any command above 6, so this is the whole set.
# --------------------------------------------------------------------------


class PowerTraceCmd(IntEnum):
    SETUP = 0
    START = 1
    STOP = 2
    FLUSH = 3
    GET_CAPS = 4
    GET_CHANNEL_CAPS = 5
    GET_NUM_ITEMS = 6


# --------------------------------------------------------------------------
# RTT (sdk)
#
# The DLL rejects any command above 5, which is exactly this set. The names
# below come from the public header.
# --------------------------------------------------------------------------


class RttCmd(IntEnum):
    START = 0
    STOP = 1
    GETDESC = 2
    GETNUMBUF = 3
    GETSTAT = 4


RTT_DIRECTION_UP = 0
"""Target to host."""

RTT_DIRECTION_DOWN = 1
"""Host to target."""

RTT_AUTO_DETECT_CONTROL_BLOCK = 0
"""Passed as CtrlBlockAddr to make the DLL search target RAM for "SEGGER RTT"."""


# --------------------------------------------------------------------------
# HSS -- high-speed sampling (sdk)
# --------------------------------------------------------------------------

HSS_FLAG_TIMESTAMP_US = 1 << 0


# --------------------------------------------------------------------------
# CoreSight (sdk)
# --------------------------------------------------------------------------


class ApDpReg(IntEnum):
    DP_ABORT = 0
    DP_CTRL_STAT = 1
    DP_SELECT = 2
    DP_RDBUFF = 3
    AP_CSW = 0
    AP_TAR = 1
    AP_DRW = 3
    AP_BD0 = 4
    AP_BD1 = 5
    AP_BD2 = 6
    AP_BD3 = 7
    AP_ROM = 0x0E
    AP_IDR = 0x0F


CORESIGHT_DP = 0
CORESIGHT_AP = 1


# --------------------------------------------------------------------------
# Trace source for JLINK_SelectTraceSource (sdk)
# --------------------------------------------------------------------------


class TraceSource(IntEnum):
    ETB = 0
    ETM_TRACE_PORT = 1
    MTB = 2


# --------------------------------------------------------------------------
# Thumb (architecture, not DLL)
# --------------------------------------------------------------------------

HALFWORD_BYTES = 2

THUMB_BIT = 1
"""ELF symbol values for Thumb functions carry this bit; trace addresses do not.

Both directions are load-bearing. Query the symbol table at
``address | THUMB_BIT``: querying the even address lands on the zero-size
mapping symbol (``$t``) that sits at every function entry, which hides the
function and drops its first instruction. Compare stream addresses at
``address & THUMB_ADDRESS_MASK``: comparing them unmasked makes
``address == fn.addr`` permanently false, so calls stop being recognised as
calls.
"""

THUMB_ADDRESS_MASK = ~THUMB_BIT

ADDRESS_MAX = 1 << 64
"""Open upper bound for an address span. Ours, not the DLL's or the
architecture's -- a sentinel that lets a span resolver return a half-open range
for the last symbol in an image without the caller special-casing ``None``."""


# --------------------------------------------------------------------------
# Defaults shared with the TypeScript capture path, so a capture driven from
# Python lands in the same place with the same shape as one driven from the UI.
# --------------------------------------------------------------------------

DEFAULT_TRACE_BUFFER_BYTES = 1 << 24
"""16 MiB probe-side ring."""

DEFAULT_TRACE_CAPACITY = 10_000_000
"""Instructions the host-side trace store holds before it drops the oldest
block. Our policy, not the DLL's and not the TypeScript path's -- it matches
what Ozone defaults its own ring to, which is the only reason for this exact
number. At 4 bytes per program counter that is a 40 MB ceiling."""

DEFAULT_SPEED_KHZ = 4000
DEFAULT_PORT_WIDTH = 4
DEFAULT_DURATION_MS = 3000
EXEC_ERROR_BUF_SIZE = 256
