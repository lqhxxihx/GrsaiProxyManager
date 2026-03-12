import json
import logging
import os
import secrets
import uuid
from typing import List

import bcrypt
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

from config import UPSTREAM_BASE_URL, ADMIN_PASSWORD_HASH, ADMIN_PASSWORD
from key_manager import key_manager
from proxy import proxy_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

RESULTS_DIR = "results"
RESULTS_INDEX = os.path.join(RESULTS_DIR, "index.json")
os.makedirs(RESULTS_DIR, exist_ok=True)


def _load_index() -> list:
    try:
        with open(RESULTS_INDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_index(items: list) -> None:
    with open(RESULTS_INDEX, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)

app = FastAPI(title="GrsaiProxyManager")

# ── Session store (in-memory) ──────────────────────────────────────────────────
_sessions: set = set()
SESSION_COOKIE = "admin_session"
# 运行时密码哈希（支持热更新）
_pw_hash = ADMIN_PASSWORD_HASH.encode() if ADMIN_PASSWORD_HASH else b""


def _verify_password(pwd: str) -> bool:
    """验证密码，优先使用 bcrypt 哈希，兼容旧版明文。"""
    if _pw_hash:
        try:
            return bcrypt.checkpw(pwd.encode(), _pw_hash)
        except Exception:
            return False
    # 兼容旧版明文
    return bool(ADMIN_PASSWORD) and pwd == ADMIN_PASSWORD


def _check_auth(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    return token in _sessions


# ── Admin auth middleware ──────────────────────────────────────────────────────
class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/ui/admin/") and path not in ("/ui/admin/login", "/ui/admin/login/"):
            if not path.endswith(('.js', '.css', '.ico', '.png', '.jpg', '.woff', '.woff2')):
                token = request.cookies.get(SESSION_COOKIE)
                if token not in _sessions:
                    return RedirectResponse("/ui/admin/login")
        return await call_next(request)

app.add_middleware(AdminAuthMiddleware)


@app.on_event("startup")
async def startup():
    key_manager.start_background_refresh()


# ── Auth pages ─────────────────────────────────────────────────────────────────

@app.get("/ui/admin/")
async def admin_index(request: Request):
    if not _check_auth(request):
        return RedirectResponse("/ui/admin/login")
    return FileResponse("static/admin/index.html")


@app.get("/ui/admin/login")
async def admin_login_page():
    return FileResponse("static/admin/login.html")


# ── Auth API ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@app.post("/admin/login")
async def admin_login(body: LoginRequest):
    if _verify_password(body.password):
        token = secrets.token_hex(32)
        _sessions.add(token)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            SESSION_COOKIE, token,
            max_age=86400,
            httponly=True,
            samesite="lax"
        )
        return resp
    return JSONResponse({"ok": False, "msg": "密码错误"}, status_code=401)


@app.post("/admin/change-password")
async def admin_change_password(body: ChangePasswordRequest, request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
    global _pw_hash
    if not _verify_password(body.old_password):
        return JSONResponse({"ok": False, "msg": "原密码错误"}, status_code=400)
    if len(body.new_password) < 6:
        return JSONResponse({"ok": False, "msg": "新密码至少6位"}, status_code=400)
    new_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt())
    _pw_hash = new_hash
    # 写入 .password 文件
    try:
        with open(".password", "w", encoding="utf-8") as f:
            f.write(new_hash.decode())
    except Exception as e:
        pass
    return JSONResponse({"ok": True})


@app.post("/admin/logout")
async def admin_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    _sessions.discard(token)
    response.delete_cookie(SESSION_COOKIE)
    return JSONResponse({"ok": True})


@app.get("/admin/check")
async def admin_check(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({"ok": True})


@app.post("/admin/verify-key")
async def verify_api_key(body: LoginRequest):
    if _verify_password(body.password):
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "msg": "API Key 错误"}, status_code=401)


@app.post("/admin/credits-summary")
async def credits_summary(body: LoginRequest):
    """验证密码后返回总积分，供画图界面使用"""
    if not _verify_password(body.password):
        return JSONResponse({"ok": False}, status_code=401)
    total = sum(e["credits"] for e in key_manager.keys if e.get("active"))
    active = sum(1 for e in key_manager.keys if e.get("active"))
    return JSONResponse({"ok": True, "total_credits": total, "active_keys": active})


# ── Key 管理 API ───────────────────────────────────────────────────────────────

class AddKeysRequest(BaseModel):
    keys: List[str]


@app.get("/admin/keys")
async def admin_list_keys(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(content={"keys": key_manager.list_keys()})


def _save_keys_to_env():
    """把当前内存中的 Key 列表同步写入 .env"""
    try:
        env_path = ".env"
        lines = open(env_path, encoding="utf-8").readlines()
        new_keys = ",".join(e["key"] for e in key_manager.keys)
        with open(env_path, "w", encoding="utf-8") as f:
            for line in lines:
                if line.startswith("GRSAI_API_KEYS="):
                    f.write(f"GRSAI_API_KEYS={new_keys}\n")
                else:
                    f.write(line)
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to save keys to .env: %s", e)


@app.post("/admin/keys")
async def admin_add_keys(body: AddKeysRequest, request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    added = []
    skipped = []
    existing = {e["key"] for e in key_manager.keys}
    for k in body.keys:
        k = k.strip()
        if not k:
            continue
        if k in existing:
            skipped.append(k)
            continue
        key_manager.keys.append({
            "key": k,
            "credits": 0,
            "active": True,
            "last_checked": None,
        })
        added.append(k)
        existing.add(k)
    if added:
        _save_keys_to_env()
    import asyncio
    asyncio.create_task(key_manager.refresh_all_credits())
    return JSONResponse(content={"added": len(added), "skipped": len(skipped)})


@app.delete("/admin/keys/{key_hint}")
async def admin_delete_key(key_hint: str, request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    before = len(key_manager.keys)
    key_manager.keys = [e for e in key_manager.keys if not e["key"].endswith(key_hint)]
    deleted = before - len(key_manager.keys)
    if key_manager.current_index >= max(len(key_manager.keys), 1):
        key_manager.current_index = 0
    if deleted:
        _save_keys_to_env()
    return JSONResponse(content={"deleted": deleted})


@app.post("/admin/keys/refresh")
async def admin_refresh_keys(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    await key_manager.refresh_all_credits()
    return JSONResponse(content={"status": "ok", "keys": key_manager.list_keys()})


@app.post("/admin/keys/refresh-subset")
async def admin_refresh_subset(body: AddKeysRequest, request: Request):
    """刷新指定 key_hint 列表对应的 Key 积分"""
    if not _check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    import asyncio
    # body.keys 传入的是 key_hint 后缀（如 '820eae'）
    targets = [e["key"] for e in key_manager.keys
               if any(e["key"].endswith(h) for h in body.keys)]
    await asyncio.gather(*[key_manager.refresh_credits(k) for k in targets], return_exceptions=True)
    return JSONResponse(content={"status": "ok", "keys": key_manager.list_keys()})


# ── 静态文件 UI（必须在 catch-all 之前）──────────────────────────────────────
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")
app.mount("/results", StaticFiles(directory="results"), name="results")


# ── Results API ───────────────────────────────────────────────────────────────

class SaveImageRequest(BaseModel):
    url: str
    prompt: str = ""
    model: str = ""


@app.post("/api/save-image")
async def save_image(body: SaveImageRequest):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(body.url)
            resp.raise_for_status()
        image_id = str(uuid.uuid4())
        file_path = os.path.join(RESULTS_DIR, f"{image_id}.png")
        with open(file_path, "wb") as f:
            f.write(resp.content)
        import time
        item = {
            "id": image_id,
            "local_url": f"/results/{image_id}.png",
            "prompt": body.prompt,
            "model": body.model,
            "timestamp": int(time.time() * 1000),
        }
        items = _load_index()
        items.insert(0, item)
        _save_index(items)
        return JSONResponse(item)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/results")
async def list_results():
    return JSONResponse({"items": _load_index()})


@app.delete("/api/results/{image_id}")
async def delete_result(image_id: str):
    file_path = os.path.join(RESULTS_DIR, f"{image_id}.png")
    if os.path.exists(file_path):
        os.remove(file_path)
    items = [i for i in _load_index() if i["id"] != image_id]
    _save_index(items)
    return JSONResponse({"ok": True})


# ── Transparent proxy — catch-all ──────────────────────────────────────────────

@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_all(request: Request, path: str):
    return await proxy_request(request, path)
