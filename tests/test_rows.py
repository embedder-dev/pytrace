"""Instruction rows generated on access.

The view replaces a `list[InstructionRow]` that callers already index, slice,
iterate and compare against literals. Everything here pins one of those, because
the failure mode of getting them wrong is not an exception -- it is a caller's
own tests failing for a reason that has nothing to do with their data.
"""

from __future__ import annotations

import array

from jtrace.artifacts import InstructionRow
from jtrace.rows import InstructionRows
from jtrace.symbols import ResolvedFunction, ResolvedSource, Span

MAIN = ("main", 0x0800_0101, 0x20)
WORKER = ("worker", 0x0800_0201, 0x20)


class FakeSymbolizer:
    """Enough of the real one to drive the view: spans plus the two lookups."""

    def __init__(self, functions=(MAIN, WORKER)):
        self.functions = functions
        self.span_calls = 0

    def _find(self, address):
        target = address & ~1
        for name, entry, size in self.functions:
            start = entry & ~1
            if start <= target < start + size:
                return name, start, start + size
        return None, None, None

    def resolve_span(self, address):
        self.span_calls += 1
        name, start, end = self._find(address)
        if name is None:
            return Span(start=address, end=address + 2, function=None, source=None)
        return Span(
            start=start,
            end=end,
            function=ResolvedFunction(name=name, addr=start | 1, size=end - start),
            source=ResolvedSource(file="demo.c", line=1 if name == "main" else 9),
        )

    def resolve_function(self, address):
        return self.resolve_span(address).function

    def resolve_source(self, address):
        return self.resolve_span(address).source

    def runs(self, addresses):
        span = None
        run_start = 0
        count = 0
        for index, address in enumerate(addresses):
            count = index + 1
            if span is not None and span.start <= address < span.end:
                continue
            if span is not None:
                yield run_start, index, span
            span = self.resolve_span(address)
            run_start = index
        if span is not None:
            yield run_start, count, span


ADDRESSES = [0x0800_0100, 0x0800_0102, 0x0800_0200, 0x0800_0202, 0x0800_0104]


def view(addresses=None, **kwargs):
    return InstructionRows(
        addresses if addresses is not None else ADDRESSES, FakeSymbolizer(), **kwargs
    )


# -- sequence compatibility ------------------------------------------------


def test_length_and_truthiness():
    assert len(view()) == 5
    assert view()
    assert not view([])


def test_indexing_matches_iteration():
    rows = view()
    assert [rows[i] for i in range(len(rows))] == list(rows)


def test_negative_indexing():
    rows = view()
    assert rows[-1].address == 0x0800_0104
    assert rows[-5].address == 0x0800_0100
    for bad in (5, -6):
        try:
            rows[bad]
        except IndexError:
            continue
        raise AssertionError(f"{bad} should have raised")


def test_slicing_returns_a_plain_list():
    rows = view()
    chunk = rows[1:3]
    assert isinstance(chunk, list)
    assert [row.address for row in chunk] == [0x0800_0102, 0x0800_0200]
    assert [row.index for row in rows[::2]] == [0, 2, 4]


def test_compares_equal_to_the_list_it_replaces():
    """`to_chronological([], ...) == []` and friends are real assertions in
    callers. A view that is not equal to its own contents breaks them all."""
    rows = view()
    assert rows == list(rows)
    assert view([]) == []
    assert rows != list(rows)[:-1]
    assert rows != "not a sequence"


def test_indices_are_contiguous_and_zero_based():
    """The store's reader pages on this ordering; index 0 must be the oldest."""
    assert [row.index for row in view()] == [0, 1, 2, 3, 4]


def test_base_index_offsets_the_reported_indices():
    rows = view(base_index=100)
    assert [row.index for row in rows] == [100, 101, 102, 103, 104]
    assert rows[0].index == 100


def test_works_over_an_array_not_just_a_list():
    rows = InstructionRows(array.array("I", ADDRESSES), FakeSymbolizer())
    assert [row.address for row in rows] == ADDRESSES


# -- content ---------------------------------------------------------------


def test_symbolization_is_carried_onto_every_row():
    rows = list(view())
    assert [row.function for row in rows] == [
        "main", "main", "worker", "worker", "main",
    ]
    assert [row.line for row in rows] == [1, 1, 9, 9, 1]


def test_unresolved_addresses_produce_rows_with_no_symbol():
    rows = list(view([0x2000_0000, 0x2000_0002]))
    assert all(row.function is None and row.file is None for row in rows)
    assert [row.address for row in rows] == [0x2000_0000, 0x2000_0002]


def test_run_count_is_per_address_not_per_run():
    counts = {0x0800_0100: 7, 0x0800_0102: 3}
    rows = InstructionRows(
        ADDRESSES, FakeSymbolizer(), lambda a: counts.get(a)
    )
    assert [row.run_count for row in rows] == [7, 3, None, None, None]


def test_rows_are_generated_not_retained():
    """Two accesses to the same position produce equal but distinct objects.
    If they were identical the rows would be held, which is the cost this whole
    module exists to avoid."""
    rows = view()
    first, second = rows[0], rows[0]
    assert first == second
    assert first is not second


# -- runs ------------------------------------------------------------------


def test_runs_group_consecutive_same_span_instructions():
    runs = list(view().runs())
    assert [(r.start_index, r.end_index, r.function) for r in runs] == [
        (0, 2, "main"),
        (2, 4, "worker"),
        (4, 5, "main"),
    ]
    assert [len(r) for r in runs] == [2, 2, 1]


def test_runs_cover_every_instruction_exactly_once():
    runs = list(view().runs())
    covered = [i for r in runs for i in range(r.start_index, r.end_index)]
    assert covered == list(range(len(ADDRESSES)))


def test_iterating_resolves_once_per_run_not_once_per_instruction():
    """This is the whole point: a trace re-enters the same line for stretches,
    and one resolved object per stretch instead of per program counter is the
    difference between a bounded cache and an unbounded one."""
    long_stream = [0x0800_0100 + (i % 2) * 2 for i in range(1000)]
    symbolizer = FakeSymbolizer()
    rows = InstructionRows(long_stream, symbolizer)
    assert len(list(rows)) == 1000
    assert symbolizer.span_calls == 1


def test_runs_of_an_empty_stream():
    assert list(view([]).runs()) == []
