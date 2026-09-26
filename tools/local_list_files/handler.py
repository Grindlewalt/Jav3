"""local_list_files: runs on the /local client, brokered by backend/localexec.py."""
from backend import localexec


async def run(path: str = ".", depth: int = 2) -> str:
    return await localexec.call("local_list_files", {"path": path or ".", "depth": depth})
