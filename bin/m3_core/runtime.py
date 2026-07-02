import logging
import os
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs): pass

M3_CORE_RS_DISABLE = os.environ.get("M3_CORE_RS_DISABLE", "0") == "1"

try:
    if M3_CORE_RS_DISABLE:
        raise ImportError
    from m3_core_rs import format_log
except ImportError:
    def format_log(event: str, *args, **kwargs) -> str:
        parts = [event]
        for a in args:
            if a is None or a == "":
                continue
            parts.append(str(a))
        for k, v in kwargs.items():
            if v is None:
                continue
            parts.append(f"{k}={v}")
        return " | ".join(parts)

logger = logging.getLogger("M3_SDK")


def ensure_utf8() -> None:
    """Guarantee the current process runs in Python UTF-8 mode.

    On Windows both stdio AND open() default to the legacy cp1252 code page, so
    any non-cp1252 character (em-dashes, arrows, box-drawing, emoji) crashes with
    UnicodeEncodeError on print or UnicodeDecodeError on a no-encoding open().
    True UTF-8 mode (PEP 540) fixes both, but the interpreter reads it only at
    startup — so we set PYTHONUTF8 and re-exec once with -X utf8.

    Shared canonical implementation: called from every m3 entry process that
    isn't guaranteed to inherit UTF-8 mode — the m3 CLI (m3_memory.cli) and the
    standalone MCP→OpenAI proxy (bin/mcp_proxy.py, the OpenClaw path, launched
    directly as `python bin/mcp_proxy.py` so it never flows through the CLI).

    Safety: no-op if already in UTF-8 mode; an env sentinel bounds the re-exec to
    exactly once so it cannot loop; sys.orig_argv reconstructs the launch
    faithfully (so -m / file-path forms survive).

    KNOWN LIMITATION: inline `python -c "<code>"` launches can mangle on re-exec
    because the OS re-quotes the program string; not a supported m3 entry path.
    Set PYTHONUTF8=1 in the env to bypass (then this short-circuits).
    """
    if sys.flags.utf8_mode:
        return
    if os.environ.get("_M3_UTF8_REEXEC") == "1":
        return
    os.environ["PYTHONUTF8"] = "1"
    os.environ["_M3_UTF8_REEXEC"] = "1"
    orig = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv])) or [
        sys.executable, *sys.argv]
    try:
        os.execv(sys.executable, [orig[0], "-X", "utf8", *orig[1:]])
    except OSError:
        # Re-exec failed (exotic launcher / permissions). Caller's stdio
        # reconfigure (if any) still handles the common print path.
        pass


# Single source of truth for the local LLM base URL + read timeout. Bridges
# import this from here instead of redefining it in each bridge.
# Still overridable via env so dev machines with LM Studio on a
# different port (or a remote Ollama) work without code edits.
LM_STUDIO_BASE = os.environ.get("LM_STUDIO_BASE", "http://localhost:1234/v1")
LM_READ_TIMEOUT = float(os.environ.get("LM_READ_TIMEOUT", "4800.0"))


class StructuredLogger:
    """Renders structured log lines as `event | k=v | k=v` for grep-friendly output."""

    def format(self, event: str, *args, **kwargs) -> str:
        return format_log(event, *args, **kwargs)

    def log(self, event: str, *args, **kwargs) -> None:
        """Helper to format and print a structured log line to stderr."""
        print(self.format(event, *args, **kwargs), file=sys.stderr)
