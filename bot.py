#!/usr/bin/env python3
"""
🔥 79 ULTIMATE SCRIPT HOSTING BOT
- Persistent bottom menu (no more /start again)
- Back buttons everywhere
- Crash-proof runner + AI debugger
"""

import asyncio
import atexit
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import List, Optional, Tuple

import psutil
from dotenv import load_dotenv
from flask import Flask, jsonify
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("79Bot")

load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "8731709572:AAGwoikRElJZFEUY9jXAttKy5QPFLEtllTE").strip()
ADMIN_IDS_STR = (os.getenv("ADMIN_IDS") or "7546911540").strip()
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()] or [7546911540]

CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "@seventyx79").strip()
PORT = int(os.getenv("PORT") or os.getenv("RAILWAY_PUBLIC_PORT") or "8080")
WORKSPACE_BASE = Path(os.getenv("WORKSPACE_BASE", "./workspaces"))
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", "10MB").replace("MB", "")) * 1024 * 1024
MAX_ARCHIVE_SIZE = int(os.getenv("MAX_ARCHIVE_SIZE", "50MB").replace("MB", "")) * 1024 * 1024
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_TIMEOUT", "300"))

WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)

UPLOAD_WAIT, TERMINAL_SESSION = range(2)


# ---------- Path registry (64-byte callback safe) ----------
class PathRegistry:
    def __init__(self):
        self._registry = {}
        self._counter = 0

    def register(self, path: Path) -> str:
        path_str = str(path.resolve())
        for k, v in self._registry.items():
            if v == path_str:
                return k
        key = f"p79_{self._counter}"
        self._registry[key] = path_str
        self._counter += 1
        return key

    def get(self, key: str) -> Optional[Path]:
        s = self._registry.get(key)
        return Path(s) if s else None


path_registry = PathRegistry()

DATA_FILE = "bot_data.json"


def load_data():
    if Path(DATA_FILE).exists():
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"users": {}, "processes": [], "telemetry": {}}


def save_data(data):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"save_data: {e}")


class UserManager:
    def __init__(self):
        self.data = load_data()
        self.users = self.data.get("users", {})
        self.processes = self.data.get("processes", [])
        self.telemetry = self.data.get("telemetry", {})

    def save(self):
        self.data["users"] = self.users
        self.data["processes"] = self.processes
        self.data["telemetry"] = self.telemetry
        save_data(self.data)

    def get_user(self, user_id: int):
        return self.users.get(str(user_id))

    def add_user(self, user_id: int, name: str, username: str):
        status = "approved" if user_id in ADMIN_IDS else "pending"
        self.users[str(user_id)] = {
            "status": status,
            "name": name,
            "username": username,
            "request_time": datetime.now().isoformat(),
            "workspace": str(WORKSPACE_BASE / str(user_id)),
        }
        self.telemetry.setdefault(str(user_id), {"runs": 0, "success": 0, "fail": 0, "bad": 0})
        self.save()

    def approve_user(self, user_id: int) -> bool:
        if u := self.users.get(str(user_id)):
            u["status"] = "approved"
            self.save()
            return True
        return False

    def ban_user(self, user_id: int) -> bool:
        if u := self.users.get(str(user_id)):
            u["status"] = "banned"
            self.save()
            return True
        return False

    def unban_user(self, user_id: int) -> bool:
        if u := self.users.get(str(user_id)):
            u["status"] = "approved"
            self.save()
            return True
        return False

    def is_approved(self, user_id: int) -> bool:
        if user_id in ADMIN_IDS:
            return True
        u = self.get_user(user_id)
        return bool(u and u.get("status") == "approved")

    def is_pending(self, user_id: int) -> bool:
        if user_id in ADMIN_IDS:
            return False
        u = self.get_user(user_id)
        return bool(u and u.get("status") == "pending")

    def is_banned(self, user_id: int) -> bool:
        u = self.get_user(user_id)
        return bool(u and u.get("status") == "banned")

    def get_workspace(self, user_id: int) -> Path:
        u = self.get_user(user_id)
        return Path(u["workspace"]) if u else WORKSPACE_BASE / str(user_id)

    def get_pending_requests(self) -> List[dict]:
        return [{"user_id": k, **v} for k, v in self.users.items() if v.get("status") == "pending"]

    def get_approved_users(self) -> List[dict]:
        return [{"user_id": k, **v} for k, v in self.users.items() if v.get("status") == "approved"]

    def get_banned_users(self) -> List[dict]:
        return [{"user_id": k, **v} for k, v in self.users.items() if v.get("status") == "banned"]

    def add_process(self, user_id: int, filename: str, pid: int, log_path: str):
        self.processes.append(
            {
                "user_id": user_id,
                "filename": filename,
                "pid": pid,
                "start_time": datetime.now().isoformat(),
                "status": "running",
                "log_path": log_path,
            }
        )
        self.save()

    def get_user_processes(self, user_id: int) -> List[dict]:
        return [p for p in self.processes if p.get("user_id") == user_id]

    def get_all_processes(self) -> List[dict]:
        return self.processes

    def stop_process(self, pid: int) -> bool:
        for p in self.processes:
            if p.get("pid") == pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
                p["status"] = "stopped"
                self.save()
                return True
        return False

    def cleanup_all(self):
        for p in list(self.processes):
            try:
                os.kill(p.get("pid"), signal.SIGTERM)
            except Exception:
                pass
        self.processes.clear()
        self.save()

    def _tele(self, user_id: int):
        uid = str(user_id)
        self.telemetry.setdefault(uid, {"runs": 0, "success": 0, "fail": 0, "bad": 0})
        return self.telemetry[uid]

    def inc_run(self, user_id: int):
        self._tele(user_id)["runs"] += 1
        self.save()

    def inc_success(self, user_id: int):
        self._tele(user_id)["success"] += 1
        self.save()

    def inc_fail(self, user_id: int):
        self._tele(user_id)["fail"] += 1
        self.save()

    def inc_bad(self, user_id: int):
        self._tele(user_id)["bad"] += 1
        self.save()

    def get_user_telemetry(self, user_id: int) -> dict:
        return self.telemetry.get(str(user_id), {"runs": 0, "success": 0, "fail": 0, "bad": 0})


user_manager = UserManager()
atexit.register(user_manager.cleanup_all)


# ---------- UI helpers ----------
def main_reply_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Always-visible bottom menu — no need to /start again."""
    rows = [
        [KeyboardButton("📁 Upload"), KeyboardButton("📂 My Scripts")],
        [KeyboardButton("💻 Terminal"), KeyboardButton("📝 Logs")],
        [KeyboardButton("🛑 Stop"), KeyboardButton("📊 Stats")],
        [KeyboardButton("🏠 Menu")],
    ]
    if user_id in ADMIN_IDS:
        rows.append([KeyboardButton("👑 Admin")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def ik_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]])


def ik_back(target: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=target)]])


def sanitize_filename(filename: str) -> str:
    return os.path.basename(filename).replace("/", "").replace("\\", "")


def ensure_workspace(user_id: int) -> Path:
    ws = user_manager.get_workspace(user_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def is_safe_path(user_id: int, path: Path) -> bool:
    ws = ensure_workspace(user_id)
    try:
        path.resolve().relative_to(ws.resolve())
        return True
    except ValueError:
        return False


def extract_zip(zip_path: Path, dest_dir: Path) -> Tuple[bool, str]:
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            total = sum(i.file_size for i in z.infolist())
            if total > MAX_ARCHIVE_SIZE:
                return False, f"Archive too large (>{MAX_ARCHIVE_SIZE // 1024 // 1024}MB)"
            for m in z.infolist():
                if m.filename.startswith("/") or ".." in m.filename:
                    return False, "Invalid path in archive"
                target = dest_dir / m.filename
                try:
                    target.resolve().relative_to(dest_dir.resolve())
                except ValueError:
                    return False, "Path traversal blocked"
            z.extractall(dest_dir)
        return True, "OK"
    except Exception as e:
        return False, str(e)


def detect_entry_point(dest_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    for name in ("main.py", "bot.py", "index.js"):
        p = dest_dir / name
        if p.exists():
            return ("py" if name.endswith(".py") else "js", str(p))
    pys = list(dest_dir.glob("*.py"))
    if pys:
        return ("py", str(pys[0]))
    jss = list(dest_dir.glob("*.js"))
    if jss:
        return ("js", str(jss[0]))
    return None, None


async def install_dependencies(dest_dir: Path) -> Tuple[bool, str]:
    req = dest_dir / "requirements.txt"
    pkg = dest_dir / "package.json"
    if req.exists():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-r", str(req),
            cwd=str(dest_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="ignore")
        if proc.returncode != 0:
            return False, text[:400]
        return True, text[:400]
    if pkg.exists():
        try:
            proc = await asyncio.create_subprocess_exec(
                "npm", "install", "--production",
                cwd=str(dest_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return False, out.decode(errors="ignore")[:400]
            return True, "npm ok"
        except Exception as e:
            return False, str(e)
    return True, "no deps file"


async def auto_install_module(module_name: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", module_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception:
        return False


def extract_module_name_from_error(error_text: str) -> Optional[str]:
    for pat in (
        r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]",
        r"ImportError: No module named ['\"]([^'\"]+)['\"]",
    ):
        m = re.search(pat, error_text)
        if m:
            return m.group(1)
    return None


async def get_ai_debug_suggestion(error_log: str) -> str:
    if not OPENAI_API_KEY:
        return (
            "🔧 AI Debugger off — set OPENAI_API_KEY in Railway → Variables, then redeploy.\n"
            "Meanwhile open *View Log* to see the real error."
        )
    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY)
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "You debug Python/Node errors. Reply in max 3 short lines with the fix.",
                },
                {"role": "user", "content": error_log[:1800]},
            ],
            max_tokens=180,
        )
        return f"🤖 *79 AI Fix:*\n{response.choices[0].message.content.strip()}"
    except Exception as e:
        return f"⚠️ AI error: `{e}`\nOpen *View Log* for full traceback."


async def run_script_with_watchdog(
    user_id: int, script_path: Path, file_type: str, context: ContextTypes.DEFAULT_TYPE
) -> Tuple[int, str, str]:
    user_manager.inc_run(user_id)
    ws = ensure_workspace(user_id)
    log_dir = ws / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{script_path.stem}.log"

    cmd = [sys.executable, "-u", str(script_path)] if file_type == "py" else ["node", str(script_path)]

    def _start(log_mode="w"):
        lf = open(log_path, log_mode, buffering=1, encoding="utf-8", errors="ignore")
        p = subprocess.Popen(
            cmd,
            cwd=str(script_path.parent),
            stdout=lf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        return p, lf

    proc, log_file = _start("w")
    await asyncio.sleep(3)
    poll = proc.poll()

    if poll is None:
        user_manager.inc_success(user_id)
        return proc.pid, str(log_path), f"🚀 Running in background (PID `{proc.pid}`)"

    try:
        log_file.close()
    except Exception:
        pass

    output = Path(log_path).read_text(errors="ignore") if log_path.exists() else ""

    if poll == 0:
        user_manager.inc_success(user_id)
        return proc.pid, str(log_path), "✅ Finished successfully."

    missing = extract_module_name_from_error(output)
    if missing:
        await context.bot.send_message(user_id, f"⚙️ Installing missing `{missing}`...")
        if await auto_install_module(missing):
            await context.bot.send_message(user_id, f"✅ Installed `{missing}`. Restarting...")
            proc2, lf2 = _start("a")
            await asyncio.sleep(3)
            poll2 = proc2.poll()
            if poll2 is None:
                user_manager.inc_success(user_id)
                return proc2.pid, str(log_path), f"🚀 Running after fix (PID `{proc2.pid}`)"
            if poll2 == 0:
                user_manager.inc_success(user_id)
                return proc2.pid, str(log_path), "✅ Success after auto-install."
            try:
                lf2.close()
            except Exception:
                pass
            output2 = Path(log_path).read_text(errors="ignore")
            user_manager.inc_fail(user_id)
            ai = await get_ai_debug_suggestion(output2)
            return proc2.pid, str(log_path), f"❌ Still failing after install.\n{ai}"
        user_manager.inc_bad(user_id)
        ai = await get_ai_debug_suggestion(output)
        return proc.pid, str(log_path), f"❌ Could not install `{missing}`.\n{ai}"

    user_manager.inc_fail(user_id)
    ai = await get_ai_debug_suggestion(output)
    return proc.pid, str(log_path), f"❌ Exit code {poll}.\n{ai}"


async def is_member_of_channel(bot, user_id: int) -> bool:
    if not CHANNEL_USERNAME or user_id in ADMIN_IDS:
        return True
    try:
        chat = CHANNEL_USERNAME if CHANNEL_USERNAME.startswith("@") else f"@{CHANNEL_USERNAME}"
        m = await bot.get_chat_member(chat, user_id)
        return m.status not in ("left", "kicked")
    except Exception as e:
        logger.warning(f"join check bypass: {e}")
        return True


async def gate_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if user can use bot. Sends messages if not."""
    user = update.effective_user
    if not user:
        return False
    uid = user.id
    msg = update.effective_message

    if not await is_member_of_channel(context.bot, uid):
        ch = CHANNEL_USERNAME.lstrip("@")
        if msg:
            await msg.reply_text(
                f"🔒 Join {CHANNEL_USERNAME} first.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("📢 Join", url=f"https://t.me/{ch}")],
                        [InlineKeyboardButton("✅ I joined", callback_data="check_join")],
                    ]
                ),
            )
        return False

    u = user_manager.get_user(uid)
    if not u:
        user_manager.add_user(uid, user.full_name, user.username or "-")
        if uid not in ADMIN_IDS:
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"🔔 New user\n👤 {user.full_name}\n🆔 `{uid}`",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
                                    InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{uid}"),
                                ]
                            ]
                        ),
                    )
                except Exception:
                    pass
            if msg:
                await msg.reply_text("⏳ Request sent to admin. Wait for approval.")
            return False

    if user_manager.is_banned(uid):
        if msg:
            await msg.reply_text("🚫 You are banned.")
        return False
    if user_manager.is_pending(uid):
        if msg:
            await msg.reply_text("⏳ Pending admin approval.")
        return False
    return True


# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_user(update, context):
        return
    user = update.effective_user
    await update.message.reply_text(
        f"👋 *Welcome {user.first_name} — 79 Hosting*\n"
        f"Channel: {CHANNEL_USERNAME}\n\n"
        "Use the *buttons below* anytime — no need to /start again.\n"
        "Or tap 🏠 Menu.",
        parse_mode="Markdown",
        reply_markup=main_reply_keyboard(user.id),
    )


async def show_menu_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "📋 *79 Main Menu*"):
    uid = update.effective_user.id
    target = update.callback_query.message if update.callback_query else update.effective_message
    kb = main_reply_keyboard(uid)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text + "\n\n⬇️ Use bottom buttons or pick:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("📁 Upload", callback_data="upload"),
                            InlineKeyboardButton("📂 Scripts", callback_data="my_scripts"),
                        ],
                        [
                            InlineKeyboardButton("💻 Terminal", callback_data="terminal"),
                            InlineKeyboardButton("📝 Logs", callback_data="logs"),
                        ],
                        [
                            InlineKeyboardButton("🛑 Stop", callback_data="stop"),
                            InlineKeyboardButton("📊 Stats", callback_data="my_stats"),
                        ]
                        + (
                            [[InlineKeyboardButton("👑 Admin", callback_data="admin_panel")]]
                            if uid in ADMIN_IDS
                            else []
                        ),
                    ]
                ),
            )
        except Exception:
            await context.bot.send_message(
                uid, text, parse_mode="Markdown", reply_markup=kb
            )
        # refresh reply keyboard
        await context.bot.send_message(uid, "⬇️ Menu ready", reply_markup=kb)
    else:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_manager.is_approved(update.effective_user.id):
        await update.callback_query.answer("Not approved", show_alert=True)
        return
    await show_menu_msg(update, context)


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if await gate_user(update, context):
        await q.edit_message_text("✅ Verified. Send /start or use menu below.")
        await context.bot.send_message(
            q.from_user.id,
            "🏠 Menu unlocked",
            reply_markup=main_reply_keyboard(q.from_user.id),
        )


# ---------- Reply keyboard text router ----------
async def menu_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_user(update, context):
        return
    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    mapping = {
        "📁 Upload": "upload_prompt",
        "📂 My Scripts": "scripts",
        "💻 Terminal": "term_prompt",
        "📝 Logs": "logs",
        "🛑 Stop": "stop",
        "📊 Stats": "stats",
        "🏠 Menu": "menu",
        "👑 Admin": "admin",
    }
    action = mapping.get(text)
    if not action:
        await update.message.reply_text(
            "Use the bottom buttons ⬇️",
            reply_markup=main_reply_keyboard(uid),
        )
        return

    if action == "menu":
        await update.message.reply_text(
            "📋 *79 Main Menu*\nPick a button below.",
            parse_mode="Markdown",
            reply_markup=main_reply_keyboard(uid),
        )
        return
    if action == "upload_prompt":
        await update.message.reply_text(
            "📤 Send a `.py` / `.js` / `.zip` file now.\n/cancel to abort.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("/cancel")]], resize_keyboard=True),
        )
        context.user_data["waiting_upload"] = True
        return
    if action == "term_prompt":
        ws = ensure_workspace(uid)
        context.user_data["terminal_cwd"] = str(ws)
        context.user_data["in_terminal"] = True
        await update.message.reply_text(
            f"💻 *Terminal*\n`{ws}`\n\n"
            "Allowed: pwd ls cd cat head tail mkdir cp mv rm\n"
            "Type /cancel to exit.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("/cancel")]], resize_keyboard=True),
        )
        return
    if action == "scripts":
        await send_scripts_list(update, context)
        return
    if action == "logs":
        await send_logs_list(update, context)
        return
    if action == "stop":
        await send_stop_list(update, context)
        return
    if action == "stats":
        tele = user_manager.get_user_telemetry(uid)
        await update.message.reply_text(
            f"📊 *Your Stats*\n🚀 {tele['runs']} | ✅ {tele['success']} | ❌ {tele['fail']} | 💀 {tele['bad']}",
            parse_mode="Markdown",
            reply_markup=main_reply_keyboard(uid),
        )
        return
    if action == "admin":
        if uid not in ADMIN_IDS:
            await update.message.reply_text("Not admin.", reply_markup=main_reply_keyboard(uid))
            return
        await update.message.reply_text(
            "👑 *Admin Panel*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("👥 Users", callback_data="admin_users"),
                        InlineKeyboardButton("⏳ Pending", callback_data="admin_pending"),
                    ],
                    [
                        InlineKeyboardButton("🖥️ Running", callback_data="admin_running"),
                        InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
                    ],
                    [
                        InlineKeyboardButton("🚫 Banned", callback_data="admin_banned"),
                        InlineKeyboardButton("📈 Telemetry", callback_data="admin_telemetry"),
                    ],
                    [
                        InlineKeyboardButton("🧹 Cleanup", callback_data="admin_cleanup"),
                        InlineKeyboardButton("🔙 Back", callback_data="main_menu"),
                    ],
                ]
            ),
        )


async def send_scripts_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ws = ensure_workspace(uid)
    files = (
        list(ws.glob("*.py"))
        + list(ws.glob("*.js"))
        + list(ws.glob("extracted/**/*.py"))
        + list(ws.glob("extracted/**/*.js"))
    )
    # unique
    seen = set()
    uniq = []
    for f in files:
        r = str(f.resolve())
        if r not in seen:
            seen.add(r)
            uniq.append(f)

    msg = update.effective_message
    if not uniq:
        await msg.reply_text(
            "📂 No scripts. Use 📁 Upload first.",
            reply_markup=main_reply_keyboard(uid),
        )
        return

    rows = []
    for f in uniq[:30]:
        k = path_registry.register(f)
        rows.append(
            [
                InlineKeyboardButton(f"📄 {f.name}", callback_data=f"view_script_{k}"),
                InlineKeyboardButton("▶️ Run", callback_data=f"run_script_{k}"),
            ]
        )
    rows.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
    await msg.reply_text(
        "📂 *Your Scripts* — Run or preview:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def send_logs_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    procs = user_manager.get_user_processes(uid)
    rows = []
    for p in procs[-20:]:
        lp = Path(p.get("log_path") or "")
        if lp.exists():
            k = path_registry.register(lp)
            rows.append([InlineKeyboardButton(f"📄 {lp.name}", callback_data=f"view_log_{k}")])
    if not rows:
        await update.effective_message.reply_text(
            "📝 No logs yet.", reply_markup=main_reply_keyboard(uid)
        )
        return
    rows.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
    await update.effective_message.reply_text(
        "📝 *Logs*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def send_stop_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    running = [p for p in user_manager.get_user_processes(uid) if p.get("status") == "running"]
    # also check live pids
    live = []
    for p in running:
        if psutil.pid_exists(p.get("pid")):
            live.append(p)
        else:
            p["status"] = "stopped"
    user_manager.save()
    if not live:
        await update.effective_message.reply_text(
            "🛑 No running scripts.", reply_markup=main_reply_keyboard(uid)
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                f"🛑 {p['filename']} ({p['pid']})", callback_data=f"stop_proc_{p['pid']}"
            )
        ]
        for p in live
    ]
    rows.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
    await update.effective_message.reply_text(
        "Select process to stop:", reply_markup=InlineKeyboardMarkup(rows)
    )


# ---------- Upload / Terminal free-text while waiting ----------
async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if text == "/cancel" or text.lower() == "cancel":
        context.user_data["waiting_upload"] = False
        context.user_data["in_terminal"] = False
        await update.message.reply_text(
            "Cancelled. Back to menu.",
            reply_markup=main_reply_keyboard(uid),
        )
        return

    # terminal mode
    if context.user_data.get("in_terminal"):
        await terminal_handle(update, context)
        return

    # if waiting upload but got text
    if context.user_data.get("waiting_upload"):
        await update.message.reply_text(
            "Send a *file* (document), not text.\n/cancel to exit.",
            parse_mode="Markdown",
        )
        return

    # ignore if looks like menu (handled elsewhere)
    await menu_text_router(update, context)


async def upload_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_user(update, context):
        return
    uid = update.effective_user.id
    document = update.message.document
    if not document:
        return

    # accept upload anytime (not only waiting flag)
    filename = document.file_name or "file.py"
    if not any(filename.endswith(ext) for ext in (".py", ".js", ".zip")):
        await update.message.reply_text(
            "❌ Only .py .js .zip", reply_markup=main_reply_keyboard(uid)
        )
        return
    if document.file_size and document.file_size > MAX_UPLOAD_SIZE:
        await update.message.reply_text("❌ File too large", reply_markup=main_reply_keyboard(uid))
        return

    ws = ensure_workspace(uid)
    safe = sanitize_filename(filename)
    path = ws / safe
    try:
        f = await context.bot.get_file(document.file_id)
        await f.download_to_drive(path)
    except Exception as e:
        await update.message.reply_text(f"Download failed: {e}", reply_markup=main_reply_keyboard(uid))
        return

    context.user_data["waiting_upload"] = False

    if filename.endswith(".zip"):
        extract_dir = ws / "extracted"
        extract_dir.mkdir(exist_ok=True)
        ok, msg = extract_zip(path, extract_dir)
        if not ok:
            await update.message.reply_text(f"❌ Zip: {msg}", reply_markup=main_reply_keyboard(uid))
            return
        _, entry = detect_entry_point(extract_dir)
        if not entry:
            await update.message.reply_text(
                "❌ No main.py / bot.py / index.js", reply_markup=main_reply_keyboard(uid)
            )
            return
        await install_dependencies(extract_dir)
        await update.message.reply_text(
            f"✅ Zip ready. Entry: `{Path(entry).name}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📂 My Scripts", callback_data="my_scripts")],
                    [InlineKeyboardButton("🔙 Menu", callback_data="main_menu")],
                ]
            ),
        )
        await context.bot.send_message(uid, "⬇️", reply_markup=main_reply_keyboard(uid))
        return

    await update.message.reply_text(
        f"✅ `{safe}` uploaded!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("▶️ Open My Scripts", callback_data="my_scripts")],
                [InlineKeyboardButton("🔙 Menu", callback_data="main_menu")],
            ]
        ),
    )
    await context.bot.send_message(uid, "Menu restored ⬇️", reply_markup=main_reply_keyboard(uid))


ALLOWED = {"pwd", "ls", "cd", "cat", "head", "tail", "mkdir", "cp", "mv", "rm"}


async def terminal_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if text in ("/cancel", "cancel"):
        context.user_data["in_terminal"] = False
        await update.message.reply_text("Terminal closed.", reply_markup=main_reply_keyboard(uid))
        return

    try:
        parts = shlex.split(text)
    except ValueError:
        await update.message.reply_text("Bad command quoting.")
        return
    if not parts:
        return
    cmd = parts[0].lower()
    if cmd not in ALLOWED:
        await update.message.reply_text(f"❌ `{cmd}` not allowed.", parse_mode="Markdown")
        return

    cwd = Path(context.user_data.get("terminal_cwd") or ensure_workspace(uid))

    if cmd == "cd":
        if len(parts) < 2:
            await update.message.reply_text("cd <dir>")
            return
        target = (cwd / parts[1]).resolve()
        if not is_safe_path(uid, target) or not target.is_dir():
            await update.message.reply_text("❌ Invalid directory")
            return
        context.user_data["terminal_cwd"] = str(target)
        await update.message.reply_text(f"📁 `{target}`", parse_mode="Markdown")
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = (out + err).decode(errors="ignore") or "(empty)"
        if len(output) > 3500:
            output = output[:3500] + "\n..."
        await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
    except asyncio.TimeoutError:
        await update.message.reply_text("Timeout")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# ---------- Callbacks: scripts / run / logs ----------
async def my_scripts_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not user_manager.is_approved(uid):
        return
    # reuse list via fake message path
    class W:
        effective_user = q.from_user
        effective_message = q.message

    ws = ensure_workspace(uid)
    files = list(ws.glob("*.py")) + list(ws.glob("*.js"))
    files += list(ws.glob("extracted/*.py")) + list(ws.glob("extracted/*.js"))
    if not files:
        await q.edit_message_text("📂 No scripts.", reply_markup=ik_back_main())
        return
    rows = []
    for f in files[:30]:
        k = path_registry.register(f)
        rows.append(
            [
                InlineKeyboardButton(f"📄 {f.name}", callback_data=f"view_script_{k}"),
                InlineKeyboardButton("▶️ Run", callback_data=f"run_script_{k}"),
            ]
        )
    rows.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
    await q.edit_message_text(
        "📂 *Scripts*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def view_script_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.replace("view_script_", "", 1)
    path = path_registry.get(key)
    if not path or not path.exists():
        await q.edit_message_text("File missing", reply_markup=ik_back("my_scripts"))
        return
    content = path.read_text(errors="ignore")[:800]
    await q.edit_message_text(
        f"📄 `{path.name}`\n```\n{content}\n```",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("▶️ Run", callback_data=f"run_script_{key}")],
                [InlineKeyboardButton("🔙 Scripts", callback_data="my_scripts")],
                [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")],
            ]
        ),
    )


async def run_script_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    key = q.data.replace("run_script_", "", 1)
    script = path_registry.get(key)
    if not script or not script.exists():
        await q.edit_message_text("File not found", reply_markup=ik_back_main())
        return
    ftype = "py" if script.suffix == ".py" else "js"

    for p in user_manager.get_user_processes(uid):
        if p.get("status") == "running":
            user_manager.stop_process(p["pid"])

    await q.edit_message_text("⏳ Starting...")
    pid, log_path, status = await run_script_with_watchdog(uid, script, ftype, context)
    if pid:
        user_manager.add_process(uid, script.name, pid, log_path)
    log_key = path_registry.register(Path(log_path))
    await q.edit_message_text(
        f"{status}\n📄 `{Path(log_path).name}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 View Log", callback_data=f"view_log_{log_key}")],
                [InlineKeyboardButton("📂 Scripts", callback_data="my_scripts")],
                [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")],
            ]
        ),
    )
    await context.bot.send_message(uid, "⬇️ Menu", reply_markup=main_reply_keyboard(uid))


async def view_log_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.replace("view_log_", "", 1)
    path = path_registry.get(key)
    if not path or not path.exists():
        await q.edit_message_text("Log missing", reply_markup=ik_back_main())
        return
    content = path.read_text(errors="ignore")[-3500:]
    await q.edit_message_text(
        f"📝 `{path.name}`\n```\n{content}\n```",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📂 Scripts", callback_data="my_scripts")],
                [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")],
            ]
        ),
    )


async def logs_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    rows = []
    for p in user_manager.get_user_processes(uid)[-20:]:
        lp = Path(p.get("log_path") or "")
        if lp.exists():
            k = path_registry.register(lp)
            rows.append([InlineKeyboardButton(f"📄 {lp.name}", callback_data=f"view_log_{k}")])
    if not rows:
        await q.edit_message_text("No logs", reply_markup=ik_back_main())
        return
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="main_menu")])
    await q.edit_message_text("📝 Logs", reply_markup=InlineKeyboardMarkup(rows))


async def stop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    live = [
        p
        for p in user_manager.get_user_processes(uid)
        if p.get("status") == "running" and psutil.pid_exists(p.get("pid"))
    ]
    if not live:
        await q.edit_message_text("No running process", reply_markup=ik_back_main())
        return
    rows = [
        [InlineKeyboardButton(f"🛑 {p['filename']} ({p['pid']})", callback_data=f"stop_proc_{p['pid']}")]
        for p in live
    ]
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="main_menu")])
    await q.edit_message_text("Stop which?", reply_markup=InlineKeyboardMarkup(rows))


async def stop_proc_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split("_")[2])
    user_manager.stop_process(pid)
    await q.edit_message_text(f"✅ Stopped `{pid}`", parse_mode="Markdown", reply_markup=ik_back_main())
    await context.bot.send_message(q.from_user.id, "⬇️", reply_markup=main_reply_keyboard(q.from_user.id))


async def my_stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tele = user_manager.get_user_telemetry(q.from_user.id)
    await q.edit_message_text(
        f"📊 Runs {tele['runs']} | ✅ {tele['success']} | ❌ {tele['fail']}",
        reply_markup=ik_back_main(),
    )


# ---------- Admin ----------
async def admin_panel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    await q.edit_message_text(
        "👑 *Admin*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("👥 Users", callback_data="admin_users"),
                    InlineKeyboardButton("⏳ Pending", callback_data="admin_pending"),
                ],
                [
                    InlineKeyboardButton("🖥️ Running", callback_data="admin_running"),
                    InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
                ],
                [
                    InlineKeyboardButton("🚫 Banned", callback_data="admin_banned"),
                    InlineKeyboardButton("📈 Telemetry", callback_data="admin_telemetry"),
                ],
                [
                    InlineKeyboardButton("🧹 Cleanup", callback_data="admin_cleanup"),
                    InlineKeyboardButton("🏠 Menu", callback_data="main_menu"),
                ],
            ]
        ),
    )


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    users = user_manager.get_approved_users()
    text = "👥 *Approved*\n" + "\n".join(f"• {u['name']} `{u['user_id']}`" for u in users[:40])
    await q.edit_message_text(text or "None", parse_mode="Markdown", reply_markup=ik_back("admin_panel"))


async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    pend = user_manager.get_pending_requests()
    if not pend:
        await q.edit_message_text("No pending", reply_markup=ik_back("admin_panel"))
        return
    rows = [
        [InlineKeyboardButton(f"{r['name']} ({r['user_id']})", callback_data=f"pending_{r['user_id']}")]
        for r in pend
    ]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await q.edit_message_text("⏳ Pending", reply_markup=InlineKeyboardMarkup(rows))


async def pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    uid = int(q.data.split("_")[1])
    u = user_manager.get_user(uid)
    if not u:
        return
    await q.edit_message_text(
        f"👤 {u['name']} `{uid}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
                    InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{uid}"),
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_pending")],
            ]
        ),
    )


async def approve_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    uid = int(q.data.split("_")[1])
    user_manager.approve_user(uid)
    await q.edit_message_text(f"✅ Approved `{uid}`", parse_mode="Markdown", reply_markup=ik_back("admin_pending"))
    try:
        await context.bot.send_message(
            uid, "✅ Approved! /start", reply_markup=main_reply_keyboard(uid)
        )
    except Exception:
        pass


async def ban_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    uid = int(q.data.split("_")[1])
    user_manager.ban_user(uid)
    await q.edit_message_text(f"🚫 Banned `{uid}`", parse_mode="Markdown", reply_markup=ik_back("admin_pending"))


async def admin_running(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    run = [p for p in user_manager.get_all_processes() if p.get("status") == "running"]
    text = "🖥️ *Running*\n" + "\n".join(
        f"• {p['filename']} pid {p['pid']} user {p['user_id']}" for p in run
    )
    await q.edit_message_text(text if run else "None", parse_mode="Markdown", reply_markup=ik_back("admin_panel"))


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    await q.edit_message_text(
        f"📊 Users {len(user_manager.users)}\n"
        f"✅ {len(user_manager.get_approved_users())} ⏳ {len(user_manager.get_pending_requests())}\n"
        f"🚫 {len(user_manager.get_banned_users())}",
        reply_markup=ik_back("admin_panel"),
    )


async def admin_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    banned = user_manager.get_banned_users()
    if not banned:
        await q.edit_message_text("No banned", reply_markup=ik_back("admin_panel"))
        return
    rows = [[InlineKeyboardButton(f"Unban {u['name']}", callback_data=f"unban_{u['user_id']}")] for u in banned]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await q.edit_message_text("🚫 Banned", reply_markup=InlineKeyboardMarkup(rows))


async def unban_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    uid = int(q.data.split("_")[1])
    user_manager.unban_user(uid)
    await q.edit_message_text(f"Unbanned `{uid}`", parse_mode="Markdown", reply_markup=ik_back("admin_banned"))


async def admin_telemetry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    t = user_manager.telemetry.values()
    await q.edit_message_text(
        f"📈 Runs {sum(x.get('runs',0) for x in t)} | ✅ {sum(x.get('success',0) for x in t)} | "
        f"❌ {sum(x.get('fail',0) for x in t)}",
        reply_markup=ik_back("admin_panel"),
    )


async def admin_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    cut = datetime.now() - timedelta(days=7)
    for uid in user_manager.users:
        ld = user_manager.get_workspace(int(uid)) / "logs"
        if ld.exists():
            for f in ld.glob("*.log"):
                try:
                    if datetime.fromtimestamp(f.stat().st_mtime) < cut:
                        f.unlink()
                except Exception:
                    pass
    await q.edit_message_text("🧹 Done", reply_markup=ik_back("admin_panel"))


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error: %s", context.error)


# Flask
flask_app = Flask("79")


@flask_app.route("/")
@flask_app.route("/healthz")
def health():
    return jsonify({"status": "ok", "bot": "79", "openai": bool(OPENAI_API_KEY)})


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def main():
    Thread(target=run_flask, daemon=True).start()
    logger.info("OpenAI key loaded: %s", bool(OPENAI_API_KEY))
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("cancel", lambda u, c: free_text_handler(u, c)))

    app.add_handler(CallbackQueryHandler(check_join, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(my_scripts_cb, pattern="^my_scripts$"))
    app.add_handler(CallbackQueryHandler(my_stats_cb, pattern="^my_stats$"))
    app.add_handler(CallbackQueryHandler(view_script_cb, pattern="^view_script_"))
    app.add_handler(CallbackQueryHandler(run_script_cb, pattern="^run_script_"))
    app.add_handler(CallbackQueryHandler(view_log_cb, pattern="^view_log_"))
    app.add_handler(CallbackQueryHandler(logs_cb, pattern="^logs$"))
    app.add_handler(CallbackQueryHandler(stop_cb, pattern="^stop$"))
    app.add_handler(CallbackQueryHandler(stop_proc_cb, pattern="^stop_proc_"))
    app.add_handler(CallbackQueryHandler(admin_panel_cb, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_users, pattern="^admin_users$"))
    app.add_handler(CallbackQueryHandler(admin_pending, pattern="^admin_pending$"))
    app.add_handler(CallbackQueryHandler(pending_action, pattern="^pending_"))
    app.add_handler(CallbackQueryHandler(approve_cb, pattern="^approve_"))
    app.add_handler(CallbackQueryHandler(ban_cb, pattern="^ban_"))
    app.add_handler(CallbackQueryHandler(admin_running, pattern="^admin_running$"))
    app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_banned, pattern="^admin_banned$"))
    app.add_handler(CallbackQueryHandler(unban_cb, pattern="^unban_"))
    app.add_handler(CallbackQueryHandler(admin_telemetry, pattern="^admin_telemetry$"))
    app.add_handler(CallbackQueryHandler(admin_cleanup, pattern="^admin_cleanup$"))
    # upload via inline still
    app.add_handler(CallbackQueryHandler(lambda u, c: _upload_cb(u, c), pattern="^upload$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: _term_cb(u, c), pattern="^terminal$"))

    app.add_handler(MessageHandler(filters.Document.ALL, upload_file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))
    app.add_handler(MessageHandler(filters.COMMAND, free_text_handler))

    app.add_error_handler(error_handler)
    logger.info("79 bot polling...")
    app.run_polling(drop_pending_updates=True)


async def _upload_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["waiting_upload"] = True
    await q.edit_message_text("📤 Send .py / .js / .zip now\n/cancel to abort")
    await context.bot.send_message(
        q.from_user.id,
        "Waiting for file…",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("/cancel")]], resize_keyboard=True),
    )


async def _term_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    ws = ensure_workspace(uid)
    context.user_data["terminal_cwd"] = str(ws)
    context.user_data["in_terminal"] = True
    await q.edit_message_text(f"💻 Terminal\n`{ws}`\n/cancel to exit", parse_mode="Markdown")
    await context.bot.send_message(
        uid,
        "Type commands…",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("/cancel")]], resize_keyboard=True),
    )


if __name__ == "__main__":
    main()
