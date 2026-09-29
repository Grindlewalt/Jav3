from backend import runtime
from backend.gitgate import flush_guest_writes
from backend.agent.tools.toolctx import require_project
from backend.vm import services


async def run(name: str = "", command: list | None = None, files: list | None = None,
              reason: str = "", description: str = "", workdir: str = ".",
              ports: list | None = None, restart: str = "no",
              egress_hosts: list | None = None, env: dict | None = None) -> str:
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    await flush_guest_writes(slug)      # the snapshot is taken host-side: this turn's files must be there
    args = {"name": name, "command": command, "files": files, "reason": reason,
            "description": description, "workdir": workdir, "ports": ports or [],
            "restart": restart, "egress_hosts": egress_hosts or [], "env": env or {}}
    try:
        row = await services.file_request(
            slug, args, conversation_id=runtime.conversation_id.get())
    except services.ServiceError as e:
        return f"error: {e}"
    sup = (f" It would replace approved service #{row['supersedes_id']} "
           "(the operator sees a diff)." if row["supersedes_id"] else "")
    return (f"service request #{row['id']} '{row['name']}' filed for '{slug}' "
            f"(snapshot sha256 {row['artifact_sha256'][:12]}, placement proposed: "
            f"{row['placement']}).{sup} Nothing runs until the operator approves "
            "it; check with service_status. The snapshot holds the files as they "
            "were at this call, this turn's writes included; edits after it are not in it.")
