"""Instruction rows, generated on access rather than stored.

A program-counter stream is 4 bytes per instruction. The same stream as
:class:`~jtrace.artifacts.InstructionRow` objects was measured at 352 bytes per
instruction before this module existed, which made the row list -- not the
trace -- the thing that decided how long a capture could run.

Nothing about a row is unrecoverable from the stream plus the ELF, so nothing
about it needs to be kept. :class:`InstructionRows` is a ``Sequence`` that looks
exactly like the list it replaces and builds each row when it is asked for --
the same trade any trace viewer makes when it keeps a raw store and generates
row text only for what is on screen.

:meth:`InstructionRows.runs` is the shape that actually pays. Execution stays
inside one line of one function for stretches at a time, so symbolizing per
*run* rather than per instruction turns one resolved object per program counter
into one per stretch.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from .artifacts import InstructionRow
from .symbols import Span, Symbolizer

RunCountFn = Callable[[int], "int | None"]


@dataclass(frozen=True, slots=True)
class InstructionRun:
    """A maximal stretch of instructions sharing one function and source line.

    ``end_index`` is exclusive, matching :class:`~jtrace.frames.CallFrame`.
    """

    start_index: int
    end_index: int
    address: int
    """The first program counter in the run."""

    function: str | None = None
    file: str | None = None
    line: int | None = None

    def __len__(self) -> int:
        return self.end_index - self.start_index


class InstructionRows(Sequence[InstructionRow]):
    """A lazily-materialised view over a program-counter stream.

    Indexes, slices, iterates and compares like the ``list[InstructionRow]``
    it replaces. Rows are built on access and not retained, so holding this is
    the cost of the addresses, not of the rows.
    """

    __slots__ = ("_addresses", "_symbolizer", "_run_count_at", "_base_index")

    def __init__(
        self,
        addresses: Sequence[int],
        symbolizer: Symbolizer,
        run_count_at: RunCountFn | None = None,
        *,
        base_index: int = 0,
    ) -> None:
        self._addresses = addresses
        self._symbolizer = symbolizer
        self._run_count_at = run_count_at
        self._base_index = base_index

    # -- sequence ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._addresses)

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, slice):
            return [self._row(i) for i in range(*key.indices(len(self._addresses)))]
        index = key + len(self._addresses) if key < 0 else key
        if not 0 <= index < len(self._addresses):
            raise IndexError("instruction index out of range")
        return self._row(index)

    def __iter__(self) -> Iterator[InstructionRow]:
        # Via runs, so the symbolizer is consulted once per stretch rather than
        # once per instruction. The rows produced are identical either way.
        run_count_at = self._run_count_at
        base = self._base_index
        addresses = self._addresses
        for start, end, span in self._symbolizer.runs(addresses):
            name = span.function.name if span.function else None
            file = span.source.file if span.source else None
            line = span.source.line if span.source else None
            for index in range(start, end):
                address = addresses[index]
                yield InstructionRow(
                    index=base + index,
                    address=address,
                    function=name,
                    file=file,
                    line=line,
                    run_count=run_count_at(address) if run_count_at else None,
                )

    def __eq__(self, other: object) -> bool:
        """Element-wise against any sequence, so a view compares equal to the
        list it replaced. Without this, every ``== [...]`` in a caller's tests
        would start failing for a reason that has nothing to do with the data.
        """
        if isinstance(other, InstructionRows):
            return len(self) == len(other) and all(
                a == b for a, b in zip(self, other)
            )
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return len(self) == len(other) and all(
                a == b for a, b in zip(self, other)
            )
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"<InstructionRows {len(self)} instructions, generated on access>"

    # -- runs --------------------------------------------------------------

    def runs(self) -> Iterator[InstructionRun]:
        """Group the stream into same-function, same-line stretches."""
        base = self._base_index
        for start, end, span in self._symbolizer.runs(self._addresses):
            yield InstructionRun(
                start_index=base + start,
                end_index=base + end,
                address=self._addresses[start],
                function=span.function.name if span.function else None,
                file=span.source.file if span.source else None,
                line=span.source.line if span.source else None,
            )

    # -- internals ---------------------------------------------------------

    def _row(self, index: int) -> InstructionRow:
        address = self._addresses[index]
        span: Span = self._symbolizer.resolve_span(address)
        return InstructionRow(
            index=self._base_index + index,
            address=address,
            function=span.function.name if span.function else None,
            file=span.source.file if span.source else None,
            line=span.source.line if span.source else None,
            run_count=self._run_count_at(address) if self._run_count_at else None,
        )


__all__ = ["InstructionRows", "InstructionRun"]
