from backend import agentmsg


async def run() -> str:
    return await agentmsg.fetch_tool()
