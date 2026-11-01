"""Run every unittest and make platform coverage visible in GitHub Actions."""
from pathlib import Path
import os
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WINDOWS_PROCESS_TESTS = {
    "test_process_runner.ProcessRunnerTests.test_timeout_kills_windows_descendant_and_releases_gate",
    "test_process_runner.ProcessRunnerTests.test_parent_exit_does_not_hide_running_descendant",
    "test_process_runner.ProcessRunnerTests.test_failed_windows_job_assignment_keeps_gate",
}
WINDOWS_PID_TEST = "test_desktop_control.DesktopControlTests.test_windows_pid_probe_queries_without_os_kill"
WINDOWS_REQUIRED = WINDOWS_PROCESS_TESTS | {WINDOWS_PID_TEST}


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main():
    if sys.platform == "win32":
        # Hosted runners can expose TEMP as an 8.3 alias (RUNNER~1). Tests
        # compare stored paths with Path.resolve() results, so create their
        # temporary directories under the canonical spelling from the start.
        tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    names = {test.id() for test in cases(suite)}
    process_tests = {name for name in names if name.startswith("test_process_runner.ProcessRunnerTests.")}
    missing = WINDOWS_REQUIRED - names
    if len(process_tests) < 11 or missing:
        print(f"::error::Process regression coverage missing: {sorted(missing)}; found {len(process_tests)} tests")
        return 1

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    skipped = {test.id(): reason for test, reason in result.skipped}
    if sys.platform == "win32":
        allowed_skips = {"test_desktop_reader.DesktopReaderTests.test_symlinked_standard_logs_cannot_escape_root":
                         "symlinks unavailable"}
        windows_skipped = WINDOWS_REQUIRED & skipped.keys()
        if windows_skipped:
            print(f"::error::Windows process regressions were skipped: {sorted(windows_skipped)}")
    else:
        allowed_skips = {name: "Windows process-tree check" for name in WINDOWS_PROCESS_TESTS}
        allowed_skips[next(name for name in WINDOWS_PROCESS_TESTS if "assignment" in name)] = "Windows job assignment check"
        allowed_skips[WINDOWS_PID_TEST] = "Windows PID query"
        windows_skipped = WINDOWS_REQUIRED & skipped.keys()

    unexpected = {name: reason for name, reason in skipped.items()
                  if allowed_skips.get(name) != reason}
    for name, reason in skipped.items():
        print(f"CI skip: {name}: {reason}")
    for name, reason in unexpected.items():
        print(f"::error::Unexpected skip: {name}: {reason}")

    passed = result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)
    summary = (f"Backend unittest: {passed} passed, {len(result.failures)} failed, "
               f"{len(result.errors)} errors, {len(result.skipped)} skipped; "
               f"Windows-specific regressions: {len(WINDOWS_REQUIRED - windows_skipped)}/{len(WINDOWS_REQUIRED)} executed")
    print(summary)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as stream:
            stream.write(summary + "\n")
            for name, reason in skipped.items():
                stream.write(f"- Skipped `{name}`: {reason}\n")
    return 0 if (result.wasSuccessful() and not unexpected
                 and (sys.platform != "win32" or not windows_skipped)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
