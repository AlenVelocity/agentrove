import contextlib
import os
import signal
import subprocess
import sys


def terminate_process_tree(pid: int, *, force: bool) -> None:
    """Stop a child and everything it spawned.

    Children are started with start_new_session=True, so on Unix the pid is its
    own process-group leader and one signal reaches the whole tree. Windows has
    no process groups in that sense (and runs .cmd launchers through cmd.exe,
    which never forwards anything), so taskkill walks the tree by parent pid.
    """
    if sys.platform == "win32":
        _taskkill_tree(pid)
        return

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can return EPERM for a group mid-teardown; fall back to
        # signaling the leader directly (the pre-group-kill guarantee).
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)


def _taskkill_tree(pid: int) -> None:
    # /F always: without it taskkill posts WM_CLOSE, which console processes
    # ignore. A missing pid (already exited) is a non-zero exit we don't care about.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
        creationflags=creationflags,
    )
