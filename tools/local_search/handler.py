"""local_search: runs on the /local client, brokered by backend/localexec.py."""
from backend import localexec


async def run(query: str, path: str = ".", regex: bool = False) -> str:
    return await localexec.call("local_search", {"query": query, "path": path or ".",
                                                 "regex": bool(regex)})
