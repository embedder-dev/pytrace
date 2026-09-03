"""ctypes mirrors of the J-Link DLL's public structures.

Every struct the DLL takes a pointer to begins with a size field it validates,
so each class here exposes ``new()`` which stamps that field. Getting it wrong
is not a compile error and not a crash -- the DLL simply refuses the call, or
worse, reads a shorter struct than you wrote.

``TraceRegionPropsEx`` is the one place the DLL states its own bound out loud:
it rejects ``SizeofStruct`` below 32 or above 256. The layout below is exactly
32 bytes, which is the corroboration that it is right.
"""

from __future__ import annotations

import ctypes
from ctypes import (
    c_char,
    c_int,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from typing import TypeVar

T = TypeVar("T", bound="_Sized")


class _Sized(ctypes.Structure):
    """A struct whose first field tells the DLL how much of it you wrote."""

    _size_field_ = "SizeOfStruct"

    @classmethod
    def new(cls, **kwargs):
        instance = cls(**kwargs)
        setattr(instance, cls._size_field_, ctypes.sizeof(cls))
        return instance


class EmuConnectInfo(ctypes.Structure):
    """One entry from ``JLINKARM_EMU_GetList``."""

    _fields_ = [
        ("SerialNumber", c_uint32),
        ("Connection", c_uint32),
        ("USBAddr", c_uint32),
        ("aIPAddr", c_uint8 * 16),
        ("Time", c_int),
        ("Time_us", c_uint64),
        ("HWVersion", c_uint32),
        ("abMACAddr", c_uint8 * 6),
        ("acProduct", c_char * 32),
        ("acNickName", c_char * 32),
        ("acFWString", c_char * 112),
        ("IsDHCPAssignedIP", c_char),
        ("IsDHCPAssignedIPIsValid", c_char),
        ("NumIPConnections", c_char),
        ("NumIPConnectionsIsValid", c_char),
        ("aPadding", c_uint8 * 34),
    ]


class HwStatus(ctypes.Structure):
    """Target voltage and JTAG pin levels, from ``JLINKARM_GetHWStatus``."""

    _fields_ = [
        ("VTarget", c_uint16),
        ("tck", c_uint8),
        ("tdi", c_uint8),
        ("tdo", c_uint8),
        ("tms", c_uint8),
        ("tres", c_uint8),
        ("trst", c_uint8),
    ]


class SpeedInfo(_Sized):
    _size_field_ = "SizeOfStruct"
    _fields_ = [
        ("SizeOfStruct", c_uint32),
        ("BaseFreq", c_uint32),
        ("MinDiv", c_uint16),
        ("SupportAdaptive", c_uint16),
    ]


class DataEvent(_Sized):
    """A watchpoint, in the form ``JLINKARM_SetDataEvent`` accepts.

    Prefer this over ``JLINKARM_SetWP``: the same hardware comparators back
    both, but this one can also arm trace start/stop rather than only halting.
    """

    _size_field_ = "SizeOfStruct"
    _fields_ = [
        ("SizeOfStruct", c_int),
        ("Type", c_int),
        ("Addr", c_uint32),
        ("AddrMask", c_uint32),
        ("Data", c_uint32),
        ("DataMask", c_uint32),
        ("Access", c_uint32),
        ("AccessMask", c_uint32),
    ]


class BreakpointInfo(_Sized):
    _size_field_ = "SizeOfStruct"
    _fields_ = [
        ("SizeOfStruct", c_uint32),
        ("Handle", c_uint32),
        ("Addr", c_uint32),
        ("Type", c_uint32),
        ("ImpFlags", c_uint32),
        ("UseCnt", c_uint32),
    ]


class WatchpointInfo(_Sized):
    _size_field_ = "SizeOfStruct"
    _fields_ = [
        ("SizeOfStruct", c_uint32),
        ("Handle", c_uint32),
        ("Addr", c_uint32),
        ("AddrMask", c_uint32),
        ("Data", c_uint32),
        ("DataMask", c_uint32),
        ("Ctrl", c_uint32),
        ("CtrlMask", c_uint32),
        ("WPUnit", c_uint8),
    ]


class MoeInfo(ctypes.Structure):
    """Method of entry -- why the core stopped."""

    _fields_ = [
        ("HaltReason", c_uint32),
        ("Index", c_int),
    ]


class FlashAreaInfo(ctypes.Structure):
    _fields_ = [("Addr", c_uint32), ("Size", c_uint32)]


class RamAreaInfo(ctypes.Structure):
    _fields_ = [("Addr", c_uint32), ("Size", c_uint32)]


class DeviceInfo(_Sized):
    """One entry of the DLL's device database, from ``JLINK_DEVICE_GetInfo``."""

    _size_field_ = "SizeOfStruct"
    _fields_ = [
        ("SizeOfStruct", c_uint32),
        ("sName", c_void_p),
        ("CoreId", c_uint32),
        ("FlashAddr", c_uint32),
        ("RAMAddr", c_uint32),
        ("EndianMode", c_char),
        ("FlashSize", c_uint32),
        ("RAMSize", c_uint32),
        ("sManu", c_void_p),
        ("aFlashArea", FlashAreaInfo * 32),
        ("aRAMArea", RamAreaInfo * 32),
        ("Core", c_uint32),
    ]


# --------------------------------------------------------------------------
# RTT
# --------------------------------------------------------------------------


class RttStart(ctypes.Structure):
    """Payload for ``RttCmd.START``.

    ``ConfigBlockAddress = 0`` asks the DLL to search target RAM for the
    "SEGGER RTT" signature, which is what you want unless the control block
    lives somewhere the search does not reach.
    """

    _fields_ = [
        ("ConfigBlockAddress", c_uint32),
        ("Reserved", c_uint32 * 3),
    ]


class RttBufferDesc(ctypes.Structure):
    """Payload for ``RttCmd.GETDESC``.

    Set ``BufferIndex`` and ``Direction`` before the call; the DLL fills the
    rest.
    """

    _fields_ = [
        ("BufferIndex", c_int),
        ("Direction", c_uint32),
        ("acName", c_char * 32),
        ("SizeOfBuffer", c_uint32),
        ("Flags", c_uint32),
    ]


class RttStatus(ctypes.Structure):
    """Payload for ``RttCmd.GETSTAT``."""

    _fields_ = [
        ("NumBytesTransferred", c_uint32),
        ("NumBytesRead", c_uint32),
        ("HostOverflowCount", c_int),
        ("IsRunning", c_int),
        ("NumUpBuffers", c_int),
        ("NumDownBuffers", c_int),
        ("Reserved", c_uint32 * 2),
    ]


# --------------------------------------------------------------------------
# Trace
# --------------------------------------------------------------------------


class TraceRegionProps(ctypes.Structure):
    """Payload for ``TraceCmd.GET_REGION_PROPS``."""

    _fields_ = [
        ("RegionIndex", c_uint32),
        ("NumSamples", c_uint32),
        ("Off", c_uint32),
        ("RegionCnt", c_uint32),
        ("Dummy", c_uint32),
    ]


class TraceRegionPropsEx(ctypes.Structure):
    """Payload for ``TraceCmd.GET_REGION_PROPS_EX``.

    ``SizeofStruct`` is spelled the way the DLL spells it, and this layout is
    exactly the 32 bytes its own size check demands. ``Dummy`` is not filler --
    it is what aligns the 64-bit timestamp.
    """

    _fields_ = [
        ("SizeofStruct", c_uint32),
        ("RegionIndex", c_uint32),
        ("NumSamples", c_uint32),
        ("Off", c_uint32),
        ("RegionCnt", c_uint32),
        ("Dummy", c_uint32),
        ("Timestamp", c_uint64),
    ]

    @classmethod
    def new(cls, region_index: int = 0) -> "TraceRegionPropsEx":
        instance = cls()
        instance.SizeofStruct = ctypes.sizeof(cls)
        instance.RegionIndex = region_index
        return instance


class StraceTimestampInfo(ctypes.Structure):
    """One record from ``JLINK_STRACE_ReadEx``.

    Sixteen bytes, and that size is not a guess -- it is what the DLL moves
    per record, the same way :class:`TraceRegionPropsEx` is corroborated by the
    DLL's own size check.

    ``Timestamp`` is a cycle count, meant to be interpolated between and
    divided by the core clock to get a time. ``Index`` is the position, within
    the program counters returned by the same call, that the stamp refers to.

    ``Adjust`` is **unverified**: nothing has established what the DLL puts
    there. It is carried through untouched and never acted on. See
    :meth:`jtrace.strace.Strace.read_ex` for what else about this call remains
    unmeasured.
    """

    _fields_ = [
        ("Timestamp", c_uint64),
        ("Index", c_uint32),
        ("Adjust", c_uint32),
    ]


class TraceData(ctypes.Structure):
    """One item returned by ``JLINKARM_TRACE_Read``.

    The trace buffer is a stream of these, not of bare program counters -- that
    is the difference between this API and STRACE, which hands back decoded
    PCs.
    """

    _fields_ = [
        ("PipeStat", c_uint8),
        ("Sync", c_uint8),
        ("Packet", c_uint16),
    ]


# --------------------------------------------------------------------------
# HSS
# --------------------------------------------------------------------------


class HssMemBlockDesc(ctypes.Structure):
    """One memory region for ``JLINK_HSS_Start`` to sample."""

    _fields_ = [
        ("Addr", c_uint32),
        ("NumBytes", c_uint32),
        ("Flags", c_uint32),
    ]


# --------------------------------------------------------------------------
# Power trace
#
# The two structs below follow SEGGER's published shape and are the least
# certain layouts in this module; if a probe rejects them, drive
# POWERTRACE_Control with a raw buffer instead -- PowerTrace.control() takes
# one for exactly that reason.
# --------------------------------------------------------------------------


class PowerTraceSetup(_Sized):
    _size_field_ = "SizeOfStruct"
    _fields_ = [
        ("SizeOfStruct", c_int),
        ("ChannelMask", c_int),
        ("SampleFreq", c_int),
        ("RefSelect", c_int),
    ]


class PowerTraceCaps(_Sized):
    _size_field_ = "SizeOfStruct"
    _fields_ = [
        ("SizeOfStruct", c_int),
        ("ChannelMask", c_int),
        ("MaxSampleFreq", c_int),
        ("Reserved", c_int * 5),
    ]


class PowerTraceItem(ctypes.Structure):
    _fields_ = [
        ("Timestamp", c_uint64),
        ("Value", c_uint32),
        ("Channel", c_uint32),
    ]


__all__ = [
    "BreakpointInfo",
    "DataEvent",
    "DeviceInfo",
    "EmuConnectInfo",
    "FlashAreaInfo",
    "HssMemBlockDesc",
    "HwStatus",
    "MoeInfo",
    "PowerTraceCaps",
    "PowerTraceItem",
    "PowerTraceSetup",
    "RamAreaInfo",
    "RttBufferDesc",
    "RttStart",
    "RttStatus",
    "SpeedInfo",
    "StraceTimestampInfo",
    "TraceData",
    "TraceRegionProps",
    "TraceRegionPropsEx",
    "WatchpointInfo",
]
