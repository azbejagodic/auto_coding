"""Derived execution metrics. Never used for approval, retry, or Git decisions."""
from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
import tempfile


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
STARTS = {("Codex A", "process_started"): "codex_a",
          ("Codex B", "review_started"): "codex_b",
          ("tests", "test_started"): "tests"}
FINISHES = {("Codex A", "process_finished"): "codex_a",
            ("Codex B", "review_process_finished"): "codex_b",
            ("tests", "test_finished"): "tests"}
PROCESS_OUTCOMES = {"success", "failed", "timed_out", "interrupted"}
REVIEW_OUTCOMES = {"approved", "rejected", "process_failure", "timeout", "invalid_verdict",
                   "cleanup_unverified", "launch_failure", "output_limit"}


def valid_usage(value):
    if (not isinstance(value, dict)
            or any(type(value.get(key)) is not int or value[key] < 0 for key in TOKEN_FIELDS)
            or value["cached_input_tokens"] > value["input_tokens"]):
        return None
    return {key: value[key] for key in TOKEN_FIELDS}


def codex_usage(output: str):
    """Accept one complete fresh-exec turn, never text/nested or ambiguous usage.

    Documented format: https://learn.chatgpt.com/docs/non-interactive-mode
    These are CLI-reported counters, not billing or quota measurements.
    """
    threads = turns = 0
    completed = []
    for line in io.StringIO(output):
        if not line.lstrip().startswith("{"):
            continue  # Combined stderr can contain non-JSON diagnostics.
        if len(line) > 128 * 1024:
            return None
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return None
        kind = event.get("type") if isinstance(event, dict) else None
        if kind == "thread.started":
            if threads or turns or completed:
                return None
            threads += 1
        elif kind == "turn.started":
            if threads != 1 or turns or completed:
                return None
            turns += 1
        elif kind == "turn.failed":
            return None
        elif kind == "turn.completed":
            if threads != 1 or turns != 1 or completed:
                return None
            completed.append(valid_usage(event.get("usage")))
    return completed[0] if threads == turns == len(completed) == 1 else None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _bucket():
    return {"observed_calls": 0, "invocations": 0, "launch_failures": 0,
            "unconfirmed_invocations": 0, "duration_seconds": None,
            "duration_observations": 0, "outcomes": {}, "usage": None,
            "usage_observations": 0}


def _summary():
    return {"codex_a": _bucket(), "codex_b": _bucket(), "tests": _bucket(),
            "implementation_attempts": 0, "correction_attempts": 0,
            "review_retries": 0, "review_outcomes": {}}


def _increment(counts, key):
    counts[key] = counts.get(key, 0) + 1


def summarize_events(events):
    """Fold observations by execution identity; rebuilding never adds counters."""
    executions = {}
    run_id = None
    seq = 0
    for event in events:
        if (not isinstance(event, dict) or type(event.get("seq")) is not int
                or event["seq"] != seq + 1 or not isinstance(event.get("run_id"), str)):
            raise ValueError("Invalid metrics source timeline")
        if run_id is not None and event["run_id"] != run_id:
            raise ValueError("Metrics source run mismatch")
        run_id, seq = event["run_id"], event["seq"]
        actor_event = (event.get("actor"), event.get("event"))
        metadata = event.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("Invalid metrics metadata")
        task, attempt = event.get("task_id"), event.get("attempt")
        if actor_event in STARTS:
            executions[seq] = {"role": STARTS[actor_event], "task": task, "attempt": attempt,
                               "ordinal": metadata.get("review_invocation"),
                               "observation": None, "review": None}
        elif actor_event in FINISHES:
            role = FINISHES[actor_event]
            identity = metadata.get("execution_id")
            if identity is not None and type(identity) is not int:
                raise ValueError("Invalid execution identity")
            if identity is None:
                # Older timelines have no explicit pairing ID. The controller
                # executes processes sequentially; pair the last unmatched start.
                identity = next((key for key, item in reversed(executions.items())
                                 if item["role"] == role and item["task"] == task
                                 and item["attempt"] == attempt and item["observation"] is None), None)
            if identity not in executions:
                raise ValueError("Unpaired execution observation")
            item = executions[identity]
            if (item["role"], item["task"], item["attempt"]) != (role, task, attempt):
                raise ValueError("Execution identity mismatch")
            started = metadata.get("process_started")
            if "process_started" not in metadata:
                code = metadata.get("exit_code")
                started = True if type(code) is int and code != 127 else None
            if started is not None and type(started) is not bool:
                raise ValueError("Invalid launch observation")
            duration = metadata.get("duration_seconds")
            observation = {"started": started, "duration": duration if _number(duration) else None,
                           "outcome": event["outcome"] if event.get("outcome") in PROCESS_OUTCOMES else "unknown",
                           "usage": valid_usage(metadata.get("usage")) if role != "tests" else None}
            if item["observation"] is not None and item["observation"] != observation:
                raise ValueError("Conflicting execution observations")
            item["observation"] = observation
        elif actor_event in {("Codex B", "review_finished"), ("Codex B", "review_failed")}:
            identity = metadata.get("execution_id")
            if identity is not None and type(identity) is not int:
                raise ValueError("Invalid review identity")
            if identity is None:
                identity = next((key for key, item in reversed(executions.items())
                                 if item["role"] == "codex_b" and item["task"] == task
                                 and item["attempt"] == attempt), None)
            if identity in executions and event.get("outcome") in REVIEW_OUTCOMES:
                item = executions[identity]
                if item["role"] != "codex_b" or (item["task"], item["attempt"]) != (task, attempt):
                    raise ValueError("Review identity mismatch")
                if item["review"] is not None and item["review"] != event["outcome"]:
                    raise ValueError("Conflicting review observations")
                item["review"] = event["outcome"]

    total, tasks = _summary(), {}
    attempts = set()
    for item in executions.values():
        task = item["task"]
        if not isinstance(task, str):
            continue
        per_task = tasks.setdefault(task, _summary())
        observation = item["observation"]
        for summary in (total, per_task):
            bucket = summary[item["role"]]
            if observation is None:
                bucket["unconfirmed_invocations"] += 1
                continue
            bucket["observed_calls"] += 1
            _increment(bucket["outcomes"], observation["outcome"])
            if observation["started"] is True:
                bucket["invocations"] += 1
                if item["role"] == "codex_b" and type(item["ordinal"]) is int and item["ordinal"] > 1:
                    summary["review_retries"] += 1
            elif observation["started"] is False:
                bucket["launch_failures"] += 1
            else:
                bucket["unconfirmed_invocations"] += 1
            if observation["duration"] is not None:
                bucket["duration_observations"] += 1
                bucket["duration_seconds"] = round((bucket["duration_seconds"] or 0) + observation["duration"], 6)
            if observation["usage"] is not None and observation["started"] is True:
                bucket["usage_observations"] += 1
                if bucket["usage"] is None:
                    bucket["usage"] = {key: 0 for key in TOKEN_FIELDS}
                for key in TOKEN_FIELDS:
                    bucket["usage"][key] += observation["usage"][key]
            if item["review"] is not None:
                _increment(summary["review_outcomes"], item["review"])
        if (item["role"] == "codex_a" and observation is not None
                and observation["started"] is True and type(item["attempt"]) is int):
            attempts.add((task, item["attempt"]))
    total["implementation_attempts"] = len(attempts)
    total["correction_attempts"] = sum(attempt > 1 for _, attempt in attempts)
    for task, summary in tasks.items():
        summary["implementation_attempts"] = sum(key == task for key, _ in attempts)
        summary["correction_attempts"] = sum(key == task and attempt > 1 for key, attempt in attempts)
    return {"schema_version": 1, "run_id": run_id, "source_last_seq": seq, "run": total, "tasks": tasks}


def write_metrics(logs: Path):
    """Atomically replace a derived snapshot. Callers decide how to report I/O errors."""
    source = logs / "events.jsonl"
    if not source.is_file():
        return
    def events():
        with source.open(encoding="utf-8") as stream:
            while line := stream.readline(128 * 1024 + 1):
                if len(line) > 128 * 1024 or not line.endswith("\n"):
                    raise ValueError("Incomplete or oversized metrics source event")
                yield json.loads(line)
    summary = summarize_events(events())
    fd, name = tempfile.mkstemp(prefix="metrics-", suffix=".tmp", dir=logs)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(summary, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, logs / "metrics.json")
    finally:
        temporary.unlink(missing_ok=True)
