from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .config import settings, get_jwt_secret
from .db import get_db

COOKIE_NAME = "jarvis_token"

router = APIRouter(prefix="/api/auth", tags=["auth"])


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def make_token(user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_ttl_hours),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")


def user_from_token(token: str | None) -> dict | None:
    """Validate a session token -> user dict, or None. Shared by the HTTP
    dependency and the WebSocket path (which can't use Depends(require_user))."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return {"id": int(payload["sub"]), "username": payload["username"]}


def require_user(request: Request) -> dict:
    user = user_from_token(request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    db = await get_db()
    try:
        async with db.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (body.username,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="bad credentials")
    response.set_cookie(
        COOKIE_NAME,
        make_token(row["id"], row["username"]),
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_ttl_hours * 3600,
    )
    return {"ok": True, "username": row["username"]}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(require_user)):
    return user
