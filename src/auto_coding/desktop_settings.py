"""Small, nonsecret setup hints; never authority to connect, launch or publish."""
import json
import os
from pathlib import Path
import tempfile

from auto_coding.github_workspace import normalize_repository, storage_root


def clean_preferences(value):
    result = {"repository": "", "branch": "", "plan": "", "attempts": 3}
    if not isinstance(value, dict):
        return result
    repository = value.get("repository")
    try:
        normalize_repository(repository)
        result["repository"] = repository
    except ValueError:
        pass  # Reject credential-bearing URLs and arbitrary pasted text.
    branch = value.get("branch")
    if (result["repository"] and isinstance(branch, str) and 0 < len(branch) <= 255
            and branch.lower() not in {"main", "master"}
            and not any(c.isspace() or ord(c) < 32 for c in branch)):
        result["branch"] = branch
    plan = value.get("plan")
    if isinstance(plan, str) and len(plan) <= 4096 and Path(plan).is_absolute():
        result["plan"] = plan
    if type(value.get("attempts")) is int and 1 <= value["attempts"] <= 10:
        result["attempts"] = value["attempts"]
    return result


class DesktopSettings:
    def __init__(self):
        try:
            self.path = storage_root() / "settings.json"
        except (OSError, ValueError):
            self.path = None

    def load(self):
        try:
            if self.path is not None and not self.path.is_symlink():
                with self.path.open("rb") as stream:
                    raw = stream.read(16385)
                if len(raw) <= 16384:
                    return clean_preferences(json.loads(raw))
        except (OSError, ValueError, UnicodeError, RecursionError):
            pass
        return clean_preferences({})

    def save(self, value):
        temporary = None
        try:
            if self.path is None or self.path.is_symlink():
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(clean_preferences(value), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            return True
        except (OSError, ValueError):
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
