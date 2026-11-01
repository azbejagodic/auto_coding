"""Bounded subprocess capture with conservative process-tree cleanup."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time


MAX_CAPTURE_BYTES = 16 * 1024 * 1024


class OutputLimitExceeded(OSError):
    """The complete subprocess output could not be retained."""


def _stop_tree(process: subprocess.Popen, readers: list[threading.Thread], job=None) -> bool:
    """Try to stop descendants and confirm that the output pipe has closed."""
    tree_stopped = False
    try:
        if sys.platform == "win32":
            if job is not None and job.assigned:
                tree_stopped = job.terminate_and_confirm()
            else:
                # A failed assignment gives no reliable ownership of children.
                # Try the legacy tree kill, but never treat its exit code as
                # proof that descendants have stopped.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
            tree_stopped = True
    except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
        pass
    if process.poll() is None and not tree_stopped:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        tree_stopped = False
    for reader in readers:
        reader.join(timeout=5)
    if sys.platform != "win32":
        # A descendant can create a new session and escape the process group.
        # Group disappearance alone cannot prove the full tree is quiescent.
        tree_stopped = False
    return tree_stopped and process.poll() is not None and all(not reader.is_alive() for reader in readers)


def bounded_run(argv, *, cwd=None, input=None, text=True, encoding="utf-8",
                errors="replace", stdout=None, stderr=None, capture_output=False,
                timeout=None, check=False, shell=False, env=None):
    """Subset of subprocess.run used by the backend; reject incomplete output."""
    if shell or not text or check or (capture_output and (stdout is not None or stderr is not None)):
        raise ValueError("Unsupported subprocess options")
    if not capture_output and stdout != subprocess.PIPE:
        raise ValueError("Output capture is required")
    if not capture_output and stderr != subprocess.STDOUT:
        raise ValueError("Combined output is required")

    combined = not capture_output
    output = bytearray()
    error_output = bytearray()
    excessive = threading.Event()
    stream_error = []
    stdin_file = tempfile.TemporaryFile() if input is not None else None
    if stdin_file is not None:
        stdin_file.write(input.encode(encoding, errors=errors))
        stdin_file.seek(0)
    job = None
    if sys.platform == "win32":
        from auto_coding.windows_job import WindowsJob
        try:
            job = WindowsJob()
        except BaseException:
            if stdin_file is not None:
                stdin_file.close()
            raise
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=stdin_file, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if combined else subprocess.PIPE,
            shell=False, env=env,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004) if sys.platform == "win32" else 0,
            start_new_session=sys.platform != "win32")
    except BaseException as exc:
        if stdin_file is not None:
            stdin_file.close()
        if job is not None:
            job.close()
        if isinstance(exc, OSError):
            # A missing executable never launched. Other launch errors may
            # occur after process creation and cannot establish quiescence.
            exc.cleanup_confirmed = isinstance(exc, FileNotFoundError)
            if not exc.cleanup_confirmed:
                from auto_coding.github_workspace import mark_process_uncertain
                mark_process_uncertain()
        else:
            from auto_coding.github_workspace import mark_process_uncertain
            mark_process_uncertain()
        raise

    capture_lock = threading.Lock()

    def drain(stream, target):
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                with capture_lock:
                    if len(output) + len(error_output) + len(chunk) > MAX_CAPTURE_BYTES:
                        excessive.set()
                        break
                    target.extend(chunk)
        except BaseException as exc:
            stream_error.append(exc)
        finally:
            stream.close()

    # A separate reader for each pipe avoids a child blocked on stderr while
    # the parent reads stdout. The combined size cap applies to both streams.
    streams = [(process.stdout, output)]
    if not combined:
        streams.append((process.stderr, error_output))
    readers = []
    try:
        if job is not None:
            job.assign_and_resume(process)
        for item in streams:
            reader = threading.Thread(target=drain, args=item, daemon=True)
            reader.start()
            readers.append(reader)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if excessive.is_set():
                raise OutputLimitExceeded("Subprocess output exceeded the capture limit")
            if stream_error:
                raise OSError("Subprocess output could not be read") from stream_error[0]
            if process.poll() is not None and all(not reader.is_alive() for reader in readers):
                if job is None or job.active() == 0:
                    break
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            time.sleep(0.01)
    except BaseException as exc:
        confirmed = _stop_tree(process, readers, job)
        if all(not reader.is_alive() for reader in readers):
            process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        exc.cleanup_confirmed = confirmed
        if not confirmed:
            from auto_coding.github_workspace import mark_process_uncertain
            mark_process_uncertain()
        raise
    finally:
        if stdin_file is not None:
            stdin_file.close()
        if job is not None:
            job.close()
    decode = lambda data: data.decode(encoding, errors=errors)
    return subprocess.CompletedProcess(argv, process.returncode, decode(output),
                                       None if combined else decode(error_output))
