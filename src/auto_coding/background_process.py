"""Launch options for app-owned, noninteractive background commands."""
import subprocess
import sys


def background_options(*, new_process_group: bool = False) -> dict[str, int]:
    """Hide Windows background consoles without changing streams or supervision."""
    if sys.platform != "win32":
        return {}
    # CREATE_NO_WINDOW is ignored with DETACHED_PROCESS or CREATE_NEW_CONSOLE.
    # A console-free runner still outlives the dashboard, with its own group.
    flags = subprocess.CREATE_NO_WINDOW
    if new_process_group:
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return {"creationflags": flags}
