/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The pytrace authors.
 *
 * Minimal Cortex-M4 startup for STM32F407 (SEGGER Cortex-M Trace Reference Board).
 * Provides the initial vector table, reset handler, and .data/.bss init.
 */

#include <stdint.h>

extern uint32_t _sidata;   /* start of .data init values in flash */
extern uint32_t _sdata;    /* start of .data in RAM */
extern uint32_t _edata;    /* end of .data in RAM */
extern uint32_t _sbss;     /* start of .bss in RAM */
extern uint32_t _ebss;     /* end of .bss in RAM */
extern uint32_t _estack;   /* top of stack (from linker) */

int main(void);

void Reset_Handler(void)
{
    /* Copy .data from flash to RAM */
    uint32_t *src = &_sidata;
    uint32_t *dst = &_sdata;
    while (dst < &_edata) {
        *dst++ = *src++;
    }

    /* Zero .bss */
    dst = &_sbss;
    while (dst < &_ebss) {
        *dst++ = 0U;
    }

    main();

    for (;;) {
    }
}

void Default_Handler(void)
{
    for (;;) {
    }
}

/* Weak aliases: all system handlers fall back to Default_Handler */
void NMI_Handler(void)        __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void)  __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void)  __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void)   __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void)        __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void)   __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void)     __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void)    __attribute__((weak, alias("Default_Handler")));

/* Initial vector table: only the core exceptions are populated. */
__attribute__((section(".isr_vector"), used))
void (*const g_vectors[])(void) = {
    (void (*)(void))(&_estack), /* 0x00 Initial stack pointer      */
    Reset_Handler,             /* 0x04 Reset                      */
    NMI_Handler,               /* 0x08 NMI                        */
    HardFault_Handler,         /* 0x0C HardFault                  */
    MemManage_Handler,         /* 0x10 MemManage                  */
    BusFault_Handler,          /* 0x14 BusFault                   */
    UsageFault_Handler,        /* 0x18 UsageFault                 */
    0, 0, 0, 0,                /* 0x1C-0x28 Reserved              */
    SVC_Handler,               /* 0x2C SVCall                     */
    DebugMon_Handler,          /* 0x30 Debug Monitor              */
    0,                         /* 0x34 Reserved                   */
    PendSV_Handler,            /* 0x38 PendSV                     */
    SysTick_Handler,           /* 0x3C SysTick                    */
    /* IRQ handlers omitted: not needed for polled blinky */
};
