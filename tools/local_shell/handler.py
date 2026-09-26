"""local_shell: runs on the /local client, brokered by backend/localexec.py."""
from backend import localexec


async def run(command: str, timeout_seconds: int = 120) -> str:
    return await localexec.call("local_shell", {"command": command,
                                                "timeout_seconds": timeout_seconds})
