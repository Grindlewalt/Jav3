"""local_read_file: runs on the /local client, brokered by backend/localexec.py."""
from backend import localexec


async def run(path: str, offset: int = 1, limit: int = 2000) -> str:
    return await localexec.call("local_read_file",
                                {"path": path, "offset": offset, "limit": limit})
