# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- An unpacked SEGGER tarball directly under `$HOME` is now found without
  `JLINK_LIBRARY`. On ARM Linux that is the normal install — there is no
  package, and untar-and-run-in-place leaves nothing in `/opt` and nothing on
  `PATH`, so the override was effectively mandatory. Searched last and one level
  deep, so a packaged install still wins and `$HOME` is not walked.

- `store_dirname=` and `trace_format=` overrides on every function in
  `jtrace.artifacts` that resolves a path or writes a sidecar, so the output
  layout is no longer tied to one viewer's directory name. The default
  directory is now a neutral `.pytrace`; `store_dirname=".embedder"` writes
  where the Embedder trace and coverage viewer looks.
- The oracle firmware is committed under `tests/fixtures/firmware/`, with its
  source, linker script and Makefile, so the whole test suite runs on a clean
  checkout with no ARM toolchain and no probe.
- `TROUBLESHOOTING.md`, `CONTRIBUTING.md`, `LICENSE`, `NOTICE` and CI.
- A wordmark and README banner under `assets/`, set in Embedder's house faces
  with the type converted to outlines, so the SVG carries no font dependency.
  The README loads the PNG by relative path, the form that resolves while
  the repository is private.

### Changed

- `Strace.read_extended` builds its blocks after the slice loop rather than
  between halts. Reversing and copying each 65,536-entry window in the hot path
  inflated the *following* `halt()` round-trip, and the core runs for that
  excess, so it came out of the capture as lost instructions. Deferring the
  copy cuts the median halt several-fold and most of the loss with it, keeping
  the same instructions either way. Pre-existing rather than new -- the code
  before the block store copied the window in the same place -- and exposed by
  clock, since a slow core's clamp budget absorbs the extra halt and a fast
  core's does not. Reproducible via `bench/slice_latency.py`.

- The package version is single-sourced from `jtrace.__version__`. It stamps
  every capture's sidecar, so a copy that drifted would have mislabelled
  artifacts silently.
- The missing-firmware test fixture now raises instead of skipping. A skip
  quietly retired about a seventh of the suite, including every test of the
  hand-written ELF and DWARF parsers.
- Constants tagged `binary` are now tagged `observed`, with the provenance
  stated in terms of trust rather than method.

### Removed

- `docs/DLL_API_MAP.md`.
- The schema-validation test and its TypeScript harness, which resolved
  imports from outside the repository and could not run standalone.

## [0.0.1]

Initial release.

- Target control: reset, halt, go, step, vector catch, halt reason.
- Memory and register access, breakpoints and watchpoints, flash download.
- ETM instruction trace with unlimited-length capture, instruction statistics,
  and selective start/stop.
- Raw trace buffer, SWO/ITM with a packet decoder, RTT, high-speed sampling,
  power trace, CoreSight/ETM/ETB/CP15 register access.
- Dependency-free ELF and DWARF (2–5) parsing, symbolization, coverage rows and
  call-frame reconstruction.
- Block-segmented trace store with compressed snapshots and offline replay.
- `python -m jtrace` command line, with `--json` on every command.
