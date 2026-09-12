"""The embed-server probe must report the service, not the PATH.

#167: doctor printed ``embed-server: not installed (optional)`` while the
service was registered AND serving on :8082. The verdict came entirely from
``shutil.which(BINARY_NAME)`` -- but the binary normally lives inside the
m3_core_rs wheel and is NOT on PATH, so which() returns None on a perfectly
healthy install.

Measured 2026-09-12 on this host:

    shutil.which("m3-embed-server.exe")     -> None
    embedder_admin._server_binary()         -> .../m3_core_rs/m3-embed-server.exe
    GET http://127.0.0.1:8082/health        -> 200

The contradiction was visible inside a single doctor run: the
embedding-cascade line said "healthy -- shared tier-2 embedder online" while
this probe said the binary was missing. Two checks disagreeing about one
process is a good signal one of them is asking the wrong question.

The ``(optional)`` suffix made it worse -- it invites the reader to dismiss the
line, so a GENUINELY missing embedder would read identically to this false
alarm. §3: "a warning that fires when nothing is wrong trains users to ignore
the one that matters."
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(__file__)
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
for _p in (os.path.join(_ROOT, "bin"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from doctor import embed_server_probe as esp  # noqa: E402


def test_binary_resolution_does_not_stop_at_PATH():
    """which() is a starting point, not the answer."""
    import inspect

    src = inspect.getsource(esp._resolve_binary)
    assert "shutil.which" in src, "PATH is still worth trying first"
    assert "_server_binary" in src, (
        "resolution stops at PATH; the wheel-internal binary will read as absent"
    )


def test_a_live_port_is_not_reported_as_not_installed():
    """The specific #167 verdict.

    'cannot find the binary' and 'the service is absent' are different claims.
    Conflating them reported a live server as missing.
    """
    import inspect

    src = inspect.getsource(esp.run)
    assert "_port_answers" in src, (
        "the verdict never consults the port, so a running service whose binary "
        "cannot be located is still reported as not installed"
    )
    # The 'not installed' wording must sit on the branch where the port is also
    # silent -- otherwise it is the same false alarm with extra steps.
    idx = src.index("not installed (optional)")
    assert "_port_answers" in src[:idx], (
        "'not installed' is printed without first checking whether the port "
        "answers"
    )


def test_missing_subcommand_is_not_reported_as_a_health_failure():
    """`doctor` does not exist on every build of this binary.

    Measured against m3_core_rs 3.9.7: `m3-embed-server doctor` prints USAGE and
    exits 2, while `status` works. Treating that rc 2 as FAILED reports a fine
    install as broken -- the same false alarm as #167, one layer down, and it
    was HIDDEN by the PATH bug: which() always returned None, so the subprocess
    never ran and nobody learned the subcommand was unsupported.
    """
    import inspect

    src = inspect.getsource(esp.run)
    assert "USAGE" in src, (
        "an unsupported subcommand (USAGE + non-zero rc) is indistinguishable "
        "from a real health failure"
    )
    assert '"status"' in src, "no fallback to a subcommand the binary supports"


def test_probe_runs_and_returns_an_int_on_this_box():
    """End-to-end, against whatever is really installed here.

    A probe that raises is worse than one that is wrong: doctor would lose every
    check after it. This must hold whether or not the binary exists.
    """
    rc = esp.run(brief=True)
    assert isinstance(rc, int), f"probe returned {type(rc).__name__}, not an int"
    assert rc in (0, 1, 2), f"unexpected rc {rc}"


def test_resolution_never_raises_when_the_package_is_absent(monkeypatch):
    """A doctor probe must degrade, not explode.

    _resolve_binary imports m3_memory.embedder_admin, which may not be
    importable in every environment the doctor runs in.
    """
    # Run it in a CLEAN interpreter rather than trying to un-import a module
    # this process has already loaded.
    #
    # Two in-process attempts failed for reasons that were about the HARNESS,
    # not the code: patching builtins.__import__ also breaks the imports pytest
    # performs mid-test, and poisoning sys.modules does nothing once an earlier
    # test in this file has already imported the real module -- `from x import
    # y` then resolves against the cached parent. A subprocess has neither
    # problem and tests what a real absent install actually does.
    import subprocess
    import textwrap

    code = textwrap.dedent(
        """
        import sys, os
        root = sys.argv[1]
        sys.path.insert(0, os.path.join(root, "bin"))
        sys.path.insert(0, root)
        from doctor import embed_server_probe as esp
        esp.shutil.which = lambda *a, **k: None          # nothing on PATH
        sys.modules["m3_memory.embedder_admin"] = None   # package absent
        try:
            print("RESULT:", esp._resolve_binary())
        except Exception as exc:
            print("RAISED:", type(exc).__name__)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code, _ROOT],
        capture_output=True, text=True, timeout=120,
    )
    assert "RAISED:" not in out.stdout, (
        f"_resolve_binary raised instead of degrading: {out.stdout.strip()}"
    )
    assert "RESULT: None" in out.stdout, (
        f"expected None when nothing is resolvable, got: {out.stdout.strip()}"
    )
