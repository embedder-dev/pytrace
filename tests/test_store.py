"""The trace store: indexing, eviction, and what happens at a block seam.

The seam is the point of the whole structure. A capture longer than one probe
buffer is a sequence of run/halt slices, and the two things that go wrong when
that is stored as one flat array are invisible rather than loud: loss between
slices reads as contiguous execution, and a cycle count interpolated across a
halt reads as a plausible number. Both are pinned here.
"""

from __future__ import annotations

import array

import pytest

from jtrace.constants import DEFAULT_TRACE_CAPACITY
from jtrace.store import TraceStore


def build(*sizes: int, capacity: int = DEFAULT_TRACE_CAPACITY) -> TraceStore:
    """A store of consecutive integers, split into blocks of the given sizes."""
    store = TraceStore(capacity=capacity)
    value = 0
    for size in sizes:
        store.append_block(range(value, value + size))
        value += size
    return store


# -- indexing --------------------------------------------------------------


def test_indexes_as_one_flat_stream_across_blocks():
    store = build(3, 4, 2)
    assert len(store) == 9
    assert list(store) == list(range(9))
    assert [store[i] for i in range(9)] == list(range(9))


def test_block_edges_resolve_to_the_right_block():
    """Off-by-one here puts an instruction in the wrong run, which then gets
    the wrong halt reason and the wrong block's cycle stamps."""
    store = build(3, 4, 2)
    expected = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (1, 3), (2, 0), (2, 1)]
    got = [
        (store.block_index_at(i), store.block_at(i)[1]) for i in range(len(store))
    ]
    assert got == expected


def test_empty_blocks_are_skipped_by_the_bisect():
    """A slice can retire nothing. It still counts as a poll, and it must not
    capture an index that belongs to the next block."""
    store = TraceStore()
    store.append_block([0, 1, 2])
    store.append_block([])
    store.append_block([3, 4])
    assert list(store) == [0, 1, 2, 3, 4]
    assert store.block_index_at(3) == 2
    assert store.stats().polls == 3


def test_negative_and_slice_indexing():
    store = build(3, 4, 2)
    assert store[-1] == 8
    assert store[-9] == 0
    assert store[2:5] == [2, 3, 4]
    assert store[::-1] == list(range(8, -1, -1))
    with pytest.raises(IndexError):
        store[9]
    with pytest.raises(IndexError):
        store[-10]


# -- eviction --------------------------------------------------------------


def test_eviction_drops_whole_blocks_and_advances_origin():
    store = build(4, 4, 4, capacity=9)
    # 12 > 9, so the oldest block goes entirely -- not four instructions from
    # wherever the bound happened to land.
    assert len(store) == 8
    assert list(store) == list(range(4, 12))
    assert store.origin == 4
    assert store.stats().dropped_blocks == 1
    assert store.stats().dropped_instructions == 4


def test_eviction_keeps_indices_consistent():
    store = build(4, 4, 4, capacity=9)
    assert store[0] == 4
    assert store.block_index_at(0) == 0
    assert store.block_at(0)[0] is store.blocks[0]
    assert store.block_starts == (0, 4)


def test_a_single_oversized_block_is_not_evicted_to_nothing():
    """Capacity is a target, not a guarantee: dropping the only block would
    throw away the capture rather than bound it."""
    store = TraceStore(capacity=4)
    store.append_block(range(100))
    assert len(store) == 100


def test_lifetime_counters_survive_eviction():
    """Eviction destroys the evidence of loss. Reporting a capture as lossless
    because its lossy part scrolled out would be worse than not reporting."""
    store = TraceStore(capacity=6)
    store.append_block(range(4))
    store.append_block(range(4, 8), lost_before=99)
    store.append_block(range(8, 12))
    stats = store.stats()
    assert stats.dropped_blocks >= 1
    assert stats.lost == 99
    assert stats.gaps == 1
    assert stats.is_continuous is False
    assert stats.total_read == 12


def test_stats_is_a_copy_not_the_live_object():
    store = build(2)
    snapshot = store.stats()
    store.append_block([9])
    assert snapshot.polls == 1
    assert store.stats().polls == 2


# -- truncate --------------------------------------------------------------


def test_truncate_keeps_the_oldest_and_may_split_a_block():
    store = build(4, 4, 4)
    store.truncate_to(6)
    assert list(store) == [0, 1, 2, 3, 4, 5]
    assert len(store.blocks) == 2
    assert len(store.blocks[1]) == 2


def test_truncate_drops_stamps_that_fall_outside_the_cut():
    store = TraceStore()
    store.append_block(range(10), cycles=[100, 200, 300], stamp_at=[0, 5, 9])
    store.truncate_to(6)
    block = store.blocks[0]
    assert list(block.stamp_at) == [0, 5]
    assert list(block.cycles) == [100, 200]


def test_truncate_to_more_than_held_is_a_no_op():
    store = build(3, 3)
    store.truncate_to(99)
    assert len(store) == 6


# -- boundaries ------------------------------------------------------------


def test_boundaries_mark_loss_not_every_seam():
    """A clean run/halt seam is a break in time but not in the instruction
    stream. Treating every seam as a discontinuity would shred a long capture
    into one call frame per slice."""
    store = TraceStore()
    store.append_block(range(4))
    store.append_block(range(4, 8))
    store.append_block(range(8, 12), lost_before=17)
    assert store.boundaries() == [8]


def test_leading_loss_is_not_a_boundary():
    """Instructions lost before the first retained block are the window being
    a tail, which the summary already reports; there is no seam inside the
    stream to split at."""
    store = TraceStore()
    store.append_block(range(4), lost_before=50)
    store.append_block(range(4, 8))
    assert store.boundaries() == []


# -- time ------------------------------------------------------------------


def test_cycle_is_exact_on_a_stamp_and_interpolated_between():
    store = TraceStore()
    store.append_block(range(10), cycles=[1000, 2000], stamp_at=[0, 8])
    at_stamp = store.estimate_cycle(8)
    assert at_stamp is not None
    assert (at_stamp.cycle, at_stamp.exact, at_stamp.span) == (2000, True, 0)

    between = store.estimate_cycle(4)
    assert between is not None
    assert between.cycle == 1500
    assert between.exact is False
    assert between.span == 8
    assert between.extrapolated is False


def test_cycle_past_the_last_stamp_is_flagged_as_extrapolated():
    store = TraceStore()
    store.append_block(range(10), cycles=[1000, 2000], stamp_at=[0, 8])
    beyond = store.estimate_cycle(9)
    assert beyond is not None
    assert beyond.extrapolated is True


def test_a_block_without_two_stamps_returns_none_rather_than_guessing():
    store = TraceStore()
    store.append_block(range(4))
    store.append_block(range(4, 8), cycles=[500], stamp_at=[2])
    assert store.estimate_cycle(0) is None
    assert store.estimate_cycle(5) is None
    exact = store.estimate_cycle(6)
    assert exact is not None and exact.exact is True and exact.cycle == 500


def test_cycles_are_never_interpolated_across_a_halt():
    """The core was stopped between these blocks for an unknown number of
    cycles. A store that interpolated across the seam would return a confident
    number for a quantity nobody measured."""
    store = TraceStore()
    store.append_block(range(4), cycles=[100, 130], stamp_at=[0, 3])
    store.append_block(range(4, 8), cycles=[9000, 9030], stamp_at=[0, 3])
    first = store.estimate_cycle(3)
    second = store.estimate_cycle(4)
    assert first is not None and first.cycle == 130 and first.block == 0
    assert second is not None and second.block == 1
    # Not the ~4500 a seam-crossing interpolation would produce.
    assert second.cycle == 9000


def test_estimate_index_inverts_estimate_cycle():
    store = TraceStore()
    store.append_block(range(20), cycles=[1000, 3000], stamp_at=[0, 10])
    assert store.estimate_index(2000, block=0) == pytest.approx(5.0)
    estimate = store.estimate_cycle(5)
    assert estimate is not None and estimate.cycle == 2000


def test_estimate_index_refuses_an_ambiguous_cycle():
    """Two blocks covering the same cycle range means the counter restarted or
    the answer is in both. Picking one silently is the failure mode."""
    store = TraceStore()
    store.append_block(range(10), cycles=[100, 200], stamp_at=[0, 9])
    store.append_block(range(10, 20), cycles=[100, 200], stamp_at=[0, 9])
    assert store.estimate_index(150) is None
    assert store.estimate_index(150, block=1) == pytest.approx(10 + 4.5)


def test_cycles_continuous_is_none_when_nothing_was_observed():
    store = build(4, 4)
    assert store.cycles_continuous is None


def test_cycles_continuous_reports_what_the_stamps_actually_show():
    forward = TraceStore()
    forward.append_block(range(4), cycles=[100, 130], stamp_at=[0, 3])
    forward.append_block(range(4, 8), cycles=[900, 930], stamp_at=[0, 3])
    assert forward.cycles_continuous is True

    backward = TraceStore()
    backward.append_block(range(4), cycles=[900, 930], stamp_at=[0, 3])
    backward.append_block(range(4, 8), cycles=[100, 130], stamp_at=[0, 3])
    assert backward.cycles_continuous is False


# -- construction ----------------------------------------------------------


def test_parallel_stamp_arrays_must_match():
    store = TraceStore()
    with pytest.raises(ValueError, match="parallel"):
        store.append_block(range(4), cycles=[1, 2], stamp_at=[0])


def test_an_existing_array_is_adopted_rather_than_copied():
    store = TraceStore()
    pcs = array.array("I", [1, 2, 3])
    block = store.append_block(pcs)
    assert block.pcs is pcs


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        TraceStore(capacity=0)


# -- snapshot --------------------------------------------------------------


def test_snapshot_round_trips_everything_the_store_holds(tmp_path):
    """A snapshot is the store, not the rows derived from it. That is what
    makes a capture re-symbolizable later without going near a probe."""
    store = TraceStore(capacity=999)
    store.append_block(
        range(100), cycles=[10, 900], stamp_at=[0, 99], halt_reason=3, wall_ns=42
    )
    store.append_block(range(100, 150), lost_before=7)

    restored = TraceStore.load(store.save(tmp_path / "t.jt1"))

    assert list(restored) == list(store)
    assert restored.capacity == 999
    assert restored.boundaries() == store.boundaries()
    assert [b.lost_before for b in restored.blocks] == [0, 7]
    assert [b.halt_reason for b in restored.blocks] == [3, None]
    assert [b.wall_ns for b in restored.blocks] == [42, None]
    assert list(restored.blocks[0].cycles) == [10, 900]
    assert list(restored.blocks[0].stamp_at) == [0, 99]


def test_snapshot_preserves_evidence_of_what_was_evicted(tmp_path):
    store = TraceStore(capacity=6)
    store.append_block(range(4))
    store.append_block(range(4, 8), lost_before=99)
    store.append_block(range(8, 12))

    restored = TraceStore.load(store.save(tmp_path / "t.jt1"))
    assert restored.stats().dropped_instructions == store.stats().dropped_instructions
    assert restored.stats().lost == 99
    assert restored.origin == store.origin


def test_snapshot_carries_caller_metadata(tmp_path):
    store = build(4)
    path = store.save(tmp_path / "t.jt1", meta={"elfSha256": "abc", "device": "x"})
    assert TraceStore.load(path).meta == {"elfSha256": "abc", "device": "x"}


def test_snapshot_is_far_smaller_than_the_stream(tmp_path):
    """Trace is extremely repetitive -- a loop revisits the same addresses --
    so this is the difference between a snapshot being a habit and a decision."""
    store = TraceStore()
    store.append_block(array.array("I", [0x0800_0100 + (i % 32) * 2 for i in range(50_000)]))
    path = store.save(tmp_path / "t.jt1")
    assert path.stat().st_size < 50_000 * 4 // 20


def test_lzma_is_available_for_archival(tmp_path):
    store = build(64)
    path = store.save(tmp_path / "t.jt1", codec="lzma")
    assert list(TraceStore.load(path)) == list(store)


def test_a_foreign_file_is_rejected_by_name(tmp_path):
    from jtrace.errors import TraceError

    bogus = tmp_path / "nope.jt1"
    bogus.write_bytes(b"definitely not a snapshot")
    with pytest.raises(TraceError, match="not a pytrace trace snapshot"):
        TraceStore.load(bogus)


def test_a_future_snapshot_version_is_refused_rather_than_misread(tmp_path):
    import json
    import struct

    from jtrace.errors import TraceError
    from jtrace.store import SNAPSHOT_MAGIC

    header = json.dumps({"version": 99, "codec": "zlib", "blocks": []}).encode()
    path = tmp_path / "future.jt1"
    path.write_bytes(SNAPSHOT_MAGIC + struct.pack("<I", len(header)) + header)
    with pytest.raises(TraceError, match="version"):
        TraceStore.load(path)


def test_an_empty_store_round_trips(tmp_path):
    restored = TraceStore.load(TraceStore().save(tmp_path / "t.jt1"))
    assert len(restored) == 0
    assert restored.boundaries() == []


def test_describe_reads_the_shape_without_decompressing_a_block(tmp_path):
    """Inspecting a capture must not cost materialising it. Everything about
    its shape is in the header."""
    store = TraceStore()
    store.stamp_order_observed = True
    store.append_block(range(100), cycles=[10, 900], stamp_at=[0, 99])
    store.append_block(range(100, 150), lost_before=7)
    path = store.save(tmp_path / "t.jt1")

    header = TraceStore.describe(path)
    assert header["instructionCount"] == len(store) == 150
    assert header["blockCount"] == 2
    assert header["boundaries"] == store.boundaries() == [100]
    assert header["stampedBlocks"] == 1
    assert header["stampOrderObserved"] is True
    assert header["stats"]["lost"] == 7


def test_describe_rejects_the_same_files_load_does(tmp_path):
    from jtrace.errors import TraceError

    bogus = tmp_path / "nope.jt1"
    bogus.write_bytes(b"definitely not a snapshot")
    with pytest.raises(TraceError, match="not a pytrace trace snapshot"):
        TraceStore.describe(bogus)


def test_observed_stamp_order_survives_a_round_trip(tmp_path):
    for observed in (True, False, None):
        store = build(4)
        store.stamp_order_observed = observed
        restored = TraceStore.load(store.save(tmp_path / f"{observed}.jt1"))
        assert restored.stamp_order_observed is observed
