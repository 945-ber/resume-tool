"""SQLite 存储：任务、简历版本、报告版本、用户"""
import hashlib
import secrets
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "resume_tool.db"

LEGACY_USERNAME = "legacy"  # 旧数据专属账号（无主任务自动归入）


def _conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT DEFAULT '',
        job_title TEXT DEFAULT '',
        jd_text TEXT DEFAULT '',
        resume_original TEXT DEFAULT '',
        resume_format TEXT DEFAULT '',
        resume_file TEXT DEFAULT '',
        photo_file TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    # 旧库迁移：补齐 job_title / resume_file / photo_file 列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if "job_title" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN job_title TEXT DEFAULT ''")
    if "resume_file" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN resume_file TEXT DEFAULT ''")
    if "photo_file" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN photo_file TEXT DEFAULT ''")
    # 用户体系：users 表 + tasks.user_id 归属
    if "user_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER DEFAULT NULL")
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        nickname TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        last_login TEXT DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS versions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        title TEXT DEFAULT '',
        content TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    # 事件/错误日志（管理员后台用）
    conn.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_time TEXT DEFAULT (datetime('now','localtime')),
        etype TEXT DEFAULT 'info',
        level TEXT DEFAULT 'info',
        user_name TEXT DEFAULT '',
        detail TEXT DEFAULT ''
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)")
    conn.commit()
    return conn


# ---------------- 密码与用户 ----------------
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            bytes.fromhex(salt), 100_000).hex()
    return f"{salt}${h}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return secrets.compare_digest(hash_password(password, salt), stored)
    except Exception:
        return False


def create_user(username: str, password_hash: str, nickname: str = "") -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO users(username,password_hash,nickname) VALUES(?,?,?)",
        (username, password_hash, nickname))
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return uid


def get_user_by_username(username: str):
    conn = _conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(user_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_last_login(user_id):
    conn = _conn()
    conn.execute("UPDATE users SET last_login=datetime('now','localtime') WHERE id=?",
                 (user_id,))
    conn.commit()
    conn.close()


def ensure_legacy_account() -> int:
    """确保旧数据专属账号存在；把当前所有无主任务归入它。返回 legacy 用户 id。
    固定登录口令：legacy / legacy@2026（可在个人中心登录查看旧记录，登录后可自行修改）"""
    conn = _conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (LEGACY_USERNAME,)).fetchone()
    if not row:
        cur = conn.execute(
            "INSERT INTO users(username,password_hash,nickname) VALUES(?,?,?)",
            (LEGACY_USERNAME, hash_password("legacy@2026", None), "旧数据归档账号"))
        uid = cur.lastrowid
    else:
        uid = row["id"]
        # 若 legacy 是随机密码创建的旧版本，统一重置为固定口令，保证可登录
        cur = conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password("legacy@2026", None), uid))
    conn.execute("UPDATE tasks SET user_id=? WHERE user_id IS NULL", (uid,))
    conn.commit()
    conn.close()
    return uid


def set_task_user(task_id, user_id):
    conn = _conn()
    conn.execute("UPDATE tasks SET user_id=? WHERE id=?", (user_id, task_id))
    conn.commit()
    conn.close()


# ---------------- 任务与用户数据 ----------------
def list_user_tasks(user_id, limit=50):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_user_photos(user_id, limit=24):
    """用户上传过的配图：按文件内容去重（同一张图重复上传只保留最新一张），并限制展示数量"""
    import hashlib
    conn = _conn()
    rows = conn.execute(
        "SELECT DISTINCT photo_file, created_at FROM tasks "
        "WHERE user_id=? AND photo_file!='' ORDER BY created_at DESC",
        (user_id,)).fetchall()
    conn.close()
    seen = set()
    result = []
    for r in rows:
        fp = Path(r["photo_file"])
        try:
            h = hashlib.md5(fp.read_bytes()).hexdigest() if fp.exists() else r["photo_file"]
        except Exception:
            h = r["photo_file"]
        if h in seen:
            continue
        seen.add(h)
        result.append(dict(r))
        if len(result) >= limit:
            break
    return result


def create_task(company_name="", job_title="", jd_text="", resume_original="", resume_format="", resume_file="", photo_file=""):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO tasks(company_name,job_title,jd_text,resume_original,resume_format,resume_file,photo_file) VALUES(?,?,?,?,?,?,?)",
        (company_name, job_title, jd_text, resume_original, resume_format, resume_file, photo_file))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def update_task(task_id, company_name=None, job_title=None, jd_text=None, resume_original=None, resume_file=None, photo_file=None):
    conn = _conn()
    if company_name is not None:
        conn.execute("UPDATE tasks SET company_name=? WHERE id=?", (company_name, task_id))
    if job_title is not None:
        conn.execute("UPDATE tasks SET job_title=? WHERE id=?", (job_title, task_id))
    if jd_text is not None:
        conn.execute("UPDATE tasks SET jd_text=? WHERE id=?", (jd_text, task_id))
    if resume_original is not None:
        conn.execute("UPDATE tasks SET resume_original=? WHERE id=?", (resume_original, task_id))
    if resume_file is not None:
        conn.execute("UPDATE tasks SET resume_file=? WHERE id=?", (resume_file, task_id))
    if photo_file is not None:
        conn.execute("UPDATE tasks SET photo_file=? WHERE id=?", (photo_file, task_id))
    conn.commit()
    conn.close()


def get_task(task_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_version(task_id, kind, title, content):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO versions(task_id,kind,title,content) VALUES(?,?,?,?)",
        (task_id, kind, title, content))
    conn.commit()
    vid = cur.lastrowid
    conn.close()
    return vid


def list_versions(task_id):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM versions WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_version(version_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def latest_version(task_id, kind):
    """取某任务某 kind 最新一条版本的 content（无则返回空串）"""
    conn = _conn()
    row = conn.execute(
        "SELECT content FROM versions WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
        (task_id, kind)).fetchone()
    conn.close()
    return row["content"] if row else ""


# ---------------- 事件 / 错误日志 ----------------
def log_event(etype="info", level="info", user_name="", detail=""):
    """记录一条系统事件（生成完成 / AI 错误 / 管理操作等）"""
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO events(etype,level,user_name,detail) VALUES(?,?,?,?)",
            (etype[:32], level[:16], (user_name or "")[:64], (detail or "")[:2000]))
        conn.commit()
        conn.close()
    except Exception:
        pass


def list_events(limit=100):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- 管理员后台查询 ----------------
def admin_stats():
    """后台核心指标：用户/任务/版本/图片（去重）/各类计数"""
    import hashlib
    conn = _conn()
    users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    tasks = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    versions = conn.execute("SELECT COUNT(*) c FROM versions").fetchone()["c"]
    rewrites = conn.execute(
        "SELECT COUNT(*) c FROM versions WHERE kind='rewritten'").fetchone()["c"]
    events = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    errors = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE level='error'").fetchone()["c"]
    # 图片按内容去重
    rows = conn.execute(
        "SELECT DISTINCT photo_file FROM tasks WHERE photo_file!=''").fetchall()
    conn.close()
    seen = set()
    photo_count = 0
    for r in rows:
        fp = Path(r["photo_file"])
        try:
            h = hashlib.md5(fp.read_bytes()).hexdigest() if fp.exists() else r["photo_file"]
        except Exception:
            h = r["photo_file"]
        if h in seen:
            continue
        seen.add(h)
        photo_count += 1
    return {
        "users": users, "tasks": tasks, "versions": versions, "rewrites": rewrites,
        "photos": photo_count, "events": events, "errors": errors,
    }


def admin_list_users():
    """全部用户 + 每个用户的任务数/图片数"""
    conn = _conn()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    out = []
    for r in rows:
        uid = r["id"]
        task_count = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE user_id=?", (uid,)).fetchone()["c"]
        photo_count = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE user_id=? AND photo_file!=''",
            (uid,)).fetchone()["c"]
        last_task = conn.execute(
            "SELECT created_at FROM tasks WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (uid,)).fetchone()
        u = dict(r)
        u.pop("password_hash", None)
        u["task_count"] = task_count
        u["photo_count"] = photo_count
        u["last_task_at"] = last_task["created_at"] if last_task else ""
        out.append(u)
    conn.close()
    return out


def admin_list_tasks(limit=100):
    """全部任务（含用户名 + 版本数）"""
    conn = _conn()
    rows = conn.execute(
        "SELECT t.*, u.username AS owner FROM tasks t "
        "LEFT JOIN users u ON u.id=t.user_id ORDER BY t.id DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        t = dict(r)
        t["version_count"] = conn.execute(
            "SELECT COUNT(*) c FROM versions WHERE task_id=?", (t["id"],)).fetchone()["c"]
        t["versions"] = [
            {"id": v["id"], "kind": v["kind"], "title": v["title"],
             "created_at": v["created_at"]}
            for v in conn.execute(
                "SELECT id,kind,title,created_at FROM versions WHERE task_id=? ORDER BY id",
                (t["id"],)).fetchall()]
        out.append(t)
    conn.close()
    return out


def delete_user(user_id):
    """删除用户及其全部任务/版本/配图文件（管理员操作，不可恢复）"""
    import shutil
    conn = _conn()
    rows = conn.execute(
        "SELECT photo_file, resume_file FROM tasks WHERE user_id=?", (user_id,)).fetchall()
    files = set()
    for r in rows:
        for k in ("photo_file", "resume_file"):
            fp = Path(r[k]) if r[k] else None
            if fp and fp.exists():
                files.add(str(fp))
    conn.execute("DELETE FROM versions WHERE task_id IN (SELECT id FROM tasks WHERE user_id=?)",
                 (user_id,))
    conn.execute("DELETE FROM tasks WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    for fp in files:
        try:
            Path(fp).unlink(missing_ok=True)
        except Exception:
            pass
    return True
