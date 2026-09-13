"""install_m3() must not clone a second payload on a wheel install (#174).

On pipx/wheel the live code is site-packages/m3_memory/. The old gate was
`repo_path.exists()`, which asks the wrong question: absent -> clone a redundant
copy; present -> report "already installed" naming a tree that is NOT where the
running code lives. The two trees then drift (observed: clone at v2026.8.19.2
vs wheel 2026.9.13.1, 182 commits) and anything wired to the clone runs stale.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

from m3_memory import installer

_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("root,expected", [
    ("/usr/lib/python3.12/site-packages/m3_memory", True),
    ("/usr/lib/python3/dist-packages/m3_memory", True),
    (r"C:\Users\user\pipx\venvs\m3-memory\Lib\site-packages\m3_memory", True),
    ("/home/user/src/m3-memory", False),
    (r"C:\Users\user\.m3-dev\m3-memory", False),
])
def test_is_installed_payload_classifies(root, expected):
    assert installer._is_installed_payload(pathlib.Path(root)) is expected


def test_all_three_copies_of_the_predicate_agree():
    """The predicate exists in three places because install_os.py and
    setup_memory.py are stdlib-only and run before m3 is importable. §10a allows
    the duplication only while the copies agree — this is the guard against
    drift."""
    sys.path.insert(0, str(_ROOT / "bin"))
    try:
        from generate_configs import _is_installed_layout as canonical
    finally:
        sys.path.pop(0)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_m3_install_os_pred", _ROOT / "install_os.py")
    install_os = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install_os)

    for root in (
        "/usr/lib/python3.12/site-packages/m3_memory",
        "/usr/lib/python3/dist-packages/m3_memory",
        r"C:\Users\user\pipx\venvs\m3\Lib\site-packages\m3_memory",
        "/home/user/src/m3-memory",
        r"C:\Users\user\.m3-dev\m3-memory",
    ):
        want = canonical(root)
        assert installer._is_installed_payload(pathlib.Path(root)) == want, root
        assert install_os._is_installed_layout(root) == want, root


def test_installed_payload_short_circuits_before_any_fetch(monkeypatch, tmp_path):
    """The whole point: on an installed layout install_m3 must return the
    packaged bridge WITHOUT invoking git or the tarball fallback."""
    fake_payload = tmp_path / "site-packages" / "m3_memory"
    (fake_payload / "bin").mkdir(parents=True)
    bridge = fake_payload / "bin" / "memory_bridge.py"
    bridge.write_text("# stub\n", encoding="utf-8")

    monkeypatch.setattr(installer, "_is_installed_payload", lambda p: True)
    monkeypatch.setattr(installer, "__file__", str(fake_payload / "installer.py"))

    called = {"fetch": False, "post": False}

    def _boom(*a, **k):
        called["fetch"] = True
        raise AssertionError("install_m3 attempted a fetch on an installed layout")

    for name in ("_clone_repo", "_download_tarball"):
        if hasattr(installer, name):
            monkeypatch.setattr(installer, name, _boom, raising=False)

    monkeypatch.setattr(installer, "save_config", lambda cfg: None)
    monkeypatch.setattr(installer, "_post_install",
                        lambda *a, **k: called.__setitem__("post", True))
    monkeypatch.setattr(installer, "_assert_no_deprecated_pg_url_anywhere",
                        lambda: None)
    for p in ("_prompt_endpoint_choice", "_prompt_capture_mode",
              "_prompt_db_backend", "_prompt_and_install_dashboard",
              "_prompt_and_install_cognitive_loop"):
        monkeypatch.setattr(installer, p, lambda *a, **k: None, raising=False)

    got = installer.install_m3(interactive=False)

    assert got == bridge, f"expected the packaged bridge, got {got}"
    assert called["fetch"] is False, "a fetch was attempted"
    assert called["post"] is True, "_post_install must still run (hooks/config)"


def test_force_still_allows_an_explicit_refetch(monkeypatch):
    """--force is the documented escape hatch for deliberately re-fetching a
    source tree; the guard must not swallow it."""
    import inspect
    src = inspect.getsource(installer.install_m3)
    assert "_is_installed_payload(payload_root) and not force" in src, (
        "the #174 guard must remain conditional on `not force`")
