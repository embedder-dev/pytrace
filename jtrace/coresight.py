"""CoreSight, ETM and ETB register access.

The level below every other trace module: DP/AP transactions, and direct
register access to the trace macrocell and on-chip trace buffer. Reach for this
when a target needs a bring-up sequence the DLL does not perform for you -- the
STM32C5 family, for instance, needs its DBGMCU trace bits set before TRACECLK
ever appears.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import CORESIGHT_AP, CORESIGHT_DP, ApDpReg
from .errors import JLinkError

if TYPE_CHECKING:
    from .link import JLink


# ETM v3 register indices, as JLINKARM_ETM_ReadReg numbers them.
class EtmReg:
    CONTROL = 0x00
    CONFIG_CODE = 0x01
    TRIGGER_EVENT = 0x02
    STATUS = 0x04
    SYSTEM_CONFIG = 0x05
    TRACE_ENABLE_EVENT = 0x08
    TRACE_ENABLE_CTRL1 = 0x09
    FIFOFULL_LEVEL = 0x0B
    ID = 0x79


class EtbReg:
    RAM_DEPTH = 0x01
    STATUS = 0x03
    RAM_READ_DATA = 0x04
    RAM_READ_POINTER = 0x05
    RAM_WRITE_POINTER = 0x06
    TRIGGER_COUNTER = 0x07
    CONTROL = 0x08


@dataclass(frozen=True)
class ApDpAccess:
    reg_index: int
    ap: bool
    value: int


class CoreSight:
    """CoreSight/ETM/ETB access. Reached via ``JLink.coresight``."""

    def __init__(self, link: "JLink") -> None:
        self._link = link
        self._lib = link.raw

    # -- DAP ---------------------------------------------------------------

    def configure(self, config: str) -> None:
        """Configure the DAP, e.g. ``IRPre=0;DRPre=0;IRPost=0;DRPost=0;IRLenDevice=4``.

        Also where you set ``AP`` indices on a target with several access
        ports. An empty string asks the DLL to auto-detect.
        """
        rc = self._lib.JLINKARM_CORESIGHT_Configure(config.encode() + b"\0")
        if rc < 0:
            raise JLinkError(f"CoreSight configuration ({config}) failed", rc)

    def read_dp(self, reg_index: int | ApDpReg) -> int:
        return self._read(int(reg_index), CORESIGHT_DP)

    def write_dp(self, reg_index: int | ApDpReg, value: int) -> None:
        self._write(int(reg_index), CORESIGHT_DP, value)

    def read_ap(self, reg_index: int | ApDpReg) -> int:
        return self._read(int(reg_index), CORESIGHT_AP)

    def write_ap(self, reg_index: int | ApDpReg, value: int) -> None:
        self._write(int(reg_index), CORESIGHT_AP, value)

    def _read(self, reg_index: int, ap: int) -> int:
        value = ctypes.c_uint32(0)
        rc = self._lib.JLINKARM_CORESIGHT_ReadAPDPReg(
            reg_index, ap, ctypes.byref(value)
        )
        if rc < 0:
            kind = "AP" if ap else "DP"
            raise JLinkError(f"Reading CoreSight {kind} register {reg_index} failed", rc)
        return value.value

    def _write(self, reg_index: int, ap: int, value: int) -> None:
        rc = self._lib.JLINKARM_CORESIGHT_WriteAPDPReg(reg_index, ap, value)
        if rc < 0:
            kind = "AP" if ap else "DP"
            raise JLinkError(f"Writing CoreSight {kind} register {reg_index} failed", rc)

    # -- ETM ---------------------------------------------------------------

    @property
    def etm_present(self) -> bool:
        return bool(self._lib.JLINKARM_ETM_IsPresent())

    def etm_read(self, reg_index: int) -> int:
        return int(self._lib.JLINKARM_ETM_ReadReg(reg_index))

    def etm_write(self, reg_index: int, value: int, allow_delay: bool = False) -> None:
        rc = self._lib.JLINKARM_ETM_WriteReg(reg_index, value, int(allow_delay))
        if rc < 0:
            raise JLinkError(f"Writing ETM register 0x{reg_index:02x} failed", rc)

    def etm_start(self) -> None:
        self._lib.JLINKARM_ETM_StartTrace()

    def etm_id(self) -> int:
        return self.etm_read(EtmReg.ID)

    # -- ETB ---------------------------------------------------------------

    @property
    def etb_present(self) -> bool:
        return bool(self._lib.JLINKARM_ETB_IsPresent())

    def etb_read(self, reg_index: int) -> int:
        return int(self._lib.JLINKARM_ETB_ReadReg(reg_index))

    def etb_write(self, reg_index: int, value: int, allow_delay: bool = False) -> None:
        rc = self._lib.JLINKARM_ETB_WriteReg(reg_index, value, int(allow_delay))
        if rc < 0:
            raise JLinkError(f"Writing ETB register 0x{reg_index:02x} failed", rc)

    def etb_depth(self) -> int:
        """ETB RAM depth in 32-bit words -- how much on-chip trace it holds."""
        return self.etb_read(EtbReg.RAM_DEPTH)

    # -- CP15 (Cortex-A/R) -------------------------------------------------

    @property
    def cp15_present(self) -> bool:
        return bool(self._lib.JLINKARM_CP15_IsPresent())

    def cp15_read(self, crn: int, op1: int, crm: int, op2: int) -> int:
        value = ctypes.c_uint32(0)
        rc = self._lib.JLINKARM_CP15_ReadEx(crn, crm, op1, op2, ctypes.byref(value))
        if rc < 0:
            raise JLinkError(f"Reading CP15 c{crn},c{crm},{op1},{op2} failed", rc)
        return value.value

    def cp15_write(self, crn: int, op1: int, crm: int, op2: int, value: int) -> None:
        rc = self._lib.JLINKARM_CP15_WriteEx(crn, crm, op1, op2, value)
        if rc < 0:
            raise JLinkError(f"Writing CP15 c{crn},c{crm},{op1},{op2} failed", rc)


__all__ = ["ApDpAccess", "CoreSight", "EtbReg", "EtmReg"]
