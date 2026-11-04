"""Separate target-project Python imports from the trusted runner's imports."""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path


TARGET_PYTHONPATH = "AUTOCODING_TARGET_PYTHONPATH"
TEST_ENV_CONFIG = "AUTOCODING_TEST_ENV_JSON"

# Only ordinary runtime settings cross from the trusted controller into its
# own test commands. In particular, no CODEX_*, GIT_*, cloud or auth settings
# are inherited. The controller and its Codex/Git calls keep their own env.
_TEST_RUNTIME_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "CI", "NO_COLOR",
    "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONHASHSEED",
})
_TEST_EXPLICIT_KEYS = frozenset({"AUTOCODING_CONFIG_DIR", "CI", "NO_COLOR", "PYTHONDONTWRITEBYTECODE",
                                 "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONHASHSEED",
                                 "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"})


def target_environment(repo: Path) -> dict[str, str]:
    """Build a child-only environment; never change the controller's sys.path.

    Support flat projects and the conventional src directory. Preserve additional
    PYTHONPATH entries only when they resolve inside this target, so custom
    in-repository layouts remain usable without retaining another checkout.
    Installed dependencies and interpreter selection are otherwise unchanged.
    """
    root = Path(repo).resolve()
    environment = os.environ.copy()
    # A background controller keeps target roots separately, never on its own
    # import path. Revalidate both sources against the actual child target.
    inherited = environment.pop(TARGET_PYTHONPATH, "") + os.pathsep + environment.pop("PYTHONPATH", "")
    # PYTHONHOME can redirect the selected interpreter's standard library too.
    environment.pop("PYTHONHOME", None)
    paths = [root]
    source = (root / "src").resolve()
    # Include a not-yet-created src directory too: A may create it before testing.
    if source.is_relative_to(root):
        paths.append(source)
    for entry in inherited.split(os.pathsep):
        if not entry:
            continue
        path = (root / entry).resolve()
        if path.is_relative_to(root) and path not in paths:
            paths.append(path)
    environment["PYTHONPATH"] = os.pathsep.join(map(str, paths))
    return environment


def test_environment(repo: Path, *, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Give controller tests target imports and only ordinary process settings.

    Trusted callers may explicitly set a small set of non-secret test settings
    with AUTOCODING_TEST_ENV_JSON or ``extra``. Neither can add credentials or
    replace PATH/PYTHONPATH. Values are never included in validation errors.
    This is environment hygiene, not a sandbox for arbitrary test code.
    """
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in _TEST_RUNTIME_KEYS}
    environment["PYTHONPATH"] = target_environment(repo)["PYTHONPATH"]
    configured = os.environ.get(TEST_ENV_CONFIG)
    if configured is not None:
        try:
            if len(configured) > 4096:
                raise ValueError
            configured = json.loads(configured)
        except ValueError as exc:
            raise ValueError("Invalid controller-test environment configuration") from exc
        if not isinstance(configured, dict):
            raise ValueError("Invalid controller-test environment configuration")
    for additions in (configured, extra):
        if additions is None:
            continue
        for key, value in additions.items():
            if (not isinstance(key, str) or key.upper() not in _TEST_EXPLICIT_KEYS
                    or not isinstance(value, str) or len(value) > 4096
                    or "\0" in value or "\n" in value or "\r" in value):
                raise ValueError("Unsupported controller-test environment setting")
            environment[key.upper()] = value
    return environment
