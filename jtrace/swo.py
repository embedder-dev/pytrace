"""SWO / ITM -- the single-pin trace output.

Not the same thing as ETM instruction trace, and worth being clear about which
you want. SWO carries what the firmware *chose* to emit (ITM stimulus writes)
plus PC samples the DWT unit takes periodically. ETM carries every instruction
the core actually executed. SWO needs one pin and works on a plain J-Link; ETM
needs a four-pin trace port and a J-Trace.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import SwoCmd, SwoInterface
from .errors import TraceError

if TYPE_CHECKING:
    from .link import JLink

_ALL_ITM_PORTS = 0xFFFFFFFF


@dataclass(frozen=True)
class SwoPacket:
    """One decoded ITM packet."""

    port: int
    data: bytes

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


class Swo:
    """SWO capture. Reached via ``JLink.swo``."""

    def __init__(self, link: "JLink") -> None:
        self._link = link
        self._lib = link.raw

    def _control(self, cmd: SwoCmd | int, data: object = None) -> int:
        pointer = ctypes.byref(data) if data is not None else None  # type: ignore[arg-type]
        rc = self._lib.JLINK_SWO_Control(int(cmd), pointer)
        if rc < 0:
            raise TraceError(f"SWO_Control({int(cmd)}) failed", rc)
        return rc

    def config(self, config: str) -> None:
        """Raw config string, e.g. ``Type=UART;SWOSpeed=2000000``."""
        rc = self._lib.JLINKARM_SWO_Config(config.encode() + b"\0")
        if rc < 0:
            raise TraceError(f"SWO configuration ({config}) failed", rc)

    def start(
        self,
        *,
        cpu_speed_hz: int,
        swo_speed_hz: int = 0,
        port_mask: int = _ALL_ITM_PORTS,
        mode: SwoInterface = SwoInterface.UART,
    ) -> None:
        """Enable SWO on the target and start capturing on the host.

        ``swo_speed_hz=0`` asks for the fastest rate both ends agree on, which
        is what :meth:`compatible_speeds` enumerates.
        """
        speed = swo_speed_hz or self._best_speed(cpu_speed_hz)

        class _Setup(ctypes.Structure):
            _fields_ = [
                ("Interface", ctypes.c_uint32),
                ("Speed", ctypes.c_uint32),
            ]

        setup = _Setup(Interface=int(mode), Speed=speed)
        self._control(SwoCmd.START, setup)
        rc = self._lib.JLINKARM_SWO_EnableTarget(
            cpu_speed_hz, speed, port_mask, int(mode)
        )
        if rc < 0:
            raise TraceError("Enabling SWO on the target failed", rc)

    def stop(self, port_mask: int = _ALL_ITM_PORTS) -> None:
        try:
            self._lib.JLINKARM_SWO_DisableTarget(port_mask)
        except Exception:
            pass
        try:
            self._control(SwoCmd.STOP)
        except Exception:
            pass

    def flush(self, num_bytes: int | None = None) -> None:
        """Drop buffered bytes. ``None`` drops everything pending."""
        value = ctypes.c_uint32(
            self.pending_bytes() if num_bytes is None else num_bytes
        )
        self._control(SwoCmd.FLUSH, value)

    def pending_bytes(self) -> int:
        value = ctypes.c_uint32(0)
        self._control(SwoCmd.GET_NUM_BYTES, value)
        return value.value

    def set_host_buffer_size(self, num_bytes: int) -> None:
        value = ctypes.c_uint32(num_bytes)
        self._control(SwoCmd.SET_BUFFERSIZE_HOST, value)

    def set_probe_buffer_size(self, num_bytes: int) -> None:
        value = ctypes.c_uint32(num_bytes)
        self._control(SwoCmd.SET_BUFFERSIZE_EMU, value)

    def compatible_speeds(
        self, cpu_speed_hz: int, max_swo_speed_hz: int = 30_000_000, count: int = 16
    ) -> list[int]:
        """SWO rates that divide cleanly from this CPU clock, fastest first."""
        buffer = (ctypes.c_uint32 * count)()
        rc = self._lib.JLINKARM_SWO_GetCompatibleSpeeds(
            cpu_speed_hz,
            max_swo_speed_hz,
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)),
            count,
        )
        if rc < 0:
            raise TraceError("Querying compatible SWO speeds failed", rc)
        return [value for value in buffer if value]

    def _best_speed(self, cpu_speed_hz: int) -> int:
        speeds = self.compatible_speeds(cpu_speed_hz)
        if not speeds:
            raise TraceError(
                f"No SWO speed is compatible with a {cpu_speed_hz} Hz core clock"
            )
        return speeds[0]

    def read(self, max_bytes: int = 4096) -> bytes:
        """Read raw SWO bytes -- still TPIU-framed, not yet ITM packets."""
        buffer = (ctypes.c_uint8 * max_bytes)()
        count = ctypes.c_uint32(max_bytes)
        rc = self._lib.JLINK_SWO_Read(
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8)),
            0,
            ctypes.byref(count),
        )
        if rc < 0:
            raise TraceError("SWO read failed", rc)
        data = bytes(buffer)[: count.value]
        if data:
            self.flush(len(data))
        return data

    def read_stimulus(self, port: int, max_bytes: int = 4096) -> bytes:
        """Read one ITM stimulus port, already demultiplexed by the DLL."""
        buffer = (ctypes.c_uint8 * max_bytes)()
        count = self._lib.JLINKARM_SWO_ReadStimulus(
            port, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8)), max_bytes
        )
        if count < 0:
            raise TraceError(f"Reading ITM port {port} failed", count)
        return bytes(buffer)[:count]

    def stream(
        self,
        *,
        duration_s: float,
        poll_interval_s: float = 0.01,
        chunk: int = 4096,
    ) -> Iterator[bytes]:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            data = self.read(chunk)
            if data:
                yield data
            else:
                time.sleep(poll_interval_s)


def decode_itm(data: bytes) -> list[SwoPacket]:
    """Decode ITM stimulus packets out of a raw SWO byte stream.

    Handles the source packets (stimulus writes) and skips protocol packets
    (synchronisation, overflow, timestamps, DWT) rather than trying to
    interpret them -- what almost every caller wants from SWO is the text the
    firmware printed.

    Unrecognised bytes are skipped one at a time so a corrupt stretch costs one
    packet rather than resynchronising the whole rest of the buffer.
    """
    packets: list[SwoPacket] = []
    i = 0
    n = len(data)
    while i < n:
        header = data[i]
        if header == 0x00:
            # Synchronisation: a run of zero bits ended by 0x80.
            i += 1
            while i < n and data[i] == 0x00:
                i += 1
            if i < n and data[i] == 0x80:
                i += 1
            continue
        size_bits = header & 0x03
        if size_bits == 0:
            # Protocol packet. The continuation bit chains extension bytes.
            i += 1
            if header & 0x80:
                while i < n and data[i] & 0x80:
                    i += 1
                if i < n:
                    i += 1
            continue
        payload_len = 1 if size_bits == 1 else 2 if size_bits == 2 else 4
        if i + 1 + payload_len > n:
            break
        port = header >> 3
        is_source = (header & 0x04) == 0
        if is_source:
            packets.append(SwoPacket(port=port, data=data[i + 1 : i + 1 + payload_len]))
        i += 1 + payload_len
    return packets


__all__ = ["Swo", "SwoPacket", "decode_itm"]
