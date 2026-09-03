"""ELF and DWARF reading, against the real oracle firmware.

The DWARF line table is cross-checked against pyelftools where it is installed.
That check is the reason this SDK can carry its own DWARF reader with a
straight face: an independent implementation agreeing on every instruction
address is much stronger evidence than any hand-written expectation.
"""

import bisect

import pytest

from jtrace.dwarf import LineRow, LineTable
from jtrace.elf import ElfFile
from jtrace.symbols import Symbolizer
from jtrace.thumb import instruction_starts


def test_reads_sections_and_entry(oracle_elf):
    elf = ElfFile(oracle_elf)
    assert not elf.is_64
    assert elf.machine == 0x28  # EM_ARM
    assert elf.entry & 1 == 1  # Thumb entry point
    sections = elf.executable_sections()
    assert [s.name for s in sections] == [".text"]
    assert sections[0].size > 0


def test_function_symbols_keep_the_thumb_bit(oracle_elf):
    elf = ElfFile(oracle_elf)
    by_name = {s.name: s for s in elf.function_symbols()}
    assert "main" in by_name
    assert "spin" in by_name
    # Every Thumb function's symbol value is odd. Masking it here would be the
    # bug that hides the function behind its own mapping symbol.
    assert by_name["main"].value & 1 == 1


def test_resolve_function_accepts_masked_and_unmasked_addresses(oracle_elf):
    elf = ElfFile(oracle_elf)
    main = next(s for s in elf.function_symbols() if s.name == "main")
    even = main.value & ~1
    assert elf.resolve_function(even).name == "main"
    assert elf.resolve_function(even | 1).name == "main"
    assert elf.resolve_function(even + 2).name == "main"


def test_resolve_function_respects_declared_size(oracle_elf):
    elf = ElfFile(oracle_elf)
    main = next(s for s in elf.function_symbols() if s.name == "main")
    past_end = (main.value & ~1) + main.size + 0x1000
    resolved = elf.resolve_function(past_end)
    assert resolved is None or resolved.name != "main"


def test_line_info_is_present_and_resolves(oracle_elf):
    symbolizer = Symbolizer(oracle_elf)
    assert symbolizer.has_line_info
    elf = symbolizer.elf
    section = elf.executable_sections()[0]
    starts = list(instruction_starts(elf.read_code(section), section.addr))
    resolved = [a for a in starts if symbolizer.resolve_source(a) is not None]
    # Not every address has line info -- padding between functions does not --
    # but the overwhelming majority must, or coverage rows come out empty.
    assert len(resolved) > 0.9 * len(starts)


def test_source_paths_are_absolute(oracle_elf):
    symbolizer = Symbolizer(oracle_elf)
    elf = symbolizer.elf
    section = elf.executable_sections()[0]
    for address in instruction_starts(elf.read_code(section), section.addr):
        source = symbolizer.resolve_source(address)
        if source is not None:
            # DWARF 4 puts the compilation directory only in .debug_info, so an
            # absolute path here proves that side of the parser ran.
            assert source.file.startswith("/"), source.file
            break
    else:
        pytest.fail("no address resolved to a source position")


def test_matches_pyelftools_on_every_instruction_address(oracle_elf):
    """Cross-check against an independent DWARF implementation."""
    pytest.importorskip("elftools", reason="pyelftools not installed")
    from elftools.elf.elffile import ELFFile

    rows = []
    with open(oracle_elf, "rb") as handle:
        dwarf = ELFFile(handle).get_dwarf_info()
        for unit in dwarf.iter_CUs():
            program = dwarf.line_program_for_CU(unit)
            entries = program["file_entry"]
            for entry in program.get_entries():
                state = entry.state
                if state is None:
                    continue
                name = (
                    entries[state.file - 1].name.decode()
                    if 1 <= state.file <= len(entries)
                    else "?"
                )
                rows.append(
                    (state.address, not state.end_sequence, name, state.line,
                     state.end_sequence)
                )
    rows.sort(key=lambda row: (row[0], row[1]))
    addresses = [row[0] for row in rows]

    def reference(address):
        position = bisect.bisect_right(addresses, address) - 1
        if position < 0:
            return None
        row = rows[position]
        return None if row[4] else (row[2], row[3])

    symbolizer = Symbolizer(oracle_elf)
    elf = symbolizer.elf
    section = elf.executable_sections()[0]
    starts = list(instruction_starts(elf.read_code(section), section.addr))

    mismatches = []
    for address in starts:
        mine = symbolizer.resolve_source(address)
        theirs = reference(address)
        if mine is None and theirs is None:
            continue
        if (
            mine is None
            or theirs is None
            or mine.file.rsplit("/", 1)[-1] != theirs[0].rsplit("/", 1)[-1]
            or mine.line != theirs[1]
        ):
            mismatches.append((hex(address), mine, theirs))
    assert not mismatches, mismatches[:5]


def test_end_sequence_row_loses_to_a_real_row_at_the_same_address():
    """The boundary case that the oracle firmware happens to exercise.

    An end_sequence row's address is one past the last byte of its sequence, so
    where it collides with the first row of the next compilation unit's code,
    the real row has to win.
    """
    table = LineTable(
        [
            LineRow(address=0x100, file="a.c", line=9, end_sequence=True),
            LineRow(address=0x100, file="b.c", line=1),
        ]
    )
    assert table.resolve(0x100) == ("b.c", 1)


def test_address_past_the_last_sequence_resolves_to_nothing():
    table = LineTable(
        [
            LineRow(address=0x100, file="a.c", line=1),
            LineRow(address=0x110, file="a.c", line=2, end_sequence=True),
        ]
    )
    assert table.resolve(0x104) == ("a.c", 1)
    assert table.resolve(0x110) is None
    assert table.resolve(0x200) is None
    assert table.resolve(0x00) is None


def test_missing_file_is_a_clear_error():
    from jtrace.errors import SymbolizationError

    with pytest.raises(SymbolizationError):
        ElfFile("/nonexistent/firmware.elf")


def test_non_elf_input_is_rejected(tmp_path):
    from jtrace.errors import SymbolizationError

    path = tmp_path / "not.elf"
    path.write_bytes(b"MZ\x90\x00" + b"\0" * 64)
    with pytest.raises(SymbolizationError):
        ElfFile(path)
