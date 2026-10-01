"""/api/services: the operator's side of service boxes (WP3).

Cookie session only (require_user reads the cookie, never a bearer/device
token) and same-origin gated by auth.SameOriginMiddleware, like every other
control-plane route. There is no agent path to any of these: the agent's only
verbs are the tools service_request (files), service_status and service_logs
(read). The reviewer never decides a service: approval needs `acknowledge`,
an explicit `placement` and an explicit `expose_ports`, and nothing calls
services.approve except the handler below.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_user
from .config import settings
from .vm import portfwd, services

router = APIRouter(prefix="/api/services", tags=["services"],
                   dependencies=[Depends(require_user)])

services.register()


def _http(e: services.ServiceError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


def _who(user: dict) -> str:
    return user.get("username") or "operator"


@router.get("")
async def list_all(project: str | None = None):
    rows = await services.list_services(project)
    try:
        lan_ip, lan_error = portfwd.lan_address(), None
    except portfwd.PortfwdError as e:
        lan_ip, lan_error = "", str(e)
    # services_lan_ip: the configured address ONLY when it passes the checks
    # (dedicated, not Jav3's own); the UI offers LAN exposure only then
    return {"services": [services.row_json(r) for r in rows],
            "services_lan_ip": lan_ip,
            "services_lan_ip_configured": settings.services_lan_ip or "",
            "lan_error": lan_error if settings.services_lan_ip else None,
            "relays": portfwd.status()}


@router.get("/{sid}")
async def get_one(sid: int):
    row = await services.get(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="no such service")
    return {**services.row_json(row), "diff": await services.diff(sid),
            "relays": portfwd.status(sid)}


class ExposePort(BaseModel):
    port: int
    # contract: loopback | lan. The web UI's names are accepted too:
    # "host" = loopback, "none" = listed but not exposed (dropped).
    bind: Literal["loopback", "lan", "host", "none"]


class Approve(BaseModel):
    acknowledge: bool = False
    # REQUIRED, no default (operator decision 0.1): 422 without it
    placement: Literal["per_service", "per_project", "shared"]
    # REQUIRED too: the operator states the exposure, even when it is []
    expose_ports: list[ExposePort]


@router.post("/{sid}/approve")
async def approve(sid: int, body: Approve, user: dict = Depends(require_user)):
    if not body.acknowledge:
        raise HTTPException(status_code=400,
                            detail="approving a service requires acknowledge=true")
    alias = {"host": "loopback"}
    exposure = [{"port": e.port, "bind": alias.get(e.bind, e.bind)}
                for e in body.expose_ports if e.bind != "none"]
    try:
        row = await services.approve(
            sid, placement=body.placement, expose_ports=exposure, by=_who(user),
            by_operator=True)
    except services.ServiceError as e:
        raise _http(e) from None
    return services.row_json(row)


class Reject(BaseModel):
    reason: str = ""


@router.post("/{sid}/reject")
async def reject(sid: int, body: Reject, user: dict = Depends(require_user)):
    try:
        return services.row_json(await services.reject(sid, body.reason, _who(user),
                                                         by_operator=True))
    except services.ServiceError as e:
        raise _http(e) from None


@router.post("/{sid}/start")
async def start(sid: int, user: dict = Depends(require_user)):
    try:
        return services.row_json(await services.set_desired(sid, "running", _who(user)))
    except services.ServiceError as e:
        raise _http(e) from None


@router.post("/{sid}/stop")
async def stop(sid: int, user: dict = Depends(require_user)):
    try:
        return services.row_json(await services.set_desired(sid, "stopped", _who(user)))
    except services.ServiceError as e:
        raise _http(e) from None


class Revoke(BaseModel):
    confirm: bool = False
    delete_data: bool = False


@router.post("/{sid}/revoke")
async def revoke(sid: int, body: Revoke, user: dict = Depends(require_user)):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="revoke requires confirm=true")
    try:
        row = await services.revoke(sid, delete_data=body.delete_data, by=_who(user),
                                    by_operator=True)
    except services.ServiceError as e:
        raise _http(e) from None
    return {**services.row_json(row), "data_deleted": row.get("data_deleted")}


@router.get("/{sid}/logs")
async def logs(sid: int, lines: int = 100):
    try:
        text = await services.logs(sid, lines)
    except services.ServiceError as e:
        raise _http(e) from None
    return {"service_id": sid, "untrusted": True, "text": text}
