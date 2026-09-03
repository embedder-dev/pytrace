"""Reconstructing the call stack from a program-counter stream.

Semantics are pinned to a reference TypeScript implementation. The two must
agree: a session captured from Python and one captured from the UI land in the
same store and are read by the same viewer, and frames that disagree between
them would be indistinguishable from a decoder bug.

ETM records which instructions ran, not why, so calls and returns are inferred
from where the program counter lands:

* landing exactly on a function's entry address is a call -- which is what
  separates recursion from a return into the same function;
* landing anywhere else in a function already on the stack is a return, so
  every frame above it closes;
* landing mid-function in something never seen before is a return into an
  ancestor that entered before the capture window opened. That frame is placed
  one level *below* the current one, which is why levels are normalised at the
  end rather than counted up from zero.

The stack that existed before the window is unrecoverable from PCs alone --
only the ancestors execution actually returns into can be inferred.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .constants import THUMB_ADDRESS_MASK
from .symbols import ResolvedFunction, Symbolizer

DEFAULT_MAX_DEPTH = 64
"""Nesting ceiling. Beyond it the stream keeps running in the deepest frame
rather than growing the stack, so a pathological capture cannot allocate
without bound."""

MIN_LEVEL = -32
"""Returning into never-observed ancestors walks levels downward; this bounds
how far, so a stream that only ever returns cannot run away either."""


@dataclass
class CallFrame:
    """One function invocation, bounded by instruction indices.

    Not timestamps: ETM records order, never duration, so a frame's width is
    work done rather than wall-clock elapsed.
    """

    name: str
    address: int
    depth: int
    start_index: int
    end_index: int
    """Exclusive. ``end_index - start_index`` is the instruction count."""

    file: str | None = None
    line: int | None = None
    open_at_start: bool = False
    """Already on the stack when the window began: its true entry point is off
    the front of the buffer."""

    open_at_end: bool = False
    """Had not returned when the window ended."""

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "name": self.name,
            "address": self.address,
            "depth": self.depth,
            "startIndex": self.start_index,
            "endIndex": self.end_index,
        }
        if self.file is not None:
            out["file"] = self.file
        if self.line is not None:
            out["line"] = self.line
        if self.open_at_start:
            out["openAtStart"] = True
        if self.open_at_end:
            out["openAtEnd"] = True
        return out


@dataclass
class FrameResult:
    frames: list[CallFrame]
    max_depth: int


@dataclass
class _Open:
    name: str
    address: int
    unknown: bool
    level: int
    start_index: int
    file: str | None = None
    line: int | None = None
    open_at_start: bool = False


def _entry_address(function: ResolvedFunction) -> int:
    return function.addr & THUMB_ADDRESS_MASK


def _unknown_name(address: int) -> str:
    return f"0x{address:08x}"


def build_call_frames(
    addresses: Sequence[int],
    symbolizer: Symbolizer,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    boundaries: Sequence[int] = (),
) -> FrameResult:
    """Turn a chronological PC stream into a list of call frames.

    ``boundaries`` names indices where the stream is known to be discontinuous
    -- instructions were captured on both sides but some in between were lost.
    Every open frame is closed there and reopened after, because the alternative
    is worse than a gap: the address after the hole is compared against a stack
    that describes execution before it, and the mismatch is reported as a call
    or a return that never happened.

    It defaults to empty, which reproduces the reference implementation
    exactly -- that producer cannot generate a gapped stream, so it has nothing
    to declare. :meth:`jtrace.store.TraceStore.boundaries` supplies the real
    ones.
    """
    closed: list[tuple[_Open, int, bool]] = []
    stack: list[_Open] = []
    breaks = frozenset(boundaries)

    def open_frame(
        index: int, address: int, level: int, function: ResolvedFunction | None
    ) -> _Open:
        source = symbolizer.resolve_source(address)
        return _Open(
            name=function.name if function else _unknown_name(address),
            address=_entry_address(function) if function else address,
            unknown=function is None,
            level=level,
            start_index=index,
            file=source.file if source else None,
            line=source.line if source else None,
        )

    for index, address in enumerate(addresses):
        function = symbolizer.resolve_function(address)
        entry = None if function is None else _entry_address(function)

        if index in breaks and stack:
            # Nothing after the hole can be attributed to what was on the stack
            # before it, so the stack does not survive the hole.
            for frame in reversed(stack):
                closed.append((frame, index, True))
            stack.clear()
        is_entry = entry is not None and (address & THUMB_ADDRESS_MASK) == entry

        top = stack[-1] if stack else None
        if top is None:
            frame = open_frame(index, address, 0, function)
            frame.open_at_start = True
            stack.append(frame)
            continue

        # A run of unresolved addresses stays one region rather than becoming
        # one frame per instruction, so unknown identity is "same as any
        # unknown".
        same_frame = top.unknown if entry is None else top.address == entry
        if same_frame and not is_entry:
            continue

        if is_entry:
            if len(stack) >= max_depth:
                continue
            stack.append(open_frame(index, address, top.level + 1, function))
            continue

        ancestor = -1
        if entry is not None:
            for position in range(len(stack) - 1, -1, -1):
                if stack[position].address == entry:
                    ancestor = position
                    break
        if ancestor >= 0:
            for position in range(len(stack) - 1, ancestor, -1):
                closed.append((stack[position], index, False))
            del stack[ancestor + 1 :]
            continue

        # A function we have not seen, entered somewhere other than its start:
        # execution returned into a caller from before the window.
        closed.append((top, index, False))
        stack.pop()
        level = max(MIN_LEVEL, top.level - 1)
        frame = open_frame(index, address, level, function)
        frame.open_at_start = True
        stack.append(frame)

    for frame in stack:
        closed.append((frame, len(addresses), True))

    if not closed:
        return FrameResult(frames=[], max_depth=0)

    min_level = min(entry[0].level for entry in closed)
    max_level = max(entry[0].level for entry in closed)

    frames = [
        CallFrame(
            name=frame.name,
            address=frame.address,
            depth=frame.level - min_level,
            start_index=frame.start_index,
            end_index=end_index,
            file=frame.file,
            line=frame.line,
            open_at_start=frame.open_at_start,
            open_at_end=open_at_end,
        )
        for frame, end_index, open_at_end in closed
    ]
    frames.sort(key=lambda f: (f.start_index, f.depth))

    return FrameResult(frames=frames, max_depth=max_level - min_level + 1)


__all__ = ["CallFrame", "FrameResult", "build_call_frames"]
