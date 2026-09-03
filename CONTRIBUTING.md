# Contributing

## Setup

```bash
git clone https://github.com/embedder-dev/pytrace
cd pytrace
pip install -e ".[dev]"
python3 -m pytest tests/ -q
```

That is the whole setup. **No probe and no SEGGER software are needed to run
the test suite** — the oracle firmware every hardware-shaped test resolves
against is committed under `tests/fixtures/firmware/`.

If a test skips, something is wrong with the checkout rather than with your
machine. The suite is designed to run in full, and CI fails on any skip.

## The shape of the codebase

```
jtrace/loader.py     ctypes binding: locating the library, declaring prototypes
jtrace/link.py       the probe: run control, memory, registers, breakpoints
jtrace/strace.py     ETM instruction trace (decoded program counters)
jtrace/tracebuf.py   the raw, offset-addressable trace buffer
jtrace/swo.py rtt.py hss.py powertrace.py coresight.py    other subsystems
jtrace/store.py      program counters, block-segmented, bounded, snapshottable
jtrace/elf.py dwarf.py symbols.py    address -> function / file:line
jtrace/rows.py       lazy instruction rows over a PC stream
jtrace/frames.py     call-stack reconstruction
jtrace/coverage.py   per-function and per-line coverage rows
jtrace/artifacts.py  writing a session to disk
jtrace/cli.py        python -m jtrace
```

## House style

**Comments say why, not what.** The code says what it does. A comment earns its
place by recording the reason a thing is the way it is — usually a failure mode
that is not visible from the code. If a comment would restate the line below
it, delete it.

**Record how much a value is trusted.** `jtrace/constants.py` tags every group
with its provenance:

- `sdk` — from SEGGER's public headers.
- `observed` — not published in a header; recovered from the shipped library
  and corroborated against a live probe. Version-specific, not a contract.
- `empirical` — measured on real hardware against known-count firmware.

Anything unverified says so in its own comment. **Guesses do not go in.** If
you do not know what a field means, carry it through untouched and document
that it is unverified — `StraceTimestampInfo.Adjust` is the worked example.

**Do not document how to reverse-engineer the vendor's library.** Publishing a
constant recovered from it is fine and is what makes this SDK useful.
Publishing binary offsets, instruction sequences or a method for repeating the
analysis is not. Say what a value is and how far to trust it; not where it was
found.

**Failures should be loud where silence would be worse.** A large part of this
SDK exists because the underlying library fails quietly — a missing trace cache
gives you wrong counts rather than an error, a reversed buffer gives you a
plausible and entirely wrong timeline. Where you can turn a silent wrong answer
into a loud one, do.

## Tests

New behaviour needs a test. Two conventions worth following:

- **Test names are sentences.** `test_a_lossy_capture_never_produces_a_frame_
  spanning_the_hole` says what is being protected; `test_frames_2` does not.
- **A test's docstring explains what would break.** Several tests here exist
  because of a specific defect; the docstring records it, so a later reader can
  tell an important assertion from an incidental one.

The fakes are layered on purpose. `tests/test_capture.py` replaces `JLink`
wholesale and pins the *order* of the capture sequence.
`tests/test_capture_e2e.py` replaces the bound library instead, so the real
`JLink`, `Strace` and every ctypes buffer execute — only the USB transport is
absent. Prefer the second when the thing you are testing could plausibly break
in the wrapper.

## Hardware changes

If you change anything on the capture path and have a probe, say so in the PR:
which probe, which target, and what you observed. If you do not have hardware,
say that too — it is useful information for a reviewer, not a disqualification,
and the fake-probe tests cover a great deal.

## Pull requests

- One concern per PR.
- CI must pass on all supported Python versions, with no skips.
- Update `CHANGELOG.md` under `Unreleased`.
