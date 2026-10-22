"""Operator API for environment requests (docs/agent-requests-contract.md, 1).

Cookie-only (require_user) and same-origin gated like packages_api. An agent's
only way in is the `environment_request` tool, which can only file a pending
row."""
from fastapi import APIRouter, Depends

from .auth import require_user

router = APIRouter(prefix="/api/environments", tags=["environments"],
                   dependencies=[Depends(require_user)])
