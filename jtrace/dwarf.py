"""DWARF line-number tables, versions 2 through 5.

Enough DWARF to turn a program counter into ``file:line`` and no more. The
line-number program is a bytecode that reconstructs a sorted address->source
table; running it is the whole job.

``.debug_info`` is parsed too, but only far enough to read each compilation
unit's ``DW_AT_comp_dir`` and ``DW_AT_stmt_list``. That is what makes paths
absolute on DWARF 4, where the line table's directory list does not include
the compilation directory. DWARF 5 puts it in directory 0 and needs no help.
"""

from __future__ import annotations

import posixpath
from bisect import bisect_right
from dataclasses import dataclass

from .constants import ADDRESS_MAX

# Line program standard opcodes
DW_LNS_copy = 1
DW_LNS_advance_pc = 2
DW_LNS_advance_line = 3
DW_LNS_set_file = 4
DW_LNS_set_column = 5
DW_LNS_negate_stmt = 6
DW_LNS_set_basic_block = 7
DW_LNS_const_add_pc = 8
DW_LNS_fixed_advance_pc = 9
DW_LNS_set_prologue_end = 10
DW_LNS_set_epilogue_begin = 11
DW_LNS_set_isa = 12

# Extended opcodes
DW_LNE_end_sequence = 1
DW_LNE_set_address = 2
DW_LNE_define_file = 3
DW_LNE_set_discriminator = 4

# v5 content types
DW_LNCT_path = 1
DW_LNCT_directory_index = 2
DW_LNCT_timestamp = 3
DW_LNCT_size = 4
DW_LNCT_MD5 = 5

# Attributes we care about
DW_AT_name = 0x03
DW_AT_stmt_list = 0x10
DW_AT_comp_dir = 0x1B

DW_TAG_compile_unit = 0x11


class _Reader:
    """A cursor over a DWARF section."""

    __slots__ = ("data", "offset")

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset

    def eof(self) -> bool:
        return self.offset >= len(self.data)

    def u8(self) -> int:
        value = self.data[self.offset]
        self.offset += 1
        return value

    def i8(self) -> int:
        value = self.data[self.offset]
        self.offset += 1
        return value - 256 if value > 127 else value

    def u16(self) -> int:
        value = int.from_bytes(self.data[self.offset : self.offset + 2], "little")
        self.offset += 2
        return value

    def u24(self) -> int:
        value = int.from_bytes(self.data[self.offset : self.offset + 3], "little")
        self.offset += 3
        return value

    def u32(self) -> int:
        value = int.from_bytes(self.data[self.offset : self.offset + 4], "little")
        self.offset += 4
        return value

    def u64(self) -> int:
        value = int.from_bytes(self.data[self.offset : self.offset + 8], "little")
        self.offset += 8
        return value

    def uleb(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self.data[self.offset]
            self.offset += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    def sleb(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self.data[self.offset]
            self.offset += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                if byte & 0x40:
                    result -= 1 << shift
                return result

    def cstr(self) -> str:
        end = self.data.find(b"\0", self.offset)
        if end < 0:
            end = len(self.data)
        value = self.data[self.offset : end].decode("utf-8", errors="replace")
        self.offset = end + 1
        return value

    def bytes(self, count: int) -> bytes:
        value = self.data[self.offset : self.offset + count]
        self.offset += count
        return value

    def skip(self, count: int) -> None:
        self.offset += count


def _cstr_at(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("utf-8", errors="replace")


@dataclass
class LineRow:
    address: int
    file: str
    line: int
    is_stmt: bool = True
    end_sequence: bool = False


@dataclass
class _State:
    address: int = 0
    op_index: int = 0
    file: int = 1
    line: int = 1
    column: int = 0
    is_stmt: bool = True
    end_sequence: bool = False


class LineTable:
    """Address -> source position for a whole image.

    Rows from every compilation unit are merged and sorted once, so lookups are
    a bisect. ``end_sequence`` rows are kept: they mark where a unit's code
    stops, and dropping them would make the last function of one unit appear to
    extend into the next.
    """

    def __init__(self, rows: list[LineRow]) -> None:
        # An end_sequence row's address is one past the last byte of its
        # sequence, so where it collides with a real row -- which happens at
        # every boundary between two compilation units' code -- the real row
        # has to win. Sorting end_sequence first puts it there.
        rows.sort(key=lambda row: (row.address, not row.end_sequence))
        self._rows = rows
        self._addresses = [row.address for row in rows]

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def rows(self) -> list[LineRow]:
        return self._rows

    def resolve(self, address: int) -> tuple[str, int] | None:
        """The source position covering ``address``, or None if outside."""
        if not self._rows:
            return None
        position = bisect_right(self._addresses, address) - 1
        if position < 0:
            return None
        row = self._rows[position]
        if row.end_sequence:
            return None
        return row.file, row.line

    def span(self, address: int) -> tuple[str | None, int | None, int, int]:
        """``(file, line, start, end)`` -- the range over which the answer to
        :meth:`resolve` is constant.

        The next row's address is the end, and the bisect has already found it;
        :meth:`resolve` just discards it. ``file`` and ``line`` are ``None``
        exactly where :meth:`resolve` returns ``None`` -- before the first row,
        and inside an ``end_sequence`` row's range -- so a hole is a span too and
        costs a caller no more than a hit.

        Rows may share an address (an ``end_sequence`` row sorts before a real
        row at the same address), but ``bisect_right`` lands past every equal
        entry, so ``end`` is always strictly greater than ``address``.
        """
        if not self._rows:
            return None, None, 0, ADDRESS_MAX
        position = bisect_right(self._addresses, address) - 1
        if position < 0:
            return None, None, 0, self._addresses[0]
        start = self._addresses[position]
        end = (
            self._addresses[position + 1]
            if position + 1 < len(self._addresses)
            else ADDRESS_MAX
        )
        row = self._rows[position]
        if row.end_sequence:
            return None, None, start, end
        return row.file, row.line, start, end


@dataclass
class _CompilationUnit:
    stmt_list: int | None = None
    comp_dir: str = ""
    name: str = ""


def _read_initial_length(reader: _Reader) -> tuple[int, int]:
    """Returns (unit_length, offset_size). 0xFFFFFFFF selects 64-bit DWARF."""
    first = reader.u32()
    if first == 0xFFFFFFFF:
        return reader.u64(), 8
    return first, 4


def parse_line_table(
    debug_line: bytes,
    debug_line_str: bytes = b"",
    debug_str: bytes = b"",
    comp_dirs: dict[int, str] | None = None,
) -> LineTable:
    """Run every line-number program in ``.debug_line``.

    ``comp_dirs`` maps a program's offset within the section to the compilation
    directory of the unit that references it, which is how DWARF 2-4 paths
    become absolute.
    """
    rows: list[LineRow] = []
    reader = _Reader(debug_line)
    directories = comp_dirs or {}

    while reader.offset + 4 <= len(debug_line):
        program_offset = reader.offset
        try:
            unit_length, offset_size = _read_initial_length(reader)
        except IndexError:
            break
        if unit_length == 0:
            break
        end = reader.offset + unit_length
        if end > len(debug_line):
            break
        try:
            _parse_one_program(
                reader,
                end,
                offset_size,
                debug_line_str,
                debug_str,
                directories.get(program_offset, ""),
                rows,
            )
        except (IndexError, ValueError):
            # A single malformed unit must not cost the whole table. Trace with
            # partial line info is far more useful than trace with none.
            pass
        reader.offset = end

    return LineTable(rows)


def _parse_one_program(
    reader: _Reader,
    end: int,
    offset_size: int,
    debug_line_str: bytes,
    debug_str: bytes,
    comp_dir: str,
    rows: list[LineRow],
) -> None:
    version = reader.u16()
    if version >= 5:
        reader.u8()  # address_size
        reader.u8()  # segment_selector_size

    header_length = reader.u64() if offset_size == 8 else reader.u32()
    program_start = reader.offset + header_length

    min_inst_length = reader.u8()
    max_ops_per_inst = reader.u8() if version >= 4 else 1
    default_is_stmt = bool(reader.u8())
    line_base = reader.i8()
    line_range = reader.u8()
    opcode_base = reader.u8()
    standard_lengths = [reader.u8() for _ in range(max(0, opcode_base - 1))]

    if version >= 5:
        file_names = _read_v5_file_table(
            reader, offset_size, debug_line_str, debug_str, comp_dir
        )
        first_file_index = 0
    else:
        file_names = _read_v2_file_table(reader, comp_dir)
        first_file_index = 1

    def file_at(index: int) -> str:
        position = index - first_file_index
        if 0 <= position < len(file_names):
            return file_names[position]
        return f"<file {index}>"

    reader.offset = program_start
    state = _State(is_stmt=default_is_stmt, file=first_file_index or 1)

    def emit() -> None:
        rows.append(
            LineRow(
                address=state.address,
                file=file_at(state.file),
                line=state.line,
                is_stmt=state.is_stmt,
                end_sequence=state.end_sequence,
            )
        )

    def advance(operation_advance: int) -> None:
        if max_ops_per_inst <= 1:
            state.address += min_inst_length * operation_advance
            return
        total = state.op_index + operation_advance
        state.address += min_inst_length * (total // max_ops_per_inst)
        state.op_index = total % max_ops_per_inst

    while reader.offset < end:
        opcode = reader.u8()

        if opcode >= opcode_base:
            adjusted = opcode - opcode_base
            advance(adjusted // line_range)
            state.line += line_base + (adjusted % line_range)
            emit()
            continue

        if opcode == 0:
            length = reader.uleb()
            sub_end = reader.offset + length
            sub = reader.u8() if length else 0
            if sub == DW_LNE_end_sequence:
                state.end_sequence = True
                emit()
                state = _State(is_stmt=default_is_stmt, file=first_file_index or 1)
            elif sub == DW_LNE_set_address:
                width = sub_end - reader.offset
                state.address = int.from_bytes(reader.bytes(width), "little")
                state.op_index = 0
            reader.offset = sub_end
            continue

        if opcode == DW_LNS_copy:
            emit()
        elif opcode == DW_LNS_advance_pc:
            advance(reader.uleb())
        elif opcode == DW_LNS_advance_line:
            state.line += reader.sleb()
        elif opcode == DW_LNS_set_file:
            state.file = reader.uleb()
        elif opcode == DW_LNS_set_column:
            state.column = reader.uleb()
        elif opcode == DW_LNS_negate_stmt:
            state.is_stmt = not state.is_stmt
        elif opcode == DW_LNS_set_basic_block:
            pass
        elif opcode == DW_LNS_const_add_pc:
            advance((255 - opcode_base) // line_range)
        elif opcode == DW_LNS_fixed_advance_pc:
            state.address += reader.u16()
            state.op_index = 0
        elif opcode in (DW_LNS_set_prologue_end, DW_LNS_set_epilogue_begin):
            pass
        elif opcode == DW_LNS_set_isa:
            reader.uleb()
        else:
            # A standard opcode this version added and we do not model: its
            # operand count is in the header, so it can be skipped exactly.
            for _ in range(standard_lengths[opcode - 1] if opcode - 1 < len(standard_lengths) else 0):
                reader.uleb()


def _read_v2_file_table(reader: _Reader, comp_dir: str) -> list[str]:
    """DWARF 2-4: NUL-terminated directory list, then file entries.

    Index 0 of the directory list is not stored; it means "the compilation
    directory", which is why ``comp_dir`` has to be threaded in from
    ``.debug_info``.
    """
    include_dirs: list[str] = [comp_dir]
    while True:
        entry = reader.cstr()
        if not entry:
            break
        include_dirs.append(entry)

    files: list[str] = []
    while True:
        name = reader.cstr()
        if not name:
            break
        dir_index = reader.uleb()
        reader.uleb()  # mtime
        reader.uleb()  # length
        directory = (
            include_dirs[dir_index] if 0 <= dir_index < len(include_dirs) else ""
        )
        files.append(_join(directory, name, comp_dir))
    return files


def _read_v5_file_table(
    reader: _Reader,
    offset_size: int,
    debug_line_str: bytes,
    debug_str: bytes,
    comp_dir: str,
) -> list[str]:
    """DWARF 5: both lists are described by an explicit entry format."""
    dir_format = _read_entry_format(reader)
    dir_count = reader.uleb()
    directories: list[str] = []
    for _ in range(dir_count):
        entry = _read_entry(
            reader, dir_format, offset_size, debug_line_str, debug_str
        )
        directories.append(entry.get(DW_LNCT_path, ""))

    file_format = _read_entry_format(reader)
    file_count = reader.uleb()
    files: list[str] = []
    for _ in range(file_count):
        entry = _read_entry(
            reader, file_format, offset_size, debug_line_str, debug_str
        )
        name = entry.get(DW_LNCT_path, "")
        index = entry.get(DW_LNCT_directory_index, 0)
        directory = directories[index] if 0 <= index < len(directories) else ""
        # Directory 0 is the compilation directory in DWARF 5, so the fallback
        # only matters for a producer that omitted it.
        files.append(_join(directory, name, comp_dir or (directories[0] if directories else "")))
    return files


def _read_entry_format(reader: _Reader) -> list[tuple[int, int]]:
    count = reader.u8()
    return [(reader.uleb(), reader.uleb()) for _ in range(count)]


def _read_entry(
    reader: _Reader,
    entry_format: list[tuple[int, int]],
    offset_size: int,
    debug_line_str: bytes,
    debug_str: bytes,
) -> dict[int, object]:
    out: dict[int, object] = {}
    for content_type, form in entry_format:
        out[content_type] = _read_form(
            reader, form, offset_size, debug_line_str, debug_str
        )
    return out


def _read_form(
    reader: _Reader,
    form: int,
    offset_size: int,
    debug_line_str: bytes,
    debug_str: bytes,
) -> object:
    if form == 0x08:  # DW_FORM_string
        return reader.cstr()
    if form == 0x1F:  # DW_FORM_line_strp
        offset = reader.u64() if offset_size == 8 else reader.u32()
        return _cstr_at(debug_line_str, offset)
    if form == 0x0E:  # DW_FORM_strp
        offset = reader.u64() if offset_size == 8 else reader.u32()
        return _cstr_at(debug_str, offset)
    if form == 0x0B:  # data1
        return reader.u8()
    if form == 0x05:  # data2
        return reader.u16()
    if form == 0x06:  # data4
        return reader.u32()
    if form == 0x07:  # data8
        return reader.u64()
    if form == 0x1E:  # data16 (an MD5 hash, in practice)
        return reader.bytes(16)
    if form == 0x0F:  # udata
        return reader.uleb()
    if form == 0x0D:  # sdata
        return reader.sleb()
    if form == 0x09:  # block
        return reader.bytes(reader.uleb())
    if form in (0x1A, 0x22, 0x23):  # strx, loclistx, rnglistx
        return reader.uleb()
    if form == 0x25:  # strx1
        return reader.u8()
    if form == 0x26:  # strx2
        return reader.u16()
    if form == 0x27:  # strx3
        return reader.u24()
    if form == 0x28:  # strx4
        return reader.u32()
    raise ValueError(f"Unsupported DWARF form 0x{form:02x} in a line table")


def _join(directory: str, name: str, comp_dir: str) -> str:
    """Build the fullest path the DWARF actually supports.

    Never fabricates: if the pieces are relative and no compilation directory
    was recorded, the result stays relative rather than being anchored to
    something invented.
    """
    if posixpath.isabs(name) or (len(name) > 2 and name[1] == ":"):
        return name
    path = posixpath.join(directory, name) if directory else name
    if posixpath.isabs(path) or not comp_dir:
        return posixpath.normpath(path) if path else name
    return posixpath.normpath(posixpath.join(comp_dir, path))


# --------------------------------------------------------------------------
# .debug_info, only as far as comp_dir and stmt_list
# --------------------------------------------------------------------------


def parse_comp_dirs(
    debug_info: bytes,
    debug_abbrev: bytes,
    debug_str: bytes = b"",
    debug_line_str: bytes = b"",
) -> dict[int, str]:
    """Map each line program's section offset to its compilation directory.

    Only the first DIE of each unit is read -- that is the ``DW_TAG_compile_unit``
    that carries both attributes -- so this costs a few hundred bytes per unit
    rather than a full DIE tree walk.
    """
    out: dict[int, str] = {}
    reader = _Reader(debug_info)

    while reader.offset + 11 <= len(debug_info):
        unit_start = reader.offset
        try:
            unit_length, offset_size = _read_initial_length(reader)
        except IndexError:
            break
        if unit_length == 0:
            break
        unit_end = reader.offset + unit_length
        if unit_end > len(debug_info):
            break

        try:
            version = reader.u16()
            if version >= 5:
                reader.u8()  # unit_type
                address_size = reader.u8()
                abbrev_offset = reader.u64() if offset_size == 8 else reader.u32()
            else:
                abbrev_offset = reader.u64() if offset_size == 8 else reader.u32()
                address_size = reader.u8()

            abbrevs = _parse_abbrev(debug_abbrev, abbrev_offset)
            code = reader.uleb()
            declaration = abbrevs.get(code)
            if declaration is not None:
                unit = _read_cu_die(
                    reader,
                    declaration,
                    offset_size,
                    address_size,
                    debug_str,
                    debug_line_str,
                )
                if unit.stmt_list is not None:
                    out[unit.stmt_list] = unit.comp_dir
        except (IndexError, ValueError, KeyError):
            pass

        reader.offset = unit_end
        if reader.offset <= unit_start:
            break

    return out


def _parse_abbrev(
    debug_abbrev: bytes, offset: int
) -> dict[int, tuple[int, bool, list[tuple[int, int, int | None]]]]:
    """Parse one abbreviation table into {code: (tag, has_children, attrs)}."""
    table: dict[int, tuple[int, bool, list[tuple[int, int, int | None]]]] = {}
    reader = _Reader(debug_abbrev, offset)
    while not reader.eof():
        code = reader.uleb()
        if code == 0:
            break
        tag = reader.uleb()
        has_children = bool(reader.u8())
        attrs: list[tuple[int, int, int | None]] = []
        while True:
            attr = reader.uleb()
            form = reader.uleb()
            implicit = reader.sleb() if form == 0x21 else None
            if attr == 0 and form == 0:
                break
            attrs.append((attr, form, implicit))
        table[code] = (tag, has_children, attrs)
    return table


def _read_cu_die(
    reader: _Reader,
    declaration: tuple[int, bool, list[tuple[int, int, int | None]]],
    offset_size: int,
    address_size: int,
    debug_str: bytes,
    debug_line_str: bytes,
) -> _CompilationUnit:
    _tag, _has_children, attrs = declaration
    unit = _CompilationUnit()
    for attr, form, implicit in attrs:
        value = _read_info_form(
            reader, form, implicit, offset_size, address_size, debug_str, debug_line_str
        )
        if attr == DW_AT_comp_dir and isinstance(value, str):
            unit.comp_dir = value
        elif attr == DW_AT_stmt_list and isinstance(value, int):
            unit.stmt_list = value
        elif attr == DW_AT_name and isinstance(value, str):
            unit.name = value
    return unit


def _read_info_form(
    reader: _Reader,
    form: int,
    implicit: int | None,
    offset_size: int,
    address_size: int,
    debug_str: bytes,
    debug_line_str: bytes,
) -> object:
    """Read (or skip past) one attribute value in .debug_info."""
    if form == 0x01:  # addr
        return int.from_bytes(reader.bytes(address_size), "little")
    if form == 0x03:  # block2
        return reader.bytes(reader.u16())
    if form == 0x04:  # block4
        return reader.bytes(reader.u32())
    if form == 0x05:  # data2
        return reader.u16()
    if form == 0x06:  # data4
        return reader.u32()
    if form == 0x07:  # data8
        return reader.u64()
    if form == 0x08:  # string
        return reader.cstr()
    if form in (0x09, 0x18):  # block, exprloc
        return reader.bytes(reader.uleb())
    if form == 0x0A:  # block1
        return reader.bytes(reader.u8())
    if form in (0x0B, 0x0C, 0x11):  # data1, flag, ref1
        return reader.u8()
    if form == 0x0D:  # sdata
        return reader.sleb()
    if form == 0x0E:  # strp
        return _cstr_at(debug_str, reader.u64() if offset_size == 8 else reader.u32())
    if form in (0x0F, 0x15, 0x1A, 0x1B, 0x22, 0x23):  # udata, ref_udata, strx, addrx, ...
        return reader.uleb()
    if form in (0x10, 0x17, 0x1D):  # ref_addr, sec_offset, strp_sup
        return reader.u64() if offset_size == 8 else reader.u32()
    if form == 0x12:  # ref2
        return reader.u16()
    if form in (0x13, 0x1C):  # ref4, ref_sup4
        return reader.u32()
    if form in (0x14, 0x20):  # ref8, ref_sig8
        return reader.u64()
    if form == 0x16:  # indirect
        actual = reader.uleb()
        return _read_info_form(
            reader, actual, None, offset_size, address_size, debug_str, debug_line_str
        )
    if form == 0x19:  # flag_present
        return 1
    if form == 0x1E:  # data16
        return reader.bytes(16)
    if form == 0x1F:  # line_strp
        return _cstr_at(
            debug_line_str, reader.u64() if offset_size == 8 else reader.u32()
        )
    if form == 0x21:  # implicit_const
        return implicit
    if form in (0x25, 0x29):  # strx1, addrx1
        return reader.u8()
    if form in (0x26, 0x2A):  # strx2, addrx2
        return reader.u16()
    if form in (0x27, 0x2B):  # strx3, addrx3
        return reader.u24()
    if form in (0x28, 0x2C):  # strx4, addrx4
        return reader.u32()
    raise ValueError(f"Unsupported DWARF form 0x{form:02x} in .debug_info")


__all__ = ["LineRow", "LineTable", "parse_comp_dirs", "parse_line_table"]
