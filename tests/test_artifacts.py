"""Artifacts: the on-disk layout, and whether a reader will accept it.

The consuming viewer validates every file it loads against a schema, and a
field with the wrong name or type does not degrade -- the session is skipped
and the viewer shows nothing. So the shapes here are asserted field by field
rather than left to drift.
"""

import json
from pathlib import Path

import pytest

from jtrace.artifacts import (
    CoverageReportMeta,
    InstructionRow,
    InstructionSummary,
    TraceSessionMeta,
    build_sidecar,
    list_coverage_reports,
    list_trace_sessions,
    make_coverage_report_id,
    make_trace_session_id,
    read_trace_session,
    sha256_file,
    write_coverage_report,
    write_trace_session,
)
from jtrace.coverage import CoverageRows, FunctionRow, LineRow



def sample_meta(elf: Path) -> TraceSessionMeta:
    return TraceSessionMeta(
        device="STM32F407VG",
        elf_path=str(elf),
        elf_sha256=sha256_file(elf),
        trace_items=65536,
        buffer_bytes=1 << 24,
        duration_ms=3000,
        instruction_count=3,
        speed_khz=4000,
        port_width=4,
        target_interface="SWD",
        cpu_freq_hz=16_000_000,
        label="demo",
    )


def sample_session(elf: Path):
    instructions = [
        InstructionRow(index=0, address=0x0800_0100, function="main", file="a.c", line=1, run_count=5),
        InstructionRow(index=1, address=0x0800_0200, function="worker", file="a.c", line=9, run_count=2),
        InstructionRow(index=2, address=0x0800_0102, function="main", file="a.c", line=2, run_count=5),
    ]
    from jtrace.frames import CallFrame

    frames = [
        CallFrame("main", 0x0800_0100, 0, 0, 3, "a.c", 1, open_at_start=True, open_at_end=True),
        CallFrame("worker", 0x0800_0200, 1, 1, 2, "a.c", 9),
    ]
    summary = InstructionSummary(
        instruction_count=3,
        instructions_executed=120,
        frame_count=2,
        max_depth=2,
        window_truncated=True,
        cpu_freq_hz=16_000_000,
    )
    return instructions, frames, summary


def sample_rows() -> CoverageRows:
    return CoverageRows(
        functions=[
            FunctionRow("a.c", 1, "main", 4, 3, 12, 10, 5, 60),
            FunctionRow("a.c", 9, "worker", 2, 2, 6, 6, 2, 12),
            FunctionRow("a.c", 20, "never_called", 3, 0, 8, 0, 0, 0),
        ],
        lines=[LineRow("a.c", 1, 3, 3, 5), LineRow("a.c", 9, 2, 2, 2)],
    )


# -- ids -------------------------------------------------------------------


def test_session_ids_are_unique_and_prefixed():
    ids = {make_trace_session_id() for _ in range(50)}
    assert len(ids) > 40  # the random suffix does its job
    assert all(i.startswith("etm-") for i in ids)


def test_report_ids_are_safe_for_a_directory_name():
    import re

    for _ in range(20):
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", make_coverage_report_id())


def test_sha256_of_a_missing_file_is_zeroes():
    assert sha256_file("/nonexistent") == "0" * 64


# -- sidecar ---------------------------------------------------------------


def test_sidecar_declares_an_instruction_capture(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    sidecar = build_sidecar("etm-test", sample_meta(elf))
    assert sidecar["traceFormat"] == "embedder-trace-instruction-v1"
    assert sidecar["transport"]["kind"] == "etm"
    assert sidecar["state"] == "stopped"


def test_sidecar_records_the_real_buffer_size_not_a_derived_one(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    meta = sample_meta(elf)
    sidecar = build_sidecar("etm-test", meta)
    # traceItems * 4 would be 256 KiB here: 64x under the real ring.
    assert sidecar["captureSettings"]["bufferBytes"] == meta.buffer_bytes
    assert sidecar["transport"]["bufferBytes"] == meta.buffer_bytes


def test_sidecar_marks_symbolization_exact(tmp_path):
    """Without this the Trace tab shows a permanent "Attach ELF..." banner."""
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    sidecar = build_sidecar("etm-test", sample_meta(elf))
    assert sidecar["symbolization"]["state"] == "exact"
    assert sidecar["symbolization"]["elfSha256"] == sidecar["firmware"]["elfSha256"]


def test_sidecar_timestamps_are_utc_with_a_z(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    sidecar = build_sidecar("etm-test", sample_meta(elf))
    assert sidecar["startedAt"].endswith("Z")
    assert sidecar["endedAt"].endswith("Z")
    assert sidecar["startedAt"] < sidecar["endedAt"]


# -- trace session on disk -------------------------------------------------


def test_writes_every_file_the_store_expects(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    instructions, frames, summary = sample_session(elf)
    directory = write_trace_session(
        instructions, frames, summary, sample_meta(elf),
        session_id="etm-fixed", root=tmp_path,
    )
    assert directory == tmp_path / ".pytrace" / "traces" / "etm-fixed"
    for name in ("sidecar.json", "instructions.json", "index.json", "events.ndjson"):
        assert (directory / name).is_file(), name
    # The store creates these for every session and its readers stat them; a
    # session missing them reads as corrupt rather than as empty.
    assert (directory / "raw" / "stream.bin").is_file()


def test_instruction_payload_round_trips(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    instructions, frames, summary = sample_session(elf)
    directory = write_trace_session(
        instructions, frames, summary, sample_meta(elf), root=tmp_path
    )
    payload = json.loads((directory / "instructions.json").read_text())
    assert payload["payloadVersion"] == 1
    assert [row["index"] for row in payload["instructions"]] == [0, 1, 2]
    assert payload["summary"]["windowTruncated"] is True
    assert payload["frames"][0]["openAtStart"] is True
    # Absent flags are omitted, not written false.
    assert "openAtStart" not in payload["frames"][1]


def test_list_and_read_find_what_was_written(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    instructions, frames, summary = sample_session(elf)
    write_trace_session(
        instructions, frames, summary, sample_meta(elf),
        session_id="etm-one", root=tmp_path,
    )
    sessions = list_trace_sessions(tmp_path)
    assert [s["sessionId"] for s in sessions] == ["etm-one"]
    loaded = read_trace_session("etm-one", tmp_path)
    assert loaded is not None
    assert len(loaded["instructions"]["instructions"]) == 3
    assert read_trace_session("etm-missing", tmp_path) is None


def test_listing_an_empty_tree_is_not_an_error(tmp_path):
    assert list_trace_sessions(tmp_path) == []
    assert list_coverage_reports(tmp_path) == []


# -- coverage on disk ------------------------------------------------------


def test_coverage_report_layout(tmp_path):
    path = write_coverage_report(
        sample_rows(),
        CoverageReportMeta(elf_path=str(tmp_path / "fw.elf"), device="STM32F407VG", duration_sec=3.0),
        session_id="s1",
        report_id="cov_fixed",
        project_root=tmp_path,
    )
    assert path == tmp_path / ".pytrace" / "coverage" / "s1" / "cov_fixed" / "report.json"
    stored = json.loads(path.read_text())
    assert stored["report"]["reportId"] == "cov_fixed"
    assert stored["report"]["hasLineRows"] is True
    assert stored["functionsTruncated"] is False


def test_totals_cover_every_row_even_when_the_stored_slice_is_capped(tmp_path):
    """A report over the row cap must not describe its first N functions as the firmware."""
    rows = CoverageRows(
        functions=[
            FunctionRow("a.c", i, f"f{i}", 1, 1, 2, 2 if i < 5 else 0, 1, 2)
            for i in range(10)
        ],
        lines=[],
    )
    path = write_coverage_report(
        rows,
        CoverageReportMeta(elf_path="fw.elf", device="d", duration_sec=1.0),
        session_id="s1",
        project_root=tmp_path,
        max_function_rows=3,
    )
    stored = json.loads(path.read_text())
    assert len(stored["functions"]) == 3
    assert stored["functionsTruncated"] is True
    assert stored["report"]["totals"]["functions"] == 10
    assert stored["report"]["totals"]["functionsCovered"] == 5


def test_coverage_listing_is_newest_first(tmp_path):
    for name in ("cov_a", "cov_b"):
        write_coverage_report(
            sample_rows(),
            CoverageReportMeta(elf_path="fw.elf", device="d", duration_sec=1.0),
            session_id="s1",
            report_id=name,
            project_root=tmp_path,
        )
    reports = list_coverage_reports(tmp_path)
    assert len(reports) == 2
    assert reports[0]["createdAt"] >= reports[1]["createdAt"]


def test_zero_duration_omits_the_cap_rather_than_writing_zero(tmp_path):
    """maxDurationMs is positive-and-optional; a written 0 fails validation.

    A capture with no duration cap is legitimate -- read out whatever the probe
    already holds -- and it must not produce a sidecar the Trace tab skips.
    """
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    meta = sample_meta(elf)
    meta.duration_ms = 0
    sidecar = build_sidecar("etm-test", meta)
    assert "maxDurationMs" not in sidecar["captureSettings"]
    # A real duration is still recorded.
    meta.duration_ms = 3000
    assert build_sidecar("etm-test", meta)["captureSettings"]["maxDurationMs"] == 3000


def test_oversized_sessions_warn_rather_than_silently_setting_a_trap(tmp_path):
    """A million-row session is ~166 MB, and the Trace tab reads it whole."""
    import warnings

    from jtrace.artifacts import TRACE_TAB_COMFORTABLE_ROWS

    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    _instructions, frames, summary = sample_session(elf)
    big = [
        InstructionRow(index=i, address=0x0800_0100 + (i % 8) * 2)
        for i in range(TRACE_TAB_COMFORTABLE_ROWS + 1)
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        write_trace_session(big, frames, summary, sample_meta(elf), root=tmp_path)
    assert any("Trace tab" in str(w.message) for w in caught)


def test_ordinary_sessions_do_not_warn(tmp_path):
    import warnings

    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    instructions, frames, summary = sample_session(elf)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        write_trace_session(instructions, frames, summary, sample_meta(elf), root=tmp_path)
    assert not caught


def test_streamed_payload_is_byte_identical_to_json_dumps(tmp_path):
    """The writer builds the document incrementally so a large session never
    materialises it. That is only safe if the bytes are indistinguishable --
    the reader validates this file against a Zod schema, and a stray separator
    is the kind of difference that shows up as a session silently skipped.
    """
    import json

    from jtrace.artifacts import (
        TRACE_INSTRUCTION_PAYLOAD_VERSION,
        _write_instruction_payload,
    )

    instructions, frames, summary = sample_session(tmp_path / "fw.elf")
    path = tmp_path / "instructions.json"
    _write_instruction_payload(path, instructions, frames, summary)

    expected = json.dumps(
        {
            "payloadVersion": TRACE_INSTRUCTION_PAYLOAD_VERSION,
            "summary": summary.to_json(),
            "instructions": [row.to_json() for row in instructions],
            "frames": [frame.to_json() for frame in frames],
        }
    )
    assert path.read_text() == expected


def test_streamed_payload_handles_an_empty_capture(tmp_path):
    import json

    from jtrace.artifacts import InstructionSummary, _write_instruction_payload

    path = tmp_path / "instructions.json"
    summary = InstructionSummary(
        instruction_count=0,
        instructions_executed=0,
        frame_count=0,
        max_depth=0,
        window_truncated=False,
    )
    _write_instruction_payload(path, [], [], summary)
    payload = json.loads(path.read_text())
    assert payload["instructions"] == []
    assert payload["frames"] == []


# -- layout overrides ------------------------------------------------------


def test_the_store_directory_can_be_renamed(tmp_path):
    """The default layout targets one viewer; a caller need not adopt its name.

    Hardcoding the directory would oblige every user of this SDK to carry a
    particular product's convention, which is not something the writer needs
    to know to do its job.
    """
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    instructions, frames, summary = sample_session(elf)

    directory = write_trace_session(
        instructions, frames, summary, sample_meta(elf),
        session_id="etm-custom", root=tmp_path, store_dirname=".traces",
    )
    assert directory == tmp_path / ".traces" / "traces" / "etm-custom"
    assert (directory / "sidecar.json").is_file()
    assert not (tmp_path / ".pytrace").exists()

    listed = list_trace_sessions(tmp_path, store_dirname=".traces")
    assert [entry["sessionId"] for entry in listed] == ["etm-custom"]
    assert list_trace_sessions(tmp_path) == []


def test_the_trace_format_identifier_can_be_overridden(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    instructions, frames, summary = sample_session(elf)

    directory = write_trace_session(
        instructions, frames, summary, sample_meta(elf),
        session_id="etm-fmt", root=tmp_path, trace_format="my-format-v1",
    )
    sidecar = json.loads((directory / "sidecar.json").read_text())
    assert sidecar["traceFormat"] == "my-format-v1"


def test_coverage_reports_honour_the_store_directory_override(tmp_path):
    path = write_coverage_report(
        sample_rows(),
        CoverageReportMeta(elf_path="fw.elf", device="STM32F407VG", duration_sec=1.0),
        session_id="s1",
        report_id="cov_1",
        project_root=tmp_path,
        store_dirname=".cov",
    )
    assert path == tmp_path / ".cov" / "coverage" / "s1" / "cov_1" / "report.json"
    assert len(list_coverage_reports(tmp_path, store_dirname=".cov")) == 1
    assert list_coverage_reports(tmp_path) == []
