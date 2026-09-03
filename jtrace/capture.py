"""One-call instruction-trace and coverage capture.

The call sequence here matches the one a working capture worker performs, in
the same order, and the order is not incidental:

1. open the probe, suppress dialogs, select device/interface/speed, connect
2. reset, halt
3. ``ReadIntoTraceCache`` every executable section -- **before** starting trace
4. read the code image back, for the Thumb instruction-boundary walk
5. size the buffer, configure the port width, start trace
6. go
7. ... let it run ...
8. halt, read instruction statistics, read the trace buffer
9. stop trace, close

Step 3 is the make-or-break one. Without it the DLL cannot expand ETM's
branch/sync points back into a full instruction stream, and every subsequent
count is silently wrong rather than obviously empty.

Once the probe is open it belongs to this process until the capture finishes or
tears down. Every failure path below closes it, because a J-Link left open
wedges every later flash, debug session and RTT connection on the machine.
"""

from __future__ import annotations

import array
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .artifacts import (
    CoverageReportMeta,
    InstructionRow,
    InstructionSummary,
    TraceSessionMeta,
    make_trace_session_id,
    sha256_file,
    write_coverage_report,
    write_trace_session,
)
from .constants import (
    DEFAULT_DURATION_MS,
    DEFAULT_TRACE_CAPACITY,
    DEFAULT_PORT_WIDTH,
    DEFAULT_SPEED_KHZ,
    DEFAULT_TRACE_BUFFER_BYTES,
    HALFWORD_BYTES,
    MAX_STRACE_ITEMS,
)
from .coverage import (
    CoverageRows,
    SectionCounts,
    build_coverage_rows,
    compute_totals,
    run_count_lookup,
)
from .elf import ElfFile
from .errors import TraceError
from .frames import CallFrame, build_call_frames
from .link import JLink
from .rows import InstructionRows
from .store import TraceStore
from .strace import StreamingStats
from .symbols import Symbolizer
from .thumb import instruction_starts

ProgressFn = Callable[[str], None]


@dataclass
class CaptureOptions:
    elf_path: str | Path
    device: str
    interface: str = "SWD"
    speed_khz: int = DEFAULT_SPEED_KHZ
    serial_number: int | None = None
    port_width: int = DEFAULT_PORT_WIDTH
    duration_ms: int = DEFAULT_DURATION_MS
    trace_items: int = MAX_STRACE_ITEMS
    buffer_bytes: int = DEFAULT_TRACE_BUFFER_BYTES
    capacity: int = DEFAULT_TRACE_CAPACITY
    """Host-side ceiling on retained instructions. Past it the oldest whole
    block is dropped, so a long capture degrades to its tail instead of 
    growing until the process dies."""
    cpu_freq_hz: int | None = None
    label: str | None = None
    board_name: str | None = None
    log_path: str | Path | None = None
    build_rows: bool = True
    """Per-function and per-line rows walk every instruction start in the image
    twice. A trace-only capture throws the result away, so it does not pay."""

    read_trace: bool = True
    """Read the PC window back. Coverage alone does not need it."""

    on_progress: ProgressFn | None = None


@dataclass
class CaptureResult:
    """Everything a capture produced, before anything is written to disk."""

    instructions: Sequence[InstructionRow]
    """Chronological: index 0 is the oldest instruction still in the window.

    A lazy view over :attr:`store` -- it indexes, slices, iterates and compares
    like the list it replaces, but the rows are built on access rather than
    held. See :class:`jtrace.rows.InstructionRows`.
    """

    frames: list[CallFrame]
    summary: InstructionSummary
    rows: CoverageRows | None
    section_counts: list[SectionCounts]
    instructions_executed: int
    buffer_bytes: int
    duration_ms: int
    elf_path: str
    device: str
    store: TraceStore | None = None
    """The raw program counters, segmented into one block per uninterrupted run.

    The source of truth the rows are generated from. Keeping it is what makes a
    capture re-symbolizable against a different ELF, and what lets
    :meth:`~jtrace.store.TraceStore.boundaries` tell the call-frame builder
    where the stream is discontinuous.
    """

    streaming: StreamingStats | None = None
    """Set only for a long capture. ``streaming.is_continuous`` is the thing to
    check: a capture with gaps has holes the call frames cannot see."""

    @property
    def totals(self):
        return compute_totals(self.rows.functions) if self.rows else None


def symbolize_stream(
    addresses: Sequence[int],
    symbolizer: Symbolizer,
    run_count_at: Callable[[int], int | None],
) -> InstructionRows:
    """Wrap an already-chronological address stream as instruction rows.

    Returns a view rather than a list: the rows are a pure function of the
    addresses and the ELF, so materialising all of them costs two orders of
    magnitude more memory than the stream and buys nothing a caller cannot get
    by indexing.
    """
    return InstructionRows(addresses, symbolizer, run_count_at)


def to_chronological(
    trace: Sequence[int],
    symbolizer: Symbolizer,
    run_count_at: Callable[[int], int | None],
) -> InstructionRows:
    """Reverse the probe's newest-first buffer, then symbolize it.

    Indices are assigned after reversing, so index 0 is the oldest instruction
    still in the window. :meth:`Strace.read_extended` already returns
    chronological order and goes through :func:`symbolize_stream` instead.
    """
    reversed_stream = array.array("I")
    reversed_stream.extend(reversed(trace if isinstance(trace, Sequence) else list(trace)))
    return symbolize_stream(reversed_stream, symbolizer, run_count_at)


def run_capture(options: CaptureOptions) -> CaptureResult:
    """Run one ETM capture and return everything it produced.

    Writes nothing. :func:`capture_instruction_trace` and
    :func:`capture_coverage` layer the artifact writing on top, so a caller
    that wants the data without the side effects can stop here.

    Named ``run_capture`` rather than ``capture`` so that ``jtrace.capture``
    unambiguously means this module. A package attribute that is sometimes a
    module and sometimes a function depending on import order is exactly the
    kind of thing a generated script trips over.
    """
    report = options.on_progress or (lambda _message: None)
    elf = ElfFile(options.elf_path)
    sections = elf.executable_sections()
    if not sections:
        raise TraceError(
            f"No executable sections found in {options.elf_path}. "
            f"Coverage needs a firmware ELF with a .text section."
        )

    streaming = StreamingStats()
    report("Connecting to the target and priming the trace cache...")
    with JLink(
        device=options.device,
        interface=options.interface,
        speed_khz=options.speed_khz,
        serial_number=options.serial_number,
        log_path=options.log_path,
    ) as link:
        strace = link.strace

        link.reset(halt=True)
        link.halt()

        for section in sections:
            link.read_into_trace_cache(section.addr, section.size)
        code = [link.read_memory(section.addr, section.size) for section in sections]

        strace.set_buffer_size(options.buffer_bytes)
        strace.configure_port(options.port_width)
        strace.start()
        link.go()

        try:
            long_capture = options.read_trace and options.trace_items > MAX_STRACE_ITEMS
            if long_capture:
                # Past the single-read clamp the target has to be run in
                # slices, draining the buffer each time -- see
                # Strace.read_extended. The slice loop *is* the run phase, so
                # duration_ms becomes a wall-clock cap rather than a sleep.
                report(
                    f"Capturing up to {options.trace_items:,} instructions "
                    f"in slices..."
                )
                store = strace.read_extended(
                    sections[0].addr,
                    target_items=options.trace_items,
                    deadline_s=options.duration_ms / 1000,
                    stats=streaming,
                    on_progress=_slice_progress(report),
                    # The bound must never quietly undercut an explicit ask.
                    # Headroom of one maximal slice on top of the target: the
                    # run overshoots before it can stop, and without the slack
                    # that overshoot evicts the oldest instructions the caller
                    # asked to keep.
                    capacity=max(
                        options.capacity, options.trace_items + MAX_STRACE_ITEMS
                    ),
                )
            else:
                _run_for(options.duration_ms, link, sections, report)
                store = None

            report("Halting the target and reading final coverage...")
            link.halt()

            counts = [
                strace.instruction_counts(
                    section.addr, section.size // HALFWORD_BYTES
                )
                for section in sections
            ]
            executed = strace.total_executed(sections[0].addr)
            if store is None:
                window = (
                    strace.read(options.trace_items)
                    if options.read_trace and options.trace_items > 0
                    else array.array("I")
                )
                # The probe hands the buffer back newest-first. One read is one
                # uninterrupted run, so it is one block.
                chronological = array.array("I")
                chronological.extend(reversed(window))
                store = TraceStore(capacity=max(options.capacity, len(chronological)))
                if len(chronological):
                    store.append_block(chronological)
        finally:
            strace.stop()

    report("Symbolizing and reconstructing the call stack...")
    symbolizer = Symbolizer(elf)
    starts = (
        [instruction_starts(code[i], section.addr) for i, section in enumerate(sections)]
        if options.build_rows
        else [() for _ in sections]
    )
    section_counts = [
        SectionCounts(
            base_address=section.addr,
            instruction_starts=starts[i],
            counts=counts[i],
        )
        for i, section in enumerate(sections)
    ]

    rows = build_coverage_rows(section_counts, symbolizer) if options.build_rows else None
    instructions = symbolize_stream(
        store, symbolizer, run_count_lookup(section_counts)
    )
    # Straight off the store: the frame builder only ever needed addresses, and
    # routing them through the rows was what forced every row to exist.
    frame_result = build_call_frames(
        store, symbolizer, boundaries=store.boundaries()
    )

    summary = InstructionSummary(
        instruction_count=len(instructions),
        instructions_executed=executed,
        frame_count=len(frame_result.frames),
        max_depth=frame_result.max_depth,
        cpu_freq_hz=options.cpu_freq_hz,
        # The buffer is a ring: if the run executed more than it holds, what
        # came back is the tail of execution, not the whole of it.
        window_truncated=executed > len(instructions),
    )

    return CaptureResult(
        instructions=instructions,
        store=store,
        frames=frame_result.frames,
        summary=summary,
        rows=rows,
        section_counts=section_counts,
        instructions_executed=executed,
        buffer_bytes=options.buffer_bytes,
        duration_ms=options.duration_ms,
        elf_path=str(options.elf_path),
        device=options.device,
        streaming=streaming if streaming.polls else None,
    )


def _slice_progress(report: ProgressFn) -> Callable[[int, int], None]:
    """Report slice progress about once a second, not once per slice.

    A slice is a couple of milliseconds; reporting each one would emit
    thousands of lines for a capture the caller asked to be long.
    """
    state = {"next": 0.0}

    def on_progress(collected: int, target: int) -> None:
        now = time.monotonic()
        if now < state["next"]:
            return
        state["next"] = now + 1.0
        report(f"Captured {collected:,} of {target:,} instructions...")

    return on_progress


def _run_for(
    duration_ms: int, link: JLink, sections, report: ProgressFn
) -> None:
    """Let the target run, reporting progress every ten seconds."""
    deadline = time.monotonic() + duration_ms / 1000
    next_report = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        if time.monotonic() >= next_report:
            next_report = time.monotonic() + 10
            remaining = max(0, int(deadline - time.monotonic()) + 1)
            sampled = link.strace.total_executed(sections[0].addr)
            report(
                f"Capturing trace ({remaining}s left, "
                f"{sampled:,} instructions so far)..."
            )


def capture_instruction_trace(
    elf_path: str | Path,
    device: str,
    *,
    session_root: str | Path | None = None,
    **kwargs,
) -> tuple[str, CaptureResult]:
    """Capture an instruction trace and write it where the Trace tab reads.

    Returns ``(session_id, result)``. The session appears in the Trace tab's
    session list immediately -- no CLI restart, no import step.

        session_id, result = capture_instruction_trace(
            "firmware.elf", "STM32F407VG", duration_ms=3000
        )
    """
    options = CaptureOptions(
        elf_path=elf_path,
        device=device,
        # Rows would be built and discarded, and building them walks every
        # instruction start in the image.
        build_rows=kwargs.pop("build_rows", False),
        **kwargs,
    )
    result = run_capture(options)

    if not result.instructions:
        raise TraceError(
            "The trace buffer came back empty. Check that the J-Trace is "
            "connected over the fine-pitch CoreSight-20 cable and that the "
            "target was running."
        )

    session_id = make_trace_session_id()
    write_trace_session(
        result.instructions,
        result.frames,
        result.summary,
        TraceSessionMeta(
            device=device,
            elf_path=str(elf_path),
            elf_sha256=sha256_file(elf_path),
            trace_items=options.trace_items,
            buffer_bytes=result.buffer_bytes,
            duration_ms=result.duration_ms,
            instruction_count=len(result.instructions),
            board_name=options.board_name,
            serial_number=options.serial_number,
            speed_khz=options.speed_khz,
            port_width=options.port_width,
            target_interface=options.interface,
            cpu_freq_hz=options.cpu_freq_hz,
            label=options.label,
        ),
        session_id=session_id,
        root=session_root,
        store=result.store,
    )
    return session_id, result


def capture_coverage(
    elf_path: str | Path,
    device: str,
    *,
    session_id: str = "default",
    project_root: str | Path | None = None,
    **kwargs,
) -> tuple[Path, CaptureResult]:
    """Capture coverage and write a report the Coverage tab will list.

    Returns ``(report_path, result)``.
    """
    options = CaptureOptions(
        elf_path=elf_path,
        device=device,
        build_rows=True,
        read_trace=kwargs.pop("read_trace", False),
        **kwargs,
    )
    result = run_capture(options)
    assert result.rows is not None  # build_rows=True above

    path = write_coverage_report(
        result.rows,
        CoverageReportMeta(
            elf_path=str(elf_path),
            device=device,
            duration_sec=result.duration_ms / 1000,
            label=options.label,
        ),
        session_id=session_id,
        project_root=project_root,
    )
    return path, result


__all__ = [
    "CaptureOptions",
    "CaptureResult",
    "capture_coverage",
    "capture_instruction_trace",
    "run_capture",
    "to_chronological",
]
