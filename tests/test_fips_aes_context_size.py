"""The wolfCrypt `Aes` context buffer must not be undersized.

A too-small buffer is the worst kind of bug: nothing fails at the call site.
`wc_AesGcmSetKey` writes past the end, every KAT still returns 0 and computes
the correct known answer, and the process dies 0xC0000005 much later in an
unrelated garbage collection at interpreter shutdown.

That is exactly what happened with 512 bytes (2026-07-28): every FIPS-enabled
process segfaulted at EXIT after doing its work correctly, so `m3 setup` read
the non-zero status and reported the embedder "SKIPPED (not installed)" when it
had installed fine. The crypto was right; only the exit code lied.

The struct is bigger than it looks — wolfSSL built with AES-NI (normal on
x86-64) carries an aligned hardware key schedule, and async/devcrypto builds add
more. We do not control it and cannot portably sizeof() it, so the buffer is
deliberately over-allocated. These tests keep it that way.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_PROVIDER = Path(__file__).resolve().parent.parent / "bin" / "crypto_provider.py"


def test_aes_context_is_generously_sized():
    """>= 1024 bytes. 512 demonstrably corrupts the heap on a stock AES-NI
    build; 1024 was the first size that survived. The shipped value is higher
    still, on purpose."""
    import sys as _sys
    _sys.path.insert(0, str(_PROVIDER.parent))
    import crypto_provider as cp

    assert cp._WC_AES_CTX_BYTES >= 1024, (
        f"Aes context buffer is {cp._WC_AES_CTX_BYTES} bytes — too small. "
        "wc_AesGcmSetKey overflows it and corrupts the heap; the fault surfaces "
        "later as a 0xC0000005 during GC, long after the crypto succeeded."
    )


def test_no_hardcoded_aes_buffer_sizes_remain():
    """Every AES context must come from the named constant.

    Three call sites each allocated their own literal 512, so fixing one would
    have left the other two corrupting memory. A literal here is the bug.
    """
    src = _PROVIDER.read_text(encoding="utf-8")
    offenders = [
        f"{_PROVIDER.name}:{n}: {line.strip()}"
        for n, line in enumerate(src.splitlines(), 1)
        if re.search(r"aes\s*=\s*ctypes\.create_string_buffer\(\s*\d+\s*\)", line)
    ]
    assert not offenders, (
        "AES context allocated with a literal size instead of _WC_AES_CTX_BYTES:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.skipif(sys.platform != "win32", reason="wolfSSL is installed at ~/.m3/lib on this host only")
def test_fips_process_exits_cleanly():
    """End-to-end: a FIPS-mode process must exit 0.

    This is the check that would have caught the original bug. It asserts on the
    EXIT CODE, not on output — the output was always correct.
    """
    lib = Path.home() / ".m3" / "lib" / "wolfssl.dll"
    if not lib.is_file():
        pytest.skip("wolfSSL not installed on this host")

    code = (
        f"import sys; sys.path.insert(0, r'{_PROVIDER.parent}');"
        "import crypto_provider as c;"
        "assert c.provider.sha256(b'abc').startswith('ba7816bf')"
    )
    # Pin the library explicitly: the child may be a different interpreter whose
    # trusted-path resolution does not find ~/.m3/lib, and this test is about the
    # EXIT CODE, not about discovery.
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**__import__("os").environ,
                            "M3_FIPS_MODE": "1",
                            "M3_WOLFSSL_LIB": str(lib)})
    assert r.returncode == 0, (
        f"FIPS-mode process exited {r.returncode} "
        f"({'0xC0000005 access violation — heap corruption' if r.returncode in (3221225477, -1073741819) else 'unexpected'}). "
        f"stderr: {r.stderr[-400:]}"
    )
