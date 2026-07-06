from backend.webtools import read
from backend.agent.tools.toolctx import active_slug


async def run(url: str) -> str:
    session = (await active_slug()) or "global"
    return await read(url, session)
