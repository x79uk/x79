#!/usr/bin/env python3
"""
🔥 ULTIMATE SCRIPT HOSTING BOT – OP, NON-STUCK, AUTO-REPAIR
Supports multiple admins (ADMIN_IDS comma-separated).
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
    constants,
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

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")  # comma-separated, e.g., "123,456,789"
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    # Fallback to old ADMIN_ID for backward compatibility
    old_admin = os.getenv("ADMIN_ID")
    if old_admin and old_admin.isdigit():
        ADMIN_IDS = [int(old_admin)]
    else:
        raise ValueError("ADMIN_IDS or ADMIN_ID must be set with at least one admin ID")

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()
PORT = int(os.getenv("PORT", "8080"))
WORKSPACE_BASE = Path(os.getenv("WORKSPACE_BASE", "./workspaces"))
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", "10MB").replace("MB", "")) * 1024 * 1024
MAX_ARCHIVE_SIZE = int(os.getenv("MAX_ARCHIVE_SIZE", "50MB").replace("MB", "")) * 1024 * 1024
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # Optional
SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_TIMEOUT", "300"))  # 5 min default

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN must be set")

WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)

# ---------- PERSISTENCE ----------
DATA_FILE = "bot_data.json"

def load_data():
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"users": {}, "processes": [], "telemetry": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ---------- USER & TELEMETRY MANAGER ----------
class UserManager:
    def __init__(self):
        self.data = load_data()
        self.users = self.data.get("users", {})
        self.processes = self.data.get("processes", [])
        self.telemetry = self.data.get("telemetry", {})
        self._running_processes = {}  # pid -> (proc, log_file, user_id)

    def save(self):
        self.data["users"] = self.users
        self.data["processes"] = self.processes
        self.data["telemetry"] = self.telemetry
        save_data(self.data)

    def get_user(self, user_id: int) -> dict:
        return self.users.get(str(user_id), None)

    def add_user(self, user_id: int, name: str, username: str):
        self.users[str(user_id)] = {
            "status": "pending",
            "name": name,
            "username": username,
            "request_time": datetime.now().isoformat(),
            "workspace": str(WORKSPACE_BASE / str(user_id)),
        }
        self.telemetry[str(user_id)] = {"runs": 0, "success": 0, "fail": 0, "bad": 0}
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
        u = self.get_user(user_id)
        return u and u.get("status") == "approved"

    def is_pending(self, user_id: int) -> bool:
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
        return [{"user_id": k, **v} for k, v in self.users.items() if v["status"] == "pending"]

    def get_approved_users(self) -> List[dict]:
        return [{"user_id": k, **v} for k, v in self.users.items() if v["status"] == "approved"]

    def get_banned_users(self) -> List[dict]:
        return [{"user_id": k, **v} for k, v in self.users.items() if v["status"] == "banned"]

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
        return [p for p in self.processes if p["user_id"] == user_id]

    def get_all_processes(self) -> List[dict]:
        return self.processes

    def stop_process(self, pid: int) -> bool:
        for p in self.processes:
            if p["pid"] == pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except:
                    pass
                p["status"] = "stopped"
                self.save()
                return True
        return False

    def remove_terminated(self):
        to_remove = []
        for i, p in enumerate(self.processes):
            if not psutil.pid_exists(p["pid"]):
                to_remove.append(i)
        for i in reversed(to_remove):
            self.processes.pop(i)
        if to_remove:
            self.save()

    def cleanup_all(self):
        for p in self.processes:
            try:
                os.kill(p["pid"], signal.SIGTERM)
            except:
                pass
        self.processes.clear()
        self.save()

    # Telemetry
    def inc_run(self, user_id: int):
        uid = str(user_id)
        if uid not in self.telemetry:
            self.telemetry[uid] = {"runs": 0, "success": 0, "fail": 0, "bad": 0}
        self.telemetry[uid]["runs"] += 1
        self.save()

    def inc_success(self, user_id: int):
        uid = str(user_id)
        if uid not in self.telemetry:
            self.telemetry[uid] = {"runs": 0, "success": 0, "fail": 0, "bad": 0}
        self.telemetry[uid]["success"] += 1
        self.save()

    def inc_fail(self, user_id: int):
        uid = str(user_id)
        if uid not in self.telemetry:
            self.telemetry[uid] = {"runs": 0, "success": 0, "fail": 0, "bad": 0}
        self.telemetry[uid]["fail"] += 1
        self.save()

    def inc_bad(self, user_id: int):
        uid = str(user_id)
        if uid not in self.telemetry:
            self.telemetry[uid] = {"runs": 0, "success": 0, "fail": 0, "bad": 0}
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
        cmd = ["pip", "install", "--user", "-r", str(req_file)]
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
    else:
        output_lines.append("No dependency file found; skipping installation.")
    return True, "\n".join(output_lines)

# ---------- AUTO INSTALL MISSING MODULE ----------
async def auto_install_module(module_name: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--user", module_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _ = await proc.communicate()
        return proc.returncode == 0
    except:
        return False

def extract_module_name_from_error(error_text: str) -> Optional[str]:
    match = re.search(r"ModuleNotFoundError: No module named ['\"](.+?)['\"]", error_text)
    if match:
        return match.group(1)
    match = re.search(r"ImportError: No module named ['\"](.+?)['\"]", error_text)
    if match:
        return match.group(1)
    return None

# ---------- AI DEBUGGER ----------
async def get_ai_debug_suggestion(error_log: str) -> str:
    if not OPENAI_API_KEY:
        return "🔧 AI Debugger inactive (no OpenAI API key)."
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful Python/Node.js debugging assistant. Given the error log, suggest a fix."},
                {"role": "user", "content": f"Error log:\n{error_log[:2000]}"}
            ],
            max_tokens=200,
        )
        return f"🤖 *AI Suggestion:*\n{response.choices[0].message.content.strip()}"
    except Exception as e:
        return f"⚠️ AI Debugger error: {str(e)}"

# ---------- NON-STUCK SCRIPT RUNNER ----------
async def run_script_with_watchdog(user_id: int, script_path: Path, file_type: str, context: ContextTypes.DEFAULT_TYPE) -> Tuple[int, str, str]:
    """Run script with timeout, auto-kill, and auto-pip."""
    user_manager.inc_run(user_id)
    ws = ensure_workspace(user_id)
    log_dir = ws / "logs"
    log_dir.mkdir(exist_ok=True)
    log_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{script_path.stem}.log"
    log_path = log_dir / log_name

    if file_type == "py":
        cmd = [sys.executable, str(script_path)]
    else:
        cmd = ["node", str(script_path)]

    # First attempt
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(script_path.parent),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout_data, _ = await asyncio.wait_for(proc.communicate(), timeout=SCRIPT_TIMEOUT)
        output = stdout_data.decode()
        returncode = proc.returncode
    except asyncio.TimeoutError:
        proc.terminate()
        await proc.wait()
        output = f"⏰ Script exceeded {SCRIPT_TIMEOUT}s timeout and was terminated.\n"
        returncode = -1

    if returncode != 0:
        missing = extract_module_name_from_error(output)
        if missing:
            await context.bot.send_message(user_id, f"⚙️ Missing `{missing}`. Installing...")
            installed = await auto_install_module(missing)
            if installed:
                await context.bot.send_message(user_id, f"✅ Installed `{missing}`. Restarting...")
                proc2 = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(script_path.parent),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=SCRIPT_TIMEOUT)
                    output2 = stdout2.decode()
                    returncode2 = proc2.returncode
                except asyncio.TimeoutError:
                    proc2.terminate()
                    await proc2.wait()
                    output2 = f"⏰ Script exceeded {SCRIPT_TIMEOUT}s timeout and was terminated.\n"
                    returncode2 = -1
                with open(log_path, "w") as f:
                    f.write(f"[First attempt]\n{output}\n\n[After install]\n{output2}")
                if returncode2 == 0:
                    user_manager.inc_success(user_id)
                    return (proc2.pid, str(log_path), "✅ Success after auto-install.")
                else:
                    user_manager.inc_fail(user_id)
                    ai_suggestion = await get_ai_debug_suggestion(output2)
                    return (proc2.pid, str(log_path), f"❌ Still failing after install.\n{ai_suggestion}")
            else:
                user_manager.inc_bad(user_id)
                with open(log_path, "w") as f:
                    f.write(output)
                ai_suggestion = await get_ai_debug_suggestion(output)
                return (proc.pid, str(log_path), f"❌ Failed to install `{missing}`.\n{ai_suggestion}")
        else:
            user_manager.inc_fail(user_id)
            with open(log_path, "w") as f:
                f.write(output)
            ai_suggestion = await get_ai_debug_suggestion(output)
            return (proc.pid, str(log_path), f"❌ Script crashed.\n{ai_suggestion}")
    else:
        user_manager.inc_success(user_id)
        with open(log_path, "w") as f:
            f.write(output)
        return (proc.pid, str(log_path), "✅ Script executed successfully.")

# ---------- TELEGRAM BOT HANDLERS ----------
(UPLOAD_WAIT, TERMINAL_SESSION) = range(2)

def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("💻 Terminal", callback_data="terminal"),
         InlineKeyboardButton("📁 Upload", callback_data="upload")],
        [InlineKeyboardButton("📂 My Scripts", callback_data="my_scripts"),
         InlineKeyboardButton("📝 View Logs", callback_data="logs")],
        [InlineKeyboardButton("🛑 Stop Script", callback_data="stop")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

# ----- START / JOIN -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "No username"
    fullname = user.full_name

    if CHANNEL_USERNAME:
        try:
            member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
            if member.status in ("left", "kicked"):
                await update.message.reply_text(
                    f"🔒 *Join {CHANNEL_USERNAME} to use this bot*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📢 Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
                        [InlineKeyboardButton("✅ I Joined — Check", callback_data="check_join")]
                    ])
                )
                return
        except Exception:
            await update.message.reply_text("⚠️ Could not verify channel. Try again later.")
            return

    u = user_manager.get_user(user_id)
    if not u:
        user_manager.add_user(user_id, fullname, username)
        admin_msg = (
            f"🔔 *New Access Request*\n"
            f"👤 {fullname}\n"
            f"🆔 `{user_id}`\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        for admin_id in ADMIN_IDS:
            await context.bot.send_message(
                admin_id,
                admin_msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
                     InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{user_id}")]
                ])
            )
        await update.message.reply_text("✅ *Request sent to admin.*", parse_mode="Markdown")
        return

    if user_manager.is_banned(user_id):
        await update.message.reply_text("🚫 *You are banned.*", parse_mode="Markdown")
        return
    if user_manager.is_pending(user_id):
        await update.message.reply_text("⏳ *Your request is pending.*", parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"👋 *Welcome {fullname}!*\nSelect an option:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(user_id)
    )

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if CHANNEL_USERNAME:
        try:
            member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
            if member.status in ("left", "kicked"):
                await query.edit_message_text(
                    "❌ Still not joined.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📢 Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
                        [InlineKeyboardButton("✅ I Joined — Check", callback_data="check_join")]
                    ])
                )
                return
        except Exception:
            await query.edit_message_text("⚠️ Error checking membership.")
            return
    user = query.from_user
    fullname = user.full_name
    username = user.username or "No username"
    u = user_manager.get_user(user_id)
    if not u:
        user_manager.add_user(user_id, fullname, username)
        admin_msg = (
            f"🔔 *New Access Request*\n"
            f"👤 {fullname}\n"
            f"🆔 `{user_id}`\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        for admin_id in ADMIN_IDS:
            await context.bot.send_message(
                admin_id,
                admin_msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
                     InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{user_id}")]
                ])
            )
        await query.edit_message_text("✅ Request sent.")
        return
    if user_manager.is_banned(user_id):
        await query.edit_message_text("🚫 Banned.")
        return
    if user_manager.is_pending(user_id):
        await query.edit_message_text("⏳ Pending.")
        return
    await query.edit_message_text(
        f"👋 *Welcome {fullname}!*",
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
        "📋 *Main Menu*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(user_id)
    )

# ----- UPLOAD -----
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not user_manager.is_approved(user_id):
        await query.edit_message_text("❌ Not approved.")
        return
    await query.edit_message_text(
        "📤 *Send a file* (`.py`, `.js`, or `.zip`)\n"
        "ZIP will be extracted.",
        parse_mode="Markdown"
    )
    return UPLOAD_WAIT

async def upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Please send a file.")
        return UPLOAD_WAIT

    filename = document.file_name
    if not any(filename.endswith(ext) for ext in [".py", ".js", ".zip"]):
        await update.message.reply_text("❌ Only `.py`, `.js`, `.zip` allowed.")
        return ConversationHandler.END

    if document.file_size > MAX_UPLOAD_SIZE:
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
            await update.message.reply_text(f"❌ Extraction failed: {msg}")
            return ConversationHandler.END
        file_type, entry = detect_entry_point(extract_dir)
        if not entry:
            await update.message.reply_text("❌ No `main.py` or `index.js` found.")
            return ConversationHandler.END
        dep_ok, dep_msg = await install_dependencies(user_id, extract_dir)
        if not dep_ok:
            await update.message.reply_text(f"❌ Dependency install failed:\n{dep_msg}")
            return ConversationHandler.END
        await update.message.reply_text(
            f"✅ Archive extracted and dependencies installed.\n"
            f"📄 Entry: `{Path(entry).name}`\n"
            "▶️ Use *My Scripts* to run.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ `{safe_name}` uploaded.\n"
            "▶️ Use *My Scripts* to run.",
            parse_mode="Markdown"
        )
    return ConversationHandler.END

async def upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Upload cancelled.")
    return ConversationHandler.END

# ----- TERMINAL -----
ALLOWED_COMMANDS = {"pwd", "ls", "cd", "cat", "head", "tail", "mkdir", "cp", "mv", "rm"}

async def terminal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not user_manager.is_approved(user_id):
        await query.edit_message_text("❌ Not approved.")
        return
    ws = ensure_workspace(user_id)
    context.user_data["terminal_cwd"] = str(ws)
    await query.edit_message_text(
        f"💻 *Terminal*\n`{ws}`\n\n"
        "Allowed: `pwd, ls, cd, cat, head, tail, mkdir, cp, mv, rm`\n"
        "Send `/cancel` to exit.",
        parse_mode="Markdown"
    )
    return TERMINAL_SESSION

async def terminal_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Please enter a command.")
        return TERMINAL_SESSION

    if text.lower() == "/cancel":
        await update.message.reply_text("Terminal ended.")
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
        target = Path(parts[1])
        if not target.is_absolute():
            target = cwd / target
        try:
            target = target.resolve()
            if not is_safe_path(user_id, target):
                await update.message.reply_text("❌ Cannot cd outside workspace.")
                return TERMINAL_SESSION
            if not target.is_dir():
                await update.message.reply_text(f"❌ `{target}` is not a directory.", parse_mode="Markdown")
                return TERMINAL_SESSION
            context.user_data["terminal_cwd"] = str(target)
            await update.message.reply_text(f"📁 Changed to: `{target}`", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
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
            output = "(no output)"
        if len(output) > 4000:
            output = output[:4000] + "\n... (truncated)"
        await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
    except asyncio.TimeoutError:
        proc.terminate()
        await update.message.reply_text("❌ Command timed out.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    return TERMINAL_SESSION

async def terminal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Terminal cancelled.")
    return ConversationHandler.END

# ----- MY SCRIPTS WITH RUN -----
async def my_scripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    ws = ensure_workspace(user_id)
    files = list(ws.glob("*.py")) + list(ws.glob("*.js")) + list(ws.glob("extracted/*.py")) + list(ws.glob("extracted/*.js"))
    if not files:
        await query.edit_message_text("📂 *No scripts found.*\nUpload one using 📁 Upload.", parse_mode="Markdown")
        return
    keyboard = []
    for f in files:
        keyboard.append([
            InlineKeyboardButton(f"📄 {f.name}", callback_data=f"view_script_{f}"),
            InlineKeyboardButton("▶️ Run", callback_data=f"run_script_{f}")
        ])
    keyboard.append([InlineKeyboardButton("📊 My Stats", callback_data="my_stats")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await query.edit_message_text(
        "📂 *Your Scripts*\nClick a file to view or run.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    tele = user_manager.get_user_telemetry(user_id)
    text = (
        f"📊 *Your Telemetry*\n"
        f"🚀 Total Runs: {tele['runs']}\n"
        f"✅ Success: {tele['success']}\n"
        f"❌ Fail: {tele['fail']}\n"
        f"💀 Bad (critical): {tele['bad']}"
    )
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="my_scripts")]]))

async def view_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    file_path = query.data.replace("view_script_", "", 1)
    try:
        with open(file_path, "r") as f:
            content = f.read(500)
            if len(content) >= 500:
                content += "\n... (truncated)"
            await query.edit_message_text(
                f"📄 *File:* `{Path(file_path).name}`\n\n```\n{content}\n```",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ Run", callback_data=f"run_script_{file_path}")],
                    [InlineKeyboardButton("🔙 Back", callback_data="my_scripts")]
                ])
            )
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {str(e)}")

async def run_script_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    file_path = query.data.replace("run_script_", "", 1)
    script_path = Path(file_path)
    if not script_path.exists():
        await query.edit_message_text("❌ File not found.")
        return
    if script_path.suffix == ".py":
        file_type = "py"
    elif script_path.suffix == ".js":
        file_type = "js"
    else:
        await query.edit_message_text("❌ Unsupported file type.")
        return

    # Auto-kill old running processes for this user
    procs = user_manager.get_user_processes(user_id)
    running = [p for p in procs if p["status"] == "running"]
    if running:
        for p in running:
            user_manager.stop_process(p["pid"])
        await context.bot.send_message(user_id, "⚡ Terminated old process. Starting new...")

    await query.edit_message_text("⏳ *Starting script...*", parse_mode="Markdown")
    pid, log_path, status_msg = await run_script_with_watchdog(user_id, script_path, file_type, context)
    if pid:
        user_manager.add_process(user_id, script_path.name, pid, log_path)
    await query.edit_message_text(
        f"{status_msg}\n📄 Log: `{Path(log_path).name}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 View Log", callback_data=f"view_log_{log_path}")],
            [InlineKeyboardButton("🔙 Back", callback_data="my_scripts")]
        ])
    )

async def view_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    log_path = query.data.replace("view_log_", "", 1)
    try:
        with open(log_path, "r") as f:
            content = f.read(3000)
            if len(content) >= 3000:
                content += "\n... (truncated)"
            await query.edit_message_text(
                f"📝 *Log: `{Path(log_path).name}`*\n\n```\n{content}\n```",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="my_scripts")]
                ])
            )
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {str(e)}")

# ----- VIEW LOGS (list) -----
async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    procs = user_manager.get_user_processes(user_id)
    if not procs:
        await query.edit_message_text("📝 No logs available.")
        return
    keyboard = []
    for p in procs:
        log_path = Path(p.get("log_path", ""))
        if log_path.exists():
            keyboard.append([
                InlineKeyboardButton(f"📄 {log_path.name}", callback_data=f"view_log_{log_path}")
            ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await query.edit_message_text(
        "📝 *Your Logs*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ----- STOP SCRIPT -----
async def stop_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    procs = user_manager.get_user_processes(user_id)
    running = [p for p in procs if p["status"] == "running"]
    if not running:
        await query.edit_message_text("🛑 *No running processes.*", parse_mode="Markdown")
        return
    keyboard = []
    for p in running:
        keyboard.append([InlineKeyboardButton(
            f"Stop {p['filename']} (PID {p['pid']})",
            callback_data=f"stop_proc_{p['pid']}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await query.edit_message_text(
        "Select a process to stop:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def stop_proc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    pid = int(query.data.split("_")[2])
    proc = next((p for p in user_manager.get_user_processes(user_id) if p["pid"] == pid), None)
    if not proc:
        await query.edit_message_text("❌ Process not found.")
        return
    if user_manager.stop_process(pid):
        await query.edit_message_text(f"✅ Process {pid} stopped.")
    else:
        await query.edit_message_text("❌ Failed to stop.")

# ----- ADMIN PANEL (ADVANCED) -----
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    keyboard = [
        [InlineKeyboardButton("👥 Users", callback_data="admin_users"),
         InlineKeyboardButton("⏳ Pending", callback_data="admin_pending")],
        [InlineKeyboardButton("🖥️ Running", callback_data="admin_running"),
         InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🚫 Banned", callback_data="admin_banned")],
        [InlineKeyboardButton("📈 Telemetry", callback_data="admin_telemetry")],
        [InlineKeyboardButton("🧹 Cleanup", callback_data="admin_cleanup")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ]
    await query.edit_message_text(
        "👑 *Admin Panel*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    users = user_manager.get_approved_users()
    if not users:
        await query.edit_message_text("No approved users.")
        return
    text = "👥 *Approved Users*\n\n"
    for u in users:
        tele = user_manager.get_user_telemetry(int(u['user_id']))
        text += f"• {u['name']} (@{u['username']}) – `{u['user_id']}`\n"
        text += f"  Runs: {tele['runs']} | ✅{tele['success']} ❌{tele['fail']} 💀{tele['bad']}\n"
    await query.edit_message_text(text, parse_mode="Markdown")

async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    pendings = user_manager.get_pending_requests()
    if not pendings:
        await query.edit_message_text("No pending requests.")
        return
    keyboard = []
    for req in pendings:
        uid = req['user_id']
        keyboard.append([InlineKeyboardButton(
            f"{req['name']} (@{req['username']})",
            callback_data=f"pending_{uid}"
        )])
    await query.edit_message_text(
        "⏳ *Pending Requests*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    uid = int(query.data.split("_")[1])
    user = user_manager.get_user(uid)
    if not user:
        await query.edit_message_text("User not found.")
        return
    keyboard = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
         InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{uid}")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_pending")]
    ]
    await query.edit_message_text(
        f"👤 *{user['name']}* (@{user['username']})\n🆔 `{uid}`\n📅 {user['request_time']}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def approve_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    uid = int(query.data.split("_")[1])
    if user_manager.approve_user(uid):
        await query.edit_message_text(f"✅ User `{uid}` approved.")
        try:
            await context.bot.send_message(uid, "✅ *Access approved!* Use /start.", parse_mode="Markdown")
        except:
            pass
    else:
        await query.edit_message_text("❌ Failed to approve.")

async def ban_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    uid = int(query.data.split("_")[1])
    if user_manager.ban_user(uid):
        await query.edit_message_text(f"🚫 User `{uid}` banned.")
        try:
            await context.bot.send_message(uid, "🚫 *You have been banned.*", parse_mode="Markdown")
        except:
            pass
    else:
        await query.edit_message_text("❌ Failed to ban.")

async def admin_running(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    procs = user_manager.get_all_processes()
    running = [p for p in procs if p["status"] == "running"]
    if not running:
        await query.edit_message_text("No running processes.")
        return
    text = "🖥️ *Running Scripts*\n\n"
    for p in running:
        text += f"• {p['filename']} (PID {p['pid']}) – User `{p['user_id']}`\n"
    await query.edit_message_text(text, parse_mode="Markdown")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    total = len(user_manager.users)
    approved = len(user_manager.get_approved_users())
    pending = len(user_manager.get_pending_requests())
    banned = len(user_manager.get_banned_users())
    running = len([p for p in user_manager.get_all_processes() if p["status"] == "running"])
    text = (
        f"📊 *Statistics*\n"
        f"👥 Total: {total}\n"
        f"✅ Approved: {approved}\n"
        f"⏳ Pending: {pending}\n"
        f"🚫 Banned: {banned}\n"
        f"🖥️ Running: {running}"
    )
    await query.edit_message_text(text, parse_mode="Markdown")

async def admin_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    banned = user_manager.get_banned_users()
    if not banned:
        await query.edit_message_text("No banned users.")
        return
    keyboard = []
    for u in banned:
        uid = u['user_id']
        keyboard.append([InlineKeyboardButton(f"Unban {u['name']}", callback_data=f"unban_{uid}")])
    await query.edit_message_text(
        "🚫 *Banned Users*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    uid = int(query.data.split("_")[1])
    if user_manager.unban_user(uid):
        await query.edit_message_text(f"✅ User `{uid}` unbanned.")
    else:
        await query.edit_message_text("❌ Failed to unban.")

async def admin_telemetry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    tele = user_manager.telemetry
    if not tele:
        await query.edit_message_text("No telemetry data yet.")
        return
    text = "📈 *Global Telemetry*\n\n"
    total_runs = sum(t["runs"] for t in tele.values())
    total_success = sum(t["success"] for t in tele.values())
    total_fail = sum(t["fail"] for t in tele.values())
    total_bad = sum(t["bad"] for t in tele.values())
    text += f"Total Runs: {total_runs}\n✅ Success: {total_success}\n❌ Fail: {total_fail}\n💀 Bad: {total_bad}"
    await query.edit_message_text(text, parse_mode="Markdown")

async def admin_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Not admin.")
        return
    # Clean logs older than 7 days
    seven_days_ago = datetime.now() - timedelta(days=7)
    for user_id in user_manager.users:
        ws = user_manager.get_workspace(int(user_id))
        log_dir = ws / "logs"
        if log_dir.exists():
            for log_file in log_dir.glob("*.log"):
                if datetime.fromtimestamp(log_file.stat().st_mtime) < seven_days_ago:
                    log_file.unlink()
    await query.edit_message_text("🧹 Cleanup completed. Old logs removed.")

# ---------- ERROR HANDLER ----------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update {update} caused error {context.error}")
    try:
        await update.message.reply_text("⚠️ Internal error. Try again.")
    except:
        pass

# ---------- FLASK HEALTH ----------
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/api/')
@flask_app.route('/api/healthz')
def health():
    return jsonify({"status": "ok", "service": "telegram-bot"})

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ---------- MAIN ----------
def main():
    Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(check_join, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))
    application.add_handler(CallbackQueryHandler(upload_start, pattern="^upload$"))
    application.add_handler(CallbackQueryHandler(terminal_start, pattern="^terminal$"))
    application.add_handler(CallbackQueryHandler(my_scripts, pattern="^my_scripts$"))
    application.add_handler(CallbackQueryHandler(my_stats, pattern="^my_stats$"))
    application.add_handler(CallbackQueryHandler(view_script, pattern="^view_script_"))
    application.add_handler(CallbackQueryHandler(run_script_callback, pattern="^run_script_"))
    application.add_handler(CallbackQueryHandler(view_log, pattern="^view_log_"))
    application.add_handler(CallbackQueryHandler(view_logs, pattern="^logs$"))
    application.add_handler(CallbackQueryHandler(stop_script, pattern="^stop$"))
    application.add_handler(CallbackQueryHandler(stop_proc_callback, pattern="^stop_proc_"))
    application.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_users, pattern="^admin_users$"))
    application.add_handler(CallbackQueryHandler(admin_pending, pattern="^admin_pending$"))
    application.add_handler(CallbackQueryHandler(pending_action, pattern="^pending_"))
    application.add_handler(CallbackQueryHandler(approve_user_callback, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(ban_user_callback, pattern="^ban_"))
    application.add_handler(CallbackQueryHandler(admin_running, pattern="^admin_running$"))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    application.add_handler(CallbackQueryHandler(admin_banned, pattern="^admin_banned$"))
    application.add_handler(CallbackQueryHandler(unban_callback, pattern="^unban_"))
    application.add_handler(CallbackQueryHandler(admin_telemetry, pattern="^admin_telemetry$"))
    application.add_handler(CallbackQueryHandler(admin_cleanup, pattern="^admin_cleanup$"))

    upload_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(upload_start, pattern="^upload$")],
        states={UPLOAD_WAIT: [MessageHandler(filters.Document.ALL, upload_receive)]},
        fallbacks=[CommandHandler("cancel", upload_cancel)],
    )
    application.add_handler(upload_conv)

    terminal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(terminal_start, pattern="^terminal$")],
        states={TERMINAL_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, terminal_handle)]},
        fallbacks=[CommandHandler("cancel", terminal_cancel)],
    )
    application.add_handler(terminal_conv)

    application.add_error_handler(error_handler)

    # Auto-cleanup every 24 hours
    async def scheduled_cleanup():
        while True:
            await asyncio.sleep(86400)
            seven_days_ago = datetime.now() - timedelta(days=7)
            for user_id in user_manager.users:
                ws = user_manager.get_workspace(int(user_id))
                log_dir = ws / "logs"
                if log_dir.exists():
                    for log_file in log_dir.glob("*.log"):
                        if datetime.fromtimestamp(log_file.stat().st_mtime) < seven_days_ago:
                            log_file.unlink()
    asyncio.create_task(scheduled_cleanup())

    application.run_polling()

if __name__ == "__main__":
    main()