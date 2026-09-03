"""POWERTRACE -- the probe's current-measurement channel.

Only available on probes that carry the hardware for it. The command values
are `observed` -- the DLL rejects anything above 6, so the set in
:class:`~jtrace.constants.PowerTraceCmd` is complete. The struct layouts follow
SEGGER's published shape and are the less certain part, which is why
:meth:`PowerTrace.control` takes a raw buffer.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import PowerTraceCmd
from .errors import JLinkError
from .structs import PowerTraceCaps, PowerTraceItem, PowerTraceSetup

if TYPE_CHECKING:
    from .link import JLink


@dataclass(frozen=True)
class PowerSample:
    timestamp: int
    channel: int
    value: int


class PowerTrace:
    """Power trace. Reached via ``JLink.power``."""

    def __init__(self, link: "JLink") -> None:
        self._link = link
        self._lib = link.raw

    def control(
        self, cmd: PowerTraceCmd | int, data_in: object = None, data_out: object = None
    ) -> int:
        """Raw ``JLINK_POWERTRACE_Control``.

        Public because the struct layouts here are the least certain in this
        SDK. If a probe rejects the typed helpers, drive it through here with
        a buffer you have shaped yourself.
        """
        in_ptr = ctypes.byref(data_in) if data_in is not None else None  # type: ignore[arg-type]
        out_ptr = ctypes.byref(data_out) if data_out is not None else None  # type: ignore[arg-type]
        rc = self._lib.JLINK_POWERTRACE_Control(int(cmd), in_ptr, out_ptr)
        if rc < 0:
            raise JLinkError(f"POWERTRACE_Control({int(cmd)}) failed", rc)
        return rc

    def capabilities(self) -> PowerTraceCaps:
        caps = PowerTraceCaps.new()
        self.control(PowerTraceCmd.GET_CAPS, None, caps)
        return caps

    def setup(self, channel_mask: int, sample_freq_hz: int, ref_select: int = 0) -> None:
        config = PowerTraceSetup.new()
        config.ChannelMask = channel_mask
        config.SampleFreq = sample_freq_hz
        config.RefSelect = ref_select
        self.control(PowerTraceCmd.SETUP, config)

    def start(self) -> None:
        self.control(PowerTraceCmd.START)

    def stop(self) -> None:
        try:
            self.control(PowerTraceCmd.STOP)
        except Exception:
            pass

    def flush(self) -> None:
        self.control(PowerTraceCmd.FLUSH)

    def num_items(self) -> int:
        value = ctypes.c_uint32(0)
        self.control(PowerTraceCmd.GET_NUM_ITEMS, None, value)
        return value.value

    def read(self, max_items: int | None = None) -> list[PowerSample]:
        wanted = self.num_items() if max_items is None else max_items
        if wanted <= 0:
            return []
        buffer = (PowerTraceItem * wanted)()
        count = self._lib.JLINK_POWERTRACE_Read(ctypes.byref(buffer), wanted)
        if count < 0:
            raise JLinkError("Reading power trace failed", count)
        return [
            PowerSample(
                timestamp=buffer[i].Timestamp,
                channel=buffer[i].Channel,
                value=buffer[i].Value,
            )
            for i in range(count)
        ]


__all__ = ["PowerSample", "PowerTrace"]
