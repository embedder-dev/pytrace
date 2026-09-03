"""Coverage row building.

The subtle rule here is that source lines are keyed by ``file:line`` and not by
line number. Keying on the number alone merges an inlined callee with its
caller, which under-counts srcLines for the function and, through the totals,
for the whole report -- a bug that makes coverage look *better* than it is.
"""

from jtrace.coverage import (
    SectionCounts,
    build_coverage_rows,
    compute_totals,
    run_count_lookup,
)
from jtrace.symbols import ResolvedFunction, ResolvedSource


class FakeSymbolizer:
    def __init__(self, functions, sources=None):
        self.functions = functions
        self.sources = sources or {}

    def resolve_function(self, address):
        target = address & ~1
        for name, entry, size in self.functions:
            start = entry & ~1
            if start <= target < start + size:
                return ResolvedFunction(name=name, addr=entry, size=size)
        return None

    def resolve_source(self, address):
        found = self.sources.get(address & ~1)
        return ResolvedSource(file=found[0], line=found[1]) if found else None


BASE = 0x0800_0000


def test_covered_and_uncovered_instructions_are_separated():
    syms = FakeSymbolizer([("f", BASE | 1, 8)])
    section = SectionCounts(
        base_address=BASE,
        instruction_starts=[BASE, BASE + 2, BASE + 4, BASE + 6],
        counts=[7, 0, 3, 0],
    )
    rows = build_coverage_rows([section], syms)
    assert len(rows.functions) == 1
    row = rows.functions[0]
    assert row.instructions == 4
    assert row.instructions_covered == 2
    assert row.instructions_executed == 10
    # runCount is the entry address's count, not the sum.
    assert row.run_count == 7


def test_trailing_halfword_of_a_32bit_instruction_is_not_counted():
    # Slot 1 is the second half of a 32-bit instruction: it never carries a
    # count and it is not an instruction start, so it must not appear in the
    # denominator.
    syms = FakeSymbolizer([("f", BASE | 1, 8)])
    section = SectionCounts(
        base_address=BASE,
        instruction_starts=[BASE, BASE + 4],
        counts=[5, 0, 5, 0],
    )
    row = build_coverage_rows([section], syms).functions[0]
    assert row.instructions == 2
    assert row.instructions_covered == 2


def test_same_line_number_in_two_files_stays_two_lines():
    syms = FakeSymbolizer(
        [("f", BASE | 1, 8)],
        sources={
            BASE: ("a.c", 10),
            BASE + 2: ("header.h", 10),
            BASE + 4: ("a.c", 10),
        },
    )
    section = SectionCounts(
        base_address=BASE,
        instruction_starts=[BASE, BASE + 2, BASE + 4],
        counts=[1, 0, 1],
    )
    rows = build_coverage_rows([section], syms)
    assert rows.functions[0].src_lines == 2
    assert rows.functions[0].src_lines_covered == 1
    assert {(r.file, r.line) for r in rows.lines} == {("a.c", 10), ("header.h", 10)}


def test_a_line_is_covered_if_any_of_its_instructions_ran():
    syms = FakeSymbolizer(
        [("f", BASE | 1, 8)],
        sources={BASE: ("a.c", 4), BASE + 2: ("a.c", 4)},
    )
    section = SectionCounts(
        base_address=BASE,
        instruction_starts=[BASE, BASE + 2],
        counts=[0, 9],
    )
    rows = build_coverage_rows([section], syms)
    assert rows.functions[0].src_lines_covered == 1
    assert rows.lines[0].instructions_covered == 1
    # A line's runCount is the max over its instructions, not the sum: the same
    # source line executing once can span several instructions.
    assert rows.lines[0].run_count == 9


def test_addresses_outside_any_function_are_skipped():
    syms = FakeSymbolizer([("f", BASE | 1, 4)])
    section = SectionCounts(
        base_address=BASE,
        instruction_starts=[BASE, BASE + 2, BASE + 100],
        counts=[1, 1] + [0] * 60,
    )
    rows = build_coverage_rows([section], syms)
    assert rows.functions[0].instructions == 2


def test_counts_shorter_than_the_section_read_as_zero():
    syms = FakeSymbolizer([("f", BASE | 1, 8)])
    section = SectionCounts(
        base_address=BASE,
        instruction_starts=[BASE, BASE + 2, BASE + 4],
        counts=[4],
    )
    row = build_coverage_rows([section], syms).functions[0]
    assert row.instructions == 3
    assert row.instructions_covered == 1


def test_totals_span_every_row():
    syms = FakeSymbolizer(
        [("a", BASE | 1, 4), ("b", (BASE + 4) | 1, 4)],
        sources={BASE: ("x.c", 1), BASE + 4: ("x.c", 2)},
    )
    section = SectionCounts(
        base_address=BASE,
        instruction_starts=[BASE, BASE + 2, BASE + 4, BASE + 6],
        counts=[3, 0, 0, 0],
    )
    rows = build_coverage_rows([section], syms)
    totals = compute_totals(rows.functions)
    assert totals.functions == 2
    assert totals.functions_covered == 1
    assert totals.instructions == 4
    assert totals.instructions_covered == 1
    assert totals.total_run_count == 3
    assert totals.function_percent == 50.0


def test_rows_sort_by_file_then_line_then_name():
    syms = FakeSymbolizer(
        [("zebra", BASE | 1, 2), ("apple", (BASE + 2) | 1, 2)],
        sources={BASE: ("b.c", 5), BASE + 2: ("a.c", 9)},
    )
    section = SectionCounts(
        base_address=BASE,
        instruction_starts=[BASE, BASE + 2],
        counts=[1, 1],
    )
    rows = build_coverage_rows([section], syms)
    assert [r.file for r in rows.functions] == ["a.c", "b.c"]


def test_run_count_lookup_spans_sections():
    lookup = run_count_lookup(
        [
            SectionCounts(base_address=0x1000, instruction_starts=[], counts=[1, 2]),
            SectionCounts(base_address=0x2000, instruction_starts=[], counts=[7, 8]),
        ]
    )
    assert lookup(0x1000) == 1
    assert lookup(0x1002) == 2
    assert lookup(0x2002) == 8
    assert lookup(0x9000) is None


def test_json_keys_are_the_protocol_names():
    syms = FakeSymbolizer([("f", BASE | 1, 2)], sources={BASE: ("a.c", 3)})
    section = SectionCounts(
        base_address=BASE, instruction_starts=[BASE], counts=[2]
    )
    rows = build_coverage_rows([section], syms)
    assert set(rows.functions[0].to_json()) == {
        "file",
        "line",
        "name",
        "srcLines",
        "srcLinesCovered",
        "instructions",
        "instructionsCovered",
        "runCount",
        "instructionsExecuted",
    }
    assert set(rows.lines[0].to_json()) == {
        "file",
        "line",
        "instructions",
        "instructionsCovered",
        "runCount",
    }
