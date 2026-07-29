import asyncio
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ENV_ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ENV_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@tizimod").strip()
ENV_CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yourchannel").strip()
ENV_SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/tizimod").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080") or 8080)

HTTP_RUNNER = None
BOT_USERNAME = ""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column(conn, table: str, definition: str):
    name = definition.split()[0]
    if name not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db():
    with db_connect() as conn:
        # Compatible with the old bot database.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                total_deposit INTEGER NOT NULL DEFAULT 0,
                monthly_deposit INTEGER NOT NULL DEFAULT 0,
                balance INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        for definition in [
            "coins INTEGER NOT NULL DEFAULT 0",
            "total_earned INTEGER NOT NULL DEFAULT 0",
            "total_spent INTEGER NOT NULL DEFAULT 0",
            "banned INTEGER NOT NULL DEFAULT 0",
            "last_seen TEXT",
        ]:
            add_column(conn, "users", definition)
        conn.execute("UPDATE users SET coins=balance WHERE coins=0 AND balance!=0")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                reward INTEGER NOT NULL,
                daily_limit INTEGER NOT NULL DEFAULT 1,
                min_seconds INTEGER NOT NULL DEFAULT 20,
                shortener_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                claimed_at TEXT,
                ip TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_task ON task_sessions(user_id, task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_claimed ON task_sessions(claimed_at)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shorteners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_template TEXT NOT NULL,
                response_key TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                stock INTEGER NOT NULL DEFAULT -1,
                delivery_type TEXT NOT NULL DEFAULT 'manual',
                delivery_value TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reward_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reward_id INTEGER NOT NULL,
                value TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                user_id INTEGER,
                used_at TEXT,
                UNIQUE(reward_id, value)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                reward_id INTEGER NOT NULL,
                reward_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                delivery TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        defaults = {
            "admin_id": str(ENV_ADMIN_ID or 0),
            "admin_username": ENV_ADMIN_USERNAME,
            "channel_username": ENV_CHANNEL_USERNAME,
            "support_url": ENV_SUPPORT_URL,
            "guide_text": "1. Chọn 🎯 Làm nhiệm vụ.\n2. Vượt link đến trang xác nhận.\n3. Quay lại bot và bấm ✅ Kiểm tra & Nhận xu.\n4. Dùng xu trong 🎁 Đổi quà.",
            "max_claims_per_ip_day": "10",
            "session_expire_minutes": "60",
            "allow_direct_task": "0",
            "maintenance": "0",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, str(v)))
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def log_admin(admin_id: int, action: str, detail: str = ""):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO audit_logs(admin_id,action,detail,created_at) VALUES (?,?,?,?)",
            (admin_id, action, detail, now_iso()),
        )
        conn.commit()


def ensure_user(update: Update):
    user = update.effective_user
    if not user:
        return
    full_name = " ".join(x for x in [user.first_name, user.last_name] if x)
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users(user_id, username, full_name, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                last_seen=excluded.last_seen
            """,
            (user.id, user.username or "", full_name, now_iso(), now_iso()),
        )
        conn.commit()


def is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    saved_id = int(get_setting("admin_id", "0") or 0)
    if saved_id:
        return user.id == saved_id
    if ENV_ADMIN_ID:
        return user.id == ENV_ADMIN_ID
    configured = get_setting("admin_username", ENV_ADMIN_USERNAME).lstrip("@").lower()
    if user.username and configured and user.username.lower() == configured:
        # First successful admin login pins the numeric Telegram ID.
        set_setting("admin_id", str(user.id))
        return True
    return False


def is_banned(user_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["banned"])


def fmt_num(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def fmt_xu(value: int) -> str:
    return f"{fmt_num(value)} Xu"


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Tài khoản", callback_data="account"),
            InlineKeyboardButton("🎯 Làm nhiệm vụ", callback_data="tasks"),
        ],
        [
            InlineKeyboardButton("🎁 Đổi quà", callback_data="rewards"),
            InlineKeyboardButton("📜 Lịch sử", callback_data="history"),
        ],
        [
            InlineKeyboardButton("🏆 BXH", callback_data="leaderboard"),
            InlineKeyboardButton("📖 Hướng dẫn", callback_data="guide"),
        ],
        [InlineKeyboardButton("🕺 Hỗ trợ / Liên hệ", url=get_setting("support_url", ENV_SUPPORT_URL))],
    ])


def back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Trang chủ", callback_data="home")]])


def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Người dùng", callback_data="adm:users"), InlineKeyboardButton("🎯 Nhiệm vụ", callback_data="adm:tasks")],
        [InlineKeyboardButton("💰 Quản lý xu", callback_data="adm:coins"), InlineKeyboardButton("🎁 Quà tặng", callback_data="adm:rewards")],
        [InlineKeyboardButton("🔑 Key / File", callback_data="adm:keys"), InlineKeyboardButton("📊 Thống kê", callback_data="adm:stats")],
        [InlineKeyboardButton("📢 Thông báo", callback_data="adm:broadcast"), InlineKeyboardButton("⚙️ Cài đặt", callback_data="adm:settings")],
        [InlineKeyboardButton("🛡 Bảo mật", callback_data="adm:security"), InlineKeyboardButton("🔗 API rút gọn", callback_data="adm:shorteners")],
        [InlineKeyboardButton("📜 Lịch sử hệ thống", callback_data="adm:history"), InlineKeyboardButton("📦 Đơn đổi quà", callback_data="adm:orders")],
        [InlineKeyboardButton("🏠 Menu người dùng", callback_data="home")],
    ])


def admin_back(section: str = "main") -> InlineKeyboardMarkup:
    data = "adm:main" if section == "main" else f"adm:{section}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data=data)]])


def home_text(user_id: int) -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    earned = row["total_earned"] if row else 0
    spent = row["total_spent"] if row else 0
    coins = row["coins"] if row else 0
    channel = escape(get_setting("channel_username", ENV_CHANNEL_USERNAME))
    admin = escape(get_setting("admin_username", ENV_ADMIN_USERNAME))
    return (
        "<b>CHÀO MỪNG BẠN ĐẾN VỚI BOT NHIỆM VỤ NHẬN XU</b>\n\n"
        "<blockquote>"
        f"🌐 <b>Kênh thông báo:</b> {channel}\n"
        f"🧑‍💼 <b>Admin:</b> {admin}\n"
        "➖ ➖ ➖ ➖ ➖ ➖ ➖ ➖\n"
        f"🏅 <b>Tổng xu đã kiếm:</b> {fmt_xu(earned)}\n"
        f"🎁 <b>Tổng xu đã đổi:</b> {fmt_xu(spent)}\n"
        f"💰 <b>Số dư:</b> {fmt_xu(coins)}"
        "</blockquote>"
    )


async def safe_edit(query, text: str, keyboard=None):
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard or back_home(),
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    uid = update.effective_user.id
    if is_banned(uid):
        await update.message.reply_text("🚫 Tài khoản của bạn đã bị khóa. Liên hệ admin để được hỗ trợ.")
        return
    if get_setting("maintenance", "0") == "1" and not is_admin(update):
        await update.message.reply_text("🛠 Bot đang bảo trì. Vui lòng quay lại sau.")
        return
    await update.message.reply_text(home_text(uid), parse_mode=ParseMode.HTML, reply_markup=home_keyboard())


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    if not is_admin(update):
        await update.message.reply_text("⛔ Bạn không có quyền sử dụng /admin.")
        return
    await update.message.reply_text(
        "<b>🛠 BẢNG QUẢN TRỊ BOT</b>\n\nChọn chức năng cần quản lý:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_main_keyboard(),
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await update.message.reply_text(f"🆔 Telegram ID của bạn: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


def today_start_iso() -> str:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def claimed_today(conn, user_id: int, task_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM task_sessions WHERE user_id=? AND task_id=? AND status='claimed' AND claimed_at>=?",
        (user_id, task_id, today_start_iso()),
    ).fetchone()["c"]


def get_public_base_url() -> str:
    return get_setting("public_base_url", PUBLIC_BASE_URL).strip().rstrip("/")


async def shorten_url(shortener, target_url: str, user_id: int, token: str) -> str:
    template = shortener["api_template"]
    values = {
        "url": target_url,
        "url_encoded": quote(target_url, safe=""),
        "user_id": str(user_id),
        "token": token,
    }
    try:
        api_url = template.format(**values)
    except KeyError as e:
        raise RuntimeError(f"API template chứa biến không hỗ trợ: {e}")

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(api_url, allow_redirects=True) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            response_key = (shortener["response_key"] or "").strip()
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = None
            if data is not None:
                if response_key:
                    current = data
                    for part in response_key.split("."):
                        current = current[part]
                    result = str(current)
                else:
                    result = ""
                    for key in ["shortenedUrl", "short_url", "shortUrl", "url", "link", "result"]:
                        if isinstance(data, dict) and data.get(key):
                            result = str(data[key])
                            break
                    if not result:
                        raise RuntimeError("Không tìm thấy URL trong JSON; hãy đặt response_key")
            else:
                result = body.strip()
            if not result.startswith(("http://", "https://")):
                raise RuntimeError("API không trả về link hợp lệ")
            return result


async def build_task_link(task, user_id: int, token: str) -> str:
    public_base = get_public_base_url()
    if not public_base:
        raise RuntimeError("Chưa cấu hình PUBLIC_BASE_URL")
    target = f"{public_base}/complete/{token}"
    with db_connect() as conn:
        shortener = None
        if task["shortener_id"]:
            shortener = conn.execute("SELECT * FROM shorteners WHERE id=? AND active=1", (task["shortener_id"],)).fetchone()
        if not shortener:
            shortener = conn.execute("SELECT * FROM shorteners WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    if shortener:
        return await shorten_url(shortener, target, user_id, token)
    if get_setting("allow_direct_task", "0") == "1":
        return target
    raise RuntimeError("Chưa có API rút gọn đang hoạt động")


async def task_complete_handler(request: web.Request):
    token = request.match_info.get("token", "")
    with db_connect() as conn:
        session = conn.execute(
            "SELECT s.*, t.name, t.min_seconds FROM task_sessions s JOIN tasks t ON t.id=s.task_id WHERE s.token=?",
            (token,),
        ).fetchone()
        if not session:
            return web.Response(text="Liên kết không hợp lệ hoặc đã hết hạn.", content_type="text/html", status=404)
        created = datetime.fromisoformat(session["created_at"])
        expire_minutes = int(get_setting("session_expire_minutes", "60") or 60)
        if datetime.now() > created + timedelta(minutes=expire_minutes):
            return web.Response(text="Phiên nhiệm vụ đã hết hạn. Hãy quay lại bot lấy link mới.", content_type="text/html", status=410)
        if (datetime.now() - created).total_seconds() < session["min_seconds"]:
            remain = int(session["min_seconds"] - (datetime.now() - created).total_seconds()) + 1
            return web.Response(text=f"Bạn quay lại quá sớm. Vui lòng chờ thêm khoảng {remain} giây.", content_type="text/html", status=429)
        if session["status"] == "claimed":
            msg = "Phiên này đã nhận xu trước đó."
        else:
            ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or ""
            max_ip = int(get_setting("max_claims_per_ip_day", "10") or 10)
            if max_ip > 0 and ip:
                c = conn.execute(
                    "SELECT COUNT(*) AS c FROM task_sessions WHERE ip=? AND status IN ('completed','claimed') AND completed_at>=?",
                    (ip, today_start_iso()),
                ).fetchone()["c"]
                if c >= max_ip:
                    return web.Response(text="Thiết bị/IP này đã vượt giới hạn xác nhận hôm nay.", content_type="text/html", status=429)
            conn.execute(
                "UPDATE task_sessions SET status='completed', completed_at=?, ip=? WHERE token=? AND status='pending'",
                (now_iso(), ip, token),
            )
            conn.commit()
            msg = "Xác nhận thành công. Hãy quay lại Telegram và bấm ‘Kiểm tra & Nhận xu’."
    back = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me/"
    html = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Hoàn thành</title>
<style>body{{font-family:Arial,sans-serif;background:#0f172a;color:#fff;display:grid;place-items:center;min-height:100vh;margin:0}}.card{{max-width:560px;background:#111827;padding:28px;border-radius:18px;text-align:center}}a{{display:inline-block;margin-top:18px;padding:12px 18px;background:#229ED9;color:#fff;text-decoration:none;border-radius:12px}}</style></head><body><div class='card'><h2>✅ Hoàn thành nhiệm vụ</h2><p>{escape(msg)}</p><a href='{escape(back)}'>Quay lại Telegram</a></div></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def health_handler(request: web.Request):
    return web.json_response({"ok": True, "service": "telegram-reward-bot"})


async def start_http_server(app: Application):
    global HTTP_RUNNER, BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username or ""
    base = get_public_base_url()
    if not base:
        print("[HTTP] PUBLIC_BASE_URL chưa cấu hình: chế độ xác nhận vượt link sẽ chưa hoạt động.")
        return
    web_app = web.Application()
    web_app.router.add_get("/", health_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.router.add_get("/complete/{token}", task_complete_handler)
    HTTP_RUNNER = web.AppRunner(web_app)
    await HTTP_RUNNER.setup()
    site = web.TCPSite(HTTP_RUNNER, "0.0.0.0", PORT)
    await site.start()
    print(f"[HTTP] Listening on 0.0.0.0:{PORT} | Public: {base}")


async def stop_http_server(app: Application):
    global HTTP_RUNNER
    if HTTP_RUNNER:
        await HTTP_RUNNER.cleanup()
        HTTP_RUNNER = None


async def user_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ensure_user(update)
    uid = update.effective_user.id
    data = query.data

    if is_banned(uid) and not is_admin(update):
        await query.answer("Tài khoản đã bị khóa.", show_alert=True)
        return
    if get_setting("maintenance", "0") == "1" and not is_admin(update) and data != "home":
        await query.answer("Bot đang bảo trì.", show_alert=True)
        return

    if data == "home":
        await safe_edit(query, home_text(uid), home_keyboard())
        return

    if data == "account":
        with db_connect() as conn:
            u = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        username = f"@{update.effective_user.username}" if update.effective_user.username else "Chưa đặt"
        text = (
            "<b>👤 THÔNG TIN TÀI KHOẢN</b>\n\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"👤 Username: {escape(username)}\n"
            f"💰 Số dư: <b>{fmt_xu(u['coins'])}</b>\n"
            f"🏅 Đã kiếm: <b>{fmt_xu(u['total_earned'])}</b>\n"
            f"🎁 Đã đổi: <b>{fmt_xu(u['total_spent'])}</b>"
        )
        await safe_edit(query, text)
        return

    if data == "tasks":
        with db_connect() as conn:
            tasks = conn.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id").fetchall()
            lines = ["<b>🎯 NHIỆM VỤ NHẬN XU</b>", ""]
            buttons = []
            if not tasks:
                lines.append("Hiện chưa có nhiệm vụ.")
            for t in tasks:
                done = claimed_today(conn, uid, t["id"])
                left = max(0, t["daily_limit"] - done)
                lines.append(f"• <b>{escape(t['name'])}</b> — +{fmt_xu(t['reward'])} — còn {left}/{t['daily_limit']} lượt")
                buttons.append([InlineKeyboardButton(f"🎯 {t['name']} (+{fmt_num(t['reward'])} Xu)", callback_data=f"task:start:{t['id']}")])
            buttons.append([InlineKeyboardButton("⬅️ Trang chủ", callback_data="home")])
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))
        return

    if data.startswith("task:start:"):
        task_id = int(data.rsplit(":", 1)[1])
        with db_connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=? AND active=1", (task_id,)).fetchone()
            if not task:
                await query.answer("Nhiệm vụ không tồn tại hoặc đã tắt.", show_alert=True)
                return
            if claimed_today(conn, uid, task_id) >= task["daily_limit"]:
                await query.answer("Bạn đã hết lượt nhiệm vụ này hôm nay.", show_alert=True)
                return
            old = conn.execute(
                "SELECT * FROM task_sessions WHERE user_id=? AND task_id=? AND status IN ('pending','completed') ORDER BY created_at DESC LIMIT 1",
                (uid, task_id),
            ).fetchone()
            if old:
                token = old["token"]
            else:
                token = secrets.token_urlsafe(24)
                conn.execute(
                    "INSERT INTO task_sessions(token,user_id,task_id,status,created_at) VALUES (?,?,?,'pending',?)",
                    (token, uid, task_id, now_iso()),
                )
                conn.commit()
        try:
            link = await build_task_link(task, uid, token)
        except Exception as e:
            await query.answer("Không tạo được link nhiệm vụ. Admin cần kiểm tra API rút gọn.", show_alert=True)
            if is_admin(update):
                print("Shortener error:", repr(e))
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Vượt Link Lấy Xu", url=link)],
            [InlineKeyboardButton("✅ Kiểm tra & Nhận xu", callback_data=f"task:claim:{token}")],
            [InlineKeyboardButton("⬅️ Danh sách nhiệm vụ", callback_data="tasks")],
        ])
        await safe_edit(
            query,
            f"<b>🎯 {escape(task['name'])}</b>\n\n💰 Thưởng: <b>+{fmt_xu(task['reward'])}</b>\n⏱ Thời gian tối thiểu: <b>{task['min_seconds']} giây</b>\n\n1. Bấm <b>Vượt Link Lấy Xu</b>.\n2. Hoàn thành link đến trang xác nhận.\n3. Quay lại đây bấm <b>Kiểm tra & Nhận xu</b>.",
            kb,
        )
        return

    if data.startswith("task:claim:"):
        token = data.split(":", 2)[2]
        with db_connect() as conn:
            row = conn.execute(
                "SELECT s.*,t.name,t.reward,t.daily_limit FROM task_sessions s JOIN tasks t ON t.id=s.task_id WHERE s.token=?",
                (token,),
            ).fetchone()
            if not row or row["user_id"] != uid:
                await query.answer("Phiên nhiệm vụ không hợp lệ.", show_alert=True)
                return
            if row["status"] == "claimed":
                await query.answer("Phiên này đã nhận xu rồi.", show_alert=True)
                return
            if row["status"] != "completed":
                await query.answer("Chưa xác nhận hoàn thành. Hãy vượt link đến trang cuối trước.", show_alert=True)
                return
            if claimed_today(conn, uid, row["task_id"]) >= row["daily_limit"]:
                await query.answer("Bạn đã đạt giới hạn hôm nay.", show_alert=True)
                return
            cur = conn.execute("UPDATE task_sessions SET status='claimed', claimed_at=? WHERE token=? AND status='completed'", (now_iso(), token))
            if cur.rowcount != 1:
                await query.answer("Phiên đã được xử lý.", show_alert=True)
                return
            conn.execute("UPDATE users SET coins=coins+?, total_earned=total_earned+? WHERE user_id=?", (row["reward"], row["reward"], uid))
            conn.execute(
                "INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES (?,?,?,?,?)",
                (uid, "task_reward", row["reward"], f"Nhiệm vụ: {row['name']}", now_iso()),
            )
            conn.commit()
        await query.answer(f"Đã cộng +{fmt_num(row['reward'])} Xu!", show_alert=True)
        await safe_edit(query, f"✅ <b>HOÀN THÀNH</b>\n\nBạn nhận được <b>+{fmt_xu(row['reward'])}</b> từ nhiệm vụ <b>{escape(row['name'])}</b>.", back_home())
        return

    if data == "rewards":
        with db_connect() as conn:
            rewards = conn.execute("SELECT * FROM rewards WHERE active=1 ORDER BY id").fetchall()
            user = conn.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()
            lines = ["<b>🎁 ĐỔI QUÀ</b>", f"💰 Số dư: <b>{fmt_xu(user['coins'])}</b>", ""]
            buttons = []
            if not rewards:
                lines.append("Hiện chưa có quà.")
            for r in rewards:
                if r["delivery_type"] == "keypool":
                    available = conn.execute("SELECT COUNT(*) AS c FROM reward_keys WHERE reward_id=? AND used=0", (r["id"],)).fetchone()["c"]
                    stock_text = str(available)
                else:
                    stock_text = "∞" if r["stock"] < 0 else str(r["stock"])
                lines.append(f"• <b>{escape(r['name'])}</b> — {fmt_xu(r['price'])} — kho {stock_text}")
                buttons.append([InlineKeyboardButton(f"🎁 {r['name']} — {fmt_num(r['price'])} Xu", callback_data=f"reward:view:{r['id']}")])
            buttons.append([InlineKeyboardButton("⬅️ Trang chủ", callback_data="home")])
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))
        return

    if data.startswith("reward:view:"):
        rid = int(data.rsplit(":", 1)[1])
        with db_connect() as conn:
            r = conn.execute("SELECT * FROM rewards WHERE id=? AND active=1", (rid,)).fetchone()
            if not r:
                await query.answer("Quà không tồn tại.", show_alert=True)
                return
            if r["delivery_type"] == "keypool":
                stock = conn.execute("SELECT COUNT(*) AS c FROM reward_keys WHERE reward_id=? AND used=0", (rid,)).fetchone()["c"]
            else:
                stock = r["stock"]
        stock_text = "∞" if stock < 0 else str(stock)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Xác nhận đổi", callback_data=f"reward:buy:{rid}")],
            [InlineKeyboardButton("⬅️ Đổi quà", callback_data="rewards")],
        ])
        await safe_edit(query, f"<b>🎁 {escape(r['name'])}</b>\n\n💰 Giá: <b>{fmt_xu(r['price'])}</b>\n📦 Còn: <b>{stock_text}</b>\n📨 Kiểu giao: <b>{escape(r['delivery_type'])}</b>", kb)
        return

    if data.startswith("reward:buy:"):
        rid = int(data.rsplit(":", 1)[1])
        delivery = None
        delivery_type = None
        order_id = None
        with db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT * FROM rewards WHERE id=? AND active=1", (rid,)).fetchone()
            u = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            if not r:
                conn.rollback(); await query.answer("Quà không tồn tại.", show_alert=True); return
            if u["coins"] < r["price"]:
                conn.rollback(); await query.answer("Bạn không đủ xu.", show_alert=True); return
            delivery_type = r["delivery_type"]
            if delivery_type == "keypool":
                keyrow = conn.execute("SELECT * FROM reward_keys WHERE reward_id=? AND used=0 ORDER BY id LIMIT 1", (rid,)).fetchone()
                if not keyrow:
                    conn.rollback(); await query.answer("Quà đã hết key.", show_alert=True); return
                delivery = keyrow["value"]
                conn.execute("UPDATE reward_keys SET used=1,user_id=?,used_at=? WHERE id=? AND used=0", (uid, now_iso(), keyrow["id"]))
            else:
                if r["stock"] == 0:
                    conn.rollback(); await query.answer("Quà đã hết hàng.", show_alert=True); return
                if r["stock"] > 0:
                    conn.execute("UPDATE rewards SET stock=stock-1 WHERE id=?", (rid,))
                delivery = r["delivery_value"] or ""
            status = "pending" if delivery_type == "manual" else "completed"
            cur = conn.execute(
                "INSERT INTO orders(user_id,reward_id,reward_name,price,status,delivery,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, rid, r["name"], r["price"], status, delivery, now_iso(), now_iso()),
            )
            order_id = cur.lastrowid
            conn.execute("UPDATE users SET coins=coins-?, total_spent=total_spent+? WHERE user_id=?", (r["price"], r["price"], uid))
            conn.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES (?,?,?,?,?)", (uid, "redeem", -r["price"], f"Đổi quà: {r['name']}", now_iso()))
            conn.commit()
        if delivery_type == "file_id" and delivery:
            try:
                await context.bot.send_document(chat_id=uid, document=delivery, caption=f"✅ Quà: {r['name']}")
            except Exception:
                await context.bot.send_message(chat_id=uid, text="✅ Đổi quà thành công nhưng gửi file lỗi. Admin sẽ kiểm tra đơn của bạn.")
        elif delivery_type in ("text", "keypool") and delivery:
            await context.bot.send_message(chat_id=uid, text=f"✅ {r['name']}\n\n{delivery}")
        elif delivery_type == "manual":
            admin_id = int(get_setting("admin_id", "0") or 0)
            if admin_id:
                try:
                    await context.bot.send_message(admin_id, f"📦 Có đơn thủ công mới #{order_id}\nUser: {uid}\nQuà: {r['name']}\nGiá: {fmt_xu(r['price'])}")
                except Exception:
                    pass
        await query.answer("Đổi quà thành công!", show_alert=True)
        msg = f"✅ <b>ĐỔI QUÀ THÀNH CÔNG</b>\n\n🎁 {escape(r['name'])}\n💰 Đã trừ: <b>{fmt_xu(r['price'])}</b>"
        if delivery_type == "manual":
            msg += f"\n📦 Mã đơn: <code>#{order_id}</code>\nAdmin sẽ xử lý đơn này."
        else:
            msg += "\n📨 Quà đã được gửi cho bạn."
        await safe_edit(query, msg, back_home())
        return

    if data == "history":
        with db_connect() as conn:
            rows = conn.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 15", (uid,)).fetchall()
        lines = ["<b>📜 LỊCH SỬ GẦN ĐÂY</b>", ""]
        if not rows:
            lines.append("Chưa có giao dịch.")
        for r in rows:
            sign = "+" if r["amount"] > 0 else ""
            lines.append(f"• {escape(r['note'] or r['type'])}: <b>{sign}{fmt_num(r['amount'])} Xu</b>\n  <code>{escape(r['created_at'][:16])}</code>")
        await safe_edit(query, "\n".join(lines))
        return

    if data == "leaderboard":
        with db_connect() as conn:
            rows = conn.execute("SELECT user_id,username,full_name,total_earned FROM users WHERE banned=0 ORDER BY total_earned DESC LIMIT 10").fetchall()
        lines = ["<b>🏆 BXH KIẾM XU</b>", ""]
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(rows, 1):
            name = f"@{r['username']}" if r["username"] else (r["full_name"] or str(r["user_id"]))
            prefix = medals[i-1] if i <= 3 else f"{i}."
            lines.append(f"{prefix} {escape(name)} — <b>{fmt_xu(r['total_earned'])}</b>")
        if not rows:
            lines.append("Chưa có dữ liệu.")
        await safe_edit(query, "\n".join(lines))
        return

    if data == "guide":
        guide = escape(get_setting("guide_text", ""))
        await safe_edit(query, f"<b>📖 HƯỚNG DẪN</b>\n\n{guide}")
        return


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ensure_user(update)
    if not is_admin(update):
        await query.answer("Không có quyền admin.", show_alert=True)
        return
    data = query.data
    aid = update.effective_user.id

    if data == "adm:main":
        context.user_data.pop("admin_action", None)
        await safe_edit(query, "<b>🛠 BẢNG QUẢN TRỊ BOT</b>\n\nChọn chức năng cần quản lý:", admin_main_keyboard())
        return

    if data == "adm:users":
        with db_connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            banned = conn.execute("SELECT COUNT(*) AS c FROM users WHERE banned=1").fetchone()["c"]
            active = conn.execute("SELECT COUNT(*) AS c FROM users WHERE last_seen>=?", ((datetime.now()-timedelta(days=1)).isoformat(timespec="seconds"),)).fetchone()["c"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Tìm user", callback_data="adm:ask:user_search")],
            [InlineKeyboardButton("🚫 Danh sách bị khóa", callback_data="adm:banned")],
            [InlineKeyboardButton("⬅️ Admin", callback_data="adm:main")],
        ])
        await safe_edit(query, f"<b>👥 NGƯỜI DÙNG</b>\n\n👥 Tổng: <b>{fmt_num(total)}</b>\n🟢 Hoạt động 24h: <b>{fmt_num(active)}</b>\n🚫 Bị khóa: <b>{fmt_num(banned)}</b>", kb)
        return

    if data == "adm:banned":
        with db_connect() as conn:
            rows = conn.execute("SELECT * FROM users WHERE banned=1 ORDER BY last_seen DESC LIMIT 20").fetchall()
        lines = ["<b>🚫 USER BỊ KHÓA</b>", ""]
        buttons=[]
        for u in rows:
            lines.append(f"• <code>{u['user_id']}</code> @{escape(u['username'] or '-')}")
            buttons.append([InlineKeyboardButton(f"Mở khóa {u['user_id']}", callback_data=f"adm:user:unban:{u['user_id']}")])
        if not rows: lines.append("Không có user bị khóa.")
        buttons.append([InlineKeyboardButton("⬅️ Người dùng", callback_data="adm:users")])
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))
        return

    if data.startswith("adm:user:show:"):
        target = int(data.rsplit(":",1)[1])
        await show_admin_user(query, target)
        return

    if data.startswith("adm:user:ban:") or data.startswith("adm:user:unban:"):
        parts=data.split(":")
        action=parts[2]; target=int(parts[3])
        with db_connect() as conn:
            conn.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if action=="ban" else 0, target)); conn.commit()
        log_admin(aid, action, str(target))
        await show_admin_user(query, target)
        return

    if data.startswith("adm:user:reset:"):
        target=int(data.rsplit(":",1)[1])
        with db_connect() as conn:
            conn.execute("DELETE FROM task_sessions WHERE user_id=? AND status!='claimed'", (target,)); conn.commit()
        log_admin(aid,"reset_sessions",str(target))
        await query.answer("Đã reset phiên nhiệm vụ chưa nhận xu.", show_alert=True)
        await show_admin_user(query,target)
        return

    if data == "adm:coins":
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Cộng xu", callback_data="adm:ask:addcoins"), InlineKeyboardButton("➖ Trừ xu", callback_data="adm:ask:subcoins")],
            [InlineKeyboardButton("🎁 Tặng xu toàn bộ", callback_data="adm:ask:giftall")],
            [InlineKeyboardButton("🏆 Top số dư", callback_data="adm:topcoins")],
            [InlineKeyboardButton("⬅️ Admin", callback_data="adm:main")],
        ])
        await safe_edit(query,"<b>💰 QUẢN LÝ XU</b>\n\nCộng/trừ xu được ghi vào lịch sử và audit log.",kb); return

    if data == "adm:topcoins":
        with db_connect() as conn:
            rows=conn.execute("SELECT user_id,username,coins FROM users ORDER BY coins DESC LIMIT 20").fetchall()
        lines=["<b>💰 TOP SỐ DƯ</b>",""]
        for i,u in enumerate(rows,1): lines.append(f"{i}. <code>{u['user_id']}</code> @{escape(u['username'] or '-')} — <b>{fmt_xu(u['coins'])}</b>")
        await safe_edit(query,"\n".join(lines),admin_back("coins")); return

    if data == "adm:tasks":
        with db_connect() as conn:
            rows=conn.execute("SELECT t.*, s.name AS shortener_name FROM tasks t LEFT JOIN shorteners s ON s.id=t.shortener_id ORDER BY t.id DESC LIMIT 30").fetchall()
        lines=["<b>🎯 QUẢN LÝ NHIỆM VỤ</b>",""]
        buttons=[[InlineKeyboardButton("➕ Thêm nhiệm vụ",callback_data="adm:ask:task_add")]]
        for t in rows:
            st="🟢" if t["active"] else "🔴"
            lines.append(f"{st} #{t['id']} <b>{escape(t['name'])}</b> — +{fmt_xu(t['reward'])} — {t['daily_limit']}/ngày")
            buttons.append([InlineKeyboardButton(f"{st} #{t['id']} {t['name'][:24]}",callback_data=f"adm:task:{t['id']}")])
        if not rows: lines.append("Chưa có nhiệm vụ.")
        buttons.append([InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")])
        await safe_edit(query,"\n".join(lines),InlineKeyboardMarkup(buttons)); return

    if data.startswith("adm:task:") and data.count(":")==2:
        tid=int(data.rsplit(":",1)[1]); await show_admin_task(query,tid); return

    if data.startswith("adm:task:toggle:"):
        tid=int(data.rsplit(":",1)[1])
        with db_connect() as conn:
            conn.execute("UPDATE tasks SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(tid,)); conn.commit()
        log_admin(aid,"toggle_task",str(tid)); await show_admin_task(query,tid); return

    if data.startswith("adm:task:delete:"):
        tid=int(data.rsplit(":",1)[1])
        with db_connect() as conn:
            conn.execute("UPDATE tasks SET active=0 WHERE id=?",(tid,)); conn.commit()
        log_admin(aid,"disable_task",str(tid)); await query.answer("Đã tắt nhiệm vụ.",show_alert=True); await show_admin_task(query,tid); return

    if data.startswith("adm:task:edit:"):
        tid=int(data.rsplit(":",1)[1])
        context.user_data["admin_action"]="task_edit"
        context.user_data["edit_id"]=tid
        await safe_edit(query,"<b>✏️ SỬA NHIỆM VỤ</b>\n\nGửi:\n<code>Tên | Xu thưởng | Giới hạn/ngày | Chờ giây | Shortener ID</code>\nShortener ID = 0 để tự chọn API active.",InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy",callback_data=f"adm:task:{tid}")]])); return

    if data == "adm:rewards":
        with db_connect() as conn:
            rows=conn.execute("SELECT * FROM rewards ORDER BY id DESC LIMIT 30").fetchall()
        lines=["<b>🎁 QUẢN LÝ QUÀ</b>",""]
        buttons=[
            [InlineKeyboardButton("➕ Thêm quà",callback_data="adm:ask:reward_add")],
            [InlineKeyboardButton("📦 Đơn chờ duyệt",callback_data="adm:orders")],
        ]
        for r in rows:
            st="🟢" if r["active"] else "🔴"
            lines.append(f"{st} #{r['id']} <b>{escape(r['name'])}</b> — {fmt_xu(r['price'])} — {escape(r['delivery_type'])}")
            buttons.append([InlineKeyboardButton(f"{st} #{r['id']} {r['name'][:24]}",callback_data=f"adm:reward:{r['id']}")])
        if not rows: lines.append("Chưa có quà.")
        buttons.append([InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")])
        await safe_edit(query,"\n".join(lines),InlineKeyboardMarkup(buttons)); return

    if data.startswith("adm:reward:") and data.count(":")==2:
        rid=int(data.rsplit(":",1)[1]); await show_admin_reward(query,rid); return

    if data.startswith("adm:reward:toggle:"):
        rid=int(data.rsplit(":",1)[1])
        with db_connect() as conn:
            conn.execute("UPDATE rewards SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(rid,)); conn.commit()
        log_admin(aid,"toggle_reward",str(rid)); await show_admin_reward(query,rid); return

    if data.startswith("adm:reward:delete:"):
        rid=int(data.rsplit(":",1)[1])
        with db_connect() as conn:
            conn.execute("UPDATE rewards SET active=0 WHERE id=?",(rid,)); conn.commit()
        log_admin(aid,"disable_reward",str(rid)); await query.answer("Đã ẩn quà.",show_alert=True); await show_admin_reward(query,rid); return

    if data.startswith("adm:reward:edit:"):
        rid=int(data.rsplit(":",1)[1])
        context.user_data["admin_action"]="reward_edit"
        context.user_data["edit_id"]=rid
        await safe_edit(query,"<b>✏️ SỬA QUÀ</b>\n\nGửi:\n<code>Tên | Giá xu | Kho | Kiểu | Nội dung</code>\nKiểu: text/manual/keypool/file_id. Kho -1 = không giới hạn. Dùng <code>-</code> để giữ nội dung hiện tại.",InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy",callback_data=f"adm:reward:{rid}")]])); return

    if data == "adm:orders":
        with db_connect() as conn:
            rows=conn.execute("SELECT * FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 30").fetchall()
        lines=["<b>📦 ĐƠN CHỜ DUYỆT</b>",""]
        buttons=[]
        for o in rows:
            lines.append(f"• #{o['id']} — user <code>{o['user_id']}</code> — {escape(o['reward_name'])} — {fmt_xu(o['price'])}")
            buttons.append([InlineKeyboardButton(f"📦 Đơn #{o['id']}",callback_data=f"adm:order:{o['id']}")])
        if not rows: lines.append("Không có đơn chờ.")
        buttons.append([InlineKeyboardButton("⬅️ Quà tặng",callback_data="adm:rewards")])
        await safe_edit(query,"\n".join(lines),InlineKeyboardMarkup(buttons)); return

    if data.startswith("adm:order:") and data.count(":")==2:
        oid=int(data.rsplit(":",1)[1]); await show_admin_order(query,oid); return

    if data.startswith("adm:order:approve:"):
        oid=int(data.rsplit(":",1)[1]); await approve_order(query,context,oid,aid); return

    if data.startswith("adm:order:reject:"):
        oid=int(data.rsplit(":",1)[1]); await reject_order(query,context,oid,aid); return

    if data == "adm:keys":
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Upload TXT key",callback_data="adm:ask:key_reward")],
            [InlineKeyboardButton("📁 Gán file Telegram cho quà",callback_data="adm:ask:file_reward")],
            [InlineKeyboardButton("📦 Xem kho key",callback_data="adm:key_stock")],
            [InlineKeyboardButton("🧹 Xóa key chưa dùng",callback_data="adm:ask:clear_keys")],
            [InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")],
        ])
        await safe_edit(query,"<b>🔑 KEY / FILE</b>\n\n• TXT: mỗi dòng 1 key.\n• File Telegram: bot lưu file_id và tự gửi sau khi user đổi quà.",kb); return

    if data == "adm:key_stock":
        with db_connect() as conn:
            rows=conn.execute("SELECT r.id,r.name,COUNT(k.id) total,SUM(CASE WHEN k.used=0 THEN 1 ELSE 0 END) available FROM rewards r LEFT JOIN reward_keys k ON k.reward_id=r.id GROUP BY r.id ORDER BY r.id").fetchall()
        lines=["<b>📦 KHO KEY</b>",""]
        for r in rows: lines.append(f"• #{r['id']} {escape(r['name'])}: <b>{r['available'] or 0}</b> còn / {r['total'] or 0} tổng")
        await safe_edit(query,"\n".join(lines),admin_back("keys")); return


    if data == "adm:history":
        with db_connect() as conn:
            tx=conn.execute("SELECT t.*,u.username FROM transactions t LEFT JOIN users u ON u.user_id=t.user_id ORDER BY t.id DESC LIMIT 12").fetchall()
            claims=conn.execute("SELECT s.*,t.name FROM task_sessions s JOIN tasks t ON t.id=s.task_id WHERE s.status='claimed' ORDER BY s.claimed_at DESC LIMIT 8").fetchall()
        lines=["<b>📜 LỊCH SỬ HỆ THỐNG</b>","","<b>💰 Giao dịch gần đây</b>"]
        if not tx: lines.append("Chưa có giao dịch.")
        for r in tx:
            sign="+" if r["amount"]>0 else ""
            lines.append(f"• <code>{r['user_id']}</code> @{escape(r['username'] or '-')} — {escape(r['note'] or r['type'])} — <b>{sign}{fmt_num(r['amount'])} Xu</b>")
        lines.append("\n<b>🎯 Claim gần đây</b>")
        if not claims: lines.append("Chưa có claim.")
        for c in claims:
            lines.append(f"• <code>{c['user_id']}</code> — {escape(c['name'])} — <code>{escape((c['claimed_at'] or '')[:16])}</code>")
        await safe_edit(query,"\n".join(lines),admin_back()); return

    if data == "adm:stats":
        with db_connect() as conn:
            users=conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            today_users=conn.execute("SELECT COUNT(*) c FROM users WHERE last_seen>=?",(today_start_iso(),)).fetchone()["c"]
            claims=conn.execute("SELECT COUNT(*) c FROM task_sessions WHERE status='claimed' AND claimed_at>=?",(today_start_iso(),)).fetchone()["c"]
            earned=conn.execute("SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE type='task_reward'").fetchone()["s"]
            orders=conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
            pending=conn.execute("SELECT COUNT(*) c FROM orders WHERE status='pending'").fetchone()["c"]
            last7=(datetime.now()-timedelta(days=7)).isoformat(timespec="seconds")
            claims7=conn.execute("SELECT COUNT(*) c FROM task_sessions WHERE status='claimed' AND claimed_at>=?",(last7,)).fetchone()["c"]
        text=("<b>📊 THỐNG KÊ</b>\n\n"
              f"👥 Tổng user: <b>{fmt_num(users)}</b>\n"
              f"🟢 User hôm nay: <b>{fmt_num(today_users)}</b>\n"
              f"🎯 Nhiệm vụ hôm nay: <b>{fmt_num(claims)}</b>\n"
              f"📈 Nhiệm vụ 7 ngày: <b>{fmt_num(claims7)}</b>\n"
              f"💰 Tổng xu đã phát: <b>{fmt_xu(earned)}</b>\n"
              f"🎁 Tổng đơn đổi quà: <b>{fmt_num(orders)}</b>\n"
              f"📦 Đơn chờ: <b>{fmt_num(pending)}</b>")
        await safe_edit(query,text,admin_back()); return

    if data == "adm:broadcast":
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Soạn thông báo",callback_data="adm:ask:broadcast")],
            [InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")],
        ])
        await safe_edit(query,"<b>📢 GỬI THÔNG BÁO</b>\n\nBot sẽ gửi tới toàn bộ user chưa bị khóa. Bạn sẽ được xem trước và xác nhận trước khi gửi.",kb); return

    if data == "adm:broadcast:confirm":
        text=context.user_data.pop("broadcast_text","")
        if not text:
            await query.answer("Không còn nội dung để gửi.",show_alert=True); return
        with db_connect() as conn:
            ids=[r["user_id"] for r in conn.execute("SELECT user_id FROM users WHERE banned=0").fetchall()]
        ok=fail=0
        await safe_edit(query,f"⏳ Đang gửi tới {len(ids)} user...",admin_back())
        for i,target in enumerate(ids,1):
            try:
                await context.bot.send_message(target,text)
                ok+=1
            except (Forbidden,BadRequest): fail+=1
            except Exception: fail+=1
            if i%25==0: await asyncio.sleep(0.5)
        log_admin(aid,"broadcast",f"ok={ok},fail={fail}")
        await safe_edit(query,f"✅ <b>GỬI XONG</b>\n\nThành công: <b>{ok}</b>\nLỗi/chặn bot: <b>{fail}</b>",admin_back()); return

    if data == "adm:broadcast:cancel":
        context.user_data.pop("broadcast_text",None); await safe_edit(query,"Đã hủy thông báo.",admin_back()); return

    if data == "adm:settings":
        maint="BẬT" if get_setting("maintenance","0")=="1" else "TẮT"
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Public URL",callback_data="adm:ask:set_public_url"), InlineKeyboardButton("📢 Kênh",callback_data="adm:ask:set_channel")],
            [InlineKeyboardButton("🕺 Hỗ trợ",callback_data="adm:ask:set_support"), InlineKeyboardButton("📖 Hướng dẫn",callback_data="adm:ask:set_guide")],
            [InlineKeyboardButton(f"🛠 Bảo trì: {maint}",callback_data="adm:maintenance")],
            [InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")],
        ])
        text=("<b>⚙️ CÀI ĐẶT</b>\n\n"
              f"🌐 Public URL: <code>{escape(get_public_base_url() or 'Chưa đặt')}</code>\n"
              f"📢 Kênh: {escape(get_setting('channel_username',ENV_CHANNEL_USERNAME))}\n"
              f"🕺 Hỗ trợ: {escape(get_setting('support_url',ENV_SUPPORT_URL))}\n"
              f"🛠 Bảo trì: <b>{maint}</b>")
        await safe_edit(query,text,kb); return

    if data == "adm:maintenance":
        new="0" if get_setting("maintenance","0")=="1" else "1"; set_setting("maintenance",new); log_admin(aid,"maintenance",new)
        await query.answer("Đã đổi trạng thái bảo trì.",show_alert=True)
        # redraw via duplicated settings block trigger by callback value update
        maint="BẬT" if new=="1" else "TẮT"
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Public URL",callback_data="adm:ask:set_public_url"), InlineKeyboardButton("📢 Kênh",callback_data="adm:ask:set_channel")],
            [InlineKeyboardButton("🕺 Hỗ trợ",callback_data="adm:ask:set_support"), InlineKeyboardButton("📖 Hướng dẫn",callback_data="adm:ask:set_guide")],
            [InlineKeyboardButton(f"🛠 Bảo trì: {maint}",callback_data="adm:maintenance")],
            [InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")],
        ])
        await safe_edit(query,f"<b>⚙️ CÀI ĐẶT</b>\n\n🛠 Bảo trì: <b>{maint}</b>\n🌐 Public URL: <code>{escape(get_public_base_url() or 'Chưa đặt')}</code>",kb); return

    if data == "adm:security":
        direct="BẬT" if get_setting("allow_direct_task","0")=="1" else "TẮT"
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Giới hạn IP/ngày",callback_data="adm:ask:set_ip_limit")],
            [InlineKeyboardButton("⏳ Hết hạn session",callback_data="adm:ask:set_expire")],
            [InlineKeyboardButton(f"⚠️ Link trực tiếp: {direct}",callback_data="adm:toggle_direct")],
            [InlineKeyboardButton("📋 Log admin",callback_data="adm:audit")],
            [InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")],
        ])
        text=("<b>🛡 BẢO MẬT / CHỐNG GIAN LẬN</b>\n\n"
              "✅ Session ngẫu nhiên theo user\n✅ Mỗi session chỉ claim 1 lần\n✅ Giới hạn lượt/ngày theo nhiệm vụ\n✅ Kiểm tra thời gian tối thiểu trên server\n✅ Có thể giới hạn nhiều tài khoản cùng IP\n✅ Không cộng xu từ frontend\n\n"
              f"🌐 IP/ngày: <b>{escape(get_setting('max_claims_per_ip_day','10'))}</b>\n"
              f"⏳ Session hết hạn: <b>{escape(get_setting('session_expire_minutes','60'))} phút</b>\n"
              f"⚠️ Cho phép link xác nhận trực tiếp: <b>{direct}</b>")
        await safe_edit(query,text,kb); return

    if data == "adm:toggle_direct":
        new="0" if get_setting("allow_direct_task","0")=="1" else "1"; set_setting("allow_direct_task",new); log_admin(aid,"allow_direct_task",new)
        await query.answer("Đã thay đổi. Nên để TẮT khi chạy thật.",show_alert=True)
        direct="BẬT" if new=="1" else "TẮT"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"⚠️ Link trực tiếp: {direct}",callback_data="adm:toggle_direct")],[InlineKeyboardButton("⬅️ Bảo mật",callback_data="adm:security")]])
        await safe_edit(query,f"<b>🛡 LINK TRỰC TIẾP</b>\n\nTrạng thái: <b>{direct}</b>\n\nKhi TẮT, bot bắt buộc phải tạo link qua API rút gọn.",kb); return

    if data == "adm:audit":
        with db_connect() as conn:
            rows=conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 20").fetchall()
        lines=["<b>📋 ADMIN LOG</b>",""]
        for r in rows: lines.append(f"• {escape(r['created_at'][:16])} — <b>{escape(r['action'])}</b> — {escape((r['detail'] or '')[:80])}")
        if not rows: lines.append("Chưa có log.")
        await safe_edit(query,"\n".join(lines),admin_back("security")); return

    if data == "adm:shorteners":
        with db_connect() as conn:
            rows=conn.execute("SELECT * FROM shorteners ORDER BY id DESC").fetchall()
        lines=["<b>🔗 API RÚT GỌN</b>","","Template hỗ trợ: <code>{url}</code>, <code>{url_encoded}</code>, <code>{user_id}</code>, <code>{token}</code>.",""]
        buttons=[[InlineKeyboardButton("➕ Thêm API",callback_data="adm:ask:shortener_add")]]
        for s in rows:
            st="🟢" if s["active"] else "🔴"; lines.append(f"{st} #{s['id']} <b>{escape(s['name'])}</b> — key: <code>{escape(s['response_key'] or 'auto')}</code>")
            buttons.append([InlineKeyboardButton(f"{st} #{s['id']} {s['name'][:26]}",callback_data=f"adm:shortener:{s['id']}")])
        if not rows: lines.append("Chưa có API.")
        buttons.append([InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")])
        await safe_edit(query,"\n".join(lines),InlineKeyboardMarkup(buttons)); return

    if data.startswith("adm:shortener:") and data.count(":")==2:
        sid=int(data.rsplit(":",1)[1]); await show_shortener(query,sid); return

    if data.startswith("adm:shortener:toggle:"):
        sid=int(data.rsplit(":",1)[1])
        with db_connect() as conn:
            conn.execute("UPDATE shorteners SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(sid,)); conn.commit()
        log_admin(aid,"toggle_shortener",str(sid)); await show_shortener(query,sid); return

    if data.startswith("adm:shortener:delete:"):
        sid=int(data.rsplit(":",1)[1])
        with db_connect() as conn:
            conn.execute("UPDATE shorteners SET active=0 WHERE id=?",(sid,)); conn.commit()
        log_admin(aid,"disable_shortener",str(sid)); await show_shortener(query,sid); return

    if data.startswith("adm:ask:"):
        action=data.split(":",2)[2]
        prompts={
            "user_search":"Gửi <b>Telegram User ID</b> cần tìm.",
            "addcoins":"Gửi theo mẫu: <code>USER_ID | SỐ_XU | GHI_CHÚ</code>\nVí dụ: <code>123456789 | 5000 | Thưởng sự kiện</code>",
            "subcoins":"Gửi theo mẫu: <code>USER_ID | SỐ_XU | GHI_CHÚ</code>",
            "giftall":"Gửi số xu muốn tặng cho <b>tất cả user chưa bị khóa</b>. Ví dụ: <code>1000</code>",
            "task_add":"Gửi 1 dòng:\n<code>Tên | Xu thưởng | Giới hạn/ngày | Chờ giây | Shortener ID</code>\nVí dụ: <code>Vượt Link 1 | 500 | 2 | 20 | 1</code>\nShortener ID có thể để 0 để bot dùng API active đầu tiên.",
            "reward_add":"Gửi 1 dòng:\n<code>Tên | Giá xu | Kho | Kiểu | Nội dung</code>\nKiểu: <code>text</code>, <code>manual</code>, <code>keypool</code>, <code>file_id</code>.\nKho: -1 = không giới hạn.\nVí dụ: <code>Key VIP 1H | 5000 | -1 | keypool | -</code>",
            "key_reward":"Gửi <b>ID quà</b> cần nhập key. Sau đó bot sẽ yêu cầu file .txt.",
            "file_reward":"Gửi <b>ID quà</b> cần gán file. Sau đó gửi file trực tiếp cho bot.",
            "clear_keys":"Gửi <b>ID quà</b> cần xóa toàn bộ key CHƯA DÙNG. Key đã phát vẫn được giữ trong lịch sử.",
            "broadcast":"Gửi nội dung thông báo muốn broadcast. Bot sẽ cho bạn xem trước.",
            "set_public_url":"Gửi Public URL chạy bot HTTP, ví dụ: <code>https://bot.example.com</code>",
            "set_channel":"Gửi username kênh, ví dụ: <code>@mychannel</code>",
            "set_support":"Gửi link hỗ trợ, ví dụ: <code>https://t.me/tizimod</code>",
            "set_guide":"Gửi toàn bộ nội dung hướng dẫn mới.",
            "set_ip_limit":"Gửi số lượt xác nhận tối đa cho 1 IP/ngày. <code>0</code> = tắt giới hạn.",
            "set_expire":"Gửi số phút session tồn tại. Ví dụ: <code>60</code>",
            "shortener_add":"Gửi 1 dòng:\n<code>Tên | API template | response_key</code>\nVí dụ: <code>MyAPI | https://api.example.com/short?key=ABC&url={url_encoded} | short_url</code>\nNếu API trả text link trực tiếp hoặc key phổ biến, response_key có thể để <code>-</code>.",
        }
        context.user_data["admin_action"]=action
        await safe_edit(query,f"<b>⌨️ NHẬP DỮ LIỆU</b>\n\n{prompts.get(action,'Gửi dữ liệu.')}",InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy",callback_data="adm:main")]])); return


async def show_admin_user(query, target: int):
    with db_connect() as conn:
        u=conn.execute("SELECT * FROM users WHERE user_id=?",(target,)).fetchone()
        claims=conn.execute("SELECT COUNT(*) c FROM task_sessions WHERE user_id=? AND status='claimed'",(target,)).fetchone()["c"]
        orders=conn.execute("SELECT COUNT(*) c FROM orders WHERE user_id=?",(target,)).fetchone()["c"]
    if not u:
        await safe_edit(query,"Không tìm thấy user.",admin_back("users")); return
    status="🚫 Đã khóa" if u["banned"] else "🟢 Hoạt động"
    ban_cb=f"adm:user:{'unban' if u['banned'] else 'ban'}:{target}"
    ban_label="✅ Mở khóa" if u["banned"] else "🚫 Khóa user"
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton(ban_label,callback_data=ban_cb),InlineKeyboardButton("♻️ Reset phiên",callback_data=f"adm:user:reset:{target}")],
        [InlineKeyboardButton("⬅️ Người dùng",callback_data="adm:users")],
    ])
    text=(f"<b>👤 USER {target}</b>\n\n"
          f"Username: @{escape(u['username'] or '-')}\nTên: {escape(u['full_name'] or '-')}\n"
          f"💰 Số dư: <b>{fmt_xu(u['coins'])}</b>\n🏅 Đã kiếm: <b>{fmt_xu(u['total_earned'])}</b>\n🎁 Đã đổi: <b>{fmt_xu(u['total_spent'])}</b>\n"
          f"🎯 Nhiệm vụ đã claim: <b>{claims}</b>\n📦 Đơn: <b>{orders}</b>\nTrạng thái: <b>{status}</b>")
    await safe_edit(query,text,kb)


async def show_admin_task(query, tid:int):
    with db_connect() as conn:
        t=conn.execute("SELECT t.*,s.name shortener_name FROM tasks t LEFT JOIN shorteners s ON s.id=t.shortener_id WHERE t.id=?",(tid,)).fetchone()
        total=conn.execute("SELECT COUNT(*) c FROM task_sessions WHERE task_id=? AND status='claimed'",(tid,)).fetchone()["c"]
        today=conn.execute("SELECT COUNT(*) c FROM task_sessions WHERE task_id=? AND status='claimed' AND claimed_at>=?",(tid,today_start_iso())).fetchone()["c"]
    if not t: await safe_edit(query,"Không tìm thấy nhiệm vụ.",admin_back("tasks")); return
    st="🟢 Bật" if t["active"] else "🔴 Tắt"
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Sửa",callback_data=f"adm:task:edit:{tid}"), InlineKeyboardButton("🔄 Bật/Tắt",callback_data=f"adm:task:toggle:{tid}")],
        [InlineKeyboardButton("🗑 Tắt nhiệm vụ",callback_data=f"adm:task:delete:{tid}")],
        [InlineKeyboardButton("⬅️ Nhiệm vụ",callback_data="adm:tasks")],
    ])
    text=(f"<b>🎯 NHIỆM VỤ #{tid}</b>\n\nTên: <b>{escape(t['name'])}</b>\nThưởng: <b>{fmt_xu(t['reward'])}</b>\nGiới hạn: <b>{t['daily_limit']}/ngày</b>\nChờ tối thiểu: <b>{t['min_seconds']} giây</b>\nAPI: <b>{escape(t['shortener_name'] or 'Tự chọn API active')}</b>\nTrạng thái: <b>{st}</b>\n\n📊 Hôm nay: <b>{today}</b> claim\n📈 Tổng: <b>{total}</b> claim")
    await safe_edit(query,text,kb)


async def show_admin_reward(query,rid:int):
    with db_connect() as conn:
        r=conn.execute("SELECT * FROM rewards WHERE id=?",(rid,)).fetchone()
        keys=conn.execute("SELECT COUNT(*) total,SUM(CASE WHEN used=0 THEN 1 ELSE 0 END) available FROM reward_keys WHERE reward_id=?",(rid,)).fetchone()
        orders=conn.execute("SELECT COUNT(*) c FROM orders WHERE reward_id=?",(rid,)).fetchone()["c"]
    if not r: await safe_edit(query,"Không tìm thấy quà.",admin_back("rewards")); return
    st="🟢 Bật" if r["active"] else "🔴 Tắt"; stock="∞" if r["stock"]<0 else str(r["stock"])
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Sửa",callback_data=f"adm:reward:edit:{rid}"), InlineKeyboardButton("🔄 Bật/Tắt",callback_data=f"adm:reward:toggle:{rid}")],
        [InlineKeyboardButton("🗑 Ẩn quà",callback_data=f"adm:reward:delete:{rid}")],
        [InlineKeyboardButton("⬅️ Quà tặng",callback_data="adm:rewards")],
    ])
    text=(f"<b>🎁 QUÀ #{rid}</b>\n\nTên: <b>{escape(r['name'])}</b>\nGiá: <b>{fmt_xu(r['price'])}</b>\nKho cấu hình: <b>{stock}</b>\nKiểu giao: <b>{escape(r['delivery_type'])}</b>\nKey còn: <b>{keys['available'] or 0}/{keys['total'] or 0}</b>\nĐã có đơn: <b>{orders}</b>\nTrạng thái: <b>{st}</b>")
    await safe_edit(query,text,kb)


async def show_admin_order(query,oid:int):
    with db_connect() as conn:
        o=conn.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o: await safe_edit(query,"Không tìm thấy đơn.",admin_back("rewards")); return
    buttons=[]
    if o["status"]=="pending":
        buttons.append([InlineKeyboardButton("✅ Duyệt",callback_data=f"adm:order:approve:{oid}"),InlineKeyboardButton("❌ Từ chối + hoàn xu",callback_data=f"adm:order:reject:{oid}")])
    buttons.append([InlineKeyboardButton("⬅️ Đơn chờ",callback_data="adm:orders")])
    text=(f"<b>📦 ĐƠN #{oid}</b>\n\nUser: <code>{o['user_id']}</code>\nQuà: <b>{escape(o['reward_name'])}</b>\nGiá: <b>{fmt_xu(o['price'])}</b>\nTrạng thái: <b>{escape(o['status'])}</b>\nTạo: <code>{escape(o['created_at'])}</code>")
    await safe_edit(query,text,InlineKeyboardMarkup(buttons))


async def approve_order(query,context,oid:int,aid:int):
    with db_connect() as conn:
        o=conn.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        if not o or o["status"]!="pending": await query.answer("Đơn không ở trạng thái chờ.",show_alert=True); return
        conn.execute("UPDATE orders SET status='completed',updated_at=? WHERE id=?",(now_iso(),oid)); conn.commit()
    try: await context.bot.send_message(o["user_id"],f"✅ Đơn #{oid} ({o['reward_name']}) đã được admin duyệt.")
    except Exception: pass
    log_admin(aid,"approve_order",str(oid)); await query.answer("Đã duyệt.",show_alert=True); await show_admin_order(query,oid)


async def reject_order(query,context,oid:int,aid:int):
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        o=conn.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        if not o or o["status"]!="pending": conn.rollback(); await query.answer("Đơn không ở trạng thái chờ.",show_alert=True); return
        conn.execute("UPDATE orders SET status='rejected',updated_at=? WHERE id=?",(now_iso(),oid))
        conn.execute("UPDATE users SET coins=coins+?, total_spent=MAX(0,total_spent-?) WHERE user_id=?",(o["price"],o["price"],o["user_id"]))
        conn.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES (?,?,?,?,?)",(o["user_id"],"refund",o["price"],f"Hoàn xu đơn #{oid}",now_iso()))
        conn.commit()
    try: await context.bot.send_message(o["user_id"],f"❌ Đơn #{oid} đã bị từ chối. Bot đã hoàn {fmt_xu(o['price'])}.")
    except Exception: pass
    log_admin(aid,"reject_order",str(oid)); await query.answer("Đã từ chối và hoàn xu.",show_alert=True); await show_admin_order(query,oid)


async def show_shortener(query,sid:int):
    with db_connect() as conn:
        s=conn.execute("SELECT * FROM shorteners WHERE id=?",(sid,)).fetchone()
        used=conn.execute("SELECT COUNT(*) c FROM tasks WHERE shortener_id=?",(sid,)).fetchone()["c"]
    if not s: await safe_edit(query,"Không tìm thấy API.",admin_back("shorteners")); return
    st="🟢 Bật" if s["active"] else "🔴 Tắt"
    # Hide likely secrets in displayed URL.
    tpl=s["api_template"]
    shown=tpl[:90]+("…" if len(tpl)>90 else "")
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Bật/Tắt",callback_data=f"adm:shortener:toggle:{sid}"),InlineKeyboardButton("🗑 Tắt API",callback_data=f"adm:shortener:delete:{sid}")],
        [InlineKeyboardButton("⬅️ API rút gọn",callback_data="adm:shorteners")],
    ])
    await safe_edit(query,f"<b>🔗 API #{sid}</b>\n\nTên: <b>{escape(s['name'])}</b>\nTemplate: <code>{escape(shown)}</code>\nResponse key: <code>{escape(s['response_key'] or 'auto')}</code>\nNhiệm vụ đang gán: <b>{used}</b>\nTrạng thái: <b>{st}</b>",kb)


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    action=context.user_data.get("admin_action")
    if not action:
        return
    text=(update.message.text or "").strip()
    aid=update.effective_user.id
    try:
        if action=="user_search":
            target=int(text); context.user_data.pop("admin_action",None)
            # Cannot edit a command message; send a fresh card.
            with db_connect() as conn: u=conn.execute("SELECT * FROM users WHERE user_id=?",(target,)).fetchone()
            if not u: await update.message.reply_text("Không tìm thấy user."); return
            status="🚫 Đã khóa" if u["banned"] else "🟢 Hoạt động"
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("Xem / quản lý",callback_data=f"adm:user:show:{target}")],[InlineKeyboardButton("⬅️ Admin",callback_data="adm:main")]])
            await update.message.reply_text(f"👤 User <code>{target}</code>\n@{escape(u['username'] or '-')}\nSố dư: <b>{fmt_xu(u['coins'])}</b>\n{status}",parse_mode=ParseMode.HTML,reply_markup=kb); return

        if action in ("addcoins","subcoins"):
            parts=[p.strip() for p in text.split("|")]
            if len(parts)<2: raise ValueError("Thiếu USER_ID hoặc số xu")
            target=int(parts[0]); amount=abs(int(parts[1])); note=parts[2] if len(parts)>2 else ("Admin cộng xu" if action=="addcoins" else "Admin trừ xu")
            delta=amount if action=="addcoins" else -amount
            with db_connect() as conn:
                conn.execute("INSERT OR IGNORE INTO users(user_id,username,full_name,created_at,last_seen) VALUES (?,?,?,?,?)",(target,"","",now_iso(),now_iso()))
                if delta<0:
                    bal=conn.execute("SELECT coins FROM users WHERE user_id=?",(target,)).fetchone()["coins"]
                    if bal<amount: raise ValueError("User không đủ xu để trừ")
                conn.execute("UPDATE users SET coins=coins+? WHERE user_id=?",(delta,target))
                conn.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES (?,?,?,?,?)",(target,"admin_adjust",delta,note,now_iso())); conn.commit()
            log_admin(aid,action,f"{target}:{delta}:{note}"); context.user_data.pop("admin_action",None)
            await update.message.reply_text(f"✅ Đã {'cộng' if delta>0 else 'trừ'} {fmt_xu(amount)} {'cho' if delta>0 else 'của'} {target}.",reply_markup=admin_main_keyboard()); return

        if action=="giftall":
            amount=abs(int(text));
            if amount<=0: raise ValueError("Số xu phải > 0")
            with db_connect() as conn:
                users=conn.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
                conn.execute("UPDATE users SET coins=coins+? WHERE banned=0",(amount,))
                conn.executemany("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES (?,?,?,?,?)",[(u["user_id"],"gift_all",amount,"Quà toàn hệ thống",now_iso()) for u in users]); conn.commit()
            log_admin(aid,"gift_all",f"{amount} x {len(users)}"); context.user_data.pop("admin_action",None)
            await update.message.reply_text(f"✅ Đã tặng {fmt_xu(amount)} cho {len(users)} user.",reply_markup=admin_main_keyboard()); return

        if action in {"task_add","task_edit"}:
            p=[x.strip() for x in text.split("|")]
            if len(p)<5: raise ValueError("Cần đủ 5 trường")
            name=p[0]; reward=int(p[1]); daily=int(p[2]); wait=int(p[3]); sid=int(p[4]) or None
            if reward<=0 or daily<=0 or wait<0: raise ValueError("Thông số không hợp lệ")
            with db_connect() as conn:
                if sid and not conn.execute("SELECT 1 FROM shorteners WHERE id=?",(sid,)).fetchone(): raise ValueError("Shortener ID không tồn tại")
                if action=="task_add":
                    cur=conn.execute("INSERT INTO tasks(name,reward,daily_limit,min_seconds,shortener_id,created_at) VALUES (?,?,?,?,?,?)",(name,reward,daily,wait,sid,now_iso())); tid=cur.lastrowid
                else:
                    tid=int(context.user_data.get("edit_id",0))
                    if not conn.execute("SELECT 1 FROM tasks WHERE id=?",(tid,)).fetchone(): raise ValueError("Nhiệm vụ không tồn tại")
                    conn.execute("UPDATE tasks SET name=?,reward=?,daily_limit=?,min_seconds=?,shortener_id=? WHERE id=?",(name,reward,daily,wait,sid,tid))
                conn.commit()
            log_admin(aid,"add_task" if action=="task_add" else "edit_task",str(tid)); context.user_data.pop("admin_action",None); context.user_data.pop("edit_id",None)
            await update.message.reply_text(f"✅ Đã {'thêm' if action=='task_add' else 'cập nhật'} nhiệm vụ #{tid}: {name}",reply_markup=admin_main_keyboard()); return

        if action in {"reward_add","reward_edit"}:
            p=[x.strip() for x in text.split("|",4)]
            if len(p)<5: raise ValueError("Cần đủ 5 trường")
            name=p[0]; price=int(p[1]); stock=int(p[2]); typ=p[3].lower(); raw_value=p[4]
            if typ not in {"text","manual","keypool","file_id"}: raise ValueError("Kiểu phải là text/manual/keypool/file_id")
            if price<0 or stock<-1: raise ValueError("Giá/kho không hợp lệ")
            with db_connect() as conn:
                if action=="reward_add":
                    value="" if raw_value=="-" else raw_value
                    cur=conn.execute("INSERT INTO rewards(name,price,stock,delivery_type,delivery_value,created_at) VALUES (?,?,?,?,?,?)",(name,price,stock,typ,value,now_iso())); rid=cur.lastrowid
                else:
                    rid=int(context.user_data.get("edit_id",0))
                    oldrow=conn.execute("SELECT * FROM rewards WHERE id=?",(rid,)).fetchone()
                    if not oldrow: raise ValueError("Quà không tồn tại")
                    value=oldrow["delivery_value"] if raw_value=="-" else raw_value
                    conn.execute("UPDATE rewards SET name=?,price=?,stock=?,delivery_type=?,delivery_value=? WHERE id=?",(name,price,stock,typ,value,rid))
                conn.commit()
            log_admin(aid,"add_reward" if action=="reward_add" else "edit_reward",str(rid)); context.user_data.pop("admin_action",None); context.user_data.pop("edit_id",None)
            await update.message.reply_text(f"✅ Đã {'thêm' if action=='reward_add' else 'cập nhật'} quà #{rid}: {name}",reply_markup=admin_main_keyboard()); return

        if action=="key_reward":
            rid=int(text)
            with db_connect() as conn:
                r=conn.execute("SELECT * FROM rewards WHERE id=?",(rid,)).fetchone()
                if not r: raise ValueError("Không tìm thấy quà")
                conn.execute("UPDATE rewards SET delivery_type='keypool' WHERE id=?",(rid,)); conn.commit()
            context.user_data["admin_action"]="key_file"; context.user_data["reward_id"]=rid
            await update.message.reply_text(f"📄 Bây giờ gửi file <b>.txt</b> chứa key cho quà #{rid}. Mỗi dòng 1 key.",parse_mode=ParseMode.HTML); return

        if action=="file_reward":
            rid=int(text)
            with db_connect() as conn:
                if not conn.execute("SELECT 1 FROM rewards WHERE id=?",(rid,)).fetchone(): raise ValueError("Không tìm thấy quà")
            context.user_data["admin_action"]="telegram_file"; context.user_data["reward_id"]=rid
            await update.message.reply_text(f"📁 Bây giờ gửi file cần giao cho user đối với quà #{rid}."); return

        if action=="clear_keys":
            rid=int(text)
            with db_connect() as conn:
                r=conn.execute("SELECT name FROM rewards WHERE id=?",(rid,)).fetchone()
                if not r: raise ValueError("Không tìm thấy quà")
                cur=conn.execute("DELETE FROM reward_keys WHERE reward_id=? AND used=0",(rid,)); deleted=cur.rowcount; conn.commit()
            log_admin(aid,"clear_unused_keys",f"reward={rid},deleted={deleted}"); context.user_data.pop("admin_action",None)
            await update.message.reply_text(f"✅ Đã xóa {deleted} key chưa dùng của quà #{rid}.",reply_markup=admin_main_keyboard()); return

        if action=="broadcast":
            context.user_data.pop("admin_action",None); context.user_data["broadcast_text"]=text
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Gửi tất cả",callback_data="adm:broadcast:confirm"),InlineKeyboardButton("❌ Hủy",callback_data="adm:broadcast:cancel")]])
            await update.message.reply_text(f"<b>📢 XEM TRƯỚC</b>\n\n{escape(text)}",parse_mode=ParseMode.HTML,reply_markup=kb); return

        if action in {"set_public_url","set_channel","set_support","set_guide","set_ip_limit","set_expire"}:
            mapping={"set_public_url":"public_base_url","set_channel":"channel_username","set_support":"support_url","set_guide":"guide_text","set_ip_limit":"max_claims_per_ip_day","set_expire":"session_expire_minutes"}
            key=mapping[action]; val=text
            if action=="set_public_url":
                if not val.startswith("https://"): raise ValueError("Public URL nên bắt đầu bằng https://")
                val=val.rstrip("/")
            if action in {"set_ip_limit","set_expire"}:
                n=int(val)
                if n<0 or (action=="set_expire" and n<1): raise ValueError("Giá trị không hợp lệ")
                val=str(n)
            set_setting(key,val); log_admin(aid,action,val[:100]); context.user_data.pop("admin_action",None)
            await update.message.reply_text("✅ Đã cập nhật cài đặt.",reply_markup=admin_main_keyboard()); return

        if action=="shortener_add":
            p=[x.strip() for x in text.split("|",2)]
            if len(p)<3: raise ValueError("Cần đủ 3 trường")
            name,template,response_key=p; response_key="" if response_key=="-" else response_key
            if "{url}" not in template and "{url_encoded}" not in template: raise ValueError("API template phải có {url} hoặc {url_encoded}")
            with db_connect() as conn:
                cur=conn.execute("INSERT INTO shorteners(name,api_template,response_key,created_at) VALUES (?,?,?,?)",(name,template,response_key,now_iso())); conn.commit(); sid=cur.lastrowid
            log_admin(aid,"add_shortener",str(sid)); context.user_data.pop("admin_action",None)
            await update.message.reply_text(f"✅ Đã thêm API rút gọn #{sid}: {name}",reply_markup=admin_main_keyboard()); return

    except Exception as e:
        await update.message.reply_text(f"❌ Dữ liệu không hợp lệ: {escape(str(e))}\n\nGửi lại đúng định dạng hoặc bấm /admin để hủy.",parse_mode=ParseMode.HTML)


async def admin_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    action=context.user_data.get("admin_action")
    if action not in {"key_file","telegram_file"}: return
    rid=context.user_data.get("reward_id")
    if not rid: return
    doc=update.message.document
    if action=="telegram_file":
        with db_connect() as conn:
            conn.execute("UPDATE rewards SET delivery_type='file_id',delivery_value=? WHERE id=?",(doc.file_id,rid)); conn.commit()
        log_admin(update.effective_user.id,"assign_file",str(rid)); context.user_data.pop("admin_action",None); context.user_data.pop("reward_id",None)
        await update.message.reply_text(f"✅ Đã gán file Telegram cho quà #{rid}. Bot sẽ tự gửi file này khi user đổi quà.",reply_markup=admin_main_keyboard()); return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("❌ Hãy gửi file .txt, mỗi dòng 1 key."); return
    if doc.file_size and doc.file_size>2_000_000:
        await update.message.reply_text("❌ File key quá lớn. Giới hạn 2 MB."); return
    tgfile=await context.bot.get_file(doc.file_id)
    data=await tgfile.download_as_bytearray()
    text=bytes(data).decode("utf-8-sig",errors="ignore")
    keys=[]; seen=set()
    for line in text.splitlines():
        key=line.strip()
        if key and key not in seen:
            seen.add(key); keys.append(key)
    if not keys:
        await update.message.reply_text("❌ File không có key hợp lệ."); return
    added=0
    with db_connect() as conn:
        for key in keys:
            cur=conn.execute("INSERT OR IGNORE INTO reward_keys(reward_id,value) VALUES (?,?)",(rid,key)); added+=cur.rowcount
        conn.execute("UPDATE rewards SET delivery_type='keypool' WHERE id=?",(rid,)); conn.commit()
    log_admin(update.effective_user.id,"upload_keys",f"reward={rid},added={added}"); context.user_data.pop("admin_action",None); context.user_data.pop("reward_id",None)
    await update.message.reply_text(f"✅ Đã nhập {added}/{len(keys)} key mới cho quà #{rid}.",reply_markup=admin_main_keyboard())


async def addxu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    if not is_admin(update): return
    if len(context.args) < 2:
        await update.message.reply_text("Dùng: /addxu USER_ID SO_XU [ghi chú]")
        return
    try:
        target=int(context.args[0]); amount=abs(int(context.args[1])); note=" ".join(context.args[2:]) or "Admin cộng xu"
        with db_connect() as conn:
            conn.execute("INSERT OR IGNORE INTO users(user_id,username,full_name,created_at,last_seen) VALUES (?,?,?,?,?)",(target,"","",now_iso(),now_iso()))
            conn.execute("UPDATE users SET coins=coins+? WHERE user_id=?",(amount,target))
            conn.execute("INSERT INTO transactions(user_id,type,amount,note,created_at) VALUES (?,?,?,?,?)",(target,"admin_adjust",amount,note,now_iso())); conn.commit()
        log_admin(update.effective_user.id,"addxu_command",f"{target}:{amount}")
        await update.message.reply_text(f"✅ Đã cộng {fmt_xu(amount)} cho {target}.")
    except ValueError:
        await update.message.reply_text("USER_ID và SO_XU phải là số.")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("admin_action",None); context.user_data.pop("reward_id",None); context.user_data.pop("broadcast_text",None)
    await update.message.reply_text("Đã hủy thao tác.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Unhandled error:", repr(context.error))


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu BOT_TOKEN trong file .env")
    init_db()
    app=(Application.builder().token(BOT_TOKEN).post_init(start_http_server).post_shutdown(stop_http_server).build())
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin_command))
    app.add_handler(CommandHandler("id",id_command))
    app.add_handler(CommandHandler("addxu",addxu_command))
    app.add_handler(CommandHandler("addmoney",addxu_command))
    app.add_handler(CommandHandler("cancel",cancel_command))
    app.add_handler(CallbackQueryHandler(admin_callbacks,pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(user_callbacks))
    app.add_handler(MessageHandler(filters.Document.ALL,admin_document_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,admin_text_input))
    app.add_error_handler(error_handler)
    print("Bot đang chạy...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
