"""TRACE -- the offset-addressable trace buffer.

The other half of the trace story, and the half no capture path here drives
yet. Where :mod:`jtrace.strace` hands back decoded program counters through a
call that clamps at 65,536 items, this API exposes the raw buffer:

* :meth:`TraceBuffer.num_samples` says how much is in there;
* :meth:`TraceBuffer.read` takes an ``offset``, so you can walk all of it;
* :meth:`TraceBuffer.capacity` can be raised toward :meth:`max_capacity`.

The command set is not guesswork: ``JLINKARM_TRACE_Control`` rejects anything
outside the thirteen commands in :class:`~jtrace.constants.TraceCmd`. The DLL's
own out-of-range diagnostic names the second quantity too -- "TRACE_Read()
called with parameter Offset out of bounds: Offset = %d, NumSamplesInBuffer =
%d" -- which is where the sample count comes from.

What you get back is :class:`~jtrace.structs.TraceData` items -- pipeline
status and packet bytes -- not program counters. Turning those into a PC stream
means implementing an ETM decoder, which this SDK does not do. Use this module
when you want the raw capture (to archive it, to feed an external decoder, or
to measure how much the probe actually holds); use :mod:`jtrace.strace` when
you want instructions.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import TraceCmd, TraceFormat, TraceSource
from .errors import TraceError
from .structs import TraceData, TraceRegionProps, TraceRegionPropsEx

if TYPE_CHECKING:
    from .link import JLink


@dataclass(frozen=True)
class Region:
    """A contiguous run of samples in the buffer.

    The buffer is not necessarily one stream: a capture that stopped and
    restarted leaves several regions, each with its own timestamp.
    """

    index: int
    num_samples: int
    offset: int
    region_count: int
    timestamp: int | None = None


class TraceBuffer:
    """Raw trace buffer access. Reached via ``JLink.trace``."""

    def __init__(self, link: "JLink") -> None:
        self._link = link
        self._lib = link.raw

    # -- control -----------------------------------------------------------

    def control(self, cmd: TraceCmd | int, data: object = None) -> int:
        pointer = ctypes.byref(data) if data is not None else None  # type: ignore[arg-type]
        rc = self._lib.JLINKARM_TRACE_Control(int(cmd), pointer)
        if rc < 0:
            name = TraceCmd(cmd).name if cmd in set(TraceCmd) else f"0x{int(cmd):02X}"
            raise TraceError(f"TRACE_Control({name}) failed", rc)
        return rc

    def _query(self, cmd: TraceCmd) -> int:
        value = ctypes.c_uint32(0)
        self.control(cmd, value)
        return value.value

    def start(self) -> None:
        self.control(TraceCmd.START)

    def stop(self) -> None:
        self.control(TraceCmd.STOP)

    def flush(self) -> None:
        """Discard buffered samples. The usual prelude to a fresh capture."""
        self.control(TraceCmd.FLUSH)

    # -- capacity ----------------------------------------------------------

    def num_samples(self) -> int:
        """Samples currently in the buffer. STRACE has no equivalent."""
        return self._query(TraceCmd.GET_NUM_SAMPLES)

    def capacity(self) -> int:
        """The configured capacity, in samples."""
        return self._query(TraceCmd.GET_CONF_CAPACITY)

    def min_capacity(self) -> int:
        return self._query(TraceCmd.GET_MIN_CAPACITY)

    def max_capacity(self) -> int:
        """The largest capacity this probe will accept.

        Worth reading before assuming a ceiling: it is a property of the probe
        and its firmware, not of the API.
        """
        return self._query(TraceCmd.GET_MAX_CAPACITY)

    def set_capacity(self, num_samples: int) -> None:
        value = ctypes.c_uint32(num_samples)
        self.control(TraceCmd.SET_CAPACITY, value)

    # -- format ------------------------------------------------------------

    def format(self) -> int:
        return self._query(TraceCmd.GET_FORMAT)

    def set_format(self, flags: TraceFormat | int) -> None:
        value = ctypes.c_uint32(int(flags))
        self.control(TraceCmd.SET_FORMAT, value)

    def select_source(self, source: TraceSource | int) -> None:
        """Choose between the trace port, an on-chip ETB, or an MTB."""
        rc = self._lib.JLINK_SelectTraceSource(int(source))
        if rc < 0:
            raise TraceError(f"Selecting trace source {int(source)} failed", rc)

    # -- regions -----------------------------------------------------------

    def num_regions(self) -> int:
        return self._query(TraceCmd.GET_NUM_REGIONS)

    def region(self, index: int, *, extended: bool = True) -> Region:
        """Describe one region. ``extended`` adds the 64-bit timestamp.

        The extended struct is the one the DLL size-checks against 32..256
        bytes; :class:`~jtrace.structs.TraceRegionPropsEx` is exactly 32.
        """
        if extended:
            props = TraceRegionPropsEx.new(index)
            self.control(TraceCmd.GET_REGION_PROPS_EX, props)
            return Region(
                index=props.RegionIndex,
                num_samples=props.NumSamples,
                offset=props.Off,
                region_count=props.RegionCnt,
                timestamp=props.Timestamp,
            )
        props_basic = TraceRegionProps()
        props_basic.RegionIndex = index
        self.control(TraceCmd.GET_REGION_PROPS, props_basic)
        return Region(
            index=props_basic.RegionIndex,
            num_samples=props_basic.NumSamples,
            offset=props_basic.Off,
            region_count=props_basic.RegionCnt,
        )

    def regions(self, *, extended: bool = True) -> list[Region]:
        return [self.region(i, extended=extended) for i in range(self.num_regions())]

    # -- reading -----------------------------------------------------------

    def read(self, offset: int = 0, num_items: int | None = None) -> list[TraceData]:
        """Read ``num_items`` samples starting at ``offset``.

        ``num_items=None`` reads everything from ``offset`` to the end. Unlike
        ``STRACE_Read`` there is no per-call clamp here -- the only bound is
        what the buffer holds, and passing an offset past that is an error the
        DLL names explicitly rather than a short read.
        """
        available = self.num_samples()
        if offset > available:
            raise TraceError(
                f"Offset {offset} is past the end of the buffer "
                f"({available} samples)"
            )
        wanted = available - offset if num_items is None else min(
            num_items, available - offset
        )
        if wanted <= 0:
            return []

        buffer = (TraceData * wanted)()
        count = ctypes.c_uint32(wanted)
        rc = self._lib.JLINKARM_TRACE_Read(
            ctypes.byref(buffer), offset, ctypes.byref(count)
        )
        if rc < 0:
            raise TraceError(f"TRACE_Read at offset {offset} failed", rc)
        return [buffer[i] for i in range(count.value)]

    def read_all(self, chunk: int = 65536) -> list[TraceData]:
        """Walk the whole buffer, chunked.

        Chunked purely to bound peak memory: ``read`` itself has no ceiling.
        """
        total = self.num_samples()
        out: list[TraceData] = []
        offset = 0
        while offset < total:
            batch = self.read(offset, min(chunk, total - offset))
            if not batch:
                break
            out.extend(batch)
            offset += len(batch)
        return out

    def read_raw(self, offset: int = 0, num_items: int | None = None) -> bytes:
        """Same as :meth:`read`, as bytes -- for archiving or external decoding."""
        available = self.num_samples()
        wanted = available - offset if num_items is None else min(
            num_items, available - offset
        )
        if wanted <= 0:
            return b""
        buffer = (TraceData * wanted)()
        count = ctypes.c_uint32(wanted)
        rc = self._lib.JLINKARM_TRACE_Read(
            ctypes.byref(buffer), offset, ctypes.byref(count)
        )
        if rc < 0:
            raise TraceError(f"TRACE_Read at offset {offset} failed", rc)
        return bytes(buffer)[: count.value * ctypes.sizeof(TraceData)]

    # -- injection ---------------------------------------------------------

    def add_instruction(self, address: int, branch_address: int) -> None:
        """Feed the DLL's decoder a known instruction/branch pair."""
        rc = self._lib.JLINKARM_TRACE_AddInst(address, branch_address)
        if rc < 0:
            raise TraceError("TRACE_AddInst failed", rc)

    def add_items(self, items: bytes) -> None:
        """Push raw trace items into the DLL's buffer, as if captured.

        Exists for replaying an archived capture through the DLL's own decoder
        without a probe attached.
        """
        size = ctypes.sizeof(TraceData)
        if len(items) % size:
            raise TraceError(f"Trace item data must be a multiple of {size} bytes")
        buffer = (ctypes.c_uint8 * len(items)).from_buffer_copy(items)
        rc = self._lib.JLINKARM_TRACE_AddItems(
            ctypes.byref(buffer), len(items) // size
        )
        if rc < 0:
            raise TraceError("TRACE_AddItems failed", rc)


class RawTrace:
    """RAWTRACE -- undecoded trace-port bytes. Reached via ``JLink.raw_trace``.

    Values are `observed` from the shipped library; the DLL
    bounds Cmd at 4.
    """

    def __init__(self, link: "JLink") -> None:
        self._link = link
        self._lib = link.raw

    def control(self, cmd: int, data: object = None) -> int:
        pointer = ctypes.byref(data) if data is not None else None  # type: ignore[arg-type]
        rc = self._lib.JLINKARM_RAWTRACE_Control(int(cmd), pointer)
        if rc < 0:
            raise TraceError(f"RAWTRACE_Control({int(cmd)}) failed", rc)
        return rc

    def start(self) -> None:
        from .constants import RawTraceCmd

        self.control(RawTraceCmd.START)

    def stop(self) -> None:
        from .constants import RawTraceCmd

        self.control(RawTraceCmd.STOP)

    def trace_frequency_hz(self) -> int:
        from .constants import RawTraceCmd

        value = ctypes.c_uint32(0)
        self.control(RawTraceCmd.GET_TRACE_FREQ, value)
        return value.value

    def capabilities(self) -> int:
        from .constants import RawTraceCmd

        value = ctypes.c_uint32(0)
        self.control(RawTraceCmd.GET_CAPS, value)
        return value.value

    def set_buffer_size(self, num_bytes: int) -> None:
        from .constants import RawTraceCmd

        value = ctypes.c_uint32(num_bytes)
        self.control(RawTraceCmd.SET_BUFF_SIZE, value)

    def read(self, num_bytes: int) -> bytes:
        buffer = (ctypes.c_uint8 * num_bytes)()
        count = self._lib.JLINKARM_RAWTRACE_Read(
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8)), num_bytes
        )
        if count < 0:
            raise TraceError("RAWTRACE_Read failed", count)
        return bytes(buffer)[:count]


__all__ = ["RawTrace", "Region", "TraceBuffer"]
