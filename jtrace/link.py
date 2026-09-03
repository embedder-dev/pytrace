"""The J-Link connection: everything that is not a trace subsystem.

The DLL keeps one open probe per process, so :class:`JLink` is a handle to that
global rather than an independent object. Opening a second one while the first
is open raises instead of silently stealing the probe, because the failure mode
of not doing that -- two halves of a program each believing they own the target
-- is very hard to see from the outside.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    ACCESS_DIR_READ,
    ACCESS_DIR_WRITE,
    DEFAULT_SPEED_KHZ,
    EXEC_ERROR_BUF_SIZE,
    HOST_IF_ALL,
    INTERFACE_BY_NAME,
    AccessSize,
    BreakpointType,
    DataEventType,
    Interface,
    ResetType,
)
from .errors import JLinkError, NotConnectedError
from .loader import RawLibrary, load
from .structs import (
    BreakpointInfo,
    DataEvent,
    EmuConnectInfo,
    HwStatus,
    MoeInfo,
    SpeedInfo,
    WatchpointInfo,
)

_MAX_PROBES = 32


def _c_str(value: str) -> bytes:
    return value.encode("utf-8") + b"\0"


def _register_aliases(name: str) -> set[str]:
    """Every lowercase spelling of one DLL register name.

    ``"R15 (PC)"`` yields ``{"r15 (pc)", "r15", "pc"}``, which is what lets a
    caller write ``read_register("PC")``.
    """
    lowered = name.lower()
    aliases = {lowered}
    head, _, tail = lowered.partition("(")
    if tail:
        aliases.add(head.strip())
        aliases.add(tail.rstrip(")").strip())
    return {alias for alias in aliases if alias}


@dataclass(frozen=True)
class ProbeInfo:
    """A J-Link visible to the host, before anything is opened.

    Enumeration is deliberately cheap, and the DLL fills in correspondingly
    little: for a USB probe, ``firmware``, ``nickname`` and ``hardware_version``
    come back empty or zero. They are only known once the probe is open -- see
    :meth:`JLink.firmware_string`.
    """

    serial_number: int
    product: str
    nickname: str
    firmware: str
    hardware_version: int
    connection: str

    @property
    def is_trace_capable(self) -> bool:
        """Whether the product name says J-Trace.

        A name check, not a capability query: capabilities need an open probe,
        and the point of this type is to choose one before opening anything.
        Use :meth:`JLink.has_capability` once connected.
        """
        return "trace" in self.product.lower()


@dataclass(frozen=True)
class Register:
    index: int
    name: str


class JLink:
    """An open connection to a J-Link or J-Trace.

    Use it as a context manager. The destructor is not a substitute: leaving
    the probe open wedges every later flash, debug session and RTT connection
    on the machine until the process exits.

        with JLink(device="STM32C071RB") as jl:
            jl.reset()
            jl.halt()
            print(hex(jl.read_u32(0x08000000)))
    """

    _open_instance: "JLink | None" = None

    def __init__(
        self,
        device: str | None = None,
        interface: str | Interface = Interface.SWD,
        speed_khz: int = DEFAULT_SPEED_KHZ,
        serial_number: int | None = None,
        log_path: str | Path | None = None,
        library: str | Path | None = None,
        connect: bool = True,
        suppress_dialogs: bool = True,
    ) -> None:
        self._lib: RawLibrary = load(library)
        self._opened = False
        self._connected = False
        self._device = device
        self._register_cache: list[Register] | None = None

        if JLink._open_instance is not None:
            raise JLinkError(
                "A J-Link is already open in this process. The DLL holds one "
                "probe globally; close the existing JLink before opening another."
            )

        if log_path is not None:
            self._lib.JLINK_SetLogFile(_c_str(str(log_path)))

        if serial_number is not None:
            rc = self._lib.JLINKARM_EMU_SelectByUSBSN(serial_number)
            if rc < 0:
                raise JLinkError(f"No J-Link with serial number {serial_number}", rc)

        error = self._lib.JLINKARM_OpenEx(None, None)
        if error:
            raise JLinkError(f"J-Link open failed: {error.decode(errors='replace')}")
        self._opened = True
        JLink._open_instance = self

        try:
            if suppress_dialogs:
                self.suppress_dialogs()
            if device is not None and connect:
                self.connect(device, interface, speed_khz)
        except Exception:
            self.close()
            raise

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "JLink":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Hand the probe back. Safe to call twice."""
        if not self._opened:
            return
        try:
            self._lib.JLINKARM_Close()
        except Exception:
            pass
        self._opened = False
        self._connected = False
        if JLink._open_instance is self:
            JLink._open_instance = None

    @property
    def raw(self) -> RawLibrary:
        """The bound DLL, for calls this wrapper does not model.

        Deliberately public: the surface is 613 exports and this SDK models the
        ones with a sane shape. Reaching past it is expected, not a smell.
        """
        return self._lib

    @property
    def is_open(self) -> bool:
        return self._opened and bool(self._lib.JLINKARM_IsOpen())

    @property
    def is_connected(self) -> bool:
        return bool(self._lib.JLINKARM_IsConnected())

    def _require_open(self) -> None:
        if not self._opened:
            raise NotConnectedError("The J-Link is closed")

    # -- probe discovery ---------------------------------------------------

    @staticmethod
    def list_probes(library: str | Path | None = None) -> list[ProbeInfo]:
        """Enumerate attached probes without opening any of them."""
        lib = load(library)
        buffer = (EmuConnectInfo * _MAX_PROBES)()
        count = lib.JLINKARM_EMU_GetList(HOST_IF_ALL, ctypes.byref(buffer), _MAX_PROBES)
        if count < 0:
            raise JLinkError("Enumerating J-Link probes failed", count)
        probes: list[ProbeInfo] = []
        for i in range(min(count, _MAX_PROBES)):
            entry = buffer[i]
            probes.append(
                ProbeInfo(
                    serial_number=entry.SerialNumber,
                    product=entry.acProduct.decode(errors="replace"),
                    nickname=entry.acNickName.decode(errors="replace"),
                    firmware=entry.acFWString.decode(errors="replace"),
                    hardware_version=entry.HWVersion,
                    connection="ip" if entry.Connection == 2 else "usb",
                )
            )
        return probes

    # -- commands ----------------------------------------------------------

    def exec_command(self, command: str) -> str | None:
        """Run a J-Link command string. Returns the DLL's complaint, or None.

        This is the widest part of the API. ``Device=``, ``SetResetType``,
        ``ReadIntoTraceCache``, ``SetTraceFile`` and roughly a hundred others
        are reachable only here -- see SEGGER's J-Link command strings
        reference for the full list.
        """
        self._require_open()
        buffer = ctypes.create_string_buffer(EXEC_ERROR_BUF_SIZE)
        self._lib.JLINKARM_ExecCommand(_c_str(command), buffer, EXEC_ERROR_BUF_SIZE)
        text = buffer.value.decode(errors="replace").strip()
        return text or None

    def suppress_dialogs(self) -> None:
        """Stop the DLL opening native modal windows.

        Several conditions -- an unknown device name, a firmware-update prompt,
        the control panel -- are answered by the DLL with a window. Driving FFI
        from a script there is nobody to click them: the call blocks until a
        human finds the window, and headless it blocks forever. Failures are
        ignored because an older DLL may not know a command, and failing to
        suppress a dialog must not fail the run.
        """
        for command in (
            "SetBatchMode = 1",
            "HideDeviceSelection = 1",
            "SuppressControlPanel",
            "SilentUpdateFW",
            "SuppressInfoUpdateFW",
        ):
            try:
                self.exec_command(command)
            except Exception:
                pass

    # -- device / interface ------------------------------------------------

    def select_device(self, device: str) -> None:
        error = self.exec_command(f"Device = {device}")
        if error:
            raise JLinkError(f"J-Link device selection failed: {error}")
        self._device = device

    def select_interface(self, interface: str | Interface) -> None:
        value = (
            INTERFACE_BY_NAME[interface.upper()]
            if isinstance(interface, str)
            else interface
        )
        self._lib.JLINKARM_TIF_Select(int(value))

    @property
    def speed_khz(self) -> int:
        return int(self._lib.JLINKARM_GetSpeed())

    @speed_khz.setter
    def speed_khz(self, value: int) -> None:
        self._lib.JLINKARM_SetSpeed(value)

    def set_max_speed(self) -> None:
        self._lib.JLINKARM_SetMaxSpeed()

    def speed_info(self) -> SpeedInfo:
        info = SpeedInfo.new()
        self._lib.JLINKARM_GetSpeedInfo(ctypes.byref(info))
        return info

    def connect(
        self,
        device: str | None = None,
        interface: str | Interface = Interface.SWD,
        speed_khz: int = DEFAULT_SPEED_KHZ,
    ) -> None:
        """Select the device, interface and speed, then attach to the core."""
        self._require_open()
        target = device or self._device
        if target is None:
            raise JLinkError("connect() needs a device name")
        self.select_device(target)
        self.select_interface(interface)
        self.speed_khz = speed_khz
        rc = self._lib.JLINKARM_Connect()
        if rc < 0:
            raise JLinkError(f"J-Link connect to {target} failed", rc)
        self._connected = True

    def available_interfaces(self) -> list[Interface]:
        mask = ctypes.c_uint32(0)
        self._lib.JLINKARM_TIF_GetAvailable(ctypes.byref(mask))
        return [iface for iface in Interface if mask.value & (1 << int(iface))]

    def core_id(self) -> int:
        return int(self._lib.JLINKARM_CORE_GetFound())

    def core_name(self) -> str:
        buffer = ctypes.create_string_buffer(128)
        self._lib.JLINKARM_Core2CoreName(self.core_id(), buffer, 128)
        return buffer.value.decode(errors="replace")

    def measure_cpu_speed_hz(
        self, ram_addr: int, preserve_memory: bool = True
    ) -> int:
        """Time a loop the DLL downloads into RAM. Needs the core halted."""
        rc = self._lib.JLINKARM_MeasureCPUSpeedEx(ram_addr, int(preserve_memory), 1)
        if rc < 0:
            raise JLinkError("Measuring CPU speed failed", rc)
        return rc

    # -- probe info --------------------------------------------------------

    def dll_version(self) -> str:
        raw = int(self._lib.JLINKARM_GetDLLVersion())
        return f"{raw // 10000}.{(raw // 100) % 100}{chr(ord('a') + raw % 100 - 1) if raw % 100 else ''}"

    def serial_number(self) -> int:
        return int(self._lib.JLINKARM_GetSN())

    def firmware_string(self) -> str:
        buffer = ctypes.create_string_buffer(256)
        self._lib.JLINKARM_GetFirmwareString(buffer, 256)
        return buffer.value.decode(errors="replace")

    def hardware_version(self) -> str:
        raw = int(self._lib.JLINKARM_GetHardwareVersion())
        return f"{raw // 10000}.{(raw // 100) % 100}"

    def features(self) -> list[str]:
        buffer = ctypes.create_string_buffer(512)
        self._lib.JLINKARM_GetFeatureString(buffer)
        return [f for f in buffer.value.decode(errors="replace").split(",") if f]

    def capabilities(self) -> int:
        return int(self._lib.JLINKARM_GetEmuCaps())

    def has_capability(self, bit: int) -> bool:
        return bool(self._lib.JLINKARM_EMU_HasCapEx(bit))

    def hardware_status(self) -> HwStatus:
        status = HwStatus()
        rc = self._lib.JLINKARM_GetHWStatus(ctypes.byref(status))
        if rc < 0:
            raise JLinkError("Reading hardware status failed", rc)
        return status

    def target_voltage_mv(self) -> int:
        return int(self.hardware_status().VTarget)

    # -- run control -------------------------------------------------------

    def set_reset_type(self, reset_type: ResetType | int) -> None:
        self._lib.JLINKARM_SetResetType(int(reset_type))

    def set_reset_delay_ms(self, delay: int) -> None:
        self._lib.JLINKARM_SetResetDelay(delay)

    def reset(self, halt: bool = True) -> None:
        """Reset the target. ``halt=False`` lets it run straight out of reset."""
        if halt:
            self._lib.JLINKARM_Reset()
        else:
            self._lib.JLINKARM_ResetNoHalt()

    def halt(self) -> None:
        rc = self._lib.JLINKARM_Halt()
        if rc < 0:
            raise JLinkError("J-Link halt failed", rc)

    def go(self) -> None:
        self._lib.JLINKARM_Go()

    def go_interrupts_disabled(self) -> None:
        self._lib.JLINKARM_GoIntDis()

    def step(self) -> None:
        rc = self._lib.JLINKARM_Step()
        if rc < 0:
            raise JLinkError("Single step failed", rc)

    @property
    def is_halted(self) -> bool:
        rc = int(self._lib.JLINKARM_IsHalted())
        if rc < 0:
            raise JLinkError("Reading halt state failed", rc)
        return bool(rc)

    def wait_for_halt(self, timeout_ms: int = 1000) -> bool:
        """Block until the core halts. Returns False on timeout."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.is_halted:
                return True
            time.sleep(0.005)
        return False

    def halt_reason(self) -> list[MoeInfo]:
        buffer = (MoeInfo * 8)()
        count = self._lib.JLINKARM_GetMOEs(ctypes.byref(buffer), 8)
        return [buffer[i] for i in range(max(0, count))]

    @contextmanager
    def halted(self) -> Iterator["JLink"]:
        """Halt for the body, then resume only if we were the one who halted."""
        was_running = not self.is_halted
        if was_running:
            self.halt()
        try:
            yield self
        finally:
            if was_running:
                self.go()

    def set_vector_catch(self, mask: int) -> None:
        rc = self._lib.JLINKARM_WriteVectorCatch(mask)
        if rc < 0:
            raise JLinkError("Setting vector catch failed", rc)

    # -- memory ------------------------------------------------------------

    def read_memory(self, address: int, num_bytes: int, zone: str | None = None) -> bytes:
        """Read raw bytes. ``zone`` selects a memory zone on cores that have them."""
        buffer = (ctypes.c_uint8 * num_bytes)()
        if zone is not None:
            rc = self._lib.JLINK_ReadMemZonedEx(
                address, num_bytes, ctypes.byref(buffer), 0, _c_str(zone)
            )
        else:
            rc = self._lib.JLINKARM_ReadMemEx(
                address, num_bytes, ctypes.byref(buffer), 0
            )
        if rc < 0:
            raise JLinkError(f"Memory read failed at 0x{address:08x}", rc)
        return bytes(buffer)

    def write_memory(self, address: int, data: bytes, zone: str | None = None) -> None:
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        if zone is not None:
            rc = self._lib.JLINK_WriteMemZonedEx(
                address, len(data), ctypes.byref(buffer), 0, _c_str(zone)
            )
        else:
            rc = self._lib.JLINKARM_WriteMem(
                address, len(data), ctypes.byref(buffer)
            )
        if rc < 0:
            raise JLinkError(f"Memory write failed at 0x{address:08x}", rc)

    def read_u8(self, address: int) -> int:
        return self.read_memory(address, 1)[0]

    def read_u16(self, address: int) -> int:
        return int.from_bytes(self.read_memory(address, 2), "little")

    def read_u32(self, address: int) -> int:
        return int.from_bytes(self.read_memory(address, 4), "little")

    def read_u64(self, address: int) -> int:
        return int.from_bytes(self.read_memory(address, 8), "little")

    def read_u32_array(self, address: int, count: int) -> list[int]:
        buffer = (ctypes.c_uint32 * count)()
        status = (ctypes.c_uint8 * count)()
        rc = self._lib.JLINKARM_ReadMemU32(
            address,
            count,
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)),
            ctypes.cast(status, ctypes.POINTER(ctypes.c_uint8)),
        )
        if rc < 0:
            raise JLinkError(f"Word read failed at 0x{address:08x}", rc)
        return list(buffer[:rc])

    def write_u8(self, address: int, value: int) -> None:
        self._check(self._lib.JLINKARM_WriteU8(address, value), "byte write")

    def write_u16(self, address: int, value: int) -> None:
        self._check(self._lib.JLINKARM_WriteU16(address, value), "halfword write")

    def write_u32(self, address: int, value: int) -> None:
        self._check(self._lib.JLINKARM_WriteU32(address, value), "word write")

    def write_u64(self, address: int, value: int) -> None:
        self._check(self._lib.JLINKARM_WriteU64(address, value), "doubleword write")

    def verify_memory(self, address: int, data: bytes) -> bool:
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        return self._lib.JLINKARM_WriteVerifyMem(
            address, len(data), ctypes.byref(buffer)
        ) >= 0

    # -- registers ---------------------------------------------------------

    def registers(self) -> list[Register]:
        """The core's register list, as the DLL reports it for this device.

        Queried rather than hardcoded: the indices differ between Cortex-M,
        Cortex-A and RISC-V, and a table baked in here would be wrong on two
        of the three.
        """
        if self._register_cache is not None:
            return self._register_cache
        indices = (ctypes.c_uint32 * 512)()
        count = self._lib.JLINK_GetRegisterList(
            ctypes.cast(indices, ctypes.POINTER(ctypes.c_uint32)), 512
        )
        if count < 0:
            raise JLinkError("Reading the register list failed", count)
        out: list[Register] = []
        for i in range(count):
            index = indices[i]
            name = self._lib.JLINK_GetRegisterName(index)
            out.append(
                Register(
                    index=index,
                    name=name.decode(errors="replace") if name else f"r{index}",
                )
            )
        self._register_cache = out
        return out

    def _register_index(self, register: int | str) -> int:
        """Resolve a register name to the index this device uses.

        Matching is forgiving because the DLL's names are not what anyone
        types. A Cortex-M reports the program counter as ``R15 (PC)``, so an
        exact-match-only lookup would reject ``"PC"`` -- the single most likely
        thing a caller passes.
        """
        if isinstance(register, int):
            return register
        wanted = register.strip().lower()
        entries = self.registers()

        for entry in entries:
            if entry.name.lower() == wanted:
                return entry.index
        # "R15 (PC)" -> {"r15", "pc"}
        for entry in entries:
            if wanted in _register_aliases(entry.name):
                return entry.index

        available = ", ".join(entry.name for entry in entries[:16])
        raise JLinkError(f"Unknown register {register!r}. Available: {available}...")

    def read_register(self, register: int | str) -> int:
        return int(self._lib.JLINKARM_ReadReg(self._register_index(register)))

    def write_register(self, register: int | str, value: int) -> None:
        self._check(
            self._lib.JLINKARM_WriteReg(self._register_index(register), value),
            "register write",
        )

    def read_registers(self, registers: Sequence[int | str]) -> list[int]:
        indices = [self._register_index(r) for r in registers]
        count = len(indices)
        index_buf = (ctypes.c_uint32 * count)(*indices)
        data_buf = (ctypes.c_uint32 * count)()
        status_buf = (ctypes.c_uint8 * count)()
        rc = self._lib.JLINKARM_ReadRegs(
            ctypes.cast(index_buf, ctypes.POINTER(ctypes.c_uint32)),
            ctypes.cast(data_buf, ctypes.POINTER(ctypes.c_uint32)),
            ctypes.cast(status_buf, ctypes.POINTER(ctypes.c_uint8)),
            count,
        )
        if rc < 0:
            raise JLinkError("Bulk register read failed", rc)
        return list(data_buf)

    def register_dump(self) -> dict[str, int]:
        entries = self.registers()
        values = self.read_registers([e.index for e in entries])
        return {e.name: v for e, v in zip(entries, values)}

    # -- breakpoints -------------------------------------------------------

    def set_breakpoint(
        self, address: int, kind: BreakpointType | int = BreakpointType.ANY
    ) -> int:
        """Set a breakpoint and return its handle."""
        handle = self._lib.JLINKARM_SetBPEx(address, int(kind))
        if handle < 0:
            raise JLinkError(f"Setting a breakpoint at 0x{address:08x} failed", handle)
        return handle

    def clear_breakpoint(self, handle: int) -> None:
        self._check(self._lib.JLINKARM_ClrBPEx(handle), "clearing a breakpoint")

    def clear_all_breakpoints(self) -> None:
        self._lib.JLINKARM_ClrBPEx(-1)

    def breakpoint_count(self) -> int:
        return int(self._lib.JLINKARM_GetNumBPs())

    def breakpoints(self) -> list[BreakpointInfo]:
        out: list[BreakpointInfo] = []
        for i in range(self.breakpoint_count()):
            info = BreakpointInfo.new()
            if self._lib.JLINKARM_GetBPInfoEx(i, ctypes.byref(info)) >= 0:
                out.append(info)
        return out

    # -- watchpoints -------------------------------------------------------

    def set_watchpoint(
        self,
        address: int,
        *,
        address_mask: int = 0,
        data: int | None = None,
        data_mask: int = 0,
        on_write: bool = True,
        on_read: bool = False,
        size: AccessSize = AccessSize.SIZE_32,
        event_type: DataEventType = DataEventType.BP_DATA,
    ) -> int:
        """Arm a data watchpoint and return its handle.

        Routed through ``SetDataEvent`` rather than ``SetWP`` because the same
        comparators can also start and stop trace (``DataEventType.TRACE_START``
        / ``TRACE_STOP``), and only this entry point can ask for that.
        """
        if on_read and on_write:
            access, access_mask = 0, ACCESS_DIR_WRITE
        elif on_read:
            access, access_mask = ACCESS_DIR_READ, 0
        else:
            access, access_mask = ACCESS_DIR_WRITE, 0

        event = DataEvent.new()
        event.Type = int(event_type)
        event.Addr = address
        event.AddrMask = address_mask
        event.Data = data or 0
        # No data value given means "match any value", which is the mask being
        # fully open rather than the data being zero.
        event.DataMask = 0xFFFFFFFF if data is None else data_mask
        event.Access = access | (int(size) << 1)
        event.AccessMask = access_mask

        handle = ctypes.c_uint32(0)
        rc = self._lib.JLINKARM_SetDataEvent(
            ctypes.byref(event), ctypes.byref(handle)
        )
        if rc < 0:
            raise JLinkError(f"Setting a watchpoint at 0x{address:08x} failed", rc)
        return handle.value

    def clear_watchpoint(self, handle: int) -> None:
        self._check(self._lib.JLINKARM_ClrDataEvent(handle), "clearing a watchpoint")

    def clear_all_watchpoints(self) -> None:
        self._lib.JLINKARM_ClrDataEvent(0xFFFFFFFF)

    def watchpoints(self) -> list[WatchpointInfo]:
        out: list[WatchpointInfo] = []
        for i in range(int(self._lib.JLINKARM_GetNumWPs())):
            info = WatchpointInfo.new()
            if self._lib.JLINKARM_GetWPInfoEx(i, ctypes.byref(info)) >= 0:
                out.append(info)
        return out

    # -- flash -------------------------------------------------------------

    def download_file(self, path: str | Path, address: int = 0) -> None:
        """Program a .hex/.bin/.elf/.srec into flash."""
        rc = self._lib.JLINK_DownloadFile(_c_str(str(path)), address)
        if rc < 0:
            raise JLinkError(f"Downloading {path} failed", rc)

    def erase_chip(self) -> None:
        self._check(self._lib.JLINK_EraseChip(), "chip erase")

    # -- trace cache -------------------------------------------------------

    def read_into_trace_cache(self, address: int, num_bytes: int) -> None:
        """Give the DLL the code image it needs to reconstruct the PC stream.

        Make-or-break for ETM. Without it the DLL cannot expand ETM's
        branch/sync points back into a full instruction stream, and every
        subsequent count is silently wrong rather than obviously empty.
        """
        error = self.exec_command(
            f"ReadIntoTraceCache 0x{address:x} 0x{num_bytes:x}"
        )
        if error:
            raise JLinkError(f"Priming the trace cache failed: {error}")

    # -- subsystems --------------------------------------------------------

    @property
    def strace(self):
        from .strace import Strace

        return Strace(self)

    @property
    def trace(self):
        from .tracebuf import TraceBuffer

        return TraceBuffer(self)

    @property
    def raw_trace(self):
        from .tracebuf import RawTrace

        return RawTrace(self)

    @property
    def rtt(self):
        from .rtt import Rtt

        return Rtt(self)

    @property
    def swo(self):
        from .swo import Swo

        return Swo(self)

    @property
    def hss(self):
        from .hss import Hss

        return Hss(self)

    @property
    def power(self):
        from .powertrace import PowerTrace

        return PowerTrace(self)

    @property
    def coresight(self):
        from .coresight import CoreSight

        return CoreSight(self)

    # -- helpers -----------------------------------------------------------

    def _check(self, rc: int, what: str) -> int:
        if rc is not None and rc < 0:
            raise JLinkError(f"{what} failed", rc)
        return rc


def close_open_link() -> bool:
    """Close whatever probe this process still holds. Returns whether it did.

    A script that raises never reaches its own ``close()``, and a J-Link left
    open wedges every later flash, debug session and RTT connection on the
    machine until the process exits. Harmless to call when nothing is open,
    which is the normal case for a script that used the context manager.
    """
    link = JLink._open_instance
    if link is None:
        return False
    link.close()
    return True


__all__ = ["JLink", "ProbeInfo", "Register", "close_open_link"]
