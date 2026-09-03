"""Thumb instruction boundaries.

STRACE reports execution counts per 2-byte slot, which makes a zero slot
ambiguous: it is either an instruction that never ran, or the trailing halfword
of a 32-bit Thumb-2 instruction, which never carries a count. Walking the real
instruction boundaries is what tells the two apart, and getting it wrong
inflates the denominator of every coverage percentage in the report.
"""

from __future__ import annotations

import array

from .constants import HALFWORD_BYTES, THUMB_ADDRESS_MASK, THUMB_BIT

_THUMB32_PREFIX_MASK = 0xF800
_THUMB32_PREFIX_MIN = 0xE800


def is_thumb32(halfword: int) -> bool:
    """Whether this halfword opens a 32-bit Thumb-2 instruction."""
    return (halfword & _THUMB32_PREFIX_MASK) >= _THUMB32_PREFIX_MIN


def instruction_starts(code: bytes, base_address: int) -> array.array:
    """Addresses at which an instruction begins, walking ``code`` linearly.

    Linear decode, not a basic-block walk: the region is known to be all code,
    and data embedded in a Thumb .text section (literal pools) would derail a
    control-flow walk just as badly while costing far more.
    """
    halfwords = len(code) // HALFWORD_BYTES
    starts = array.array("I")
    index = 0
    while index < halfwords:
        starts.append(base_address + index * HALFWORD_BYTES)
        offset = index * HALFWORD_BYTES
        halfword = code[offset] | (code[offset + 1] << 8)
        index += 2 if is_thumb32(halfword) else 1
    return starts


__all__ = [
    "HALFWORD_BYTES",
    "THUMB_ADDRESS_MASK",
    "THUMB_BIT",
    "instruction_starts",
    "is_thumb32",
]
