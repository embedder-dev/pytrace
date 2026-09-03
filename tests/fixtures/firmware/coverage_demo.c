/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The pytrace authors.
 *
 * Coverage oracle firmware for the SEGGER Cortex-M Trace Reference Board
 * (STM32F407, Cortex-M4).
 *
 * Deliberately peripheral-free: no RCC, GPIO or UART access, so there are no
 * device-address assumptions to get wrong. Coverage only needs instructions to
 * execute, and every function below has a KNOWN expected outcome so a captured
 * report can be checked against ground truth:
 *
 *   main               - covered, runs once
 *   called_once        - covered, run count exactly 1
 *   hot_loop_work      - covered, runs every loop iteration (high run count)
 *   partially_covered  - covered, but ONLY the (x < 10) branch is ever taken
 *   spin               - covered, busy-wait between iterations
 *   never_called_a     - NOT covered (kept alive by a never-taken call site)
 *   never_called_b     - NOT covered (kept alive by a never-taken call site)
 */

#include <stdint.h>

static volatile uint32_t g_accumulator;
static volatile uint32_t g_loop_count;

static void spin(volatile uint32_t count)
{
    while (count--) {
        __asm__ volatile("nop");
    }
}

/* Expected: covered, run count == 1. */
void called_once(void)
{
    g_accumulator += 0x1000U;
}

/* Expected: covered, run count == number of loop iterations. */
void hot_loop_work(void)
{
    g_accumulator += g_loop_count;
    g_loop_count++;
}

/*
 * Expected: covered, but only the first branch. The (x >= 10) arm below must
 * show as uncovered source lines.
 */
uint32_t partially_covered(uint32_t x)
{
    if (x < 10U) {
        g_accumulator += x;
        return x * 2U;
    }

    g_accumulator -= x;
    if (x > 1000U) {
        g_accumulator ^= 0xFFFFU;
        return x / 2U;
    }
    return x + 1U;
}

/* Expected: NOT covered. */
void never_called_a(void)
{
    g_accumulator = 0xDEADU;
    g_loop_count = 0xBEEFU;
}

/* Expected: NOT covered. */
uint32_t never_called_b(uint32_t seed)
{
    uint32_t acc = seed;
    for (uint32_t i = 0; i < 32U; i++) {
        acc = (acc << 1) ^ (acc >> 31);
    }
    return acc;
}

typedef void (*void_fn)(void);
typedef uint32_t (*u32_fn)(uint32_t);

volatile void_fn g_keep_a = never_called_a;
volatile u32_fn g_keep_b = never_called_b;

/*
 * Never true: a function pointer can never be 1. Forces the linker to keep both
 * functions and leaves two call sites that must show up as uncovered.
 */
static void unreachable_calls(void)
{
    if ((uintptr_t)g_keep_a == 1U) {
        g_keep_a();
    }
    if ((uintptr_t)g_keep_b == 1U) {
        g_accumulator = g_keep_b(g_loop_count);
    }
}

int main(void)
{
    called_once();
    unreachable_calls();

    for (;;) {
        hot_loop_work();
        (void)partially_covered(g_loop_count & 7U);
        /*
         * Deliberately short. The ETM trace buffer holds 65,536 instructions,
         * and spin(20000) is roughly 120,000 of them -- one call would fill the
         * whole window, so a captured timeline would be a single flat bar with
         * no call boundary anywhere in it. At 200 the window spans dozens of
         * loop iterations and the call structure is visible.
         */
        spin(200);
    }
}
