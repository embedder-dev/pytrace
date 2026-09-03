"""Span resolution: the same answers as per-address lookup, bounded.

Symbolization used to memoise per exact address in two dicts that grew one
entry per distinct address queried -- on a coverage run over a 512 KB image
that retained more memory than the trace did. Spans replace it, and every test
here exists because a span is a *claim about a range*: get the range wrong and
the wrong answer is returned confidently for every address in it, which is
harder to notice than a lookup that fails.
"""

from __future__ import annotations

import random


from jtrace.constants import ADDRESS_MAX, THUMB_BIT
from jtrace.dwarf import LineRow, LineTable
from jtrace.symbols import SPAN_CACHE_SLOTS, ResolvedFunction, Symbolizer


# -- the line-table half ---------------------------------------------------


def table(*rows):
    return LineTable([LineRow(*row) for row in rows])


def test_span_ends_at_the_next_row():
    lt = table((0x1000, "a.c", 1), (0x1010, "a.c", 2))
    assert lt.span(0x1004) == ("a.c", 1, 0x1000, 0x1010)
    assert lt.span(0x1010) == ("a.c", 2, 0x1010, ADDRESS_MAX)


def test_span_agrees_with_resolve_everywhere():
    lt = table((0x1000, "a.c", 1), (0x1010, "a.c", 2), (0x1020, "a.c", 3))
    for address in range(0x0FF0, 0x1040, 2):
        file, line, start, end = lt.span(address)
        expected = lt.resolve(address)
        assert (None if file is None else (file, line)) == expected
        assert start <= address < end


def test_before_the_first_row_is_a_span_not_a_special_case():
    """A miss has to cache as cheaply as a hit, or an address outside the image
    re-runs the bisect every time it appears in the stream."""
    lt = table((0x1000, "a.c", 1))
    assert lt.span(0x0900) == (None, None, 0, 0x1000)


def test_an_end_sequence_row_produces_a_source_less_span():
    """end_sequence marks where a unit's code stops. Its range must resolve to
    nothing, not to the last real line of the unit before it."""
    lt = table((0x1000, "a.c", 1), (0x1010, "a.c", 9, True, True))
    assert lt.resolve(0x1014) is None
    assert lt.span(0x1014) == (None, None, 0x1010, ADDRESS_MAX)


def test_rows_sharing_an_address_still_give_a_forward_span():
    """An end_sequence row sorts before a real row at the same address, so the
    bisect can land on a duplicate. `end` must still be strictly greater than
    the address, or a span of zero width caches and never matches."""
    lt = table(
        (0x1000, "a.c", 1),
        (0x1010, "a.c", 9, True, True),
        (0x1010, "b.c", 1),
        (0x1020, "b.c", 2),
    )
    file, line, start, end = lt.span(0x1010)
    assert (file, line) == ("b.c", 1)
    assert start <= 0x1010 < end


def test_empty_table_spans_everything():
    assert table().span(0x1000) == (None, None, 0, ADDRESS_MAX)


# -- the function half -----------------------------------------------------


class Symbol:
    def __init__(self, name, value, size):
        self.name, self.value, self.size = name, value, size


class FakeElf:
    """Just enough ElfFile to build a Symbolizer with no line information."""

    def __init__(self, symbols):
        self._symbols = sorted(symbols, key=lambda s: s.value & ~1)
        self.function_span_calls = 0

    def function_symbols(self):
        return list(self._symbols)

    def section_data(self, _name):
        return b""

    def function_span(self, address):
        self.function_span_calls += 1
        target = address & ~1
        starts = [s.value & ~1 for s in self._symbols]
        position = -1
        for i, start in enumerate(starts):
            if start <= target:
                position = i
        if position < 0:
            return 0, (starts[0] if starts else ADDRESS_MAX), None
        symbol = self._symbols[position]
        start = starts[position]
        next_start = starts[position + 1] if position + 1 < len(starts) else ADDRESS_MAX
        if not symbol.size:
            return start, next_start, symbol
        end = min(start + symbol.size, next_start)
        return (start, end, symbol) if target < end else (end, next_start, None)


MAIN = Symbol("main", 0x0800_0101, 0x20)
GAP_AFTER_MAIN = Symbol("later", 0x0800_0200, 0x10)
NO_SIZE = Symbol("asm_stub", 0x0800_0301, 0)
TRAILER = Symbol("trailer", 0x0800_0400, 0x10)


def symbolizer(*symbols):
    return Symbolizer(FakeElf(list(symbols)), demangle=False)


def test_function_span_matches_resolve_function_across_a_hole():
    sym = symbolizer(MAIN, GAP_AFTER_MAIN)
    inside = sym.resolve_function(0x0800_0110)
    assert isinstance(inside, ResolvedFunction) and inside.name == "main"
    # Past main's declared size but before the next symbol: a hole.
    assert sym.resolve_function(0x0800_0180) is None
    assert sym.resolve_function(0x0800_0204).name == "later"


def test_a_zero_size_symbol_reaches_the_next_one():
    """Assembly routines and linker stubs carry no size. Refusing to resolve
    them leaves holes in the middle of a trace."""
    sym = symbolizer(NO_SIZE, TRAILER)
    assert sym.resolve_function(0x0800_0350).name == "asm_stub"
    assert sym.resolve_function(0x0800_0400).name == "trailer"


def test_resolve_function_keeps_the_thumb_bit_unmasked():
    """Callers do their own masking; frames.py compares `address == fn.addr`
    after masking, and a pre-masked addr would break recursion detection."""
    sym = symbolizer(MAIN)
    assert sym.resolve_function(0x0800_0100).addr == 0x0800_0101
    assert sym.resolve_function(0x0800_0100).addr & THUMB_BIT == 1


def test_the_function_side_is_queried_with_the_thumb_bit_set():
    """Querying the even address lands on the zero-size mapping symbol that
    sits at every Thumb entry, which hides the function and drops its first
    instruction. The asymmetry with the line side is deliberate."""
    seen = []

    class Recording(FakeElf):
        def function_span(self, address):
            seen.append(address)
            return super().function_span(address)

    Symbolizer(Recording([MAIN]), demangle=False).resolve_function(0x0800_0100)
    assert seen == [0x0800_0101]


# -- the cache ------------------------------------------------------------


def test_repeated_addresses_do_not_re_enter_the_elf():
    sym = symbolizer(MAIN)
    for _ in range(500):
        sym.resolve_function(0x0800_0110)
    assert sym.elf.function_span_calls == 1


def test_walking_a_function_costs_lookups_per_block_not_per_instruction():
    """The point of a span: the answer is constant across the range.

    Not literally one lookup -- the table is indexed by 16-byte block, so a
    span wider than that replicates into each slot it touches. What matters is
    that the count follows the function's *size*, not its instruction count.
    """
    sym = symbolizer(MAIN)  # 0x20 bytes == two 16-byte blocks
    addresses = list(range(0x0800_0100, 0x0800_0120, 2))
    for address in addresses:
        sym.resolve_function(address)
    assert len(addresses) == 16
    assert sym.elf.function_span_calls == 2


def test_cached_answers_match_an_uncached_reference_under_shuffling():
    """Direct-mapped means collisions evict. A collision must cost a recomputed
    bisect and nothing else -- never a stale answer for the evicting address.
    """
    symbols = [
        Symbol(f"fn{i}", 0x0800_0000 + i * 0x400 + 1, 0x100) for i in range(64)
    ]
    sym = symbolizer(*symbols)
    reference = FakeElf(symbols)

    addresses = [0x0800_0000 + i * 0x40 for i in range(64 * 16)]
    random.seed(5)
    random.shuffle(addresses)
    for address in addresses * 3:
        got = sym.resolve_function(address)
        _s, _e, expected = reference.function_span(address | THUMB_BIT)
        assert (got.name if got else None) == (expected.name if expected else None)


def test_the_cache_does_not_grow_with_distinct_addresses():
    """The regression this replaces: two dicts that retained one entry per
    address ever queried."""
    symbols = [
        Symbol(f"fn{i}", 0x0800_0000 + i * 0x100 + 1, 0x100) for i in range(200)
    ]
    sym = symbolizer(*symbols)
    for address in range(0x0800_0000, 0x0800_C800, 2):
        sym.resolve_function(address)
    assert len(sym._fn_cache) == SPAN_CACHE_SLOTS
    assert len(sym._src_cache) == SPAN_CACHE_SLOTS


def test_resolve_function_does_not_force_the_dwarf_parse():
    """Separate caches for the two halves, so a caller that only wants function
    names does not pay for the line table."""
    sym = symbolizer(MAIN)
    sym.resolve_function(0x0800_0110)
    assert sym._line_table is None


# -- spans and runs --------------------------------------------------------


def test_resolve_span_intersects_both_halves():
    sym = symbolizer(MAIN)
    span = sym.resolve_span(0x0800_0110)
    assert span.function is not None and span.function.name == "main"
    assert span.contains(0x0800_0110)
    assert span.start <= 0x0800_0110 < span.end


def test_runs_group_consecutive_addresses_and_cover_all_of_them():
    sym = symbolizer(MAIN, GAP_AFTER_MAIN)
    stream = [0x0800_0100, 0x0800_0102, 0x0800_0200, 0x0800_0202, 0x0800_0104]
    runs = list(sym.runs(stream))
    assert [(lo, hi) for lo, hi, _ in runs] == [(0, 2), (2, 4), (4, 5)]
    covered = [i for lo, hi, _ in runs for i in range(lo, hi)]
    assert covered == list(range(len(stream)))


def test_runs_of_an_empty_stream_yields_nothing():
    assert list(symbolizer(MAIN).runs([])) == []


# -- against the real thing ------------------------------------------------


def test_matches_uncached_resolution_on_a_real_elf(oracle_elf):
    """The equivalence that matters: same answers as the algorithm this
    replaced, over every instruction start plus odd and out-of-range probes.
    """
    from jtrace.elf import ElfFile
    from jtrace.thumb import instruction_starts

    elf = ElfFile(oracle_elf)
    section = elf.executable_sections()[0]
    sym = Symbolizer(elf)
    line_table = sym.line_table

    starts = list(instruction_starts(elf.read_code(section), section.addr))
    assert starts, "oracle ELF should decode to instructions"

    probes = starts + [a - 1 for a in starts] + [a + 1 for a in starts]
    probes += [section.addr - 4, section.addr + section.size + 16, 0, 0xFFFF_FFFF]
    random.seed(11)
    random.shuffle(probes)

    for address in probes:
        got = sym.resolve_function(address)
        expected = elf.resolve_function(address | THUMB_BIT)
        assert (got.name if got else None) == (
            expected.name if expected else None
        ), hex(address)

        source = sym.resolve_source(address)
        reference = line_table.resolve(address)
        assert (
            None if source is None else (source.file, source.line)
        ) == reference, hex(address)


def test_runs_agree_with_per_address_resolution_on_a_real_elf(oracle_elf):
    from jtrace.elf import ElfFile
    from jtrace.thumb import instruction_starts

    elf = ElfFile(oracle_elf)
    section = elf.executable_sections()[0]
    sym = Symbolizer(elf)
    stream = [a for a in instruction_starts(elf.read_code(section), section.addr)] * 3

    for lo, hi, span in sym.runs(stream):
        for index in range(lo, hi):
            address = stream[index]
            direct = sym.resolve_function(address)
            assert (span.function.name if span.function else None) == (
                direct.name if direct else None
            ), hex(address)
