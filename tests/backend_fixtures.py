"""Explicit final-suite double for pre-existing task-flow regression fixtures.

These tests opt in to controller focused checks to retain their original safety
assertions. Final-gate failures and scheduling are tested with the real run API
in test_final_verification, without this helper.
"""
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from auto_coding import simple_run as runner


FINAL_POLICY = {"test_commands": [["fixture-final-suite"]], "minimum_tests": 1, "timeout_seconds": 1800}


@contextmanager
def approved_plan_fixture():
    """Mock only the B08 prerequisite in existing downstream A/B safety tests.

    The real authorization gate is exercised independently in test_plan_audit.
    Existing fixtures include legacy TXT plans and unborn disposable repositories.
    """
    with patch.object(runner.plan_audit, "authorize", return_value={"directory": "fixture-approved"}), \
            patch.object(runner.plan_audit, "verify_resume"):
        yield


def run_with_verification(plan, repo, logs, **kwargs):
    def no_real_executor(*args, **options):
        raise AssertionError("Regression fixtures must inject a fake executor; real Codex is forbidden")
    execute = kwargs.get("execute", no_real_executor)
    kwargs.setdefault("final_verification_config", FINAL_POLICY)
    kwargs.setdefault("controller_tests", True)
    def with_final(argv, cwd, timeout, **options):
        if argv == ["fixture-final-suite"]:
            # Fail the test if final verification is ever scheduled after an
            # uncertain process or before all independent approvals/commits.
            state = json.loads((Path(logs) / "state.json").read_text())
            manifest = json.loads((Path(logs) / "run-manifest.json").read_text())
            assert not any(state.get(key) for key in ("uncertain_test", "uncertain_process",
                "pending_execution", "pending_review", "pending_checkpoint")), "Unsafe final-suite scheduling"
            assert state["completed"] == manifest["task_ids"], "Final suite preceded task completion"
            records = [record for record in state["history"] if record.get("commit")]
            assert [record["task"] for record in records] == state["completed"]
            assert all(record["review"]["decision"] == "APPROVED" and not record["review"]["issues"]
                       for record in records), "Final suite preceded independent approval"
            if state.get("auto_push"):
                assert state.get("publication", {}).get("outcome") == "published"
            return runner._CallResult(0, "Ran 1 test in 0.001s\n\nOK\n", False, True, True)
        return execute(argv, cwd, timeout, **options)
    kwargs["execute"] = with_final
    with approved_plan_fixture():
        return runner.run(plan, repo, logs, **kwargs)
