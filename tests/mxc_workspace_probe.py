"""Opt-in real Windows MXC acceptance probe, separate from mocked unittests.

Run from the controller checkout: python tests/mxc_workspace_probe.py
Retains fixture and controller-owned evidence in external temporary storage.
Never substitutes host execution when MXC cannot start.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from auto_coding import controller_test_sandbox as sandbox
from auto_coding import simple_run as runner


PROBE = r'''
import os, pathlib, subprocess, sys, tempfile, unittest
ORIGINAL = pathlib.Path(__ORIGINAL__)
SIBLING = pathlib.Path(__SIBLING__)

class WorkspaceProbe(unittest.TestCase):
    def test_original_checkout_cannot_be_read_or_modified(self):
        with self.assertRaises(PermissionError):
            ORIGINAL.joinpath("guard.txt").read_text()
        with self.assertRaises(PermissionError):
            ORIGINAL.joinpath("guard.txt").write_text("forbidden")
        with self.assertRaises(PermissionError):
            ORIGINAL.joinpath(".git", "config").read_text()

    def test_sibling_host_files_and_controller_evidence_are_denied(self):
        with self.assertRaises(PermissionError):
            SIBLING.read_text()
        logs = pathlib.Path.cwd().parent.parent
        with self.assertRaises(PermissionError):
            logs.joinpath("controller-canary.txt").read_text()

    def test_source_copy_and_generated_files(self):
        self.assertEqual(pathlib.Path("guard.txt").read_text(), "approved bytes")
        pathlib.Path("generated.txt").write_text("generated in disposable copy")
        self.assertFalse(pathlib.Path(".git").exists())

    def test_temp_homes_caches_and_environment(self):
        root = pathlib.Path.cwd().parent
        for key in ("TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA",
                    "LOCALAPPDATA", "XDG_CACHE_HOME", "PIP_CACHE_DIR", "npm_config_cache"):
            path = pathlib.Path(os.environ[key])
            self.assertTrue(path.is_relative_to(root), key)
            path.joinpath("probe.txt").write_text("writable")
        self.assertNotIn("AUTOCODING_PROBE_SECRET", os.environ)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(pathlib.Path(directory).parent, pathlib.Path(os.environ["TEMP"]))

    def test_nested_python_and_git_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            subprocess.run(["git", "-C", directory, "-c", "user.name=Probe",
                            "-c", "user.email=probe@example.invalid", "-c", "commit.gpgsign=false",
                            "commit", "--allow-empty", "-qm", "isolated"], check=True)
            subprocess.run([sys.executable, "-I", "-c", "import tempfile; assert tempfile.gettempdir()"], check=True)

    def test_node_subprocess(self):
        result = subprocess.run(["node", "-e", "process.stdout.write('node-ok')"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout, "node-ok")
'''


def main():
    if sys.platform != "win32":
        print("Real MXC verification requires Windows", file=sys.stderr)
        return 1
    root = Path(tempfile.mkdtemp(prefix="autocoding-mxc-probe-")).resolve()
    repo, logs = root / "target", root / "evidence"
    repo.mkdir(); logs.mkdir()
    (repo / "guard.txt").write_text("approved bytes")
    sibling = root / "host-private.txt"; sibling.write_text("fixture canary")
    (logs / "controller-canary.txt").write_text("controller-only fixture")
    source = PROBE.replace("__ORIGINAL__", repr(str(repo))).replace("__SIBLING__", repr(str(sibling)))
    (repo / "test_probe.py").write_text(source, encoding="utf-8")
    def git(args):
        subprocess.run(["git", "-c", "core.hooksPath=" + str(root / "no-hooks"), *args],
                       cwd=repo, check=True, capture_output=True, text=True, timeout=30)
    git(["init", "-q"])
    git(["add", "."])
    git(["-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "probe snapshot"])
    print(f"Real MXC probe evidence: {logs}", flush=True)
    os.environ["AUTOCODING_PROBE_SECRET"] = "sanitization-canary"
    try:
        result = sandbox.run([sys.executable, "-B", "-m", "unittest", "discover", "-v"],
                             repo, logs, 120, git=runner.git, launch=runner.call)
    except Exception as error:
        print(f"MXC probe blocked: {type(error).__name__}: {error}", file=sys.stderr)
        print(f"cleanup_confirmed={getattr(error, 'cleanup_confirmed', None)}", file=sys.stderr)
        return 1
    finally:
        os.environ.pop("AUTOCODING_PROBE_SECRET", None)
    code, output, timed = result
    print(output)
    summary = runner.unittest_evidence(output)
    passed = (code == 0 and not timed and result.cleanup_confirmed is True and summary
              and summary["successful"] and summary["tests"] == 6 and not summary["skipped"]
              and (repo / "guard.txt").read_text() == "approved bytes"
              and not list(logs.glob("review-scratch-*")))
    print(json.dumps({"real_mxc": True, "passed": bool(passed), "exit_code": code,
                      "timed_out": timed, "cleanup_confirmed": result.cleanup_confirmed}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
