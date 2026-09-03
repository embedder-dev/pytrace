"""CLI argument handling.

Small surface, but the failure mode is bad: a global flag that parses without
error and is then silently discarded looks exactly like the feature not
working.
"""

import pytest

from jtrace.cli import build_parser


@pytest.mark.parametrize(
    "argv",
    [
        ["elf", "--elf", "fw.elf", "--json"],   # after the subcommand
        ["--json", "elf", "--elf", "fw.elf"],   # before it
    ],
)
def test_json_is_accepted_on_either_side_of_the_subcommand(argv):
    assert build_parser().parse_args(argv).json is True


def test_json_defaults_off():
    assert build_parser().parse_args(["elf", "--elf", "fw.elf"]).json is False


@pytest.mark.parametrize(
    "argv",
    [
        ["sessions", "--root", "/tmp/x"],
        ["--root", "/tmp/x", "sessions"],
    ],
)
def test_root_is_accepted_on_either_side(argv):
    assert build_parser().parse_args(argv).root == "/tmp/x"


def test_root_defaults_to_none():
    assert build_parser().parse_args(["sessions"]).root is None


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_trace_defaults_to_the_single_read_clamp():
    from jtrace.constants import MAX_STRACE_ITEMS

    args = build_parser().parse_args(
        ["trace", "--elf", "fw.elf", "--device", "STM32F407VE"]
    )
    assert args.items == MAX_STRACE_ITEMS


def test_trace_accepts_a_long_capture():
    args = build_parser().parse_args(
        ["trace", "--elf", "fw.elf", "--device", "STM32F407VE", "--items", "1000000"]
    )
    assert args.items == 1_000_000


def test_every_subcommand_carries_the_global_flags():
    parser = build_parser()
    for argv in (
        ["info"],
        ["sessions"],
        ["reports"],
        ["elf", "--elf", "fw.elf"],
        ["show", "etm-x"],
        ["target", "--device", "d"],
        ["trace", "--elf", "f", "--device", "d"],
        ["coverage", "--elf", "f", "--device", "d"],
        ["rtt", "--device", "d"],
        ["replay", "t.jt1", "fw.elf"],
        ["snapshot", "t.jt1"],
    ):
        args = parser.parse_args([*argv, "--json"])
        assert args.json is True, argv


def test_replay_takes_a_snapshot_and_an_elf():
    args = build_parser().parse_args(["replay", "raw/trace.jt1", "fw.elf"])
    assert args.snapshot == "raw/trace.jt1"
    assert args.elf == "fw.elf"
    assert args.limit == 20


def test_replay_re_symbolizes_a_snapshot_without_a_probe(tmp_path, capsys, oracle_elf):
    """The capability the snapshot exists for. `instructions.json` holds rows
    already resolved against one build; the snapshot holds the stream, so the
    same capture can be pointed at a different ELF afterwards.
    """
    import argparse
    import array
    import json

    from jtrace.cli import cmd_replay
    from jtrace.elf import ElfFile
    from jtrace.store import TraceStore

    section = ElfFile(oracle_elf).executable_sections()[0]
    store = TraceStore()
    store.append_block(
        array.array("I", [section.addr + (i % 8) * 2 for i in range(64)])
    )
    store.append_block(
        array.array("I", [section.addr + 4 for _ in range(8)]), lost_before=99
    )
    path = store.save(tmp_path / "trace.jt1", meta={"elfPath": "/elsewhere/fw.elf"})

    args = argparse.Namespace(
        snapshot=str(path), elf=str(oracle_elf), limit=5, json=True, root=None
    )
    assert cmd_replay(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["instructionCount"] == 72
    assert payload["blocks"] == 2
    assert payload["boundaries"] == [64]
    assert payload["lost"] == 99
    assert payload["capturedAgainst"] == "/elsewhere/fw.elf"
    assert payload["frameCount"] > 0


def test_snapshot_describes_a_capture_without_an_elf(tmp_path, capsys):
    """`replay` needs an image; this needs nothing. It reports the properties
    of the capture itself rather than of any particular build."""
    import argparse
    import array
    import json

    from jtrace.cli import cmd_snapshot
    from jtrace.store import TraceStore

    store = TraceStore()
    store.append_block(array.array("I", range(32)), cycles=[0, 310], stamp_at=[0, 31])
    store.append_block(array.array("I", range(32, 48)), lost_before=12)
    path = store.save(tmp_path / "trace.jt1")

    args = argparse.Namespace(snapshot=str(path), json=True, root=None)
    assert cmd_snapshot(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["instructionCount"] == 48
    assert payload["blocks"] == 2
    assert payload["boundaries"] == [32]
    assert payload["lost"] == 12
    assert payload["stamped"] == 1
    assert payload["stampOrderObserved"] is None
    assert payload["codec"] == "zlib"
