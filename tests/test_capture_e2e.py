"""The capture path, driven through the real JLink and Strace.

Every other fake in this suite replaces `JLink` or `Strace` wholesale, so
the wrappers themselves never run. This one stands in for `RawLibrary` --
the object holding the bound C functions -- which means the genuine
`run_capture`, `JLink`, `Strace`, and every ctypes buffer, `byref`,
`memmove` and return-code check execute exactly as they do against a probe.
Only the USB transport is absent.

It models the DLL behaviour this SDK documents as measured: a read drains,
returns newest-first, and clamps; the probe ring holds a bounded window.

It earns its place. It caught two defects the 227 tests above it did not:
a capacity bound that silently returned a fraction of the requested
instructions, and -- in the first attempt to fix that -- a slice loop whose
progress was cancelled by the eviction it triggered, so it spun to its
deadline instead of terminating.
"""

from __future__ import annotations

import ctypes
import os

import pytest

from jtrace.capture import CaptureOptions, run_capture
from jtrace.constants import MAX_STRACE_ITEMS
from jtrace.elf import ElfFile


def _deref(arg):
    """Recover the object behind ctypes.byref(). Test-only."""
    return getattr(arg, "_obj", arg)


class FakeDLL:
    RING = 4096
    def __init__(self, code, base, per_halt, halt_reason=3):
        self.code, self.base, self.per_halt = code, base, per_halt
        self._hr = halt_reason
        self.pending, self.retired, self.executed = [], [], 0
        self.running = self.strace_started = self.closed = False
        self.calls, self.commands = {}, []
        self.n_insts = len(code) // 2
    def _t(self, n): self.calls[n] = self.calls.get(n, 0) + 1
    def has(self, n): return hasattr(self, n)
    def JLINK_SetLogFile(self, *_): return 0
    def JLINKARM_EMU_SelectByUSBSN(self, *_): return 0
    def JLINKARM_OpenEx(self, *_): self._t("OpenEx"); return None
    def JLINKARM_Close(self): self._t("Close"); self.closed = True; return 0
    def JLINKARM_IsOpen(self): return 1
    def JLINKARM_IsConnected(self): return 1
    def JLINKARM_Connect(self): self._t("Connect"); return 0
    def JLINKARM_TIF_Select(self, *_): return 0
    def JLINKARM_GetSpeed(self): return 4000
    def JLINKARM_SetSpeed(self, *_): return 0
    def JLINKARM_ExecCommand(self, cmd, buf, size):
        self._t("ExecCommand"); self.commands.append(cmd.decode().rstrip("\x00"))
        _deref(buf).value = b""; return 0
    def JLINKARM_Reset(self): self._t("Reset"); self.running = False; return 0
    def JLINKARM_ResetNoHalt(self): return 0
    def JLINKARM_Go(self): self._t("Go"); self.running = True; return 0
    def JLINKARM_Halt(self):
        self._t("Halt")
        if self.running and self.strace_started:
            new = [self.base + ((self.executed + i) % self.n_insts) * 2 for i in range(self.per_halt)]
            self.retired.extend(new); self.executed += self.per_halt
            self.pending = (self.pending + new)[-self.RING:]
        self.running = False; return 0
    def JLINKARM_GetMOEs(self, buf, maxn):
        self._t("GetMOEs"); a = _deref(buf); a[0].HaltReason = self._hr; a[0].Index = 0; return 1
    def JLINKARM_ReadMemEx(self, addr, n, buf, _f):
        self._t("ReadMemEx"); off = addr - self.base
        ctypes.memmove(_deref(buf), self.code[off:off+n].ljust(n, b"\x00"), n); return 0
    def JLINK_STRACE_Config(self, s): self.commands.append("STRACE:"+s.decode().rstrip("\x00")); return 0
    def JLINK_STRACE_Control(self, c, d): self._t("STRACE_Control"); return 0
    def JLINK_STRACE_Start(self): self._t("STRACE_Start"); self.strace_started = True; return 0
    def JLINK_STRACE_Stop(self): self._t("STRACE_Stop"); self.strace_started = False; return 0
    def JLINK_STRACE_Read(self, buf, num_items):
        self._t("STRACE_Read")
        take = self.pending[-min(num_items, len(self.pending)):] if self.pending else []
        self.pending = []
        nf = list(reversed(take))
        for i, pc in enumerate(nf): buf[i] = pc
        return len(nf)
    def JLINK_STRACE_GetInstStats(self, buf, addr, num_items, item_bytes, type_):
        self._t("STRACE_GetInstStats"); raw = _deref(buf)
        if type_ == 4:
            ctypes.memmove(raw, self.executed.to_bytes(8, "little"), 8); return 1
        c = {}
        for pc in self.retired: c[pc] = c.get(pc, 0) + 1
        blob = bytearray()
        for i in range(num_items): blob += c.get(addr+i*2, 0).to_bytes(4, "little") + b"\x00"*4
        ctypes.memmove(raw, bytes(blob), len(blob)); return num_items


@pytest.fixture
def probe(monkeypatch, oracle_elf):
    """Install the double in place of the bound DLL, for the whole capture."""
    elf = ElfFile(oracle_elf)
    section = elf.executable_sections()[0]

    def make(per_halt=3000):
        dll = FakeDLL(elf.read_code(section), section.addr, per_halt=per_halt)
        import jtrace.link as link_mod
        import jtrace.loader as loader_mod

        monkeypatch.setattr(link_mod, "load", lambda *_a, **_k: dll)
        monkeypatch.setattr(loader_mod, "load", lambda *_a, **_k: dll)
        return dll

    return make


def capture(oracle_elf, **kwargs):
    kwargs.setdefault("duration_ms", 20_000)
    kwargs.setdefault("build_rows", False)
    return run_capture(
        CaptureOptions(elf_path=oracle_elf, device="STM32F407VG", **kwargs)
    )


def test_a_short_capture_runs_end_to_end_through_the_real_wrappers(probe, oracle_elf):
    dll = probe(per_halt=800)
    result = capture(oracle_elf, trace_items=800, duration_ms=1)
    assert len(result.instructions) == 800
    assert [row.index for row in result.instructions] == list(range(800))
    assert result.instructions[0].function is not None
    assert dll.closed, "the probe must be handed back"


def test_the_documented_call_order_holds_against_the_bound_dll(probe, oracle_elf):
    """ReadIntoTraceCache before STRACE_Start is make-or-break: without it the
    DLL cannot expand ETM's branch points, and every later count is silently
    wrong rather than obviously empty."""
    dll = probe(per_halt=800)
    capture(oracle_elf, trace_items=800, duration_ms=1)
    primed = [c for c in dll.commands if c.startswith("ReadIntoTraceCache")]
    assert primed, "trace cache was never primed"
    assert dll.calls["STRACE_Start"] == 1
    assert dll.calls["STRACE_Stop"] == 1


def test_an_explicit_trace_items_is_honoured_despite_the_capacity_bound(
    probe, oracle_elf
):
    """The bound exists to stop a runaway capture, not to quietly return a
    fraction of what was asked for. The shortfall used to be visible only in
    summary.window_truncated, which reads as a probe-side truncation."""
    probe(per_halt=3000)
    result = capture(oracle_elf, trace_items=70_000, capacity=20_000)
    assert len(result.instructions) == 70_000
    assert result.streaming.dropped == 0


def test_slices_that_do_not_divide_the_target_still_terminate(probe, oracle_elf):
    """With capacity equal to the target, every slice past it evicted as much as
    it added, so progress measured by what the store held never advanced and the
    loop ran to its deadline."""
    probe(per_halt=3500)
    result = capture(oracle_elf, trace_items=70_000, capacity=70_000)
    assert len(result.instructions) == 70_000
    assert result.streaming.polls < 100


def test_halt_reasons_are_read_once_per_productive_slice(probe, oracle_elf):
    """A probe transaction `main` never made. One per slice that returned data
    is affordable; one per idle poll would not be."""
    dll = probe(per_halt=3000)
    result = capture(oracle_elf, trace_items=70_000)
    assert dll.calls["GetMOEs"] == len(result.store.blocks)
    assert all(block.halt_reason == 3 for block in result.store.blocks)


def test_a_target_that_goes_quiet_does_not_allocate_a_block_per_turn(
    probe, oracle_elf
):
    dll = probe(per_halt=3000)
    original = dll.JLINKARM_Halt

    def quiet():
        if dll.calls.get("Halt", 0) > 3:
            dll.per_halt = 0
        return original()

    dll.JLINKARM_Halt = quiet
    result = capture(oracle_elf, trace_items=70_000, duration_ms=400)
    assert len(result.store.blocks) <= 5
    assert result.streaming.polls > len(result.store.blocks)


def test_a_lossy_capture_never_produces_a_frame_spanning_the_hole(
    probe, oracle_elf
):
    """The defect this branch fixes, on the real path: instructions between two
    windows are gone, so a frame drawn across the seam asserts a call or return
    that never happened."""
    # Above the clamp, so the capture slices; and each slice retires far past
    # the probe ring, so each one loses.
    probe(per_halt=20_000)
    result = capture(oracle_elf, trace_items=100_000)
    boundaries = result.store.boundaries()
    assert boundaries, "expected loss with a slice larger than the ring"
    assert result.streaming.gaps > 0
    for edge in boundaries:
        spanning = [
            f for f in result.frames if f.start_index < edge < f.end_index
        ]
        assert not spanning, f"{len(spanning)} frames span the hole at {edge}"
