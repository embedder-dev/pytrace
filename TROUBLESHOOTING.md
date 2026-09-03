# Troubleshooting

Things that bite, in rough order of how often they bite. Most trace problems
are one of the first three.

## "Trace start failed", or the buffer comes back empty

**Check the cable first.** ETM needs the fine-pitch CoreSight-20 cable. The
wide 0.1" ribbon carries no trace signals at all, and a target wired through it
will connect, halt, single-step and flash perfectly — everything except trace.
This is the single most common cause.

After that, in order:

- **The target has to drive TRACECLK.** Some families need their debug clock
  enabled before the trace port produces anything. An STM32C5, for instance,
  needs its DBGMCU trace bits set; `JLink.coresight` is there for exactly these
  bring-up sequences.
- **You need a J-Trace, not a J-Link.** ETM instruction trace needs a four-pin
  trace port and a probe that can capture it. A plain J-Link does SWO and
  nothing else.
- **The target must actually be running.** A capture over a halted core
  produces an empty window, correctly.

## Every count is zero, or implausibly small

You almost certainly skipped `ReadIntoTraceCache`.

```python
for section in elf.executable_sections():
    link.read_into_trace_cache(section.addr, section.size)
```

This is make-or-break and it is not optional. ETM does not transmit every
instruction — it transmits branches and periodic sync points, and the DLL
reconstructs the full stream by walking the code image between them. Without
the image it cannot do that, and **it does not fail**: it returns a stream that
is short, or empty, and every count derived from it is silently wrong rather
than obviously missing.

`capture_instruction_trace` and `capture_coverage` do this for you, before
starting trace. If you hand-roll a capture, do it in that order too.

## Callees appear to contain their callers

You reversed the wrong buffer, or reversed one twice.

`Strace.read()` returns the probe's order, which is **newest first**.
`read_extended()` and every capture helper return chronological order, oldest
first. If you are mixing the two, normalise once, at the boundary:

```python
window = strace.read()                       # newest first
chronological = array.array("I", reversed(window))
```

`jtrace.capture.to_chronological` does this and symbolizes in one step.

## The trace only covers the end of the run

That is expected, and `summary.window_truncated` tells you so.

The probe buffer is a ring. If the target retired more instructions than it
holds, what you read back is the **tail** of execution, not all of it. For real
firmware at any real clock, that is the normal case for a short capture.

- `summary.instructions_executed` — the whole run.
- `summary.instruction_count` — only what was read back.

Coverage is not affected: coverage counts come from instruction statistics,
which cover the whole run whether or not the window held it.

To capture from reset instead of from the end, ask for more than one buffer's
worth — see below.

## Captures longer than 65,536 instructions

A single DLL read clamps at 65,536 items. That is a per-call ceiling, not a
limit on how much you can capture. Ask for more and the SDK runs the target in
slices and drains the buffer each time:

```python
session_id, result = capture_instruction_trace(
    "firmware.elf", "STM32F407VE",
    trace_items=1_000_000, duration_ms=30_000,
)
assert result.streaming.is_continuous   # False means instructions were dropped
```

On a slow core this reaches a million instructions with zero loss. It also
captures the reset-to-`main` startup sequence, which a 65,536-item tail window
can never contain.

**Zero loss is not clock-independent, and the default slice does not survive a
fast core.** The same capture on a fast core comes back with gaps and
`is_continuous False`. A much shorter `slice_ms` passed to
`Strace.read_extended` can get a given run to zero loss -- but see below before
treating it as a fix, because a longer run at the same setting loses
instructions again.

The reason is that the 65,536-item clamp is a budget in *instructions*, so
faster silicon spends it sooner. Expressed as running time:

| core clock | instructions/s | clamp reached after |
|---|---|---|
| 16 MHz | ~10.6M | 6.2 ms |
| 72 MHz | ~47.6M | 1.4 ms |
| 168 MHz | ~111M | 0.59 ms |
| 400 MHz | ~264M | 0.25 ms |

The default 0.5 ms slice sits comfortably inside a 16 MHz budget and at 98% of
a 168 MHz one, which is why it worked on one board and not the other.

Two things eat what margin is left:

- `time.sleep` overshoots badly at this scale -- a requested 0.05 ms measured
  0.11 ms on an ARM Linux host and 0.07 ms on macOS.
- The `go`/`halt` edges add running time the sleep does not account for.

A third used to, and was the largest of them. The SDK reversed and copied each
window *between* a halt and the next go, and that work inflated the following
`halt()` round-trip several-fold -- with the core running for the excess, so it
came out of the capture as lost instructions. The copy now happens after the
slice loop ends. `bench/slice_latency.py` runs both shapes against the same
target, alternating them so drift cannot favour one, and reports halt latency
and loss for each.

Deferring the copy cuts the median halt several-fold and most of the loss with
it, keeping the same instructions either way. That is a large improvement and
**not a cure** -- on a fast core the deferred run still loses instructions.

So treat `clamp / instructions-per-second` as a **ceiling to stay well under,
not a setting**. On a fast core a slice a twelfth of the ceiling can still lose
instructions over a long run, so a value that measures clean once is not a
remedy. Pick with room to spare, then confirm empirically:
capture once, check `result.streaming.is_continuous`, and shorten the slice if
it is False. Loss on a fast target is erratic rather than proportional, which
is the real argument for verifying per target rather than reasoning from the
table.

**Always check `result.streaming.is_continuous`.** `streaming.gaps` counts
slices where the target outran the buffer, and `streaming.lost` is the exact
number of instructions dropped.

A lossy capture is still trustworthy about *what* it holds. The hole is
recorded where it happened -- `result.store.boundaries()` gives the indices --
and the call-frame builder closes and reopens the stack there, so no frame is
drawn across a gap claiming a call or a return that never happened.

What loss costs you is the **stream**, and only the stream. The instruction
list and the call frames are missing regions of execution, so anything you
report from them should say which region it came from.

Coverage is unaffected. Rows and run counts are built from instruction
statistics, which count through the holes, so they stay exact for the whole run
-- the same reason they cover more than the readable window. A lossy capture
still reports never-called functions at 0, and identical run counts for
functions that ran the same number of times.

**The target is halted between slices.** A long capture is a concatenation of
contiguous windows, not one uninterrupted real-time trace. Fine for a
free-running loop; not fine for anything driven by an interrupt, a timeout or a
peripheral handshake. Say so when reporting results from timing-sensitive code.

`result.store` is where the gaps actually are:

```python
result.store.boundaries()            # indices where instructions were lost
len(result.store.blocks)             # uninterrupted runs of the core
result.store.blocks[0].halt_reason   # raw MoeInfo value, undecoded
```

Call frames are already closed and reopened at those boundaries, so no frame
ever spans a hole.

## The session is enormous, or slow to open

`instructions.json` grows about 170 bytes per row: 65,536 rows is ~11 MB, a
million is ~166 MB, and a viewer reads and validates the file whole. Above
~250,000 rows `write_trace_session` warns.

In memory the same capture is cheap — a million instructions is about 4 MB,
because `result.instructions` builds rows on access rather than holding them.
So analyse a large capture in Python rather than in a viewer:

```python
for run in result.instructions.runs():        # one entry per same-line stretch
    print(run.function, len(run))
```

## Re-analysing a capture against a different build

`raw/trace.jt1` in the session directory is the program counters themselves,
compressed — about 20–30 KB per million instructions. It re-symbolizes against
any ELF, including one that did not exist when the capture ran:

```bash
python3 -m jtrace snapshot .pytrace/traces/<id>/raw/trace.jt1   # no ELF needed
python3 -m jtrace replay  .pytrace/traces/<id>/raw/trace.jt1 firmware.elf
```

Neither needs a probe. `instructions.json` holds rows, which are the stream
already resolved against one particular build and cannot be re-attributed; the
snapshot holds the stream.

## "J-Link software not found"

`find_library()` looks in the packaged install locations, then `PATH`, then the
Windows registry, then any unpacked SEGGER tarball directly under `$HOME` —
`~/JLink_Linux_V972_arm64/libjlinkarm.so` and the like, newest first.

If it still comes up empty, point at the library directly:

```bash
export JLINK_LIBRARY=$HOME/JLink_Linux_V972_arm64/libjlinkarm.so
python3 -m jtrace info
```

That override beats everything, and it is also how you pin one version when
several are installed side by side — which unpacked tarballs make more likely,
not less. Only directories one level below `$HOME` whose names start with
`JLink` are searched; `$HOME` is not walked.

## Cycle timestamps are absent

They are off by default, and turning them on takes **two** steps, not one.
Missing the second is the usual reason a stamped capture comes back with no
stamps at all:

```python
link.exec_command("TRACE_SetEnableTimestamps = 1")   # <- easy to miss
store = link.strace.read_extended(..., timestamps=True)
```

Without the exec command `STRACE_ReadEx` returns program counters and zero
stamps, however many instructions come back.

What to expect once it is on:

- **Cadence** is roughly one stamp per 700 instructions, so a cycle count
  between two of them is a short interpolation rather than a wild guess.
- **Cycle counts run forward across a halt**, so `store.cycles_continuous`
  should report `True`.
- The window comes back newest-first, like an unstamped read. That is detected
  from the stamps rather than assumed, so the timeline cannot silently invert;
  `store.stamp_order_observed` records what the probe actually did.

A cycle count between two stamps is interpolated either way;
`CycleEstimate.exact` says which is which, and `CycleEstimate.span` says how
far apart the stamps that produced it were.

## Timing looks wrong

ETM records **order, never duration**. A call frame's width is work done, not
wall-clock elapsed. `cpu_freq_hz` turns an instruction index into an estimate
and nothing more — it assumes one instruction per cycle, which is not true of
any real Cortex-M.

## The DLL opens a window and the script hangs

A wrong device name makes the DLL put up a native modal dialog, and from a
script there is nobody to click it. `JLink` suppresses the common ones on open
(`SetBatchMode`, `HideDeviceSelection`, and friends), but the connect will
still fail — with a name, not a hang.

Do not guess a device name. `python3 -m jtrace info` lists what is attached;
the device string is the one SEGGER uses, e.g. `STM32F407VE`.

## Everything hangs after a crashed script

A J-Link left open wedges every later flash, debug session and RTT connection
on the machine until the process exits.

**Always use the context manager.**

```python
with JLink(device="STM32F407VE") as jl:
    ...
```

Only one probe can be open per process; a second `JLink()` raises rather than
silently stealing it. `jtrace.link.close_open_link()` is a net for a script
that raised before its own `close()` — it is not a substitute, and it does not
help you between two `JLink()` calls in one script.

## Symbols are missing or mangled

- **No `file`/`line` anywhere** — the ELF has no `.debug_line`. Build with
  `-g`. `python3 -m jtrace elf --elf firmware.elf` reports whether line info
  was found and how many rows.
- **C++ names come out mangled** — no demangler is installed. Install the
  `demangle` extra (`pip install "pytrace-embedder[demangle]"`) or put
  `c++filt` on `PATH`. The SDK reports mangled names rather than failing.
- **A function's first instruction is attributed to nothing** — this is the
  Thumb-bit rule, and the SDK already handles it. If you are resolving
  addresses yourself, query at `address | 1`; querying the even address lands
  on the zero-size mapping symbol at every Thumb function entry.
