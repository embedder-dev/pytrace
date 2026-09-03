"""The Thumb instruction-boundary walk.

Wrong boundaries do not fail loudly -- they inflate the denominator of every
coverage percentage in the report, which looks like a target that simply ran
less code.
"""

from jtrace.thumb import instruction_starts, is_thumb32


def test_thumb32_prefix_range():
    # 0xE800..0xFFFF opens a 32-bit instruction; everything below is 16-bit.
    assert not is_thumb32(0xE7FF)
    assert is_thumb32(0xE800)
    assert is_thumb32(0xF000)
    assert is_thumb32(0xFFFF)
    assert not is_thumb32(0x0000)
    assert not is_thumb32(0x4770)  # bx lr


def test_all_16bit_instructions_each_take_one_slot():
    code = bytes([0x70, 0x47] * 4)  # bx lr, four times
    assert list(instruction_starts(code, 0x0800_0000)) == [
        0x0800_0000,
        0x0800_0002,
        0x0800_0004,
        0x0800_0006,
    ]


def test_32bit_instruction_consumes_its_trailing_halfword():
    # bl <offset> is 0xF000 0xF800: the second halfword must not become a start.
    code = bytes([0x00, 0xF0, 0x00, 0xF8, 0x70, 0x47])
    assert list(instruction_starts(code, 0x0800_0000)) == [0x0800_0000, 0x0800_0004]


def test_mixed_widths():
    code = bytes(
        [0x70, 0x47]  # 16-bit
        + [0x00, 0xF0, 0x00, 0xF8]  # 32-bit
        + [0x00, 0xBF]  # 16-bit nop
        + [0x2F, 0xE8, 0x00, 0x00]  # 32-bit (0xE82F)
    )
    assert list(instruction_starts(code, 0x1000)) == [0x1000, 0x1002, 0x1006, 0x1008]


def test_odd_trailing_byte_is_ignored():
    # A section whose size is not a whole number of halfwords must not read
    # past its end.
    assert list(instruction_starts(bytes([0x70, 0x47, 0x00]), 0)) == [0]


def test_empty_region():
    assert list(instruction_starts(b"", 0x0800_0000)) == []


def test_truncated_32bit_instruction_at_end_does_not_overrun():
    # The last halfword opens a 32-bit instruction whose second half is off the
    # end of the section. It still counts as one start, and the walk stops.
    code = bytes([0x70, 0x47, 0x00, 0xF0])
    assert list(instruction_starts(code, 0)) == [0, 2]
