"""Locating ``libjlinkarm`` and declaring its C prototypes to ctypes.

Discovery deliberately mirrors what the SEGGER tools themselves search, so a
capture driven from Python binds the same library a debug session on the same
machine would have used. Two processes talking to two different J-Link DLLs on
one machine is a class of bug worth designing out.

The one addition is an unpacked tarball under ``$HOME``, searched last. On ARM
Linux that is how the software normally arrives -- there is no package, and
untar-and-run-in-place leaves nothing in /opt and nothing on PATH.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from ctypes import (
    POINTER,
    c_char_p,
    c_int,
    c_int32,
    c_int64,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from pathlib import Path

from .errors import LibraryNotFoundError

_SEARCH_DIRS: dict[str, list[str]] = {
    "darwin": ["/Applications/SEGGER/JLink"],
    "linux": ["/opt/SEGGER/JLink", "/usr/lib", "/usr/lib64", "/usr/bin"],
    "win32": [
        r"C:\Program Files\SEGGER\JLink",
        r"C:\Program Files (x86)\SEGGER\JLink",
    ],
}

_EXE_NAMES = ["JLinkExe", "JLink.exe", "JLinkExe.exe"]

# SEGGER ships Linux and macOS as a tarball meant to be unpacked and run in
# place, which is a normal install rather than an odd one. It leaves the library
# somewhere like ``~/JLink_Linux_V972_arm64/libjlinkarm.so`` with nothing in
# /opt and nothing on PATH -- which is the usual reason JLINK_LIBRARY ends up
# mandatory on ARM Linux.
_HOME_DIR_GLOBS: dict[str, list[str]] = {
    "linux": ["JLink*", "jlink*"],
    "darwin": ["JLink*", "jlink*"],
}


def _library_patterns() -> list[re.Pattern[str]]:
    if sys.platform == "win32":
        if sys.maxsize <= 2**32:
            return [re.compile(r"^JLinkARM\.dll$", re.I)]
        return [
            re.compile(r"^JLink_x64\.dll$", re.I),
            re.compile(r"^JLinkARM_x64\.dll$", re.I),
            re.compile(r"^JLinkARM\.dll$", re.I),
        ]
    if sys.platform == "darwin":
        return [
            re.compile(r"^libjlinkarm\.dylib$"),
            re.compile(r"^libjlinkarm(\.\d+)+\.dylib$"),
        ]
    return [
        re.compile(r"^libjlinkarm\.so$"),
        re.compile(r"^libjlinkarm\.so(\.\d+)+$"),
    ]


def _windows_install_dirs() -> list[str]:
    if sys.platform != "win32":
        return []
    found: list[str] = []
    try:
        out = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\SEGGER\J-Link", "/v", "InstallPath"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        match = re.search(r"InstallPath\s+REG_SZ\s+(.+)", out.stdout, re.I)
        if out.returncode == 0 and match:
            found.append(match.group(1).strip())
    except Exception:
        pass
    return found


def _unpacked_install_dirs() -> list[Path]:
    """Directories under ``$HOME`` that look like an unpacked SEGGER tarball.

    One level deep and anchored on the name SEGGER actually uses. A wider walk
    of ``$HOME`` would be slow on every lookup and would be a way to bind
    something that merely resembles a J-Link library, which is worse than not
    finding one.

    Reverse-sorted so a newer unpacked version wins over an older one left
    beside it -- ``JLink_Linux_V972_arm64`` before ``V918``. That ordering is
    lexical, not semantic; ``JLINK_LIBRARY`` is still how you pin one exactly.
    """
    patterns = _HOME_DIR_GLOBS.get(sys.platform)
    if not patterns:
        return []
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return []
    found: list[Path] = []
    for pattern in patterns:
        try:
            found.extend(entry for entry in home.glob(pattern) if entry.is_dir())
        except OSError:
            continue
    return sorted(dict.fromkeys(found), reverse=True)


def _candidate_dirs() -> list[Path]:
    """Where to look, in order of precedence."""
    candidates: list[Path] = [Path(d) for d in _SEARCH_DIRS.get(sys.platform, [])]
    for name in _EXE_NAMES:
        located = shutil.which(name)
        if located:
            candidates.append(Path(located).parent)
    candidates.extend(Path(d) for d in _windows_install_dirs())
    # Last, so a packaged install still wins over a tarball someone happened to
    # unpack beside it.
    candidates.extend(_unpacked_install_dirs())
    return list(dict.fromkeys(candidates))


def find_library() -> Path | None:
    """Return the J-Link DLL path, or None if the software pack is not installed.

    Looks in the packaged install locations, then ``PATH``, then the Windows
    registry, then any unpacked SEGGER tarball directly under ``$HOME`` -- in
    that order, so a system install wins over one somebody unpacked beside it.

    ``JLINK_LIBRARY`` overrides everything, which is how you pin a specific
    version when several are installed side by side (a J-Link install leaves
    ``JLink_V918`` next to ``JLink``, and they are not interchangeable for
    trace work). Unpacked tarballs make that more likely, not less.
    """
    override = os.environ.get("JLINK_LIBRARY", "").strip()
    if override:
        path = Path(override)
        return path if path.exists() else None

    listings: list[tuple[Path, list[str]]] = []
    for directory in _candidate_dirs():
        try:
            listings.append((directory, os.listdir(directory)))
        except OSError:
            continue

    for pattern in _library_patterns():
        for directory, names in listings:
            for name in sorted(names):
                if pattern.match(name):
                    return directory / name
    return None


# --------------------------------------------------------------------------
# Prototypes
#
# Every entry is (restype, [argtypes]). Functions the library does not export
# are skipped silently at bind time: the DLL surface differs across versions
# and platforms, and a missing POWERTRACE entry point must not stop someone
# capturing instruction trace. Call a missing one and you get a clear
# AttributeError naming it, which is a better failure than a segfault.
# --------------------------------------------------------------------------

U8 = c_uint8
U16 = c_uint16
U32 = c_uint32
U64 = c_uint64
I32 = c_int32
I64 = c_int64
PU8 = POINTER(c_uint8)
PU32 = POINTER(c_uint32)
PI32 = POINTER(c_int32)

_PROTOTYPES: dict[str, tuple[object, list[object]]] = {
    # ---- lifecycle -------------------------------------------------------
    "JLINKARM_OpenEx": (c_char_p, [c_void_p, c_void_p]),
    "JLINKARM_Open": (c_char_p, []),
    "JLINKARM_Close": (None, []),
    "JLINKARM_IsOpen": (c_int, []),
    "JLINK_SetLogFile": (c_int, [c_char_p]),
    "JLINKARM_EnableLog": (None, [c_void_p]),
    "JLINKARM_SetErrorOutHandler": (None, [c_void_p]),
    "JLINKARM_SetWarnOutHandler": (None, [c_void_p]),
    "JLINKARM_ExecCommand": (c_int, [c_char_p, c_char_p, c_int]),
    "JLINKARM_GetDLLVersion": (c_int, []),
    "JLINKARM_GetCompileDateTime": (c_char_p, []),
    "JLINK_GetPCode": (c_int, [c_void_p, c_void_p]),
    # ---- probe selection / info -----------------------------------------
    "JLINKARM_EMU_SelectByUSBSN": (c_int, [U32]),
    "JLINK_EMU_SelectByIndex": (c_int, [c_int]),
    "JLINK_EMU_SelectByUSBNickname": (c_int, [c_char_p]),
    "JLINKARM_EMU_SelectIP": (c_int, [c_char_p, c_int]),
    "JLINKARM_EMU_SelectIPBySN": (c_int, [U32]),
    "JLINKARM_EMU_GetNumDevices": (c_int, []),
    "JLINKARM_EMU_GetList": (c_int, [c_int, c_void_p, c_int]),
    "JLINKARM_EMU_GetProductName": (c_int, [c_char_p, U32]),
    "JLINKARM_EMU_GetDeviceInfo": (c_int, [U32, c_void_p]),
    "JLINK_EMU_GetProductId": (c_int, []),
    "JLINKARM_EMU_IsConnected": (c_int, []),
    "JLINKARM_EMU_GetNumConnections": (c_int, []),
    "JLINKARM_EMU_HasCapEx": (c_int, [c_int]),
    "JLINKARM_EMU_HasCPUCap": (c_int, [c_int]),
    "JLINKARM_EMU_GetCounters": (c_int, [U32, PU32]),
    "JLINKARM_EMU_GetMaxMemBlock": (U32, []),
    "JLINK_EMU_GetVCOMPorts": (c_int, [c_void_p, c_int]),
    "JLINK_EMU_GPIO_GetProps": (c_int, [c_void_p, c_int]),
    "JLINK_EMU_GPIO_GetState": (c_int, [PU32, PU8, c_int]),
    "JLINK_EMU_GPIO_SetState": (c_int, [PU32, PU8, PU8, c_int]),
    "JLINK_EMU_GetLicenses": (c_int, [c_char_p, U32]),
    "JLINK_EMU_AddLicense": (c_int, [c_char_p]),
    "JLINK_EMU_EraseLicenses": (c_int, []),
    "JLINK_EMU_FILE_GetList": (c_int, [c_char_p, U32]),
    "JLINK_EMU_FILE_GetSize": (c_int, [c_char_p]),
    "JLINK_EMU_FILE_Read": (c_int, [c_char_p, PU8, U32, U32]),
    "JLINK_EMU_FILE_Write": (c_int, [c_char_p, PU8, U32, U32]),
    "JLINK_EMU_FILE_Delete": (c_int, [c_char_p]),
    "JLINKARM_GetSN": (c_int, []),
    "JLINKARM_GetFirmwareString": (None, [c_char_p, c_int]),
    "JLINKARM_GetEmbeddedFWString": (c_int, [c_char_p, c_char_p, c_int]),
    "JLINKARM_GetHardwareVersion": (U32, []),
    "JLINKARM_GetEmuCaps": (U32, []),
    "JLINKARM_GetEmuCapsEx": (c_int, [PU8, c_int]),
    "JLINKARM_GetFeatureString": (None, [c_char_p]),
    "JLINKARM_GetOEMString": (c_int, [c_char_p]),
    "JLINK_GetAvailableLicense": (c_int, [c_char_p, U32]),
    "JLINKARM_GetHWStatus": (c_int, [c_void_p]),
    "JLINK_GetHWInfo": (c_int, [U32, PU32]),
    "JLINK_INDICATORS_SetState": (c_int, [c_void_p]),
    "JLINKARM_NET_Open": (c_int, [c_char_p, c_int]),
    "JLINKARM_NET_Close": (None, []),
    # ---- device / interface ---------------------------------------------
    "JLINKARM_TIF_Select": (c_int, [c_int]),
    "JLINKARM_TIF_GetAvailable": (c_int, [PU32]),
    "JLINKARM_SetSpeed": (None, [U32]),
    "JLINKARM_GetSpeed": (U32, []),
    "JLINKARM_SetMaxSpeed": (None, []),
    "JLINKARM_GetSpeedInfo": (None, [c_void_p]),
    "JLINKARM_Connect": (c_int, []),
    "JLINKARM_IsConnected": (c_int, []),
    "JLINKARM_GetDeviceFamily": (c_int, []),
    "JLINK_DEVICE_GetIndex": (c_int, [c_char_p]),
    "JLINK_DEVICE_GetInfo": (c_int, [c_int, c_void_p]),
    "JLINKARM_GetSelDevice": (c_int, []),
    "JLINKARM_Core2CoreName": (c_int, [U32, c_char_p, c_int]),
    "JLINKARM_CORE_GetFound": (U32, []),
    "JLINKARM_CORE_Select": (c_int, [U32]),
    "JLINKARM_SetCoreIndex": (c_int, [c_int]),
    "JLINKARM_GetId": (U32, []),
    "JLINKARM_GetIdData": (c_int, [c_void_p]),
    "JLINKARM_MeasureCPUSpeed": (c_int, [U32, c_int]),
    "JLINKARM_MeasureCPUSpeedEx": (c_int, [U32, c_int, c_int]),
    "JLINKARM_GetDebugInfo": (c_int, [U32, PU32]),
    "JLINK_GetMemZones": (c_int, [c_void_p, c_int]),
    "JLINKARM_SetEndian": (c_int, [c_int]),
    # ---- run control -----------------------------------------------------
    "JLINKARM_Reset": (c_int, []),
    "JLINKARM_ResetNoHalt": (None, []),
    "JLINKARM_SetResetType": (c_int, [c_int]),
    "JLINKARM_SetResetDelay": (None, [c_int]),
    "JLINKARM_SetResetPara": (c_int, [c_int]),
    "JLINK_GetResetTypeDesc": (c_char_p, [c_int]),
    "JLINKARM_Halt": (c_int, []),
    "JLINKARM_IsHalted": (c_int, []),
    "JLINKARM_Go": (None, []),
    "JLINKARM_GoEx": (None, [c_int, U32]),
    "JLINKARM_GoHalt": (c_int, [U32]),
    "JLINKARM_GoIntDis": (None, []),
    "JLINKARM_GoAllowSim": (c_int, [U32, c_void_p, c_void_p]),
    "JLINKARM_Step": (c_int, []),
    "JLINKARM_StepComposite": (c_int, []),
    "JLINKARM_WaitForHalt": (c_int, [c_int]),
    "JLINKARM_GetMOEs": (c_int, [c_void_p, c_int]),
    "JLINKARM_ClrError": (None, []),
    "JLINKARM_HasError": (c_int, []),
    "JLINKARM_WriteVectorCatch": (c_int, [U32]),
    "JLINKARM_SetInitRegsOnReset": (c_int, [c_int]),
    "JLINKARM_SimulateInstruction": (c_int, [U32]),
    "JLINKARM_GetExecTime": (U32, []),
    "JLINKARM_ClrExecTime": (None, []),
    "JLINKARM_EnablePerformanceCnt": (c_int, [c_int]),
    "JLINKARM_GetPerformanceCnt": (c_int, [c_int, PU32]),
    # ---- reset / pin control --------------------------------------------
    "JLINKARM_SetRESET": (None, []),
    "JLINKARM_ClrRESET": (None, []),
    "JLINKARM_SetTRST": (None, []),
    "JLINKARM_ClrTRST": (None, []),
    "JLINKARM_SetTCK": (c_int, []),
    "JLINKARM_ClrTCK": (c_int, []),
    "JLINKARM_SetTDI": (None, []),
    "JLINKARM_ClrTDI": (None, []),
    "JLINKARM_SetTMS": (None, []),
    "JLINKARM_ClrTMS": (None, []),
    "JLINKARM_ResetTRST": (None, []),
    "JLINKARM_Clock": (U32, []),
    # ---- memory ----------------------------------------------------------
    "JLINKARM_ReadMem": (c_int, [U32, U32, c_void_p]),
    "JLINKARM_ReadMemEx": (c_int, [U32, U32, c_void_p, U32]),
    "JLINK_ReadMemEx_64": (c_int, [U64, U32, c_void_p, U32]),
    "JLINKARM_ReadMemHW": (c_int, [U32, U32, c_void_p]),
    "JLINKARM_ReadMemIndirect": (c_int, [U32, U32, c_void_p]),
    "JLINKARM_ReadCodeMem": (c_int, [U32, U32, c_void_p]),
    "JLINKARM_ReadMemU8": (c_int, [U32, U32, PU8, PU8]),
    "JLINKARM_ReadMemU16": (c_int, [U32, U32, POINTER(c_uint16), PU8]),
    "JLINKARM_ReadMemU32": (c_int, [U32, U32, PU32, PU8]),
    "JLINKARM_ReadMemU64": (c_int, [U32, U32, POINTER(c_uint64), PU8]),
    "JLINK_ReadMemZonedEx": (c_int, [U32, U32, c_void_p, U32, c_char_p]),
    "JLINK_ReadMemZonedU32": (c_int, [U32, U32, PU32, PU8, c_char_p]),
    "JLINKARM_WriteMem": (c_int, [U32, U32, c_void_p]),
    "JLINKARM_WriteMemEx": (c_int, [U32, U32, c_void_p, U32]),
    "JLINK_WriteMemEx_64": (c_int, [U64, U32, c_void_p, U32]),
    "JLINKARM_WriteMemHW": (c_int, [U32, U32, c_void_p]),
    "JLINKARM_WriteMemDelayed": (c_int, [U32, U32, c_void_p]),
    "JLINK_WriteMemZonedEx": (c_int, [U32, U32, c_void_p, U32, c_char_p]),
    "JLINKARM_WriteU8": (c_int, [U32, U8]),
    "JLINKARM_WriteU16": (c_int, [U32, U16]),
    "JLINKARM_WriteU32": (c_int, [U32, U32]),
    "JLINKARM_WriteU64": (c_int, [U32, U64]),
    "JLINK_WriteZonedU32": (c_int, [U32, U32, c_char_p]),
    "JLINKARM_WriteVerifyMem": (c_int, [U32, U32, c_void_p]),
    "JLINKARM_EnableFlashCache": (c_int, [c_int]),
    "JLINKARM_EnableCheckModeAfterWrite": (c_int, [c_int]),
    "JLINKARM_AddMirrorAreaEx": (c_int, [U32, U32]),
    "JLINK_WA_AddRange": (c_int, [U32, U32]),
    "JLINK_WA_Restore": (c_int, []),
    # ---- registers -------------------------------------------------------
    "JLINKARM_ReadReg": (U32, [c_int]),
    "JLINKARM_WriteReg": (c_int, [c_int, U32]),
    "JLINKARM_ReadRegs": (c_int, [PU32, PU32, PU8, U32]),
    "JLINKARM_WriteRegs": (c_int, [PU32, PU32, PU8, U32]),
    "JLINK_GetRegisterList": (c_int, [PU32, c_int]),
    "JLINK_GetRegisterName": (c_char_p, [U32]),
    "JLINK_ReadSystemReg": (c_int, [U32, U32, U32, U32, PU32]),
    "JLINK_WriteSystemReg": (c_int, [U32, U32, U32, U32, U32]),
    "JLINKARM_ReadICEReg": (U32, [c_int]),
    "JLINKARM_WriteICEReg": (c_int, [c_int, U32, c_int]),
    "JLINKARM_ReadDebugReg": (c_int, [U32, PU32]),
    "JLINKARM_WriteDebugReg": (c_int, [U32, U32]),
    "JLINKARM_ReadDebugPort": (c_int, [U32, PU32]),
    "JLINKARM_WriteDebugPort": (c_int, [U32, U32]),
    "JLINKARM_ReadControlReg": (c_int, [U32, PU32]),
    "JLINKARM_WriteControlReg": (c_int, [U32, U32]),
    "JLINKARM_CP15_IsPresent": (c_int, []),
    "JLINKARM_CP15_ReadEx": (c_int, [U8, U8, U8, U8, PU32]),
    "JLINKARM_CP15_WriteEx": (c_int, [U8, U8, U8, U8, U32]),
    # ---- breakpoints / watchpoints --------------------------------------
    "JLINKARM_SetBPEx": (c_int, [U32, U32]),
    "JLINK_SetBPEx_64": (c_int, [U64, U32]),
    "JLINKARM_ClrBPEx": (c_int, [c_int]),
    "JLINKARM_FindBP": (c_int, [U32]),
    "JLINKARM_GetNumBPs": (c_int, []),
    "JLINKARM_GetNumBPUnits": (c_int, [U32]),
    "JLINKARM_GetBPInfoEx": (c_int, [c_int, c_void_p]),
    "JLINKARM_EnableSoftBPs": (None, [c_int]),
    "JLINKARM_SetWP": (c_int, [U32, U32, U32, U32, U8, U8]),
    "JLINKARM_ClrWP": (c_int, [c_int]),
    "JLINKARM_GetNumWPs": (c_int, []),
    "JLINKARM_GetNumWPUnits": (c_int, []),
    "JLINKARM_GetWPInfoEx": (c_int, [c_int, c_void_p]),
    "JLINKARM_SetDataEvent": (c_int, [c_void_p, PU32]),
    "JLINKARM_ClrDataEvent": (c_int, [U32]),
    # ---- flash / download ------------------------------------------------
    "JLINK_DownloadFile": (c_int, [c_char_p, U32]),
    "JLINK_EraseChip": (c_int, []),
    "JLINKARM_BeginDownload": (None, [U32]),
    "JLINKARM_EndDownload": (c_int, []),
    "JLINKARM_SetFlashArea": (None, [U32, U32]),
    "JLINK_SetFlashProgProgressCallback": (None, [c_void_p]),
    "JLINK_DEVICE_GetLoaderName": (c_int, [c_int, c_int, c_char_p, U32]),
    # ---- STRACE (instruction trace) --------------------------------------
    "JLINK_STRACE_Config": (c_int, [c_char_p]),
    "JLINK_STRACE_Start": (c_int, []),
    "JLINK_STRACE_Stop": (c_int, []),
    "JLINK_STRACE_Read": (c_int, [PU32, U32]),
    # Five args, not three, and confirmed two independent ways against the
    # shipped library. The public headers do not carry this prototype.
    "JLINK_STRACE_ReadEx": (c_int, [PU32, U32, c_void_p, PI32, U32]),
    "JLINK_STRACE_Control": (c_int, [U32, c_void_p]),
    "JLINK_STRACE_GetInstStats": (c_int, [c_void_p, U32, U32, U32, U32]),
    # ---- TRACE (offset-addressable buffer) -------------------------------
    "JLINKARM_TRACE_Control": (c_int, [U32, c_void_p]),
    "JLINKARM_TRACE_Read": (c_int, [c_void_p, U32, PU32]),
    "JLINKARM_TRACE_AddInst": (c_int, [U32, U32]),
    "JLINKARM_TRACE_AddItems": (c_int, [c_void_p, U32]),
    "JLINK_SelectTraceSource": (c_int, [c_int]),
    # ---- RAWTRACE --------------------------------------------------------
    "JLINKARM_RAWTRACE_Control": (c_int, [U32, c_void_p]),
    "JLINKARM_RAWTRACE_Read": (c_int, [PU8, U32]),
    # ---- SWO / ITM -------------------------------------------------------
    "JLINKARM_SWO_Config": (c_int, [c_char_p]),
    "JLINK_SWO_Control": (c_int, [U32, c_void_p]),
    "JLINK_SWO_Read": (c_int, [PU8, U32, PU32]),
    "JLINKARM_SWO_ReadStimulus": (c_int, [c_int, PU8, U32]),
    "JLINKARM_SWO_EnableTarget": (c_int, [U32, U32, U32, U32]),
    "JLINKARM_SWO_DisableTarget": (c_int, [U32]),
    "JLINKARM_SWO_GetCompatibleSpeeds": (c_int, [U32, U32, PU32, U32]),
    # ---- RTT -------------------------------------------------------------
    "JLINK_RTTERMINAL_Control": (c_int, [U32, c_void_p]),
    "JLINK_RTTERMINAL_Read": (c_int, [U32, c_char_p, U32]),
    "JLINK_RTTERMINAL_Write": (c_int, [U32, c_char_p, U32]),
    # ---- HSS -------------------------------------------------------------
    "JLINK_HSS_Start": (c_int, [c_void_p, c_int, c_int, c_int]),
    "JLINK_HSS_Stop": (c_int, []),
    "JLINK_HSS_Read": (c_int, [c_void_p, U32]),
    "JLINK_HSS_GetCaps": (c_int, [PU32]),
    # ---- power trace -----------------------------------------------------
    "JLINK_POWERTRACE_Control": (c_int, [U32, c_void_p, c_void_p]),
    "JLINK_POWERTRACE_Read": (c_int, [c_void_p, U32]),
    # ---- ETM / ETB / CoreSight -------------------------------------------
    "JLINKARM_ETM_IsPresent": (c_int, []),
    "JLINKARM_ETM_ReadReg": (U32, [U32]),
    "JLINKARM_ETM_WriteReg": (c_int, [U32, U32, c_int]),
    "JLINKARM_ETM_StartTrace": (None, []),
    "JLINKARM_ETB_IsPresent": (c_int, []),
    "JLINKARM_ETB_ReadReg": (U32, [U32]),
    "JLINKARM_ETB_WriteReg": (c_int, [U32, U32, c_int]),
    "JLINKARM_CORESIGHT_Configure": (c_int, [c_char_p]),
    "JLINKARM_CORESIGHT_ReadAPDPReg": (c_int, [U8, U8, PU32]),
    "JLINKARM_CORESIGHT_WriteAPDPReg": (c_int, [U8, U8, U32]),
    # ---- misc target channels -------------------------------------------
    "JLINKARM_ReadTerminal": (c_int, [PU8, U32]),
    "JLINKARM_ReadDCC": (c_int, [PU32, U32, c_int]),
    "JLINKARM_WriteDCC": (c_int, [PU32, U32, c_int]),
    "JLINK_SPI_Transfer": (c_int, [PU8, PU8, U32, U32]),
    "JLINKARM_DisassembleInst": (c_int, [c_char_p, c_int, U32]),
    "JLINKARM_PERIODIC_Control": (c_int, [U32, c_void_p]),
    "JLINKARM_PERIODIC_Read": (c_int, [c_void_p, U32]),
    "JLINKARM_PERIODIC_ConfReadMem": (c_int, [c_void_p, U32]),
    # ---- dialogs ---------------------------------------------------------
    "JLINK_DIALOG_Configure": (c_int, [c_void_p]),
    "JLINK_DIALOG_ConfigureEx": (c_int, [c_void_p]),
    "JLINK_SetHookUnsecureDialog": (c_int, [c_void_p]),
    "JLINKARM_UpdateFirmwareIfNewer": (c_int, []),
}


class RawLibrary:
    """A bound ``libjlinkarm`` with C prototypes applied.

    Thin on purpose: it owns argtypes/restype and nothing else. Every policy
    decision -- what counts as an error, when to halt, how to encode a string
    -- belongs to the layers above, so this stays a faithful view of the C API.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        located = Path(path) if path is not None else find_library()
        if located is None:
            raise LibraryNotFoundError(
                "J-Link software not found. Install the SEGGER J-Link Software "
                "and Documentation Pack, or set JLINK_LIBRARY to the DLL path."
            )
        if not located.exists():
            raise LibraryNotFoundError(f"J-Link library does not exist: {located}")

        self.path = located
        loader = ctypes.WinDLL if sys.platform == "win32" else ctypes.CDLL
        self._dll = loader(str(located))
        self.bound: set[str] = set()
        self.missing: set[str] = set()

        for name, (restype, argtypes) in _PROTOTYPES.items():
            try:
                fn = getattr(self._dll, name)
            except AttributeError:
                self.missing.add(name)
                continue
            fn.restype = restype
            fn.argtypes = argtypes
            # Bound onto the instance, not left on the CDLL: __getattr__ below
            # only runs for names that are *not* instance attributes, so a
            # prototype that stayed on the CDLL would be indistinguishable
            # from one the DLL never exported.
            setattr(self, name, fn)
            self.bound.add(name)

    def __getattr__(self, name: str):
        if name in _PROTOTYPES:
            raise AttributeError(
                f"{name} is not exported by {self.path}. "
                f"This J-Link DLL predates it, or the feature is unavailable "
                f"on this platform."
            )
        return getattr(self._dll, name)

    def has(self, name: str) -> bool:
        return name in self.bound


_CACHED: RawLibrary | None = None


def load(path: str | os.PathLike[str] | None = None) -> RawLibrary:
    """Load (once) and return the J-Link library.

    The DLL keeps process-global state -- one open probe, one selected device,
    one trace buffer -- so loading it twice in a process would not give you two
    independent probes anyway. The cache makes that explicit.
    """
    global _CACHED
    if path is not None:
        return RawLibrary(path)
    if _CACHED is None:
        _CACHED = RawLibrary()
    return _CACHED


def is_available() -> bool:
    """Whether a J-Link library can be found on this machine.

    A cheap pre-flight for a script that wants to degrade rather than raise --
    everything in this SDK that does not touch a probe (ELF and DWARF parsing,
    symbolization, coverage rows, artifact writing) works when this is False.
    """
    return find_library() is not None


__all__ = [
    "RawLibrary",
    "find_library",
    "is_available",
    "load",
]
