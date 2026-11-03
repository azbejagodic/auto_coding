"""Separate target-project Python imports from the trusted runner's imports."""
from __future__ import annotations

import os
from pathlib import Path


TARGET_PYTHONPATH = "AUTOCODING_TARGET_PYTHONPATH"


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
