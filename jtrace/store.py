"""The trace store: program counters, kept.

Where :mod:`jtrace.capture` used to expand a program-counter stream straight
into one dataclass per instruction and drop the raw array, this holds the raw
stream and lets the rows be generated from it. That is what makes a capture
re-symbolizable against a different ELF, snapshottable, and bounded.

The unit is a **block**: one uninterrupted run of the core. pytrace gets that
segmentation for free, because a STRACE read drains the probe buffer, so one
drain is one run. The usual alternative is parallel paged arrays plus a rule
that eviction must land on a block boundary; here a block is an object,
eviction is ``blocks.pop(0)``, and the alignment is true by construction.

The reason it matters most is time. Cycle stamps are sparse -- the DLL emits
them at its own cadence -- so a cycle count between two stamps is interpolated.
Interpolating *across a run/halt seam* is meaningless: the core was stopped for
milliseconds of wall time and an unknown number of cycles. A single global stamp
vector cannot express that and will interpolate straight through a halt. Stamps
here belong to a block, which makes the discontinuity unrepresentable rather
than merely undocumented.
"""

from __future__ import annotations

import array
import json
import struct
import sys
import zlib
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .constants import DEFAULT_TRACE_CAPACITY
from .errors import TraceError

PC_TYPECODE = "I"
"""Program counters, 32-bit. What the DLL hands back; widening to 64 would
double the store to describe targets this SDK does not support."""

CYCLE_TYPECODE = "Q"
STAMP_INDEX_TYPECODE = "I"


SNAPSHOT_MAGIC = b"PYTRACE1"
SNAPSHOT_VERSION = 1

_CODECS = {
    "zlib": (zlib.compress, zlib.decompress),
    "none": (lambda data, _level=0: data, lambda data: data),
}


def _codec(name: str):
    if name == "lzma":
        import lzma

        # lzma.compress takes `format` positionally, not a level; passing the
        # level through unnamed silently asks for a container that does not
        # exist.
        return (
            lambda data, level=6: lzma.compress(data, preset=level),
            lzma.decompress,
        )
    try:
        return _CODECS[name]
    except KeyError:
        raise TraceError(f"Unknown snapshot codec {name!r}") from None

@dataclass(frozen=True, slots=True)
class CycleEstimate:
    """A cycle count for one instruction index, and how much to trust it.

    Named an estimate because between two stamps it is one. ``exact`` is true
    only when the index landed on a stamp; ``span`` is the number of
    instructions between the bracketing stamps, which is the coarseness of the
    interpolation that produced it.
    """

    cycle: int
    exact: bool
    span: int
    block: int
    extrapolated: bool = False
    """The index fell outside the stamped range and the nearest pair was
    extended to reach it. Strictly worse than an interpolation."""


@dataclass(slots=True)
class TraceBlock:
    """One uninterrupted run of the core, and what was captured of it."""

    pcs: array.array
    """Program counters, chronological. Index 0 is the oldest."""

    lost_before: int = 0
    """Instructions the probe buffer dropped between the previous block and
    this one. Non-zero means the stream has a hole here."""

    halt_reason: int | None = None
    """Raw ``MoeInfo.HaltReason`` from the halt that ended this block.

    Undecoded on purpose: this SDK does not have a verified mapping from the
    value to a reason, and :mod:`jtrace.constants` does not carry guesses.
    """

    wall_ns: int | None = None
    """``time.monotonic_ns()`` at the halt that ended this block. Coarse, but
    real, and unlike cycle stamps it needs no probe support."""

    cycles: array.array = field(
        default_factory=lambda: array.array(CYCLE_TYPECODE)
    )
    """Sparse cycle stamps, parallel to :attr:`stamp_at`."""

    stamp_at: array.array = field(
        default_factory=lambda: array.array(STAMP_INDEX_TYPECODE)
    )
    """Block-local instruction index each stamp refers to. Ascending."""

    def __len__(self) -> int:
        return len(self.pcs)

    @property
    def stamped(self) -> bool:
        return len(self.cycles) > 0

    def estimate_cycle(self, offset: int, *, block: int = 0) -> CycleEstimate | None:
        """Cycle count at a block-local instruction index.

        ``None`` when the block carries no usable stamps, rather than a guess.
        A single stamp still answers exactly for its own index.
        """
        stamps = self.stamp_at
        if not stamps:
            return None
        position = bisect_left(stamps, offset)
        if position < len(stamps) and stamps[position] == offset:
            return CycleEstimate(
                cycle=int(self.cycles[position]), exact=True, span=0, block=block
            )
        if len(stamps) < 2:
            return None

        # Bracket the offset; where it falls outside the stamped range, extend
        # the nearest pair rather than clamping, and say so.
        low = min(max(position - 1, 0), len(stamps) - 2)
        high = low + 1
        i0, i1 = stamps[low], stamps[high]
        c0, c1 = int(self.cycles[low]), int(self.cycles[high])
        span = i1 - i0
        if span <= 0:
            return None
        cycle = c0 + (c1 - c0) * (offset - i0) / span
        return CycleEstimate(
            cycle=int(cycle),
            exact=False,
            span=span,
            block=block,
            extrapolated=not (i0 <= offset <= i1),
        )


@dataclass(slots=True)
class StoreStats:
    """What a capture managed to collect. Derived, not accumulated by hand.

    The four lifetime counters exist because eviction destroys the evidence:
    once the oldest blocks are dropped, the loss they recorded is no longer
    derivable from what is retained, and reporting a capture as lossless
    because its lossy part scrolled out would be worse than not reporting.
    """

    polls: int = 0
    stitched: int = 0
    gaps: int = 0
    lost: int = 0
    total_read: int = 0
    dropped_blocks: int = 0
    dropped_instructions: int = 0

    @property
    def is_continuous(self) -> bool:
        return self.gaps == 0


class TraceStore(Sequence[int]):
    """A bounded, block-segmented program-counter stream.

    Indexes as a flat chronological sequence of program counters -- index 0 is
    the oldest retained instruction -- while keeping the block structure
    available underneath for anything that needs to know where the core stopped.
    """

    def __init__(self, *, capacity: int = DEFAULT_TRACE_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._blocks: list[TraceBlock] = []
        self._starts: list[int] = []
        self.meta: dict = {}
        """Whatever a snapshot carried alongside the stream. Free-form."""

        self.stamp_order_observed: bool | None = None
        """Whether the probe returned windows newest-first, as *observed* from
        the cycle stamps rather than assumed.

        ``None`` when nothing was observed -- an unstamped capture, or stamps
        too sparse to show a direction. A real stamped capture answers this on
        its own, which is the point: it is the one open question about
        ``STRACE_ReadEx`` that would otherwise fail silently."""
        self._length = 0
        self._stats = StoreStats()

    # -- construction ------------------------------------------------------

    def append_block(
        self,
        pcs: Iterable[int] | array.array,
        *,
        lost_before: int = 0,
        halt_reason: int | None = None,
        wall_ns: int | None = None,
        cycles: Iterable[int] | None = None,
        stamp_at: Iterable[int] | None = None,
    ) -> TraceBlock:
        """Append one drained slice. Evicts oldest blocks past ``capacity``."""
        buffer = (
            pcs
            if isinstance(pcs, array.array) and pcs.typecode == PC_TYPECODE
            else array.array(PC_TYPECODE, pcs)
        )
        block = TraceBlock(
            pcs=buffer,
            lost_before=lost_before,
            halt_reason=halt_reason,
            wall_ns=wall_ns,
            cycles=array.array(CYCLE_TYPECODE, cycles or ()),
            stamp_at=array.array(STAMP_INDEX_TYPECODE, stamp_at or ()),
        )
        if len(block.cycles) != len(block.stamp_at):
            raise ValueError(
                f"{len(block.cycles)} cycle stamps for "
                f"{len(block.stamp_at)} indices; they are parallel arrays"
            )

        self._stats.polls += 1
        self._stats.total_read += len(block)
        if lost_before > 0:
            self._stats.gaps += 1
            self._stats.lost += lost_before
        elif len(block):
            self._stats.stitched += 1

        self._starts.append(self._length)
        self._blocks.append(block)
        self._length += len(block)
        self._evict()
        return block

    def note_idle_poll(self) -> None:
        """Record a slice that retired nothing and lost nothing.

        It is still a poll, and ``polls`` is how a caller sees that the loop
        span rather than that the target went quiet. But it is not a block: a
        capture whose target count is never reached spins until its deadline,
        and materialising an empty block per turn would cost more memory than
        the trace.
        """
        self._stats.polls += 1

    def _evict(self) -> None:
        """Drop whole oldest blocks until the bound holds.

        Whole blocks, never part of one: a half-block would leave stamps whose
        indices no longer mean anything and a call stack reconstructed from a
        stream that starts mid-run without saying so.
        """
        dropped = 0
        while self._length > self.capacity and len(self._blocks) > 1:
            block = self._blocks.pop(0)
            self._length -= len(block)
            self._stats.dropped_blocks += 1
            self._stats.dropped_instructions += len(block)
            dropped += 1
        if dropped:
            self._rebuild_starts()

    def _rebuild_starts(self) -> None:
        starts: list[int] = []
        offset = 0
        for block in self._blocks:
            starts.append(offset)
            offset += len(block)
        self._starts = starts
        self._length = offset

    def truncate_to(self, count: int) -> None:
        """Keep the oldest ``count`` instructions, dropping the newest.

        For honouring a caller's ``target_items`` once collection overshot it.
        Unlike eviction this may split a block, since the caller asked for a
        precise count; the split block keeps only the stamps still in range.
        """
        if count >= self._length:
            return
        if count <= 0:
            self._blocks = []
            self._rebuild_starts()
            return
        keep: list[TraceBlock] = []
        remaining = count
        for block in self._blocks:
            if remaining <= 0:
                break
            if len(block) <= remaining:
                keep.append(block)
                remaining -= len(block)
                continue
            cut = remaining
            limit = bisect_right(block.stamp_at, cut - 1)
            keep.append(
                TraceBlock(
                    pcs=block.pcs[:cut],
                    lost_before=block.lost_before,
                    halt_reason=block.halt_reason,
                    wall_ns=block.wall_ns,
                    cycles=block.cycles[:limit],
                    stamp_at=block.stamp_at[:limit],
                )
            )
            remaining = 0
        self._blocks = keep
        self._rebuild_starts()

    # -- sequence ----------------------------------------------------------

    def __len__(self) -> int:
        return self._length

    def __iter__(self) -> Iterator[int]:
        for block in self._blocks:
            yield from block.pcs

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, slice):
            return [self[i] for i in range(*key.indices(self._length))]
        index = key + self._length if key < 0 else key
        if not 0 <= index < self._length:
            raise IndexError("trace index out of range")
        block, offset = self.block_at(index)
        return int(block.pcs[offset])

    def block_at(self, index: int) -> tuple[TraceBlock, int]:
        """The block holding ``index``, and the offset within it."""
        index = index + self._length if index < 0 else index
        if not 0 <= index < self._length:
            raise IndexError("trace index out of range")
        position = bisect_right(self._starts, index) - 1
        return self._blocks[position], index - self._starts[position]

    def block_index_at(self, index: int) -> int:
        index = index + self._length if index < 0 else index
        if not 0 <= index < self._length:
            raise IndexError("trace index out of range")
        return bisect_right(self._starts, index) - 1

    # -- structure ---------------------------------------------------------

    @property
    def blocks(self) -> tuple[TraceBlock, ...]:
        return tuple(self._blocks)

    @property
    def block_starts(self) -> tuple[int, ...]:
        return tuple(self._starts)

    @property
    def origin(self) -> int:
        """Absolute index of item 0: how many instructions eviction dropped."""
        return self._stats.dropped_instructions

    def boundaries(self) -> list[int]:
        """Indices where the stream is known to be discontinuous.

        Only blocks that lost instructions, not every block boundary. A clean
        run/halt seam is a break in *time* but not in the instruction stream --
        the next instruction really is the one that executed next -- so closing
        call frames there would shred a long capture into one frame per slice.
        """
        return [
            start
            for start, block in zip(self._starts, self._blocks)
            if block.lost_before > 0 and start > 0
        ]

    def stats(self) -> StoreStats:
        return replace(self._stats)

    # -- time --------------------------------------------------------------

    def estimate_cycle(self, index: int) -> CycleEstimate | None:
        """Cycle count at a store-wide instruction index, or ``None``.

        Never interpolates across a block: the containing block is found first,
        and the estimate comes from that block's own stamps.
        """
        position = self.block_index_at(index)
        block = self._blocks[position]
        return block.estimate_cycle(index - self._starts[position], block=position)

    def estimate_index(self, cycle: int, *, block: int | None = None) -> float | None:
        """Inverse of :meth:`estimate_cycle`, within one block.

        ``block`` defaults to the only stamped block whose range contains
        ``cycle``; if none or several do, the answer is ambiguous and this
        returns ``None`` rather than picking one. Whether the target's cycle
        counter even runs monotonically across a halt is unverified -- see
        :attr:`cycles_continuous`.
        """
        if block is None:
            candidates = [
                i
                for i, b in enumerate(self._blocks)
                if b.stamped and int(b.cycles[0]) <= cycle <= int(b.cycles[-1])
            ]
            if len(candidates) != 1:
                return None
            block = candidates[0]
        target = self._blocks[block]
        if len(target.cycles) < 2:
            return None
        position = bisect_right(target.cycles, cycle) - 1
        low = min(max(position, 0), len(target.cycles) - 2)
        c0, c1 = int(target.cycles[low]), int(target.cycles[low + 1])
        i0, i1 = target.stamp_at[low], target.stamp_at[low + 1]
        if c1 == c0:
            return float(self._starts[block] + i0)
        offset = i0 + (i1 - i0) * (cycle - c0) / (c1 - c0)
        return float(self._starts[block] + offset)

    @property
    def cycles_continuous(self) -> bool | None:
        """Whether cycle stamps run forward across block seams.

        ``None`` when fewer than two blocks carry stamps, because then nothing
        has been observed either way. Derived from the data rather than assumed:
        it is not known whether the target's counter survives a halt, and that
        is exactly the kind of thing this SDK does not encode as a constant.
        """
        stamped = [b for b in self._blocks if b.stamped]
        if len(stamped) < 2:
            return None
        return all(
            int(b.cycles[0]) >= int(a.cycles[-1])
            for a, b in zip(stamped, stamped[1:])
        )



    # -- snapshot ----------------------------------------------------------

    def save(
        self,
        path: str | Path,
        *,
        codec: str = "zlib",
        level: int = 6,
        meta: dict | None = None,
    ) -> Path:
        """Write the store to a compressed binary snapshot. Returns the path.

        This is the whole store, not the rows derived from it, which is what
        makes it a snapshot rather than an export: reloading gives back the
        program counters, the block seams, the loss, and the stamps, so a
        capture can be re-symbolized against a different ELF without going near
        a probe again.

        Measured on realistic looping trace data, a million program counters is
        4.0 MB raw, 32 KB with ``zlib`` at level 6, and 20 KB with ``lzma``.
        The default is zlib: lzma buys about 12 KB for four times the compress
        time and roughly five times the decompress time, and the point of this
        file is that reopening it is cheap.

        One compressed member per block, with the lengths in the header. That
        is what makes :meth:`describe` possible without touching a single block,
        and it leaves the door open to partial reads; it is not there because
        the whole-file read is slow. It is not.
        """
        compress, _ = _codec(codec)
        blocks: list[dict[str, object]] = []
        members: list[bytes] = []
        for block in self._blocks:
            payload = (
                block.pcs.tobytes()
                + block.cycles.tobytes()
                + block.stamp_at.tobytes()
            )
            member = compress(payload, level) if codec != "none" else payload
            members.append(member)
            blocks.append(
                {
                    "n": len(block.pcs),
                    "stamps": len(block.cycles),
                    "lostBefore": block.lost_before,
                    "haltReason": block.halt_reason,
                    "wallNs": block.wall_ns,
                    "clen": len(member),
                }
            )

        header = {
            "version": SNAPSHOT_VERSION,
            "codec": codec,
            "byteorder": sys.byteorder,
            "typecodes": {
                "pcs": PC_TYPECODE,
                "cycles": CYCLE_TYPECODE,
                "stampAt": STAMP_INDEX_TYPECODE,
            },
            "capacity": self.capacity,
            "stats": {
                "polls": self._stats.polls,
                "stitched": self._stats.stitched,
                "gaps": self._stats.gaps,
                "lost": self._stats.lost,
                "totalRead": self._stats.total_read,
                "droppedBlocks": self._stats.dropped_blocks,
                "droppedInstructions": self._stats.dropped_instructions,
            },
            "stampOrderObserved": self.stamp_order_observed,
            "blocks": blocks,
            "meta": meta or {},
        }
        encoded = json.dumps(header).encode("utf-8")

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(SNAPSHOT_MAGIC)
            handle.write(struct.pack("<I", len(encoded)))
            handle.write(encoded)
            for member in members:
                handle.write(member)
        return target

    @staticmethod
    def describe(path: str | Path) -> dict:
        """The snapshot's header, without decompressing a single block.

        Everything about the shape of a capture -- how many instructions, how
        many uninterrupted runs, where instructions were lost, whether there is
        a time axis -- is in the header. Reading it costs about 50 KB and a
        fraction of a millisecond, against 40 MB and ten milliseconds to
        materialise a ten-million-instruction stream that the caller then only
        counts.
        """
        source = Path(path)
        with source.open("rb") as handle:
            if handle.read(len(SNAPSHOT_MAGIC)) != SNAPSHOT_MAGIC:
                raise TraceError(f"{source} is not a pytrace trace snapshot")
            (header_len,) = struct.unpack("<I", handle.read(4))
            header = json.loads(handle.read(header_len).decode("utf-8"))
        if header.get("version") != SNAPSHOT_VERSION:
            raise TraceError(
                f"{source} is snapshot version {header.get('version')}; "
                f"this build reads version {SNAPSHOT_VERSION}"
            )
        blocks = header.get("blocks", [])
        counts = [block["n"] for block in blocks]
        starts, offset = [], 0
        for count in counts:
            starts.append(offset)
            offset += count
        header["instructionCount"] = offset
        header["blockCount"] = len(blocks)
        header["boundaries"] = [
            start
            for start, block in zip(starts, blocks)
            if block.get("lostBefore", 0) > 0 and start > 0
        ]
        header["stampedBlocks"] = sum(1 for b in blocks if b.get("stamps", 0) > 0)
        return header

    @classmethod
    def load(cls, path: str | Path) -> "TraceStore":
        """Read a snapshot back, in full.

        Eager, and measured rather than assumed to be adequate: ten million
        program counters decompress in about ten milliseconds. Deferring that
        per block would be complexity bought against a cost that is already
        below noise, and it would put a lazily-thrown IO error behind an
        attribute access.

        What *is* worth avoiding is materialising 40 MB to answer a question
        about the capture's shape -- :meth:`describe` does that from the header
        alone, for about a thousandth of the memory.
        """
        source = Path(path)
        with source.open("rb") as handle:
            if handle.read(len(SNAPSHOT_MAGIC)) != SNAPSHOT_MAGIC:
                raise TraceError(f"{source} is not a pytrace trace snapshot")
            (header_len,) = struct.unpack("<I", handle.read(4))
            header = json.loads(handle.read(header_len).decode("utf-8"))
            if header.get("version") != SNAPSHOT_VERSION:
                raise TraceError(
                    f"{source} is snapshot version {header.get('version')}; "
                    f"this build reads version {SNAPSHOT_VERSION}"
                )
            _, decompress = _codec(header["codec"])
            swap = header.get("byteorder") != sys.byteorder

            store = cls(capacity=header.get("capacity", DEFAULT_TRACE_CAPACITY))
            for spec in header["blocks"]:
                payload = handle.read(spec["clen"])
                if header["codec"] != "none":
                    payload = decompress(payload)
                count, stamps = spec["n"], spec["stamps"]
                pcs = array.array(PC_TYPECODE)
                cycles = array.array(CYCLE_TYPECODE)
                stamp_at = array.array(STAMP_INDEX_TYPECODE)
                offset = count * pcs.itemsize
                pcs.frombytes(payload[:offset])
                cycle_bytes = stamps * cycles.itemsize
                cycles.frombytes(payload[offset : offset + cycle_bytes])
                offset += cycle_bytes
                stamp_at.frombytes(
                    payload[offset : offset + stamps * stamp_at.itemsize]
                )
                if swap:
                    # array.array is native-endian, so a snapshot written on a
                    # machine of the other endianness is byte-swapped rather
                    # than silently misread.
                    pcs.byteswap()
                    cycles.byteswap()
                    stamp_at.byteswap()
                store.append_block(
                    pcs,
                    lost_before=spec["lostBefore"],
                    halt_reason=spec["haltReason"],
                    wall_ns=spec["wallNs"],
                    cycles=cycles,
                    stamp_at=stamp_at,
                )

        recorded = header.get("stats", {})
        store._stats.dropped_blocks = recorded.get("droppedBlocks", 0)
        store._stats.dropped_instructions = recorded.get("droppedInstructions", 0)
        store._stats.polls = recorded.get("polls", store._stats.polls)
        store._stats.lost = recorded.get("lost", store._stats.lost)
        store._stats.gaps = recorded.get("gaps", store._stats.gaps)
        store._stats.stitched = recorded.get("stitched", store._stats.stitched)
        store._stats.total_read = recorded.get("totalRead", store._stats.total_read)
        store.meta = header.get("meta", {})
        store.stamp_order_observed = header.get("stampOrderObserved")
        return store

__all__ = [
    "CYCLE_TYPECODE",
    "SNAPSHOT_MAGIC",
    "SNAPSHOT_VERSION",
    "PC_TYPECODE",
    "STAMP_INDEX_TYPECODE",
    "CycleEstimate",
    "StoreStats",
    "TraceBlock",
    "TraceStore",
]
