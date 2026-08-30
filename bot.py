#!/usr/bin/env python3
"""
🔥 79 ULTIMATE SCRIPT HOSTING BOT – OP, NON-STUCK, CRASH-PROOF
Customized for @seventyx79
"""

import asyncio
import atexit
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
import logging
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Dict, List, Optional, Tuple, Any

import psutil
from dotenv import load_dotenv
from flask import Flask, jsonify
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("79HostingBot")

# ---------- ENV & CONFIG ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8731709572:AAGwoikRElJZFEUY9jXAttKy5QPFLEtllTE").strip()
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "7546911540").strip()
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    ADMIN_IDS = [7546911540]

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@seventyx79").strip()
PORT = int(os.getenv("PORT", "8080"))
WORKSPACE_BASE = Path(os.getenv("WORKSPACE_BASE", "./workspaces"))
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", "10MB").replace("MB", "")) * 1024 * 1024
MAX_ARCHIVE_SIZE = int(os.getenv("MAX_ARCHIVE_SIZE", "50MB").replace("MB", "")) * 1024 * 1024
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_TIMEOUT", "300"))

WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)

# ---------- PATH REGISTRY (Prevents Telegram 64-byte Callback Limit Crash) ----------
class PathRegistry:
    def __init__(self):
        self._registry = {}
        self._counter = 0

    def register(self, path: Path) -> str:
        path_str = str(path.resolve())
        for key, val in self._registry.items():
            if val == path_str:
                return key
        key = f"p79_{self._counter}"
        self._registry[key] = path_str
        self._counter += 1
        return key

    def get(self, key: str) -> Optional[Path]:
        path_str = self._registry.get(key)
        return Path(path_str) if path_str else None

path_registry = PathRegistry()

# ---------- PERSISTENCE ----------
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
        logger.error(f"Error saving data: {e}")

# ---------- USER & TELEMETRY MANAGER ----------
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

    def get_user(self, user_id: int) -> dict:
        return self.users.get(str(user_id), None)

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
        return u and u.get("status") == "approved"

    def is_pending(self, user_id: int) -> bool:
        if user_id in ADMIN_IDS:
            return False
        u = self.get_user(user_id)
        return u and u.get("status") == "pending"

    def is_banned(self, user_id: int) -> bool:
        u = self.get_user(user_id)
        return u and u.get("status") == "banned"

    def get_workspace(self, user_id: int) -> Path:
        u = self.get_user(user_id)
        if u:
            return Path(u["workspace"])
        return WORKSPACE_BASE / str(user_id)

    def get_pending_requests(self) -> List[dict]:
        return [{"user_id": k, **v} for k, v in self.users.items() if v.get("status") == "pending"]

    def get_approved_users(self) -> List[dict]:
        return [{"user_id": k, **v} for k, v in self.users.items() if v.get("status") == "approved"]

    def get_banned_users(self) -> List[dict]:
        return [{"user_id": k, **v} for k, v in self.users.items() if v.get("status") == "banned"]

    def add_process(self, user_id: int, filename: str, pid: int, log_path: str):
        proc = {
            "user_id": user_id,
            "filename": filename,
            "pid": pid,
            "start_time": datetime.now().isoformat(),
            "status": "running",
            "log_path": log_path,
        }
        self.processes.append(proc)
        self.save()
        return proc

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
        for p in self.processes:
            try:
                os.kill(p.get("pid"), signal.SIGTERM)
            except Exception:
                pass
        self.processes.clear()
        self.save()

    def inc_run(self, user_id: int):
        uid = str(user_id)
        self.telemetry.setdefault(uid, {"runs": 0, "success": 0, "fail": 0, "bad": 0})
        self.telemetry[uid]["runs"] += 1
        self.save()

    def inc_success(self, user_id: int):
        uid = str(user_id)
        self.telemetry.setdefault(uid, {"runs": 0, "success": 0, "fail": 0, "bad": 0})
        self.telemetry[uid]["success"] += 1
        self.save()

    def inc_fail(self, user_id: int):
        uid = str(user_id)
        self.telemetry.setdefault(uid, {"runs": 0, "success": 0, "fail": 0, "bad": 0})
        self.telemetry[uid]["fail"] += 1
        self.save()

    def inc_bad(self, user_id: int):
        uid = str(user_id)
        self.telemetry.setdefault(uid, {"runs": 0, "success": 0, "fail": 0, "bad": 0})
        self.telemetry[uid]["bad"] += 1
        self.save()

    def get_user_telemetry(self, user_id: int) -> dict:
        return self.telemetry.get(str(user_id), {"runs": 0, "success": 0, "fail": 0, "bad": 0})

user_manager = UserManager()
atexit.register(user_manager.cleanup_all)

# ---------- HELPER FUNCTIONS ----------
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

def extract_zip(user_id: int, zip_path: Path, dest_dir: Path) -> Tuple[bool, str]:
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            total_size = sum(info.file_size for info in zip_ref.infolist())
            if total_size > MAX_ARCHIVE_SIZE:
                return False, f"Archive too large (>{MAX_ARCHIVE_SIZE//1024//1024}MB)"
            for member in zip_ref.infolist():
                if member.filename.startswith("/") or ".." in member.filename:
                    return False, "Invalid file path in archive"
                target = dest_dir / member.filename
                try:
                    target.resolve().relative_to(dest_dir.resolve())
                except ValueError:
                    return False, "Path traversal attempt detected"
            zip_ref.extractall(dest_dir)
        return True, "Extraction successful"
    except Exception as e:
        return False, f"Extraction error: {str(e)}"

def detect_entry_point(dest_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    for py in dest_dir.glob("main.py"):
        return ("py", str(py))
    for js in dest_dir.glob("index.js"):
        return ("js", str(js))
    for py in dest_dir.glob("bot.py"):
        return ("py", str(py))
    py_files = list(dest_dir.glob("*.py"))
    if py_files:
        return ("py", str(py_files[0]))
    js_files = list(dest_dir.glob("*.js"))
    if js_files:
        return ("js", str(js_files[0]))
    return (None, None)

async def install_dependencies(user_id: int, dest_dir: Path) -> Tuple[bool, str]:
    req_file = dest_dir / "requirements.txt"
    package_file = dest_dir / "package.json"
    output_lines = []
    if req_file.exists():
        cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(dest_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        output_lines.append(f"pip install output:\n{output[:500]}...")
        if proc.returncode != 0:
            return False, f"Dependency installation failed: {output[:300]}"
    elif package_file.exists():
        cmd = ["npm", "install", "--production"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(dest_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode()
            output_lines.append(f"npm install output:\n{output[:500]}...")
            if proc.returncode != 0:
                return False, f"npm install failed: {output[:300]}"
        except Exception:
            output_lines.append("npm not found on system. Skipped.")
    else:
        output_lines.append("No dependency file found; skipping.")
    return True, "\n".join(output_lines)

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
    match = re.search(r"ModuleNotFoundError: No module named ['\"](.+?)['\"]", error_text)
    if match:
        return match.group(1)
    match = re.search(r"ImportError: No module named ['\"](.+?)['\"]", error_text)
    if match:
        return match.group(1)
    return None

# ---------- AI DEBUGGER (Crash-Proof) ----------
async def get_ai_debug_suggestion(error_log: str) -> str:
    if not OPENAI_API_KEY:
        return "🔧 79 AI Debugger inactive (no OpenAI API key configured)."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a concise debugging assistant. In 2 lines, explain how to fix this error."},
                {"role": "user", "content": f"Error:\n{error_log[:1500]}"}
            ],
            max_tokens=150,
        )
        return f"🤖 *79 AI Solution:*\n{response.choices[0].message.content.strip()}"
    except Exception as e:
        return f"⚠️ 79 AI Debugger: {str(e)}"

# ---------- ADVANCED BACKGROUND SCRIPT RUNNER ----------
async def run_script_with_watchdog(user_id: int, script_path: Path, file_type: str, context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, str, str]:
    user_manager.inc_run(user_id)
    ws = ensure_workspace(user_id)
    log_dir = ws / "logs"
    log_dir.mkdir(exist_ok=True)
    log_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{script_path.stem}.log"
    log_path = log_dir / log_name

    cmd = [sys.executable, "-u", str(script_path)] if file_type == "py" else ["node", str(script_path)]

    log_file = open(log_path, "w", buffering=1, encoding="utf-8", errors="ignore")

    proc = subprocess.Popen(
        cmd,
        cwd=str(script_path.parent),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL
    )

    # Initial stability check (Wait 3 seconds to see if script starts cleanly or crashes instantly)
    await asyncio.sleep(3)
    poll = proc.poll()

    if poll is None:
        # Running continuously in background!
        user_manager.inc_success(user_id)
        return (proc.pid, str(log_path), f"🚀 Script is running in background! (PID `{proc.pid}`)")
    
    # Process exited immediately, let's analyze logs
    log_file.close()
    with open(log_path, "r", errors="ignore") as f:
        output = f.read()

    if poll == 0:
        user_manager.inc_success(user_id)
        return (proc.pid, str(log_path), "✅ Script executed and exited successfully.")

    # Script crashed immediately
    missing = extract_module_name_from_error(output)
    if missing:
        await context.bot.send_message(user_id, f"⚙️ Auto-installing missing module: `{missing}`...")
        installed = await auto_install_module(missing)
        if installed:
            await context.bot.send_message(user_id, f"✅ Installed `{missing}`. Re-launching script...")
            log_file2 = open(log_path, "a", buffering=1, encoding="utf-8", errors="ignore")
            proc2 = subprocess.Popen(
                cmd,
                cwd=str(script_path.parent),
                stdout=log_file2,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL
            )
            await asyncio.sleep(3)
            poll2 = proc2.poll()
            if poll2 is None:
                user_manager.inc_success(user_id)
                return (proc2.pid, str(log_path), f"✅ Script is now running in background! (PID `{proc2.pid}`)")
            elif poll2 == 0:
                user_manager.inc_success(user_id)
                return (proc2.pid, str(log_path), "✅ Script executed successfully after auto-install.")
            else:
                user_manager.inc_fail(user_id)
                log_file2.close()
                with open(log_path, "r", errors="ignore") as f:
                    output2 = f.read()
                ai_fix = await get_ai_debug_suggestion(output2)
                return (proc2.pid, str(log_path), f"❌ Failed after auto-install.\n{ai_fix}")
        else:
            user_manager.inc_bad(user_id)
            ai_fix = await get_ai_debug_suggestion(output)
            return (proc.pid, str(log_path), f"❌ Could not install `{missing}`.\n{ai_fix}")
    else:
        user_manager.inc_fail(user_id)
        ai_fix = await get_ai_debug_suggestion(output)
        return (proc.pid, str(log_path), f"❌ Script exited with error code {poll}.\n{ai_fix}")

# ---------- BOT MENUS & KEYBOARDS ----------
(UPLOAD_WAIT, TERMINAL_SESSION) = range(2)

def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("💻 79 Terminal", callback_data="terminal"),
         InlineKeyboardButton("📁 Upload", callback_data="upload")],
        [InlineKeyboardButton("📂 My Scripts", callback_data="my_scripts"),
         InlineKeyboardButton("📝 View Logs", callback_data="logs")],
        [InlineKeyboardButton("🛑 Stop Script", callback_data="stop")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("👑 79 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

# ----- START / JOIN CHECK -----
async def is_member_of_channel(bot, user_id: int) -> bool:
    if not CHANNEL_USERNAME or user_id in ADMIN_IDS:
        return True
    try:
        chat_id = CHANNEL_USERNAME if CHANNEL_USERNAME.startswith("@") else f"@{CHANNEL_USERNAME}"
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logger.warning(f"Channel join check safely bypassed: {e}")
        return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "No username"
    fullname = user.full_name

    if not await is_member_of_channel(context.bot, user_id):
        clean_channel = CHANNEL_USERNAME.lstrip('@')
        await update.message.reply_text(
            f"🔒 *Access Locked*\nPlease join {CHANNEL_USERNAME} to use this bot.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{clean_channel}")],
                [InlineKeyboardButton("✅ Verify Join", callback_data="check_join")]
            ])
        )
        return

    u = user_manager.get_user(user_id)
    if not u:
        user_manager.add_user(user_id, fullname, username)
        if user_id not in ADMIN_IDS:
            admin_msg = f"🔔 *New 79 Registration*\n👤 {fullname} (@{username})\n🆔 `{user_id}`"
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        admin_msg,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
                             InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{user_id}")]
                        ])
                    )
                except Exception:
                    pass
            await update.message.reply_text("✅ *Request sent to 79 Admin for approval.*", parse_mode="Markdown")
            return

    if user_manager.is_banned(user_id):
        await update.message.reply_text("🚫 *Your access has been revoked.*", parse_mode="Markdown")
        return
    if user_manager.is_pending(user_id):
        await update.message.reply_text("⏳ *Your approval is pending with Admin.*", parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"👋 *Welcome {fullname} to 79 Hosting Engine!*\nOfficial Channel: {CHANNEL_USERNAME}\n\nChoose an action:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(user_id)
    )

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if not await is_member_of_channel(context.bot, user_id):
        clean_channel = CHANNEL_USERNAME.lstrip('@')
        await query.edit_message_text(
            f"❌ *You haven't joined {CHANNEL_USERNAME} yet!*\nPlease join and click Verify below:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{clean_channel}")],
                [InlineKeyboardButton("✅ Verify Join", callback_data="check_join")]
            ])
        )
        return

    user = query.from_user
    u = user_manager.get_user(user_id)
    if not u:
        user_manager.add_user(user_id, user.full_name, user.username or "No username")

    if user_manager.is_banned(user_id):
        await query.edit_message_text("🚫 *Banned.*", parse_mode="Markdown")
        return
    if user_manager.is_pending(user_id):
        await query.edit_message_text("⏳ *Your request is pending.*", parse_mode="Markdown")
        return

    await query.edit_message_text(
        f"👋 *Welcome {user.full_name} to 79 Hosting!*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(user_id)
    )

# ----- MAIN MENU -----
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not user_manager.is_approved(user_id):
        await query.edit_message_text("❌ Not approved.")
        return
    await query.edit_message_text(
        "📋 *79 Main Menu*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(user_id)
    )

# ----- UPLOAD CONVERSATION -----
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not user_manager.is_approved(user_id):
        await query.edit_message_text("❌ Not approved.")
        return ConversationHandler.END
    await query.edit_message_text(
        "📤 *Send a file* (`.py`, `.js`, or `.zip`)\n"
        "ZIP files extract automatically.\n"
        "Send `/cancel` to abort.",
        parse_mode="Markdown"
    )
    return UPLOAD_WAIT

async def upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Please send a file as document.")
        return UPLOAD_WAIT

    filename = document.file_name or "file.py"
    if not any(filename.endswith(ext) for ext in [".py", ".js", ".zip"]):
        await update.message.reply_text("❌ Only `.py`, `.js`, and `.zip` supported.")
        return ConversationHandler.END

    if document.file_size and document.file_size > MAX_UPLOAD_SIZE:
        await update.message.reply_text(f"❌ File too large (max {MAX_UPLOAD_SIZE//1024//1024}MB)")
        return ConversationHandler.END

    ws = ensure_workspace(user_id)
    safe_name = sanitize_filename(filename)
    file_path = ws / safe_name

    try:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(file_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Download failed: {str(e)}")
        return ConversationHandler.END

    if filename.endswith(".zip"):
        extract_dir = ws / "extracted"
        extract_dir.mkdir(exist_ok=True)
        success, msg = extract_zip(user_id, file_path, extract_dir)
        if not success:
            await update.message.reply_text(f"❌ Extraction error: {msg}")
            return ConversationHandler.END
        file_type, entry = detect_entry_point(extract_dir)
        if not entry:
            await update.message.reply_text("❌ No entry file (`main.py` / `bot.py` / `index.js`) found.")
            return ConversationHandler.END
        dep_ok, dep_msg = await install_dependencies(user_id, extract_dir)
        await update.message.reply_text(
            f"✅ *Archive Ready!*\nEntry Point: `{Path(entry).name}`\n▶️ Go to *My Scripts* to run.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ `{safe_name}` uploaded!\n▶️ Go to *My Scripts* to run.",
            parse_mode="Markdown"
        )
    return ConversationHandler.END

async def upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Upload cancelled.")
    return ConversationHandler.END

# ----- TERMINAL CONVERSATION -----
ALLOWED_COMMANDS = {"pwd", "ls", "cd", "cat", "head", "tail", "mkdir", "cp", "mv", "rm"}

async def terminal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not user_manager.is_approved(user_id):
        await query.edit_message_text("❌ Access not approved.")
        return ConversationHandler.END
    ws = ensure_workspace(user_id)
    context.user_data["terminal_cwd"] = str(ws)
    await query.edit_message_text(
        f"💻 *79 Terminal Session*\nPath: `{ws}`\n\n"
        "Commands: `pwd, ls, cd, cat, head, tail, mkdir, cp, mv, rm`\n"
        "Send `/cancel` to exit terminal.",
        parse_mode="Markdown"
    )
    return TERMINAL_SESSION

async def terminal_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text:
        return TERMINAL_SESSION

    if text.lower() == "/cancel":
        await update.message.reply_text("Terminal closed.")
        return ConversationHandler.END

    parts = shlex.split(text)
    cmd = parts[0].lower()
    if cmd not in ALLOWED_COMMANDS:
        await update.message.reply_text(f"❌ Command `{cmd}` not allowed.")
        return TERMINAL_SESSION

    cwd_str = context.user_data.get("terminal_cwd", str(ensure_workspace(user_id)))
    cwd = Path(cwd_str)

    if cmd == "cd":
        if len(parts) < 2:
            await update.message.reply_text("Usage: cd <dir>")
            return TERMINAL_SESSION
        target = (cwd / parts[1]).resolve()
        if not is_safe_path(user_id, target):
            await update.message.reply_text("❌ Cannot escape workspace directory.")
            return TERMINAL_SESSION
        if not target.is_dir():
            await update.message.reply_text(f"❌ `{target}` is not a directory.")
            return TERMINAL_SESSION
        context.user_data["terminal_cwd"] = str(target)
        await update.message.reply_text(f"📁 Directory: `{target}`", parse_mode="Markdown")
        return TERMINAL_SESSION

    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode() + stderr.decode()
        if not output.strip():
            output = "(empty output)"
        if len(output) > 3500:
            output = output[:3500] + "\n... (truncated)"
        await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
    except asyncio.TimeoutError:
        proc.terminate()
        await update.message.reply_text("❌ Command timeout.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    return TERMINAL_SESSION

async def terminal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Terminal session ended.")
    return ConversationHandler.END

# ----- SCRIPTS & EXECUTION -----
async def my_scripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    ws = ensure_workspace(user_id)
    files = list(ws.glob("*.py")) + list(ws.glob("*.js")) + list(ws.glob("extracted/*.py")) + list(ws.glob("extracted/*.js"))
    if not files:
        await query.edit_message_text(
            "📂 *No scripts found.*\nUpload a file using 📁 Upload.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]])
        )
        return
    keyboard = []
    for f in files:
        pkey = path_registry.register(f)
        keyboard.append([
            InlineKeyboardButton(f"📄 {f.name}", callback_data=f"view_script_{pkey}"),
            InlineKeyboardButton("▶️ Run", callback_data=f"run_script_{pkey}")
        ])
    keyboard.append([InlineKeyboardButton("📊 My Stats", callback_data="my_stats")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await query.edit_message_text(
        "📂 *79 Script Hub*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    tele = user_manager.get_user_telemetry(user_id)
    text = (
        f"📊 *79 Stats:*\n"
        f"🚀 Runs: {tele['runs']}\n"
        f"✅ Success: {tele['success']}\n"
        f"❌ Failed: {tele['fail']}\n"
        f"💀 Errors: {tele['bad']}"
    )
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="my_scripts")]]))

async def view_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pkey = query.data.replace("view_script_", "", 1)
    file_path = path_registry.get(pkey)
    if not file_path or not file_path.exists():
        await query.edit_message_text("❌ File not found.")
        return
    try:
        with open(file_path, "r", errors="ignore") as f:
            content = f.read(500)
            if len(content) >= 500:
                content += "\n... (truncated)"
            await query.edit_message_text(
                f"📄 *Script:* `{file_path.name}`\n\n```\n{content}\n```",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ Run", callback_data=f"run_script_{pkey}")],
                    [InlineKeyboardButton("🔙 Back", callback_data="my_scripts")]
                ])
            )
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {str(e)}")

async def run_script_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    pkey = query.data.replace("run_script_", "", 1)
    script_path = path_registry.get(pkey)

    if not script_path or not script_path.exists():
        await query.edit_message_text("❌ File not found.")
        return

    file_type = "py" if script_path.suffix == ".py" else "js"

    # Terminate existing running processes for this user
    for p in user_manager.get_user_processes(user_id):
        if p.get("status") == "running":
            user_manager.stop_process(p.get("pid"))

    await query.edit_message_text("⏳ *Starting script container...*", parse_mode="Markdown")
    pid, log_path, status_msg = await run_script_with_watchdog(user_id, script_path, file_type, context)
    
    if pid:
        user_manager.add_process(user_id, script_path.name, pid, log_path)

    log_key = path_registry.register(Path(log_path))
    await query.edit_message_text(
        f"{status_msg}\n📄 Log: `{Path(log_path).name}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 View Log", callback_data=f"view_log_{log_key}")],
            [InlineKeyboardButton("🔙 Back", callback_data="my_scripts")]
        ])
    )

async def view_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    log_key = query.data.replace("view_log_", "", 1)
    log_path = path_registry.get(log_key)
    if not log_path or not log_path.exists():
        await query.edit_message_text("❌ Log not found.")
        return
    try:
        with open(log_path, "r", errors="ignore") as f:
            content = f.read(3000)
            if len(content) >= 3000:
                content += "\n... (truncated)"
            await query.edit_message_text(
                f"📝 *Log:* `{log_path.name}`\n\n```\n{content}\n```",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="my_scripts")]])
            )
    except Exception as e:
        await query.edit_message_text(f"❌ Error reading log: {str(e)}")

async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    procs = user_manager.get_user_processes(user_id)
    if not procs:
        await query.edit_message_text("📝 No logs yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))
        return
    keyboard = []
    for p in procs:
        lp = Path(p.get("log_path", ""))
        if lp.exists():
            lkey = path_registry.register(lp)
            keyboard.append([InlineKeyboardButton(f"📄 {lp.name}", callback_data=f"view_log_{lkey}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await query.edit_message_text("📝 *Execution Logs:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def stop_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    running = [p for p in user_manager.get_user_processes(user_id) if p.get("status") == "running"]
    if not running:
        await query.edit_message_text("🛑 *No running scripts.*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))
        return
    keyboard = [[InlineKeyboardButton(f"🛑 Stop {p['filename']} ({p['pid']})", callback_data=f"stop_proc_{p['pid']}")] for p in running]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await query.edit_message_text("Select process to stop:", reply_markup=InlineKeyboardMarkup(keyboard))

async def stop_proc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = int(query.data.split("_")[2])
    user_manager.stop_process(pid)
    await query.edit_message_text(f"✅ Process {pid} stopped.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

# ----- ADMIN PANEL -----
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    keyboard = [
        [InlineKeyboardButton("👥 Users", callback_data="admin_users"), InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
        [InlineKeyboardButton("🖥️ Running", callback_data="admin_running"), InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🚫 Banned", callback_data="admin_banned"), InlineKeyboardButton("📈 Telemetry", callback_data="admin_telemetry")],
        [InlineKeyboardButton("🧹 Cleanup", callback_data="admin_cleanup"), InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")],
    ]
    await query.edit_message_text("👑 *79 Admin Panel*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    users = user_manager.get_approved_users()
    text = "👥 *Approved Users:*\n\n" + "\n".join([f"• {u['name']} (`{u['user_id']}`)" for u in users]) if users else "No approved users."
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    pendings = user_manager.get_pending_requests()
    if not pendings:
        await query.edit_message_text("No pending requests.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return
    keyboard = [[InlineKeyboardButton(f"{req['name']} ({req['user_id']})", callback_data=f"pending_{req['user_id']}")] for req in pendings]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text("⏳ *Pending Requests:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    uid = int(query.data.split("_")[1])
    u = user_manager.get_user(uid)
    if not u:
        return
    keyboard = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
         InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{uid}")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_pending")]
    ]
    await query.edit_message_text(f"👤 *{u['name']}* (`{uid}`)\nTime: {u['request_time']}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def approve_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    uid = int(query.data.split("_")[1])
    user_manager.approve_user(uid)
    await query.edit_message_text(f"✅ User `{uid}` Approved!", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_pending")]]))
    try:
        await context.bot.send_message(uid, "✅ *Your 79 Hosting account is approved!* Send /start to begin.", parse_mode="Markdown")
    except Exception:
        pass

async def ban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    uid = int(query.data.split("_")[1])
    user_manager.ban_user(uid)
    await query.edit_message_text(f"🚫 User `{uid}` Banned.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_pending")]]))

async def admin_running(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    running = [p for p in user_manager.get_all_processes() if p.get("status") == "running"]
    text = "🖥️ *Running Processes:*\n\n" + "\n".join([f"• {p['filename']} ({p['pid']}) – `{p['user_id']}`" for p in running]) if running else "No processes running."
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    text = (
        f"📊 *79 Global Stats:*\n"
        f"👥 Total Users: {len(user_manager.users)}\n"
        f"✅ Approved: {len(user_manager.get_approved_users())}\n"
        f"⏳ Pending: {len(user_manager.get_pending_requests())}\n"
        f"🚫 Banned: {len(user_manager.get_banned_users())}\n"
        f"🖥️ CPU Tasks: {len([p for p in user_manager.get_all_processes() if p.get('status') == 'running'])}"
    )
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def admin_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    banned = user_manager.get_banned_users()
    if not banned:
        await query.edit_message_text("No banned users.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
        return
    keyboard = [[InlineKeyboardButton(f"Unban {u['name']}", callback_data=f"unban_{u['user_id']}")] for u in banned]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text("🚫 *Banned Users:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    uid = int(query.data.split("_")[1])
    user_manager.unban_user(uid)
    await query.edit_message_text(f"✅ User `{uid}` unbanned.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def admin_telemetry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    tele = user_manager.telemetry
    runs = sum(t.get("runs", 0) for t in tele.values())
    succ = sum(t.get("success", 0) for t in tele.values())
    fail = sum(t.get("fail", 0) for t in tele.values())
    bad = sum(t.get("bad", 0) for t in tele.values())
    text = f"📈 *79 Telemetry Overview*\n\nRuns: {runs}\n✅ Success: {succ}\n❌ Failed: {fail}\n💀 Bad Errors: {bad}"
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def admin_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    seven_days = datetime.now() - timedelta(days=7)
    for uid in user_manager.users:
        ld = user_manager.get_workspace(int(uid)) / "logs"
        if ld.exists():
            for f in ld.glob("*.log"):
                if datetime.fromtimestamp(f.stat().st_mtime) < seven_days:
                    try:
                        f.unlink()
                    except Exception:
                        pass
    await query.edit_message_text("🧹 Logs older than 7 days removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

# ---------- ERROR HANDLER ----------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Telegram Exception: {context.error}")

# ---------- FLASK KEEP-ALIVE SERVER ----------
flask_app = Flask("79KeepAlive")

@flask_app.route('/')
@flask_app.route('/healthz')
def health():
    return jsonify({"status": "healthy", "bot": "79-hosting-bot"})

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ---------- MAIN RUNNER ----------
def main():
    Thread(target=run_flask, daemon=True).start()
    logger.info(f"Web health service running on port {PORT}")

    app = Application.builder().token(BOT_TOKEN).build()

    upload_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(upload_start, pattern="^upload$")],
        states={UPLOAD_WAIT: [MessageHandler(filters.Document.ALL, upload_receive)]},
        fallbacks=[CommandHandler("cancel", upload_cancel)],
    )
    app.add_handler(upload_conv)

    terminal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(terminal_start, pattern="^terminal$")],
        states={TERMINAL_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, terminal_handle)]},
        fallbacks=[CommandHandler("cancel", terminal_cancel)],
    )
    app.add_handler(terminal_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_join, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(my_scripts, pattern="^my_scripts$"))
    app.add_handler(CallbackQueryHandler(my_stats, pattern="^my_stats$"))
    app.add_handler(CallbackQueryHandler(view_script, pattern="^view_script_"))
    app.add_handler(CallbackQueryHandler(run_script_callback, pattern="^run_script_"))
    app.add_handler(CallbackQueryHandler(view_log, pattern="^view_log_"))
    app.add_handler(CallbackQueryHandler(view_logs, pattern="^logs$"))
    app.add_handler(CallbackQueryHandler(stop_script, pattern="^stop$"))
    app.add_handler(CallbackQueryHandler(stop_proc_callback, pattern="^stop_proc_"))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_users, pattern="^admin_users$"))
    app.add_handler(CallbackQueryHandler(admin_pending, pattern="^admin_pending$"))
    app.add_handler(CallbackQueryHandler(pending_action, pattern="^pending_"))
    app.add_handler(CallbackQueryHandler(approve_user_callback, pattern="^approve_"))
    app.add_handler(CallbackQueryHandler(ban_user_callback, pattern="^ban_"))
    app.add_handler(CallbackQueryHandler(admin_running, pattern="^admin_running$"))
    app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_banned, pattern="^admin_banned$"))
    app.add_handler(CallbackQueryHandler(unban_callback, pattern="^unban_"))
    app.add_handler(CallbackQueryHandler(admin_telemetry, pattern="^admin_telemetry$"))
    app.add_handler(CallbackQueryHandler(admin_cleanup, pattern="^admin_cleanup$"))

    app.add_error_handler(error_handler)

    logger.info("Bot is starting polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
