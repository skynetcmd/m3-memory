"""Tests for governor_migration — detect + remove + privileged-command logic.

Subprocess calls (schtasks / crontab) are mocked so the tests are deterministic.

⚠ Mocking `subprocess` is NOT sufficient to isolate detection, and this
docstring used to claim it was ("never touch the real host scheduler"). Unix
detection has a SECOND path: the cognitive loop is a launchd agent / systemd
--user unit, found with `os.path.exists()` on the live service file. A test that
mocks only `subprocess.run` therefore reads the real machine, and
`test_detect_never_raises_without_scheduler` did exactly that — passing on hosts
without m3's loop installed and failing on hosts with it, i.e. on a correctly
configured box (measured 2026-09-30). Patch `_unix_service_paths` too when the
premise is "nothing is installed".
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import governor_migration as gm  # noqa: E402


def test_eligible_and_not_migratable_are_disjoint():
    eligible = set(gm.GOVERNOR_ELIGIBLE)
    not_mig = {n for n, _ in gm.NOT_MIGRATABLE}
    assert eligible.isdisjoint(not_mig)
    # The two known non-migratable tasks must be classified as such.
    assert "AgentOS_SecretRotator" in not_mig
    assert "AgentOS_CognitiveLoop" in not_mig


def test_all_three_categories_are_mutually_disjoint():
    """A task must be in AT MOST one category — eligible / floor / not-migratable.
    An overlap would either delete a task that should stay or vice-versa."""
    eligible = set(gm.GOVERNOR_ELIGIBLE)
    floor = {n for n, _ in gm.KEEP_SCHEDULED_FLOOR}
    not_mig = {n for n, _ in gm.NOT_MIGRATABLE}
    assert eligible.isdisjoint(floor)
    assert eligible.isdisjoint(not_mig)
    assert floor.isdisjoint(not_mig)


def test_hourlysync_is_a_kept_floor_not_eligible():
    """Regression guard: warehouse sync (AgentOS_HourlySync) must NEVER be in the
    removal set. It was deleted by `governor migrate` on the false premise the
    governor runs it, silently stopping sync. It is a scheduled floor."""
    floor = {n for n, _ in gm.KEEP_SCHEDULED_FLOOR}
    assert "AgentOS_HourlySync" in floor
    assert "AgentOS_HourlySync" not in gm.GOVERNOR_ELIGIBLE


def test_sync_runs_via_both_governor_and_scheduled_task_all_oses():
    """Requirement: sync runs via BOTH the governor loop pass AND the hourly
    scheduled task, by default, on every supported OS.
    - Governor path: the cognitive loop registers a 'sync' pass, default-on.
    - Scheduled floor: Windows schtasks + Unix crontab both invoke sync hourly.
    This test locks the dual-path default so neither half can silently regress."""
    import pathlib
    import re
    repo = pathlib.Path(__file__).resolve().parents[1]

    loop_src = (repo / "bin" / "m3_cognitive_loop.py").read_text(encoding="utf-8")
    # Governor path: a 'sync' pass exists in the registry and defaults ON
    # (gated by --skip-sync, which is a store_true flag => default False).
    assert '"name": "sync"' in loop_src
    assert 'run_sync_pass' in loop_src
    assert re.search(r'--skip-sync["\'],\s*action=["\']store_true', loop_src)

    # Scheduled floor — Unix: the rendered cron block runs sync hourly, and
    # macOS gets a launchd agent with the same hourly interval.
    import install_schedules
    cron = install_schedules.render_cron_block(str(repo))
    assert re.search(r'^0 \* \* \* \* .*sync_all\.py', cron, re.MULTILINE)
    hourly = next(s for s in install_schedules.unix_periodic_specs(str(repo))
                  if s["name"] == "AgentOS_HourlySync")
    assert install_schedules.launchd_trigger(hourly) == {"StartInterval": 3600}

    # Scheduled floor — Windows: install_schedules emits an AgentOS_HourlySync
    # task running sync_all.py, and it is the KEEP_SCHEDULED_FLOOR entry.
    sched = (repo / "bin" / "install_schedules.py").read_text(encoding="utf-8")
    assert "AgentOS_HourlySync" in sched
    assert "sync_all.py" in sched
    assert "AgentOS_HourlySync" in {n for n, _ in gm.KEEP_SCHEDULED_FLOOR}


def test_chatlog_embed_sweep_is_a_kept_floor_not_eligible():
    """Regression guard: the chatlog embed sweep must NEVER be in the removal set.

    Its work IS governor-appropriate (GPU backfill + spill drain, both periodic
    and interruptible) and the loop's 'embed' pass now runs both halves — but the
    scheduled entry is kept as a low-frequency FLOOR, exactly like
    AgentOS_HourlySync. Rationale: the governor HALTs background passes under
    sustained load, and a spilled turn lives ONLY as a JSONL line on disk until
    drained — not in any store, not searchable. Indefinite deferral of that is
    unbounded data at risk, so a rigid entry bounds the worst case.

    GOVERNOR_ELIGIBLE means "delete this scheduler entry", not "the loop runs
    it" — a floor task must never appear there (see HourlySync, which the loop
    also runs as a pass).

    History: `governor migrate` deleted this task on the premise the loop ran its
    work, when the loop ran only embed_backfill and nothing drained spill.
    Observed 2026-08-09: spill from 2026-07-14/17 undrained, last_sweeper_run_at
    frozen at 2026-07-31.
    """
    floor = {n for n, _ in gm.KEEP_SCHEDULED_FLOOR}
    assert "AgentOS_ChatlogEmbedSweep" in floor
    assert "AgentOS_ChatlogEmbedSweep" not in gm.GOVERNOR_ELIGIBLE


def test_loop_embed_pass_drains_spill():
    """The invariant GOVERNOR_ELIGIBLE documents: a task may only be retired into
    the loop if the loop genuinely executes its work.

    The loop's 'embed' pass must drain chatlog spill, not merely embed rows that
    are already in a store. Spilled rows are invisible to embed_backfill's
    _count_pending (they are not in any DB yet), so a pass that only embeds
    leaves them on disk forever.

    Asserts the MECHANISM rather than a name list, so it stays meaningful if the
    task is ever re-classified.
    """
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[1]
    loop_src = (repo / "bin" / "m3_cognitive_loop.py").read_text(encoding="utf-8")

    assert "_drain_chatlog_spill" in loop_src, (
        "the cognitive loop no longer defines a spill-drain step; the embed pass "
        "would silently stop recovering spilled chatlog turns"
    )
    assert "drain_spill" in loop_src, (
        "spill drainage must delegate to chatlog_embed_sweeper.drain_spill — the "
        "single implementation that honours each row's captured _db_path"
    )
    # The gate must consider spill too, or a spill-only backlog never schedules
    # the pass (the exact hole that let 2026-07-14/17 spill sit undrained).
    assert "def has_spill_work" in loop_src
    assert re.search(r"def has_embed_work.*?has_spill_work\(\)", loop_src, re.S), (
        "has_embed_work must consult has_spill_work, else a spill backlog with an "
        "empty embedding backlog leaves the embed pass unscheduled"
    )


def test_floor_and_not_migratable_excluded_from_eligible(monkeypatch):
    """When all canonical tasks are installed, the floor (HourlySync) and the
    not-migratable tasks (SecretRotator/CognitiveLoop) must be EXCLUDED from the
    removal set, and only the genuinely loop-covered tasks remain eligible."""
    monkeypatch.setattr(
        gm, "_list_installed_task_names",
        lambda: {
            "AgentOS_HourlySync", "AgentOS_ChatlogEmbedSweep",
            "AgentOS_ObservationDrain", "AgentOS_Maintenance",
            "AgentOS_WeeklyAuditor", "AgentOS_SecretRotator",
            "AgentOS_CognitiveLoop",
        })
    out = gm.detect_scheduled_tasks()
    assert set(out["eligible"]) == set(gm.GOVERNOR_ELIGIBLE)
    assert set(out["keep_scheduled_floor"]) == {n for n, _ in gm.KEEP_SCHEDULED_FLOOR}
    for nm in ("AgentOS_HourlySync", "AgentOS_ChatlogEmbedSweep"):
        assert nm in out["keep_scheduled_floor"]
        assert nm not in out["eligible"]
    for nm in ("AgentOS_SecretRotator", "AgentOS_CognitiveLoop"):
        assert nm not in out["eligible"]
        assert nm in out["not_migratable_present"]


def test_privileged_commands_windows(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    cmds = gm.privileged_removal_commands(["AgentOS_HourlySync", "AgentOS_Maintenance"])
    assert cmds == [
        'schtasks /Delete /TN "AgentOS_HourlySync" /F',
        'schtasks /Delete /TN "AgentOS_Maintenance" /F',
    ]


def test_privileged_commands_linux_uses_crontab(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Linux")
    cmds = gm.privileged_removal_commands(["AgentOS_HourlySync"])
    joined = "\n".join(cmds)
    assert "crontab -e" in joined
    # The HourlySync cron line is sync_all.py when rendered from the spec and
    # pg_sync.sh in crontabs older payloads wrote; removal must cover both.
    assert "pg_sync.sh" in joined
    assert "sync_all.py" in joined
    assert "sudo" in joined  # system-crontab hint present


def test_privileged_commands_macos_uses_crontab(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Darwin")
    cmds = gm.privileged_removal_commands(["AgentOS_Maintenance"])
    joined = "\n".join(cmds)
    assert "crontab -e" in joined
    assert "memory_maintenance.py" in joined


def test_hourlysync_is_detected_in_both_crontab_forms():
    # The rendered line runs sync_all.py; crontabs written by older payloads
    # run pg_sync.sh. Detection must see both or an existing floor goes missing.
    assert gm._cron_line_is("AgentOS_HourlySync",
                            "0 * * * * /v/bin/python /p/bin/sync_all.py --log-file /l/s.log")
    assert gm._cron_line_is("AgentOS_HourlySync", "0 * * * * /p/bin/pg_sync.sh >> /l/s.log 2>&1")
    assert not gm._cron_line_is("AgentOS_HourlySync", "*/30 * * * * /p/bin/chatlog_embed_sweeper.py")
    # Cognitive loop is NOT a cron marker (it's a service).
    assert "AgentOS_CognitiveLoop" not in gm._UNIX_CRON_MARKERS


def test_cognitive_loop_detected_as_service(monkeypatch, tmp_path):
    # On Linux the cognitive loop is a systemd unit; detection must find it by
    # service-file presence, not crontab.
    monkeypatch.setattr(gm, "_os_name", lambda: "Linux")
    svc = tmp_path / "m3-cognitive-loop.service"
    svc.write_text("[Unit]\n")
    monkeypatch.setattr(
        gm, "_unix_service_paths",
        lambda: {"AgentOS_CognitiveLoop": str(svc)},
    )

    class _R:
        returncode = 0
        stdout = ""  # empty crontab

    monkeypatch.setattr(gm.subprocess, "run", lambda *a, **k: _R())
    out = gm.detect_scheduled_tasks()
    assert "AgentOS_CognitiveLoop" in out["not_migratable_present"]
    assert out["eligible"] == []


def test_privileged_commands_empty_for_no_tasks():
    assert gm.privileged_removal_commands([]) == []


def test_detect_windows(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")

    class _R:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out

    def fake_run(cmd, **kw):
        # cmd = ["schtasks","/Query","/TN", name]
        name = cmd[-1]
        present = {"AgentOS_HourlySync", "AgentOS_SecretRotator"}
        return _R(0, name) if name in present else _R(1, "")

    monkeypatch.setattr(gm.subprocess, "run", fake_run)
    out = gm.detect_scheduled_tasks()
    # HourlySync is a scheduled FLOOR (governor-paced in-loop AND kept scheduled),
    # not eligible for removal — deleting it silently stopped warehouse sync.
    assert out["eligible"] == []
    assert out["keep_scheduled_floor"] == ["AgentOS_HourlySync"]
    assert out["not_migratable_present"] == ["AgentOS_SecretRotator"]


def test_remove_windows_partial_failure(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")

    class _R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = ""

    def fake_run(cmd, **kw):
        # Succeed for HourlySync, fail (privilege) for Maintenance.
        name = cmd[cmd.index("/TN") + 1]
        return _R(0 if name == "AgentOS_HourlySync" else 1)

    monkeypatch.setattr(gm.subprocess, "run", fake_run)
    removed, failed = gm.try_remove_scheduled_tasks(["AgentOS_HourlySync", "AgentOS_Maintenance"])
    assert removed == ["AgentOS_HourlySync"]
    assert failed == ["AgentOS_Maintenance"]


def test_remove_empty_is_noop():
    assert gm.try_remove_scheduled_tasks([]) == ([], [])


def test_detect_never_raises_without_scheduler(monkeypatch):
    """No scheduler at all -> every list empty, and no exception.

    "No scheduler" has TWO halves on Unix and this test used to simulate only
    one. Mocking `subprocess.run` covers `crontab -l`, but the cognitive loop is
    a launchd agent / systemd --user unit, so `_unix_installed_from_cron` also
    probes `os.path.exists()` on the REAL service paths. With that half
    unmocked the test read host state: on a Linux box where m3 is installed the
    way we recommend, `~/.config/systemd/user/m3-cognitive-loop.service` exists
    and detection correctly returned `AgentOS_CognitiveLoop`, failing a test
    whose premise is that nothing is installed.

    Measured 2026-09-30 on claude-dev, and it fails identically on the
    pre-change commit, so it is a latent isolation defect rather than a
    regression. It stayed hidden because the test pins `_os_name` to "Linux":
    macOS then checks the LINUX path (absent there, even though that host has
    the launchd plist), and a Linux box with no m3 install has nothing to find.
    So it only failed on a correctly-configured Linux host — the one
    configuration we most want green.
    """
    monkeypatch.setattr(gm, "_os_name", lambda: "Linux")

    def boom(*a, **k):
        raise FileNotFoundError("crontab")

    monkeypatch.setattr(gm.subprocess, "run", boom)
    # The other half: point service detection at paths that cannot exist, so
    # "no scheduler" is actually simulated instead of inherited from the host.
    # Capture the ORIGINAL first -- reading gm._unix_service_paths() from inside
    # the replacement calls the replacement (RecursionError; hit on the first
    # draft of this fix). Derive the key set from the real function so a newly
    # added service is covered automatically.
    real_paths = gm._unix_service_paths()
    monkeypatch.setattr(
        gm, "_unix_service_paths",
        lambda: {name: "/nonexistent/m3-test/%s.service" % name
                 for name in real_paths},
    )
    out = gm.detect_scheduled_tasks()
    assert out == {"eligible": [], "not_migratable_present": [], "keep_scheduled_floor": []}


def test_detect_reports_the_loop_when_its_service_file_EXISTS(monkeypatch, tmp_path):
    """The positive case, which nothing covered — so the detection path the bug
    above rode in on had no test of its own.

    Without this, isolating the path above could be "fixed" by breaking service
    detection entirely and both tests would still pass.
    """
    monkeypatch.setattr(gm, "_os_name", lambda: "Linux")
    monkeypatch.setattr(gm.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("crontab")))
    svc = tmp_path / "m3-cognitive-loop.service"
    svc.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(gm, "_unix_service_paths",
                        lambda: {"AgentOS_CognitiveLoop": str(svc)})

    out = gm.detect_scheduled_tasks()
    assert out["not_migratable_present"] == ["AgentOS_CognitiveLoop"], (
        "a present service file must still be detected; this is the behaviour "
        "the isolation fix must not break"
    )
    assert out["eligible"] == []


def test_service_filenames_match_what_the_INSTALLER_writes(monkeypatch):
    """Detection and installation must agree on the service FILENAME.

    The name is spelled in two places: `_unix_service_paths()` here (detection,
    via `os.path.exists`) and `install_schedules.py` (installation — it copies
    `bin/<name>` to the user's launchd/systemd directory under the same
    basename). Nothing tied them together, so renaming the unit on either side
    would leave detection looking for a file that is never written: `m3 doctor`
    and `m3 governor migrate` would report the cognitive loop as absent while it
    ran perfectly well, and no test would fail.

    This is the gap the two tests above do NOT cover — both patch
    `_unix_service_paths`, so they verify the detection LOGIC and say nothing
    about the string. Asserted against the template files actually present in
    `bin/`, which is the thing the installer copies, so a rename on either side
    breaks this test.

    Pure file existence, so it runs on every OS regardless of host state — the
    defect this file already had once was a test that read the live machine.
    """
    bin_dir = REPO / "bin"
    for os_name in ("Darwin", "Linux"):
        monkeypatch.setattr(gm, "_os_name", lambda _o=os_name: _o)
        paths = gm._unix_service_paths()
        assert paths, f"{os_name}: no service paths declared"
        for task, path in paths.items():
            template = bin_dir / os.path.basename(path)
            assert template.is_file(), (
                f"{os_name}: detection looks for {os.path.basename(path)!r} "
                f"(task {task}), but bin/ has no such template for the "
                f"installer to copy. Detection and install_schedules.py have "
                f"drifted apart; one of them was renamed."
            )


def test_windows_declares_no_unix_service_paths(monkeypatch):
    """Guards the branch shape: Windows tasks are schtasks entries, so the Unix
    service map must be empty there rather than falling through to a Linux path
    that can never exist on that host."""
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    assert gm._unix_service_paths() == {}


def test_not_migratable_lines_have_reasons():
    lines = gm.not_migratable_lines()
    assert len(lines) == len(gm.NOT_MIGRATABLE)
    assert all("—" in line for line in lines)  # name — reason format


# ── Windows legacy-action detection (hardening A) ──────────────────────────────
# A pre-bf110222 / hand-named task (e.g. `m3-memory-sync`) runs sync_all.py but
# is NOT named AgentOS_HourlySync, so the name-only query misses it. Detection
# must catch it by its ACTION and surface it under its REAL name for removal.

def _verbose_list(records: list[tuple[str, str]]) -> str:
    """Render a fake `schtasks /Query /FO LIST /V` blob from (TaskName, action)."""
    blocks = []
    for name, action in records:
        blocks.append(
            f"HostName:                             HOSTPC\n"
            f"TaskName:                             {name}\n"
            f"Task To Run:                          {action}\n"
        )
    return "\n".join(blocks)


def _win_run_factory(verbose_blob: str, name_present: set[str]):
    """Build a fake subprocess.run that answers BOTH query shapes:
    - /TN <name>            → present iff name in name_present
    - /FO LIST /V           → returns the verbose blob
    """
    class _R:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    def fake_run(cmd, **kw):
        if "/V" in cmd:
            return _R(0, verbose_blob)
        name = cmd[-1]  # /TN <name>
        return _R(0, name) if name in name_present else _R(1, "")

    return fake_run


def test_windows_detects_legacy_sync_task_by_action(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    # No canonical task installed by name; a legacy task runs sync_all.py.
    blob = _verbose_list([
        (r"\m3-memory-sync",
         r'powershell.exe -Command "& ...\.venv\Scripts\python.exe ...\bin\sync_all.py"'),
        (r"\Microsoft\Windows\SomethingElse", r"C:\Windows\system32\noop.exe"),
    ])
    monkeypatch.setattr(gm.subprocess, "run", _win_run_factory(blob, set()))
    out = gm.detect_scheduled_tasks()
    # Surfaced under its REAL name so `schtasks /Delete /TN m3-memory-sync` works.
    assert "m3-memory-sync" in out["eligible"]
    assert out["not_migratable_present"] == []


def test_windows_action_scan_skips_canonical_names(monkeypatch):
    # A task NAMED AgentOS_HourlySync running sync_all.py must NOT be double-listed
    # by the action scan — it's already found by the name-based query.
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    blob = _verbose_list([
        (r"\AgentOS_HourlySync", r'"...\pythonw.exe" "...\bin\sync_all.py"'),
    ])
    monkeypatch.setattr(
        gm.subprocess, "run",
        _win_run_factory(blob, {"AgentOS_HourlySync"}),
    )
    out = gm.detect_scheduled_tasks()
    # Canonical AgentOS_HourlySync is classified as the scheduled floor, not
    # eligible — and it appears EXACTLY once (found by name, not double-listed by
    # the action scan folding it into eligible under its reserved name).
    assert out["eligible"] == []
    assert out["keep_scheduled_floor"] == ["AgentOS_HourlySync"]


def test_windows_action_match_is_path_anchored_not_bare_substring(monkeypatch):
    # §3/§6 hardening: detection feeds `schtasks /Delete`, so the action match
    # must be path-anchored. A task that merely MENTIONS a script filename (not as
    # an invoked path) must NOT be matched and scheduled for deletion.
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    blob = _verbose_list([
        # bare mention in an argument — NOT an invocation, must be ignored
        (r"\unrelated-backup-job",
         r'C:\tools\backup.exe --note "remember to port sync_all.py settings"'),
    ])
    monkeypatch.setattr(gm.subprocess, "run", _win_run_factory(blob, set()))
    out = gm.detect_scheduled_tasks()
    assert "unrelated-backup-job" not in out["eligible"]
    assert out["eligible"] == []


def test_action_invokes_marker_path_anchored_helper():
    markers = ("sync_all.py", "m3_enrich.py")
    # Invoked by path (Windows or POSIX separator) -> match.
    assert gm._action_invokes_marker(r'"py" "C:\m3\bin\sync_all.py"', markers)
    assert gm._action_invokes_marker('python /opt/m3/bin/sync_all.py', markers)
    # Case-insensitive (Windows paths) -> match.
    assert gm._action_invokes_marker(r'"PY" "C:\M3\BIN\SYNC_ALL.PY"', markers)
    # Bare mention with no leading separator -> no match.
    assert not gm._action_invokes_marker('echo sync_all.py is the script', markers)
    # Different marker not present -> no match.
    assert not gm._action_invokes_marker(r'"py" "C:\m3\bin\other.py"', markers)


def test_windows_action_marker_is_sync_all_not_pg_sync_sh():
    # CROSS-OS TRAP GUARD: the Windows action invokes sync_all.py directly; the
    # Unix wrapper pg_sync.sh never appears in a Windows action. The two marker
    # maps MUST stay independent or HourlySync legacy tasks go undetected.
    assert gm._WINDOWS_ACTION_MARKERS["AgentOS_HourlySync"] == "sync_all.py"
    assert "pg_sync.sh" in gm._UNIX_CRON_MARKERS["AgentOS_HourlySync"]


def test_windows_legacy_detection_never_raises_without_schtasks(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")

    def boom(*a, **k):
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr(gm.subprocess, "run", boom)
    # Whole detect path must swallow it and return empty, not raise.
    assert gm.detect_windows_legacy_action_tasks() == set()
    assert gm.detect_scheduled_tasks() == {"eligible": [], "not_migratable_present": [], "keep_scheduled_floor": []}


def test_windows_legacy_does_not_reclassify_not_migratable(monkeypatch):
    # A hand-named task whose leaf collides with a NOT_MIGRATABLE name must not be
    # pulled into `eligible` (that would schedule a security task for removal).
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    blob = _verbose_list([
        (r"\AgentOS_SecretRotator", r'"...\pythonw.exe" "...\bin\secret_rotator.py"'),
    ])
    monkeypatch.setattr(
        gm.subprocess, "run",
        _win_run_factory(blob, {"AgentOS_SecretRotator"}),
    )
    out = gm.detect_scheduled_tasks()
    assert "AgentOS_SecretRotator" in out["not_migratable_present"]
    assert "AgentOS_SecretRotator" not in out["eligible"]


def test_leaf_task_name_strips_path():
    assert gm._leaf_task_name(r"\m3-memory-sync") == "m3-memory-sync"
    assert gm._leaf_task_name(r"\Microsoft\Windows\Foo\Bar") == "Bar"
    assert gm._leaf_task_name("PlainName") == "PlainName"
