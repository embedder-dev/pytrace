"""Trace timestamps: the ctypes boundary, and the reversal that hides mistakes.

`JLINK_STRACE_ReadEx` was declared in this SDK with a three-argument prototype
and never called. The real one takes five. Nothing caught that, because an
unused wrong declaration is silent -- these tests exercise the marshalling so
the next wrong prototype is not.
"""

from __future__ import annotations

import array
import ctypes

import pytest

from jtrace.errors import TraceError
from jtrace.strace import (
    Strace,
    StraceTimestamp,
    chronologise,
    detect_stamp_order,
)
from jtrace.structs import StraceTimestampInfo


# -- chronologise ----------------------------------------------------------


def test_reversing_remaps_stamp_indices_onto_the_reversed_stream():
    """The trap this pins: reversing the program counters and leaving the stamp
    indices alone. The result is monotonic, plausible, and attributes every
    instruction the cycle count of its mirror image. Nothing downstream can
    tell -- there is no invariant it violates.
    """
    # Probe order (newest first): index 0 is the NEWEST instruction.
    pcs = array.array("I", [40, 30, 20, 10])
    stamps = [StraceTimestamp(cycle=400, index=0), StraceTimestamp(cycle=100, index=3)]

    out, remapped = chronologise(pcs, stamps)

    assert list(out) == [10, 20, 30, 40]
    # The stamp that was on the newest instruction is now on the last one.
    assert [(s.index, s.cycle) for s in remapped] == [(0, 100), (3, 400)]
    # ... and cycles now ascend with index, which is the whole point.
    assert [s.cycle for s in remapped] == sorted(s.cycle for s in remapped)


def test_remapped_stamps_come_back_in_index_order():
    pcs = array.array("I", [50, 40, 30, 20, 10])
    stamps = [
        StraceTimestamp(cycle=500, index=0),
        StraceTimestamp(cycle=300, index=2),
        StraceTimestamp(cycle=100, index=4),
    ]
    _out, remapped = chronologise(pcs, stamps)
    assert [s.index for s in remapped] == [0, 2, 4]


def test_out_of_range_stamps_are_dropped_not_clamped():
    """A clamped stamp is an invented measurement: it claims a cycle count for
    an instruction the probe never stamped."""
    pcs = array.array("I", [30, 20, 10])
    stamps = [
        StraceTimestamp(cycle=1, index=99),
        StraceTimestamp(cycle=2, index=-1),
        StraceTimestamp(cycle=3, index=1),
    ]
    _out, remapped = chronologise(pcs, stamps)
    assert [(s.index, s.cycle) for s in remapped] == [(1, 3)]


def test_adjust_is_carried_through_untouched():
    """Its meaning is unverified, so it is neither used nor discarded."""
    pcs = array.array("I", [20, 10])
    _out, remapped = chronologise(pcs, [StraceTimestamp(7, 0, adjust=0xABCD)])
    assert remapped[0].adjust == 0xABCD


def test_chronologise_of_an_empty_window():
    out, remapped = chronologise(array.array("I"), [])
    assert list(out) == [] and remapped == []


# -- the ctypes boundary ---------------------------------------------------


class FakeRawLibrary:
    """Stands in for `RawLibrary`, writing through the real ctypes buffers.

    The only test in this repo that exercises the FFI marshalling rather than
    the wrapper above it.
    """

    def __init__(self, pcs, stamps, *, exports=True, result=None):
        self.pcs = list(pcs)
        self.stamps = list(stamps)
        self.exports = exports
        self.result = len(self.pcs) if result is None else result
        self.calls = []

    def has(self, name):
        return self.exports and name == "JLINK_STRACE_ReadEx"

    def JLINK_STRACE_ReadEx(self, pc_ptr, num_items, ts_ptr, num_ts_ptr, flags):
        self.calls.append((num_items, flags))
        for i, value in enumerate(self.pcs[:num_items]):
            pc_ptr[i] = value
        if ts_ptr:
            records = ctypes.cast(ts_ptr, ctypes.POINTER(StraceTimestampInfo))
            for i, (cycle, index, adjust) in enumerate(self.stamps[:num_items]):
                records[i].Timestamp = cycle
                records[i].Index = index
                records[i].Adjust = adjust
        num_ts_ptr[0] = len(self.stamps)
        return self.result


def strace_over(library):
    link = type("FakeLink", (), {"raw": library})()
    return Strace(link)


def test_read_ex_marshals_counters_and_stamps_back():
    library = FakeRawLibrary(
        [40, 30, 20, 10], [(400, 0, 0), (100, 3, 0)]
    )
    pcs, stamps = strace_over(library).read_ex(max_items=4)
    assert list(pcs) == [40, 30, 20, 10]
    assert [(s.cycle, s.index, s.adjust) for s in stamps] == [(400, 0, 0), (100, 3, 0)]


def test_read_ex_returns_probe_order_untouched():
    """Whether ReadEx is newest-first like Read is unmeasured, so this must not
    reverse anything -- `chronologise` does that where it can be tested."""
    library = FakeRawLibrary([9, 8, 7], [])
    pcs, _stamps = strace_over(library).read_ex(max_items=3)
    assert list(pcs) == [9, 8, 7]


def test_read_ex_honours_the_per_call_clamp():
    library = FakeRawLibrary(list(range(10)), [])
    strace_over(library).read_ex(max_items=1_000_000)
    assert library.calls[0][0] == 0x10000


def test_read_ex_passes_flags_zero_by_default():
    library = FakeRawLibrary([1], [])
    strace_over(library).read_ex(max_items=1)
    assert library.calls[0][1] == 0


def test_read_ex_truncates_to_the_returned_count():
    """The DLL's return value is the authority on how many counters are real;
    the rest of the buffer is whatever was in it."""
    library = FakeRawLibrary([5, 6, 7, 8], [], result=2)
    pcs, _stamps = strace_over(library).read_ex(max_items=4)
    assert list(pcs) == [5, 6]


def test_read_ex_ignores_a_stamp_count_past_the_buffer():
    library = FakeRawLibrary([1, 2], [(10, 0, 0)] * 2)
    library.stamps = [(10, 0, 0)] * 2

    class Overclaiming(FakeRawLibrary):
        def JLINK_STRACE_ReadEx(self, pc_ptr, num_items, ts_ptr, num_ts_ptr, flags):
            super().JLINK_STRACE_ReadEx(pc_ptr, num_items, ts_ptr, num_ts_ptr, flags)
            num_ts_ptr[0] = 10_000_000
            return len(self.pcs)

    _pcs, stamps = strace_over(Overclaiming([1, 2], [(10, 0, 0)])).read_ex(max_items=2)
    assert len(stamps) <= 2


def test_read_ex_raises_on_a_negative_return():
    library = FakeRawLibrary([], [], result=-5)
    with pytest.raises(TraceError):
        strace_over(library).read_ex(max_items=4)


def test_read_ex_says_so_when_the_dll_predates_it():
    library = FakeRawLibrary([], [], exports=False)
    with pytest.raises(TraceError, match="does not export"):
        strace_over(library).read_ex()


def test_read_ex_then_chronologise_produces_an_ascending_timeline():
    """End to end over the boundary: probe order in, time order out."""
    library = FakeRawLibrary(
        [40, 30, 20, 10], [(400, 0, 0), (300, 1, 0), (100, 3, 0)]
    )
    pcs, stamps = strace_over(library).read_ex(max_items=4)
    ordered, remapped = chronologise(pcs, stamps)
    assert list(ordered) == [10, 20, 30, 40]
    assert [s.cycle for s in remapped] == [100, 300, 400]
    assert [s.index for s in remapped] == [0, 2, 3]


# -- through a capture -----------------------------------------------------


class StampingProbe:
    """A draining probe that also stamps, in the probe's newest-first order."""

    WINDOW = 8

    def __init__(self, per_slice, cycles_per_inst=10):
        self._per_slice = list(per_slice)
        self._pending = []
        self._executed = 0
        self._rate = cycles_per_inst
        self.retired = []

    def go(self):
        pass

    def halt(self):
        if not self._per_slice:
            return
        count = self._per_slice.pop(0)
        new = list(range(self._executed, self._executed + count))
        self.retired.extend(new)
        self._executed += count
        self._pending = (self._pending + new)[-self.WINDOW:]

    def total_executed(self, _address):
        return self._executed

    def read(self, _max_items):
        drained, self._pending = self._pending, []
        return array.array("I", reversed(drained))

    def read_ex(self, _max_items=None, **_kwargs):
        drained, self._pending = self._pending, []
        pcs = array.array("I", reversed(drained))
        # Stamp the first and last of the window, newest-first like the PCs.
        stamps = []
        if len(pcs):
            stamps = [
                StraceTimestamp(cycle=drained[-1] * self._rate, index=0),
                StraceTimestamp(cycle=drained[0] * self._rate, index=len(pcs) - 1),
            ]
        return pcs, stamps

    _link = property(lambda self: self)


def test_timestamps_land_on_blocks_in_time_order():
    store = Strace.read_extended(
        StampingProbe([4, 4]), 0x0800_0000,
        target_items=8, slice_ms=0.0, timestamps=True,
    )
    assert len(store.blocks) == 2
    for block in store.blocks:
        assert len(block.cycles) == 2
        # Ascending with index, which only holds if the remap was applied.
        assert list(block.stamp_at) == sorted(block.stamp_at)
        assert list(block.cycles) == sorted(block.cycles)


def test_a_stamped_capture_can_answer_when_an_instruction_ran():
    store = Strace.read_extended(
        StampingProbe([8]), 0x0800_0000,
        target_items=8, slice_ms=0.0, timestamps=True,
    )
    exact = store.estimate_cycle(0)
    assert exact is not None and exact.exact is True and exact.cycle == 0
    middle = store.estimate_cycle(4)
    assert middle is not None and middle.exact is False
    assert middle.cycle == 40


def test_timestamps_default_off_so_nothing_changes_without_a_probe():
    """Until the DLL's behaviour is measured, a capture must not silently start
    depending on it."""
    store = Strace.read_extended(
        StampingProbe([4]), 0x0800_0000, target_items=4, slice_ms=0.0
    )
    assert all(not block.stamped for block in store.blocks)
    assert store.cycles_continuous is None


# -- orientation, observed rather than assumed -----------------------------


def test_cycles_falling_with_the_index_means_newest_first():
    assert detect_stamp_order(
        [StraceTimestamp(400, 0), StraceTimestamp(300, 1), StraceTimestamp(100, 3)]
    ) is True


def test_cycles_rising_with_the_index_means_oldest_first():
    """The case the SDK cannot rule out. `STRACE_Read` is measurably
    newest-first and `ReadEx` fills the same buffer through the same transfer
    primitive, but that is an inference, and a wrong reversal is silent."""
    assert detect_stamp_order(
        [StraceTimestamp(100, 0), StraceTimestamp(300, 1), StraceTimestamp(400, 3)]
    ) is False


def test_contradictory_stamps_refuse_to_vote():
    """Disagreement means an assumption is already wrong somewhere. A majority
    verdict would bury that."""
    assert detect_stamp_order(
        [StraceTimestamp(100, 0), StraceTimestamp(400, 1), StraceTimestamp(200, 2)]
    ) is None


def test_too_few_stamps_to_tell():
    assert detect_stamp_order([]) is None
    assert detect_stamp_order([StraceTimestamp(100, 0)]) is None
    # Equal cycles carry no direction.
    assert detect_stamp_order(
        [StraceTimestamp(100, 0), StraceTimestamp(100, 4)]
    ) is None


def test_an_oldest_first_window_is_not_reversed():
    """If the probe ever hands back time order, honouring the evidence keeps
    the timeline right where a fixed assumption would invert it."""
    pcs = array.array("I", [10, 20, 30, 40])
    stamps = [StraceTimestamp(100, 0), StraceTimestamp(400, 3)]
    out, remapped = chronologise(pcs, stamps)
    assert list(out) == [10, 20, 30, 40]
    assert [(s.index, s.cycle) for s in remapped] == [(0, 100), (3, 400)]


def test_unstamped_windows_fall_back_to_the_measured_convention():
    """With no evidence, use what `read()` was actually measured to do."""
    out, _ = chronologise(array.array("I", [40, 30, 20, 10]), [])
    assert list(out) == [10, 20, 30, 40]


def test_an_explicit_orientation_overrides_the_evidence():
    pcs = array.array("I", [10, 20, 30, 40])
    stamps = [StraceTimestamp(100, 0), StraceTimestamp(400, 3)]
    out, _ = chronologise(pcs, stamps, newest_first=True)
    assert list(out) == [40, 30, 20, 10]


def test_a_capture_records_the_orientation_it_observed():
    """The open question about ReadEx answers itself on the first real run,
    instead of waiting on someone to run a checklist."""
    store = Strace.read_extended(
        StampingProbe([8]), 0x0800_0000,
        target_items=8, slice_ms=0.0, timestamps=True,
    )
    assert store.stamp_order_observed is True


def test_an_unstamped_capture_observes_nothing_rather_than_guessing():
    store = Strace.read_extended(
        StampingProbe([8]), 0x0800_0000, target_items=8, slice_ms=0.0
    )
    assert store.stamp_order_observed is None
