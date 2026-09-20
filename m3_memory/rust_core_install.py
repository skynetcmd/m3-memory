"""Resolve and install the right prebuilt `m3-core-rs` wheel for this host.

m3-core-rs is published to PyPI as several differently-named packages, one
per (OS, backend) pair, all installing the same `m3_core_rs` import module:

    m3-core-rs-windows-cpu      m3-core-rs-linux-cpu
    m3-core-rs-windows-cuda     m3-core-rs-linux-cuda
    m3-core-rs-windows-vulkan   m3-core-rs-linux-vulkan
                                m3-core-rs-macos-metal

The user's single entry point is the m3 setup wizard / `m3 install-gpu`,
which detects (os, backend) here, installs the matching prebuilt wheel from
PyPI, and only falls back to a from-source build when no prebuilt wheel is
available for the host's platform + Python.

The (os, backend) -> package-name mapping MUST stay byte-identical to
`crates/m3-core-py/build_wheel.py::package_name` in the m3-core-rs repo —
that script names the wheels this module installs.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from m3_memory._platform import os_name as _os_name

# Release this m3-memory build expects. Bump in lockstep with the m3-core-rs
# release. The wheel VERSION and the GitHub release TAG are independent: the tag
# follows the date-based v2026.MM.DD convention (v2026.7.25), while the wheels
# are semver-versioned (3.7.25). Used as the version pin for both the prebuilt
# PyPI install (by version) and the GitHub-release asset fetch (by tag).
# 3.7.4 was the first release whose wheels bundle the m3-embed-server binary.
#
# 3.9.16 / v2026.9.16 (2026-09-16). Verify the Release is complete with:
#   gh release view v2026.9.16 --repo skynetcmd/m3-core-rs
#   (expect 29 assets: 7 (os,backend) packages x cp311-314, + SHA256SUMS)
#   NOTE: 3.9.16 is the LAST release carrying cp311. m3-core-rs raised its
#   requires-python to >=3.12 to match m3-memory's own floor, so releases
#   after this one are 21 wheels (7 x cp312-314).
#
# ⚠ 3.9.16 CHANGES THE EMBED SERVER'S MEMORY FOOTPRINT PER WHEEL. Each worker
# stream materialises its own llama.cpp compute graph on FIRST USE, sized for a
# worst-case n_ctx batch, and holds it for the process lifetime — measured
# ~3.85 GiB per stream (bge-m3, n_ctx=8192). The cost tracks n_ctx^2 x heads,
# so it is a property of the MODEL, not the backend: a CPU-only box pays the
# same ~4 GiB per stream as a GPU box.
#
# So the default now depends on the WHEEL, because the wheel implies the
# hardware:
#   GPU wheels (cuda / vulkan / metal) -> streams = 2
#       A discrete GPU or Apple unified memory implies a machine with headroom,
#       and the second context buys concurrency for bulk ingest.
#   CPU-only wheels                    -> streams = 1
#       A CPU-only deployment is the modest-hardware case almost by definition;
#       a second graph is a large fraction of such a machine. Better to ship
#       something that runs than something fast that will not fit.
#
# ⚠ THIS IS A MEMORY-FOR-THROUGHPUT TRADE, not a free win. One context serves
# one embedding batch at a time, so on a CPU-only host concurrent callers now
# QUEUE where they previously ran on two contexts — most visible during bulk
# ingest. `queue_depth` on the server's /metrics shows whether callers are
# waiting; if they are and the host has the memory, raise it.
#
# Total footprint is bounded: baseline + streams x graph. It is NOT a leak —
# exactly `streams` requests pay, and every request after that is free whatever
# its size. Overridable per host by M3_EMBED_STREAMS or [embed].streams in the
# server's config.toml; the resolved value is logged at startup.
#
# WHERE EACH WHEEL LIVES — not every backend can go to PyPI. The CUDA wheels
# exceed PyPI's per-file size limit (windows-cuda ~244 MiB, linux-cuda ~949 MiB
# against a 100 MB limit — an order of magnitude, so size is a permanent
# barrier, not a hurdle a rebuild clears). They ship ONLY via the GitHub
# Release, and release.yml's publish matrix deliberately omits them;
# `m3-core-rs-windows-cuda` being a 404 on PyPI is BY DESIGN.
#
# The resolver therefore cascades GitHub Release -> PyPI -> source. The Release
# is the CANONICAL channel: it carries all 7 backends x 4 interpreters on every
# tag, so it is complete by construction, whereas PyPI can never carry CUDA.
#
# It is also the only channel that is CURRENT. The 5 PyPI-eligible backends
# (cpu / vulkan / macos-metal) are supposed to publish to PyPI but do not: every
# publish job fails trusted-publishing exchange with `invalid-publisher`, so all
# five still serve 3.7.4 (2026-07-04) while 3.7.25/.27/.28/.29/.31 each shipped a
# complete Release. A PyPI-first cascade would install that stale core and STOP,
# because pip exits 0 — a stale success is worse than a clean miss. Fixing the
# PyPI publishers (see docs) does not make PyPI-first correct again; leave the
# Release first.
M3_CORE_RS_VERSION = "3.9.16"
M3_CORE_RS_GIT_TAG = "v2026.9.16"

# Cargo features per backend, mirroring build_wheel.py's _MATRIX (the source
# fallback passes these to maturin via pip's config-settings).
# CPU uses `embedded` (CPU-only llama.cpp) so every build ships an in-process
# BGE-M3 EmbeddedEmbedder — m3 must always have a default bge-m3 embedder, not
# depend on the embed-server being present. A source-fallback CPU build thus
# needs a C/C++ compiler + cmake.
_BACKEND_FEATURES: dict[str, list[str]] = {
    "cpu": ["embedded"],
    "cuda": ["embedded-cuda"],
    "vulkan": ["embedded-vulkan"],
    "metal": ["embedded-metal"],
}

# Valid (os, backend) combinations. macOS is Metal-only by design.
_VALID: set[tuple[str, str]] = {
    ("windows", "cpu"), ("windows", "cuda"), ("windows", "vulkan"),
    ("linux", "cpu"), ("linux", "cuda"), ("linux", "vulkan"),
    ("macos", "metal"),
}


# ── canonical Project Oxidation speed framing ────────────────────────────────
# Single source of truth for every user-facing message about the native wheel.
# The numbers and framing are deliberate (see project memory, 2026-06-27):
#
#   * OXIDATION_SPEEDUP_X — the RELATIVE multiplier (native in-process
#     EmbeddedEmbedder vs the no-wheel HTTP fallback). Authoritative; the one
#     figure tests may assert.
#   * The ABSOLUTE latencies (~10-50 ms with; ~0.3-2.5 s without) are
#     ILLUSTRATIVE ONLY — they always carry a "varies by host" qualifier and
#     must NEVER be asserted as fact in a test (no measured per-embed
#     benchmark exists yet).
#
# The three states this distinguishes:
#   1. GPU wheel  -> in-process EmbeddedEmbedder (CUDA/Metal/Vulkan) — fastest.
#   2. CPU wheel  -> in-process EmbeddedEmbedder (CPU llama.cpp) — STILL the
#      oxidized hot path; "no GPU" does NOT mean "no in-process embedder".
#   3. NO wheel   -> HTTP fallback (:8082 / primary). The only state the
#      reassurance below describes.
OXIDATION_SPEEDUP_X = "~10-85x"
_OXIDATION_WITH_MS = "~10-50 ms"
_OXIDATION_WITHOUT_S = "~0.3-2.5 s"


def oxidation_fallback_note(*, indent: str = "") -> str:
    """Canonical reassurance shown whenever the native wheel is absent.

    Reassures that m3 is fully functional as a pure-Python solution and that
    only Project Oxidation's hot-path optimizations are missing, so speed is
    not maximized — but it remains very usable. Stated both ways: the typical
    with-Oxidation latency and the (illustrative) without-Oxidation latency in
    seconds. ``indent`` is prepended to every line so callers can nest it under
    their own output.
    """
    lines = [
        "m3 is fully functional as a pure-Python solution — your memories,",
        "search, chatlog, and sync all work. Only Project Oxidation's hot-path",
        "optimizations are not employed, so speed is not maximized.",
        "",
        "  With Project Oxidation (the native in-process embedder) a typical",
        f"  embed completes in {_OXIDATION_WITH_MS}. Without it, the same embed runs",
        f"  on the HTTP fallback path {OXIDATION_SPEEDUP_X} longer — roughly",
        f"  {_OXIDATION_WITHOUT_S} each (illustrative; varies by host). Slower, but",
        "  still very usable.",
    ]
    return "\n".join(f"{indent}{ln}".rstrip() for ln in lines)


def native_core_outcome_note(*, indent: str = "") -> str:
    """What to tell the user after an install/upgrade of the native core FAILED.

    A non-zero exit from `embedder install-gpu` means the FETCH failed. It does
    NOT mean the hot path is gone: on an UPGRADE the previously-installed wheel
    is still imported and still serving. Those two facts come apart on exactly
    the case where the reassurance is most wrong, so this probes the live tier
    instead of inferring capability from an exit code.

    Two outcomes, never conflated:
      * native core still loaded -> say so, with backend and version, and name
        the upgrade as the only thing that did not land.
      * no native core at all    -> the pure-Python reassurance, which is the
        ONLY state ``oxidation_fallback_note`` describes (see the comment above
        it). Printing it while a wheel is live is a false alarm: it tells the
        user to install a toolchain they do not need to fix a slowdown they do
        not have.

    Single owner of this decision — every call site that reports a failed
    native-core install must route through here rather than printing the
    fallback note unconditionally.
    """
    tier = active_embedder_tier()
    if not tier.get("native"):
        return oxidation_fallback_note(indent=indent)

    backend = tier.get("backend")
    where = f"{backend}, " if backend else ""
    # §3 evidence levels: the tier probe is MEASURED (it imported the module and
    # read its backend), so it is stated as observed. Why the fetch failed is
    # NOT measured here — this function sees no channel exit codes — so the
    # candidates are offered as `possible:` and never asserted as the cause.
    lines = [
        f"observed: native core loaded and serving ({where}"
        f"m3_core_rs {tier.get('version')}) — the in-process hot path is "
        f"UNAFFECTED.",
        f"observed: the upgrade to {M3_CORE_RS_VERSION} did not land; the "
        f"loaded core is unchanged.",
        "possible: no wheel published for this platform/Python yet, or the "
        "download failed.",
        "inspect : `m3 doctor` (oxidation section), or the install log via "
        "`m3 embedder install-gpu`.",
        "",
        "  No action needed and no toolchain to install: embeds keep running",
        "  in-process at full speed. The upgrade retries on the next",
        "  `m3 setup` once a matching wheel is available.",
    ]
    return "\n".join(f"{indent}{ln}".rstrip() for ln in lines)


def active_embedder_tier() -> dict:
    """Report which embedder tier is actually live on this host.

    Returns a dict: {"native": bool, "backend": str|None, "version": str|None,
    "summary": str}. Distinguishes the three states (GPU wheel / CPU wheel /
    no wheel) by probing whether ``m3_core_rs`` imports and exposes
    ``EmbeddedEmbedder``. Best-effort and import-safe — never raises; a host
    without the wheel just reports the pure-Python fallback state.

    Note: this reports whether the NATIVE WHEEL is installed and usable, not
    whether a GGUF is configured. A wheel with no GGUF set still means the hot
    path is available; ``memory/doctor.py`` owns the GGUF/tier-1-vs-tier-2
    runtime probe. This is the install-time "did Oxidation land?" view.
    """
    out = {"native": False, "backend": None, "version": None, "summary": ""}
    try:
        import m3_core_rs  # type: ignore
    except Exception:  # noqa: BLE001 — no wheel installed
        out["summary"] = (
            "pure-Python (Project Oxidation native wheel not installed — "
            f"embeds run {OXIDATION_SPEEDUP_X} slower on the HTTP fallback path "
            "but m3 is fully usable). Run `m3 embedder install-gpu`."
        )
        return out
    if not hasattr(m3_core_rs, "EmbeddedEmbedder"):
        out["summary"] = (
            "native wheel present but built WITHOUT the embedded feature — "
            "embeds use the HTTP fallback. Reinstall with "
            "`m3 embedder install-gpu`."
        )
        return out
    out["native"] = True
    out["version"] = getattr(m3_core_rs, "__version__", None) or M3_CORE_RS_VERSION
    # Best-effort backend label (cpu/cuda/metal/vulkan) if the wheel exposes one.
    backend = None
    for attr in ("embed_backend_label", "backend_label", "BACKEND"):
        val = getattr(m3_core_rs, attr, None)
        try:
            backend = val() if callable(val) else val
        except Exception:  # noqa: BLE001
            backend = None
        if backend:
            break
    out["backend"] = str(backend) if backend else None
    inner = f"{out['backend']}, " if out["backend"] else ""
    out["summary"] = (
        f"tier-1 in-process — Project Oxidation active "
        f"({inner}m3_core_rs {out['version']})"
    )
    return out


def _parse_version(v: str) -> tuple:
    """Best-effort tuple parse of a dotted version for comparison. Non-numeric
    components sort after numeric ones (so '3.6.27' > '3.6.27rc1' is avoided —
    we keep it simple: split on '.', int where possible else fall back to a
    high-sorting marker so a pre-release isn't treated as newer)."""
    parts: list = []
    for tok in str(v).strip().split("."):
        num = "".join(c for c in tok if c.isdigit())
        parts.append(int(num) if num and num == tok else (int(num) if num else 0, tok))
    return tuple(parts)


def installed_rust_core_version() -> "str | None":
    """The version of the installed m3_core_rs, or None if not importable."""
    try:
        import m3_core_rs  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    return getattr(m3_core_rs, "__version__", None)


def is_rust_core_current() -> bool:
    """True iff the embedded native wheel is installed AND demonstrably at (or
    newer than) the target M3_CORE_RS_VERSION. Used to skip a redundant
    reinstall. CONSERVATIVE: if the real installed version can't be read from the
    wheel (no __version__), return False so the install proceeds — never skip an
    upgrade on a guess. (active_embedder_tier falls back to M3_CORE_RS_VERSION
    for display when __version__ is absent; do NOT trust that for the skip
    decision — read __version__ directly.)"""
    try:
        import m3_core_rs  # type: ignore
    except Exception:  # noqa: BLE001 — no wheel
        return False
    if not hasattr(m3_core_rs, "EmbeddedEmbedder"):
        return False  # wheel built without the embedded feature — must reinstall
    cur = getattr(m3_core_rs, "__version__", None)
    if not cur:
        return False  # unknown version — don't skip; reinstall to be safe
    try:
        return _parse_version(cur) >= _parse_version(M3_CORE_RS_VERSION)
    except Exception:  # noqa: BLE001 — unparseable → don't skip, reinstall
        return cur == M3_CORE_RS_VERSION


@dataclass(frozen=True)
class BackendChoice:
    os_tok: str          # windows | linux | macos
    backend: str         # cpu | cuda | vulkan | metal
    reason: str          # human-readable why this backend was picked

    @property
    def package(self) -> str:
        return package_name(self.os_tok, self.backend)

    @property
    def features(self) -> list[str]:
        return _BACKEND_FEATURES[self.backend]


def package_name(os_tok: str, backend: str) -> str:
    """PyPI project name for an (os, backend) pair. Mirrors build_wheel.py."""
    return f"m3-core-rs-{os_tok}-{backend}"


def host_os() -> str:
    """OS token (windows/linux/macos) from the WMI-safe platform helper."""
    name = _os_name()  # 'Windows' | 'Darwin' | 'Linux'
    return {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[name]


def _vulkan_has_device() -> bool:
    """Return True if vulkaninfo reports at least one physical GPU device.

    vulkaninfo is often installed on headless Linux boxes (as part of the
    mesa/vulkan-tools package) without any Vulkan-capable GPU. Presence of
    the binary alone is not a reliable signal. We run `vulkaninfo --summary`
    (fast, no display required) and look for a GPU name line, which only
    appears when a real device is enumerated.

    Returns False on any error (timeout, permission denied, parse failure)
    so the caller always falls back to CPU safely.
    """
    try:
        result = subprocess.run(
            ["vulkaninfo", "--summary"],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout + result.stderr
        # vulkaninfo --summary prints "GPU id : 0 (Device Name)" for each device.
        # "No devices available" or empty deviceName means no real GPU.
        for line in output.splitlines():
            lo = line.lower()
            if "gpu id" in lo and "no device" not in lo:
                return True
            # Also catch "deviceName" from the full JSON-style output.
            if "devicename" in lo and lo.split("=")[-1].strip() not in ("", "unknown"):
                return True
    except Exception:
        pass
    return False


def detect_backend(os_tok: Optional[str] = None) -> BackendChoice:
    """Pick the best backend for this host.

    Order: macOS -> Metal (always). Else NVIDIA toolchain (nvcc) -> CUDA;
    else a Vulkan runtime/SDK -> Vulkan; else CPU. This intentionally matches
    the legacy detection in embedder_admin.cmd_install_gpu so behavior is
    unchanged for callers that relied on it — only the *install action*
    (prebuilt wheel first) changes.
    """
    os_tok = os_tok or host_os()

    if os_tok == "macos":
        return BackendChoice(os_tok, "metal", "macOS — Metal is the only backend")

    if shutil.which("nvcc") or os.environ.get("CUDA_PATH"):
        return BackendChoice(os_tok, "cuda", "NVIDIA CUDA toolchain detected")

    # Vulkan: SDK env var (explicit build-time config) OR vulkaninfo reports
    # at least one real device. vulkaninfo presence alone is not enough —
    # the Vulkan loader/tools are often installed system-wide on headless
    # Linux boxes without any Vulkan-capable GPU. Probe the output.
    if os.environ.get("VULKAN_SDK"):
        return BackendChoice(os_tok, "vulkan", "VULKAN_SDK env var set")
    if shutil.which("vulkaninfo") and _vulkan_has_device():
        return BackendChoice(os_tok, "vulkan", "Vulkan device detected via vulkaninfo")

    return BackendChoice(os_tok, "cpu", "no GPU toolchain detected — CPU build")


def detect_backend_chain(os_tok: Optional[str] = None) -> list[BackendChoice]:
    """The ordered list of backends to try for this host, best first.

    ``detect_backend`` names the single BEST backend. That is the right answer
    to "what should this host run", but the wrong input to an installer: when
    no wheel exists for that backend, the host does not suddenly lose its GPU.
    A CUDA box that cannot get a CUDA wheel can still run the Vulkan wheel, and
    failing that the CPU wheel — all three are the in-process EmbeddedEmbedder
    (the oxidized hot path). Dropping to pure-Python HTTP because ONE of the
    three was unpublished discards a working native tier for no reason.

    Order is by hardware capability, and each step is a real downgrade the host
    can actually execute:

        NVIDIA host  : cuda -> vulkan -> cpu
        Vulkan host  : vulkan -> cpu
        macOS        : metal -> cpu
        no GPU       : cpu

    ⚠ Vulkan is offered ONLY to a host shown to have a Vulkan-capable device,
    never as a blind middle step. An NVIDIA GPU is Vulkan-capable by
    construction (every supported NVIDIA driver ships a Vulkan ICD), which is
    what makes cuda -> vulkan sound without a second probe. A CPU-only host
    gets no vulkan entry: a wheel whose device is absent would install happily
    and then fail at runtime, which is worse than not installing it.

    The CPU wheel is always last and always present — it is the floor, not a
    fallback, and it keeps the install inside the native hot path.
    """
    best = detect_backend(os_tok or host_os())
    # Derive the OS from the CHOSEN backend, never from this call's argument:
    # detect_backend is the single owner of that decision and may legitimately
    # return a different os_tok than it was handed. Reading the argument here
    # instead produced a chain whose fallbacks named a different OS than its
    # head (linux-cuda -> windows-vulkan) — a package that cannot exist.
    os_tok = best.os_tok

    # NOTE metal has no fallback by construction: macOS publishes ONLY the
    # metal wheel (there is no ("macos", "cpu") in _VALID), so the generic
    # path below correctly yields a single-entry chain.

    def _add(chain: list, backend: str, reason: str) -> None:
        """Append a fallback only if that (os, backend) wheel actually exists.

        Guards against naming a package the project never publishes — a step
        that could only ever 404, converting one honest miss into several and
        burying the real reason under noise.
        """
        if (os_tok, backend) in _VALID:
            chain.append(BackendChoice(os_tok, backend, reason))

    chain = [best]
    if best.backend == "cuda":
        # An NVIDIA GPU always exposes Vulkan, so this needs no device probe.
        # Do not add a vulkaninfo check here: the tools are frequently absent
        # on a working CUDA box and their absence would wrongly drop the tier.
        _add(chain, "vulkan",
             "fallback — no CUDA wheel; NVIDIA GPUs are Vulkan-capable")
        _add(chain, "cpu", "fallback — no CUDA or Vulkan wheel")
    elif best.backend == "vulkan":
        _add(chain, "cpu", "fallback — no Vulkan wheel available")
    return chain


def _pip(*args: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        env=env if env is not None else os.environ.copy(),
        capture_output=True,
        text=True,
    )


def _is_pep668(result: subprocess.CompletedProcess) -> bool:
    """Return True if pip refused due to PEP 668 (externally-managed-environment)."""
    return result.returncode != 0 and "externally-managed-environment" in result.stderr


def _can_sudo() -> bool:
    """Return True if the current user can run sudo without a password prompt.

    Uses `sudo -n true` — the -n flag makes sudo fail immediately rather than
    prompting, so this is safe to call non-interactively.

    Windows has no `sudo`; and on a Unix box that doesn't ship sudo the binary
    is absent (raising FileNotFoundError). Both cases mean "cannot sudo" — we
    return False rather than letting the missing-binary exception escape into
    the install path.
    """
    if sys.platform == "win32" or not shutil.which("sudo"):
        return False
    try:
        return subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
        ).returncode == 0
    except OSError:
        return False


def _in_privileged_group() -> bool:
    """Return True if the user is in the 'sudo' or 'wheel' group.

    The `grp` module is Unix-only — it does not exist on Windows, where the
    sudo/wheel concept is meaningless anyway (privilege there is the
    Administrators group / UAC). So on Windows this is always False by design.
    We branch on the platform explicitly rather than catching the ImportError
    blindly, so a real failure inside the lookup isn't silently swallowed as
    "not privileged".
    """
    if sys.platform == "win32":
        return False
    try:
        import grp  # Unix-only; guarded by the platform check above.
    except ImportError:
        return False
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return False
    try:
        return any(
            g.gr_name in {"sudo", "wheel"} and user in g.gr_mem
            for g in grp.getgrall()
        )
    except OSError:
        # getgrall() can fail on misconfigured NSS / LDAP; treat as "unknown,
        # assume not privileged" rather than crashing the install path.
        return False


def _pip_install_with_pep668_fallback(*pip_args: str) -> int:
    """Run pip install, retrying with --user on PEP 668 systems.

    Strategy:
      1. Try pip install <args> as-is.
      2. If pip rejects with PEP 668 (externally-managed-environment),
         retry with --user (installs to ~/.local, no root needed).
      3. If --user also fails, detect whether the user can sudo and
         print the exact command(s) to run as root, then return non-zero.

    Returns 0 on success, non-zero otherwise.
    """
    result = _pip(*pip_args)
    if result.returncode == 0:
        return 0

    if not _is_pep668(result):
        # Some other pip error — print stderr and propagate.
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return result.returncode

    print("[rust-core] pip refused (PEP 668 externally-managed-environment); "
          "retrying with --user ...", file=sys.stderr)
    user_result = _pip("--user", *pip_args)
    if user_result.returncode == 0:
        return 0

    # --user also failed. Build the exact sudo command and advise the user.
    sudo_cmd = (
        f"sudo pip install --break-system-packages {' '.join(pip_args)}"
    )
    can_sudo = _can_sudo()
    in_group = _in_privileged_group()

    if can_sudo:
        print(
            "[rust-core] --user install also failed. You have sudo access — "
            "run this command to install system-wide:\n"
            f"    {sudo_cmd}",
            file=sys.stderr,
        )
    elif in_group:
        print(
            "[rust-core] --user install also failed. You are in the sudo/wheel "
            "group but sudo requires your password. Open another shell and run:\n"
            f"    {sudo_cmd}",
            file=sys.stderr,
        )
    else:
        print(
            "[rust-core] --user install also failed and you do not appear to "
            "have sudo access.\n"
            "Ask a system administrator to open a root shell and run:\n"
            f"    {sudo_cmd}\n"
            "Or ask them to add you to the 'sudo' (Debian/Ubuntu) or 'wheel' "
            "(RHEL/Fedora/Arch) group, then log out and back in.",
            file=sys.stderr,
        )

    if user_result.stderr:
        print(user_result.stderr, file=sys.stderr, end="")
    return user_result.returncode


def install_prebuilt(choice: BackendChoice, *, version: str = M3_CORE_RS_VERSION) -> int:
    """Try to install the matching prebuilt wheel via pip.

    pip resolves from PyPI *or* from its local wheel cache, and this returns only
    an exit code — so a 0 here means "a prebuilt wheel got installed", NOT "it
    came from PyPI". Callers must not label the result as a PyPI install.

    Returns 0 on success, non-zero otherwise. A non-zero result (other than a
    PEP 668 advisory already printed) signals to fall back to a source build.
    """
    spec = f"{choice.package}=={version}"
    print(f"[rust-core] installing prebuilt wheel: {spec}  ({choice.reason})")
    return _pip_install_with_pep668_fallback(
        "install", "--upgrade", "--only-binary=:all:", spec
    )


def _find_executable(candidates: list[str]) -> Optional[str]:
    """Return the first executable from candidates that exists and runs.

    A binary that exists but isn't executable by the current user is
    treated as absent (same end-user symptom: the build can't invoke it).
    """
    for cmd in candidates:
        path = shutil.which(cmd)
        if path:
            try:
                subprocess.run([path, "--version"], capture_output=True, timeout=5)
                return path
            except (PermissionError, OSError):
                pass  # binary exists but not executable for this user
    return None


def _find_cargo() -> Optional[str]:
    """Locate cargo, including rustup-installed toolchains not on PATH.

    The friction case (2026-06-07): user had rustup-installed Rust but
    ~/.cargo/bin wasn't sourced, so `shutil.which("cargo")` returned None
    even though cargo was at ~/.rustup/toolchains/<triple>/bin/cargo. The
    source-build then failed mid-compile with a cryptic error after pip
    had already pulled all deps. Probing rustup's toolchain dirs catches
    this and lets us report a clean missing-prereq error up front.
    """
    found = _find_executable(["cargo"])
    if found:
        return found

    # ~/.cargo/bin is rustup's "current toolchain" symlink dir
    candidate = os.path.expanduser("~/.cargo/bin/cargo")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate

    # Toolchain-specific dirs (rustup default install w/o PATH wiring)
    rustup_home = os.path.expanduser(os.environ.get("RUSTUP_HOME", "~/.rustup"))
    toolchains_dir = os.path.join(rustup_home, "toolchains")
    if os.path.isdir(toolchains_dir):
        try:
            for toolchain in os.listdir(toolchains_dir):
                candidate = os.path.join(toolchains_dir, toolchain, "bin", "cargo")
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
        except OSError:
            pass
    return None


def _check_build_tools() -> list[str]:
    """Return a list of missing build tools needed for a source build.

    Checks for cmake, a C++ compiler, and the Rust toolchain (cargo). All
    must be executable by the current user — a binary that exists but
    isn't executable is reported as missing (same symptom as absent).

    Rust check probes rustup's toolchain dirs in addition to PATH — see
    _find_cargo for the rationale.
    """
    missing = []
    if not _find_executable(["cmake"]):
        missing.append("cmake")
    if not _find_executable(["c++", "g++", "clang++"]):
        missing.append("C++ compiler")
    if not _find_cargo():
        missing.append("Rust (cargo)")
    return missing


def _print_manual_build_recommendation(
    choice: BackendChoice, *, pypi_rc: int, release_rc: int
) -> None:
    """Print a multi-line, actionable recommendation when both prebuilt
    paths missed and the caller has disabled auto-source-build.

    Two audiences read this:
      - The curl-install.sh user who saw "Project Oxidation" prompt say yes.
        For them, the wheel isn't critical — tier-2 HTTP keeps embeddings
        working. They need to know that (so they don't think they're broken)
        AND how to opt into the optional build if they want the speed.
      - Operators triaging a CI/non-interactive deploy. They need the exact
        repro: package name, version, and the explicit command to run.
    """
    feats = ",".join(choice.features) if choice.features else "(none)"
    install_rust_cmd = (
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
    )

    # An UPGRADE over an already-working wheel fails here too, and for that host
    # every line below — the slowdown, the toolchain, the build steps — is false
    # and actively misleading. Report the live tier and stop: there is no hot
    # path to unlock because it is already running.
    if active_embedder_tier().get("native"):
        print(
            f"\n[rust-core] No prebuilt wheel available for {choice.package} "
            f"{M3_CORE_RS_VERSION} on this Python.\n"
            f"            PyPI returned exit {pypi_rc}; "
            f"GitHub Release returned exit {release_rc}.\n"
            f"\n"
            f"{native_core_outcome_note(indent='            ')}\n",
            file=sys.stderr,
        )
        return

    print(
        f"\n[rust-core] No prebuilt wheel available for {choice.package} "
        f"{M3_CORE_RS_VERSION} on this Python.\n"
        f"            PyPI returned exit {pypi_rc}; "
        f"GitHub Release returned exit {release_rc}.\n"
        f"\n"
        f"{oxidation_fallback_note(indent='            ')}\n"
        f"\n"
        f"            To unlock the native hot path, build your own wheel.\n"
        f"            Full guide: docs/BUILD_WHEELS.md "
        f"(or crates/m3-core-py/build_wheel.py in the m3-core-rs repo).\n"
        f"\n"
        f"            1. Install prerequisites:\n"
        f"                 macOS:          xcode-select --install && brew install cmake\n"
        f"                 Debian/Ubuntu:  sudo apt install cmake build-essential\n"
        f"                 Fedora/RHEL:    sudo dnf install cmake gcc-c++\n"
        f"                 Arch:           sudo pacman -S cmake base-devel\n"
        f"                 Windows:        install Visual Studio Build Tools (C++ workload)\n"
        f"\n"
        f"            2. Install the Rust toolchain (if not already present):\n"
        f"                 {install_rust_cmd}\n"
        f"                 source \"$HOME/.cargo/env\"\n"
        f"\n"
        f"            3. Run the source-build path explicitly:\n"
        f"                 m3 embedder install-gpu\n"
        f"\n"
        f"            (Will build {choice.package} from "
        f"{M3_CORE_RS_GIT_TAG} with features: {feats}.)\n",
        file=sys.stderr,
    )


# GitHub Release fallback — owner/repo for the published-wheels release.
# Kept module-level so tests can monkeypatch and so a fork can override
# without touching the install logic.
M3_CORE_RS_GH_REPO = "skynetcmd/m3-core-rs"


def _emit_download_progress(downloaded: int, total: int, *, tty: bool) -> None:
    """One heartbeat of wheel-download progress.

    On a TTY this REWRITES A SINGLE LINE via carriage return. These wheels run
    244 MB (windows-cuda) to 949 MB (linux-cuda) and the old code printed a
    fresh line every 10 MiB — roughly 95 lines of scroll for one download,
    which buries whatever setup said before it.

    Off a TTY (CI, a redirected log) it prints discrete lines instead: a
    carriage return in a log file collapses the whole download into one
    unreadable smear.

    Never raises — progress reporting must not be able to break a download.
    """
    mib = downloaded / (1024 * 1024)
    if total > 0:
        pct = min(100.0, downloaded * 100.0 / total)
        filled = int(pct // 5)  # 20-cell bar
        bar = "#" * filled + "-" * (20 - filled)
        msg = (f"[rust-core]   [{bar}] {pct:5.1f}%  "
               f"{mib:,.0f}/{total / (1024 * 1024):,.0f} MiB")
    else:
        # No Content-Length: report what is known rather than a fake percentage.
        msg = f"[rust-core]   {mib:,.0f} MiB"
    try:
        if tty:
            # Pad to a fixed width: the previous line may have been longer
            # (1,000 -> 999 MiB), and its tail would otherwise linger.
            sys.stderr.write("\r" + msg.ljust(78))
            sys.stderr.flush()
        else:
            print(msg, file=sys.stderr)
    except Exception:  # noqa: BLE001 — never fail a download writing to stderr
        pass


def _install_log_path() -> "pathlib.Path | None":
    """Where the rust-core install/upgrade history is appended, or None.

    Same resolution the embed watchdog uses (`m3_embed_watchdog._log_path`):
    the SDK config root when importable, else the documented
    M3_CONFIG_ROOT > M3_MEMORY_ROOT/config > ~/.m3/config precedence, with
    macOS logs under ~/Library/Logs by platform convention.
    """
    import pathlib
    _Path = pathlib.Path
    try:
        try:
            from m3_sdk import get_m3_config_root
            config_root = _Path(get_m3_config_root())
        except Exception:  # noqa: BLE001 - half-upgraded venv; fall back
            master = os.environ.get("M3_MEMORY_ROOT")
            config_root = _Path(
                os.environ.get("M3_CONFIG_ROOT")
                or (os.path.join(master, "config") if master else "")
                or os.path.expanduser("~/.m3/config")
            )
        if _os_name() == "Darwin":
            return _Path(os.path.expanduser("~/Library/Logs/m3_rust_core_install.log"))
        return config_root.parent / "logs" / "m3_rust_core_install.log"
    except Exception:  # noqa: BLE001 - logging must never break an install
        return None


def install_log(msg: str) -> None:
    """Append one UTC-stamped line to the rust-core install log.

    Why a file and not just the console: the console output of an upgrade is
    gone by the time anyone asks "when did this host move to 3.9.16, and did
    its wheel verify?". A digest mismatch in particular is an after-the-fact
    investigation -- the terminal that showed it has long since scrolled away.

    ⚠ Best-effort by construction. A read-only or missing log directory must
    never fail an install that would otherwise succeed, so every error here is
    swallowed. This is the one place in the module where silence is correct:
    the same facts have already gone to stdout/stderr, so nothing is lost --
    only the durable copy.
    """
    path = _install_log_path()
    if path is None:
        return
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass


def _fetch_release_sha256sums(assets: list, *, git_tag: str) -> "dict[str, str] | None":
    """Digests from the Release's ``SHA256SUMS`` asset, or None if absent.

    Returns ``{basename: hexdigest}``. None means the manifest is not published
    for this release, which is NOT an error: releases before v2026.9.16 predate
    it and a rollback to one must still install. The caller warns and continues;
    only a MISMATCH is fatal.
    """
    import urllib.error
    import urllib.request

    entry = next((a for a in assets if str(a.get("name", "")) == "SHA256SUMS"), None)
    if entry is None:
        return None
    url = str(entry.get("browser_download_url", ""))
    # Same scheme pin as the wheel download: this URL is external data.
    if not url.lower().startswith("https://"):
        print(f"[rust-core] refusing non-https SHA256SUMS URL ({url!r}); "
              f"skipping digest verification", file=sys.stderr)
        return None
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # nosec B310 - https validated
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[rust-core] could not fetch SHA256SUMS for {git_tag} "
              f"({type(e).__name__}: {e}); skipping digest verification",
              file=sys.stderr)
        return None

    out: "dict[str, str]" = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # GNU coreutils format: "<hex>  <name>" (text) or "<hex> *<name>"
        # (binary). Our generator emits the binary marker; accept both, and key
        # on the basename so a path prefix cannot confuse the lookup.
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].lstrip("*").strip()
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            out[os.path.basename(name)] = digest
    return out or None


def _download_wheel_once(wheel_url: str, wheel_path: str) -> "tuple[int, str]":
    """Stream ``wheel_url`` to ``wheel_path``; return (bytes_written, sha256).

    The digest is computed from the same chunks that are written, so it costs
    one pass rather than re-reading a file that can be ~980 MiB.

    Raises urllib/OS errors to the caller, which decides whether to retry.
    """
    import hashlib
    import urllib.request

    h = hashlib.sha256()
    downloaded = 0
    resp = urllib.request.urlopen(wheel_url, timeout=300)  # nosec B310 - https validated by caller
    with resp, open(wheel_path, "wb") as out:
        chunk_size = 1024 * 1024            # 1 MiB
        next_progress = 10 * 1024 * 1024    # heartbeat every 10 MiB
        # getattr, not resp.headers: not every response object exposes headers
        # (urllib's do; wrappers and test doubles may not), and a missing
        # Content-Length is already a supported case. Progress reporting must
        # never be able to fail the download it is describing.
        try:
            _hdrs = getattr(resp, "headers", None)
            total_bytes = int((_hdrs.get("Content-Length") if _hdrs else 0) or 0)
        except (AttributeError, TypeError, ValueError):
            total_bytes = 0
        try:
            _tty = sys.stderr.isatty()
        except Exception:  # noqa: BLE001 - an odd stream is "not a tty"
            _tty = False
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            downloaded += len(chunk)
            if downloaded >= next_progress:
                _emit_download_progress(downloaded, total_bytes, tty=_tty)
                next_progress += 10 * 1024 * 1024
        if _tty and downloaded:
            # Close the in-place line so later output starts clean.
            print(file=sys.stderr)
    return downloaded, h.hexdigest()


def install_from_github_release(
    choice: BackendChoice, *,
    version: str = M3_CORE_RS_VERSION,
    git_tag: str = M3_CORE_RS_GIT_TAG,
    repo: str = M3_CORE_RS_GH_REPO,
) -> int:
    """Try to install the matching prebuilt wheel from the GitHub Release.

    Sits between install_prebuilt (PyPI) and install_from_source. The
    GitHub Release is the canonical home for wheels too large for PyPI's
    100 MiB cap (Linux CUDA static build is 464 MB) and a defensive
    fallback for every other backend when PyPI is missing the right
    version. Public release only — unauthenticated GitHub API; draft
    releases are invisible here by design.

    Wheel naming convention (set by m3-core-rs/crates/m3-core-py/build_wheel.py):
        m3_core_rs_<os>_<backend>-<version>-cp<py>-cp<py>-<platform_tag>.whl
    The platform tag varies per backend (manylinux_2_17 for Linux CPU,
    bare linux_x86_64 for Linux CUDA static, macosx_*_arm64, win_amd64, ...).
    We match by the deterministic prefix — `m3_core_rs_<os>_<backend>-<ver>-cp<py>-`
    — and pick the single asset that starts with it. If multiple match,
    pick the first (sorted) so the choice is deterministic across runs.

    Returns 0 on success, non-zero on any failure (API miss, no asset,
    download failure, pip rejection). On non-zero the caller falls through
    to install_from_source.
    """
    import json
    import tempfile
    import urllib.error
    import urllib.request

    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    asset_prefix = f"m3_core_rs_{choice.os_tok}_{choice.backend}-{version}-{py_tag}-"

    print(f"[rust-core] looking for GitHub Release asset matching "
          f"{asset_prefix}*.whl  (repo={repo}, tag={git_tag})")

    api_url = f"https://api.github.com/repos/{repo}/releases/tags/{git_tag}"
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "m3-memory-installer",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - static https GitHub API URL
            release = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[rust-core] release {git_tag} not found on GitHub "
                  f"(may be draft or not yet published); skipping Release fallback",
                  file=sys.stderr)
        else:
            print(f"[rust-core] GitHub API HTTP {e.code} fetching {git_tag}; "
                  f"skipping Release fallback", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[rust-core] GitHub API fetch failed ({type(e).__name__}: {e}); "
              f"skipping Release fallback", file=sys.stderr)
        return 1

    assets = release.get("assets") or []
    matches = sorted(
        (a for a in assets if str(a.get("name", "")).startswith(asset_prefix)),
        key=lambda a: a["name"],
    )
    if not matches:
        print(f"[rust-core] no Release asset matches {asset_prefix}*.whl "
              f"({len(assets)} assets in {git_tag})", file=sys.stderr)
        return 1

    asset = matches[0]
    wheel_url = asset["browser_download_url"]
    wheel_name = asset["name"]
    wheel_size = asset.get("size", 0)

    # The download URL comes from the GitHub API response, i.e. external data.
    # Pin the scheme to https before opening so a tampered/unexpected response
    # can't redirect the installer to file:// (local-file read) or a plaintext
    # http downgrade. Fail loud rather than fetch an untrusted scheme.
    if not wheel_url.lower().startswith("https://"):
        print(f"[rust-core] refusing non-https asset URL ({wheel_url!r}); "
              f"skipping Release fallback", file=sys.stderr)
        return 1

    print(f"[rust-core] downloading {wheel_name} "
          f"({wheel_size / (1024*1024):.1f} MiB)...")

    # Download into a temp DIR, keeping the original filename: pip parses
    # the wheel filename per PEP 427 to identify the package, so the file
    # must be named e.g. m3_core_rs_macos_metal-3.6.27-cp314-cp314-macosx_11_0_arm64.whl
    # — a random NamedTemporaryFile path like /tmp/tmpXXXX.whl is rejected
    # by pip with "Invalid wheel filename (wrong number of parts)".
    tmp_dir = tempfile.mkdtemp(prefix="m3-core-rs-")
    wheel_path = os.path.join(tmp_dir, wheel_name)
    try:
        # Digests for this release, if it publishes them. Fetched ONCE, outside
        # the retry loop: a re-download must be checked against the same
        # manifest, or a retry could "pass" against a freshly-fetched bad one.
        sums = _fetch_release_sha256sums(assets, git_tag=git_tag)
        expected = (sums or {}).get(wheel_name)
        if sums is None:
            print(f"[rust-core] note: {git_tag} publishes no SHA256SUMS asset; "
                  f"installing without digest verification (releases before "
                  f"v2026.9.16 predate it)", file=sys.stderr)
        elif expected is None:
            print(f"[rust-core] note: SHA256SUMS for {git_tag} has no entry for "
                  f"{wheel_name}; installing without digest verification",
                  file=sys.stderr)

        # Two attempts. The failures this guards against -- a truncated body or
        # bits flipped in transit -- are transient, so a second fetch usually
        # succeeds. A repeat failure means the published asset itself is bad,
        # and no number of retries will fix that.
        attempts = 2
        downloaded = 0
        for attempt in range(1, attempts + 1):
            try:
                downloaded, actual = _download_wheel_once(wheel_url, wheel_path)
            except (urllib.error.URLError, OSError) as e:
                print(f"[rust-core] wheel download failed "
                      f"({type(e).__name__}: {e})", file=sys.stderr)
                install_log(f"code=wheel_download_failed wheel={wheel_name} "
                            f"tag={git_tag} error={type(e).__name__}: {e} "
                            f"attempt={attempt} of {attempts}")
                if attempt < attempts:
                    print(f"[rust-core] retrying download "
                          f"({attempt + 1} of {attempts})...", file=sys.stderr)
                    continue
                return 1

            if downloaded == 0:
                print("[rust-core] wheel download yielded 0 bytes", file=sys.stderr)
                install_log(f"code=wheel_empty_download wheel={wheel_name} "
                            f"tag={git_tag} attempt={attempt} of {attempts}")
                if attempt < attempts:
                    print(f"[rust-core] retrying download "
                          f"({attempt + 1} of {attempts})...", file=sys.stderr)
                    continue
                return 1

            # Size check before the digest: it is free, and it names the
            # specific failure. pip would reject a truncated wheel anyway, but
            # with an opaque "Wheel ... is invalid" that sends the operator
            # looking at the wrong thing.
            if wheel_size and downloaded != wheel_size:
                print(f"[rust-core] size mismatch, download is incomplete: "
                      f"code=wheel_size_mismatch wheel={wheel_name} "
                      f"expected_bytes={wheel_size} actual_bytes={downloaded}",
                      file=sys.stderr)
                install_log(f"code=wheel_size_mismatch wheel={wheel_name} "
                            f"tag={git_tag} expected_bytes={wheel_size} "
                            f"actual_bytes={downloaded} attempt={attempt} of {attempts}")
                if attempt < attempts:
                    print(f"[rust-core] retrying download "
                          f"({attempt + 1} of {attempts})...", file=sys.stderr)
                    continue
                return 1

            if expected is None:
                break  # nothing to compare against; size check already passed

            if actual == expected:
                # Report the SUCCESS too, not just failures: a silent pass is
                # indistinguishable from a check that never ran, and this is
                # the line that tells an operator the wheel they installed is
                # byte-identical to the published one. Short digest prefix --
                # enough to eyeball against SHA256SUMS, not a wall of hex.
                print(f"[rust-core] sha256 OK: {wheel_name} matches the digest "
                      f"published in {git_tag} SHA256SUMS "
                      f"(sha256={actual[:16]}..., {downloaded} bytes)")
                install_log(f"sha256 OK wheel={wheel_name} tag={git_tag} "
                            f"sha256={actual} bytes={downloaded} attempt={attempt}")
                break

            # ⚠ A mismatch is NOT proof of tampering and must not be reported as
            # one: in-flight corruption is far likelier, which is exactly why we
            # retry. Say what was observed and let the repeat decide.
            print(f"[rust-core] sha256 MISMATCH, the download does not match the "
                  f"digest published for this release: "
                  f"code=wheel_sha256_mismatch wheel={wheel_name} "
                  f"expected={expected} actual={actual}", file=sys.stderr)
            install_log(f"code=wheel_sha256_mismatch wheel={wheel_name} "
                        f"tag={git_tag} expected={expected} actual={actual} "
                        f"attempt={attempt} of {attempts}")
            if attempt < attempts:
                print(f"[rust-core] this usually means corruption in flight; "
                      f"re-downloading ({attempt + 1} of {attempts})...",
                      file=sys.stderr)
                continue
            print(f"[rust-core] digest still wrong after {attempts} downloads, "
                  f"so the published asset is likely bad rather than the "
                  f"transfer. NOT installing it. Verify by hand with:\n"
                  f"    gh release download {git_tag} --repo {repo} "
                  f"--pattern SHA256SUMS --pattern {wheel_name}\n"
                  f"    sha256sum -c --ignore-missing SHA256SUMS",
                  file=sys.stderr)
            # Remove it so a corrupt wheel cannot be picked up by anything else.
            try:
                os.unlink(wheel_path)
            except OSError:
                pass
            return 1

        print(f"[rust-core] downloaded {downloaded / (1024*1024):.1f} MiB; "
              f"installing via pip...")
        return _pip_install_with_pep668_fallback(
            "install", "--force-reinstall", "--no-deps", wheel_path,
        )
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass


def install_from_source(choice: BackendChoice, *,
                        git_tag: str = M3_CORE_RS_GIT_TAG) -> int:
    """Build m3-core-rs from the git source with the backend's Cargo features.

    Fallback when no prebuilt wheel matches. Requires a Rust toolchain
    (>=1.94) + maturin, and the backend's native toolchain (CUDA/Vulkan/Metal
    + a C/C++ compiler). Features are passed to maturin via pip's PEP 517
    config-settings — NOT the old M3_CORE_RS_BUILD_FEATURES env var, which the
    crate never read (latent no-op bug in the prior implementation).
    """
    # Pre-flight: check build tools before launching a multi-minute compile
    # that will fail with a cryptic Permission denied buried in 1000+ lines.
    missing = _check_build_tools()
    if missing:
        print(
            f"[rust-core] source build requires: {', '.join(missing)}\n"
            "  cmake + C++:\n"
            "    Debian/Ubuntu:  sudo apt install cmake build-essential\n"
            "    Fedora/RHEL:    sudo dnf install cmake gcc-c++\n"
            "    Arch:           sudo pacman -S cmake base-devel\n"
            "    macOS:          xcode-select --install && brew install cmake\n"
            "  Rust toolchain:\n"
            "    All platforms:  curl --proto '=https' --tlsv1.2 -sSf "
            "https://sh.rustup.rs | sh -s -- -y\n"
            "    Then source the env: source \"$HOME/.cargo/env\"\n"
            "  If cmake/c++/cargo exists but gives 'Permission denied', the\n"
            "  binary is not executable by this user — ask an admin to fix\n"
            "  permissions or install the package for this user's distro.\n"
            # Toolchain advice above stands — the caller asked for a SOURCE
            # build, so naming the missing prerequisites is the answer. Only the
            # closing reassurance can be wrong: route it through the live-tier
            # probe so a host whose existing wheel still serves is not told its
            # embeds fell back to HTTP.
            f"{native_core_outcome_note(indent='  ')}",
            file=sys.stderr,
        )
        return 1

    url = (f"m3-core-rs @ git+https://github.com/skynetcmd/m3-core-rs.git"
           f"@{git_tag}#subdirectory=crates/m3-core-py")
    args = ["install", "--force-reinstall", "--no-deps", url]
    feats = choice.features
    if feats:
        # maturin reads build args from --config-settings build-args=...
        args += ["--config-settings", f"build-args=--features {','.join(feats)}"]
    print(f"[rust-core] building from source @ {git_tag} "
          f"(features={feats or '(none)'}); this needs Rust + a compiler")
    return _pip_install_with_pep668_fallback(*args)


def install_rust_core(os_tok: Optional[str] = None, *,
                      allow_source_fallback: bool = True,
                      backend: Optional[str] = None,
                      force: bool = False) -> int:
    """Top-level: detect backend, install prebuilt wheel, fall back to source.

    Args:
        os_tok: Override the OS token (windows/linux/macos). Defaults to
            auto-detection via host_os().
        allow_source_fallback: If False, fail instead of building from source
            when no prebuilt wheel matches this platform/Python.
        backend: Explicit backend override (cpu/cuda/vulkan/metal). Skips
            auto-detection entirely. Use when detection picks the wrong backend
            (e.g. Vulkan tools present but no Vulkan GPU).
        force: Reinstall even if the target version is already present. By
            default a host that already has the embedded wheel at the target
            version is left untouched (no redundant 100+ MB re-download).

    Returns 0 on success, non-zero otherwise. Used by the wizard and the
    `m3 embedder install-gpu` CLI command.
    """
    # Skip-if-current: if the embedded native wheel is already installed at the
    # target version, there's nothing to do — avoid re-downloading a large wheel
    # on every `m3 setup` / `m3 update`. Backend cannot be told apart from the
    # PyPI version alone, so an explicit --backend override always proceeds (the
    # user may be switching cpu<->cuda). `force` always proceeds too.
    if not force and backend is None and is_rust_core_current():
        cur = active_embedder_tier()
        print(f"[rust-core] already current: {cur['summary']} — skipping install "
              "(use --force to reinstall).")
        return 0

    if backend is not None:
        os_tok = os_tok or host_os()
        if (os_tok, backend) not in _VALID:
            valid_backends = [b for o, b in _VALID if o == os_tok]
            print(
                f"[rust-core] invalid backend '{backend}' for {os_tok}. "
                f"Valid options: {', '.join(sorted(valid_backends))}",
                file=sys.stderr,
            )
            return 2
        choice = BackendChoice(os_tok, backend, "explicit --backend override")
        # An explicit override is an INSTRUCTION, not a preference: silently
        # installing a different backend than the one named would defeat the
        # flag's only purpose. No chain here — this backend or nothing.
        chain = [choice]
        print(f"[rust-core] backend override: {choice.package}")
    else:
        chain = detect_backend_chain(os_tok)
        choice = chain[0]
        print(f"[rust-core] detected backend: {choice.package} ({choice.reason})")
        if len(chain) > 1:
            print("[rust-core] fallback order: "
                  + " -> ".join(c.backend for c in chain))

    if (choice.os_tok, choice.backend) not in _VALID:
        print(f"[rust-core] unsupported combination "
              f"{choice.os_tok}-{choice.backend}", file=sys.stderr)
        return 2

    # Three-tier install cascade — GITHUB RELEASE FIRST.
    #
    #   1. GitHub Release prebuilt — the canonical, COMPLETE channel: every
    #      release carries all 7 backends x 4 interpreters, and it is the only
    #      channel for the size-capped CUDA wheels (windows-cuda ~244 MiB,
    #      linux-cuda ~949 MiB against a 100 MB per-file limit).
    #   2. pip prebuilt — PyPI or pip's local wheel cache.
    #   3. Source build — last resort, needs Rust + cmake + C++ + backend SDK.
    #
    # WHY THE RELEASE COMES FIRST (changed 2026-07-31). PyPI is not merely
    # incomplete, it is STALE: every `publish` job has failed trusted-publishing
    # exchange with `invalid-publisher` since 3.7.4, so all five PyPI-eligible
    # projects still serve a 2026-07-04 build while 3.7.25/.27/.28/.29/.31 each
    # shipped a complete 28-asset Release. Trying PyPI first therefore meant
    # either installing a months-old core (pip exits 0 — a SUCCESS, so the
    # cascade stops and never reaches the good wheel) or paying a doomed network
    # round-trip before falling through. Version-pinning the pip install does not
    # save it: `==3.7.31` simply 404s on PyPI, so the fast path is a guaranteed
    # miss for every backend.
    #
    # Ordering by "which channel actually has the artifact" rather than by
    # "which is nominally fastest" also fixes the CUDA case, where the pip hop
    # was always a wasted attempt against a package that 404s by design.
    #
    # This is not a workaround to unwind once PyPI is fixed: the Release is
    # complete by construction for all 7 backends, while PyPI can never carry
    # CUDA. Keep the Release first.
    # Recorded BEFORE the cascade so the log line reads as a transition
    # ("3.9.7 -> 3.9.16") rather than a bare end state. None means no native
    # core was importable, i.e. this is a first install rather than an upgrade.
    _before = installed_rust_core_version()
    install_log(f"install start: package={choice.package} "
                f"target={M3_CORE_RS_VERSION} tag={M3_CORE_RS_GIT_TAG} "
                f"from_version={_before or '(none)'} reason={choice.reason}")

    # Both prebuilt channels are tried for EVERY backend in the chain before
    # giving up on prebuilts entirely. A missing wheel for the best backend is
    # a PUBLISHING gap, not a statement about this host's hardware: the box
    # that cannot get a CUDA wheel still runs the Vulkan or CPU wheel in-process
    # at full native speed. Falling through to pure-Python because one wheel was
    # unpublished discards a working hot path for no reason.
    rc_gh = rc = 1
    installed = choice  # the backend that actually landed; may differ from choice
    for attempt in chain:
        installed = attempt
        if attempt is not choice:
            print(f"[rust-core] falling back to {attempt.package} "
                  f"({attempt.reason})", file=sys.stderr)
        rc_gh = install_from_github_release(attempt)
        if rc_gh == 0:
            print(f"[rust-core] installed {attempt.package} {M3_CORE_RS_VERSION} "
                  f"(GitHub Release)")
            install_log(f"install OK: {_before or '(none)'} -> "
                        f"{M3_CORE_RS_VERSION} package={attempt.package} "
                        f"channel=github-release requested={choice.package}")
            return 0

        print(f"[rust-core] GitHub Release unavailable for {attempt.package} "
              f"(exit {rc_gh}); trying pip prebuilt.", file=sys.stderr)
        rc = install_prebuilt(attempt)
        if rc == 0:
            break
    if rc == 0:
        # Deliberately does NOT claim "PyPI". pip exits 0 just as happily from
        # its local cache without contacting PyPI at all, and install_prebuilt
        # returns only an exit code — the source is not observable here. Saying
        # "PyPI prebuilt" was therefore an assertion, not an observation, and it
        # was provably wrong for CUDA: `m3-core-rs-windows-cuda` is a 404 on
        # PyPI by design, yet a cache hit still printed "(PyPI prebuilt)" — a
        # misleading claim in exactly the place someone debugs distribution.
        print(f"[rust-core] installed {installed.package} {M3_CORE_RS_VERSION} "
              f"(prebuilt wheel via pip)")
        install_log(f"install OK: {_before or '(none)'} -> {M3_CORE_RS_VERSION} "
                    f"package={installed.package} channel=pip-prebuilt "
                    f"requested={choice.package}")
        return 0

    if not allow_source_fallback:
        _print_manual_build_recommendation(choice, pypi_rc=rc, release_rc=rc_gh)
        return rc_gh

    print(f"[rust-core] no prebuilt wheel available for {choice.package} "
          f"(Release={rc_gh}, PyPI={rc}); falling back to source build.",
          file=sys.stderr)
    rc_src = install_from_source(choice)
    if rc_src != 0:
        print(f"[rust-core] source build failed (exit {rc_src}). The CPU "
              f"embedder still serves embeddings; see docs/EMBED_DEPLOYMENT.md.",
              file=sys.stderr)
        install_log(f"install FAILED: package={choice.package} "
                    f"target={M3_CORE_RS_VERSION} release_rc={rc_gh} "
                    f"pip_rc={rc} source_rc={rc_src} "
                    f"still_at={_before or '(none)'}")
    else:
        install_log(f"install OK: {_before or '(none)'} -> {M3_CORE_RS_VERSION} "
                    f"package={choice.package} channel=source-build")
    return rc_src
