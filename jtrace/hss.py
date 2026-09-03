"""HSS -- High Speed Sampling.

The probe reads a set of target memory blocks on a timer and buffers the
samples, so you get a waveform of a variable without halting the core and
without any firmware support. It is the cheapest way to watch a value change in
real time: no instrumentation, no RTT, no trace port.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import HSS_FLAG_TIMESTAMP_US
from .errors import JLinkError
from .structs import HssMemBlockDesc

if TYPE_CHECKING:
    from .link import JLink


@dataclass(frozen=True)
class HssSample:
    """One sampling period: the bytes of every configured block, plus a time."""

    timestamp_us: int | None
    blocks: tuple[bytes, ...]

    def u32(self, block: int = 0, offset: int = 0) -> int:
        return int.from_bytes(self.blocks[block][offset : offset + 4], "little")

    def u16(self, block: int = 0, offset: int = 0) -> int:
        return int.from_bytes(self.blocks[block][offset : offset + 2], "little")

    def u8(self, block: int = 0, offset: int = 0) -> int:
        return self.blocks[block][offset]


class Hss:
    """High-speed sampling. Reached via ``JLink.hss``."""

    def __init__(self, link: "JLink") -> None:
        self._link = link
        self._lib = link.raw
        self._blocks: tuple[tuple[int, int], ...] = ()
        self._timestamped = False

    def capabilities(self) -> int:
        caps = ctypes.c_uint32(0)
        rc = self._lib.JLINK_HSS_GetCaps(ctypes.byref(caps))
        if rc < 0:
            raise JLinkError("Querying HSS capabilities failed", rc)
        return caps.value

    def start(
        self,
        blocks: Sequence[tuple[int, int]],
        *,
        period_us: int = 1000,
        timestamps: bool = True,
    ) -> None:
        """Begin sampling. ``blocks`` is a sequence of ``(address, num_bytes)``.

        The period is a request, not a guarantee: how fast the probe can
        actually service it depends on the interface speed and the total bytes
        per period.
        """
        if not blocks:
            raise JLinkError("HSS needs at least one memory block to sample")
        descriptors = (HssMemBlockDesc * len(blocks))()
        for i, (address, num_bytes) in enumerate(blocks):
            descriptors[i].Addr = address
            descriptors[i].NumBytes = num_bytes
            descriptors[i].Flags = 0
        flags = HSS_FLAG_TIMESTAMP_US if timestamps else 0
        rc = self._lib.JLINK_HSS_Start(
            ctypes.byref(descriptors), len(blocks), period_us, flags
        )
        if rc < 0:
            raise JLinkError("Starting high-speed sampling failed", rc)
        self._blocks = tuple(blocks)
        self._timestamped = timestamps

    def stop(self) -> None:
        try:
            self._lib.JLINK_HSS_Stop()
        except Exception:
            pass
        self._blocks = ()

    @property
    def sample_size(self) -> int:
        payload = sum(num_bytes for _, num_bytes in self._blocks)
        return payload + (4 if self._timestamped else 0)

    def read(self, max_samples: int = 256) -> list[HssSample]:
        """Drain buffered samples. Returns [] when nothing has been collected."""
        if not self._blocks:
            raise JLinkError("No HSS capture is running")
        stride = self.sample_size
        buffer = (ctypes.c_uint8 * (stride * max_samples))()
        count = self._lib.JLINK_HSS_Read(ctypes.byref(buffer), stride * max_samples)
        if count < 0:
            raise JLinkError("Reading high-speed samples failed", count)
        blob = bytes(buffer)
        samples: list[HssSample] = []
        # The DLL reports bytes, not samples; a partial trailing sample is
        # dropped rather than decoded from whatever follows it.
        for index in range(count // stride):
            base = index * stride
            offset = base
            timestamp = None
            if self._timestamped:
                timestamp = int.from_bytes(blob[offset : offset + 4], "little")
                offset += 4
            parts: list[bytes] = []
            for _, num_bytes in self._blocks:
                parts.append(blob[offset : offset + num_bytes])
                offset += num_bytes
            samples.append(HssSample(timestamp_us=timestamp, blocks=tuple(parts)))
        return samples

    def stream(
        self, *, duration_s: float, poll_interval_s: float = 0.01
    ) -> Iterator[HssSample]:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            batch = self.read()
            if batch:
                yield from batch
            else:
                time.sleep(poll_interval_s)


__all__ = ["Hss", "HssSample"]
