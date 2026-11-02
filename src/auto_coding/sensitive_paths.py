"""Shared filename policy for repository-relative Git changes, not content scanning."""

import re


# Explicit detection exposes both sides of renames and copies, even when an
# unchanged source would be absent from --no-renames filename enumeration.
# Git execution remains bounded by the existing process/output limits.
CHANGE_PATH_OPTIONS = ("--name-status", "-z", "--find-renames", "--find-copies",
                       "--find-copies-harder", "-l0", "--no-ext-diff", "--no-textconv")


def changed_paths(output: str) -> list[str]:
    """Read Git's NUL-delimited status metadata, never patch contents."""
    if not output:
        return []
    if not output.endswith("\0"):
        raise ValueError("Incomplete Git change metadata")
    fields = output[:-1].split("\0")
    paths = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status in {"A", "D", "M", "T"}:
            count = 1
        elif re.fullmatch(r"[RC][0-9]{1,3}", status) and int(status[1:]) <= 100:
            count = 2
        else:
            raise ValueError("Unverified Git change status")
        names = fields[index:index + count]
        if len(names) != count or any(not name for name in names):
            raise ValueError("Incomplete Git change paths")
        paths.extend(names)
        index += count
    return paths


def is_sensitive_path(path: str) -> bool:
    # Apply the same conservative policy on both hosts, including Windows
    # separators, case, alternate streams and trailing dot/space aliases.
    literal = path.replace("\\", "/").casefold().split("/")
    aliases = [part.split(":", 1)[0].rstrip(" .") for part in literal]
    # POSIX permits colons literally: normalization must not erase an existing
    # denial such as safe:private.key while also blocking private.key:stream.
    return (any(part in {".git", ".ssh", ".aws"} for part in literal + aliases)
            or any(name.startswith(".env")
                   or name in {"credentials", "credentials.json", "id_rsa", "id_ed25519"}
                   or name.endswith((".pem", ".p12", ".pfx", ".key"))
                   for name in (literal[-1], aliases[-1])))
