from backend.webtools import search
from backend.agent.tools.toolctx import active_slug


async def run(query: str) -> str:
    session = (await active_slug()) or "global"
    return await search(query, session)
