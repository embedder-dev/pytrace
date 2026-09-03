"""The capture sequence, against a fake probe.

What this pins is *order*, not values. Getting ``ReadIntoTraceCache`` after
``STRACE_Start`` -- or skipping it -- does not fail: the DLL cannot expand
ETM's branch and sync points back into a full instruction stream, and every
count that follows is silently wrong rather than obviously empty. The only
place that can be caught cheaply is here.
"""

import array
import importlib

import pytest

from jtrace.capture import CaptureOptions, run_capture, to_chronological
from jtrace.errors import TraceError
from jtrace.symbols import Symbolizer

# Fetched via importlib because `jtrace.capture` is the module while
# `jtrace.run_capture` is the function; monkeypatching needs the module object.
capture_module = importlib.import_module("jtrace.capture")


class FakeStrace:
    def __init__(self, log, window, counts, executed):
        self._log = log
        self._window = window
        self._counts = counts
        self._executed = executed

    def set_buffer_size(self, num_bytes):
        self._log.append(("set_buffer_size", num_bytes))

    def configure_port(self, width):
        self._log.append(("configure_port", width))

    def start(self):
        self._log.append(("strace_start",))

    def stop(self):
        self._log.append(("strace_stop",))

    def instruction_counts(self, address, num_halfwords):
        self._log.append(("instruction_counts", address, num_halfwords))
        counts = array.array("I", self._counts[:num_halfwords])
        while len(counts) < num_halfwords:
            counts.append(0)
        return counts

    def total_executed(self, address):
        return self._executed

    def read(self, max_items):
        self._log.append(("read", max_items))
        return array.array("I", self._window[:max_items])


class FakeLink:
    """Records every call, in order, and hands back canned data."""

    instances: list["FakeLink"] = []

    def __init__(self, log, code, window, counts, executed, **kwargs):
        self._log = log
        self._code = code
        self.kwargs = kwargs
        self.closed = False
        self.strace = FakeStrace(log, window, counts, executed)
        FakeLink.instances.append(self)
        log.append(("open", kwargs.get("device")))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True
        self._log.append(("close",))

    def reset(self, halt=True):
        self._log.append(("reset", halt))

    def halt(self):
        self._log.append(("halt",))

    def go(self):
        self._log.append(("go",))

    def read_into_trace_cache(self, address, num_bytes):
        self._log.append(("read_into_trace_cache", address, num_bytes))

    def read_memory(self, address, num_bytes, zone=None):
        self._log.append(("read_memory", address, num_bytes))
        return self._code[:num_bytes].ljust(num_bytes, b"\x00")


@pytest.fixture
def fake_probe(monkeypatch, oracle_elf):
    """Install a fake JLink and return the call log it writes into."""
    from jtrace.elf import ElfFile

    elf = ElfFile(oracle_elf)
    section = elf.executable_sections()[0]
    code = elf.read_code(section)

    main = next(s for s in elf.function_symbols() if s.name == "main")
    spin = next(s for s in elf.function_symbols() if s.name == "spin")
    # Newest-first, as the probe hands it back.
    window = [
        spin.value & ~1,
        (main.value & ~1) + 2,
        main.value & ~1,
    ]
    counts = [3] * (section.size // 2)

    log: list[tuple] = []
    FakeLink.instances.clear()

    def factory(**kwargs):
        return FakeLink(log, code, window, counts, 4242, **kwargs)

    monkeypatch.setattr(capture_module, "JLink", factory)
    return log, elf, section


def run(oracle_elf, **overrides):
    options = CaptureOptions(
        elf_path=oracle_elf,
        device="STM32F407VG",
        duration_ms=0,
        **overrides,
    )
    return run_capture(options)


def test_trace_cache_is_primed_before_trace_starts(fake_probe, oracle_elf):
    log, _elf, _section = fake_probe
    run(oracle_elf)
    names = [entry[0] for entry in log]
    assert names.index("read_into_trace_cache") < names.index("strace_start")


def test_full_sequence_matches_the_shipped_worker(fake_probe, oracle_elf):
    log, _elf, section = fake_probe
    run(oracle_elf)
    assert [entry[0] for entry in log] == [
        "open",
        "reset",
        "halt",
        "read_into_trace_cache",
        "read_memory",
        "set_buffer_size",
        "configure_port",
        "strace_start",
        "go",
        "halt",
        "instruction_counts",
        "read",
        "strace_stop",
        "close",
    ]
    assert ("read_into_trace_cache", section.addr, section.size) in log


def test_probe_is_released_even_when_the_capture_throws(
    monkeypatch, fake_probe, oracle_elf
):
    log, _elf, _section = fake_probe

    def explode(self, max_items):
        raise TraceError("no trace clock")

    monkeypatch.setattr(FakeStrace, "read", explode)
    with pytest.raises(TraceError):
        run(oracle_elf)
    # A J-Link left open wedges every later flash, debug session and RTT
    # connection on the machine, so the teardown path is not optional.
    assert FakeLink.instances[0].closed is True
    assert ("strace_stop",) in log


def test_stream_is_reversed_into_chronological_order(fake_probe, oracle_elf):
    _log, elf, _section = fake_probe
    result = run(oracle_elf)
    assert [row.index for row in result.instructions] == [0, 1, 2]
    main = next(s for s in elf.function_symbols() if s.name == "main")
    # The probe's newest-first buffer put main's entry last; index 0 is oldest.
    assert result.instructions[0].address == main.value & ~1
    assert result.instructions[0].function == "main"
    assert result.instructions[-1].function == "spin"


def test_summary_reports_a_truncated_window(fake_probe, oracle_elf):
    _log, _elf, _section = fake_probe
    result = run(oracle_elf)
    # 4242 executed against a 3-instruction window: the buffer is a ring, and
    # what came back is the tail of the run rather than all of it.
    assert result.summary.instructions_executed == 4242
    assert result.summary.instruction_count == 3
    assert result.summary.window_truncated is True


def test_frames_are_reconstructed(fake_probe, oracle_elf):
    _log, _elf, _section = fake_probe
    result = run(oracle_elf)
    assert result.summary.frame_count == len(result.frames)
    assert {f.name for f in result.frames} == {"main", "spin"}


def test_rows_are_skipped_unless_asked_for(fake_probe, oracle_elf):
    _log, _elf, _section = fake_probe
    assert run(oracle_elf, build_rows=False).rows is None
    rows = run(oracle_elf, build_rows=True).rows
    assert rows is not None and len(rows.functions) > 0


def test_coverage_rows_use_the_real_instruction_boundaries(fake_probe, oracle_elf):
    _log, _elf, section = fake_probe
    result = run(oracle_elf, build_rows=True)
    assert result.rows is not None
    total = sum(row.instructions for row in result.rows.functions)
    # Every count slot is 3 in the fake, so if the trailing halfwords of 32-bit
    # instructions were counted the total would reach size/2 rather than the
    # instruction-start count.
    assert total < section.size // 2


def test_capture_needs_executable_sections(monkeypatch, tmp_path, oracle_elf):
    from jtrace.elf import ElfFile

    monkeypatch.setattr(ElfFile, "executable_sections", lambda self: [])
    with pytest.raises(TraceError, match="No executable sections"):
        run(oracle_elf)


def test_to_chronological_assigns_indices_after_reversing(oracle_elf):
    symbolizer = Symbolizer(oracle_elf)
    stream = [0x0800_0010, 0x0800_0008, 0x0800_0004]
    rows = to_chronological(stream, symbolizer, lambda _address: 7)
    assert [row.address for row in rows] == [0x0800_0004, 0x0800_0008, 0x0800_0010]
    assert [row.index for row in rows] == [0, 1, 2]
    assert all(row.run_count == 7 for row in rows)


def test_empty_window_produces_no_rows(oracle_elf):
    symbolizer = Symbolizer(oracle_elf)
    assert to_chronological([], symbolizer, lambda _address: None) == []


def test_end_to_end_capture_writes_artifacts_the_schemas_accept(
    fake_probe, oracle_elf, tmp_path, monkeypatch
):
    """The whole pipeline: fake probe -> capture -> artifacts on disk.

    The unit tests above each pin one stage. This one is the only check that
    the stages compose -- that what `run_capture` produces is exactly what
    `write_trace_session` and `write_coverage_report` expect, in the types they
    expect.
    """
    from jtrace.capture import capture_coverage, capture_instruction_trace

    session_id, result = capture_instruction_trace(
        oracle_elf,
        "STM32F407VG",
        duration_ms=0,
        cpu_freq_hz=16_000_000,
        session_root=tmp_path,
    )
    assert session_id.startswith("etm-")
    session_dir = tmp_path / ".pytrace" / "traces" / session_id
    assert (session_dir / "instructions.json").is_file()

    report_path, coverage_result = capture_coverage(
        oracle_elf,
        "STM32F407VG",
        duration_ms=0,
        session_id="e2e",
        project_root=tmp_path,
    )
    assert report_path.is_file()
    # The oracle firmware has functions that are deliberately never called, so
    # a report claiming full coverage would mean the counts were not read.
    totals = coverage_result.totals
    assert 0 < totals.functions_covered <= totals.functions
