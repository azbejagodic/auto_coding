import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from auto_coding.metrics import codex_usage, summarize_events, write_metrics


USAGE = {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 7}


def stream(usage=USAGE):
    return "\n".join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": "thread"},
        {"type": "turn.started"}, {"type": "turn.completed", "usage": usage}])


def event(seq, actor, name, *, task="one", attempt=1, outcome="success", metadata=None):
    return {"run_id": "run", "seq": seq, "actor": actor, "event": name,
            "task_id": task, "attempt": attempt, "outcome": outcome, "metadata": metadata or {}}


class MetricsTests(unittest.TestCase):
    def test_documented_usage_is_whitelisted_and_actual_zero_is_preserved(self):
        self.assertEqual(codex_usage("warning on stderr\n" + stream(USAGE | {"secret": "do not retain"})), USAGE)
        zero = {key: 0 for key in USAGE}
        self.assertEqual(codex_usage(stream(zero)), zero)

    def test_missing_malformed_nested_and_ambiguous_usage_remains_unknown(self):
        invalid = [None, {}, USAGE | {"input_tokens": True}, USAGE | {"output_tokens": -1},
                   USAGE | {"input_tokens": 1.5}, USAGE | {"cached_input_tokens": 101}]
        for usage in invalid:
            with self.subTest(usage=usage):
                self.assertIsNone(codex_usage(stream(usage)))
        for text in ("plain model text", '{"item":{"type":"turn.completed","usage":{}}}',
                     stream() + "\n" + stream(), stream() + "\n{torn",
                     stream() + '\n{"type":"turn.failed"}',
                     '{"type":"turn.completed","usage":' + json.dumps(USAGE) + "}",
                     '{"type":"item.completed","text":"' + "x" * (128 * 1024) + '"}\n' + stream()):
            with self.subTest(text=text[:50]):
                self.assertIsNone(codex_usage(text))

    def test_counts_are_derived_once_with_partial_usage_and_per_task_totals(self):
        observation = {"execution_id": 1, "exit_code": 0, "process_started": True,
                       "duration_seconds": 1.25, "usage": USAGE}
        events = [
            event(1, "Codex A", "process_started"),
            event(2, "Codex A", "process_finished", metadata=observation),
            event(3, "Codex A", "process_finished", metadata=observation),  # Duplicate observation.
            event(4, "Codex B", "review_started", metadata={"review_invocation": 2}),
            event(5, "Codex B", "review_process_finished", metadata={
                "execution_id": 4, "exit_code": 0, "process_started": True, "duration_seconds": 2}),
            event(6, "Codex B", "review_failed", outcome="invalid_verdict", metadata={"execution_id": 4}),
            event(7, "Codex A", "process_started", task="two", attempt=2),
            event(8, "Codex A", "process_finished", task="two", attempt=2, metadata={
                "execution_id": 7, "exit_code": 0, "process_started": True, "duration_seconds": 3}),
        ]
        summary = summarize_events(events)
        self.assertEqual(summary["run"]["codex_a"]["invocations"], 2)
        self.assertEqual(summary["run"]["codex_a"]["duration_seconds"], 4.25)
        self.assertEqual(summary["run"]["codex_a"]["usage"], USAGE)
        self.assertEqual(summary["run"]["codex_a"]["usage_observations"], 1)
        self.assertIsNone(summary["run"]["codex_b"]["usage"])
        self.assertEqual(summary["run"]["review_retries"], 1)
        self.assertEqual(summary["run"]["correction_attempts"], 1)
        self.assertEqual(summary["run"]["review_outcomes"], {"invalid_verdict": 1})
        self.assertEqual(summary["tasks"]["one"]["codex_a"]["invocations"], 1)
        self.assertEqual(summary["tasks"]["two"]["codex_a"]["invocations"], 1)
        self.assertEqual(summary, summarize_events(events))
        for item in events:
            if item["event"] in {"process_started", "review_started"}:
                item["metadata"].update(requested_model="fixture-model", model_source="controller_override",
                                        reasoning_effort="unknown")
        self.assertEqual(summary, summarize_events(events))

    def test_legacy_and_incomplete_records_do_not_invent_duration_usage_or_launches(self):
        events = [event(1, "Codex A", "process_started"),
                  event(2, "Codex A", "process_finished", metadata={"exit_code": 0}),
                  event(3, "Codex B", "review_started", metadata={"review_invocation": 8})]
        summary = summarize_events(events)["run"]
        self.assertEqual(summary["codex_a"]["invocations"], 1)
        self.assertIsNone(summary["codex_a"]["duration_seconds"])
        self.assertIsNone(summary["codex_a"]["usage"])
        self.assertEqual(summary["codex_b"]["invocations"], 0)
        self.assertEqual(summary["codex_b"]["unconfirmed_invocations"], 1)
        self.assertEqual(summary["review_retries"], 0)

    def test_launch_failures_are_observed_calls_not_confirmed_process_invocations(self):
        events = [event(1, "Codex B", "review_started"),
                  event(2, "Codex B", "review_process_finished", outcome="failed", metadata={
                      "execution_id": 1, "process_started": False, "exit_code": 127,
                      "duration_seconds": 0.1})]
        bucket = summarize_events(events)["run"]["codex_b"]
        self.assertEqual(bucket["observed_calls"], 1)
        self.assertEqual(bucket["launch_failures"], 1)
        self.assertEqual(bucket["invocations"], 0)
        self.assertIsNone(bucket["usage"])

    def test_conflicting_identity_and_invalid_timeline_fail_instead_of_miscounting(self):
        initial = [event(1, "Codex A", "process_started"),
                   event(2, "Codex A", "process_finished", metadata={"execution_id": 1, "exit_code": 0})]
        for events in (initial + [event(3, "Codex A", "process_finished",
                                      metadata={"execution_id": 1, "exit_code": 0, "duration_seconds": 5})],
                       [event(2, "Codex A", "process_started")],
                       initial + [event(3, "Codex B", "review_process_finished",
                                        metadata={"execution_id": 1, "exit_code": 0})]):
            with self.assertRaises(ValueError):
                summarize_events(events)

    def test_export_rebuilds_from_source_and_leaves_prior_snapshot_on_torn_source(self):
        with tempfile.TemporaryDirectory() as root:
            logs = Path(root)
            source = logs / "events.jsonl"
            events = [event(1, "Codex A", "process_started"),
                      event(2, "Codex A", "process_finished", metadata={"exit_code": 0})]
            source.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
            write_metrics(logs)
            original = (logs / "metrics.json").read_bytes()
            write_metrics(logs)
            self.assertEqual((logs / "metrics.json").read_bytes(), original)
            with source.open("a", encoding="utf-8") as output:
                output.write('{"torn')
            with self.assertRaises(ValueError):
                write_metrics(logs)
            self.assertEqual((logs / "metrics.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
