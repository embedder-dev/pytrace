"""Logic in the trace subsystems that runs without a probe.

The window stitcher and the ITM decoder are both places where being wrong
produces plausible output rather than an error, so they are worth pinning
independently of hardware.
"""

import array
import types
import ctypes


from jtrace.constants import (
    MAX_STRACE_ITEMS,
    PowerTraceCmd,
    RawTraceCmd,
    StraceCmd,
    SwoCmd,
    TraceCmd,
)
from jtrace.strace import StreamingStats
from jtrace.swo import decode_itm


# -- extended reads -------------------------------------------------------
#
# Modelled on measured DLL behaviour, not on a guess about it: a read drains
# the buffer, and below the clamp it returns exactly as many instructions as
# executed. Both were confirmed on hardware -- read twice in a row and the
# second call returns 0; a slice retiring 28,753 returned 28,753.


def test_streaming_stats_report_continuity():
    stats = StreamingStats(polls=4, stitched=3, gaps=0)
    assert stats.is_continuous is True
    assert StreamingStats(gaps=1).is_continuous is False


class FakeDrainingProbe:
    """A Strace/JLink pair with the DLL's real buffer semantics."""

    WINDOW = 8  # a tiny clamp, so the arithmetic is legible

    def __init__(self, per_slice):
        self._per_slice = list(per_slice)
        self._pending = []      # accumulated since the last read
        self._executed = 0      # cumulative, as GetInstStats reports it
        self.retired = []       # the whole true stream, for comparison

    # -- JLink half
    def go(self):
        pass

    def halt(self):
        if not self._per_slice:
            return
        count = self._per_slice.pop(0)
        new = list(range(self._executed, self._executed + count))
        self.retired.extend(new)
        self._executed += count
        # The probe ring holds only the newest WINDOW items.
        self._pending = (self._pending + new)[-self.WINDOW :]

    # -- Strace half
    def total_executed(self, _address):
        return self._executed

    def read(self, _max_items):
        drained = self._pending
        self._pending = []          # a read drains
        return array.array("I", reversed(drained))   # newest-first

    _link = property(lambda self: self)


def _extended_store(per_slice, target_items, **kwargs):
    from jtrace.strace import Strace

    return Strace.read_extended(
        FakeDrainingProbe(per_slice), 0x0800_0000,
        target_items=target_items, slice_ms=0.0, **kwargs
    )


def run_extended(per_slice, target_items, **kwargs):
    from jtrace.strace import Strace

    probe = FakeDrainingProbe(per_slice)
    stats = StreamingStats()
    collected = Strace.read_extended(
        probe, 0x0800_0000, target_items=target_items, slice_ms=0.0,
        stats=stats, **kwargs
    )
    return list(collected), stats, probe


def test_slices_concatenate_into_the_true_stream():
    # Every slice stays under the 8-item window, so nothing is dropped and the
    # result is exactly what executed.
    collected, stats, probe = run_extended([5, 4, 6, 3], target_items=18)
    assert collected == list(range(18))
    assert collected == probe.retired[:18]
    assert stats.gaps == 0
    assert stats.lost == 0
    assert stats.is_continuous is True


def test_reads_are_not_deduplicated_because_they_do_not_overlap():
    """The bug this pins: treating consecutive windows as overlapping.

    A read drains, so trimming a supposed overlap off the front of each window
    silently discards real instructions.
    """
    collected, _stats, probe = run_extended([4, 4, 4], target_items=12)
    assert len(collected) == 12
    assert collected == probe.retired[:12]


def test_periodic_code_does_not_confuse_the_join():
    """Content matching is what a tight loop breaks; arithmetic is not.

    On the oracle firmware a suffix/prefix matcher reported the same bogus
    3,708-item overlap for every pair of windows.
    """
    from jtrace.strace import Strace

    class Periodic(FakeDrainingProbe):
        def halt(self):
            if not self._per_slice:
                return
            count = self._per_slice.pop(0)
            new = [0x0800_0084 + (i % 2) * 2 for i in range(count)]
            self.retired.extend(new)
            self._executed += count
            self._pending = (self._pending + new)[-self.WINDOW :]

    probe = Periodic([4, 4, 4])
    stats = StreamingStats()
    collected = list(
        Strace.read_extended(
            probe, 0x0800_0000, target_items=12, slice_ms=0.0, stats=stats
        )
    )
    assert collected == probe.retired[:12]
    assert stats.gaps == 0


def test_overflowing_the_window_is_counted_and_measured():
    # 20 retired against an 8-item window: 12 are gone, and the count is exact
    # because it is executed-minus-returned, not an estimate.
    collected, stats, _probe = run_extended([20], target_items=64)
    assert stats.gaps == 1
    assert stats.lost == 12
    assert stats.is_continuous is False
    assert len(collected) == 8


def test_a_slice_that_retires_nothing_is_not_a_gap():
    collected, stats, probe = run_extended([5, 0, 3], target_items=8)
    assert collected == probe.retired[:8]
    assert stats.gaps == 0


def test_stops_once_the_target_count_is_reached():
    collected, _stats, _probe = run_extended([4] * 100, target_items=20)
    assert len(collected) == 20


def test_returns_chronological_order():
    collected, _stats, _probe = run_extended([5, 4], target_items=9)
    assert collected == sorted(collected)


def test_each_slice_becomes_one_block():
    """A read drains, so one drain is one uninterrupted run of the core. That
    is the segmentation the store is built on; it is not inferred."""
    _collected, _stats, _probe = run_extended([5, 4, 6], target_items=15)
    store = _extended_store([5, 4, 6], target_items=15)
    assert [len(block) for block in store.blocks] == [5, 4, 6]


def test_loss_is_recorded_on_the_block_it_preceded():
    """The 12 dropped instructions are the oldest of that slice, so they sit
    before the 8 that came back -- not after them, and not on the next block."""
    store = _extended_store([4, 20, 4], target_items=64)
    assert [block.lost_before for block in store.blocks] == [0, 12, 0]
    # Block 0 is 4 long, so the hole opens at index 4.
    assert store.boundaries() == [4]


def test_a_clean_seam_is_not_a_boundary():
    """Every slice ends in a halt, but a halt that lost nothing leaves the
    instruction stream contiguous. Marking all of them would shred a long
    capture into one call frame per slice."""
    store = _extended_store([4, 4, 4], target_items=12)
    assert len(store.blocks) == 3
    assert store.boundaries() == []


def test_halt_reason_is_recorded_raw_when_the_probe_offers_one():
    class Reasoning(FakeDrainingProbe):
        def halt_reason(self):
            return [types.SimpleNamespace(HaltReason=3, Index=0)]

    from jtrace.strace import Strace

    probe = Reasoning([4, 4])
    store = Strace.read_extended(
        probe, 0x0800_0000, target_items=8, slice_ms=0.0
    )
    assert [block.halt_reason for block in store.blocks] == [3, 3]


def test_a_probe_without_halt_reasons_still_captures():
    """Older DLLs and every fake lack it. A diagnostic must not fail a run."""
    store = _extended_store([4], target_items=4)
    assert store.blocks[0].halt_reason is None
    assert len(store) == 4


def test_stats_still_come_back_through_the_out_parameter():
    """`result.streaming` is documented surface; it is now derived from the
    blocks rather than accumulated alongside them, and must still agree."""
    _collected, stats, _probe = run_extended([4, 20, 4], target_items=64)
    assert (stats.gaps, stats.lost) == (1, 12)
    assert stats.polls >= 3
    assert stats.is_continuous is False


def test_an_unreachable_target_does_not_allocate_a_block_per_turn():
    """The loop spins until its deadline when the target stops executing. A
    block per idle turn would cost more memory than the trace itself."""
    store = _extended_store([4], target_items=10_000, max_slices=5_000)
    assert len(store.blocks) == 1
    assert store.stats().polls >= 5_000


def test_progress_rises_during_the_run_rather_than_flatlining():
    """Blocks are built after the loop, so the store is empty while it runs.
    Progress therefore has to come from the loop's own counter -- reporting
    `len(store)` would sit at zero for the whole capture and then jump."""
    from jtrace.strace import Strace

    seen = []
    Strace.read_extended(
        FakeDrainingProbe([4] * 5), 0x0800_0000, target_items=20,
        slice_ms=0.0, on_progress=lambda done, target: seen.append(done),
    )
    assert seen, "on_progress was never called"
    assert seen == sorted(seen), f"progress went backwards: {seen}"
    assert seen[0] > 0, "first report was zero -- reporting the empty store"
    assert seen[-1] >= 20


def test_a_caller_supplied_store_is_filled():
    """`store=` is public surface. Deferring the build must not turn it into a
    parameter that quietly stays empty."""
    from jtrace.store import TraceStore
    from jtrace.strace import Strace

    mine = TraceStore(capacity=1_000)
    returned = Strace.read_extended(
        FakeDrainingProbe([4, 4]), 0x0800_0000, target_items=8,
        slice_ms=0.0, store=mine,
    )
    assert returned is mine
    assert len(mine) == 8
    assert [len(b) for b in mine.blocks] == [4, 4]


def test_the_store_is_untouched_until_the_loop_ends():
    """The point of the change: no block work between a halt and the next go.

    A store that grew mid-loop would mean the reverse-and-copy is still in the
    hot path, which is what inflates the following halt and costs instructions.
    """
    from jtrace.store import TraceStore
    from jtrace.strace import Strace

    store = TraceStore(capacity=1_000)
    probe = FakeDrainingProbe([4] * 4)
    seen_at_go = []
    resume = probe.go
    probe.go = lambda: (seen_at_go.append(len(store)), resume())[1]

    Strace.read_extended(
        probe, 0x0800_0000, target_items=16, slice_ms=0.0, store=store
    )

    assert seen_at_go, "the loop never ran"
    assert all(n == 0 for n in seen_at_go), f"store grew mid-loop: {seen_at_go}"
    assert len(store) == 16


def test_deferring_does_not_defeat_the_stores_own_bound():
    """A caller whose capacity is below their target asked to keep a tail, not
    to hold the whole run in a stash.

    Deferring the build moved eviction out of the loop with it, so a capture
    bounded to 8 instructions still accumulated every window it ever read --
    839 KiB for a store the caller capped at 32 bytes.
    """
    from jtrace.store import TraceStore
    from jtrace.strace import Strace

    store = TraceStore(capacity=8)
    Strace.read_extended(
        FakeDrainingProbe([4] * 40), 0x0800_0000, target_items=160,
        slice_ms=0.0, store=store, max_slices=60,
    )
    # The bound held, and eviction ran during the capture rather than only at
    # the end -- which is what keeps the stash from growing without limit.
    assert len(store) <= 8
    assert store.stats().dropped_instructions > 0


def test_the_bound_does_not_fire_when_capacity_covers_the_target():
    """The flush is for callers who deliberately asked for a tail. Every
    capture this SDK builds for itself has capacity >= target, and those must
    keep the whole deferred build in one pass."""
    from jtrace.store import TraceStore
    from jtrace.strace import Strace

    store = TraceStore(capacity=1_000)
    Strace.read_extended(
        FakeDrainingProbe([4] * 5), 0x0800_0000, target_items=20,
        slice_ms=0.0, store=store,
    )
    assert len(store) == 20
    assert store.stats().dropped_instructions == 0


def test_auto_tuning_adapts_the_slice_to_the_measured_rate():
    """slice_ms=None must converge rather than over- or under-shooting."""
    from jtrace.strace import Strace

    probe = FakeDrainingProbe([4] * 40)
    stats = StreamingStats()
    Strace.read_extended(probe, 0x0800_0000, target_items=40, stats=stats)
    assert stats.polls > 0
    assert stats.gaps == 0


# -- ITM decoding ----------------------------------------------------------


def test_decodes_single_byte_stimulus_writes():
    # Header 0x01: port 0, one payload byte.
    data = bytes([0x01, ord("h"), 0x01, ord("i")])
    packets = decode_itm(data)
    assert [p.port for p in packets] == [0, 0]
    assert b"".join(p.data for p in packets) == b"hi"


def test_decodes_wider_payloads():
    assert decode_itm(bytes([0x02, 0xAA, 0xBB]))[0].data == b"\xaa\xbb"
    assert decode_itm(bytes([0x03, 1, 2, 3, 4]))[0].data == b"\x01\x02\x03\x04"


def test_port_number_comes_from_the_top_five_bits():
    # Port 5 -> header 0x29 (5 << 3 | 1).
    assert decode_itm(bytes([0x29, ord("x")]))[0].port == 5


def test_synchronisation_run_is_skipped():
    data = bytes([0x00] * 5 + [0x80] + [0x01, ord("a")])
    packets = decode_itm(data)
    assert len(packets) == 1
    assert packets[0].text == "a"


def test_protocol_packets_are_skipped_not_decoded():
    # A local timestamp: header with size bits 0 and the continuation bit set.
    data = bytes([0xC0, 0x81, 0x02]) + bytes([0x01, ord("z")])
    packets = decode_itm(data)
    assert [p.text for p in packets] == ["z"]


def test_truncated_trailing_packet_is_dropped():
    # Header says four payload bytes but only two are present.
    assert decode_itm(bytes([0x03, 1, 2])) == []


def test_empty_input():
    assert decode_itm(b"") == []


def test_packet_text_survives_invalid_utf8():
    assert decode_itm(bytes([0x01, 0xFF]))[0].text == "�"


# -- constants -------------------------------------------------------------


def test_strace_read_clamp_is_the_documented_value():
    # 256 KiB / 4 bytes: the fixed allocation in the DLL's read path.
    assert MAX_STRACE_ITEMS == 0x10000
    assert MAX_STRACE_ITEMS * 4 == 0x40000


def test_command_values_match_what_was_decoded_from_the_binary():
    # STRACE bounds Cmd at 3; TRACE at 0x32; RAWTRACE at 4; POWERTRACE at 6.
    assert max(int(c) for c in StraceCmd) == 3
    assert max(int(c) for c in TraceCmd) == 0x32
    assert max(int(c) for c in RawTraceCmd) == 4
    assert max(int(c) for c in PowerTraceCmd) == 6
    assert max(int(c) for c in SwoCmd) == 0x15


def test_trace_command_numbering():
    assert (TraceCmd.START, TraceCmd.STOP, TraceCmd.FLUSH) == (0, 1, 2)
    assert TraceCmd.GET_NUM_SAMPLES == 0x10
    assert TraceCmd.SET_CAPACITY == 0x12
    assert TraceCmd.GET_MAX_CAPACITY == 0x14
    assert TraceCmd.GET_REGION_PROPS_EX == 0x32


def test_rawtrace_numbering_matches_the_name_pointer_array():
    assert RawTraceCmd.START == 0
    assert RawTraceCmd.GET_TRACE_FREQ == 2
    assert RawTraceCmd.GET_CAPS == 4


def test_powertrace_setup_is_command_zero():
    assert PowerTraceCmd.SETUP == 0
    assert PowerTraceCmd.GET_NUM_ITEMS == 6


def test_trace_data_item_is_four_bytes():
    from jtrace.structs import TraceData

    assert ctypes.sizeof(TraceData) == 4
