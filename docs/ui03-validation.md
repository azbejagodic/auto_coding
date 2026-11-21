# UI03 implementation and validation

The existing pywebview app now has Dashboard and Configuration pages. Browse only selects a JSON file. Configuration uses authenticated, paginated Codex app-server capabilities for the audit, implementer and reviewer models. Audit reasoning remains fixed at High. Explicit A/B reasoning overrides preserve reviewer permission preparation. Controller validation, native confirmation, audit approval and durable run identity remain separate gates.

Non-secret preferences are saved atomically in `desktop-settings.json` beside controller run storage, outside the target checkout. They are restored after the same checkout, JSON plan bytes and branch are explicitly selected and revalidated. No checkout or branch is switched by navigation or settings restoration. Auto-push remains off by default for a new run; Resume retains saved publication authorization. New runs record requested A/B models and efforts in their state, manifest and execution evidence. Resume checks its original profile rather than adopting current form choices.

Changed files:

- `src/auto_coding/desktop_app.py`
- `src/auto_coding/desktop_control.py`
- `src/auto_coding/model_catalog.py` (new)
- `src/auto_coding/plan_audit.py`
- `src/auto_coding/run_launcher.py`
- `src/auto_coding/simple_run.py`
- `tests/desktop_ui_harness.cjs`
- `tests/test_dashboard_configuration.py` (new)
- `tests/test_dashboard_acceptance.py`
- `tests/test_desktop_bridge.py`
- `tests/test_desktop_ui.py`
- `tests/test_plan_audit.py`
- `docs/ui03-validation.md` (this record)

## Commands and observed results

Repeated development runs are summarized by their latest result below. Early UI/integration failures exposed obsolete modal/configuration fixtures and a reviewer permission-layer rejection of reasoning overrides. The fixtures were updated to supply authenticated mocked capabilities and applied configuration; the reviewer override now follows permission preparation, matching the existing fixed-High audit pattern. Affected tests were rerun.

| Actual command | Observed result |
| --- | --- |
| `python -B -m unittest discover -s tests -p test_desktop_ui.py -v` | 4 passed |
| `python -B -m unittest discover -s tests -p test_dashboard_configuration.py -v` | 16 passed |
| `python -B -m unittest discover -s tests -p test_dashboard_acceptance.py -v` | 7 passed |
| `python -B -m unittest discover -s tests -p test_desktop_bridge.py -v` | 39 passed |
| `python -B -m unittest discover -s tests -p test_desktop_control.py -v` | 37 passed |
| `python -B -m unittest discover -s tests -p test_run_launcher.py -v` | 15 passed |
| `python -B -m unittest discover -s tests -p test_simple_run.py -v` | 28 passed |
| `python -B -m unittest discover -s tests -p test_plan_audit.py -v` | 39 passed, 1 failed because its GUI fixture omitted newly required configuration. Fixture corrected; focused rerun below passed. The unchanged passing cases were not rerun. |
| `python -B -m unittest discover -s tests -p test_plan_audit.py -k test_powershell_audit_gui_start_and_authorized_resume_share_runner -v` | 1 passed after fixture correction |
| `python -B -m unittest discover -s tests -p test_plan_audit.py -k test_catalog_model_id_with_provider_prefix_keeps_fixed_high_contract -v` | 1 new regression passed |
| `python -B -m unittest discover -s tests -p test_desktop_bridge.py -k test_real_controller_duplicate_start_and_resume_without_process_launch -v` | 1 passed after restoring real preflight over a disposable Git fixture |
| `python -B -m unittest discover -s tests -p test_dashboard_acceptance.py -k test_selected_json_audit_once_native_start_three_checkpoints_completed -v` | 1 passed after final reasoning-validation changes; exercises actual CLI argv, A/B overrides, checkpoints, final verification and UI rendering |
| `git diff --check` | Passed |

Catalog responses and model execution are mocked. Integration tests use real disposable Git repositories and focused child checks; no real model invocation is used. These results do not establish native Windows MXC behavior. No tests were skipped or weakened to accommodate the new UI. The obsolete structural modal/no-navigation assertions were replaced with assertions for exactly two pages.

The complete staged/unstaged task diff and all new files were inspected. There were no staged changes. No history changes or pushes were made in the working repository. The full test suite was not run.

## Remaining checks and limits

- Current native-window screenshot/manual visual inspection is pending. The existing approved reference image was inspected; it is not evidence of the new running UI.
- The separate real Windows MXC probe (`python tests/mxc_workspace_probe.py`) was not run. Its infrastructure and test isolation policy are unchanged.
- Catalog access requires an installed/authenticated CLI with the expected app-server schema. Missing, malformed or incomplete catalogs lock launches. The catalog is bounded to ten pages of 100 entries and a 40-second owned helper process. There is no custom-ID fallback or automatic model substitution.
- Saved choices restore only for an explicitly selected matching identity. Stale capabilities require refreshing/reapplying valid choices. A saved run with an absent or inconsistent authorized model profile can be rejected rather than adopting new settings.
- Branch create/select/fetch actions retain the existing managed-GitHub scope. A directly selected local checkout must already be on a verified feature branch. Native launch confirmations remain mandatory.
