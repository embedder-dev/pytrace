"""pytrace -- a Python SDK for SEGGER J-Link and J-Trace.

Covers the J-Link DLL: target control, memory, registers, breakpoints,
instruction trace (ETM), the raw trace buffer, SWO/ITM, RTT, high-speed
sampling, power trace and CoreSight. Captures can be written straight to disk
in a layout a trace viewer can open -- see :mod:`jtrace.artifacts`.

Quick start::

    from jtrace import JLink, capture_instruction_trace

    # Drive the target directly
    with JLink(device="STM32F407VG", interface="SWD", speed_khz=4000) as jl:
        jl.reset()
        jl.halt()
        print(hex(jl.read_u32(0x08000000)))
        print(jl.register_dump())

    # Or capture a trace and write it where a viewer will find it
    session_id, result = capture_instruction_trace(
        "firmware.elf", "STM32F407VG", duration_ms=3000
    )
    print(session_id, result.summary.instruction_count)

Nothing here needs a probe until you open one, so ELF parsing, symbolization,
coverage row building and artifact writing all work on a machine with no
hardware attached.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .artifacts import (
    CoverageReportMeta,
    InstructionRow,
    InstructionSummary,
    TraceSessionMeta,
    coverage_dir,
    list_coverage_reports,
    list_trace_sessions,
    make_coverage_report_id,
    make_trace_session_id,
    read_trace_session,
    traces_dir,
    write_coverage_report,
    write_trace_session,
)
from .capture import (
    CaptureOptions,
    CaptureResult,
    capture_coverage,
    capture_instruction_trace,
    run_capture,
    to_chronological,
)
from .constants import (
    DEFAULT_TRACE_BUFFER_BYTES,
    DEFAULT_TRACE_CAPACITY,
    MAX_STRACE_ITEMS,
    AccessSize,
    BreakpointType,
    Interface,
    PowerTraceCmd,
    RawTraceCmd,
    ResetType,
    RttCmd,
    StraceCmd,
    StraceEventType,
    StraceOperation,
    SwoCmd,
    SwoInterface,
    TraceCmd,
    TraceFormat,
    TraceSource,
)
from .coverage import (
    CoverageRows,
    FunctionRow,
    LineRow,
    SectionCounts,
    Totals,
    build_coverage_rows,
    compute_totals,
)
from .elf import ElfFile, Section, Symbol
from .errors import (
    JLinkError,
    LibraryNotFoundError,
    NotConnectedError,
    SymbolizationError,
    TraceError,
)
from .frames import CallFrame, FrameResult, build_call_frames
from .link import JLink, ProbeInfo, Register
from .loader import find_library, is_available, load
from .rows import InstructionRows, InstructionRun
from .store import CycleEstimate, StoreStats, TraceBlock, TraceStore
from .symbols import ResolvedFunction, ResolvedSource, Span, Symbolizer
from .thumb import instruction_starts, is_thumb32

__all__ = [
    "AccessSize",
    "BreakpointType",
    "CallFrame",
    "CaptureOptions",
    "CaptureResult",
    "CoverageReportMeta",
    "CoverageRows",
    "CycleEstimate",
    "DEFAULT_TRACE_BUFFER_BYTES",
    "DEFAULT_TRACE_CAPACITY",
    "ElfFile",
    "FrameResult",
    "FunctionRow",
    "InstructionRow",
    "InstructionRows",
    "InstructionRun",
    "InstructionSummary",
    "Interface",
    "JLink",
    "JLinkError",
    "LibraryNotFoundError",
    "LineRow",
    "MAX_STRACE_ITEMS",
    "NotConnectedError",
    "PowerTraceCmd",
    "ProbeInfo",
    "RawTraceCmd",
    "Register",
    "ResetType",
    "ResolvedFunction",
    "ResolvedSource",
    "RttCmd",
    "Section",
    "SectionCounts",
    "Span",
    "StoreStats",
    "StraceCmd",
    "StraceEventType",
    "StraceOperation",
    "SwoCmd",
    "SwoInterface",
    "Symbol",
    "SymbolizationError",
    "Symbolizer",
    "Totals",
    "TraceBlock",
    "TraceCmd",
    "TraceError",
    "TraceFormat",
    "TraceSessionMeta",
    "TraceSource",
    "TraceStore",
    "__version__",
    "build_call_frames",
    "build_coverage_rows",
    "capture_coverage",
    "capture_instruction_trace",
    "compute_totals",
    "coverage_dir",
    "find_library",
    "instruction_starts",
    "is_available",
    "is_thumb32",
    "list_coverage_reports",
    "list_trace_sessions",
    "load",
    "make_coverage_report_id",
    "make_trace_session_id",
    "read_trace_session",
    "run_capture",
    "to_chronological",
    "traces_dir",
    "write_coverage_report",
    "write_trace_session",
]
