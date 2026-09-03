"""Command-line front end: ``python -m jtrace <command>``.

Exists so an agent can drive a J-Trace from a shell without writing a script,
and so a script that does exist has a worked example of every capture path.
Every command prints JSON with ``--json``, which is what makes the output
usable as a step in something larger.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .artifacts import list_coverage_reports, list_trace_sessions, read_trace_session
from .capture import (
    capture_coverage,
    capture_instruction_trace,
)
from .constants import DEFAULT_TRACE_BUFFER_BYTES, MAX_STRACE_ITEMS
from .coverage import compute_totals
from .elf import ElfFile
from .errors import JLinkError
from .frames import build_call_frames
from .link import JLink
from .loader import find_library
from .rows import InstructionRows
from .store import TraceStore
from .symbols import Symbolizer
from .thumb import instruction_starts


def _progress(message: str) -> None:
    print(f"  {message}", file=sys.stderr)


def _emit(payload: object, as_json: bool, plain: str | None = None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    elif plain is not None:
        print(plain)


# -- commands --------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    library = find_library()
    payload: dict[str, object] = {
        "version": __version__,
        "library": str(library) if library else None,
    }
    if library is None:
        _emit(payload, args.json, "J-Link software not found.")
        return 1
    probes = JLink.list_probes()
    payload["probes"] = [
        {
            "serialNumber": p.serial_number,
            "product": p.product,
            "nickname": p.nickname,
            "firmware": p.firmware,
            "connection": p.connection,
            "traceCapable": p.is_trace_capable,
        }
        for p in probes
    ]
    if args.json:
        _emit(payload, True)
    else:
        print(f"pytrace {__version__}")
        print(f"library: {library}")
        if not probes:
            print("probes:  none attached")
        for probe in probes:
            mark = " [trace]" if probe.is_trace_capable else ""
            # Firmware and nickname come back empty for a USB probe that has
            # not been opened -- the DLL does not populate them during
            # enumeration. Printing "fw=" with nothing after it reads as a bug
            # rather than as "not known yet".
            extra = f" fw={probe.firmware}" if probe.firmware else ""
            print(
                f"probe:   {probe.serial_number} {probe.product}{mark} "
                f"({probe.connection}){extra}"
            )
    return 0


def cmd_target(args: argparse.Namespace) -> int:
    with JLink(
        device=args.device,
        interface=args.interface,
        speed_khz=args.speed,
        serial_number=args.serial,
    ) as link:
        status = link.hardware_status()
        payload = {
            "device": args.device,
            "core": link.core_name(),
            "coreId": link.core_id(),
            "speedKhz": link.speed_khz,
            "targetVoltageMv": status.VTarget,
            "halted": link.is_halted,
            "connected": link.is_connected,
            "firmware": link.firmware_string(),
            "serialNumber": link.serial_number(),
        }
        if args.registers:
            with link.halted():
                payload["registers"] = {
                    name: f"0x{value:08x}"
                    for name, value in link.register_dump().items()
                }
    if args.json:
        _emit(payload, True)
    else:
        for key, value in payload.items():
            if key == "registers":
                print("registers:")
                for name, raw in value.items():  # type: ignore[union-attr]
                    print(f"  {name:<10} {raw}")
            else:
                print(f"{key}: {value}")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    session_id, result = capture_instruction_trace(
        args.elf,
        args.device,
        interface=args.interface,
        speed_khz=args.speed,
        serial_number=args.serial,
        port_width=args.port_width,
        duration_ms=args.duration,
        trace_items=args.items,
        buffer_bytes=args.buffer_bytes,
        cpu_freq_hz=args.cpu_freq,
        label=args.label,
        session_root=args.root,
        on_progress=None if args.json else _progress,
    )
    payload = {
        "sessionId": session_id,
        "instructionCount": result.summary.instruction_count,
        "instructionsExecuted": result.summary.instructions_executed,
        "frameCount": result.summary.frame_count,
        "maxDepth": result.summary.max_depth,
        "windowTruncated": result.summary.window_truncated,
    }
    if args.json:
        _emit(payload, True)
    else:
        print(f"session:      {session_id}")
        print(f"instructions: {result.summary.instruction_count:,} in the window")
        print(f"executed:     {result.summary.instructions_executed:,} total")
        print(
            f"frames:       {result.summary.frame_count:,}, "
            f"max depth {result.summary.max_depth}"
        )
        if result.summary.window_truncated:
            print("note:         the run outran the buffer; this is its tail")
        print("Open the Trace tab to view it.")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    path, result = capture_coverage(
        args.elf,
        args.device,
        interface=args.interface,
        speed_khz=args.speed,
        serial_number=args.serial,
        port_width=args.port_width,
        duration_ms=args.duration,
        buffer_bytes=args.buffer_bytes,
        label=args.label,
        session_id=args.session,
        project_root=args.root,
        on_progress=None if args.json else _progress,
    )
    assert result.rows is not None
    totals = compute_totals(result.rows.functions)
    payload = {"reportPath": str(path), "totals": totals.to_json()}
    if args.json:
        _emit(payload, True)
    else:
        print(f"report:       {path}")
        print(
            f"functions:    {totals.functions_covered}/{totals.functions} "
            f"({totals.function_percent:.1f}%)"
        )
        print(
            f"instructions: {totals.instructions_covered}/{totals.instructions} "
            f"({totals.instruction_percent:.1f}%)"
        )
        print(
            f"source lines: {totals.src_lines_covered}/{totals.src_lines} "
            f"({totals.line_percent:.1f}%)"
        )
        print("Open the Coverage tab to view it.")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    sessions = list_trace_sessions(args.root)
    if args.json:
        _emit(sessions, True)
        return 0
    if not sessions:
        print("No trace sessions found.")
        return 0
    for entry in sessions:
        counters = entry.get("counters", {})
        print(
            f"{entry.get('sessionId'):<28} {entry.get('startedAt'):<26} "
            f"{counters.get('eventCount', 0):>8} instructions"
        )
    return 0


def cmd_reports(args: argparse.Namespace) -> int:
    reports = list_coverage_reports(args.root)
    if args.json:
        _emit(reports, True)
        return 0
    if not reports:
        print("No coverage reports found.")
        return 0
    for entry in reports:
        totals = entry.get("totals", {})
        covered = totals.get("functionsCovered", 0)
        total = totals.get("functions", 0)
        percent = 100.0 * covered / total if total else 0.0
        print(
            f"{entry.get('reportId'):<28} {entry.get('createdAt'):<26} "
            f"{covered}/{total} functions ({percent:.1f}%)"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    session = read_trace_session(args.session_id, args.root)
    if session is None:
        print(f"No such session: {args.session_id}", file=sys.stderr)
        return 1
    payload = session.get("instructions", {})
    if args.json:
        _emit(session, True)
        return 0
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    print(f"session:      {args.session_id}")
    for key, label in (
        ("instructionCount", "instructions"),
        ("instructionsExecuted", "executed"),
        ("frameCount", "frames"),
        ("maxDepth", "max depth"),
    ):
        print(f"{label + ':':<14}{summary.get(key, 0):,}")
    frames = payload.get("frames", []) if isinstance(payload, dict) else []
    for frame in frames[: args.limit]:
        indent = "  " * frame.get("depth", 0)
        width = frame.get("endIndex", 0) - frame.get("startIndex", 0)
        print(f"  {indent}{frame.get('name')} ({width} instructions)")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Describe a stored trace snapshot, from its header alone.

    `replay` needs an image to resolve against; this needs nothing, and it does
    not decompress the stream either -- everything it prints is a property of
    the capture's shape, which the header already carries. Inspecting a ten
    million instruction capture costs about 50 KB rather than 40 MB.
    """
    header = TraceStore.describe(args.snapshot)
    stats = header.get("stats", {})
    payload = {
        "path": str(Path(args.snapshot).resolve()),
        "bytesOnDisk": Path(args.snapshot).stat().st_size,
        "instructionCount": header["instructionCount"],
        "blocks": header["blockCount"],
        "boundaries": header["boundaries"],
        "lost": stats.get("lost", 0),
        "gaps": stats.get("gaps", 0),
        "polls": stats.get("polls", 0),
        "droppedInstructions": stats.get("droppedInstructions", 0),
        "stamped": header["stampedBlocks"],
        "stampOrderObserved": header.get("stampOrderObserved"),
        "codec": header.get("codec"),
        "meta": header.get("meta", {}),
    }
    if args.json:
        _emit(payload, True)
        return 0

    print(f"snapshot:     {payload['path']}")
    print(
        f"size:         {payload['bytesOnDisk']:,} bytes "
        f"for {payload['instructionCount']:,} instructions ({payload['codec']})"
    )
    print(f"blocks:       {payload['blocks']:,} uninterrupted run(s)")
    if payload["lost"]:
        print(
            f"lost:         {payload['lost']:,} instructions at "
            f"{len(payload['boundaries'])} boundary/ies -> {payload['boundaries'][:8]}"
        )
    else:
        print("lost:         none; the stream is contiguous")
    if payload["droppedInstructions"]:
        print(f"evicted:      {payload['droppedInstructions']:,} (capacity bound)")
    if payload["stamped"]:
        observed = payload["stampOrderObserved"]
        order = (
            "unobserved"
            if observed is None
            else ("newest-first" if observed else "oldest-first")
        )
        print(
            f"timing:       {payload['stamped']}/{payload['blocks']} blocks "
            f"stamped; probe order observed as {order}"
        )
    else:
        print("timing:       no cycle stamps (captured without timestamps)")
    captured = payload["meta"].get("elfPath")
    if captured:
        print(f"captured against: {captured}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-symbolize a stored capture against an ELF, with no probe involved.

    The capability the snapshot exists for. ``instructions.json`` holds rows,
    which are the stream already resolved against one particular build; once
    written, a row cannot be re-attributed. ``raw/trace.jt1`` holds the program
    counters, so the same capture can be pointed at a rebuilt or differently
    optimised image -- or simply at an ELF that was not to hand at capture time.
    """
    store = TraceStore.load(args.snapshot)
    symbolizer = Symbolizer(ElfFile(args.elf))
    rows = InstructionRows(store, symbolizer)
    frames = build_call_frames(store, symbolizer, boundaries=store.boundaries())

    payload = {
        "snapshot": str(Path(args.snapshot).resolve()),
        "elfPath": str(Path(args.elf).resolve()),
        "capturedAgainst": store.meta.get("elfPath"),
        "instructionCount": len(store),
        "blocks": len(store.blocks),
        "boundaries": store.boundaries(),
        "lost": store.stats().lost,
        "frameCount": len(frames.frames),
        "maxDepth": frames.max_depth,
        "cyclesContinuous": store.cycles_continuous,
    }
    if args.json:
        _emit(payload, True)
        return 0

    print(f"snapshot:     {payload['snapshot']}")
    captured = payload["capturedAgainst"]
    if captured and captured != payload["elfPath"]:
        print(f"captured against: {captured}")
        print(f"replayed against: {payload['elfPath']}")
    else:
        print(f"elf:          {payload['elfPath']}")
    print(f"instructions: {len(store):,} in {len(store.blocks)} block(s)")
    if store.stats().lost:
        print(
            f"lost:         {store.stats().lost:,} instructions "
            f"at {len(store.boundaries())} boundary/ies"
        )
    print(f"frames:       {len(frames.frames):,} (max depth {frames.max_depth})")
    for row in rows.runs():
        if row.start_index >= args.limit:
            break
        print(
            f"  [{row.start_index:>8}] {row.function or '?'} "
            f"({len(row)} instructions)"
            + (f"  {row.file}:{row.line}" if row.file else "")
        )
    return 0


def cmd_elf(args: argparse.Namespace) -> int:
    elf = ElfFile(args.elf)
    symbolizer = Symbolizer(elf)
    sections = elf.executable_sections()
    total_starts = sum(
        len(instruction_starts(elf.read_code(section), section.addr))
        for section in sections
    )
    payload = {
        "path": str(Path(args.elf).resolve()),
        "machine": elf.machine,
        "entry": elf.entry,
        "executableSections": [
            {"name": s.name, "addr": s.addr, "size": s.size} for s in sections
        ],
        "functions": len(elf.function_symbols()),
        "instructionStarts": total_starts,
        "hasLineInfo": symbolizer.has_line_info,
        "lineRows": len(symbolizer.line_table),
    }
    if args.json:
        _emit(payload, True)
    else:
        print(f"path:      {payload['path']}")
        print(f"entry:     0x{elf.entry:08x}")
        for section in sections:
            print(f"section:   {section.name} @ 0x{section.addr:08x} ({section.size} bytes)")
        print(f"functions: {payload['functions']}")
        print(f"instructions: {total_starts}")
        print(
            f"line info: {'yes' if symbolizer.has_line_info else 'no'} "
            f"({len(symbolizer.line_table)} rows)"
        )
    return 0


def cmd_rtt(args: argparse.Namespace) -> int:
    with JLink(
        device=args.device,
        interface=args.interface,
        speed_khz=args.speed,
        serial_number=args.serial,
    ) as link:
        rtt = link.rtt
        rtt.start(args.control_block)
        if not rtt.wait_until_running(args.timeout):
            print("RTT did not come up: the target may not have run SEGGER_RTT_Init",
                  file=sys.stderr)
            return 1
        try:
            for chunk in rtt.stream(args.channel, duration_s=args.duration):
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
        finally:
            rtt.stop()
    return 0


# -- argument plumbing -----------------------------------------------------


def _add_probe_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", required=True, help="J-Link device name")
    parser.add_argument("--interface", default="SWD", choices=["SWD", "JTAG"])
    parser.add_argument("--speed", type=int, default=4000, help="kHz")
    parser.add_argument("--serial", type=int, default=None, help="probe serial number")


def _add_capture_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--elf", required=True, help="firmware ELF")
    _add_probe_args(parser)
    parser.add_argument("--duration", type=int, default=3000, help="ms")
    parser.add_argument("--port-width", type=int, default=4, choices=[1, 2, 4])
    parser.add_argument("--buffer-bytes", type=int, default=DEFAULT_TRACE_BUFFER_BYTES)
    parser.add_argument("--label", default=None)


def build_parser() -> argparse.ArgumentParser:
    # Global flags are accepted on either side of the subcommand. Only allowing
    # them before it is the argparse default and it is a trap: every natural
    # invocation puts them at the end.
    #
    # The subcommand copies default to SUPPRESS rather than to a real value.
    # Without that, the subparser writes its own default into the shared
    # namespace after the root parser has already set the flag, so `--json`
    # given *before* the subcommand would be silently discarded -- which is a
    # worse bug than the one this fixes.
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON"
    )
    sub_common.add_argument(
        "--root", default=argparse.SUPPRESS, help="project root (default: cwd)"
    )

    parser = argparse.ArgumentParser(
        prog="python -m jtrace",
        description="Drive a SEGGER J-Link or J-Trace: trace, coverage, RTT.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--root", default=None, help="project root (default: cwd)")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=lambda **kw: argparse.ArgumentParser(parents=[sub_common], **kw),
    )

    info = sub.add_parser("info", help="show the DLL path and attached probes")
    info.set_defaults(func=cmd_info)

    target = sub.add_parser("target", help="connect and report target state")
    _add_probe_args(target)
    target.add_argument("--registers", action="store_true", help="dump core registers")
    target.set_defaults(func=cmd_target)

    trace = sub.add_parser("trace", help="capture an instruction trace")
    _add_capture_args(trace)
    trace.add_argument(
        "--items", type=int, default=MAX_STRACE_ITEMS,
        help=f"instructions to capture. Above {MAX_STRACE_ITEMS:,} the target "
             f"is run in slices, which is lossless but halts it between them.",
    )
    trace.add_argument("--cpu-freq", type=int, default=None, help="Hz, for the time axis")
    trace.set_defaults(func=cmd_trace)

    coverage = sub.add_parser("coverage", help="capture a coverage report")
    _add_capture_args(coverage)
    coverage.add_argument("--session", default="default", help="coverage session id")
    coverage.set_defaults(func=cmd_coverage)

    sessions = sub.add_parser("sessions", help="list stored trace sessions")
    sessions.set_defaults(func=cmd_sessions)

    snapshot = sub.add_parser(
        "snapshot", help="describe a stored trace snapshot (no ELF needed)"
    )
    snapshot.add_argument("snapshot", help="path to raw/trace.jt1")
    snapshot.set_defaults(func=cmd_snapshot)

    replay = sub.add_parser(
        "replay", help="re-symbolize a stored trace snapshot against an ELF"
    )
    replay.add_argument("snapshot", help="path to raw/trace.jt1")
    replay.add_argument("elf", help="ELF to resolve against (need not be the original)")
    replay.add_argument("--limit", type=int, default=20, help="runs to print")
    replay.set_defaults(func=cmd_replay)

    reports = sub.add_parser("reports", help="list stored coverage reports")
    reports.set_defaults(func=cmd_reports)

    show = sub.add_parser("show", help="summarise one stored trace session")
    show.add_argument("session_id")
    show.add_argument("--limit", type=int, default=20, help="frames to print")
    show.set_defaults(func=cmd_show)

    elf = sub.add_parser("elf", help="inspect a firmware ELF (no probe needed)")
    elf.add_argument("--elf", required=True)
    elf.set_defaults(func=cmd_elf)

    rtt = sub.add_parser("rtt", help="stream RTT output")
    _add_probe_args(rtt)
    rtt.add_argument("--channel", type=int, default=0)
    rtt.add_argument("--duration", type=float, default=10.0, help="seconds")
    rtt.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for RTT")
    rtt.add_argument(
        "--control-block", type=lambda v: int(v, 0), default=0,
        help="control block address; 0 auto-detects",
    )
    rtt.set_defaults(func=cmd_rtt)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except JLinkError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
