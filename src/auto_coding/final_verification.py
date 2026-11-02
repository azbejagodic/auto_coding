"""Small plan policy and evidence checks for the final unittest verification gate."""
import hashlib
import json
import re


def verification_policy(value):
    if (not isinstance(value, dict) or not {"test_commands", "minimum_tests"} <= set(value)
            <= {"test_commands", "minimum_tests", "timeout_seconds"}):
        raise ValueError("Final verification requires test_commands and minimum_tests; timeout_seconds is optional")
    commands = value["test_commands"]
    if (not isinstance(commands, list) or not 1 <= len(commands) <= 10
            or any(not isinstance(cmd, list) or not 1 <= len(cmd) <= 32
                   or any(not isinstance(arg, str) or not arg or len(arg) > 4096 for arg in cmd) for cmd in commands)):
        raise ValueError("Final test commands must be nonempty argv lists, never shell strings")
    timeout = value.get("timeout_seconds", 1800)
    if type(timeout) is not int or not 30 <= timeout <= 86400:
        raise ValueError("Verification timeout must be an integer from 30 to 86400 seconds")
    minimum = value["minimum_tests"]
    if type(minimum) is not int or not 1 <= minimum <= 1000000:
        raise ValueError("Final verification minimum_tests must be a positive bounded integer")
    return {"test_commands": [list(cmd) for cmd in commands], "minimum_tests": minimum,
            "timeout_seconds": timeout}


def policy_sha256(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unittest_evidence(output):
    """An exit code or an arbitrary 'OK' line is not a full-suite result.

    The prepared plan designates the suite and its expected minimum coverage.
    Use the last unittest summary, so nested suite output cannot hide failure
    of the enclosing suite. Skips remain incomplete verification in this gate.
    """
    match = None
    for match in re.finditer(r"(?m)^Ran ([0-9]{1,7}) tests? in [0-9.]+s\r?$", output):
        pass
    if match is None:
        return None
    verdict = output[match.end():].lstrip("\r\n").split("\n", 1)[0].rstrip("\r")
    successful = re.fullmatch(r"OK(?: \(skipped=([0-9]+)\))?", verdict)
    return {"tests": int(match[1]), "skipped": int(successful[1] or 0) if successful else 0,
            "successful": successful is not None,
            "failed": verdict.startswith("FAILED (")}
