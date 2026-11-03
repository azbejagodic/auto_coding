"""Fast checks before implementation, without importing or testing project code."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

from auto_coding.process_runner import bounded_run
from auto_coding.reviewer_permissions import prepare_review, check_cli, ReviewerEnvironmentError


TRUSTED_CHECKOUT = Path(__file__).resolve().parents[2]


class WorkflowPreflightError(ReviewerEnvironmentError):
    pass


_STORAGE_PROBE = r'''
import json, os, pathlib, sys, tempfile
expected = pathlib.Path(sys.argv[1]).resolve()
assert all(pathlib.Path(os.environ[key]).resolve() == expected for key in ("TEMP", "TMP", "TMPDIR")), "Temporary variables disagree"
assert os.environ.get("PYTHONPATH") == sys.argv[2], "Target import environment was not propagated"
assert pathlib.Path(tempfile.gettempdir()).resolve() == expected, "Python selected unexpected temporary storage"
with tempfile.TemporaryDirectory() as directory:
    assert pathlib.Path(directory).parent.resolve() == expected
    first = pathlib.Path(directory, "probe")
    first.write_text("preflight", encoding="utf-8")
    second = first.with_name("renamed")
    first.rename(second)
    second.unlink()
print(json.dumps({"storage": True, "target_environment": True}))
'''


def probe_controller_storage(repo):
    # Inspect Python's own candidate ordering without gettempdir() probing a
    # forbidden fallback CWD. Never create a probe in either source checkout.
    protected = (repo, TRUSTED_CHECKOUT.resolve())
    for key in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            directory = Path(value).resolve()
            if any(directory == root or directory.is_relative_to(root) for root in protected):
                raise WorkflowPreflightError(f"{key} points inside a source checkout; configure external temporary storage")
    candidates = [tempfile.tempdir] if tempfile.tempdir is not None else None
    if candidates is None:
        factory = getattr(tempfile, "_candidate_tempdir_list", None)
        if not callable(factory):
            raise WorkflowPreflightError("Cannot inspect Python temporary storage safely; configure an external TEMP/TMPDIR")
        candidates = factory()
    for candidate in candidates:
        directory = Path(os.fsdecode(candidate)).resolve()
        if any(directory == root or directory.is_relative_to(root) for root in protected):
            raise WorkflowPreflightError("Controller temporary storage resolves inside a source checkout; configure external TEMP/TMP/TMPDIR")
        try:
            with tempfile.TemporaryFile(dir=directory) as stream:
                stream.write(b"workflow preflight")
                stream.flush()
        except OSError:
            continue
        return
    raise WorkflowPreflightError("Controller Python temporary storage is unavailable; configure writable external TEMP/TMP/TMPDIR")


def probe_review_storage(spec, repo):
    command = [spec.executable, "sandbox"]
    if sys.platform in {"linux", "darwin"}:
        command.append("linux" if sys.platform == "linux" else "macos")
    elif sys.platform != "win32":
        raise WorkflowPreflightError("Native reviewer storage validation is unsupported on this platform")
    command.extend(["--permission-profile", spec.profile, "--include-managed-config", "--cd", str(repo)])
    for setting in spec.settings:
        command.extend(["-c", setting])
    # Isolated/no-site Python cannot execute target sitecustomize or import project code.
    command.extend(["--", sys.executable, "-I", "-S", "-c", _STORAGE_PROBE,
                    str(spec.scratch / "tmp"), spec.environment["PYTHONPATH"]])
    result = bounded_run(command, cwd=str(repo), env=spec.environment,
                         capture_output=True, text=True, timeout=15)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-800:]
        raise WorkflowPreflightError(f"Native reviewer temporary-storage probe failed (exit {result.returncode}): {detail}")
    try:
        if json.loads(result.stdout) != {"storage": True, "target_environment": True}:
            raise ValueError("Incomplete storage evidence")
    except ValueError as exc:
        raise WorkflowPreflightError("Native reviewer probe did not provide complete storage evidence") from exc


def workflow_preflight(repo, logs, git, *, check=check_cli, probe=probe_review_storage):
    """Caller already validated plan, Git root/branch/HEAD, clean new-run state and locks.

    Injected executors can omit CLI/native probes, as in B07. Normal execution
    checks the actual CLI and sandbox. No reusable approval is persisted.
    """
    repo = Path(repo).resolve(strict=True)
    if repo == TRUSTED_CHECKOUT.resolve():
        error = WorkflowPreflightError("Target is the trusted controller checkout. Select a separate regular clone on a feature branch")
        error.preflight_check = "target/controller separation"
        raise error
    spec = None
    stage = "controller temporary storage"
    try:
        probe_controller_storage(repo)
        stage = "reviewer compatibility"
        spec = prepare_review(None, repo, logs, git, check=check)
        stage = "target Python environment"
        roots = [Path(item).resolve() for item in spec.environment["PYTHONPATH"].split(os.pathsep) if item]
        if not roots or roots[0] != repo or any(not root.is_relative_to(repo) for root in roots):
            raise WorkflowPreflightError("Target Python import roots are missing or escape the selected checkout")
        stage = "temporary storage"
        with tempfile.TemporaryDirectory(dir=spec.scratch / "tmp") as directory:
            Path(directory, "write-probe").write_text("preflight", encoding="utf-8")
        if probe:
            probe(spec, repo)
    except BaseException as exc:
        exc.preflight_check = stage
        exc.preflight_correction = ("Use a separate regular checkout with contained Git metadata, the validated Codex CLI/configuration, "
                                    "and external writable run storage under a provisioned sandbox account")
        safe = (getattr(exc, "cleanup_confirmed", None) is True
                or getattr(exc, "process_started", None) is False
                or not (hasattr(exc, "process_started") or hasattr(exc, "cleanup_confirmed")))
        if spec is not None and safe:
            try:
                spec.cleanup()
            except (OSError, ReviewerEnvironmentError):
                pass  # Retain ownership/path ambiguity; never broaden ACLs to remove it.
        raise
    try:
        spec.cleanup()
    except (OSError, ReviewerEnvironmentError) as exc:
        raise WorkflowPreflightError("Preflight scratch could not be safely removed; inspect external run storage permissions") from exc
