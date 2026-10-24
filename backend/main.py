"""简历智能改写工具 - FastAPI 后端入口"""
import concurrent.futures
import csv
import os
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
import zipfile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_client
import render
import intel
import ocr_local
import storage
from parser import parse_file

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
INTEL_CACHE_DIR = DATA_DIR / "intel_cache"

def _intel_cached(company, website_url, extra_text, ttl_hours=24):
    """公司情报按公司名缓存（前缀缓存友好：同一公司 24h 内情报块逐字节不变，只在真正更新时才重抓）"""
    safe = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", company or "该公司")[:40] or "unknown"
    cache_file = INTEL_CACHE_DIR / (safe + ".txt")
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < ttl_hours * 3600:
            try:
                return {"raw_text": cache_file.read_text(encoding="utf-8"), "sources": ["cache"]}
            except Exception:
                pass
    raw = intel.collect(company or "该公司", website_url, extra_text)
    try:
        INTEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(raw["raw_text"], encoding="utf-8")
    except Exception:
        pass
    return raw

# 生产环境关闭接口文档（docs/redoc/openapi）：本地 .env 设 SHOW_API_DOCS=1 保留，服务器不设/设 0 则关闭
_show_docs = os.environ.get("SHOW_API_DOCS", "").strip() in ("1", "true", "True")
app = FastAPI(
    title="简历智能改写工具",
    version="1.2.0",
    docs_url="/docs" if _show_docs else None,
    redoc_url="/redoc" if _show_docs else None,
    openapi_url="/openapi.json" if _show_docs else None,
)

# ---------------- 安全加固（#3-#7） ----------------
# #3 上传文件大小上限 15MB（文件级硬校验）；multipart 请求体上限 16MB（含 boundary 开销余量）
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = 16 * 1024 * 1024
# #4 非文件请求体（JSON 等）上限 4MB，防超大 JSON 拖垮解析
MAX_JSON_BODY_BYTES = 4 * 1024 * 1024
# #5 登录 token 有效期 7 天（到期自动失效，需重新登录）
TOKEN_TTL = 7 * 24 * 3600


@app.middleware("http")
async def _security_headers_and_size(request: Request, call_next):
    """#4 请求体大小限制（按 Content-Length 提前拦截）+ #7 全站安全响应头"""
    cl = request.headers.get("content-length")
    if cl:
        try:
            size = int(cl)
        except ValueError:
            size = 0
        ctype = request.headers.get("content-type", "")
        limit = MAX_UPLOAD_REQUEST_BYTES if "multipart/form-data" in ctype else MAX_JSON_BODY_BYTES
        if size > limit:
            return JSONResponse(status_code=413, content={"detail": "请求体过大（文件最大 15MB）"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'"
    )
    try:
        del response.headers["Server"]
    except Exception:
        pass
    return response


# ---------------- 用户体系 ----------------
TOKENS: dict[str, tuple[int, float]] = {}  # token -> (user_id, 过期时间戳)（内存态，重启后需重新登录）

# ---------------- 管理员体系 ----------------
ADMIN_TOKENS: dict[str, str] = {}  # token -> 会话名（内存态）

def _require_admin(request: Request):
    token = request.headers.get("X-Admin-Token") or ""
    if token not in ADMIN_TOKENS:
        raise HTTPException(401, "管理员未登录或会话已过期")


class EggVerifyReq(BaseModel):
    seq: list[int] = []


# 彩蛋解锁序列（前端不存口令：九柱点击序列提交后端比对，序列只在本机 .env）
# 连续失败 5 次按 IP 锁定 10 分钟，防暴力枚举
EGG_SEQ_FAIL: dict[str, dict] = {}  # ip -> {"n": int, "lock": float}


def _egg_sequence() -> list[int]:
    """读取 .env 的 EGG_SEQUENCE（数字串 → 点击序列列表）；未配置时使用内置默认序列"""
    try:
        for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("EGG_SEQUENCE="):
                raw = line.split("=", 1)[1].strip()
                if raw:
                    return [int(x) for x in re.findall(r"\d", raw)]
    except Exception:
        pass
    return [6, 6, 1]


def _client_ip(request: Request) -> str:
    """nginx 反代后取真实客户端 IP（X-Forwarded-For 首个地址）；本地直连回退 client.host"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "local"


@app.get("/api/admin/egg/len")
def admin_egg_len():
    """只向前端暴露序列长度（不暴露序列本身）：前端点满长度即自动提交校验"""
    return {"len": len(_egg_sequence())}


@app.post("/api/admin/egg/verify")
def admin_egg_verify(req: EggVerifyReq, request: Request):
    ip = _client_ip(request)
    now = time.time()
    st = EGG_SEQ_FAIL.get(ip)
    if st and st.get("lock") and now < st["lock"]:
        raise HTTPException(429, "尝试次数过多，请 10 分钟后再试")
    target = _egg_sequence()
    if not req.seq or req.seq != target:
        n = (st["n"] if st else 0) + 1
        EGG_SEQ_FAIL[ip] = {"n": n, "lock": (now + 600) if n >= 5 else 0}
        raise HTTPException(403, "解锁序列不正确")
    EGG_SEQ_FAIL.pop(ip, None)
    token = secrets.token_hex(24)
    ADMIN_TOKENS[token] = "admin"
    storage.log_event("admin", "info", "admin", "彩蛋解锁进入管理员")
    return {"token": token}


@app.post("/api/admin/logout")
def admin_logout(request: Request):
    token = request.headers.get("X-Admin-Token") or ""
    ADMIN_TOKENS.pop(token, None)
    return {"ok": True}


@app.get("/api/admin/stats")
def admin_stats(request: Request):
    _require_admin(request)
    return storage.admin_stats()


@app.get("/api/admin/users")
def admin_users(request: Request):
    _require_admin(request)
    return {"users": storage.admin_list_users()}


@app.get("/api/admin/tasks")
def admin_tasks(request: Request, limit: int = 100):
    _require_admin(request)
    return {"tasks": storage.admin_list_tasks(limit=min(limit, 500))}


@app.get("/api/admin/photos")
def admin_photos(request: Request):
    _require_admin(request)
    conn_rows = []
    try:
        import sqlite3
        conn = sqlite3.connect(storage.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn_rows = conn.execute(
            "SELECT t.photo_file, t.created_at, u.username AS owner, t.id AS task_id "
            "FROM tasks t LEFT JOIN users u ON u.id=t.user_id "
            "WHERE t.photo_file!='' ORDER BY t.id DESC").fetchall()
        conn.close()
    except Exception:
        pass
    # 按内容去重
    import hashlib
    from pathlib import Path as _P
    seen = set()
    photos = []
    for r in conn_rows:
        fp = _P(r["photo_file"])
        try:
            h = hashlib.md5(fp.read_bytes()).hexdigest() if fp.exists() else r["photo_file"]
        except Exception:
            h = r["photo_file"]
        if h in seen:
            continue
        seen.add(h)
        fname = fp.name if fp else ""
        photos.append({"name": fname, "url": f"/api/uploads/{fname}",
                       "created_at": r["created_at"], "owner": r["owner"],
                       "task_id": r["task_id"]})
    return {"photos": photos[:100]}


@app.get("/api/admin/usage")
def admin_usage(request: Request):
    _require_admin(request)
    rows = []
    try:
        with open(DATA_DIR / "usage.csv", encoding="utf-8") as f:
            for r in csv.reader(f):
                if len(r) >= 5:
                    rows.append(r)
    except Exception:
        pass
    total_prompt = sum(int(r[3] or 0) for r in rows)
    total_comp = sum(int(r[4] or 0) for r in rows)
    total_hit = sum(int(r[5] or 0) for r in rows if len(r) >= 6)
    total_miss = sum(int(r[6] or 0) for r in rows if len(r) >= 7)
    today = time.strftime("%Y-%m-%d")
    today_rows = [r for r in rows if r[0].startswith(today)]
    stage_map = {}
    for r in rows:
        st = r[2] or "misc"
        item = stage_map.setdefault(st, {"calls": 0, "prompt": 0, "completion": 0})
        item["calls"] += 1
        item["prompt"] += int(r[3] or 0)
        item["completion"] += int(r[4] or 0)
    detail = [{"ts": r[0], "model": r[1], "stage": r[2],
               "prompt": int(r[3] or 0), "completion": int(r[4] or 0),
               "cache_hit": int(r[5] or 0) if len(r) >= 6 else 0,
               "cache_miss": int(r[6] or 0) if len(r) >= 7 else 0}
              for r in rows[-200:]][::-1]
    hit_rate = round(total_hit / max(1, total_hit + total_miss) * 100, 1)
    return {
        "calls": len(rows), "prompt_tokens": total_prompt,
        "completion_tokens": total_comp, "total_tokens": total_prompt + total_comp,
        "cache_hit_tokens": total_hit, "cache_miss_tokens": total_miss, "cache_hit_rate": hit_rate,
        "today_calls": len(today_rows),
        "today_tokens": sum(int(r[3] or 0) for r in today_rows) + sum(int(r[4] or 0) for r in today_rows),
        "stages": stage_map, "detail": detail,
    }


@app.get("/api/admin/events")
def admin_events(request: Request, limit: int = 100):
    _require_admin(request)
    return {"events": storage.list_events(limit=min(limit, 300))}


@app.get("/api/admin/balance")
def admin_balance(request: Request):
    _require_admin(request)
    # 余额查询可能因 DeepSeek 过载而慢/失败，最多等 6 秒，失败返回 null 不阻塞
    try:
        bal = ai_client.get_balance()
    except Exception:
        bal = None
    return {"balance": bal}


@app.post("/api/admin/users/{user_id}/delete")
def admin_delete_user(user_id: int, request: Request):
    _require_admin(request)
    user = storage.get_user(user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user["username"] == "legacy":
        raise HTTPException(400, "旧数据归档账号不可删除")
    storage.delete_user(user_id)
    storage.log_event("admin", "error", "admin", f"管理员删除用户 {user['username']}（id={user_id}）")
    return {"ok": True}


def _current_user(request: Request):
    token = request.headers.get("X-Auth-Token") or ""
    ent = TOKENS.get(token)
    if not ent:
        return None
    uid, exp = ent
    if time.time() > exp:
        TOKENS.pop(token, None)  # token 过期：立即失效
        return None
    return storage.get_user(uid) if uid else None


def _auth_token(user_id: int) -> str:
    token = secrets.token_hex(24)
    TOKENS[token] = (user_id, time.time() + TOKEN_TTL)
    return token


# #6 登录失败限速：同 IP 连续失败 5 次锁 10 分钟（防密码爆破）
AUTH_FAIL: dict[str, dict] = {}  # ip -> {"n": int, "lock": float}
# 注册成功限速：同 IP 10 分钟内最多注册 5 个账号（防批量注册刷库）
REG_OK: dict[str, list[float]] = {}  # ip -> [成功时间戳]


def _auth_fail_check(ip: str):
    st = AUTH_FAIL.get(ip)
    if st and st.get("lock") and time.time() < st["lock"]:
        raise HTTPException(429, "尝试次数过多，请 10 分钟后再试")


def _auth_mark_fail(ip: str):
    st = AUTH_FAIL.get(ip) or {"n": 0, "lock": 0}
    st["n"] += 1
    if st["n"] >= 5:
        st["lock"] = time.time() + 600
    AUTH_FAIL[ip] = st


def _public_user(user: dict) -> dict:
    """对外返回用户信息时剔除敏感字段（密码哈希等）"""
    return {k: v for k, v in user.items() if k != "password_hash"}


class AuthReq(BaseModel):
    username: str = ""
    password: str = ""
    nickname: str = ""


@app.post("/api/auth/register")
def auth_register(req: AuthReq, request: Request):
    ip = _client_ip(request)
    now = time.time()
    ts = [t for t in REG_OK.get(ip, []) if now - t < 600]
    if len(ts) >= 5:
        raise HTTPException(429, "注册过于频繁，请稍后再试")
    username = req.username.strip()
    if len(username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if len(req.password) < 4:
        raise HTTPException(400, "密码至少 4 位")
    if storage.get_user_by_username(username):
        raise HTTPException(400, "用户名已存在，请直接登录")
    uid = storage.create_user(username, storage.hash_password(req.password),
                              req.nickname.strip())
    REG_OK[ip] = ts + [now]
    return {"token": _auth_token(uid), "user": _public_user(storage.get_user(uid))}


@app.post("/api/auth/login")
def auth_login(req: AuthReq, request: Request):
    ip = _client_ip(request)
    _auth_fail_check(ip)
    user = storage.get_user_by_username(req.username.strip())
    if not user or not storage.verify_password(req.password, user["password_hash"]):
        _auth_mark_fail(ip)
        raise HTTPException(400, "用户名或密码错误")
    AUTH_FAIL.pop(ip, None)
    storage.update_last_login(user["id"])
    return {"token": _auth_token(user["id"]), "user": _public_user(storage.get_user(user["id"]))}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.headers.get("X-Auth-Token") or ""
    TOKENS.pop(token, None)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    return {"user": _public_user(user)}


# 个人中心：严格归属校验，只能看自己的数据
@app.get("/api/me/tasks")
def me_tasks(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    tasks = storage.list_user_tasks(user["id"])
    for t in tasks:
        t["versions"] = [
            {"id": v["id"], "kind": v["kind"], "title": v["title"],
             "created_at": v["created_at"]}
            for v in storage.list_versions(t["id"])]
    return {"tasks": tasks}


@app.get("/api/task/{task_id}")
def api_task(task_id: int, request: Request):
    """个人中心载入历史任务：返回任务详情与全部版本（含正文），仅限本人"""
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    t = storage.get_task(task_id)
    if not t or t.get("user_id") != user["id"]:
        raise HTTPException(404, "任务不存在或无权访问")
    return {"task": t, "versions": storage.list_versions(task_id)}


@app.get("/api/uploads/{name}")
def upload_file(name: str):
    """个人中心常备图片访问（防路径穿越）"""
    name = Path(name).name
    fp = UPLOAD_DIR / name
    if not fp.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(fp))


@app.get("/api/me/photos")
def me_photos(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "未登录")
    photos = []
    for p in storage.list_user_photos(user["id"]):
        fname = Path(p["photo_file"]).name if p["photo_file"] else ""
        photos.append({"name": fname, "url": f"/api/uploads/{fname}",
                       "created_at": p["created_at"]})
    return {"photos": photos}


# 旧数据归档：启动时把无主任务归入专属 legacy 账号（可登录查看旧记录）
storage.ensure_legacy_account()

ALLOWED_EXT = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".txt"}


async def _save_upload(file: UploadFile, fpath: Path) -> int:
    """#3 分块落盘：累计超过 15MB 立即中断并清理残留，返回实际字节数"""
    size = 0
    try:
        with open(fpath, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(413, "文件超过 15MB 限制，请压缩后重试")
                f.write(chunk)
        return size
    except HTTPException:
        fpath.unlink(missing_ok=True)
        raise
    except Exception:
        fpath.unlink(missing_ok=True)
        raise

# 静态资源（前端 Vue/ElementPlus 库与页面）
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/api/balance")
def api_balance(request: Request):
    """查询 DeepSeek 账户实时余额（元）——仅管理员可见"""
    _require_admin(request)
    bal = ai_client.get_balance()
    return {"balance": bal}


@app.get("/api/usage")
def api_usage(request: Request):
    """返回累计 AI 调用用量（usage.csv 明细）——仅管理员可见"""
    _require_admin(request)
    rows = []
    try:
        with open(DATA_DIR / "usage.csv", encoding="utf-8") as f:
            for r in csv.reader(f):
                if len(r) >= 5:
                    rows.append(r)
    except Exception:
        pass
    total_prompt = sum(int(r[3] or 0) for r in rows)
    total_comp = sum(int(r[4] or 0) for r in rows)
    today = time.strftime("%Y-%m-%d")
    today_rows = [r for r in rows if r[0].startswith(today)]
    today_prompt = sum(int(r[3] or 0) for r in today_rows)
    today_comp = sum(int(r[4] or 0) for r in today_rows)
    # 分阶段聚合
    stage_map = {}
    for r in rows:
        st = r[2] or "misc"
        item = stage_map.setdefault(st, {"calls": 0, "prompt": 0, "completion": 0})
        item["calls"] += 1
        item["prompt"] += int(r[3] or 0)
        item["completion"] += int(r[4] or 0)
    stages = {k: dict(v) for k, v in stage_map.items()}
    # 明细（最近 30 条，倒序）
    detail = [{"ts": r[0], "model": r[1], "stage": r[2],
               "prompt": int(r[3] or 0), "completion": int(r[4] or 0)}
              for r in rows[-30:]][::-1]
    return {
        "calls": len(rows),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_comp,
        "total_tokens": total_prompt + total_comp,
        "today_calls": len(today_rows),
        "today_tokens": today_prompt + today_comp,
        "stages": stages,
        "detail": detail,
    }



@app.get("/")
def index():
    # 禁用浏览器缓存，保证前端改动即时生效（用户之前打开旧版就是因为缓存）
    return FileResponse(str(FRONTEND_DIR / "index.html"),
                        headers={"Cache-Control": "no-store, max-age=0",
                                 "Pragma": "no-cache", "Expires": "0"})


@app.get("/admin")
def admin_page():
    """管理员后台独立页面"""
    return FileResponse(str(FRONTEND_DIR / "admin.html"),
                        headers={"Cache-Control": "no-store, max-age=0",
                                 "Pragma": "no-cache", "Expires": "0"})


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), request: Request = None):
    ext = Path(file.filename or "resume.docx").suffix.lower() or ".txt"
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "不支持的文件格式，请上传 Word(.docx)/PDF/图片/TXT")
    fname = f"{uuid.uuid4().hex}{ext}"
    fpath = UPLOAD_DIR / fname
    await _save_upload(file, fpath)

    parsed = parse_file(str(fpath))
    task_id = storage.create_task(
        resume_original=parsed["text"], resume_format=parsed["format"])
    user = _current_user(request) if request else None
    if user:
        storage.set_task_user(task_id, user["id"])
    storage.update_task(task_id, resume_file=str(fpath))
    storage.save_version(task_id, "original", "原始简历", parsed["text"])

    if parsed["needs_ocr"]:
        # 本地 OCR 优先（0 token）；视觉 API 兜底仅在登录后可用（#5 未登录不消耗 API 额度）
        local_text = ocr_local.ocr_image(str(fpath))
        ocr_channel = "local"
        text = local_text
        if text and len(text.strip()) > 20:
            parsed["text"] = text
            parsed["needs_ocr"] = False
            storage.update_task(task_id, resume_original=text)
            storage.save_version(task_id, "original", "原始简历(本地识别)", text)
        elif _current_user(request) if request else False:
            try:
                text = ai_client.vision_extract_text(str(fpath), "resume")
                ocr_channel = "vision"
                if text and len(text) > 20:
                    parsed["text"] = text
                    parsed["needs_ocr"] = False
                    storage.update_task(task_id, resume_original=text)
                    storage.save_version(task_id, "original", "原始简历(OCR识别)", text)
                elif text and ("未识别到" in text or "没有" in text):
                    parsed["ocr_error"] = "这张图片中没有识别到简历相关内容，请确认上传的图片是否为简历，或直接手动粘贴"
            except Exception as e:
                parsed["ocr_error"] = str(e)[:200]
        else:
            parsed["ocr_error"] = "图片未能本地识别，登录后可使用 AI 增强识别；也可以直接手动粘贴简历文字"
        parsed["ocr_channel"] = ocr_channel

    return {
        "task_id": task_id,
        "format": parsed["format"],
        "text": parsed["text"],
        "needs_ocr": parsed["needs_ocr"],
        "ocr_error": parsed.get("ocr_error", ""),
        "ocr_channel": parsed.get("ocr_channel", "text"),
        "file_path": fname,
    }


@app.post("/api/parse_document")
async def parse_document(file: UploadFile = File(...), kind: str = Form("resume"), request: Request = None):
    """识别任意文档/图片中的文字：Word / PDF / TXT / 图片（OCR）；kind=resume|jd 决定提取口径"""
    ext = Path(file.filename or "doc.txt").suffix.lower() or ".txt"
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "不支持的文件格式，请上传 Word(.docx)/PDF/图片/TXT")
    fname = f"{uuid.uuid4().hex}{ext}"
    fpath = UPLOAD_DIR / fname
    await _save_upload(file, fpath)

    parsed = parse_file(str(fpath))
    if parsed["needs_ocr"]:
        # 本地 OCR 优先（0 token）；视觉 API 兜底仅在登录后可用（#5 未登录不消耗 API 额度）
        import asyncio
        local_text = await asyncio.to_thread(ocr_local.ocr_image, str(fpath))
        ocr_channel = "local"
        if local_text and len(local_text.strip()) > 20:
            parsed["text"] = local_text
            parsed["needs_ocr"] = False
        elif _current_user(request) if request else False:
            try:
                text = ai_client.vision_extract_text(str(fpath), kind if kind in ("resume", "jd") else "resume")
                ocr_channel = "vision"
                if text and len(text) > 20:
                    parsed["text"] = text
                    parsed["needs_ocr"] = False
                elif text and ("未识别到" in text or "没有" in text):
                    parsed["ocr_error"] = "这张图片中没有识别到" + ("招聘/JD" if kind == "jd" else "简历") + "相关内容，请确认上传的图片内容是否正确，或直接手动粘贴文字"
                else:
                    parsed["ocr_error"] = "图片识别结果为空，请确认图片清晰可读，或直接手动粘贴文字"
            except Exception as e:
                parsed["ocr_error"] = str(e)[:200]
        else:
            parsed["ocr_error"] = "图片未能本地识别，登录后可使用 AI 增强识别；也可以直接手动粘贴文字"
        parsed["ocr_channel"] = ocr_channel

    return {
        "format": parsed["format"],
        "text": parsed["text"],
        "needs_ocr": parsed["needs_ocr"],
        "ocr_error": parsed.get("ocr_error", ""),
        "ocr_channel": parsed.get("ocr_channel", "text"),
        "file_path": fname,
        "file_abs": str(fpath),
    }


class MetaReq(BaseModel):
    text: str


@app.post("/api/extract_meta")
def extract_meta(req: MetaReq, request: Request):
    """从简历或公司要求中自动提取：公司名称 / 岗位名称 / 求职意向"""
    if not _current_user(request):
        raise HTTPException(401, "请先登录后再使用 AI 识别")
    if not req.text.strip():
        raise HTTPException(400, "文本为空")
    return ai_client.extract_meta(req.text)


class GenerateReq(BaseModel):
    """一键生成：解析后的简历文本 + 投递目标 + JD + 可选情报 + 补充素材"""
    task_id: int = 0
    resume_text: str = ""
    company_name: str = ""
    job_title: str = ""
    website_url: str = ""
    jd_text: str = ""
    extra_text: str = ""
    resume_file: str = ""   # 上传的原始简历文件相对路径（用于导出时嵌入照片）
    jd_file: str = ""       # 上传的 JD 文件相对路径（备用）
    photo_file: str = ""    # 用户单独上传的配图（证件照/插画/作品），优先用于成品
    supplements: list = []  # 用户补充素材（口语原始内容，最多 3 条；由 AI 加工为简历语言）


@app.post("/api/upload_photo")
async def upload_photo(file: UploadFile = File(...)):
    """独立配图上传：证件照/插画/作品图，原图完整保存，生成时原比例嵌入简历"""
    ext = Path(file.filename or "photo.png").suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        raise HTTPException(400, "配图仅支持图片格式（PNG/JPG/WebP/BMP/GIF）")
    fname = f"photo_{uuid.uuid4().hex}{ext}"
    fpath = UPLOAD_DIR / fname
    await _save_upload(file, fpath)
    # 转 JPEG 压缩（>1600px 缩到 1600，保证嵌入体积小）
    try:
        from PIL import Image
        im = Image.open(fpath)
        im = im.convert("RGB") if im.mode in ("P", "RGBA", "CMYK") else im
        if max(im.size) > 1600:
            im.thumbnail((1600, 1600), Image.LANCZOS)
        jpg = fpath.with_suffix(".jpg")
        im.save(jpg, "JPEG", quality=88)
        if jpg.exists() and jpg.stat().st_size < fpath.stat().st_size:
            fpath.unlink(missing_ok=True)
            fpath = jpg
    except Exception:
        pass
    return {"photo_path": fpath.name}


@app.post("/api/task/{task_id}/photo")
async def task_set_photo(task_id: int, request: Request):
    """把已上传的配图即时挂到任务上：预览/导出无需重新生成即可看到照片"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "请求体需为 JSON")
    photo_name = str(payload.get("photo_path") or "").strip()
    if not photo_name:
        raise HTTPException(400, "缺少 photo_path")
    fp = UPLOAD_DIR / Path(photo_name).name
    if not fp.exists():
        raise HTTPException(404, "配图文件不存在")
    storage.update_task(task_id, photo_file=str(fp))
    return {"ok": True, "task_id": task_id}


@app.post("/api/generate")
def generate(req: GenerateReq, request: Request = None):
    """一键流程：自动补齐信息 → 并行(匹配分析+公司情报) → 并行生成 3 个成品版本"""
    # #5 AI 生成闸门：必须登录（AI 调用消耗 API 额度，未登录一律拒绝）
    if not (_current_user(request) if request else False):
        raise HTTPException(401, "请先登录后再生成简历（AI 生成需消耗 API 额度）")
    import time as _t
    _t0 = _t.time()
    def _log(msg):
        print(f"[generate] {_t.time()-_t0:6.1f}s | {msg}", flush=True)
    if not req.resume_text.strip():
        raise HTTPException(400, "简历为空，请先粘贴或上传简历")
    if not req.jd_text.strip():
        raise HTTPException(400, "请填写岗位要求 JD，或上传要求文件")
    _log(f"开始生成 company={req.company_name!r} job={req.job_title!r}")

    # 补充素材（口语原始内容，最多 3 条）：分析时作为新内容计入完整度；改写时单独交给 AI 做简历化包装
    supplements = [str(s).strip() for s in (req.supplements or []) if str(s).strip()][:3]
    supp_extra = ""
    if supplements:
        supp_extra = "\n\n【用户补充素材（口语原始内容，尚未成稿）】\n" + "\n".join(f"- {s}" for s in supplements)

    # 自动补齐缺失的公司名 / 岗位名（用户没填就自己识别，秒级本地规则优先）
    if not req.company_name.strip() or not req.job_title.strip():
        try:
            # 公司名只从 JD 里找（避免把简历里的学校/公司误认）
            if not req.company_name.strip():
                m = ai_client.extract_meta(req.jd_text)
                if m.get("company_name"):
                    req.company_name = m["company_name"]
            # 岗位名：优先 JD，其次简历求职意向
            if not req.job_title.strip():
                mj = ai_client.extract_meta(req.jd_text)
                if mj.get("job_title"):
                    req.job_title = mj["job_title"]
                else:
                    mr = ai_client.extract_meta(req.resume_text)
                    if mr.get("job_title"):
                        req.job_title = mr["job_title"]
        except Exception:
            pass  # 提取失败不阻塞，情报/改写仍可继续

    # 任务归属：优先使用已有 task_id，否则新建
    task = storage.get_task(req.task_id) if req.task_id else None
    resume_abs = ""
    if req.resume_file:
        resume_abs = str(UPLOAD_DIR / Path(req.resume_file).name)
    photo_abs = ""
    if req.photo_file:
        photo_abs = str(UPLOAD_DIR / Path(req.photo_file).name)
    if task:
        task_id = req.task_id
        storage.update_task(task_id, company_name=req.company_name,
                            job_title=req.job_title, jd_text=req.jd_text,
                            resume_original=req.resume_text,
                            resume_file=resume_abs,
                            photo_file=photo_abs)
    else:
        task_id = storage.create_task(
            company_name=req.company_name, job_title=req.job_title,
            jd_text=req.jd_text, resume_original=req.resume_text,
            resume_format="paste", resume_file=resume_abs,
            photo_file=photo_abs)
        user = _current_user(request) if request else None
        if user:
            storage.set_task_user(task_id, user["id"])
        storage.save_version(task_id, "original", "原始简历", req.resume_text)

    # 并行：① 匹配分析（原文 + 补充素材一并计入，完整度真实反映补充）+ ② 公司情报（官网未填则自动查询）
    def _run_analysis():
        analysis = ai_client.analyze_match(req.resume_text + supp_extra, req.jd_text)
        storage.save_version(task_id, "analysis", "匹配分析报告", repr(analysis))
        return analysis

    def _run_intel():
        raw = _intel_cached(req.company_name or "该公司", req.website_url, req.extra_text)
        if not raw["raw_text"].strip():
            raw["raw_text"] = (f"未通过公开渠道获取到该公司的有效情报（公司名：{req.company_name}）。"
                               f"以下结论基于有限信息，请谨慎参考。")
        summary = ai_client.intel_summary(
            raw["raw_text"], req.company_name or "该公司", req.resume_text)
        storage.save_version(task_id, "intel", "公司痛点与建议", repr(summary))
        return raw, summary

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(_run_analysis)
        fi = ex.submit(_run_intel)
        analysis = fa.result()
        raw, summary = fi.result()
    _log(f"分析+情报完成 analysis_len={len(repr(analysis))} intel_sources={len(raw['sources'])}")

    analysis_text = repr(analysis)[:1500]
    # 情报块直接用情报原文（逐字节稳定，配合 DeepSeek 前缀缓存命中；上限 3000 字）
    intel_text = raw["raw_text"].strip()[:3000]

    # ③ 生成 1 份岗位完全定制版（不满意可点"重新生成"再出 1 份）
    def _run():
        # 结果过短视为截断/异常，重试一次（去掉情报上下文，避免再次截断）
        out = ai_client.rewrite_resume(
            req.resume_text, req.jd_text, analysis_text, style="custom", intel_text=intel_text,
            supplements=supplements)
        if len(out) >= 600 or "事实核验" in out:
            return out
        out2 = ai_client.rewrite_resume(
            req.resume_text, req.jd_text, analysis_text[:800], style="custom", intel_text="",
            supplements=supplements)
        return out2 if len(out2) > len(out) else out

    out = ai_client.strip_md(_run())
    _log(f"主版本完成 len={len(out)}")
    vid = storage.save_version(task_id, "rewritten", "岗位完全定制版", out)
    versions = [{
        "key": "custom", "title": "岗位完全定制版", "desc": "唯一主成品：完全围绕目标岗位重写，贴 JD 关键词、突出真实优势，目标是让 HR 约你面试",
        "resume": out, "version_id": vid,
    }]
    # 记录生成完成事件（后台可见耗时）
    user = _current_user(request) if request else None
    storage.log_event("generate", "info", (user or {}).get("username", "游客"),
                      f"{req.company_name} / {req.job_title} | 耗时 {_t.time()-_t0:.0f}s | 成品 {len(out)} 字")
    return {
        "task_id": task_id,
        "company_name": req.company_name,
        "job_title": req.job_title,
        "analysis": analysis,
        "versions": versions,
        "intel": summary,
        "sources": raw["sources"][:20],
    }


class RegenerateReq(BaseModel):
    task_id: int


@app.post("/api/regenerate")
def regenerate(req: RegenerateReq):
    """不满意当前成品时，重新生成一份岗位完全定制版（不重复跑分析/情报）"""
    task = storage.get_task(req.task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if not task["jd_text"].strip():
        raise HTTPException(400, "任务缺少 JD，无法重新生成")
    # 复用库里最新的分析/情报结果，只重跑改写
    analysis_v = storage.latest_version(req.task_id, "analysis")
    intel_v = storage.latest_version(req.task_id, "intel")
    analysis_text = (analysis_v or "")[:1500]
    intel_text = (intel_v or "")[:1000]
    out = ai_client.strip_md(ai_client.rewrite_resume(
        task["resume_original"], task["jd_text"], analysis_text, style="custom", intel_text=intel_text))
    if len(out) < 600 and "事实核验" not in out:
        out = ai_client.rewrite_resume(
            task["resume_original"], task["jd_text"], analysis_text[:800], style="custom", intel_text="")
    vid = storage.save_version(req.task_id, "rewritten", "岗位完全定制版", out)
    return {"version_id": vid, "resume": out}


# ---- 以下为分步接口（保留供高级使用） ----

class AnalyzeReq(BaseModel):
    task_id: int
    jd_text: str
    company_name: str = ""


@app.post("/api/analyze")
def analyze(req: AnalyzeReq):
    task = storage.get_task(req.task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if not task["resume_original"].strip():
        raise HTTPException(400, "简历为空，请先上传简历")
    if not req.jd_text.strip():
        raise HTTPException(400, "请填写岗位要求 JD")
    try:
        analysis = ai_client.analyze_match(task["resume_original"], req.jd_text)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    storage.update_task(req.task_id, jd_text=req.jd_text, company_name=req.company_name)
    vid = storage.save_version(req.task_id, "analysis", "匹配分析报告", repr(analysis))
    return {"version_id": vid, "analysis": analysis}


class RewriteReq(BaseModel):
    task_id: int
    jd_text: str = ""
    analysis: str = ""


@app.post("/api/rewrite")
def rewrite(req: RewriteReq):
    task = storage.get_task(req.task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    jd = req.jd_text or task["jd_text"]
    try:
        new_resume = ai_client.strip_md(ai_client.rewrite_resume(
            task["resume_original"], jd, req.analysis))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    vid = storage.save_version(req.task_id, "rewritten", "改写简历", new_resume)
    return {"version_id": vid, "resume": new_resume}


class IntelReq(BaseModel):
    task_id: int
    company_name: str
    website_url: str = ""
    extra_text: str = ""


@app.post("/api/intel")
def intel_route(req: IntelReq):
    task = storage.get_task(req.task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if not req.company_name.strip():
        raise HTTPException(400, "请填写公司名称")
    raw = intel.collect(req.company_name, req.website_url, req.extra_text)
    if not raw["raw_text"].strip():
        raw["raw_text"] = (f"未通过公开渠道获取到该公司的有效情报（公司名：{req.company_name}）。"
                           f"以下结论基于有限信息，请谨慎参考。")
    try:
        summary = ai_client.intel_summary(
            raw["raw_text"], req.company_name, task["resume_original"])
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    storage.update_task(req.task_id, company_name=req.company_name)
    vid = storage.save_version(req.task_id, "intel", "公司痛点与建议", repr(summary))
    return {"version_id": vid, "intel": summary, "sources": raw["sources"][:20]}


@app.get("/api/task/{task_id}")
def task_detail(task_id: int):
    task = storage.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {"task": task, "versions": storage.list_versions(task_id)}


@app.get("/api/export/{version_id}")
def export(version_id: int, format: str = "pdf", template: str = "A"):
    ver = storage.get_version(version_id)
    if not ver:
        raise HTTPException(404, "版本不存在")
    if ver["kind"] != "rewritten":
        raise HTTPException(400, "仅改写后的简历版本可导出成品")
    # 成品配图：优先用户单独上传的配图（photo_file），其次从原简历提取
    photo = None
    task = storage.get_task(ver["task_id"])
    if task and task.get("photo_file"):
        fp = Path(task["photo_file"])
        if fp.exists():
            photo = str(fp)
    if not photo and task and task.get("resume_file"):
        fp = Path(task["resume_file"])
        if fp.exists():
            if fp.suffix.lower() == ".docx":
                photo = render.extract_docx_photo(str(fp), EXPORT_DIR)
            elif fp.suffix.lower() == ".pdf":
                photo = render.extract_pdf_photo(str(fp), EXPORT_DIR)
    md = ver["content"]
    tpl = (template or "A").upper()
    if tpl not in ("A", "B", "C"):
        tpl = "A"
    base = EXPORT_DIR / f"rewritten_v{ver['id']}"
    fmt = (format or "pdf").lower()
    if fmt not in ("pdf", "png"):
        raise HTTPException(400, "仅支持 PDF 版或图片版导出（Word 版已下线）")

    if fmt == "png":
        pdf = render.html_to_pdf(render.md_to_html(md, photo, template=tpl), base.with_suffix(".pdf"))
        pngs = render.pdf_to_png(pdf, EXPORT_DIR)
        if not pngs:
            raise HTTPException(500, "图片版生成失败")
        return FileResponse(str(pngs[0]), filename=f"改写简历_v{ver['id']}.png",
                            media_type="image/png")
    pdf = render.html_to_pdf(render.md_to_html(md, photo, template=tpl), base.with_suffix(".pdf"))
    return FileResponse(str(pdf), filename=f"改写简历_v{ver['id']}.pdf",
                        media_type="application/pdf")


@app.get("/api/preview/{version_id}")
def preview(version_id: int, template: str = "A"):
    """模板即时预览：按所选模板渲染成品第 1 页小图，供前端切换模板时直观对比"""
    ver = storage.get_version(version_id)
    if not ver:
        raise HTTPException(404, "版本不存在")
    if ver["kind"] != "rewritten":
        raise HTTPException(400, "仅改写后的简历版本可预览")
    photo = None
    task = storage.get_task(ver["task_id"])
    if task and task.get("photo_file"):
        fp = Path(task["photo_file"])
        if fp.exists():
            photo = str(fp)
    if not photo and task and task.get("resume_file"):
        fp = Path(task["resume_file"])
        if fp.exists():
            if fp.suffix.lower() == ".docx":
                photo = render.extract_docx_photo(str(fp), EXPORT_DIR)
            elif fp.suffix.lower() == ".pdf":
                photo = render.extract_pdf_photo(str(fp), EXPORT_DIR)
    tpl = (template or "A").upper()
    if tpl not in ("A", "B", "C"):
        tpl = "A"
    md = ver["content"]
    base = EXPORT_DIR / f"preview_v{ver['id']}_{tpl}"
    pdf = render.html_to_pdf(render.md_to_html(md, photo, template=tpl), base.with_suffix(".pdf"))
    pngs = render.pdf_to_png(pdf, EXPORT_DIR, dpi=110)
    if not pngs:
        raise HTTPException(500, "预览生成失败")
    return FileResponse(str(pngs[0]), filename=f"preview_{tpl}_v{ver['id']}.png",
                        media_type="image/png")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
