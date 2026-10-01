"""Browser use over HTTP: the extension's socket and the operator's controls.

- `ws_router`  /api/browser/ws — the ONE door a `browser`-scoped device token
               opens. The token comes in the first frame ({type: hello,
               token}), never the URL: a browser's WebSocket API cannot set an
               Authorization header. The route is exempt from the cookie
               same-origin gate (auth._ORIGIN_EXEMPT_EXACT) because it never
               reads the cookie — the extension's socket carries the operator's
               Jav3 cookie when they are logged in in that browser, from a
               chrome-extension:// origin.
- `router`     cookie-only operator routes (Settings → Browser use): per-project
               grants, Stop, the audit tail. No device token reaches these.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import browser, devicetokens
from .auth import require_user

ws_router = APIRouter(tags=["browser"])
router = APIRouter(prefix="/api/browser", tags=["browser"],
                   dependencies=[Depends(require_user)])


@ws_router.websocket("/api/browser/ws")
async def browser_ws(ws: WebSocket):
    await ws.accept()
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(),
                                                  browser.HELLO_TIMEOUT_S))
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect, KeyError):
        await ws.close(code=4400)
        return
    if not isinstance(hello, dict) or hello.get("type") != "hello":
        await ws.close(code=4400)
        return
    tok = hello.get("token")
    dev = await devicetokens.verify(tok if isinstance(tok, str) else None)
    if dev is None:
        await ws.close(code=4401)
        return
    if dev["scope"] != "browser":
        await browser._event("browser_refused", f"a '{dev['scope']}' token "
                             f"('{dev['name']}') tried to connect as a browser",
                             detail={"device_id": dev["device_id"], "scope": dev["scope"]},
                             dedup=("browser_scope", dev["device_id"]))
        await ws.close(code=4403)
        return
    b = await browser.attach(dev["device_id"], dev["name"], ws, hello,
                             ws.headers.get("host", ""))
    why = "disconnected"
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), browser.IDLE_DROP_S)
            except asyncio.TimeoutError:
                why = "went silent"
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            reply = await browser.on_frame(b, msg)
            if reply is not None:
                await b.send(reply)
    except (WebSocketDisconnect, RuntimeError, KeyError):
        pass
    finally:
        await browser.detach(b, why)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 — already closed
            pass


async def _browser_or_404(device_id: int) -> None:
    if not await browser.is_browser_token(device_id):
        raise HTTPException(status_code=404, detail="no such browser")


@router.get("")
async def list_browsers():
    return {"browsers": await browser.overview()}


class GrantBody(BaseModel):
    project: str = Field("", max_length=64)
    read: bool = False
    act: bool = False


@router.put("/{device_id:int}/grants")
async def put_grant(device_id: int, body: GrantBody):
    await _browser_or_404(device_id)
    try:
        return {"grants": await browser.set_grant(device_id, body.project,
                                                  read=body.read or body.act, act=body.act)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{device_id:int}/stop")
async def stop_browser(device_id: int, user: dict = Depends(require_user)):
    await _browser_or_404(device_id)
    return await browser.stop(device_id, by=user["username"], by_operator=True)


@router.get("/{device_id:int}/actions")
async def browser_actions(device_id: int, limit: int = 50):
    await _browser_or_404(device_id)
    return {"actions": await browser.recent_actions(device_id, limit)}
