"""Small path-only history. Run files remain authoritative and are never written."""
import json
import os
from pathlib import Path
import tempfile
import heapq

from auto_coding.desktop_reader import _confined_log_file

LIMIT = 200


class RunHistory:
    def __init__(self, runs_dir):
        self.runs_dir = Path(runs_dir).resolve()
        self.path = self.runs_dir.parent / "recent-runs.json"
        self.error = None
        self.write_error = None

    def _load(self):
        self.error = None
        try:
            if not self.path.exists():
                return {"opened": [], "forgotten": []}
            if self.path.is_symlink() or self.path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("unsafe or oversized history index")
            with self.path.open("rb") as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("oversized history index")
            value = json.loads(raw)
            if not isinstance(value, dict) or any(
                not isinstance(value.get(key), list) or
                any(not isinstance(p, str) or not Path(p).is_absolute() for p in value[key])
                for key in ("opened", "forgotten")
            ):
                raise ValueError("invalid history index")
            return value
        except (OSError, ValueError, UnicodeError) as exc:
            self.error = f"Run history unavailable: {exc}. Existing logs are unchanged."
            return None

    def _save(self, value):
        temporary = None
        try:
            encoded = json.dumps(value).encode("utf-8")
            if len(encoded) > 4 * 1024 * 1024:
                raise OSError("History index is full. Forget entries before importing more runs.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="recent-runs-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.write_error = None
            return True
        except OSError as exc:
            self.error = f"Could not save run history: {exc}. Existing logs are unchanged."
            self.write_error = self.error
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def register(self, root):
        value = self._load()
        if value is None:
            return False
        path = str(Path(root).resolve(strict=True))
        value["opened"] = [path] + [p for p in value["opened"] if p != path]
        value["forgotten"] = [p for p in value["forgotten"] if p != path]
        return self._save(value)

    def forget(self, path):
        value = self._load()
        if value is None:
            return False
        if path not in {item["path"] for item in self.list()["runs"]}:
            self.error = "Unknown history entry."
            return False
        value["opened"] = [p for p in value["opened"] if p != path]
        if path not in value["forgotten"]:
            value["forgotten"].append(path)
        return self._save(value)

    @staticmethod
    def summary(path):
        root = Path(path)
        result = {"path": path, "run_id": root.name, "status": "unknown",
                  "repo": None, "branch": None, "time": None, "completed": None}
        try:
            # A registered directory replaced by a link must never redirect inspection.
            if root.resolve() != root:
                raise ValueError("run directory now redirects elsewhere")
            if not root.is_dir():
                result["status"] = "missing"
                return result
            records = {}
            for name in ("state.json", "run-manifest.json", "desktop-run.json"):
                if not (root / name).exists() and not (root / name).is_symlink():
                    records[name] = {}
                    continue
                file = _confined_log_file(root, name)
                with file.open("rb") as stream:
                    raw = stream.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise ValueError("metadata exceeds listing limit; open to inspect")
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("invalid run metadata")
                records[name] = record
            state, manifest, desktop = (records[n] for n in ("state.json", "run-manifest.json", "desktop-run.json"))
            def label(value):
                return value[:2048] if isinstance(value, str) else None
            result.update(run_id=label(manifest.get("run_id") or state.get("run_id")) or root.name,
                          repo=label(state.get("repo") or manifest.get("target_repo") or desktop.get("repo")),
                          branch=label(state.get("branch") or (state.get("auto_push") or {}).get("branch")),
                          time=label(manifest.get("start_time_utc") or state.get("created_utc")),
                          completed=len(state["completed"]) if isinstance(state.get("completed"), list) else None)
            status = state.get("status")
            if desktop.get("status") in {"running", "launching"}:
                active = DesktopPID.active(desktop.get("pid"))
                # Desktop metadata can outlive the dashboard while the runner
                # records its final outcome. Trust that outcome only after a
                # valid saved PID is confirmed inactive; an absent PID is uncertain.
                if active is True:
                    status = "running"
                elif active is not False or status not in {"completed", "stopped"}:
                    status = "unknown"
            result["status"] = status if status in {"completed", "stopped", "running"} else "unknown"
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            result["status"] = "unavailable"
        return result

    def list(self):
        loaded = self._load()
        value = loaded or {"opened": [], "forgotten": []}
        # Rank the union before limiting the display. A full index must not
        # prevent newer managed runs from being discovered on later launches.
        candidates = {}
        for path in value["opened"]:
            try:
                modified = Path(path).stat(follow_symlinks=False).st_mtime_ns
            except (OSError, ValueError, RuntimeError):
                modified = 0
            candidates[path] = modified
        discovered = []
        try:
            if self.runs_dir.resolve() != self.runs_dir:
                raise OSError("managed run directory redirects elsewhere")
            # One level only; never recurse or follow redirected directories.
            with os.scandir(self.runs_dir) as entries:
                managed = heapq.nlargest(LIMIT, (
                    (entry.stat(follow_symlinks=False).st_mtime_ns, entry.name)
                    for entry in entries if entry.is_dir(follow_symlinks=False)
                    and str(self.runs_dir / entry.name) not in value["forgotten"]
                    and (self.runs_dir / entry.name).resolve() == self.runs_dir / entry.name
                ))
                for modified, name in managed:
                    path = str(self.runs_dir / name)
                    if path not in candidates:
                        discovered.append(path)
                    candidates[path] = modified
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError) as exc:
            self.error = f"Could not list managed runs: {exc}"
        if discovered and loaded is not None:
            value["opened"].extend(discovered)
            self._save(value)
        paths = heapq.nlargest(LIMIT, candidates, key=candidates.get)
        return {"runs": [self.summary(p) for p in paths], "error": self.error or self.write_error,
                "limit": LIMIT}


class DesktopPID:
    @staticmethod
    def active(pid):
        # Lazy import avoids a controller/history import cycle; no processes are launched.
        from auto_coding.desktop_control import DesktopController
        if not DesktopController._valid_pid(pid):
            return None
        return DesktopController._pid_active(pid)
