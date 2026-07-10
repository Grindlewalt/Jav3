from backend.webtools import search
from backend.agent.tools.toolctx import web_session


async def run(query: str) -> str:
    return await search(query, await web_session())
