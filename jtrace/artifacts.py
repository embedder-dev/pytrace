"""Writing captures to disk in a viewer-readable layout.

A capture that stays in Python is a number on a terminal. Written out through
this module it becomes a session a trace viewer can open, with a timeline, a
call-stack lane and a coverage table.

The layout is not a convention this module invented: it is the on-disk format
of the Embedder trace and coverage viewer, whose reader validates every file it
loads against a schema. A field with the wrong name or the wrong type does not
degrade -- the session is skipped and the viewer shows nothing -- which is why
the shapes below are pinned by tests rather than left to drift.

You do not have to use that viewer, and by default you are not writing for it:
:data:`STORE_DIRNAME` is a neutral ``.pytrace``, which is not where the viewer
looks. Pass ``store_dirname=".embedder"`` to write where it does.

That and :data:`TRACE_FORMAT_INSTRUCTION` are the two values that tie output to
a particular reader, and every function here takes an override, so the same
writer can lay a capture down under any directory name you like.

Trace sessions::

    <root>/<store_dirname>/traces/<sessionId>/
        sidecar.json        session metadata; the viewer lists what it finds here
        instructions.json   the PC stream, its call frames, and a summary
        events.ndjson       empty for hardware captures, but must exist
        index.json          empty index, same reason
        raw/stream.bin      empty, same reason
        raw/trace.jt1       the program counters themselves, when a store is given

Coverage reports::

    <root>/<store_dirname>/coverage/<sessionId>/<reportId>/report.json
"""

from __future__ import annotations

import hashlib
import json
import random
import string
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import __version__
from .coverage import CoverageRows, compute_totals
from .frames import CallFrame
from .store import TraceStore

TRACE_INSTRUCTION_PAYLOAD_VERSION = 1
TRACE_SESSION_INDEX_VERSION = 1
TRACE_FORMAT_INSTRUCTION = "embedder-trace-instruction-v1"
"""Format identifier written into every sidecar.

One of the two values that tie output to a particular viewer. Pass
``trace_format=`` to :func:`write_trace_session` to write something else; the
reader that consumes this layout keys on it, so change it only if you are also
the one reading it back.
"""

STORE_DIRNAME = ".pytrace"
"""Directory, under the project root, that holds traces and coverage reports.

The other coupling point, and the default is deliberately not the Embedder
viewer's: that one reads ``.embedder``, so writing for it means passing
``store_dirname=".embedder"``. Every function that resolves a path takes the
override, so neither name is imposed on a caller who wants the other.
"""
DEFAULT_MAX_FUNCTION_ROWS = 2000
"""The store truncates the stored slice here, but totals still cover every row."""

TRACE_TAB_COMFORTABLE_ROWS = 250_000
"""Above this, warn that the session may be heavy for the Trace tab.

Not a limit -- nothing here refuses to write a bigger one, and for offline
analysis bigger is fine. But instructions.json is read and schema-validated
whole on every load, and it grows about 170 bytes per row: 65,536 rows is
~11 MB, a million is ~166 MB. Somewhere past this the tab stops being
interactive, so producing one silently would be setting a trap.
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """ISO-8601 with a trailing Z, which is what the TypeScript side writes."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{moment.microsecond // 1000:03d}Z"
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _suffix(length: int = 4) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def make_trace_session_id(moment: datetime | None = None) -> str:
    """A session id of the form ``etm-<UTC timestamp>-<random suffix>``.

    The random suffix is what keeps two captures started in the same second
    from colliding, which a nightly job running several boards in parallel will
    otherwise do.
    """
    return f"etm-{_stamp(moment or _now())}-{_suffix()}"


def make_coverage_report_id(moment: datetime | None = None) -> str:
    """A report id of the form ``cov_<UTC timestamp>_<random suffix>``.

    Underscore-separated where the trace id is hyphen-separated, because the
    two identifier styles are what the consuming viewer already expects.
    """
    return f"cov_{_stamp(moment or _now())}_{_suffix()}"


def sha256_file(path: str | Path) -> str:
    """Hash an ELF for the sidecar. All zeroes when unreadable.

    Matches the TypeScript producer, which does the same rather than failing a
    completed capture over a file that moved between capture and write.
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "0" * 64


def traces_dir(
    root: str | Path | None = None, *, store_dirname: str = STORE_DIRNAME
) -> Path:
    """Where trace sessions live, under ``root`` (default: the process cwd).

    The Embedder viewer resolves this from its own working directory, so a
    capture written anywhere else is invisible to it no matter how well-formed
    it is. Pass ``root`` explicitly when your script does not run from the
    project directory.
    """
    base = Path(root) if root is not None else Path.cwd()
    return base / store_dirname / "traces"


def coverage_dir(
    project_root: str | Path | None = None, *, store_dirname: str = STORE_DIRNAME
) -> Path:
    """Where coverage reports live, under ``project_root`` (default: cwd)."""
    base = Path(project_root) if project_root is not None else Path.cwd()
    return base / store_dirname / "coverage"


@dataclass(slots=True)
class InstructionRow:
    """One executed instruction, in stream order.

    Index 0 is the oldest instruction in the capture window: the probe hands
    the buffer back newest-first and the producer reverses it, so everything
    downstream reads forwards in time.
    """

    index: int
    address: int
    function: str | None = None
    file: str | None = None
    line: int | None = None
    run_count: int | None = None
    """How often this address executed across the whole capture, from the same
    instruction-statistics read that produces coverage. A property of the
    address, not of this occurrence."""

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"index": self.index, "address": self.address}
        if self.function is not None:
            out["function"] = self.function
        if self.file is not None:
            out["file"] = self.file
        if self.line is not None:
            out["line"] = self.line
        if self.run_count is not None:
            out["runCount"] = self.run_count
        return out


@dataclass
class InstructionSummary:
    instruction_count: int
    """Instructions in the readable window, not in the whole run."""

    instructions_executed: int
    """Total executed over the capture. Usually far larger than the window."""

    frame_count: int
    max_depth: int
    window_truncated: bool
    """The run produced more instructions than the buffer holds, so the window
    is the tail of execution rather than all of it."""

    cpu_freq_hz: int | None = None
    """Turns an instruction index into an approximate time offset. Absent means
    the Trace tab's axis is ordinal only."""

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "instructionCount": self.instruction_count,
            "instructionsExecuted": self.instructions_executed,
            "frameCount": self.frame_count,
            "maxDepth": self.max_depth,
            "windowTruncated": self.window_truncated,
        }
        if self.cpu_freq_hz:
            out["cpuFreqHz"] = self.cpu_freq_hz
        return out


@dataclass
class TraceSessionMeta:
    """Everything the sidecar needs that is not derivable from the stream."""

    device: str
    elf_path: str
    elf_sha256: str
    trace_items: int
    buffer_bytes: int
    duration_ms: int
    instruction_count: int
    board_name: str | None = None
    serial_number: int | None = None
    speed_khz: int | None = None
    port_width: int | None = None
    target_interface: str | None = None
    cpu_freq_hz: int | None = None
    label: str | None = None


def build_sidecar(
    session_id: str,
    meta: TraceSessionMeta,
    *,
    trace_format: str = TRACE_FORMAT_INSTRUCTION,
) -> dict[str, object]:
    """The sidecar for a hardware instruction capture.

    Pure, so its shape can be asserted without a probe.
    """
    ended_at = _now()
    started_at = datetime.fromtimestamp(
        ended_at.timestamp() - meta.duration_ms / 1000, tz=timezone.utc
    )

    transport: dict[str, object] = {
        "kind": "etm",
        "traceItems": meta.trace_items,
        "bufferBytes": meta.buffer_bytes,
    }
    if meta.port_width is not None:
        transport["portWidth"] = meta.port_width
    if meta.target_interface is not None:
        transport["targetInterface"] = meta.target_interface
    if meta.speed_khz is not None:
        transport["speedKhz"] = meta.speed_khz
    if meta.cpu_freq_hz:
        transport["cpuFreqHz"] = meta.cpu_freq_hz

    probe: dict[str, object] = {"probeKind": "jlink"}
    if meta.serial_number is not None:
        probe["serial"] = str(meta.serial_number)
    if meta.speed_khz is not None:
        probe["speedKhz"] = meta.speed_khz

    sidecar: dict[str, object] = {
        "sessionId": session_id,
        "sidecarVersion": {"major": 1, "minor": 0},
        "traceFormat": trace_format,
        "state": "stopped",
        "startedAt": _iso(started_at),
        "endedAt": _iso(ended_at),
        "board": {
            "boardId": meta.device,
            "boardName": meta.board_name or meta.device,
        },
        "probe": probe,
        "transport": transport,
        "captureSettings": {
            "bufferingPolicy": "drop_oldest",
            # What the DLL was actually given, not traceItems * 4 -- the two are
            # unrelated, and the derived figure was 64x under the real ring size.
            "bufferBytes": meta.buffer_bytes,
            "featureFlags": ["etm_instruction_trace", "instruction_statistics"],
            # Omitted rather than zeroed when there was no duration cap: the
            # schema types this positive-and-optional, so writing 0 produces a
            # sidecar that fails validation and a session the tab silently
            # skips. Absent means "no cap", which is what 0 meant anyway.
            **({"maxDurationMs": meta.duration_ms} if meta.duration_ms > 0 else {}),
        },
        "firmware": {
            "elfPath": str(Path(meta.elf_path).resolve()),
            "elfSha256": meta.elf_sha256,
        },
        "counters": {
            "droppedRecords": 0,
            "resyncCount": 0,
            "errorCount": 0,
            "bytesIngested": meta.instruction_count * 4,
            "eventCount": meta.instruction_count,
        },
        # Every address in the stream was resolved against this exact ELF during
        # the capture, so the state is known here and never needs re-deriving.
        # Without it the store back-fills "Sidecar predates symbolization
        # tracking", which drops provenance confidence to "partial" and leaves
        # the Trace tab showing a permanent "Attach ELF..." banner on a session
        # whose symbols are already resolved.
        "symbolization": {
            "state": "exact",
            "elfSha256": meta.elf_sha256,
            "attachedAt": _iso(ended_at),
        },
        "tool": {"name": "pytrace", "version": __version__},
    }
    if meta.label:
        sidecar["notes"] = meta.label
    return sidecar


def _empty_index() -> dict[str, object]:
    return {
        "indexVersion": TRACE_SESSION_INDEX_VERSION,
        "eventCount": 0,
        "timeRangeNs": None,
        "byKind": {},
        "byThreadId": {},
        "byObjectId": {},
        "byObjectClass": {},
        "byUserChannel": {},
        "byCpu": {},
        "minuteBuckets": [],
    }


def _write_instruction_payload(
    path: Path,
    instructions: Sequence[InstructionRow],
    frames: Sequence[CallFrame],
    summary: InstructionSummary,
) -> None:
    """Stream the payload out one row at a time.

    Byte-for-byte what ``json.dumps`` of the whole document produces -- the
    default separators are ``", "`` and ``": "``, which is what is reproduced
    here, and there is a test pinning the two against each other. The reason
    not to just call ``json.dumps`` is that it builds the entire document, and
    then its entire serialisation, in memory first: for a million-row session
    that is several hundred megabytes of peak usage to write a file the reader
    is going to stream anyway.

    The rows themselves come from a lazy view, so this is the only place the
    whole capture is ever walked, and it never holds more than one row.
    """
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            '{"payloadVersion": '
            f"{json.dumps(TRACE_INSTRUCTION_PAYLOAD_VERSION)}"
            ', "summary": '
            f"{json.dumps(summary.to_json())}"
            ', "instructions": ['
        )
        for index, row in enumerate(instructions):
            if index:
                handle.write(", ")
            handle.write(json.dumps(row.to_json()))
        handle.write('], "frames": [')
        for index, frame in enumerate(frames):
            if index:
                handle.write(", ")
            handle.write(json.dumps(frame.to_json()))
        handle.write("]}")


def write_trace_session(
    instructions: Sequence[InstructionRow],
    frames: Sequence[CallFrame],
    summary: InstructionSummary,
    meta: TraceSessionMeta,
    *,
    session_id: str | None = None,
    root: str | Path | None = None,
    store: TraceStore | None = None,
    store_dirname: str = STORE_DIRNAME,
    trace_format: str = TRACE_FORMAT_INSTRUCTION,
) -> Path:
    """Write a complete instruction-trace session. Returns its directory.

    The empty ``events.ndjson``, ``index.json`` and ``raw/stream.bin`` are not
    decoration: the store creates them for every session it opens and its
    readers stat them, so a session missing them reads as corrupt rather than
    as empty.

    ``store`` additionally writes ``raw/trace.jt1``, a compressed binary
    snapshot of the program counters themselves. It is a new file rather than a
    reuse of the empty ``raw/stream.bin``, whose meaning to the store's own
    readers is not known here, and nothing validates it, so it cannot make a
    session fail to load. It is what lets the capture be re-symbolized later
    against a different ELF -- the JSON only ever held the rows, which are a
    lossy projection of the stream.
    """
    session = session_id or make_trace_session_id()
    directory = traces_dir(root, store_dirname=store_dirname) / session
    (directory / "raw").mkdir(parents=True, exist_ok=True)

    if len(instructions) > TRACE_TAB_COMFORTABLE_ROWS:
        warnings.warn(
            f"Writing {len(instructions):,} instruction rows "
            f"(~{len(instructions) * 170 / 1e6:.0f} MB). The Trace tab reads and "
            f"validates this file whole on every load, so it may be slow to open. "
            f"Captures above ~{TRACE_TAB_COMFORTABLE_ROWS:,} rows are better "
            f"analysed from the CaptureResult in Python than in the tab.",
            stacklevel=2,
        )

    _write_instruction_payload(
        directory / "instructions.json", instructions, frames, summary
    )
    (directory / "sidecar.json").write_text(
        json.dumps(
            build_sidecar(session, meta, trace_format=trace_format), indent="\t"
        )
    )
    (directory / "index.json").write_text(json.dumps(_empty_index(), indent="\t"))
    (directory / "events.ndjson").write_text("")
    (directory / "raw" / "stream.bin").write_bytes(b"")
    if store is not None:
        store.save(
            directory / "raw" / "trace.jt1",
            meta={
                "sessionId": session,
                "elfPath": str(Path(meta.elf_path).resolve()),
                "elfSha256": meta.elf_sha256,
                "device": meta.device,
            },
        )
    return directory


def list_trace_sessions(
    root: str | Path | None = None, *, store_dirname: str = STORE_DIRNAME
) -> list[dict[str, object]]:
    """Every readable session sidecar, newest first."""
    directory = traces_dir(root, store_dirname=store_dirname)
    if not directory.is_dir():
        return []
    out: list[dict[str, object]] = []
    for entry in directory.iterdir():
        sidecar = entry / "sidecar.json"
        if not entry.is_dir() or not sidecar.is_file():
            continue
        try:
            out.append(json.loads(sidecar.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda item: str(item.get("startedAt", "")), reverse=True)
    return out


def read_trace_session(
    session_id: str,
    root: str | Path | None = None,
    *,
    store_dirname: str = STORE_DIRNAME,
) -> dict[str, object] | None:
    """Read back a session's sidecar and instruction payload."""
    directory = traces_dir(root, store_dirname=store_dirname) / session_id
    sidecar = directory / "sidecar.json"
    payload = directory / "instructions.json"
    if not sidecar.is_file():
        return None
    result: dict[str, object] = {"sidecar": json.loads(sidecar.read_text())}
    if payload.is_file():
        result["instructions"] = json.loads(payload.read_text())
    return result


@dataclass
class CoverageReportMeta:
    elf_path: str
    device: str
    duration_sec: float
    label: str | None = None
    trace_source: str = "etm"
    partial: bool = False
    """Reserved for the on-chip-buffer path, where the window may not cover the
    whole run. A streaming ETM capture sees everything, so this stays false."""


def write_coverage_report(
    rows: CoverageRows,
    meta: CoverageReportMeta,
    *,
    session_id: str,
    report_id: str | None = None,
    project_root: str | Path | None = None,
    max_function_rows: int = DEFAULT_MAX_FUNCTION_ROWS,
    store_dirname: str = STORE_DIRNAME,
) -> Path:
    """Write a coverage report the Coverage tab will list. Returns its path."""
    report = report_id or make_coverage_report_id()
    directory = (
        coverage_dir(project_root, store_dirname=store_dirname)
        / session_id
        / report
    )
    directory.mkdir(parents=True, exist_ok=True)

    truncated = len(rows.functions) > max_function_rows
    stored_functions = rows.functions[:max_function_rows] if truncated else rows.functions

    stored = {
        "report": {
            "reportId": report,
            "sessionId": session_id,
            "createdAt": _iso(_now()),
            "label": meta.label,
            "elfPath": str(Path(meta.elf_path).resolve()),
            "device": meta.device,
            "traceSource": meta.trace_source,
            "partial": meta.partial,
            "durationSec": meta.duration_sec,
            "hasLineRows": len(rows.lines) > 0,
            "hasTrace": False,
            # From every row, not the stored slice.
            "totals": compute_totals(rows.functions).to_json(),
        },
        "functions": [row.to_json() for row in stored_functions],
        "functionsTruncated": truncated,
        "lines": [row.to_json() for row in rows.lines],
    }

    path = directory / "report.json"
    path.write_text(json.dumps(stored, indent="\t"))
    return path


def list_coverage_reports(
    project_root: str | Path | None = None, *, store_dirname: str = STORE_DIRNAME
) -> list[dict[str, object]]:
    """Every readable coverage report, newest first."""
    directory = coverage_dir(project_root, store_dirname=store_dirname)
    if not directory.is_dir():
        return []
    out: list[dict[str, object]] = []
    for session in directory.iterdir():
        if not session.is_dir():
            continue
        for report in session.iterdir():
            path = report / "report.json"
            if not path.is_file():
                continue
            try:
                out.append(json.loads(path.read_text())["report"])
            except (OSError, KeyError, json.JSONDecodeError):
                continue
    out.sort(key=lambda item: str(item.get("createdAt", "")), reverse=True)
    return out


__all__ = [
    "STORE_DIRNAME",
    "TRACE_FORMAT_INSTRUCTION",
    "CoverageReportMeta",
    "InstructionRow",
    "InstructionSummary",
    "TraceSessionMeta",
    "build_sidecar",
    "coverage_dir",
    "list_coverage_reports",
    "list_trace_sessions",
    "make_coverage_report_id",
    "make_trace_session_id",
    "read_trace_session",
    "sha256_file",
    "traces_dir",
    "write_coverage_report",
    "write_trace_session",
]
