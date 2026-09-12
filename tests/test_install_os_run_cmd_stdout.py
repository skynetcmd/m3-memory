import subprocess
import sys
from unittest import mock

import pytest

from install_os import run_cmd

def test_run_cmd_preserves_stdout_on_windows(capsys):
    """CREATE_NO_WINDOW suppresses inherited stdout. STARTUPINFO/SW_HIDE does not."""
    if sys.platform != "win32":
        pytest.skip("Test is Windows-specific")
    
    # We test this by actually running a real subprocess (python -c "print('hello')")
    # and ensuring the output reaches capsys (which captures the parent's stdout).
    # Since run_cmd prints "Running: ...", we should see both the "Running" message
    # and the child process output.
    run_cmd([sys.executable, "-c", "print('hello_from_child')"], optional=False)
    
    captured = capsys.readouterr()
    assert "hello_from_child" in captured.out, (
        "Child process stdout did not reach the parent. "
        "If you used CREATE_NO_WINDOW, the pipe inheritance was broken."
    )
