import subprocess
import sys
import time
from unittest.mock import patch


def test_supervisor_detects_wedge(tmp_path):
    script = """
import sys, time
print('{"t":"hb"}', flush=True)
time.sleep(10)
"""
    script_path = tmp_path / "wedged_child.py"
    script_path.write_text(script)

    import bin.m3_notification_waiter as waiter
    original_popen = subprocess.Popen

    class Args:
        agent_ids = ["test"]
        ack = False
        interval = 0.2
        timeout = 2.0
    args = Args()

    with patch("subprocess.Popen") as mock_popen:
        def popen_override(cmd, *a, **kw):
            return original_popen([sys.executable, str(script_path)], *a, **kw)
        mock_popen.side_effect = popen_override
        start = time.time()
        rc = waiter._supervise_postgres(args)
        duration = time.time() - start
        assert rc == 2
        assert duration < 8.0



def test_child_eof_self_terminates():
    import bin.m3_notification_waiter as waiter
    class Args:
        agent_ids = ["test"]
        ack = False
        interval = 1.0
        timeout = 5.0
    args = Args()

    with patch("sys.stdin.read", return_value=""):
        with patch("builtins.print") as mock_print:
            with patch("memory.orchestration.notifications_unread_ids_impl", return_value=[]):
                with patch("memory.db._db"):
                    rc = waiter.pg_child_loop(args)
                    assert rc == 0
                    mock_print.assert_any_call('{"t": "bye"}', flush=True)

def test_child_inband_stop():
    import bin.m3_notification_waiter as waiter
    class Args:
        agent_ids = ["test"]
        ack = False
        interval = 1.0
        timeout = 5.0
    args = Args()

    def mock_stdin_iter():
        yield "stop\n"

    with patch("sys.stdin", iter(mock_stdin_iter())):
        with patch("builtins.print") as mock_print:
            with patch("memory.orchestration.notifications_unread_ids_impl", return_value=[]):
                with patch("memory.db._db"):
                    rc = waiter.pg_child_loop(args)
                    assert rc == 0
                    mock_print.assert_any_call('{"t": "bye"}', flush=True)

