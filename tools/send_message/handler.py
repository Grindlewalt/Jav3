from backend import agentmsg


async def run(to: str, message: str) -> str:
    # Deliberately no `from`/sender argument: identity is resolved host-side
    # from the turn envelope (agentmsg.send_tool), so a compromised guest has
    # nothing to forge.
    return await agentmsg.send_tool(to, message)
