"""Call-stack reconstruction from a program-counter stream.

The rules being pinned here are the ones that are wrong in a way you cannot see
from the output: a Thumb-bit mask that only fails on odd entry addresses, and a
level normalisation that only matters once execution returns into a caller from
before the capture window.
"""



from jtrace.frames import MIN_LEVEL, build_call_frames
from jtrace.symbols import ResolvedFunction, ResolvedSource


class FakeSymbolizer:
    """Resolves against a list of (name, entry_with_thumb_bit, size)."""

    def __init__(self, functions):
        self.functions = functions

    def resolve_function(self, address):
        target = address & ~1
        for name, entry, size in self.functions:
            start = entry & ~1
            if start <= target < start + size:
                return ResolvedFunction(name=name, addr=entry, size=size)
        return None

    def resolve_source(self, address):
        function = self.resolve_function(address)
        return ResolvedSource(file="demo.c", line=1) if function else None


# Entry addresses carry the Thumb bit, exactly as ELF symbols do. A frame
# builder that forgets to mask never matches an entry, so nothing is ever
# recognised as a call -- and the failure is invisible on even addresses.
MAIN = ("main", 0x0800_0101, 0x20)
WORKER = ("worker", 0x0800_0201, 0x20)
INNER = ("inner", 0x0800_0301, 0x20)
SYMS = FakeSymbolizer([MAIN, WORKER, INNER])


def names_at(frames, depth):
    return [f.name for f in frames if f.depth == depth]


def test_single_function_is_one_frame_open_at_both_ends():
    result = build_call_frames([0x0800_0100, 0x0800_0102, 0x0800_0104], SYMS)
    assert len(result.frames) == 1
    frame = result.frames[0]
    assert frame.name == "main"
    assert frame.start_index == 0
    assert frame.end_index == 3
    assert frame.open_at_start is True
    assert frame.open_at_end is True
    assert result.max_depth == 1


def test_entry_address_opens_a_nested_frame():
    stream = [
        0x0800_0100,  # main, mid-function (window opened here)
        0x0800_0200,  # worker entry -> a call
        0x0800_0202,
        0x0800_0102,  # back in main -> worker returns
    ]
    result = build_call_frames(stream, SYMS)
    assert names_at(result.frames, 0) == ["main"]
    assert names_at(result.frames, 1) == ["worker"]
    worker = next(f for f in result.frames if f.name == "worker")
    assert (worker.start_index, worker.end_index) == (1, 3)
    assert worker.open_at_start is False


def test_frame_address_is_masked_to_the_even_entry():
    result = build_call_frames([0x0800_0200, 0x0800_0202], SYMS)
    assert result.frames[0].address == 0x0800_0200


def test_recursion_stacks_rather_than_returning():
    # Landing on the entry address again is a call, not a return -- which is
    # the only thing that distinguishes recursion from a loop back to the top.
    stream = [0x0800_0200, 0x0800_0202, 0x0800_0200, 0x0800_0202]
    result = build_call_frames(stream, SYMS)
    assert [f.depth for f in result.frames] == [0, 1]
    assert all(f.name == "worker" for f in result.frames)


def test_return_into_unseen_ancestor_goes_one_level_down():
    # The window opens inside `inner`; execution then lands mid-`main`, which
    # was on the stack before the capture began.
    stream = [0x0800_0300, 0x0800_0302, 0x0800_0104]
    result = build_call_frames(stream, SYMS)
    inner = next(f for f in result.frames if f.name == "inner")
    main = next(f for f in result.frames if f.name == "main")
    # Levels are normalised so the shallowest becomes 0, so main -- entered
    # earlier in real time -- ends up below inner.
    assert main.depth == 0
    assert inner.depth == 1
    assert main.open_at_start is True


def test_deep_return_chain_is_bounded():
    # A stream that only ever returns must not walk levels down without limit.
    functions = [(f"f{i}", 0x0801_0001 + i * 0x40, 0x20) for i in range(80)]
    syms = FakeSymbolizer(functions)
    stream = [(0x0801_0000 + i * 0x40) + 4 for i in range(80)]
    result = build_call_frames(stream, syms)
    assert result.max_depth <= abs(MIN_LEVEL) + 1
    assert all(f.depth >= 0 for f in result.frames)


def test_nesting_is_capped():
    functions = [(f"f{i}", 0x0802_0001 + i * 0x40, 0x20) for i in range(80)]
    syms = FakeSymbolizer(functions)
    stream = [0x0802_0000 + i * 0x40 for i in range(80)]  # every one an entry
    result = build_call_frames(stream, syms, max_depth=8)
    assert max(f.depth for f in result.frames) < 8


def test_unresolved_run_stays_one_region():
    # Unknown identity is "same as any unknown", so a stretch of unsymbolized
    # addresses is one frame rather than one frame per instruction.
    stream = [0x2000_0000, 0x2000_0002, 0x2000_0004, 0x2000_0006]
    result = build_call_frames(stream, SYMS)
    assert len(result.frames) == 1
    assert result.frames[0].name == "0x20000000"


def test_empty_stream():
    result = build_call_frames([], SYMS)
    assert result.frames == []
    assert result.max_depth == 0


def test_a_gap_closes_the_stack_rather_than_inventing_a_transition():
    """The bug this pins: running straight across a hole in the stream.

    Instructions between the two windows are gone, so the address after the
    hole gets compared against a stack describing execution before it. Without
    a declared boundary that mismatch is reported as a call or a return that
    never happened -- a frame the target never entered, indistinguishable from
    a decoder bug.
    """
    # main calls worker; then a hole; then execution resumes inside inner.
    addresses = [0x0800_0100, 0x0800_0102, 0x0800_0200, 0x0800_0304]

    seamless = build_call_frames(addresses, SYMS)
    split = build_call_frames(addresses, SYMS, boundaries=[3])

    # Nothing spans the hole.
    assert all(
        frame.end_index <= 3 or frame.start_index >= 3 for frame in split.frames
    )
    reopened = [f for f in split.frames if f.start_index == 3]
    assert reopened and all(f.open_at_start for f in reopened)
    assert all(f.open_at_end for f in split.frames if f.end_index == 3)
    # Without the boundary at least one frame runs straight through it.
    assert any(f.start_index < 3 < f.end_index for f in seamless.frames)


def test_no_boundaries_is_byte_for_byte_the_old_behaviour():
    """The TypeScript producer cannot emit a gapped stream, so the default has
    to reproduce it exactly or the two disagree on identical input."""
    addresses = [0x0800_0100, 0x0800_0102, 0x0800_0200, 0x0800_0104]
    assert [f.to_json() for f in build_call_frames(addresses, SYMS).frames] == [
        f.to_json()
        for f in build_call_frames(addresses, SYMS, boundaries=()).frames
    ]


def test_frames_are_sorted_by_start_then_depth():
    stream = [
        0x0800_0100,
        0x0800_0200,
        0x0800_0300,
        0x0800_0302,
        0x0800_0202,
        0x0800_0102,
    ]
    result = build_call_frames(stream, SYMS)
    keys = [(f.start_index, f.depth) for f in result.frames]
    assert keys == sorted(keys)


def test_json_shape_matches_the_protocol():
    result = build_call_frames([0x0800_0100, 0x0800_0200, 0x0800_0102], SYMS)
    payload = result.frames[0].to_json()
    assert set(payload) >= {"name", "address", "depth", "startIndex", "endIndex"}
    assert isinstance(payload["startIndex"], int)
    # Optional flags are omitted rather than set false, matching the TS producer.
    closed = next(f for f in result.frames if not f.open_at_end)
    assert "openAtEnd" not in closed.to_json()
