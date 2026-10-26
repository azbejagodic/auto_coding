"""GitHub checkout service. No credentials or raw subprocess diagnostics are persisted."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from functools import wraps


_uncertain_process = ContextVar("autocoding_uncertain_process", default=None)


def mark_process_uncertain():
    """Retain the current job gate when subprocess descendants may survive."""
    marker = _uncertain_process.get()
    if marker is not None:
        marker.touch()


def managed_lock(repo, suffix=".lock"):
    repo = Path(repo).resolve() if repo is not None else None
    if repo and repo.parent.name == "workspaces" and re.fullmatch(r"[0-9a-f]{64}", repo.name):
        return repo.parent / (repo.name + suffix)
    return None


def serialized_launch(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        lock = managed_lock(self.repo)
        try:
            if lock:
                with checkout_lock(lock):
                    return method(self, *args, **kwargs)
            return method(self, *args, **kwargs)
        except WorkspaceError as exc:
            return self._fail(str(exc))
    return wrapped


def reserve_job(repo):
    """Reserve under the checkout operation lock, before creating a child."""
    path = managed_lock(repo, ".job.lock")
    if path is None:
        return None
    try:
        path.mkdir()
    except FileExistsError:
        raise WorkspaceError("busy", "An Auto-Coding runner is active or uncertain for this checkout.") from None
    token = uuid.uuid4().hex
    # Failure to persist ownership deliberately leaves an uncertain reservation.
    (path / "reservation").write_text(token, encoding="ascii")
    return token


def cancel_reservation(repo, token):
    """Release an unclaimed reservation when execution is known not to have begun."""
    if token is None:
        return
    path = managed_lock(repo, ".job.lock")
    if path is None:
        return
    reservation = path / "reservation"
    try:
        owner = reservation.read_text(encoding="ascii")
    except FileNotFoundError:
        return  # Already claimed or released; never remove another job's lock.
    if owner == token:
        reservation.unlink()
        path.rmdir()


def managed_job(method):
    @wraps(method)
    def wrapped(plan, repo, logs, **kwargs):
        lock = managed_lock(repo)
        if not lock:
            return method(plan, repo, logs, **kwargs)
        job_path = managed_lock(repo, ".job.lock")
        # A newly spawned desktop child can arrive before its parent finishes
        # persisting the launch PID. Wait briefly for that lock handoff.
        deadline = time.monotonic() + 10
        while True:
            operation = checkout_lock(lock)
            try:
                operation.__enter__()
                break
            except WorkspaceError:
                if (lock / "uncertain").exists() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        try:
            token = os.environ.get("AUTOCODING_JOB_RESERVATION")
            reservation = job_path / "reservation"
            if (token and re.fullmatch(r"[0-9a-f]{32}", token)
                    and reservation.is_file()
                    and reservation.read_text(encoding="ascii") == token):
                # Claim once, while serialized with all launches/branch changes.
                reservation.unlink()
            else:
                try:
                    job_path.mkdir()
                except FileExistsError:
                    raise WorkspaceError("busy", "An Auto-Coding runner is active or uncertain for this checkout.") from None
        finally:
            operation.__exit__(None, None, None)
        defaults = method.__kwdefaults__ or {}
        if "execute" in defaults:
            execute = kwargs.get("execute", defaults["execute"])
            def tracked_execute(*args, **options):
                try:
                    result = execute(*args, **options)
                except BaseException:
                    # The executor may have spawned a process before raising.
                    (job_path / "uncertain").touch()
                    raise
                if result[2]:
                    # A timed-out parent may have left tool descendants alive.
                    (job_path / "uncertain").touch()
                return result
            kwargs["execute"] = tracked_execute
        tracking = _uncertain_process.set(job_path / "uncertain")
        try:
            return method(plan, repo, logs, **kwargs)
        finally:
            _uncertain_process.reset(tracking)
            if not (job_path / "uncertain").exists():
                job_path.rmdir()
    return wrapped


class WorkspaceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def storage_root(config_dir=None):
    if config_dir is not None:
        return Path(config_dir).expanduser().resolve() / "AutoCoding"
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]).resolve() / "AutoCoding"
    # Portable hosts must explicitly select a per-user configuration directory.
    if os.environ.get("AUTOCODING_CONFIG_DIR"):
        return Path(os.environ["AUTOCODING_CONFIG_DIR"]).expanduser().resolve() / "AutoCoding"
    raise WorkspaceError("configuration", "Set AUTOCODING_CONFIG_DIR to a per-user configuration directory.")


def normalize_repository(value):
    if not isinstance(value, str):
        raise WorkspaceError("invalid_repository", "Use a GitHub OWNER/REPO or supported GitHub URL.")
    match = re.fullmatch(r"(?:(https://github\.com/)|(git@github\.com:))?([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9_.-]{1,104})", value, re.ASCII)
    if not match:
        raise WorkspaceError("invalid_repository", "Use a GitHub OWNER/REPO or supported GitHub URL.")
    owner, repo = match[3], match[4]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo or len(repo) > 100 or repo in {".", ".."} or "--" in owner:
        raise WorkspaceError("invalid_repository", "Invalid GitHub repository name.")
    identity = f"{owner}/{repo}".lower()
    return identity, (f"git@github.com:{identity}.git" if match[2] else f"https://github.com/{identity}.git")


@contextmanager
def checkout_lock(path):
    """Atomic across processes; abandoned locks deliberately require inspection."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError:
        raise WorkspaceError("busy", "Checkout operation is active or uncertain; inspect its workspace lock.") from None
    tracking = _uncertain_process.set(path / "uncertain")
    try:
        yield
    finally:
        _uncertain_process.reset(tracking)
        # A timed-out/interrupted Git process may leave hooks or filters alive.
        # Keep the same cross-process gate used by branch changes and launches;
        # neither a retry nor a new manager instance can establish quiescence.
        if not (path / "uncertain").exists():
            path.rmdir()


class GitHubWorkspaceManager:
    def __init__(self, config_dir=None, *, active_check=None, run_command=None):
        self.root = storage_root(config_dir)
        self.workspaces = self.root / "workspaces"
        self.runs_dir = self.root / "runs"
        self.active_check = active_check
        self.run_command = run_command or subprocess.run

    def _command(self, argv, cwd=None, allowed=(0,), *, preserve=False):
        env = os.environ.copy()
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        try:
            # Git emits UTF-8 paths and refs even on Windows ACP locales.
            result = self.run_command(argv, cwd=cwd, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      shell=False, timeout=120, env=env)
        except FileNotFoundError:
            raise WorkspaceError("unavailable", "Required command is unavailable.") from None
        except subprocess.TimeoutExpired:
            mark_process_uncertain()
            raise WorkspaceError("network", "Command failed or timed out; inspect connectivity.") from None
        except OSError:
            raise WorkspaceError("network", "Command failed or timed out; inspect connectivity.") from None
        except BaseException:
            mark_process_uncertain()
            raise
        if result.returncode not in allowed:
            # Never return command arguments, URLs or raw diagnostics (which may contain tokens).
            diagnostic = (result.stderr or "").lower()
            code = "authentication" if any(s in diagnostic for s in ("authentication", "permission denied", "could not read username", "repository not found", "403", "401")) else "git"
            if any(s in diagnostic for s in ("resolve host", "connect to", "network", "timed out")):
                code = "network"
            if any(s in diagnostic for s in ("non-fast-forward", "fetch first", "[rejected]")):
                raise WorkspaceError("non_fast_forward", "Remote branch changed. Fetch and review it before retrying; automatic merge and force push are disabled.")
            messages = {"authentication": "GitHub access failed. Authenticate Git and check repository permissions, then retry.",
                        "network": "Cannot reach GitHub. Check your network connection, then retry."}
            raise WorkspaceError(code, messages.get(code, "Git operation failed. Check authentication, connectivity and repository access."))
        return result.stdout if preserve else result.stdout.strip()

    def _git(self, repo, *args):
        return self._command(["git", *args], repo)

    def _paths(self, value):
        identity, url = normalize_repository(value)
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.workspaces.mkdir(parents=True, exist_ok=True)
        path = self.workspaces / key
        if path.is_symlink() or path.resolve() != self.workspaces.resolve() / key:
            raise WorkspaceError("collision", "Managed checkout path is redirected.")
        return identity, url, path, self.workspaces / (key + ".json"), self.workspaces / (key + ".lock")

    def _verify_metadata(self, identity, metadata):
        if metadata.is_symlink():
            raise ValueError()
        value = json.loads(metadata.read_text(encoding="utf-8"))
        if value != {"repository": identity, "schema": 1}:
            raise ValueError()

    def _verify(self, identity, repo, metadata):
        try:
            self._verify_metadata(identity, metadata)
            if (repo / ".git").is_symlink() or not (repo / ".git").is_dir():
                raise ValueError()
            if Path(self._git(repo, "rev-parse", "--show-toplevel")).resolve() != repo.resolve():
                raise ValueError()
            origin, _ = normalize_repository(self._git(repo, "remote", "get-url", "--all", "origin"))
            if origin != identity:
                raise ValueError()
        except (OSError, ValueError):
            raise WorkspaceError("origin_mismatch", "Checkout metadata or origin does not match; directory left untouched.") from None

    def connect(self, repository):
        identity, url, repo, metadata, lock = self._paths(repository)
        with checkout_lock(lock):
            if repo.exists() or repo.is_symlink():
                self._verify(identity, repo, metadata)
            else:
                # Persist only identity before cloning. A failed attempt never
                # occupies the final checkout path, so ordinary retries work.
                if metadata.exists() or metadata.is_symlink():
                    try:
                        self._verify_metadata(identity, metadata)
                    except (OSError, ValueError):
                        raise WorkspaceError("origin_mismatch", "Checkout metadata does not match; directory left untouched.") from None
                else:
                    with metadata.open("x", encoding="utf-8") as stream:
                        json.dump({"repository": identity, "schema": 1}, stream)
                attempt = self.workspaces / (repo.name + ".clone-" + uuid.uuid4().hex)
                attempt.mkdir()
                checkout = attempt / "checkout"
                self._command(["git", "clone", "--origin", "origin", "--", url, str(checkout)])
                self._verify(identity, checkout, metadata)
                # Never replace/adopt an unrelated destination, even an empty
                # directory. Windows rename also refuses an existing target.
                if repo.exists() or repo.is_symlink():
                    raise WorkspaceError("collision", "Managed checkout directory already exists; left untouched.")
                checkout.rename(repo)
                attempt.rmdir()
            return {"repository": identity, "path": str(repo), **self._status(repo)}

    reopen = connect

    def _status(self, repo):
        return {"branch": self._git(repo, "branch", "--show-current") or None,
                "clean": not bool(self._git(repo, "status", "--porcelain", "--untracked-files=all"))}

    def branches(self, repository):
        identity, _, repo, metadata, lock = self._paths(repository)
        with checkout_lock(lock):
            self._verify(identity, repo, metadata)
            return {**self._status(repo),
                    "local": self._git(repo, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads/").splitlines(),
                    "remote": self._git(repo, "for-each-ref", "--format=%(refname:strip=2)", "refs/remotes/origin/").splitlines()}

    def fetch(self, repository):
        identity, _, repo, metadata, lock = self._paths(repository)
        with checkout_lock(lock):
            self._verify(identity, repo, metadata)
            self._idle(repo)
            self._git(repo, "fetch", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*")
            return self._status(repo)

    def _idle(self, repo):
        if managed_lock(repo, ".job.lock").exists():
            raise WorkspaceError("active_job", "An Auto-Coding runner is active or uncertain for this checkout.")
        if self.active_check is not None and self.active_check(repo) is not False:
            raise WorkspaceError("active_job", "An Auto-Coding job is active or uncertain for this checkout.")
        # Include legacy desktop storage and any externally selected run via callback.
        from auto_coding.desktop_control import DesktopController
        for root in (self.runs_dir, Path.home() / "AutoCoding-Runs"):
            if DesktopController(root).checkout_busy(repo):
                raise WorkspaceError("active_job", "An Auto-Coding job is active or uncertain for this checkout.")
        self._publication_tree(repo)

    def _publication_tree(self, repo):
        if not self._status(repo)["clean"]:
            raise WorkspaceError("dirty", "Commit or discard uncommitted changes before changing branches or publishing.")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "sequencer", "BISECT_START", "index.lock", "HEAD.lock"):
            path = Path(self._git(repo, "rev-parse", "--git-path", marker))
            if not path.is_absolute():
                path = repo / path
            if path.exists():
                raise WorkspaceError("git_operation", "An unresolved Git operation blocks branch changes.")

    def _branch(self, repo, name):
        if not isinstance(name, str) or not name or name.startswith("-") or name.lower() in {"main", "master"}:
            raise WorkspaceError("invalid_branch", "Choose a safe feature branch name; main/master are protected.")
        self._git(repo, "check-ref-format", "refs/heads/" + name)

    def create_branch(self, repository, name, *, base="main", confirmed_base=False):
        identity, _, repo, metadata, lock = self._paths(repository)
        with checkout_lock(lock):
            self._verify(identity, repo, metadata)
            self._idle(repo)
            self._branch(repo, name)
            if base != "main" and confirmed_base is not True:
                raise WorkspaceError("confirmation", "An alternate base requires explicit user confirmation.")
            if not isinstance(base, str) or base.startswith("-"):
                raise WorkspaceError("invalid_branch", "Invalid base branch.")
            self._git(repo, "check-ref-format", "refs/heads/" + base)
            self._git(repo, "fetch", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*")
            # An explicit refspec must succeed: wildcard fetch alone could leave
            # a stale origin/main behind when upstream has deleted that branch.
            self._git(repo, "fetch", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
            self._idle(repo)
            refs = self._git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/", "refs/remotes/origin/").splitlines()
            if "refs/heads/" + name in refs or "refs/remotes/origin/" + name in refs:
                raise WorkspaceError("branch_exists", "Branch already exists; select it or choose a new feature name.")
            commit = self._git(repo, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}")
            self._git(repo, "switch", "--no-track", "-c", name, commit)
            return self._status(repo)

    def checkout_branch(self, repository, name):
        identity, _, repo, metadata, lock = self._paths(repository)
        with checkout_lock(lock):
            self._verify(identity, repo, metadata)
            self._idle(repo)
            self._branch(repo, name)
            refs = self._git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/", "refs/remotes/origin/").splitlines()
            if "refs/heads/" + name in refs:
                self._git(repo, "switch", "--no-guess", name)
            elif "refs/remotes/origin/" + name in refs:
                self._git(repo, "switch", "-c", name, "--track", "origin/" + name)
            else:
                raise WorkspaceError("missing_branch", "Branch is not available; fetch or create a feature branch.")
            return self._status(repo)

    def list_repositories(self):
        try:
            self._command(["gh", "auth", "status"])
            raw = self._command(["gh", "repo", "list", "--limit", "100", "--json", "nameWithOwner"])
            return {"available": True, "repositories": [normalize_repository(item["nameWithOwner"])[0] for item in json.loads(raw)]}
        except (WorkspaceError, ValueError, TypeError, KeyError):
            return {"available": False, "repositories": [], "message": "Enter OWNER/REPO manually; Git authentication remains available."}

    def _push_check(self, identity, repo, approved, *, runner_owned=False, allow_published=False,
                    reconcile_only=False):
        if not runner_owned:
            self._idle(repo)
        elif not reconcile_only:
            self._publication_tree(repo)
        branch = self._status(repo)["branch"]
        self._branch(repo, branch)
        push_origin, _ = normalize_repository(self._git(repo, "remote", "get-url", "--push", "--all", "origin"))
        if push_origin != identity:
            raise WorkspaceError("origin_mismatch", "Push origin does not match the selected GitHub repository.")
        refs = self._git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").splitlines()
        target = "refs/remotes/origin/" + branch
        if target not in refs:
            target = next((ref for ref in ("refs/remotes/origin/main", "refs/remotes/origin/master") if ref in refs), None)
            if target is None:
                raise WorkspaceError("approval", "No reviewed remote base is available. Fetch the repository before publishing.")
            target = self._git(repo, "merge-base", "HEAD", target)
        if self._git(repo, "merge-base", "HEAD", target) != self._git(repo, "rev-parse", target):
            raise WorkspaceError("non_fast_forward", "Remote branch diverged. Fetch and review before publishing; no automatic merge.")
        commits = self._git(repo, "rev-list", "--reverse", target + "..HEAD").splitlines()
        if not commits and allow_published:
            commits = [self._git(repo, "rev-parse", "HEAD")]
        if not commits:
            raise WorkspaceError("approval", "No unpublished approved checkpoints on this branch.")
        from auto_coding.simple_run import is_generated_path
        for commit in commits:
            record = approved.get(commit, {})
            review = record.get("review")
            if (not isinstance(review, dict) or review.get("decision") != "APPROVED" or review.get("issues") != []
                    or not record.get("diff_sha256")):
                raise WorkspaceError("approval", "Every unpublished commit needs a recorded approved checkpoint. Older or manual commits require review.")
            parents = self._git(repo, "rev-list", "--parents", "-n", "1", commit).split()
            if parents != [commit, record.get("base_head")]:
                raise WorkspaceError("approval", "Checkpoint parent differs from its approval; inspect before publishing.")
            names = self._git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit).split("\0")
            for name in filter(None, names):
                parts = name.replace("\\", "/").lower().split("/")
                if (is_generated_path(name) or any(p in {".git", ".ssh", ".aws"} for p in parts)
                        or parts[-1].startswith(".env") or parts[-1] in {"credentials", "credentials.json", "id_rsa", "id_ed25519"}
                        or parts[-1].endswith((".pem", ".key", ".p12", ".pfx"))):
                    raise WorkspaceError("sensitive", "Generated or sensitive files block publication. Inspect the checkpoint locally.")
            patch = self._command(["git", "show", "--format=", "--binary", "--no-ext-diff", commit], repo, preserve=True)
            if hashlib.sha256(patch.encode("utf-8", errors="replace")).hexdigest() != record["diff_sha256"]:
                raise WorkspaceError("approval", "Checkpoint bytes differ from the approved patch; inspect before publishing.")
        return {"eligible": True, "branch": branch, "commits": len(commits), "reason": "Approved local checkpoints can be published. Remote state is rechecked on push."}

    def publish_checkpoint(self, repo, identity, branch, sha, approved):
        """Synchronous publication by the coordinator owning the checkout job gate."""
        self._branch(repo, branch)
        if self._status(repo)["branch"] != branch or self._git(repo, "rev-parse", "HEAD") != sha:
            raise WorkspaceError("checkpoint", "Selected branch or checkpoint changed; inspect before Retry Push.")
        # Verify origin before making any network request. Never persist URLs or diagnostics.
        for args in (("remote", "get-url", "--all", "origin"),
                     ("remote", "get-url", "--push", "--all", "origin")):
            if normalize_repository(self._git(repo, *args))[0] != identity:
                raise WorkspaceError("origin_mismatch", "Origin differs from the selected repository.")
        self._git(repo, "fetch", "--prune", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*")
        remote = self._git(repo, "ls-remote", "--heads", "origin", "refs/heads/" + branch)
        # A later unfinished task may have legitimate staged/unstaged changes.
        # Observing an already published checkpoint must preserve those changes;
        # approval/origin/lineage checks still apply, and any actual push below
        # requires the normal clean-tree and Git-operation validation.
        self._push_check(identity, repo, approved, runner_owned=True, allow_published=True,
                         reconcile_only=remote.split()[:1] == [sha])
        if remote.split()[:1] != [sha]:
            self._git(repo, "-c", "core.hooksPath=" + os.devnull,
                      "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
                      "-c", "remote.origin.mirror=false", "-c", "push.gpgSign=false",
                      "push", "origin", sha + ":refs/heads/" + branch)
            remote = self._git(repo, "ls-remote", "--heads", "origin", "refs/heads/" + branch)
        if remote.split()[:1] != [sha]:
            raise WorkspaceError("unconfirmed", "Remote checkpoint could not be confirmed. Retry Push.")

    def push_eligibility(self, repository, approved):
        identity, _, repo, metadata, lock = self._paths(repository)
        with checkout_lock(lock):
            self._verify(identity, repo, metadata)
            return self._push_check(identity, repo, approved)

    def push_approved(self, repository, approved):
        identity, _, repo, metadata, lock = self._paths(repository)
        with checkout_lock(lock):
            self._verify(identity, repo, metadata)
            self._push_check(identity, repo, approved)
            self._git(repo, "fetch", "--prune", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*")
            check = self._push_check(identity, repo, approved)
            self._git(repo, "-c", "core.hooksPath=" + os.devnull,
                      "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
                      "-c", "remote.origin.mirror=false", "-c", "push.gpgSign=false",
                      "push", "origin", "HEAD:refs/heads/" + check["branch"])
            return {"published": True, "branch": check["branch"], "message": "Approved checkpoints published to the feature branch. No merge to main."}
