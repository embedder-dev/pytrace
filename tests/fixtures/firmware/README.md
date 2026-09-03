# Coverage oracle firmware

The firmware every hardware-shaped test in this suite resolves addresses
against. It targets an STM32F407 (Cortex-M4) — the SEGGER Cortex-M Trace
Reference Board — and it is deliberately peripheral-free: no RCC, GPIO or UART
access, so there are no device-address assumptions to get wrong.

It is an *oracle* because every function has a known expected outcome, so a
captured coverage report can be checked against ground truth rather than
against itself:

| Function            | Expectation                                  |
|---------------------|----------------------------------------------|
| `main`              | covered, runs once                           |
| `called_once`       | covered, run count exactly 1                 |
| `hot_loop_work`     | run count equal to `spin`'s                  |
| `partially_covered` | only the `x < 10` arm is ever taken          |
| `never_called_a`    | run count 0                                  |
| `never_called_b`    | run count 0                                  |

`coverage_demo.elf` is committed so the suite runs on a clean checkout with no
ARM toolchain installed. Rebuild it only if you change the source:

```bash
make                      # needs arm-none-eabi-gcc on PATH
make TOOLCHAIN=/opt/gcc-arm/bin/arm-none-eabi-
```

The build is pinned to `-Og -g3 -gdwarf-4 -fno-inline` on purpose. Inlining
would destroy the per-function run counts the oracle asserts, and the DWARF
version is what the line-table tests parse. A rebuild with a different
toolchain will not produce a byte-identical ELF, and the tests do not require
one — they assert relationships between counts, not absolute addresses.

`-ffile-prefix-map` rewrites the build and toolchain directories to `/src` and
`/toolchain`, so the committed ELF carries no absolute path from whoever built
it. Those paths are visible in every symbolized trace, so please keep the flags
if you rebuild.
