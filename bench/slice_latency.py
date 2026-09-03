#!/usr/bin/env python3
"""Measure what per-slice host work costs a capture, on a real probe.

`Strace.read_extended` used to reverse and copy each 65,536-entry window
between `halt()` and the next `go()`. That work inflates the *following* halt
round-trip several-fold, and the core is running for the excess -- so it comes
out of the capture as lost instructions. This harness runs both shapes against
the same target, alternating them so drift cannot favour one, and reports halt
latency and loss for each.

It is the evidence behind the deferred build. Re-run it on a differently-clocked
board: every number in the repo comes from a single fast Cortex-M4, and a second
target is the most useful thing anyone could add.

    JLINK_LIBRARY=~/jlink/libjlinkarm.so \\
        python3 bench/slice_latency.py --elf firmware.elf --device STM32F407VE

Needs a probe and a running target. Not part of the test suite -- nothing in
`tests/` requires hardware.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from jtrace import JLink                                    # noqa: E402
from jtrace.constants import MAX_STRACE_ITEMS               # noqa: E402
from jtrace.elf import ElfFile                              # noqa: E402
from jtrace.store import TraceStore                         # noqa: E402
from jtrace.strace import chronologise                      # noqa: E402


def run_variant(link, strace, address, *, slices, slice_ms, deferred):
    """One capture, with the block build either inline or after the loop."""
    store = TraceStore(capacity=50_000_000)
    pending, halts, lost_total = [], [], 0

    for _ in range(slices):
        before = strace.total_executed(address)
        link.go()
        time.sleep(slice_ms / 1000)
        start = time.perf_counter()
        link.halt()
        halts.append((time.perf_counter() - start) * 1e3)

        advanced = strace.total_executed(address) - before
        window = strace.read(MAX_STRACE_ITEMS)
        lost = max(0, advanced - len(window))
        lost_total += lost

        if deferred:
            pending.append((window, lost))
        else:
            chronological, _ = chronologise(window, [])
            store.append_block(chronological, lost_before=lost)

    for window, lost in pending:
        chronological, _ = chronologise(window, [])
        store.append_block(chronological, lost_before=lost)

    return halts, lost_total, len(store)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--interface", default="SWD")
    parser.add_argument("--speed", type=int, default=4000)
    parser.add_argument("--slices", type=int, default=30)
    parser.add_argument("--slice-ms", type=float, default=0.5)
    parser.add_argument("--rounds", type=int, default=2)
    args = parser.parse_args()

    elf = ElfFile(args.elf)
    sections = elf.executable_sections()
    if not sections:
        print(f"No executable sections in {args.elf}", file=sys.stderr)
        return 1
    address = sections[0].addr

    with JLink(
        device=args.device, interface=args.interface, speed_khz=args.speed
    ) as link:
        strace = link.strace
        link.reset(halt=True)
        link.halt()
        for section in sections:
            link.read_into_trace_cache(section.addr, section.size)
        strace.set_buffer_size(1 << 24)
        strace.configure_port(4)
        strace.start()

        print(
            f"  {args.slices} slices at slice_ms={args.slice_ms}, "
            f"{args.rounds} rounds, alternating\n"
        )
        header = f"  {'variant':10} {'halt med':>10} {'halt p90':>10} " \
                 f"{'slow>4ms':>10} {'lost':>14} {'kept':>12}"
        print(header)
        totals: dict[str, list] = {"inline": [], "deferred": []}

        # The order is swapped every round. Whichever variant runs *second* in a
        # pair takes occasional 9-12 ms halt outliers -- reversing
        # the order moves them to the other variant, so it is a property of
        # position, not of the code under test. Alternating averages it out; a
        # fixed order silently penalises one side.
        try:
            for round_index in range(args.rounds):
                order = (("inline", False), ("deferred", True))
                if round_index % 2:
                    order = tuple(reversed(order))
                for name, deferred in order:
                    halts, lost, kept = run_variant(
                        link, strace, address,
                        slices=args.slices, slice_ms=args.slice_ms,
                        deferred=deferred,
                    )
                    p90 = sorted(halts)[int(len(halts) * 0.9)]
                    slow = sum(1 for h in halts if h > 4.0)
                    totals[name].append((statistics.median(halts), lost))
                    print(
                        f"  {name:10} {statistics.median(halts):9.2f}ms "
                        f"{p90:9.2f}ms {slow:>7}/{args.slices} "
                        f"{lost:>14,} {kept:>12,}"
                    )
        finally:
            strace.stop()
            link.go()          # leave the target running, as found

        print()
        for name in ("inline", "deferred"):
            medians = [m for m, _ in totals[name]]
            losses = [l for _, l in totals[name]]
            print(
                f"  {name:10} median halt {statistics.mean(medians):6.2f} ms   "
                f"mean lost {statistics.mean(losses):14,.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
