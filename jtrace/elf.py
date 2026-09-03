"""A minimal ELF reader: sections and symbols, no dependencies.

Deliberately not pyelftools. This SDK exists so an agent can drop a script on a
bench machine and have it run; a hard dependency that may or may not be
installed defeats that. What is here is only what trace and coverage need --
executable sections to prime the trace cache with, and a function symbol table
to resolve program counters against.
"""

from __future__ import annotations

import struct
import zlib
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from .constants import ADDRESS_MAX
from .errors import SymbolizationError

_ELF_MAGIC = b"\x7fELF"

SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4
SHF_COMPRESSED = 0x800

SHT_NOBITS = 8
SHT_SYMTAB = 2

STT_OBJECT = 1
STT_FUNC = 2


@dataclass(frozen=True)
class Section:
    name: str
    sh_type: int
    flags: int
    addr: int
    offset: int
    size: int
    entsize: int
    link: int

    @property
    def is_executable(self) -> bool:
        return bool(self.flags & SHF_EXECINSTR)

    @property
    def is_allocated(self) -> bool:
        return bool(self.flags & SHF_ALLOC)

    @property
    def has_content(self) -> bool:
        return self.sh_type != SHT_NOBITS


@dataclass(frozen=True)
class Symbol:
    name: str
    value: int
    size: int
    info: int
    shndx: int

    @property
    def kind(self) -> int:
        return self.info & 0x0F

    @property
    def is_function(self) -> bool:
        return self.kind == STT_FUNC


class ElfFile:
    """A parsed ELF image.

    Section contents are read lazily and cached, so opening a large firmware
    ELF to ask for its .text bounds does not pull DWARF into memory.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._data = self.path.read_bytes()
        except OSError as error:
            raise SymbolizationError(f"Cannot read {path}: {error}") from error

        if self._data[:4] != _ELF_MAGIC:
            raise SymbolizationError(f"{path} is not an ELF file")

        self.is_64 = self._data[4] == 2
        self.is_little_endian = self._data[5] == 1
        self._endian = "<" if self.is_little_endian else ">"
        self.machine = self._u16(18)
        self.entry = self._read_addr(24)

        self._section_cache: dict[str, bytes] = {}
        self.sections: list[Section] = self._read_sections()
        self._by_name = {section.name: section for section in self.sections}

        self._symbols: list[Symbol] | None = None
        self._function_index: tuple[list[int], list[Symbol]] | None = None

    # -- primitives --------------------------------------------------------

    def _u8(self, offset: int) -> int:
        return self._data[offset]

    def _u16(self, offset: int) -> int:
        return struct.unpack_from(f"{self._endian}H", self._data, offset)[0]

    def _u32(self, offset: int) -> int:
        return struct.unpack_from(f"{self._endian}I", self._data, offset)[0]

    def _u64(self, offset: int) -> int:
        return struct.unpack_from(f"{self._endian}Q", self._data, offset)[0]

    def _read_addr(self, offset: int) -> int:
        return self._u64(offset) if self.is_64 else self._u32(offset)

    # -- sections ----------------------------------------------------------

    def _read_sections(self) -> list[Section]:
        if self.is_64:
            sh_off, sh_entsize_off, sh_num_off, sh_strndx_off = 40, 58, 60, 62
        else:
            sh_off, sh_entsize_off, sh_num_off, sh_strndx_off = 32, 46, 48, 50

        table_offset = self._read_addr(sh_off)
        entry_size = self._u16(sh_entsize_off)
        count = self._u16(sh_num_off)
        str_index = self._u16(sh_strndx_off)
        if table_offset == 0 or count == 0:
            return []

        raw: list[tuple[int, int, int, int, int, int, int, int]] = []
        for i in range(count):
            base = table_offset + i * entry_size
            if self.is_64:
                name, sh_type = self._u32(base), self._u32(base + 4)
                flags = self._u64(base + 8)
                addr, offset, size = (
                    self._u64(base + 16),
                    self._u64(base + 24),
                    self._u64(base + 32),
                )
                link = self._u32(base + 40)
                entsize = self._u64(base + 56)
            else:
                name, sh_type = self._u32(base), self._u32(base + 4)
                flags = self._u32(base + 8)
                addr, offset, size = (
                    self._u32(base + 12),
                    self._u32(base + 16),
                    self._u32(base + 20),
                )
                link = self._u32(base + 24)
                entsize = self._u32(base + 36)
            raw.append((name, sh_type, flags, addr, offset, size, entsize, link))

        # The name table is itself a section, so names can only be resolved
        # after every header has been read.
        str_offset = raw[str_index][4] if str_index < len(raw) else 0
        out: list[Section] = []
        for name_off, sh_type, flags, addr, offset, size, entsize, link in raw:
            out.append(
                Section(
                    name=self._cstr(str_offset + name_off),
                    sh_type=sh_type,
                    flags=flags,
                    addr=addr,
                    offset=offset,
                    size=size,
                    entsize=entsize,
                    link=link,
                )
            )
        return out

    def _cstr(self, offset: int) -> str:
        end = self._data.find(b"\0", offset)
        if end < 0:
            return ""
        return self._data[offset:end].decode("utf-8", errors="replace")

    def section(self, name: str) -> Section | None:
        return self._by_name.get(name)

    def section_data(self, name: str) -> bytes:
        """Section contents, transparently decompressed if SHF_COMPRESSED.

        ``-gz`` produces zlib-compressed debug sections; without this, DWARF
        parsing would fail on any build that uses it, with an error that points
        at the parser rather than at the compression.
        """
        cached = self._section_cache.get(name)
        if cached is not None:
            return cached
        section = self._by_name.get(name)
        if section is None or not section.has_content:
            self._section_cache[name] = b""
            return b""
        blob = self._data[section.offset : section.offset + section.size]
        if section.flags & SHF_COMPRESSED:
            header = 24 if self.is_64 else 12
            try:
                blob = zlib.decompress(blob[header:])
            except zlib.error as error:
                raise SymbolizationError(
                    f"Cannot decompress section {name} in {self.path}: {error}"
                ) from error
        self._section_cache[name] = blob
        return blob

    def executable_sections(self) -> list[Section]:
        """Sections worth tracing: executable, allocated, and actually present."""
        return [
            section
            for section in self.sections
            if section.is_executable and section.size > 0 and section.has_content
        ]

    def read_code(self, section: Section) -> bytes:
        return self._data[section.offset : section.offset + section.size]

    # -- symbols -----------------------------------------------------------

    @property
    def symbols(self) -> list[Symbol]:
        if self._symbols is None:
            self._symbols = self._read_symbols()
        return self._symbols

    def _read_symbols(self) -> list[Symbol]:
        symtab = self._by_name.get(".symtab")
        if symtab is None:
            return []
        strtab_index = symtab.link
        strtab_offset = (
            self.sections[strtab_index].offset
            if 0 <= strtab_index < len(self.sections)
            else 0
        )
        entry_size = symtab.entsize or (24 if self.is_64 else 16)
        count = symtab.size // entry_size if entry_size else 0

        out: list[Symbol] = []
        for i in range(count):
            base = symtab.offset + i * entry_size
            if self.is_64:
                name_off = self._u32(base)
                info = self._u8(base + 4)
                shndx = self._u16(base + 6)
                value = self._u64(base + 8)
                size = self._u64(base + 16)
            else:
                name_off = self._u32(base)
                value = self._u32(base + 4)
                size = self._u32(base + 8)
                info = self._u8(base + 12)
                shndx = self._u16(base + 14)
            if name_off == 0:
                continue
            out.append(
                Symbol(
                    name=self._cstr(strtab_offset + name_off),
                    value=value,
                    size=size,
                    info=info,
                    shndx=shndx,
                )
            )
        return out

    def _build_function_index(self) -> tuple[list[int], list[Symbol]]:
        functions = [
            symbol
            for symbol in self.symbols
            if symbol.is_function and symbol.value != 0
        ]
        # Sort by the masked address, because a Thumb symbol's value carries
        # the Thumb bit and an ARM symbol's does not. Sorting on the raw value
        # interleaves the two by one byte and breaks the bisect below.
        functions.sort(key=lambda s: (s.value & ~1, -s.size))
        # Keep the first symbol at each address: after the sort above that is
        # the one with the largest size, which is the real function rather than
        # a zero-size alias or mapping symbol sharing its entry point.
        deduped: list[Symbol] = []
        for symbol in functions:
            if deduped and (deduped[-1].value & ~1) == (symbol.value & ~1):
                continue
            deduped.append(symbol)
        return [s.value & ~1 for s in deduped], deduped

    def resolve_function(self, address: int) -> Symbol | None:
        """The function containing ``address``, or None.

        ``address`` may carry the Thumb bit or not; both work.

        A symbol with a declared size only matches inside it. A zero-size
        symbol matches until the next symbol starts -- assembly routines and
        some linker-generated stubs have no size, and refusing to resolve them
        would leave holes in the middle of a trace.
        """
        if self._function_index is None:
            self._function_index = self._build_function_index()
        addresses, symbols = self._function_index
        if not addresses:
            return None

        target = address & ~1
        position = bisect_right(addresses, target) - 1
        if position < 0:
            return None
        symbol = symbols[position]
        start = addresses[position]
        if symbol.size:
            return symbol if target < start + symbol.size else None
        next_start = (
            addresses[position + 1] if position + 1 < len(addresses) else None
        )
        if next_start is None or target < next_start:
            return symbol
        return None

    def function_span(self, address: int) -> tuple[int, int, "Symbol | None"]:
        """``(start, end, symbol)`` -- the range over which the answer to
        :meth:`resolve_function` is constant.

        Same rules as :meth:`resolve_function`, returning the bounds that method
        already computes and then throws away. A hole between functions comes
        back as a span with ``symbol=None``, so a miss caches as cheaply as a
        hit. ``end`` is :data:`ADDRESS_MAX` where the range is open at the top.

        Bounds are masked addresses. A symbol's reach is capped at the next
        symbol's start even when its declared size runs past it, because that is
        what the bisect in :meth:`resolve_function` does.
        """
        if self._function_index is None:
            self._function_index = self._build_function_index()
        addresses, symbols = self._function_index
        if not addresses:
            return 0, ADDRESS_MAX, None

        target = address & ~1
        position = bisect_right(addresses, target) - 1
        if position < 0:
            return 0, addresses[0], None

        symbol = symbols[position]
        start = addresses[position]
        next_start = (
            addresses[position + 1] if position + 1 < len(addresses) else ADDRESS_MAX
        )
        if not symbol.size:
            return start, next_start, symbol
        end = min(start + symbol.size, next_start)
        if target < end:
            return start, end, symbol
        return end, next_start, None

    def function_symbols(self) -> list[Symbol]:
        if self._function_index is None:
            self._function_index = self._build_function_index()
        return list(self._function_index[1])


__all__ = ["ElfFile", "Section", "Symbol"]
