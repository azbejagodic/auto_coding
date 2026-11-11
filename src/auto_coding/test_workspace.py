"""Controller-owned, bounded source snapshots; MXC supplies the security boundary."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat

from auto_coding.sensitive_paths import is_sensitive_path


def _files(repo, git):
    raw = git(repo, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if raw and not raw.endswith("\0"):
        raise ValueError("Incomplete snapshot file enumeration")
    names = sorted(set(raw[:-1].split("\0"))) if raw else []
    if len(names) > 50000:
        raise ValueError("Snapshot contains too many files")
    seen = set()
    for name in names:
        parts = PurePosixPath(name).parts
        if (not parts or PurePosixPath(name).is_absolute() or "\\" in name
                or any(ord(c) < 32 for c in name) or ":" in name
                or any(p in {".", ".."} or p.endswith((".", " ")) or "~" in p
                       or p.casefold().split(".")[0] in {
                           "con", "prn", "aux", "nul", *[f"com{i}" for i in range(10)],
                           *[f"lpt{i}" for i in range(10)]} for p in parts)
                or is_sensitive_path(name) or any(p.casefold() == ".codex" for p in parts)
                or name.casefold() in seen):
            raise ValueError("Unsafe or sensitive snapshot path")
        seen.add(name.casefold())
        source = repo.joinpath(*parts)
        if not source.exists() and not source.is_symlink():
            continue  # A tracked deletion is part of the implementation snapshot.
        for component in (source, *source.parents):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Snapshot paths cannot contain links or reparse points")
            if component == repo:
                break
        if not source.is_file():
            raise ValueError("Snapshot requires regular files; submodules are unsupported")
        yield name, source


def capture(repo, git, destination=None):
    """Bind HEAD and exact working bytes, including pre-commit implementation files.

    No checkout, filters, hooks, Git config, credentials or host metadata are copied.
    The caller compares a second capture before allowing any target execution.
    """
    head = git(repo, ["rev-parse", "--revs-only", "HEAD"])
    if head and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise ValueError("Snapshot requires an exact Git commit")
    if not head:
        # --create-repo legitimately verifies its first implementation before
        # any commit exists. Bind the unborn branch and exact content tree.
        branch = git(repo, ["symbolic-ref", "HEAD"])
        if not branch.startswith("refs/heads/"):
            raise ValueError("Cannot verify unborn Git snapshot")
    staged = git(repo, ["ls-files", "--stage", "-v", "-z"])
    modes = {}
    if staged and not staged.endswith("\0"):
        raise ValueError("Incomplete snapshot index metadata")
    for entry in staged[:-1].split("\0") if staged else []:
        # -v exposes assume-unchanged (lowercase) and skip-worktree (S).
        # Neither may hide source bytes from the controller's approval checks.
        match = re.fullmatch(r"H (100644|100755) [0-9a-f]{40,64} 0\t(.+)", entry)
        if not match:
            raise ValueError("Snapshot requires regular files, a resolved index and no hidden working-tree flags")
        modes[match[2]] = match[1].encode("ascii")
    entries, size, tree = [], 0, {}
    algorithm = ("sha256" if len(head) == 64 else "sha1") if head else git(repo, ["rev-parse", "--show-object-format"])
    if algorithm not in {"sha1", "sha256"}:
        raise ValueError("Unsupported Git object format")
    def object_id(kind, data):
        return hashlib.new(algorithm, kind + b" " + str(len(data)).encode() + b"\0" + data).digest()
    for name, source in _files(repo, git):
        with source.open("rb") as stream:
            data = stream.read(128 * 1024 * 1024 + 1)
        size += len(data)
        if size > 128 * 1024 * 1024:
            raise ValueError("Snapshot exceeds 128 MiB; use a smaller verification target")
        entries.append([name, hashlib.sha256(data).hexdigest()])
        parent = tree
        for part in PurePosixPath(name).parts[:-1]:
            parent = parent.setdefault(part, {})
        parent[PurePosixPath(name).name] = (modes.get(name, b"100644"), object_id(b"blob", data))
        if destination is not None:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    digest = hashlib.sha256(json.dumps(entries, ensure_ascii=True).encode("ascii")).hexdigest()
    if (git(repo, ["rev-parse", "--revs-only", "HEAD"]) != head
            or (not head and git(repo, ["symbolic-ref", "HEAD"]) != branch)):
        raise ValueError("Git snapshot changed during capture")
    def tree_id(items):
        data = b""
        for name, value in sorted(items.items(), key=lambda item:
                                  os.fsencode(item[0]) + (b"/" if isinstance(item[1], dict) else b"")):
            directory = isinstance(value, dict)
            data += (b"40000" if directory else value[0]) + b" " + os.fsencode(name) + b"\0"
            data += tree_id(value) if directory else value[1]
        return object_id(b"tree", data)
    return {"head": head or None, "unborn_branch": branch if not head else None,
            "tree": tree_id(tree).hex(), "files_sha256": digest,
            "file_count": len(entries), "bytes": size}


def environment(repo, scratch, base, filesystem, *, source=None):
    """Only validated installation search roots reach nested processes."""
    result = dict(base)
    allowed = [Path(key) for key, value in filesystem.items() if value == "read"]
    paths = []
    for entry in result.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        path = Path(entry)
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve(strict=True)
            if (not resolved.is_dir() or not any(resolved.is_relative_to(root) for root in allowed)
                    or any(is_sensitive_path(str(part)) or part.name.casefold() == ".codex"
                           for part in (resolved, *resolved.parents))):
                continue
            # No alias into an approved installation from an unapproved root.
            if resolved != path:
                continue
        except OSError:
            continue
        if str(resolved) not in paths:
            paths.append(str(resolved))
    result["PATH"] = os.pathsep.join(paths)
    imports = [repo, repo / "src"]
    if source is not None:
        for entry in base.get("PYTHONPATH", "").split(os.pathsep):
            if not entry:
                continue
            path = Path(entry).resolve()
            if path.is_relative_to(source):
                relocated = repo / path.relative_to(source)
                if relocated not in imports:
                    imports.append(relocated)
    result["PYTHONPATH"] = os.pathsep.join(map(str, imports))
    for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL",
                "PROGRAMDATA", "HOMEDRIVE", "HOMEPATH"):
        result.pop(key, None)
    directories = {"tmp": ("TEMP", "TMP", "TMPDIR"),
                   "home": ("HOME", "USERPROFILE"),
                   "appdata": ("APPDATA",), "localappdata": ("LOCALAPPDATA",),
                   "cache": ("XDG_CACHE_HOME", "PIP_CACHE_DIR", "npm_config_cache"),
                   "config": ("AUTOCODING_CONFIG_DIR", "XDG_CONFIG_HOME"),
                   "generated": ("AUTOCODING_TEST_OUTPUT_DIR",)}
    for name, keys in directories.items():
        directory = scratch / name
        directory.mkdir(exist_ok=True)
        result.update({key: str(directory) for key in keys})
    result.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                  GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
                  GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=str(scratch / "no-hooks"),
                  npm_config_userconfig=os.devnull, npm_config_globalconfig=os.devnull,
                  PYTHONDONTWRITEBYTECODE="1")
    return result


def resolve_tool(name, environment):
    """Search only explicit sanitized PATH directories, never the controller CWD."""
    if Path(name).name != name:
        return None
    for directory in environment.get("PATH", "").split(os.pathsep):
        if directory:
            candidate = shutil.which(str(Path(directory) / name))
            if candidate and Path(candidate).resolve().parent == Path(directory).resolve():
                return str(Path(candidate).resolve())
    return None
