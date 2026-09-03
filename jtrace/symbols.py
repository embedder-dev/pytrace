"""Address resolution: the contract coverage and call-frame building share.

Mirrors a reference TypeScript implementation so a Python capture and a
TypeScript one attribute the same address to the same function and line. The
two Thumb-bit rules in :mod:`jtrace.constants` are the load-bearing part; both
are applied here and nowhere else.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .constants import THUMB_BIT
from .dwarf import LineTable, parse_comp_dirs, parse_line_table
from .elf import ElfFile


@dataclass(frozen=True)
class ResolvedFunction:
    name: str
    addr: int
    """The symbol's value, Thumb bit included -- not masked."""

    size: int


@dataclass(frozen=True)
class ResolvedSource:
    file: str
    line: int


@dataclass(frozen=True, slots=True)
class Span:
    """The address range over which symbolization does not change.

    ``[start, end)``. Both halves may be ``None`` -- a span covering a hole
    between functions, or code with no line information, is still a span, which
    is what keeps a miss as cheap as a hit.
    """

    start: int
    end: int
    function: ResolvedFunction | None
    source: ResolvedSource | None

    def contains(self, address: int) -> bool:
        return self.start <= address < self.end


SPAN_CACHE_SLOTS = 256
"""Entries in each direct-mapped span cache.

Direct-mapped rather than a dict: a dict keyed on the address is what the two
unbounded memos here used to be, and on a coverage run over a 512 KB image they
retained hundreds of megabytes. This is a fixed ~4 KB that never grows, and a
collision costs a recomputed bisect rather than a wrong answer.
"""

_SPAN_SHIFT = 4
"""Addresses within one 16-byte block share a slot, so 256 slots cover a 4 KB
working set without aliasing. A span wider than that simply replicates into the
slots actually touched, which makes the table self-tuning."""


def demangle_all(names: list[str]) -> dict[str, str]:
    """Demangle C++ symbol names, in one batch.

    Batched because a firmware image has thousands of symbols and a subprocess
    per name would dominate the capture. Falls back to the mangled names when
    no demangler is installed -- C firmware never notices, and a C++ image with
    mangled names in the report is much better than a failed capture.
    """
    mangled = sorted({name for name in names if name.startswith("_Z")})
    if not mangled:
        return {}

    try:
        import cxxfilt  # type: ignore[import-not-found]

        out: dict[str, str] = {}
        for name in mangled:
            try:
                out[name] = cxxfilt.demangle(name)
            except Exception:
                pass
        return out
    except ImportError:
        pass

    tool = (
        shutil.which("c++filt")
        or shutil.which("llvm-cxxfilt")
        or shutil.which("arm-none-eabi-c++filt")
    )
    if not tool:
        return {}
    try:
        result = subprocess.run(
            [tool],
            input="\n".join(mangled),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    lines = result.stdout.splitlines()
    if len(lines) != len(mangled):
        return {}
    return {
        name: demangled
        for name, demangled in zip(mangled, lines)
        if demangled and demangled != name
    }


class Symbolizer:
    """Resolves addresses against one ELF.

    Lookups are cached by *span* rather than by address. A trace asks about the
    same few hundred addresses tens of thousands of times, and the two bisects
    underneath are not free -- but the answer is constant across a whole
    function or line-table row, so caching each individual address is both
    redundant and unbounded. Two 256-entry direct-mapped tables replace what
    were two dicts that grew one entry per distinct address queried.

    The function and source tables are separate on purpose: it keeps
    :meth:`resolve_function` from forcing the DWARF line table to be parsed.
    :meth:`resolve_span`, which returns both, does force it.
    """

    def __init__(self, elf: ElfFile | str | Path, *, demangle: bool = True) -> None:
        # Keyed on "is this a path?" rather than "is this an ElfFile?", so
        # anything offering the same handful of methods can stand in --
        # the same duck typing frames.py and coverage.py already accept
        # for the symbolizer itself.
        self.elf = ElfFile(elf) if isinstance(elf, (str, Path)) else elf
        self._line_table: LineTable | None = None
        self._fn_cache: list[tuple[int, int, ResolvedFunction | None] | None] = [
            None
        ] * SPAN_CACHE_SLOTS
        self._src_cache: list[tuple[int, int, ResolvedSource | None] | None] = [
            None
        ] * SPAN_CACHE_SLOTS
        self._demangled: dict[str, str] = (
            demangle_all([s.name for s in self.elf.function_symbols()])
            if demangle
            else {}
        )

    @property
    def line_table(self) -> LineTable:
        """The merged line table, built on first use."""
        if self._line_table is None:
            debug_line = self.elf.section_data(".debug_line")
            if not debug_line:
                self._line_table = LineTable([])
                return self._line_table
            debug_str = self.elf.section_data(".debug_str")
            debug_line_str = self.elf.section_data(".debug_line_str")
            comp_dirs = parse_comp_dirs(
                self.elf.section_data(".debug_info"),
                self.elf.section_data(".debug_abbrev"),
                debug_str,
                debug_line_str,
            )
            self._line_table = parse_line_table(
                debug_line, debug_line_str, debug_str, comp_dirs
            )
        return self._line_table

    @property
    def has_line_info(self) -> bool:
        return len(self.line_table) > 0

    def _function_span(
        self, address: int
    ) -> tuple[int, int, ResolvedFunction | None]:
        slot = (address >> _SPAN_SHIFT) % SPAN_CACHE_SLOTS
        entry = self._fn_cache[slot]
        if entry is not None and entry[0] <= address < entry[1]:
            return entry
        # Queried at ``address | THUMB_BIT``: querying the even address lands on
        # the zero-size mapping symbol that sits at every Thumb function entry,
        # which hides the function and drops its first instruction.
        start, end, symbol = self.elf.function_span(address | THUMB_BIT)
        resolved = (
            ResolvedFunction(
                name=self._demangled.get(symbol.name, symbol.name),
                addr=symbol.value,
                size=symbol.size,
            )
            if symbol is not None
            else None
        )
        if not start <= address < end:
            # The bounds above are masked; the address asked about may not be.
            # An odd address sitting exactly on a function's end falls outside
            # its own span, so narrow to that one address rather than widen to a
            # range over which the answer is not in fact constant.
            start, end = address, address + 1
        entry = (start, end, resolved)
        self._fn_cache[slot] = entry
        return entry

    def _source_span(self, address: int) -> tuple[int, int, ResolvedSource | None]:
        slot = (address >> _SPAN_SHIFT) % SPAN_CACHE_SLOTS
        entry = self._src_cache[slot]
        if entry is not None and entry[0] <= address < entry[1]:
            return entry
        file, line, start, end = self.line_table.span(address)
        resolved = (
            ResolvedSource(file=file, line=line)
            if file is not None and line is not None
            else None
        )
        entry = (start, end, resolved)
        self._src_cache[slot] = entry
        return entry

    def resolve_function(self, address: int) -> ResolvedFunction | None:
        """The function containing ``address``."""
        return self._function_span(address)[2]

    def resolve_source(self, address: int) -> ResolvedSource | None:
        return self._source_span(address)[2]

    def resolve_span(self, address: int) -> Span:
        """Both answers, plus the range over which neither changes.

        The intersection of the function's range and the line-table row's, so a
        caller walking a program-counter stream can resolve once and then simply
        test containment until execution leaves the span.
        """
        fn_start, fn_end, function = self._function_span(address)
        src_start, src_end, source = self._source_span(address)
        return Span(
            start=max(fn_start, src_start),
            end=min(fn_end, src_end),
            function=function,
            source=source,
        )

    def runs(self, addresses: Iterable[int]) -> Iterator[tuple[int, int, Span]]:
        """Group a program-counter stream into maximal same-span runs.

        Yields ``(start_index, end_index, span)`` with ``end_index`` exclusive.
        This is the shape symbolization actually wants: a trace re-enters the
        same line of the same function for long stretches, and resolving once
        per stretch rather than once per instruction is the difference between
        one object per run and one per program counter.
        """
        span: Span | None = None
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


__all__ = [
    "SPAN_CACHE_SLOTS",
    "ResolvedFunction",
    "ResolvedSource",
    "Span",
    "Symbolizer",
    "demangle_all",
]
