"""Operator API for skill install requests (docs/agent-requests-contract.md, 2).

Cookie-only (require_user). Prefixed /api/skill-requests, not /api/skills/,
whose /{slug} route would swallow it. An agent's only way in is the
`skill_request` tool, which can only file a pending row."""
from fastapi import APIRouter, Depends

from .auth import require_user

router = APIRouter(prefix="/api/skill-requests", tags=["skill-requests"],
                   dependencies=[Depends(require_user)])
