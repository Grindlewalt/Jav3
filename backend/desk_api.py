"""Computer use over HTTP: the desk socket and the operator's controls.

- `ws_router`  /api/desk/ws — the ONE door a `desk`-scoped device token opens.
               Bearer in the Authorization header on the upgrade (never in the
               URL, which gets logged); a cookie is not a desk credential.
- `router`     cookie-only operator routes (Settings → Computer use): the
               grants, Stop, the audit tail, and the shell approval queue. A
               device token of any scope never reaches these — grants are set
               only here, never by the client they govern.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import desk, devicetokens
from .auth import require_user

ws_router = APIRouter(tags=["desk"])
router = APIRouter(prefix="/api/desk", tags=["desk"], dependencies=[Depends(require_user)])

HELLO_TIMEOUT_S = 10


@ws_router.websocket("/api/desk/ws")
async def desk_ws(ws: WebSocket):
    header = ws.headers.get("authorization", "")
    dev = (await devicetokens.verify(header[7:].strip())
           if header[:7].lower() == "bearer " else None)
    if dev is None:
        await ws.close(code=4401)
        return
    if dev["scope"] != "desk":
        # a CLI token knocking on the desk door: the injection channel the
        # scope split exists to close. Say so once per burst.
        await desk._event("desk_refused", f"a '{dev['scope']}' token "
                          f"('{dev['name']}') tried to connect as a computer-use desk",
                          detail={"device_id": dev["device_id"], "scope": dev["scope"]},
                          dedup=("desk_scope", dev["device_id"]))
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S))
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect, KeyError):
        await ws.close(code=4400)
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await ws.close(code=4400)
        return
    d = await desk.attach(dev["device_id"], dev["name"], ws, hello)
    why = "disconnected"
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), desk.IDLE_DROP_S)
            except asyncio.TimeoutError:
                why = "went silent"
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            reply = desk.on_frame(d, msg)
            if reply is not None:
                await d.send(reply)
    except (WebSocketDisconnect, RuntimeError, KeyError):
        pass
    finally:
        await desk.detach(d, why)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 — already closed
            pass


# --- operator side (cookie) ----------------------------------------------------------

async def _desk_or_404(device_id: int) -> None:
    if not await desk.is_desk_token(device_id):
        raise HTTPException(status_code=404, detail="no such computer")


@router.get("")
async def list_desks():
    return {"desks": await desk.overview(), "pending": await desk.list_pending(),
            "trusted_minutes": desk.TRUSTED_MINUTES}


@router.get("/{device_id:int}/grants")
async def get_grants(device_id: int):
    await _desk_or_404(device_id)
    return await desk.get_grants(device_id)


class GrantsBody(BaseModel):
    screen: bool | None = None
    input: bool | None = None
    shell: str | None = Field(None, pattern="^(off|ask|trusted)$")
    allowlist: list[str] | None = Field(None, max_length=100)


@router.put("/{device_id:int}/grants")
async def put_grants(device_id: int, body: GrantsBody):
    await _desk_or_404(device_id)
    return await desk.set_grants(device_id, screen=body.screen, input=body.input,
                                 shell=body.shell, allowlist=body.allowlist)


@router.post("/{device_id:int}/stop")
async def stop_desk(device_id: int, user: dict = Depends(require_user)):
    await _desk_or_404(device_id)
    return await desk.stop(device_id, by=user["username"])


@router.get("/{device_id:int}/actions")
async def desk_actions(device_id: int, limit: int = 50):
    await _desk_or_404(device_id)
    return {"actions": await desk.recent_actions(device_id, limit)}


@router.get("/shell/pending")
async def shell_pending():
    return {"pending": await desk.list_pending()}


class DecideBody(BaseModel):
    action: str = Field(..., pattern="^(once|always|deny)$")


@router.post("/shell/{pending_id:int}")
async def shell_decide(pending_id: int, body: DecideBody,
                       user: dict = Depends(require_user)):
    r = await desk.decide(pending_id, body.action, user["username"])
    if not r["ok"]:
        raise HTTPException(status_code=409, detail=r["error"])
    return r
