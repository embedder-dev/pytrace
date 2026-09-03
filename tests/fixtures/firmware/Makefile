# Coverage-oracle firmware for SEGGER Cortex-M Trace Reference Board (STM32F407, Cortex-M4)
#
# Built with -g and -fno-inline so the DWARF line table carries real
# File/Line/Function rows that can be checked against the known oracle.

TARGET   = coverage_demo
# Override if your toolchain is not on PATH, e.g. TOOLCHAIN=/opt/gcc-arm/bin/arm-none-eabi-
TOOLCHAIN ?= arm-none-eabi-
CC       = $(TOOLCHAIN)gcc
OBJCOPY  = $(TOOLCHAIN)objcopy
SIZE     = $(TOOLCHAIN)size

CPUFLAGS = -mcpu=cortex-m4 -mthumb
# -ffile-prefix-map keeps the builder's absolute paths out of the committed
# ELF's DWARF. Without it the fixture carries whichever home directory it was
# last built in, and that path is visible in every symbolized trace.
#
# The build directory maps to an absolute placeholder rather than to ".": DWARF
# 4 records the compilation directory only in .debug_info, and the line-table
# tests assert that resolved paths come out absolute, which is what proves that
# side of the parser ran. A relative comp_dir would silently retire that check.
TCROOT   := $(shell d=$$(command -v $(CC) 2>/dev/null) && cd "$$(dirname $$d)/.." && pwd)
PREFIXMAP = -ffile-prefix-map=$(CURDIR)=/src \
            $(if $(TCROOT),-ffile-prefix-map=$(TCROOT)=/toolchain)
CFLAGS   = $(CPUFLAGS) -Wall -Wextra -Og -g3 -gdwarf-4 -fno-inline \
           -ffreestanding -ffunction-sections -fdata-sections $(PREFIXMAP)
LDFLAGS  = $(CPUFLAGS) -T stm32f407.ld -nostdlib -Wl,--gc-sections -Wl,-Map=$(TARGET).map

SRCS = startup.c coverage_demo.c
OBJS = $(SRCS:.c=.o)

all: $(TARGET).bin $(TARGET).hex

$(TARGET).elf: $(OBJS) stm32f407.ld
	$(CC) $(LDFLAGS) $(OBJS) -o $@
	$(SIZE) $@

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

$(TARGET).bin: $(TARGET).elf
	$(OBJCOPY) -O binary $< $@

$(TARGET).hex: $(TARGET).elf
	$(OBJCOPY) -O ihex $< $@

clean:
	rm -f $(OBJS) $(TARGET).elf $(TARGET).bin $(TARGET).hex $(TARGET).map

.PHONY: all clean
