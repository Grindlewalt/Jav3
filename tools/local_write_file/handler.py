"""local_write_file: runs on the /local client, brokered by backend/localexec.py."""
from backend import localexec


async def run(path: str, content: str) -> str:
    return await localexec.call("local_write_file", {"path": path, "content": content})
