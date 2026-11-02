"""Shared filename policy for repository-relative Git changes, not content scanning."""


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
