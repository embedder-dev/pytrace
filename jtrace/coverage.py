"""Per-function and per-line coverage rows.

Semantics are pinned to a reference TypeScript implementation, for the same
reason :mod:`jtrace.frames` is: both producers write into one store that one
coverage viewer reads, so a divergence here would be indistinguishable from a
counting bug.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .constants import HALFWORD_BYTES, THUMB_ADDRESS_MASK
from .symbols import Symbolizer


@dataclass
class SectionCounts:
    """Execution counts for one executable section.

    ``counts`` is indexed by halfword slot from ``base_address``;
    ``instruction_starts`` says which of those slots actually begin an
    instruction.
    """

    base_address: int
    instruction_starts: Sequence[int]
    counts: Sequence[int]


@dataclass
class FunctionRow:
    file: str
    line: int
    name: str
    src_lines: int
    src_lines_covered: int
    instructions: int
    instructions_covered: int
    run_count: int
    instructions_executed: int

    def to_json(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "name": self.name,
            "srcLines": self.src_lines,
            "srcLinesCovered": self.src_lines_covered,
            "instructions": self.instructions,
            "instructionsCovered": self.instructions_covered,
            "runCount": self.run_count,
            "instructionsExecuted": self.instructions_executed,
        }


@dataclass
class LineRow:
    file: str
    line: int
    instructions: int
    instructions_covered: int
    run_count: int

    def to_json(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "instructions": self.instructions,
            "instructionsCovered": self.instructions_covered,
            "runCount": self.run_count,
        }


@dataclass
class CoverageRows:
    functions: list[FunctionRow]
    lines: list[LineRow]


@dataclass
class Totals:
    functions: int = 0
    functions_covered: int = 0
    src_lines: int = 0
    src_lines_covered: int = 0
    instructions: int = 0
    instructions_covered: int = 0
    total_run_count: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "functions": self.functions,
            "functionsCovered": self.functions_covered,
            "srcLines": self.src_lines,
            "srcLinesCovered": self.src_lines_covered,
            "instructions": self.instructions,
            "instructionsCovered": self.instructions_covered,
            "totalRunCount": self.total_run_count,
        }

    @property
    def function_percent(self) -> float:
        return 100.0 * self.functions_covered / self.functions if self.functions else 0.0

    @property
    def instruction_percent(self) -> float:
        return (
            100.0 * self.instructions_covered / self.instructions
            if self.instructions
            else 0.0
        )

    @property
    def line_percent(self) -> float:
        return (
            100.0 * self.src_lines_covered / self.src_lines if self.src_lines else 0.0
        )


@dataclass
class _FunctionAccumulator:
    name: str
    entry: int
    file: str
    line: int
    instructions: int = 0
    instructions_covered: int = 0
    instructions_executed: int = 0
    run_count: int = 0
    lines: dict[str, bool] = field(default_factory=dict)
    """Keyed by ``file:line``, not by line number.

    An inlined callee or a body in a header puts two different files under the
    same line number, and keying on the number alone silently merges them --
    which under-counts srcLines for the function and, through
    :func:`compute_totals`, for the whole report.
    """


@dataclass
class _LineAccumulator:
    file: str
    line: int
    instructions: int = 0
    instructions_covered: int = 0
    run_count: int = 0


def build_coverage_rows(
    sections: Sequence[SectionCounts], symbolizer: Symbolizer
) -> CoverageRows:
    """Turn per-slot execution counts into per-function and per-line rows.

    Walks every real instruction start in each section -- not every halfword
    slot, which would count the tail of each 32-bit Thumb-2 instruction as an
    uncovered instruction and inflate the denominator of every percentage.

    A function's ``run_count`` is the count at its entry address specifically,
    which is how often it was *called*; ``instructions_executed`` is the sum
    over its whole body. Rows come back sorted by ``(file, line, name)``.
    """
    functions: dict[int, _FunctionAccumulator] = {}
    lines: dict[str, _LineAccumulator] = {}

    for section in sections:
        counts = section.counts
        for address in section.instruction_starts:
            function = symbolizer.resolve_function(address)
            if function is None:
                continue

            slot = (address - section.base_address) // HALFWORD_BYTES
            count = counts[slot] if 0 <= slot < len(counts) else 0
            entry = function.addr & THUMB_ADDRESS_MASK
            source = symbolizer.resolve_source(address)

            accumulator = functions.get(entry)
            if accumulator is None:
                entry_source = symbolizer.resolve_source(entry) or source
                accumulator = _FunctionAccumulator(
                    name=function.name,
                    entry=entry,
                    file=entry_source.file if entry_source else "",
                    line=entry_source.line if entry_source else 0,
                )
                functions[entry] = accumulator

            accumulator.instructions += 1
            accumulator.instructions_executed += count
            if count > 0:
                accumulator.instructions_covered += 1
            if address == entry:
                accumulator.run_count = count

            if source is not None:
                key = f"{source.file}:{source.line}"
                accumulator.lines[key] = accumulator.lines.get(key, False) or count > 0

                line_accumulator = lines.get(key)
                if line_accumulator is None:
                    line_accumulator = _LineAccumulator(
                        file=source.file, line=source.line
                    )
                    lines[key] = line_accumulator
                line_accumulator.instructions += 1
                if count > 0:
                    line_accumulator.instructions_covered += 1
                line_accumulator.run_count = max(line_accumulator.run_count, count)

    function_rows = [
        FunctionRow(
            file=fn.file,
            line=fn.line,
            name=fn.name,
            src_lines=len(fn.lines),
            src_lines_covered=sum(1 for covered in fn.lines.values() if covered),
            instructions=fn.instructions,
            instructions_covered=fn.instructions_covered,
            run_count=fn.run_count,
            instructions_executed=fn.instructions_executed,
        )
        for fn in functions.values()
    ]
    function_rows.sort(key=lambda row: (row.file, row.line, row.name))

    line_rows = [
        LineRow(
            file=row.file,
            line=row.line,
            instructions=row.instructions,
            instructions_covered=row.instructions_covered,
            run_count=row.run_count,
        )
        for row in lines.values()
    ]
    line_rows.sort(key=lambda row: (row.file, row.line))

    return CoverageRows(functions=function_rows, lines=line_rows)


def compute_totals(rows: Sequence[FunctionRow]) -> Totals:
    """Totals describe the whole image, so they come from every row.

    Never from a stored slice: a report over the row cap would otherwise report
    coverage for its first 2000 functions as if that were the firmware.
    """
    totals = Totals(functions=len(rows))
    for row in rows:
        if row.instructions_covered > 0:
            totals.functions_covered += 1
        totals.src_lines += row.src_lines
        totals.src_lines_covered += row.src_lines_covered
        totals.instructions += row.instructions
        totals.instructions_covered += row.instructions_covered
        totals.total_run_count += row.run_count
    return totals


def run_count_lookup(sections: Sequence[SectionCounts]):
    """Build ``address -> execution count`` over every section."""

    def lookup(address: int) -> int | None:
        for section in sections:
            slot = (address - section.base_address) // HALFWORD_BYTES
            if slot < 0 or slot >= len(section.counts):
                continue
            return section.counts[slot]
        return None

    return lookup


__all__ = [
    "CoverageRows",
    "FunctionRow",
    "LineRow",
    "SectionCounts",
    "Totals",
    "build_coverage_rows",
    "compute_totals",
    "run_count_lookup",
]
