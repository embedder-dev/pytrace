"""STRACE -- the decoded instruction-trace path.

The API SEGGER's own tools use for instruction trace. It hands back program
counters, already reconstructed by the DLL from ETM's branch and sync packets
-- which is why :meth:`JLink.read_into_trace_cache` must have been given the
code image first.

Two limits are worth knowing before you design around this module:

* a single :meth:`Strace.read` returns at most 65,536 items. The read path
  contains a fixed 256 KiB allocation, so asking for more silently returns
  that many. It is a per-call clamp, not a buffer or hardware ceiling.
* :meth:`Strace.control` accepts commands 0..3 and nothing else, so there is no
  cursor and no "how many are buffered" query on this API. The offset-addressable
  version lives in :mod:`jtrace.tracebuf`.

Neither is a real ceiling. :meth:`Strace.read_extended` captures streams of any
length, and does so without loss whenever the core cannot outrun the 65,536
clamp between two reads, because two things are true of the DLL -- both
measured on hardware rather than inferred:

* a read *drains* the buffer, so consecutive reads never overlap;
* below the clamp, a read returns exactly as many instructions as executed.

What does not work is polling while the target runs: that returns zero items
every time. A halt is what makes the DLL materialise the decoded window, so
long captures are built out of run/halt slices.
"""

from __future__ import annotations

import array
import ctypes
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import (
    DEFAULT_TRACE_CAPACITY,
    INST_STATS_ITEM_BYTES,
    STRACE_READEX_FLAGS_NONE,
    MAX_STRACE_ITEMS,
    InstStatsType,
    StraceCmd,
    StraceEventType,
    StraceOperation,
)
from .errors import TraceError
from .store import TraceStore
from .structs import StraceTimestampInfo

if TYPE_CHECKING:
    from .link import JLink


@dataclass
class StreamingStats:
    """What a :meth:`Strace.read_extended` run actually managed to collect."""

    polls: int = 0
    """Run/halt slices performed."""

    stitched: int = 0
    """Slices joined to the previous window with no loss."""

    gaps: int = 0
    """Slices where more executed than the window holds, so instructions
    between the two windows are gone.

    A non-zero count means the stream has holes. It is reported rather than
    hidden because a silently-spliced stream produces call frames that never
    happened.

    Shortening ``slice_ms`` may not close them. On a fast core the run window is
    set by probe round-trip latency rather than by the sleep: measured on
    hardware, a slice took 23.38 ms of wall clock at ``slice_ms=0.5`` and
    22.99 ms at 0.05. That is a property of the host and probe as much as the
    target, so
    measure rather than assume it generalises.
    """

    lost: int = 0
    """Instructions the probe buffer dropped, summed across every gap.

    Exact, not an estimate: it is the executed count minus the returned count,
    both of which the DLL reports.
    """

    total_read: int = 0

    dropped: int = 0
    """Instructions the host-side store evicted to stay inside its capacity.

    Distinct from :attr:`lost`, and it does not make the stream discontinuous:
    eviction drops whole oldest blocks, so what remains is contiguous -- it is
    simply a tail. What it does mean is that the capture holds less than ran.
    Reported because a store that silently returns a fraction of what was asked
    for is worse than one that refuses.
    """

    @property
    def is_continuous(self) -> bool:
        """No holes *within* what came back.

        Not the same as complete: a capture can be perfectly continuous and
        still be a tail, either because the probe buffer wrapped
        (``summary.window_truncated``) or because the store evicted
        (:attr:`dropped`).
        """
        return self.gaps == 0


@dataclass(frozen=True, slots=True)
class StraceTimestamp:
    """One cycle stamp, and the instruction it belongs to."""

    cycle: int
    index: int
    """Position within the program counters returned by the same read."""

    adjust: int = 0
    """The record's fourth field, carried through unexamined. Its meaning is
    not established -- see :class:`jtrace.structs.StraceTimestampInfo`."""


def detect_stamp_order(stamps: "list[StraceTimestamp]") -> bool | None:
    """Work out a window's orientation from its own cycle stamps.

    Returns ``True`` for newest-first, ``False`` for oldest-first, and ``None``
    when the stamps cannot say.

    This exists because the alternative is an assumption that fails silently.
    ``JLINK_STRACE_Read`` is measurably newest-first and ``ReadEx`` very
    probably matches -- but "very probably" applied to a reversal produces a
    timeline that is monotonic, plausible and entirely wrong, with no invariant
    left for anything downstream to catch it on.

    Cycles only ever move forward in real execution, so their direction against
    the index *is* the orientation, and the window carries the evidence for its
    own interpretation. Every adjacent pair has to agree; a window whose stamps
    contradict each other returns ``None`` rather than a majority verdict,
    because disagreement means an assumption elsewhere is already wrong.
    """
    ordered = sorted(stamps, key=lambda stamp: stamp.index)
    directions = {
        later.cycle > earlier.cycle
        for earlier, later in zip(ordered, ordered[1:])
        if earlier.index != later.index and earlier.cycle != later.cycle
    }
    if len(directions) != 1:
        return None
    # Cycles rising with the index means index 0 is the oldest instruction.
    return not directions.pop()


def chronologise(
    pcs: array.array,
    stamps: "list[StraceTimestamp]",
    *,
    newest_first: bool | None = None,
) -> "tuple[array.array, list[StraceTimestamp]]":
    """Put a window into time order, remapping its stamps onto the result.

    Kept pure, separate, and tested on its own because a mistake here does not
    announce itself. A stamp index is a position in the array it arrived with;
    reversing the program counters without also mapping ``i -> count - 1 - i``
    attributes every instruction the cycle count of its mirror image.

    ``newest_first=None`` asks the stamps themselves -- see
    :func:`detect_stamp_order` -- and falls back to newest-first, which is what
    :meth:`Strace.read` measurably does, when they cannot say. Pass it
    explicitly to override.

    Stamps referring to positions outside the window are dropped rather than
    clamped: a clamped stamp is an invented measurement.
    """
    if newest_first is None:
        detected = detect_stamp_order(stamps)
        newest_first = True if detected is None else detected

    total = len(pcs)
    out = array.array("I")
    out.extend(reversed(pcs) if newest_first else pcs)
    remapped = [
        StraceTimestamp(
            cycle=stamp.cycle,
            index=(total - 1 - stamp.index) if newest_first else stamp.index,
            adjust=stamp.adjust,
        )
        for stamp in stamps
        if 0 <= stamp.index < total
    ]
    remapped.sort(key=lambda stamp: stamp.index)
    return out, remapped

def _first_halt_reason(link) -> int | None:
    """The raw halt reason, or None if the probe will not say.

    Never raises: this runs once per slice on the capture path, and a capture
    is not worth failing over a diagnostic. The value stays raw -- this SDK has
    no verified mapping from it to a reason, and :mod:`jtrace.constants` does
    not carry guesses.
    """
    try:
        reasons = link.halt_reason()
    except Exception:
        return None
    return int(reasons[0].HaltReason) if reasons else None


def _build_blocks(store: TraceStore, pending: list) -> None:
    """Turn stashed windows into blocks, oldest first.

    Each window is released as its block takes its place, so peak memory stays
    at one copy rather than holding the whole stash and the whole store at once.
    """
    for index in range(len(pending)):
        window, stamps, lost, halt_reason, wall_ns = pending[index]
        pending[index] = None
        observed = detect_stamp_order(stamps) if stamps else None
        if observed is not None:
            store.stamp_order_observed = observed
        chronological, ordered = chronologise(
            window, stamps, newest_first=observed
        )
        store.append_block(
            chronological,
            lost_before=lost,
            halt_reason=halt_reason,
            wall_ns=wall_ns,
            cycles=[stamp.cycle for stamp in ordered],
            stamp_at=[stamp.index for stamp in ordered],
        )
    pending.clear()


def _apply_stats(source, out: "StreamingStats") -> None:
    out.polls = source.polls
    out.stitched = source.stitched
    out.gaps = source.gaps
    out.lost = source.lost
    out.total_read = source.total_read
    out.dropped = source.dropped_instructions


class Strace:
    """Instruction trace over the STRACE API. Reached via ``JLink.strace``."""

    def __init__(self, link: "JLink") -> None:
        self._link = link
        self._lib = link.raw

    # -- configuration -----------------------------------------------------

    def config(self, config: str) -> None:
        """Configure the trace port, e.g. ``PortWidth=4``.

        The DLL parses this as a semicolon-separated key=value string. Known
        keys: ``PortWidth`` (1, 2 or 4 trace data pins).
        """
        rc = self._lib.JLINK_STRACE_Config(config.encode() + b"\0")
        if rc < 0:
            raise TraceError(f"Trace port configuration ({config}) failed", rc)

    def configure_port(self, width: int = 4) -> None:
        self.config(f"PortWidth={width}")

    def set_buffer_size(self, num_bytes: int) -> None:
        """Size the probe-side ring.

        Bigger means a longer window survives before it wraps. This does not
        raise the 65,536-item ceiling on a single :meth:`read` -- the two are
        unrelated, and conflating them is how a 16 MiB buffer ends up reported
        as 256 KiB.
        """
        value = ctypes.c_uint32(num_bytes)
        rc = self._lib.JLINK_STRACE_Control(
            int(StraceCmd.SET_BUFFER_SIZE), ctypes.byref(value)
        )
        if rc < 0:
            raise TraceError(f"Setting the trace buffer to {num_bytes} bytes failed", rc)

    def control(self, cmd: StraceCmd | int, data: object = None) -> int:
        """Raw ``JLINK_STRACE_Control``. Cmd must be 0..3; the DLL rejects more."""
        pointer = ctypes.byref(data) if data is not None else None  # type: ignore[arg-type]
        rc = self._lib.JLINK_STRACE_Control(int(cmd), pointer)
        if rc < 0:
            raise TraceError(f"STRACE_Control({int(cmd)}) failed", rc)
        return rc

    # -- trace events ------------------------------------------------------

    def set_event(
        self,
        operation: StraceOperation,
        address: int,
        *,
        event_type: StraceEventType = StraceEventType.CODE_FETCH,
        address_range: int = 0,
        access_mask: int = 0,
    ) -> int:
        """Arm a trace start/stop/include/exclude condition; returns its handle.

        This is what makes STRACE *selective*: rather than trace everything and
        throw most of it away, tell the probe to start recording when the PC
        reaches one address and stop at another.
        """

        class _Event(ctypes.Structure):
            _fields_ = [
                ("SizeofStruct", ctypes.c_uint32),
                ("Type", ctypes.c_uint32),
                ("Op", ctypes.c_uint32),
                ("AccessType", ctypes.c_uint32),
                ("Addr", ctypes.c_uint64),
                ("Data", ctypes.c_uint64),
                ("DataMask", ctypes.c_uint64),
                ("AddrRangeSize", ctypes.c_uint32),
                ("AccessMask", ctypes.c_uint32),
            ]

        event = _Event()
        event.SizeofStruct = ctypes.sizeof(_Event)
        event.Type = int(event_type)
        event.Op = int(operation)
        event.Addr = address
        event.AddrRangeSize = address_range
        event.AccessMask = access_mask
        return self.control(StraceCmd.TRACE_EVENT_SET, event)

    def clear_event(self, handle: int) -> None:
        value = ctypes.c_uint32(handle)
        self.control(StraceCmd.TRACE_EVENT_CLR, value)

    def clear_all_events(self) -> None:
        self.control(StraceCmd.TRACE_EVENT_CLR_ALL)

    # -- run control -------------------------------------------------------

    def start(self) -> None:
        rc = self._lib.JLINK_STRACE_Start()
        if rc < 0:
            raise TraceError(
                "Trace start failed. Check that the J-Trace is attached over the "
                "fine-pitch CoreSight-20 cable and that the target drives TRACECLK",
                rc,
            )

    def stop(self) -> None:
        """Stop tracing. Never raises: this runs on the teardown path."""
        try:
            self._lib.JLINK_STRACE_Stop()
        except Exception:
            pass

    # -- reading -----------------------------------------------------------

    def read(self, max_items: int = MAX_STRACE_ITEMS) -> array.array:
        """Read up to ``max_items`` program counters, newest first.

        The order is the probe's, not ours. Anything that wants chronological
        order has to reverse it -- see :func:`jtrace.capture.to_chronological`.
        """
        wanted = min(max_items, MAX_STRACE_ITEMS)
        buffer = (ctypes.c_uint32 * wanted)()
        count = self._lib.JLINK_STRACE_Read(
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)), wanted
        )
        if count < 0:
            raise TraceError("Trace read failed", count)
        out = array.array("I")
        out.frombytes(bytes(buffer)[: count * 4])
        return out

    def read_ex(
        self,
        max_items: int = MAX_STRACE_ITEMS,
        *,
        flags: int = STRACE_READEX_FLAGS_NONE,
    ) -> "tuple[array.array, list[StraceTimestamp]]":
        """Read program counters *and* cycle stamps, in the probe's order.

        The timestamped sibling of :meth:`read`, and the only route this SDK
        has to a time axis: the ``TRACE_*`` cursor that carries timestamps
        reads as empty while STRACE owns the trace path, and the alternative is
        writing an ETM decoder.

        **Stamps require ``TRACE_SetEnableTimestamps = 1`` first.** Without it
        this call returns program counters and no stamps at all -- measured, 0
        stamps for 2,919 instructions without the command against 57 for 39,176
        with it. That is the usual reason a stamped capture comes back bare::

            link.exec_command("TRACE_SetEnableTimestamps = 1")

        Returns ``(program_counters, stamps)`` **in the order the probe gave
        them**, which is newest-first, the same as :meth:`read`. Nothing here
        reverses anything; :func:`chronologise` does that once, where it can be
        tested, and it reads the orientation off the stamps rather than
        assuming it.

        Stamps are sparse -- roughly one per 700 instructions on a fast core
        -- so a cycle count for an instruction between two of them is
        interpolated. :class:`jtrace.store.CycleEstimate` says which is which
        and how far apart the bracketing stamps were.

        The stamp buffer is sized to ``max_items`` rather than to some smaller
        guess because that is the capacity the DLL is told the array has; a
        shorter one would be a buffer it is entitled to overrun.

        Measured on hardware: the 65,536 clamp is honoured exactly,
        :attr:`StraceTimestamp.adjust` arrives as 0 in every record, and the
        cycle counter runs forward across a halt. :meth:`read_extended` still
        leaves ``timestamps`` off by default -- changing what a capture asks
        the DLL for is a decision, not something to inherit -- but it is no
        longer unmeasured.
        """
        if not self._lib.has("JLINK_STRACE_ReadEx"):
            raise TraceError(
                "This J-Link DLL does not export JLINK_STRACE_ReadEx, so trace "
                "timestamps are unavailable. Capture without them, or update "
                "the J-Link software."
            )
        wanted = min(max_items, MAX_STRACE_ITEMS)
        buffer = (ctypes.c_uint32 * wanted)()
        stamp_buffer = (StraceTimestampInfo * wanted)()
        num_stamps = ctypes.c_int32(0)
        count = self._lib.JLINK_STRACE_ReadEx(
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)),
            wanted,
            ctypes.cast(stamp_buffer, ctypes.c_void_p),
            ctypes.pointer(num_stamps),
            int(flags),
        )
        if count < 0:
            raise TraceError("Timestamped trace read failed", count)

        pcs = array.array("I")
        pcs.frombytes(bytes(buffer)[: count * 4])
        returned = max(0, min(num_stamps.value, wanted))
        stamps = [
            StraceTimestamp(
                cycle=int(stamp_buffer[i].Timestamp),
                index=int(stamp_buffer[i].Index),
                adjust=int(stamp_buffer[i].Adjust),
            )
            for i in range(returned)
        ]
        return pcs, stamps

    def read_extended(
        self,
        code_address: int,
        *,
        target_items: int,
        slice_ms: float | None = None,
        max_slices: int = 100_000,
        deadline_s: float | None = None,
        stats: StreamingStats | None = None,
        on_progress: "Callable[[int, int], None] | None" = None,
        store: TraceStore | None = None,
        capacity: int | None = None,
        record_halt_reason: bool = True,
        timestamps: bool = False,
    ) -> TraceStore:
        """Collect an unlimited-length instruction trace, in slices.

        The 65,536 clamp on :meth:`read` is per call, and two measured facts
        about the DLL make it possible to walk straight past it:

        * **a read consumes what it returns, and returns the newest.**
          Consecutive reads never overlap, so there is nothing to deduplicate
          and nothing to match on; the join is plain concatenation. Note this
          is *not* the same as emptying the buffer: with more than 65,536
          buffered, a read hands back the newest 65,536 and leaves the rest.
          Reading again with the core still halted walks backwards through the
          remainder. But once the core runs on, the next read again returns the
          newest -- so the stream stays ordered and the older remainder is
          simply never seen, which is exactly what ``lost`` counts. Measured
          on hardware: after a slice overflowed, six consecutive run-and-read
          slices all advanced forward with contiguous cycle ranges.
        * **retention is exact below the clamp.** As long as fewer than 65,536
          instructions accumulate between reads, the read returns *precisely*
          the number that executed. Above it, the excess is dropped.

        Both were measured on hardware against known-count firmware, not
        inferred: a slice retiring 28,753 instructions returned 28,753, while
        one retiring 67,966 returned the 65,536 clamp.

        So each slice runs the target for ``slice_ms``, halts, and drains. Loss
        is detected by subtraction -- ``total_executed`` on both sides of the
        slice against the number of items returned -- rather than by trusting
        the slice length. ``code_address`` is any address in the traced region;
        it selects which statistics counter to read, and the aggregate is
        image-wide.

        ``slice_ms=None`` (the default) tunes itself: the first slice measures
        the instruction rate, and every later slice is sized to fill about half
        the window. Do not read that as a guarantee on a fast core. The model
        divides instructions retired by the *nominal* sleep, which omits the
        probe round trip the core also runs through, and on a fast core that
        made the estimate wrong by a factor of 47 at ``slice_ms=0.5``.
        Check ``streaming.is_continuous`` rather than trusting the tuner.

        ``deadline_s`` caps wall-clock time *in the loop*, returning whatever was
        collected so far rather than raising -- a short trace of a slow target
        is a result, not a failure. Blocks are built after the loop ends, so the
        call returns a little after the deadline rather than exactly on it; that
        is the price of keeping the copy out from between the halts.

        ``timestamps`` switches the read to :meth:`read_ex`, which also returns
        sparse cycle stamps. It defaults to off because the behaviour of that
        call has not yet been measured on hardware -- see :meth:`read_ex` for
        exactly what remains unknown. The plumbing is here and tested; turning
        it on is a decision to be made against a probe, not a default to
        inherit.

        Returns a :class:`~jtrace.store.TraceStore`: chronological, oldest
        first -- the opposite of :meth:`read`, which returns the probe's
        newest-first order -- and segmented into one block per slice. Iterating
        it or taking its length treats it as a flat program-counter stream.

        The cost is real, and it is not the loss: it is that the target is
        halted between slices. This is a concatenation of contiguous execution
        windows, not one uninterrupted real-time trace. For a free-running loop
        that distinction does not matter. For anything whose behaviour depends
        on timing -- an interrupt, a timeout, a peripheral handshake -- it does.
        The block structure is what makes that legible afterwards: each seam is
        a real halt, and :meth:`~jtrace.store.TraceStore.boundaries` marks the
        ones that also lost instructions.
        """
        collected = (
            store
            if store is not None
            else TraceStore(
                capacity=capacity
                if capacity is not None
                else max(target_items, DEFAULT_TRACE_CAPACITY)
            )
        )
        link = self._link
        # Aim to fill half the window per slice: enough that the per-slice
        # overhead is amortised, with enough headroom that a burst of faster
        # execution does not overflow it.
        budget = MAX_STRACE_ITEMS // 2
        current_ms = 0.5 if slice_ms is None else slice_ms

        expires = None if deadline_s is None else time.monotonic() + deadline_s
        # Progress is what has been appended, not what the store still holds.
        # Eviction can shrink the store, and a loop that measures itself by
        # len(collected) then never reaches its target: every slice is undone
        # by the eviction it triggers, and it spins to its deadline.
        appended = 0
        # Slices are stashed raw and become blocks after the loop. Reversing and
        # copying a 65,536-entry window is the most expensive thing the host does
        # per slice, and doing it between a halt and the next go inflates the
        # *following* halt several-fold -- 6.51 ms median against 1.60 ms once
        # deferred, measured on hardware.
        # The core is running for that excess, so it is instructions lost: the
        # same run lost 17.8M inline against 1.8M deferred. Stashing is free --
        # read() already returns a fresh array, so this keeps a reference.
        pending: list = []
        stashed = 0
        for _ in range(max_slices):
            if appended >= target_items:
                break
            if expires is not None and time.monotonic() >= expires:
                break

            before = self.total_executed(code_address)
            link.go()
            time.sleep(current_ms / 1000)
            link.halt()
            advanced = self.total_executed(code_address) - before

            if timestamps:
                window, stamps = self.read_ex(MAX_STRACE_ITEMS)
            else:
                window, stamps = self.read(MAX_STRACE_ITEMS), []
            wall_ns = time.monotonic_ns()

            # The buffer holds at most MAX_STRACE_ITEMS, so anything the core
            # retired beyond that between reads is gone. It is recorded on the
            # block rather than only counted, so the stream itself says where
            # the hole is: a silently spliced stream produces call frames for
            # transitions that never happened.
            lost = max(0, advanced - len(window))
            if len(window) or lost:
                # The halt reason is the one thing here that cannot wait: a
                # method-of-entry belongs to the halt that produced it, and the
                # next go() replaces it. Read under the same condition as
                # before, so it stays one probe transaction per productive
                # slice and none on an idle poll.
                pending.append(
                    (
                        window,
                        stamps,
                        lost,
                        _first_halt_reason(link) if record_halt_reason else None,
                        wall_ns,
                    )
                )
                # chronologise() returns as many counters as it is given, so
                # this is the same number the block will carry.
                appended += len(window)
                stashed += len(window)
            else:
                # Nothing retired and nothing lost. Still a poll, but a block
                # here would be pure overhead -- and a run that never reaches
                # its target spins until the deadline.
                collected.note_idle_poll()

            if slice_ms is None and advanced > 0 and current_ms > 0:
                rate = advanced / current_ms
                # Clamped so one anomalous slice cannot drive the next to
                # either a busy-loop or a guaranteed overflow.
                current_ms = min(50.0, max(0.05, budget / rate))

            if on_progress is not None:
                # `appended`, not len(collected): no block exists yet, and the
                # store's length is post-eviction in any case.
                on_progress(appended, target_items)

            # Deferring must not defeat the store's own bound. A caller whose
            # capacity is below their target asked to keep a tail, not to hold
            # the whole run in a stash -- without this, a capture bounded to
            # 8 instructions still accumulated every window it ever read.
            # Draining costs one inflated halt on data that is about to be
            # evicted anyway, and it cannot fire when capacity covers the
            # target, which is every capture this SDK builds for itself.
            if len(collected) + stashed > collected.capacity:
                _build_blocks(collected, pending)
                stashed = 0

        # Newest-first from the probe; every consumer wants time order. One path
        # whether or not stamps were asked for, so the reversal is never written
        # twice.
        _build_blocks(collected, pending)
        collected.truncate_to(target_items)
        if stats is not None:
            _apply_stats(collected.stats(), stats)
        return collected

    # -- instruction statistics -------------------------------------------

    def instruction_counts(self, address: int, num_halfwords: int) -> array.array:
        """Per-halfword-slot execution counts for a region.

        Slot *i* describes ``address + i*2``. A zero slot is ambiguous on its
        own -- either an instruction that never ran, or the trailing halfword
        of a 32-bit Thumb-2 instruction, which never carries a count. Walking
        the real instruction boundaries is what disambiguates them; see
        :func:`jtrace.thumb.instruction_starts`.
        """
        raw = (ctypes.c_uint8 * (INST_STATS_ITEM_BYTES * num_halfwords))()
        count = self._lib.JLINK_STRACE_GetInstStats(
            ctypes.byref(raw),
            address,
            num_halfwords,
            INST_STATS_ITEM_BYTES,
            int(InstStatsType.EXEC_COUNT),
        )
        if count < 0:
            raise TraceError(
                f"Reading instruction statistics at 0x{address:08x} failed", count
            )
        blob = bytes(raw)
        counts = array.array("I")
        for i in range(count):
            offset = i * INST_STATS_ITEM_BYTES
            counts.append(int.from_bytes(blob[offset : offset + 4], "little"))
        return counts

    def total_executed(self, address: int) -> int:
        """Total instructions executed over the capture.

        Not the number readable from the buffer -- usually far larger. The gap
        between the two is what tells you the window is the tail of the run
        rather than all of it.
        """
        raw = (ctypes.c_uint8 * INST_STATS_ITEM_BYTES)()
        count = self._lib.JLINK_STRACE_GetInstStats(
            ctypes.byref(raw),
            address,
            1,
            INST_STATS_ITEM_BYTES,
            int(InstStatsType.AGGREGATE),
        )
        if count < 1:
            return 0
        return int.from_bytes(bytes(raw)[:4], "little")


__all__ = [
    "Strace",
    "StraceTimestamp",
    "StreamingStats",
    "chronologise",
    "detect_stamp_order",
]
