"""local_edit_file: runs on the /local client, brokered by backend/localexec.py."""
from backend import localexec


async def run(path: str, find: str, replace: str, all: bool = False) -> str:  # noqa: A002
    return await localexec.call("local_edit_file", {"path": path, "find": find,
                                                    "replace": replace, "all": bool(all)})
