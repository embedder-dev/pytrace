"""RTT -- Real Time Transfer, the target's printf channel.

Channel assignment is a firmware convention, not something the transport
fixes: a target may well put log output on 0 and structured trace on another
buffer. Nothing here assumes a layout, which is why the buffer index is always
explicit rather than defaulted to 0.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import (
    RTT_AUTO_DETECT_CONTROL_BLOCK,
    RTT_DIRECTION_DOWN,
    RTT_DIRECTION_UP,
    RttCmd,
)
from .errors import JLinkError
from .structs import RttBufferDesc, RttStart, RttStatus

if TYPE_CHECKING:
    from .link import JLink


@dataclass(frozen=True)
class RttBuffer:
    index: int
    name: str
    size: int
    flags: int
    direction: str


@dataclass(frozen=True)
class RttState:
    bytes_transferred: int
    bytes_read: int
    host_overflow_count: int
    is_running: bool
    num_up_buffers: int
    num_down_buffers: int


class Rtt:
    """RTT control and I/O. Reached via ``JLink.rtt``."""

    def __init__(self, link: "JLink") -> None:
        self._link = link
        self._lib = link.raw

    def _control(self, cmd: RttCmd | int, data: object = None) -> int:
        pointer = ctypes.byref(data) if data is not None else None  # type: ignore[arg-type]
        rc = self._lib.JLINK_RTTERMINAL_Control(int(cmd), pointer)
        if rc < 0:
            raise JLinkError(f"RTT control command {int(cmd)} failed", rc)
        return rc

    def start(self, control_block_address: int = RTT_AUTO_DETECT_CONTROL_BLOCK) -> None:
        """Begin RTT. Address 0 makes the DLL search target RAM for the block.

        Auto-detection is a scan of target memory for the "SEGGER RTT"
        signature and it can take a moment, or miss entirely if the block sits
        outside the searched region. Pass the address from the ELF when you
        have it.
        """
        config = RttStart()
        config.ConfigBlockAddress = control_block_address
        self._control(RttCmd.START, config)

    def stop(self) -> None:
        try:
            self._control(RttCmd.STOP)
        except Exception:
            pass

    def wait_until_running(self, timeout_s: float = 5.0) -> bool:
        """Poll until the target has initialised its control block.

        RTT started before the firmware runs ``SEGGER_RTT_Init`` reports zero
        buffers rather than failing, so "started" is not "ready".
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = self.status()
            if state.is_running and state.num_up_buffers > 0:
                return True
            time.sleep(0.05)
        return False

    def status(self) -> RttState:
        stat = RttStatus()
        self._control(RttCmd.GETSTAT, stat)
        return RttState(
            bytes_transferred=stat.NumBytesTransferred,
            bytes_read=stat.NumBytesRead,
            host_overflow_count=stat.HostOverflowCount,
            is_running=bool(stat.IsRunning),
            num_up_buffers=stat.NumUpBuffers,
            num_down_buffers=stat.NumDownBuffers,
        )

    def num_buffers(self, direction: int = RTT_DIRECTION_UP) -> int:
        value = ctypes.c_uint32(direction)
        return self._control(RttCmd.GETNUMBUF, value)

    def describe(self, index: int, direction: int = RTT_DIRECTION_UP) -> RttBuffer:
        desc = RttBufferDesc()
        desc.BufferIndex = index
        desc.Direction = direction
        self._control(RttCmd.GETDESC, desc)
        return RttBuffer(
            index=desc.BufferIndex,
            name=desc.acName.decode(errors="replace"),
            size=desc.SizeOfBuffer,
            flags=desc.Flags,
            direction="up" if direction == RTT_DIRECTION_UP else "down",
        )

    def buffers(self) -> list[RttBuffer]:
        out: list[RttBuffer] = []
        for direction in (RTT_DIRECTION_UP, RTT_DIRECTION_DOWN):
            for index in range(self.num_buffers(direction)):
                try:
                    out.append(self.describe(index, direction))
                except JLinkError:
                    continue
        return out

    def read(self, index: int = 0, max_bytes: int = 4096) -> bytes:
        """Read whatever is waiting. Returns b"" when nothing is."""
        buffer = ctypes.create_string_buffer(max_bytes)
        count = self._lib.JLINK_RTTERMINAL_Read(index, buffer, max_bytes)
        if count < 0:
            raise JLinkError(f"RTT read on buffer {index} failed", count)
        return buffer.raw[:count]

    def write(self, data: bytes | str, index: int = 0) -> int:
        """Write to a down-buffer. Returns how many bytes the target took."""
        payload = data.encode() if isinstance(data, str) else data
        buffer = ctypes.create_string_buffer(payload, len(payload))
        count = self._lib.JLINK_RTTERMINAL_Write(index, buffer, len(payload))
        if count < 0:
            raise JLinkError(f"RTT write on buffer {index} failed", count)
        return count

    def stream(
        self,
        index: int = 0,
        *,
        duration_s: float | None = None,
        poll_interval_s: float = 0.01,
        chunk: int = 4096,
    ) -> Iterator[bytes]:
        """Yield RTT data as it arrives. Runs until ``duration_s`` elapses."""
        deadline = None if duration_s is None else time.monotonic() + duration_s
        while deadline is None or time.monotonic() < deadline:
            data = self.read(index, chunk)
            if data:
                yield data
            else:
                time.sleep(poll_interval_s)

    def read_text(
        self, index: int = 0, duration_s: float = 1.0, encoding: str = "utf-8"
    ) -> str:
        chunks = list(self.stream(index, duration_s=duration_s))
        return b"".join(chunks).decode(encoding, errors="replace")


__all__ = ["Rtt", "RttBuffer", "RttState"]
