"""Exception types and the DLL's negative return-code vocabulary."""

from __future__ import annotations

# The DLL reports failure as a negative return code. The generic ones below are
# shared across entry points; a handful of calls add their own, which is why
# describe_code() falls back to the raw number rather than inventing a name.
_GENERIC = {
    -1: "unspecified error",
    -2: "emulator (probe) not connected",
    -3: "target not connected or not powered",
    -4: "not supported by this probe or this target",
    -5: "invalid parameter",
    -6: "communication with the probe failed",
    -256: "target voltage too low",
    -257: "no CPU found",
}


def describe_code(code: int) -> str:
    known = _GENERIC.get(code)
    return f"{known} (code {code})" if known else f"code {code}"


class JLinkError(Exception):
    """Any failure originating from the J-Link DLL or its wrapper."""

    def __init__(self, message: str, code: int | None = None) -> None:
        self.code = code
        super().__init__(
            f"{message}: {describe_code(code)}" if code is not None else message
        )


class LibraryNotFoundError(JLinkError):
    """The J-Link Software and Documentation Pack is not installed."""


class NotConnectedError(JLinkError):
    """A call needing an open probe or an attached target was made without one."""


class TraceError(JLinkError):
    """A trace-specific failure: no trace clock, port not routed, empty buffer."""


class SymbolizationError(JLinkError):
    """The ELF could not be parsed, or carries nothing to resolve against."""
