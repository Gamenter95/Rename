import asyncio
import math
import os
import re
import shutil
import time
import sqlite3
import json
import subprocess
from collections import deque, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Any
from datetime import datetime, timedelta

from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import RPCError, FloodWait, ChannelInvalid, ChatAdminRequired

# ---------- Config & setup ----------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "7226563741:AAHguEi2PTXbN_ceRPesF1TghTfBXaHpbXA"  # fallback; prefer .env
)

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("Please set API_ID, API_HASH, and BOT_TOKEN in .env")

WORKDIR = Path("work")
THUMBDIR = WORKDIR / "thumbs"
TEMPDIR = WORKDIR / "temp"
OUTDIR = WORKDIR / "out"
for d in (THUMBDIR, TEMPDIR, OUTDIR):
    d.mkdir(parents=True, exist_ok=True)

# Process up to 5 files concurrently (global) - increased for better speed
CONCURRENCY = 5
semaphore = asyncio.Semaphore(CONCURRENCY)

# Per-chat format templates and thumbnails
user_format: Dict[int, str] = defaultdict(
    lambda: "{filename}"  # default = keep original name (sans extension)
)
user_thumbnail: Dict[int, Path] = {}  # chat_id -> path to image

# Per-chat media type and mode settings
user_media_type: Dict[int, str] = defaultdict(lambda: "document")  # "video" or "document"
user_mode: Dict[int, str] = defaultdict(lambda: "file")  # "file" or "caption"

# Per-chat caption format
user_caption_format: Dict[int, str] = defaultdict(lambda: "{file_name}")  # default = just filename

# Per-chat dump channel
user_dump_channel: Dict[int, int] = {}  # chat_id -> dump_channel_id

# Per-chat metadata settings
user_metadata_enabled: Dict[int, bool] = defaultdict(lambda: False)  # chat_id -> enabled status
user_metadata: Dict[int, Dict[str, str]] = defaultdict(lambda: {
    "title": "Encoded By @Weoo_Animes",
    "author": "@Weoo_Animes", 
    "artist": "@Weoo_Animes",
    "audio": "@Weoo_Animes",
    "subtitle": "@Weoo_Animes",
    "video": "For More @Weoo_Animes"
})  # chat_id -> metadata dict

# Admin settings
ADMIN_ID = 6186511950
admin_dump_channel: Optional[int] = None  # Global admin dump channel
admin_log_channel: Optional[int] = None  # Global admin log channel
admin_list: set = {ADMIN_ID}  # Set of admin user IDs
force_sub_channels: Dict[int, str] = {}  # channel_id -> channel_name/username
banned_users: set = set()  # Set of banned user IDs

# Global queue of tasks (FIFO). Each task = one media message to process.
task_queue: "deque[Message]" = deque()

# Flag to keep a single dispatcher loop alive
dispatcher_started = False

# Temporary storage for broadcast data
broadcast_data: Dict[int, Dict[str, Any]] = {}

# Database initialization
DB_PATH = WORKDIR / "bot_stats.db"

def init_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            files_renamed INTEGER DEFAULT 0,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            user_id INTEGER,
            date TEXT,
            files_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    ''')
    conn.commit()
    conn.close()

def update_user_stats(user_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Update overall stats
    cursor.execute('''
        INSERT OR REPLACE INTO user_stats (user_id, username, first_name, files_renamed, last_activity)
        VALUES (?, ?, ?, COALESCE((SELECT files_renamed FROM user_stats WHERE user_id = ?), 0) + 1, CURRENT_TIMESTAMP)
    ''', (user_id, username, first_name, user_id))
    
    # Update daily stats
    cursor.execute('''
        INSERT OR REPLACE INTO daily_stats (user_id, date, files_count)
        VALUES (?, ?, COALESCE((SELECT files_count FROM daily_stats WHERE user_id = ? AND date = ?), 0) + 1)
    ''', (user_id, today, user_id, today))
    
    conn.commit()
    conn.close()

# Initialize database on startup
init_database()

# Admin helper functions
def is_admin(user_id: int) -> bool:
    return user_id in admin_list

def is_banned(user_id: int) -> bool:
    return user_id in banned_users

async def log_to_admin(message: str):
    """Send log message to admin log channel"""
    if admin_log_channel:
        try:
            await app.send_message(admin_log_channel, f"📊 **Log:** {message}")
        except Exception as e:
            print(f"Failed to send log: {e}")

async def send_error_log(error: str, user_id: int = None):
    """Send error log to admin channel"""
    if admin_log_channel:
        try:
            user_info = f" | User: {user_id}" if user_id else ""
            await app.send_message(admin_log_channel, f"❌ **Error:** {error}{user_info}")
        except Exception as e:
            print(f"Failed to send error log: {e}")

# ---------- Helpers ----------
def sanitize_filename(name: str) -> str:
    # Remove illegal filesystem characters
    return re.sub(r'[\\/:*?"<>|\n\r]+', " ", name).strip()

def human_size(num_bytes: float) -> str:
    if num_bytes is None:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    if num_bytes < 1:
        return f"{num_bytes:.0f} B"
    power = min(int(math.log(num_bytes, 1024)), len(units) - 1)
    value = num_bytes / (1024 ** power)
    if value >= 100:
        return f"{value:.0f} {units[power]}"
    elif value >= 10:
        return f"{value:.1f} {units[power]}"
    else:
        return f"{value:.2f} {units[power]}"

def eta_text(done: int, total: Optional[int], speed_bps: float) -> str:
    if not total or speed_bps <= 0:
        return "—"
    remain = max(total - done, 0)
    secs = remain / speed_bps if speed_bps else 0
    if secs < 1:
        return "1s"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def extract_variables_from_filename(filename: str) -> Dict[str, str]:
    """
    Extract variables from filename using common patterns.
    Example: "One Piece S01E12 [1080p] [Dual].mkv"
    """
    out = {}
    if not filename:
        return out
    
    # Remove extension
    name = Path(filename).stem
    
    # Extract season (S01, Season 1, etc.)
    season_match = re.search(r'[Ss](?:eason\s*)?(\d+)', name)
    if season_match:
        out['season'] = season_match.group(1)
    
    # Extract episode (E12, Episode 12, etc.)
    episode_match = re.search(r'[Ee](?:pisode\s*)?(\d+)', name)
    if episode_match:
        out['episode'] = episode_match.group(1)
    
    # Extract quality ([1080p], [720p], etc.)
    quality_match = re.search(r'\[?(\d+p)\]?', name)
    if quality_match:
        out['quality'] = quality_match.group(1)
    
    # Extract chapter (Ch 12, Chapter 12, etc.)
    chapter_match = re.search(r'[Cc](?:hapter\s*|h\s*)(\d+)', name)
    if chapter_match:
        out['chapter'] = chapter_match.group(1)
    
    # Extract title (everything before season/episode info)
    title_match = re.search(r'^([^[S]+?)(?:\s*[Ss]\d+|\s*\[|$)', name)
    if title_match:
        title = title_match.group(1).strip()
        # Clean up common separators
        title = re.sub(r'\s*[-_]\s*$', '', title)
        if title:
            out['title'] = title
    
    return out

def render_filename(fmt: str, base_vars: Dict[str, Any], ext: str) -> str:
    # allow {filename}, {episode}, {season}, {chapter}, {quality} etc.
    class SafeDict(dict):
        def __missing__(self, key):
            return ""  # drop unknown keys instead of KeyError

    core = fmt.format_map(SafeDict(base_vars))
    core = sanitize_filename(core)
    if not core:
        core = "file"
    # ensure extension
    if ext and not core.lower().endswith(ext.lower()):
        core = f"{core}{ext}"
    return core

def media_extension(msg: Message) -> str:
    d = msg.document or msg.video or msg.audio or msg.voice or msg.animation
    if d and d.file_name and "." in d.file_name:
        return "." + d.file_name.split(".")[-1]
    # fallback by media type
    if msg.video:
        return ".mp4"
    if msg.audio:
        return ".mp3"
    if msg.animation:
        return ".mp4"
    return ".bin"

def original_stem(msg: Message) -> str:
    d = msg.document or msg.video or msg.audio or msg.voice or msg.animation
    if d and d.file_name:
        return Path(d.file_name).stem
    return "file"

def extract_file_metadata(msg: Message, new_filename: str, file_path: Path) -> Dict[str, str]:
    """Extract metadata for caption variables"""
    metadata = {}
    
    # Get media object
    d = msg.document or msg.video or msg.audio or msg.voice or msg.animation
    
    # File name (after renaming)
    metadata['file_name'] = Path(new_filename).stem
    
    # File size
    if d and d.file_size:
        metadata['file_size'] = human_size(d.file_size)
    elif file_path.exists():
        metadata['file_size'] = human_size(file_path.stat().st_size)
    else:
        metadata['file_size'] = "Unknown"
    
    # Duration (for video/audio)
    if msg.video and msg.video.duration:
        duration_secs = msg.video.duration
        m, s = divmod(duration_secs, 60)
        h, m = divmod(m, 60)
        if h:
            metadata['duration'] = f"{h:02d}:{m:02d}:{s:02d}"
        else:
            metadata['duration'] = f"{m:02d}:{s:02d}"
    elif msg.audio and msg.audio.duration:
        duration_secs = msg.audio.duration
        m, s = divmod(duration_secs, 60)
        h, m = divmod(m, 60)
        if h:
            metadata['duration'] = f"{h:02d}:{m:02d}:{s:02d}"
        else:
            metadata['duration'] = f"{m:02d}:{s:02d}"
    else:
        metadata['duration'] = "N/A"
    
    # Original filename
    if d and d.file_name:
        metadata['original_name'] = Path(d.file_name).stem
    else:
        metadata['original_name'] = "file"
    
    # File extension
    metadata['extension'] = Path(new_filename).suffix.lstrip('.')
    
    # MIME type
    if d and d.mime_type:
        metadata['mime_type'] = d.mime_type
    else:
        metadata['mime_type'] = "Unknown"
    
    return metadata

@dataclass
class ProgressCtx:
    start: float = field(default_factory=time.perf_counter)
    last_edit: float = 0.0
    # state to compute speed
    last_bytes: int = 0
    last_time: float = field(default_factory=time.perf_counter)

    def speed(self, current: int) -> float:
        now = time.perf_counter()
        delta_b = current - self.last_bytes
        delta_t = max(now - self.last_time, 1e-6)
        self.last_bytes = current
        self.last_time = now
        return max(delta_b / delta_t, 0.0)

async def update_status(msg: Message, status_msg: Message, phase: str,
                        current: int, total: Optional[int], ctx: ProgressCtx):
    # throttle edits to ~2 per second for better performance
    now = time.perf_counter()
    if now - ctx.last_edit < 0.5 and current != total:
        return
    ctx.last_edit = now

    spd = ctx.speed(current)
    pct = (current / total * 100) if total else 0
    bar_len = 20
    filled = int((pct / 100) * bar_len) if total else int((current % bar_len))
    bar = "█" * filled + "·" * (bar_len - filled)
    text = (
        f"**{phase}**\n"
        f"`[{bar}]` {pct:5.1f}%\n"
        f"**Done:** {human_size(current)}"
    )
    if total:
        text += f" / {human_size(total)}"
    text += f"\n**Speed:** {human_size(spd)}/s"
    text += f"\n**ETA:** {eta_text(current, total, spd)}"
    try:
        await status_msg.edit_text(
            text,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except RPCError:
        pass

# ---------- Bot ----------
app = Client(
    "auto_rename_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(WORKDIR),
    parse_mode=enums.ParseMode.MARKDOWN,
)

# ---------- Inline Keyboards ----------
def get_main_keyboard(chat_id: int):
    keyboard = [
        [
            InlineKeyboardButton("Help", callback_data=f"help_{chat_id}"),
            InlineKeyboardButton("About", callback_data=f"about_{chat_id}"),
        ],
        [
            InlineKeyboardButton("Support", url="https://t.me/WeooBotsChat"),
            InlineKeyboardButton("Dev", url="https://t.me/WeooBots"),
        ],
        [InlineKeyboardButton("Close", callback_data=f"close_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_keyboard(chat_id: int):
    keyboard = [
        [
            InlineKeyboardButton("« Back", callback_data=f"back_{chat_id}"),
            InlineKeyboardButton("Close", callback_data=f"close_{chat_id}"),
        ],
        [
            InlineKeyboardButton("Support", url="https://t.me/WeooBotsChat"),
            InlineKeyboardButton("Dev", url="https://t.me/WeooBots"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_media_type_keyboard(chat_id: int):
    current = user_media_type[chat_id]
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if current == 'video' else ''}Video", 
                callback_data=f"media_video_{chat_id}"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if current == 'document' else ''}Document", 
                callback_data=f"media_document_{chat_id}"
            ),
        ],
        [InlineKeyboardButton("« Back", callback_data=f"back_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_mode_keyboard(chat_id: int):
    current = user_mode[chat_id]
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if current == 'file' else ''}File", 
                callback_data=f"mode_file_{chat_id}"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if current == 'caption' else ''}Caption", 
                callback_data=f"mode_caption_{chat_id}"
            ),
        ],
        [InlineKeyboardButton("« Back", callback_data=f"back_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_leaderboard_keyboard(chat_id: int, period: str = "monthly", limit: int = 10):
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if period == 'daily' else ''}Daily", 
                callback_data=f"lb_daily_{limit}_{chat_id}"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if period == 'weekly' else ''}Weekly", 
                callback_data=f"lb_weekly_{limit}_{chat_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅ ' if period == 'monthly' else ''}Monthly", 
                callback_data=f"lb_monthly_{limit}_{chat_id}"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if period == 'yearly' else ''}Yearly", 
                callback_data=f"lb_yearly_{limit}_{chat_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅ ' if limit == 10 else ''}Top 10", 
                callback_data=f"lb_{period}_10_{chat_id}"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if limit == 15 else ''}Top 15", 
                callback_data=f"lb_{period}_15_{chat_id}"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if limit == 20 else ''}Top 20", 
                callback_data=f"lb_{period}_20_{chat_id}"
            ),
        ],
        [InlineKeyboardButton("« Back", callback_data=f"back_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_panel_keyboard(chat_id: int):
    keyboard = [
        [
            InlineKeyboardButton("👥 Admins", callback_data=f"admin_admins_{chat_id}"),
            InlineKeyboardButton("📢 Force Sub", callback_data=f"admin_forcesub_{chat_id}"),
        ],
        [
            InlineKeyboardButton("📤 Admin Dump", callback_data=f"admin_dump_{chat_id}"),
            InlineKeyboardButton("📊 Log Channel", callback_data=f"admin_log_{chat_id}"),
        ],
        [
            InlineKeyboardButton("📈 Statistics", callback_data=f"admin_stats_{chat_id}"),
            InlineKeyboardButton("🗄️ Database", callback_data=f"admin_db_{chat_id}"),
        ],
        [InlineKeyboardButton("« Back", callback_data=f"back_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_metadata_keyboard(chat_id: int):
    enabled = user_metadata_enabled[chat_id]
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if enabled else ''}On", 
                callback_data=f"meta_on_{chat_id}"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if not enabled else ''}Off", 
                callback_data=f"meta_off_{chat_id}"
            ),
        ],
        [InlineKeyboardButton("How to set metadata", callback_data=f"meta_help_{chat_id}")],
        [InlineKeyboardButton("« Back", callback_data=f"back_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------- Message Texts ----------
WELCOME_TEXT = (
    "**Hᴇʏ, {first_name}!**\n\n"
    "**Wᴇʟᴄᴏᴍᴇ ᴛᴏ ᴛʜᴇ ᴍᴏꜱᴛ ᴀᴅᴠᴀɴᴄᴇᴅ Rᴇɴᴀᴍᴇ Bᴏᴛ!**\n\n"
    "» **ᴡɪᴛʜ ᴍʏ ᴘᴏᴡᴇʀꜰᴜʟ ꜰᴇᴀᴛᴜʀᴇꜱ, ʏᴏᴜ ᴄᴀɴ:**\n\n"
    "• **Aᴜᴛᴏʀᴇɴᴀᴍᴇ ꜰɪʟᴇꜱ ᴡɪᴛʜ ᴄᴜꜱᴛᴏᴍ ꜰᴏʀᴍᴀᴛꜱ.**\n"
    "• **Aᴅᴅ ᴄᴀᴘᴛɪᴏɴꜱ ᴏʀ ꜱᴇʟᴇᴄᴛ ᴛʜᴜᴍʙɴᴀɪʟꜱ.**\n"
    "• **Pʀᴏᴄᴇꜱꜱ ꜰɪʟᴇꜱ ꜱᴇǫᴜᴇɴᴛɪᴀʟʟʏ ꜰᴏʀ ꜱᴍᴏᴏᴛʜ ᴡᴏʀᴋꜰʟᴏᴡ.**\n\n"
    "🔹 **ʀᴇᴀᴅʏ ᴛᴏ ʙᴇɢɪɴ? ᴊᴜꜱᴛ ꜱᴇɴᴅ ᴍᴇ ᴀɴʏ ꜰɪʟᴇ!**\n"
    "🔹 **ꜰᴏʀ ᴅᴇᴛᴀɪʟꜱ, ᴛᴀᴘ ᴛʜᴇ ʜᴇʟᴘ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ.**"
)

HELP_TEXT = (
    "**📚 Hᴇʟᴘ & Cᴏᴍᴍᴀɴᴅꜱ**\n\n"

    "**🎯 Mᴀɪɴ Cᴏᴍᴍᴀɴᴅꜱ:**\n"
    "➥ `/format <pattern>` - Sᴇᴛ ʀᴇɴᴀᴍɪɴɢ ꜰᴏʀᴍᴀᴛ\n"
    "➥ `/getformat` - Vɪᴇᴡ ᴄᴜʀʀᴇɴᴛ ꜰᴏʀᴍᴀᴛ\n"
    "➥ `/caption <pattern>` - Sᴇᴛ ᴄᴀᴘᴛɪᴏɴ ꜰᴏʀᴍᴀᴛ\n"
    "➥ `/getcp` - Vɪᴇᴡ ᴄᴜʀʀᴇɴᴛ ᴄᴀᴘᴛɪᴏɴ ꜰᴏʀᴍᴀᴛ\n"
    "➥ `/delcp` - Dᴇʟᴇᴛᴇ ᴄᴀᴘᴛɪᴏɴ ꜰᴏʀᴍᴀᴛ\n\n"

    "**🖼️ Tʜᴜᴍʙɴᴀɪʟ Cᴏᴍᴍᴀɴᴅꜱ:**\n"
    "➥ `/addpic` - Hᴏᴡ ᴛᴏ ᴀᴅᴅ ᴛʜᴜᴍʙɴᴀɪʟ\n"
    "➥ `/delpic` - Dᴇʟᴇᴛᴇ ꜱᴀᴠᴇᴅ ᴛʜᴜᴍʙɴᴀɪʟ\n"
    "➥ `/checkpic` - Vɪᴇᴡ ᴄᴜʀʀᴇɴᴛ ᴛʜᴜᴍʙɴᴀɪʟ\n"
    "➥ `/seepic` - Vɪᴇᴡ ᴄᴜʀʀᴇɴᴛ ᴛʜᴜᴍʙɴᴀɪʟ\n"
    "➥ **Sᴇɴᴅ Pʜᴏᴛᴏ** - Aᴜᴛᴏ ꜱᴀᴠᴇ ᴀꜱ ᴛʜᴜᴍʙɴᴀɪʟ\n\n"

    "**📤 Dᴜᴍᴘ Cʜᴀɴɴᴇʟ:**\n"
    "➥ `/setdump <channel_id>` - Sᴇᴛ ᴅᴜᴍᴘ ᴄʜᴀɴɴᴇʟ\n"
    "➥ `/setdump` - Vɪᴇᴡ ᴄᴜʀʀᴇɴᴛ ᴅᴜᴍᴘ ᴄʜᴀɴɴᴇʟ\n"
    "➥ `/deldump` - Rᴇᴍᴏᴠᴇ ᴅᴜᴍᴘ ᴄʜᴀɴɴᴇʟ\n"
    "➥ `/seedump` - Sᴇᴇ ᴄᴜʀʀᴇɴᴛ ᴅᴜᴍᴘ ᴄʜᴀɴɴᴇʟ\n\n"

    "**🏷️ Mᴇᴛᴀᴅᴀᴛᴀ Cᴏᴍᴍᴀɴᴅꜱ:**\n"
    "➥ `/metadata` - Mᴀɴᴀɢᴇ ᴍᴇᴛᴀᴅᴀᴛᴀ ꜱᴇᴛᴛɪɴɢꜱ\n"
    "➥ `/settitle <text>` - Sᴇᴛ ᴛɪᴛʟᴇ ᴍᴇᴛᴀᴅᴀᴛᴀ\n"
    "➥ `/setauthor <text>` - Sᴇᴛ ᴀᴜᴛʜᴏʀ ᴍᴇᴛᴀᴅᴀᴛᴀ\n"
    "➥ `/setartist <text>` - Sᴇᴛ ᴀʀᴛɪꜱᴛ ᴍᴇᴛᴀᴅᴀᴛᴀ\n"
    "➥ `/setaudio <text>` - Sᴇᴛ ᴀᴜᴅɪᴏ ᴍᴇᴛᴀᴅᴀᴛᴀ\n"
    "➥ `/setsubtitle <text>` - Sᴇᴛ ꜱᴜʙᴛɪᴛʟᴇ ᴍᴇᴛᴀᴅᴀᴛᴀ\n"
    "➥ `/setvideo <text>` - Sᴇᴛ ᴠɪᴅᴇᴏ ᴍᴇᴛᴀᴅᴀᴛᴀ\n\n"

    "**ℹ️ Iɴꜰᴏʀᴍᴀᴛɪᴏɴ:**\n"
    "➥ `/extract` - Exᴛʀᴀᴄᴛ ᴠᴀʀɪᴀʙʟᴇꜱ (ʀᴇᴘʟʏ ᴛᴏ ᴍᴇᴅɪᴀ)\n"
    "➥ `/pic` - Gᴇᴛ ꜰɪʟᴇ ᴛʜᴜᴍʙɴᴀɪʟ (ʀᴇᴘʟʏ ᴛᴏ ᴍᴇᴅɪᴀ)\n"
    "➥ `/leaderboard` - Vɪᴇᴡ ᴛᴏᴘ ᴜꜱᴇʀꜱ\n"
    "➥ `/queue` - Cʜᴇᴄᴋ ᴘʀᴏᴄᴇꜱꜱɪɴɢ Qᴜᴇᴜᴇ\n"
    "➥ `/clear` - Cʟᴇᴀʀ ʏᴏᴜʀ ꜰɪʟᴇꜱ ꜰʀᴏᴍ Qᴜᴇᴜᴇ\n\n"

    "**⚙️ Sᴇᴛᴛɪɴɢꜱ:**\n"
    "➥ `/media_type` - Sᴇᴛ ᴜᴘʟᴏᴀᴅ ᴛʏᴘᴇ (Video/Document)\n"
    "➥ `/mode` - Sᴇᴛ ᴅᴀᴛᴀ ꜱᴏᴜʀᴄᴇ (File/Caption)\n\n"

    "**👑 Aᴅᴍɪɴ Oɴʟʏ:**\n"
    "➥ `/panel` - Aᴅᴍɪɴ ᴘᴀɴᴇʟ\n"
    "➥ `/ban <user_id>` - Bᴀɴ ᴜꜱᴇʀ\n"
    "➥ `/unban <user_id>` - Uɴʙᴀɴ ᴜꜱᴇʀ\n"
    "➥ `/bans` - Vɪᴇᴡ ʙᴀɴɴᴇᴅ ᴜꜱᴇʀꜱ\n"
    "➥ `/broadcast <message>` - Sᴇɴᴅ ᴛᴏ ᴀʟʟ ᴜꜱᴇʀꜱ\n"
    "➥ `/admins` - Mᴀɴᴀɢᴇ ᴀᴅᴍɪɴꜱ\n"
    "➥ `/forcesub` - Mᴀɴᴀɢᴇ ꜰᴏʀᴄᴇ ꜱᴜʙ\n"
    "➥ `/admindump <channel_id>` - Sᴇᴛ ᴀᴅᴍɪɴ ᴅᴜᴍᴘ\n"
    "➥ `/log <channel_id>` - Sᴇᴛ ʟᴏɢ ᴄʜᴀɴɴᴇʟ\n\n"

    "**📖 Hᴏᴡ ᴛᴏ Uꜱᴇ:**\n"
    "1. Sᴇᴛ ʀᴇɴᴀᴍɪɴɢ ꜰᴏʀᴍᴀᴛ ᴡɪᴛʜ `/format`\n"
    "2. Oᴘᴛɪᴏɴᴀʟʟʏ ᴀᴅᴅ ᴛʜᴜᴍʙɴᴀɪʟ\n"
    "3. Sᴇɴᴅ ᴀɴʏ ᴍᴇᴅɪᴀ ꜰɪʟᴇ ᴛᴏ ʀᴇɴᴀᴍᴇ\n"
    "4. Fɪʟᴇꜱ ᴀʀᴇ ᴘʀᴏᴄᴇꜱꜱᴇᴅ ɪɴ Qᴜᴇᴜᴇ ᴏʀᴅᴇʀ\n\n"

    "**💡 Fᴏʀ ᴍᴏʀᴇ ʜᴇʟᴘ:** Join our support group!"
)

ABOUT_TEXT = (
    "**Aʙᴏᴜᴛ Tʜɪꜱ Bᴏᴛ**\n\n"
    "Welcome to the Auto Rename Bot – your ultimate solution for renaming and organizing files effortlessly!\n\n"

    "**Kᴇʏ Fᴇᴀᴛᴜʀᴇꜱ:**\n\n"
    "• **Custom Formats:** Set personalized renaming formats for your files.\n"
    "• **Metadata Support:** Add or modify metadata for enhanced organization.\n"
    "• **Caption Management:** Set, check, or delete captions with ease.\n"
    "• **Thumbnails:** Save, view, or replace thumbnails effortlessly.\n"
    "• **Dump Channel:** Directly store files in a custom dump channel.\n\n"

    "Whether you're managing videos, documents, or any other type of media, this bot offers a simple and efficient way to rename and organize your files with advanced customization.\n\n"

    "**Wʜʏ Cʜᴏᴏꜱᴇ Tʜɪꜱ Bᴏᴛ?**\n\n"
    "• Intuitive commands for a smooth user experience.\n"
    "• Optimized for handling large batches of files.\n"
    "• Fully customizable to fit your needs.\n\n"

    "Start using the bot today and make file renaming a breeze!"
)

IMAGE_URL = "https://envs.sh/inH.jpg"

# ---------- Handlers ----------
@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    if is_banned(m.from_user.id):
        return await m.reply_text(
            "🚫 **You are banned from using this bot.**\n\n"
            "Contact an administrator if you think this is a mistake."
        )
    
    first_name = m.from_user.first_name
    await m.reply_photo(
        photo=IMAGE_URL,
        caption=WELCOME_TEXT.format(first_name=first_name),
        reply_markup=get_main_keyboard(m.chat.id)
    )

@app.on_callback_query()
async def handle_callbacks(client, callback_query):
    data = callback_query.data
    chat_id = callback_query.message.chat.id
    
    if data.startswith("help_") or data == "help":
        await callback_query.message.edit_caption(
            caption=HELP_TEXT,
            reply_markup=get_back_keyboard(chat_id)
        )
        await callback_query.answer("📖 Help section")
    
    elif data.startswith("about_") or data == "about":
        await callback_query.message.edit_caption(
            caption=ABOUT_TEXT,
            reply_markup=get_back_keyboard(chat_id)
        )
        await callback_query.answer()
    
    elif data.startswith("back_") or data == "back":
        first_name = callback_query.from_user.first_name
        await callback_query.message.edit_caption(
            caption=WELCOME_TEXT.format(first_name=first_name),
            reply_markup=get_main_keyboard(chat_id)
        )
        await callback_query.answer()
    
    elif data.startswith("close_") or data == "close":
        await callback_query.message.delete()
        await callback_query.answer()
    
    elif data.startswith("media_video_"):
        chat_id = int(data.split("_")[-1])
        user_media_type[chat_id] = "video"
        await callback_query.message.edit_text(
            "📹 **Media Type Settings**\n\n"
            f"**Current:** Video\n\n"
            "Files will be uploaded as videos when possible.",
            reply_markup=get_media_type_keyboard(chat_id)
        )
        await callback_query.answer("✅ Set to Video")
    
    elif data.startswith("media_document_"):
        chat_id = int(data.split("_")[-1])
        user_media_type[chat_id] = "document"
        await callback_query.message.edit_text(
            "📄 **Media Type Settings**\n\n"
            f"**Current:** Document\n\n"
            "Files will be uploaded as documents.",
            reply_markup=get_media_type_keyboard(chat_id)
        )
        await callback_query.answer("✅ Set to Document")
    
    elif data.startswith("mode_file_"):
        chat_id = int(data.split("_")[-1])
        user_mode[chat_id] = "file"
        await callback_query.message.edit_text(
            "📁 **Mode Settings**\n\n"
            f"**Current:** File\n\n"
            "Format data will be extracted from filename.",
            reply_markup=get_mode_keyboard(chat_id)
        )
        await callback_query.answer("✅ Set to File mode")
    
    elif data.startswith("mode_caption_"):
        chat_id = int(data.split("_")[-1])
        user_mode[chat_id] = "caption"
        await callback_query.message.edit_text(
            "💬 **Mode Settings**\n\n"
            f"**Current:** Caption\n\n"
            "Format data will be extracted from caption.",
            reply_markup=get_mode_keyboard(chat_id)
        )
        await callback_query.answer("✅ Set to Caption mode")
    
    elif data.startswith("lb_"):
        parts = data.split("_")
        period = parts[1]
        limit = int(parts[2])
        chat_id = int(parts[3])
        
        leaderboard_text = await get_leaderboard_text(period, limit)
        await callback_query.message.edit_text(
            leaderboard_text,
            reply_markup=get_leaderboard_keyboard(chat_id, period, limit)
        )
        await callback_query.answer(f"✅ Showing {period} top {limit}")
    
    elif data.startswith("meta_"):
        action = data.split("_")[1]
        chat_id = int(data.split("_")[-1])
        
        if action == "on":
            user_metadata_enabled[chat_id] = True
            status = "On"
            await callback_query.answer("✅ Metadata enabled")
        elif action == "off":
            user_metadata_enabled[chat_id] = False
            status = "Off"
            await callback_query.answer("✅ Metadata disabled")
        elif action == "help":
            help_text = (
                "**ᴍᴀɴᴀɢɪɴɢ ᴍᴇᴛᴀᴅᴀᴛᴀ ғᴏʀ ʏᴏᴜʀ ᴠɪᴅᴇᴏs ᴀɴᴅ ғɪʟᴇs**\n\n"
                "**ᴠᴀʀɪᴏᴜꜱ ᴍᴇᴛᴀᴅᴀᴛᴀ:**\n\n"
                "- **ᴛɪᴛʟᴇ:** Descriptive title of the media.\n"
                "- **ᴀᴜᴛʜᴏʀ:** The creator or owner of the media.\n"
                "- **ᴀʀᴛɪꜱᴛ:** The artist associated with the media.\n"
                "- **ᴀᴜᴅɪᴏ:** Title or description of audio content.\n"
                "- **ꜱᴜʙᴛɪᴛʟᴇ:** Title of subtitle content.\n"
                "- **ᴠɪᴅᴇᴏ:** Title or description of video content.\n\n"
                "**ᴄᴏᴍᴍᴀɴᴅꜱ ᴛᴏ ᴛᴜʀɴ ᴏɴ ᴏғғ ᴍᴇᴛᴀᴅᴀᴛᴀ:**\n"
                "➜ `/metadata` - Turn on or off metadata.\n\n"
                "**ᴄᴏᴍᴍᴀɴᴅꜱ ᴛᴏ ꜱᴇᴛ ᴍᴇᴛᴀᴅᴀᴛᴀ:**\n\n"
                "➜ `/settitle` - Set a custom title of media.\n"
                "➜ `/setauthor` - Set the author.\n"
                "➜ `/setartist` - Set the artist.\n"
                "➜ `/setaudio` - Set audio title.\n"
                "➜ `/setsubtitle` - Set subtitle title.\n"
                "➜ `/setvideo` - Set video title.\n\n"
                "**ᴇxᴀᴍᴘʟᴇ:** `/settitle Your Title Here`\n\n"
                "ᴜꜱᴇ ᴛʜᴇꜱᴇ ᴄᴏᴍᴍᴀɴᴅꜱ ᴛᴏ ᴇɴʀɪᴄʜ ʏᴏᴜʀ ᴍᴇᴅɪᴀ ᴡɪᴛʜ ᴀᴅᴅɪᴛɪᴏɴᴀʟ ᴍᴇᴛᴀᴅᴀᴛᴀ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ!"
            )
            await callback_query.message.edit_text(
                help_text,
                reply_markup=get_back_keyboard(chat_id)
            )
            await callback_query.answer()
            return
        else:
            return
        
        # Update metadata display
        status = "On" if user_metadata_enabled[chat_id] else "Off"
        metadata = user_metadata[chat_id]
        
        text = (
            f"㊋ **Yᴏᴜʀ Mᴇᴛᴀᴅᴀᴛᴀ ɪꜱ ᴄᴜʀʀᴇɴᴛʟʏ:** {status}\n\n"
            f"◈ **Tɪᴛʟᴇ** ▹ {metadata['title']}\n"
            f"◈ **Aᴜᴛʜᴏʀ** ▹ {metadata['author']}\n"
            f"◈ **Aʀᴛɪꜱᴛ** ▹ {metadata['artist']}\n"
            f"◈ **Aᴜᴅɪᴏ** ▹ {metadata['audio']}\n"
            f"◈ **Sᴜʙᴛɪᴛʟᴇ** ▹ {metadata['subtitle']}\n"
            f"◈ **Vɪᴅᴇᴏ** ▹ {metadata['video']}"
        )
        
        await callback_query.message.edit_text(
            text,
            reply_markup=get_metadata_keyboard(chat_id)
        )
    
    elif data.startswith("broadcast_"):
        if not is_admin(callback_query.from_user.id):
            return await callback_query.answer("❌ Access denied")
        
        action = data.split("_")[1]
        msg_id = int(data.split("_")[2])
        
        if action == "confirm":
            if msg_id not in broadcast_data:
                return await callback_query.answer("❌ Broadcast data not found")
            
            broadcast_info = broadcast_data[msg_id]
            message = broadcast_info['message']
            admin_name = broadcast_info['admin_name']
            
            await callback_query.message.edit_text("📤 **Starting broadcast...**")
            
            # Get all users
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('SELECT DISTINCT user_id FROM user_stats')
                user_ids = [row[0] for row in cursor.fetchall()]
                conn.close()
            except Exception as e:
                await send_error_log(f"Broadcast users fetch error: {e}")
                return await callback_query.message.edit_text("❌ **Error fetching users.**")
            
            # Send broadcast
            success_count = 0
            failed_count = 0
            
            for user_id in user_ids:
                try:
                    await app.send_message(
                        user_id,
                        f"📢 **Broadcast Message**\n\n{message}\n\n─────────────────\n"
                        f"_This message was sent by bot administrators._"
                    )
                    success_count += 1
                    await asyncio.sleep(0.1)  # Rate limiting
                except Exception:
                    failed_count += 1
            
            # Clean up broadcast data
            del broadcast_data[msg_id]
            
            result_text = (
                f"✅ **Broadcast Complete!**\n\n"
                f"**Successfully sent:** {success_count} users\n"
                f"**Failed:** {failed_count} users\n"
                f"**Total:** {len(user_ids)} users"
            )
            
            await callback_query.message.edit_text(result_text)
            await log_to_admin(f"Broadcast sent by {admin_name}: {success_count} success, {failed_count} failed")
            
        elif action == "cancel":
            if msg_id in broadcast_data:
                del broadcast_data[msg_id]
            await callback_query.message.edit_text("❌ **Broadcast cancelled.**")
            await callback_query.answer("Broadcast cancelled")
    
    elif data.startswith("clear_"):
        if not is_admin(callback_query.from_user.id):
            return await callback_query.answer("❌ Access denied")
        
        action = data.split("_")[1]
        
        if action == "confirm":
            queue_length = len(task_queue)
            task_queue.clear()
            
            await callback_query.message.edit_text(
                f"✅ **Queue cleared successfully!**\n\n"
                f"**Removed:** {queue_length} files from queue"
            )
            await log_to_admin(f"Queue cleared by admin {callback_query.from_user.first_name}: {queue_length} files removed")
            await callback_query.answer("Queue cleared")
            
        elif action == "cancel":
            await callback_query.message.edit_text("❌ **Queue clear cancelled.**")
            await callback_query.answer("Clear cancelled")
    
    elif data.startswith("admin_"):
        if not is_admin(callback_query.from_user.id):
            return await callback_query.answer("❌ Access denied")
        
        action = data.split("_")[1]
        chat_id = int(data.split("_")[-1])
        
        if action == "admins":
            text = "👥 **Admin Management**\n\n"
            for admin_id in admin_list:
                try:
                    user = await app.get_users(admin_id)
                    name = user.first_name or "Unknown"
                    text += f"• {name} (`{admin_id}`)\n"
                except:
                    text += f"• Unknown User (`{admin_id}`)\n"
            text += "\nUse `/admins add {user_id}` to add more admins."
            
        elif action == "forcesub":
            if force_sub_channels:
                text = "📢 **Force Subscribe Channels:**\n\n"
                for channel_id, channel_name in force_sub_channels.items():
                    text += f"• {channel_name} (`{channel_id}`)\n"
            else:
                text = "❌ **No force subscribe channels set**"
            text += "\n\nUse `/forcesub add {channel_id}` to add channels."
            
        elif action == "dump":
            if admin_dump_channel:
                try:
                    chat = await app.get_chat(admin_dump_channel)
                    text = f"📤 **Admin Dump Channel:**\n{chat.title} (`{admin_dump_channel}`)"
                except:
                    text = f"📤 **Admin Dump Channel:** `{admin_dump_channel}`"
            else:
                text = "❌ **No admin dump channel set**"
            text += "\n\nUse `/admindump {channel_id}` to set."
            
        elif action == "log":
            if admin_log_channel:
                try:
                    chat = await app.get_chat(admin_log_channel)
                    text = f"📊 **Log Channel:**\n{chat.title} (`{admin_log_channel}`)"
                except:
                    text = f"📊 **Log Channel:** `{admin_log_channel}`"
            else:
                text = "❌ **No log channel set**"
            text += "\n\nUse `/log {channel_id}` to set."
            
        elif action == "stats":
            stats = await get_admin_stats()
            text = (f"📈 **Bot Statistics:**\n\n"
                   f"• Total Users: {stats['total_users']}\n"
                   f"• Total Files: {stats['total_files']}\n"
                   f"• Active Today: {stats['active_today']}\n"
                   f"• Queue Length: {len(task_queue)}")
            
        elif action == "db":
            text = ("🗄️ **Database Info:**\n\n"
                   f"• Database Path: `{DB_PATH}`\n"
                   f"• Database Size: {DB_PATH.stat().st_size if DB_PATH.exists() else 0} bytes\n"
                   f"• Tables: user_stats, daily_stats")
        else:
            text = "Unknown admin action"
        
        await callback_query.message.edit_text(
            text,
            reply_markup=get_back_keyboard(chat_id)
        )
        await callback_query.answer()

@app.on_message(filters.command("getformat"))
async def cmd_getformat(_, m: Message):
    fmt = user_format[m.chat.id]
    await m.reply_text(f"**Your Current Format is**\n\n`{fmt}`")

@app.on_message(filters.command("addpic"))
async def cmd_addpic(_, m: Message):
    await m.reply_text(
        "**📸 Hᴏᴡ ᴛᴏ ᴀᴅᴅ ᴛʜᴜᴍʙɴᴀɪʟ:**\n\n"
        "Simply send me any photo and I'll automatically save it as your thumbnail!\n\n"
        "The thumbnail will be used for all your future file uploads."
    )

@app.on_message(filters.command("delpic"))
async def cmd_delpic(_, m: Message):
    path = user_thumbnail.pop(m.chat.id, None)
    if path and path.exists():
        try:
            path.unlink()
            await m.reply_text("🗑️ **Thumbnail deleted successfully!**\n\nYour future uploads will not have a custom thumbnail.")
        except Exception:
            await m.reply_text("❌ Failed to delete thumbnail file, but removed from memory.")
    else:
        await m.reply_text("❌ **No thumbnail found to delete.**\n\nYou can add one by sending any photo.")

@app.on_message(filters.command("format"))
async def cmd_format(_, m: Message):
    if len(m.command) < 2 and not m.text.strip().startswith("/format "):
        return await m.reply_text(
            "**Usage:** `/format <pattern>`\n\n"
            "**Available variables:**\n"
            "• `{title}` - anime/series title\n"
            "• `{season}` - season number\n"
            "• `{episode}` - episode number\n"
            "• `{quality}` - video resolution\n"
            "• `{chapter}` - chapter number\n"
            "• `{filename}` - original filename\n\n"
            "**Example:** `/format S{season} E{episode} - {title} [{quality}]`"
        )
    # everything after the first space
    pattern = m.text.split(" ", 1)[1].strip()
    if not pattern:
        return await m.reply_text("Please provide a pattern after /format.")
    # quick check: reject very long patterns
    if len(pattern) > 200:
        return await m.reply_text("Pattern too long (max 200 chars).")
    user_format[m.chat.id] = pattern
    await m.reply_text(
        "✅ **Format saved successfully!**\n\n"
        f"**Your new format:** `{pattern}`\n\n"
        "Now send any media file to see the new format in action!"
    )



@app.on_message(filters.command("media_type"))
async def cmd_media_type(_, m: Message):
    current = user_media_type[m.chat.id]
    await m.reply_text(
        "📹 **Media Type Settings**\n\n"
        f"**Current:** {current.title()}\n\n"
        "Choose how you want your files to be uploaded:",
        reply_markup=get_media_type_keyboard(m.chat.id)
    )

@app.on_message(filters.command("mode"))
async def cmd_mode(_, m: Message):
    current = user_mode[m.chat.id]
    await m.reply_text(
        "⚙️ **Mode Settings**\n\n"
        f"**Current:** {current.title()}\n\n"
        "Choose where to extract format data from:",
        reply_markup=get_mode_keyboard(m.chat.id)
    )

@app.on_message(filters.command("queue"))
async def cmd_queue(_, m: Message):
    await m.reply_text(f"Queue: **{len(task_queue)}** pending.\n"
                       f"Processing up to **{CONCURRENCY}** at a time.")

@app.on_message(filters.command("caption"))
async def cmd_caption(_, m: Message):
    if len(m.command) < 2 and not m.text.strip().startswith("/caption "):
        return await m.reply_text(
            "**Usage:** `/caption <pattern>`\n\n"
            "**Available variables:**\n"
            "• `{file_name}` - renamed filename (without extension)\n"
            "• `{file_size}` - file size in human readable format\n"
            "• `{duration}` - video/audio duration (HH:MM:SS)\n"
            "• `{original_name}` - original filename (without extension)\n"
            "• `{extension}` - file extension\n"
            "• `{mime_type}` - file MIME type\n\n"
            "**Example:** `/caption 📁 {file_name}\n💾 Size: {file_size}\n⏱️ Duration: {duration}`"
        )
    
    # everything after the first space
    pattern = m.text.split(" ", 1)[1].strip()
    if not pattern:
        return await m.reply_text("Please provide a caption pattern after /caption.")
    
    # quick check: reject very long patterns
    if len(pattern) > 1000:
        return await m.reply_text("Caption pattern too long (max 1000 chars).")
    
    user_caption_format[m.chat.id] = pattern
    await m.reply_text(
        "✅ **Caption format saved successfully!**\n\n"
        f"**Your new caption format:**\n`{pattern}`\n\n"
        "Now send any media file to see the new caption format in action!"
    )

@app.on_message(filters.command("getcp"))
async def cmd_getcp(_, m: Message):
    fmt = user_caption_format[m.chat.id]
    await m.reply_text(f"**Your Current Caption Format:**\n\n`{fmt}`")

@app.on_message(filters.command("delcp"))
async def cmd_delcp(_, m: Message):
    user_caption_format[m.chat.id] = "{file_name}"  # reset to default
    await m.reply_text(
        "🗑️ **Caption format deleted successfully!**\n\n"
        "Caption format has been reset to default: `{file_name}`"
    )

@app.on_message(filters.command("extract"))
async def cmd_extract(_, m: Message):
    if not m.reply_to_message:
        return await m.reply_text(
            "❌ **Please reply to a media file to extract information.**\n\n"
            "Usage: Reply to any media file with `/extract`"
        )
    
    replied_msg = m.reply_to_message
    if not (replied_msg.document or replied_msg.video or replied_msg.audio or replied_msg.animation or replied_msg.voice):
        return await m.reply_text("❌ **Please reply to a media file (document, video, audio, etc.)**")
    
    # Get filename
    d = replied_msg.document or replied_msg.video or replied_msg.audio or replied_msg.voice or replied_msg.animation
    filename = d.file_name if d and d.file_name else "Unknown"
    
    # Extract variables
    mode = user_mode[m.chat.id]
    if mode == "caption" and replied_msg.caption:
        vars_extracted = extract_variables_from_filename(replied_msg.caption)
        source = "caption"
        source_text = replied_msg.caption
    else:
        vars_extracted = extract_variables_from_filename(filename)
        source = "filename"
        source_text = filename
    
    # Format response
    response = f"📊 **Extracted Information**\n\n"
    response += f"**Source:** {source.title()}\n"
    response += f"**Text:** `{source_text}`\n\n"
    response += f"**Extracted Variables:**\n"
    
    if vars_extracted:
        for key, value in vars_extracted.items():
            response += f"• `{{{key}}}` → `{value}`\n"
    else:
        response += "• No variables found\n"
    
    response += f"\n**Available Variables:**\n"
    response += f"• `{{title}}` - Series/anime title\n"
    response += f"• `{{season}}` - Season number\n"
    response += f"• `{{episode}}` - Episode number\n"
    response += f"• `{{chapter}}` - Chapter number\n"
    response += f"• `{{quality}}` - Video quality\n"
    response += f"• `{{filename}}` - Original filename\n"
    
    await m.reply_text(response)

@app.on_message(filters.command("checkpic"))
async def cmd_checkpic(_, m: Message):
    thumb_path = user_thumbnail.get(m.chat.id)
    if thumb_path and thumb_path.exists():
        await m.reply_photo(
            photo=str(thumb_path),
            caption="🖼️ **Your current thumbnail**\n\nThis will be used for all your file uploads."
        )
    else:
        await m.reply_text(
            "❌ **No thumbnail set**\n\n"
            "Send any photo to set it as your thumbnail, or use `/addpic` for instructions."
        )

@app.on_message(filters.command("pic"))
async def cmd_pic(_, m: Message):
    if not m.reply_to_message:
        return await m.reply_text(
            "❌ **Please reply to a media file to get its thumbnail.**\n\n"
            "Usage: Reply to any media file with `/pic`"
        )
    
    replied_msg = m.reply_to_message
    if not (replied_msg.document or replied_msg.video or replied_msg.audio or replied_msg.animation):
        return await m.reply_text("❌ **Please reply to a media file that can have thumbnails**")
    
    status = await m.reply_text("🔍 Extracting thumbnail...")
    
    try:
        # Download the file temporarily to extract thumbnail
        temp_file = TEMPDIR / f"thumb_extract_{replied_msg.id}"
        await app.download_media(replied_msg, file_name=str(temp_file))
        
        # For videos, we can try to extract thumbnail using ffmpeg (if available)
        # For now, we'll check if the file itself has a thumbnail
        d = replied_msg.document or replied_msg.video or replied_msg.audio or replied_msg.animation
        
        if hasattr(d, 'thumbs') and d.thumbs:
            # Download the thumbnail
            thumb_file = TEMPDIR / f"extracted_thumb_{replied_msg.id}.jpg"
            await app.download_media(d.thumbs[0].file_id, file_name=str(thumb_file))
            
            await status.delete()
            await m.reply_photo(
                photo=str(thumb_file),
                caption="🖼️ **Extracted thumbnail from the file**"
            )
            
            # Clean up
            if thumb_file.exists():
                thumb_file.unlink()
        else:
            await status.edit_text("❌ **No thumbnail found in this file**")
        
        # Clean up
        if temp_file.exists():
            temp_file.unlink()
            
    except Exception as e:
        await status.edit_text(f"❌ **Failed to extract thumbnail:**\n`{e}`")

@app.on_message(filters.command("leaderboard"))
async def cmd_leaderboard(_, m: Message):
    leaderboard_text = await get_leaderboard_text("monthly", 10)
    await m.reply_text(
        leaderboard_text,
        reply_markup=get_leaderboard_keyboard(m.chat.id, "monthly", 10)
    )

@app.on_message(filters.command("setdump"))
async def cmd_setdump(_, m: Message):
    if len(m.command) < 2:
        # Show current dump channel
        dump_id = user_dump_channel.get(m.chat.id)
        if dump_id:
            try:
                chat = await app.get_chat(dump_id)
                await m.reply_text(
                    f"📤 **Current dump channel:**\n"
                    f"**Name:** {chat.title or 'Unknown'}\n"
                    f"**ID:** `{dump_id}`"
                )
            except:
                await m.reply_text(f"📤 **Current dump channel ID:** `{dump_id}`")
        else:
            await m.reply_text(
                "❌ **No dump channel set**\n\n"
                "Usage: `/setdump {channel_id}`\n"
                "Example: `/setdump -1001234567890`"
            )
        return
    
    try:
        channel_id = int(m.command[1])
    except ValueError:
        return await m.reply_text(
            "❌ **Invalid channel ID**\n\n"
            "Please provide a valid channel ID (usually starts with -100)"
        )
    
    status = await m.reply_text("🔍 Checking channel permissions...")
    
    try:
        # Check if bot can access the channel
        chat = await app.get_chat(channel_id)
        
        # Check if bot is admin
        bot_member = await app.get_chat_member(channel_id, "me")
        if not bot_member.privileges or not bot_member.privileges.can_post_messages:
            return await status.edit_text(
                "❌ **Bot is not admin in this channel or doesn't have post permissions**\n\n"
                "Please make the bot admin with post message permissions."
            )
        
        user_dump_channel[m.chat.id] = channel_id
        await status.edit_text(
            f"✅ **Dump channel set successfully!**\n\n"
            f"**Channel:** {chat.title or 'Unknown'}\n"
            f"**ID:** `{channel_id}`\n\n"
            "All renamed files will now be sent to this channel as well."
        )
        
    except ChannelInvalid:
        await status.edit_text("❌ **Invalid channel or bot is not added to the channel**")
    except ChatAdminRequired:
        await status.edit_text("❌ **Bot needs admin permissions in the channel**")
    except Exception as e:
        await status.edit_text(f"❌ **Error:** `{e}`")

@app.on_message(filters.command("deldump"))
async def cmd_deldump(_, m: Message):
    if m.chat.id in user_dump_channel:
        del user_dump_channel[m.chat.id]
        await m.reply_text(
            "🗑️ **Dump channel deleted successfully!**\n\n"
            "Files will no longer be sent to the dump channel."
        )
    else:
        await m.reply_text(
            "❌ **No dump channel set to delete**\n\n"
            "Use `/setdump {channel_id}` to set a dump channel first."
        )

@app.on_message(filters.command("seedump"))
async def cmd_seedump(_, m: Message):
    dump_id = user_dump_channel.get(m.chat.id)
    if dump_id:
        try:
            chat = await app.get_chat(dump_id)
            await m.reply_text(
                f"📤 **Current dump channel:**\n"
                f"**Name:** {chat.title or 'Unknown'}\n"
                f"**ID:** `{dump_id}`"
            )
        except:
            await m.reply_text(f"📤 **Current dump channel ID:** `{dump_id}`")
    else:
        await m.reply_text(
            "❌ **No dump channel set**\n\n"
            "Use `/setdump {channel_id}` to set a dump channel first."
        )

@app.on_message(filters.command("seepic"))
async def cmd_seepic(_, m: Message):
    thumb_path = user_thumbnail.get(m.chat.id)
    if thumb_path and thumb_path.exists():
        await m.reply_photo(
            photo=str(thumb_path),
            caption="🖼️ **Your current thumbnail**\n\nThis will be used for all your file uploads."
        )
    else:
        await m.reply_text(
            "❌ **No thumbnail set**\n\n"
            "Send any photo to set it as your thumbnail, or use `/addpic` for instructions."
        )

@app.on_message(filters.command("panel"))
async def cmd_panel(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    stats = await get_admin_stats()
    await m.reply_text(
        f"🛠️ **Admin Panel**\n\n"
        f"**Bot Statistics:**\n"
        f"• Total Users: {stats['total_users']}\n"
        f"• Total Files Processed: {stats['total_files']}\n"
        f"• Active Today: {stats['active_today']}\n"
        f"• Queue Length: {len(task_queue)}\n\n"
        f"**Admin Settings:**\n"
        f"• Admin Dump: {'✅' if admin_dump_channel else '❌'}\n"
        f"• Log Channel: {'✅' if admin_log_channel else '❌'}\n"
        f"• Force Sub Channels: {len(force_sub_channels)}\n"
        f"• Total Admins: {len(admin_list)}",
        reply_markup=get_admin_panel_keyboard(m.chat.id)
    )

@app.on_message(filters.command("forcesub"))
async def cmd_forcesub(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    if len(m.command) < 2:
        # Show current force sub channels
        if force_sub_channels:
            text = "📢 **Force Subscribe Channels:**\n\n"
            for channel_id, channel_name in force_sub_channels.items():
                text += f"• {channel_name} (`{channel_id}`)\n"
        else:
            text = "❌ **No force subscribe channels set**"
        
        text += "\n\n**Usage:**\n"
        text += "• `/forcesub add {channel_id}` - Add channel\n"
        text += "• `/forcesub remove {channel_id}` - Remove channel\n"
        text += "• `/forcesub list` - Show all channels"
        
        return await m.reply_text(text)
    
    action = m.command[1].lower()
    
    if action == "add" and len(m.command) >= 3:
        try:
            channel_id = int(m.command[2])
            chat = await app.get_chat(channel_id)
            force_sub_channels[channel_id] = chat.title or f"Channel {channel_id}"
            await m.reply_text(f"✅ **Added force subscribe channel:** {chat.title}")
            await log_to_admin(f"Force sub channel added: {chat.title} ({channel_id})")
        except Exception as e:
            await m.reply_text(f"❌ **Error:** {e}")
    
    elif action == "remove" and len(m.command) >= 3:
        try:
            channel_id = int(m.command[2])
            if channel_id in force_sub_channels:
                channel_name = force_sub_channels.pop(channel_id)
                await m.reply_text(f"✅ **Removed force subscribe channel:** {channel_name}")
                await log_to_admin(f"Force sub channel removed: {channel_name} ({channel_id})")
            else:
                await m.reply_text("❌ **Channel not found in force subscribe list**")
        except Exception as e:
            await m.reply_text(f"❌ **Error:** {e}")
    
    elif action == "list":
        if force_sub_channels:
            text = "📢 **Force Subscribe Channels:**\n\n"
            for channel_id, channel_name in force_sub_channels.items():
                text += f"• {channel_name} (`{channel_id}`)\n"
        else:
            text = "❌ **No force subscribe channels set**"
        await m.reply_text(text)

@app.on_message(filters.command("admins"))
async def cmd_admins(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    if len(m.command) < 2:
        # Show current admins
        text = "👥 **Bot Admins:**\n\n"
        for admin_id in admin_list:
            try:
                user = await app.get_users(admin_id)
                name = user.first_name or "Unknown"
                username = f"@{user.username}" if user.username else ""
                text += f"• {name} {username} (`{admin_id}`)\n"
            except:
                text += f"• Unknown User (`{admin_id}`)\n"
        
        text += "\n\n**Usage:**\n"
        text += "• `/admins add {user_id}` - Add admin\n"
        text += "• `/admins remove {user_id}` - Remove admin\n"
        text += "• `/admins list` - Show all admins"
        
        return await m.reply_text(text)
    
    action = m.command[1].lower()
    
    if action == "add" and len(m.command) >= 3:
        try:
            user_id = int(m.command[2])
            if user_id not in admin_list:
                admin_list.add(user_id)
                user = await app.get_users(user_id)
                name = user.first_name or "Unknown User"
                await m.reply_text(f"✅ **Added admin:** {name} (`{user_id}`)")
                await log_to_admin(f"New admin added: {name} ({user_id})")
            else:
                await m.reply_text("❌ **User is already an admin**")
        except Exception as e:
            await m.reply_text(f"❌ **Error:** {e}")
    
    elif action == "remove" and len(m.command) >= 3:
        try:
            user_id = int(m.command[2])
            if user_id == ADMIN_ID:
                return await m.reply_text("❌ **Cannot remove main admin**")
            if user_id in admin_list:
                admin_list.remove(user_id)
                user = await app.get_users(user_id)
                name = user.first_name or "Unknown User"
                await m.reply_text(f"✅ **Removed admin:** {name} (`{user_id}`)")
                await log_to_admin(f"Admin removed: {name} ({user_id})")
            else:
                await m.reply_text("❌ **User is not an admin**")
        except Exception as e:
            await m.reply_text(f"❌ **Error:** {e}")
    
    elif action == "list":
        text = "👥 **Bot Admins:**\n\n"
        for admin_id in admin_list:
            try:
                user = await app.get_users(admin_id)
                name = user.first_name or "Unknown"
                username = f"@{user.username}" if user.username else ""
                text += f"• {name} {username} (`{admin_id}`)\n"
            except:
                text += f"• Unknown User (`{admin_id}`)\n"
        await m.reply_text(text)

@app.on_message(filters.command("admindump"))
async def cmd_admindump(_, m: Message):
    global admin_dump_channel
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    if len(m.command) < 2:
        if admin_dump_channel:
            try:
                chat = await app.get_chat(admin_dump_channel)
                await m.reply_text(
                    f"📤 **Current admin dump channel:**\n"
                    f"**Name:** {chat.title or 'Unknown'}\n"
                    f"**ID:** `{admin_dump_channel}`"
                )
            except:
                await m.reply_text(f"📤 **Current admin dump channel ID:** `{admin_dump_channel}`")
        else:
            await m.reply_text(
                "❌ **No admin dump channel set**\n\n"
                "Usage: `/admindump {channel_id}`"
            )
        return
    
    try:
        channel_id = int(m.command[1])
        chat = await app.get_chat(channel_id)
        
        # Check if bot is admin
        bot_member = await app.get_chat_member(channel_id, "me")
        if not bot_member.privileges or not bot_member.privileges.can_post_messages:
            return await m.reply_text(
                "❌ **Bot is not admin in this channel or doesn't have post permissions**"
            )
        
        admin_dump_channel = channel_id
        await m.reply_text(
            f"✅ **Admin dump channel set successfully!**\n\n"
            f"**Channel:** {chat.title or 'Unknown'}\n"
            f"**ID:** `{channel_id}`\n\n"
            "All renamed files will now be sent to this admin channel."
        )
        await log_to_admin(f"Admin dump channel set: {chat.title} ({channel_id})")
        
    except Exception as e:
        await m.reply_text(f"❌ **Error:** `{e}`")

@app.on_message(filters.command("log"))
async def cmd_log(_, m: Message):
    global admin_log_channel
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    if len(m.command) < 2:
        if admin_log_channel:
            try:
                chat = await app.get_chat(admin_log_channel)
                await m.reply_text(
                    f"📊 **Current log channel:**\n"
                    f"**Name:** {chat.title or 'Unknown'}\n"
                    f"**ID:** `{admin_log_channel}`"
                )
            except:
                await m.reply_text(f"📊 **Current log channel ID:** `{admin_log_channel}`")
        else:
            await m.reply_text(
                "❌ **No log channel set**\n\n"
                "Usage: `/log {channel_id}`"
            )
        return
    
    try:
        channel_id = int(m.command[1])
        chat = await app.get_chat(channel_id)
        
        # Check if bot is admin
        bot_member = await app.get_chat_member(channel_id, "me")
        if not bot_member.privileges or not bot_member.privileges.can_post_messages:
            return await m.reply_text(
                "❌ **Bot is not admin in this channel or doesn't have post permissions**"
            )
        
        admin_log_channel = channel_id
        await m.reply_text(
            f"✅ **Log channel set successfully!**\n\n"
            f"**Channel:** {chat.title or 'Unknown'}\n"
            f"**ID:** `{channel_id}`\n\n"
            "Bot logs and errors will now be sent to this channel."
        )
        await log_to_admin(f"Log channel set: {chat.title} ({channel_id})")
        
    except Exception as e:
        await m.reply_text(f"❌ **Error:** `{e}`")

@app.on_message(filters.command("ban"))
async def cmd_ban(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    if len(m.command) < 2:
        return await m.reply_text(
            "❌ **Usage:** `/ban {user_id}`\n\n"
            "**Example:** `/ban 123456789`"
        )
    
    try:
        user_id = int(m.command[1])
        
        # Prevent banning other admins
        if user_id in admin_list:
            return await m.reply_text("❌ **Cannot ban an admin user**")
        
        if user_id in banned_users:
            return await m.reply_text("❌ **User is already banned**")
        
        banned_users.add(user_id)
        
        try:
            user = await app.get_users(user_id)
            name = user.first_name or "Unknown User"
            username = f"@{user.username}" if user.username else ""
            await m.reply_text(
                f"🚫 **User banned successfully!**\n\n"
                f"**Name:** {name} {username}\n"
                f"**ID:** `{user_id}`\n\n"
                "This user can no longer use the bot."
            )
            await log_to_admin(f"User banned: {name} {username} ({user_id}) by admin {m.from_user.first_name}")
        except:
            await m.reply_text(
                f"🚫 **User banned successfully!**\n\n"
                f"**ID:** `{user_id}`\n\n"
                "This user can no longer use the bot."
            )
            await log_to_admin(f"User banned: Unknown User ({user_id}) by admin {m.from_user.first_name}")
        
    except ValueError:
        await m.reply_text("❌ **Invalid user ID. Please provide a valid numeric user ID.**")
    except Exception as e:
        await m.reply_text(f"❌ **Error:** `{e}`")

@app.on_message(filters.command("unban"))
async def cmd_unban(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    if len(m.command) < 2:
        return await m.reply_text(
            "❌ **Usage:** `/unban {user_id}`\n\n"
            "**Example:** `/unban 123456789`"
        )
    
    try:
        user_id = int(m.command[1])
        
        if user_id not in banned_users:
            return await m.reply_text("❌ **User is not banned**")
        
        banned_users.remove(user_id)
        
        try:
            user = await app.get_users(user_id)
            name = user.first_name or "Unknown User"
            username = f"@{user.username}" if user.username else ""
            await m.reply_text(
                f"✅ **User unbanned successfully!**\n\n"
                f"**Name:** {name} {username}\n"
                f"**ID:** `{user_id}`\n\n"
                "This user can now use the bot again."
            )
            await log_to_admin(f"User unbanned: {name} {username} ({user_id}) by admin {m.from_user.first_name}")
        except:
            await m.reply_text(
                f"✅ **User unbanned successfully!**\n\n"
                f"**ID:** `{user_id}`\n\n"
                "This user can now use the bot again."
            )
            await log_to_admin(f"User unbanned: Unknown User ({user_id}) by admin {m.from_user.first_name}")
        
    except ValueError:
        await m.reply_text("❌ **Invalid user ID. Please provide a valid numeric user ID.**")
    except Exception as e:
        await m.reply_text(f"❌ **Error:** `{e}`")

@app.on_message(filters.command("bans"))
async def cmd_bans(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    if not banned_users:
        return await m.reply_text(
            "✅ **No banned users**\n\n"
            "No users are currently banned from using the bot."
        )
    
    text = "🚫 **Banned Users List:**\n\n"
    
    for i, user_id in enumerate(banned_users, 1):
        try:
            user = await app.get_users(user_id)
            name = user.first_name or "Unknown"
            username = f"@{user.username}" if user.username else ""
            text += f"{i}. **{name}** {username} (`{user_id}`)\n"
        except:
            text += f"{i}. **Unknown User** (`{user_id}`)\n"
    
    text += f"\n**Total banned users:** {len(banned_users)}"
    text += f"\n\n**Commands:**\n• `/unban {{user_id}}` - Unban a user\n• `/ban {{user_id}}` - Ban a user"
    
    await m.reply_text(text)

@app.on_message(filters.command("metadata"))
async def cmd_metadata(_, m: Message):
    chat_id = m.chat.id
    status = "On" if user_metadata_enabled[chat_id] else "Off"
    metadata = user_metadata[chat_id]
    
    text = (
        f"㊋ **Yᴏᴜʀ Mᴇᴛᴀᴅᴀᴛᴀ ɪꜱ ᴄᴜʀʀᴇɴᴛʟʏ:** {status}\n\n"
        f"◈ **Tɪᴛʟᴇ** ▹ {metadata['title']}\n"
        f"◈ **Aᴜᴛʜᴏʀ** ▹ {metadata['author']}\n"
        f"◈ **Aʀᴛɪꜱᴛ** ▹ {metadata['artist']}\n"
        f"◈ **Aᴜᴅɪᴏ** ▹ {metadata['audio']}\n"
        f"◈ **Sᴜʙᴛɪᴛʟᴇ** ▹ {metadata['subtitle']}\n"
        f"◈ **Vɪᴅᴇᴏ** ▹ {metadata['video']}"
    )
    
    await m.reply_text(text, reply_markup=get_metadata_keyboard(chat_id))

@app.on_message(filters.command("settitle"))
async def cmd_settitle(_, m: Message):
    if len(m.command) < 2 and not m.text.strip().startswith("/settitle "):
        return await m.reply_text(
            "**Usage:** `/settitle <title>`\n\n"
            "**Example:** `/settitle Encoded By @Weoo_Animes`"
        )
    
    title = m.text.split(" ", 1)[1].strip()
    if not title:
        return await m.reply_text("Please provide a title after /settitle.")
    
    user_metadata[m.chat.id]["title"] = title
    await m.reply_text(f"✅ **Title metadata updated!**\n\n**New title:** `{title}`")

@app.on_message(filters.command("setauthor"))
async def cmd_setauthor(_, m: Message):
    if len(m.command) < 2 and not m.text.strip().startswith("/setauthor "):
        return await m.reply_text(
            "**Usage:** `/setauthor <author>`\n\n"
            "**Example:** `/setauthor @Weoo_Animes`"
        )
    
    author = m.text.split(" ", 1)[1].strip()
    if not author:
        return await m.reply_text("Please provide an author after /setauthor.")
    
    user_metadata[m.chat.id]["author"] = author
    await m.reply_text(f"✅ **Author metadata updated!**\n\n**New author:** `{author}`")

@app.on_message(filters.command("setartist"))
async def cmd_setartist(_, m: Message):
    if len(m.command) < 2 and not m.text.strip().startswith("/setartist "):
        return await m.reply_text(
            "**Usage:** `/setartist <artist>`\n\n"
            "**Example:** `/setartist @Weoo_Animes`"
        )
    
    artist = m.text.split(" ", 1)[1].strip()
    if not artist:
        return await m.reply_text("Please provide an artist after /setartist.")
    
    user_metadata[m.chat.id]["artist"] = artist
    await m.reply_text(f"✅ **Artist metadata updated!**\n\n**New artist:** `{artist}`")

@app.on_message(filters.command("setaudio"))
async def cmd_setaudio(_, m: Message):
    if len(m.command) < 2 and not m.text.strip().startswith("/setaudio "):
        return await m.reply_text(
            "**Usage:** `/setaudio <audio>`\n\n"
            "**Example:** `/setaudio @Weoo_Animes`"
        )
    
    audio = m.text.split(" ", 1)[1].strip()
    if not audio:
        return await m.reply_text("Please provide audio metadata after /setaudio.")
    
    user_metadata[m.chat.id]["audio"] = audio
    await m.reply_text(f"✅ **Audio metadata updated!**\n\n**New audio:** `{audio}`")

@app.on_message(filters.command("setsubtitle"))
async def cmd_setsubtitle(_, m: Message):
    if len(m.command) < 2 and not m.text.strip().startswith("/setsubtitle "):
        return await m.reply_text(
            "**Usage:** `/setsubtitle <subtitle>`\n\n"
            "**Example:** `/setsubtitle @Weoo_Animes`"
        )
    
    subtitle = m.text.split(" ", 1)[1].strip()
    if not subtitle:
        return await m.reply_text("Please provide subtitle metadata after /setsubtitle.")
    
    user_metadata[m.chat.id]["subtitle"] = subtitle
    await m.reply_text(f"✅ **Subtitle metadata updated!**\n\n**New subtitle:** `{subtitle}`")

@app.on_message(filters.command("setvideo"))
async def cmd_setvideo(_, m: Message):
    if len(m.command) < 2 and not m.text.strip().startswith("/setvideo "):
        return await m.reply_text(
            "**Usage:** `/setvideo <video>`\n\n"
            "**Example:** `/setvideo For More @Weoo_Animes`"
        )
    
    video = m.text.split(" ", 1)[1].strip()
    if not video:
        return await m.reply_text("Please provide video metadata after /setvideo.")
    
    user_metadata[m.chat.id]["video"] = video
    await m.reply_text(f"✅ **Video metadata updated!**\n\n**New video:** `{video}`")

@app.on_message(filters.command("broadcast"))
async def cmd_broadcast(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("❌ **Access denied.** Only admins can use this command.")
    
    if len(m.command) < 2 and not m.text.strip().startswith("/broadcast "):
        return await m.reply_text(
            "**Usage:** `/broadcast <message>`\n\n"
            "**Example:** `/broadcast Important announcement for all users!`\n\n"
            "This will send the message to all users who have used the bot."
        )
    
    broadcast_message = m.text.split(" ", 1)[1].strip()
    if not broadcast_message:
        return await m.reply_text("Please provide a message to broadcast after /broadcast.")
    
    # Get total users count
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(DISTINCT user_id) FROM user_stats')
        total_users = cursor.fetchone()[0]
        conn.close()
    except Exception as e:
        await send_error_log(f"Broadcast count error: {e}")
        return await m.reply_text("❌ **Error getting user count. Please try again later.**")
    
    if total_users == 0:
        return await m.reply_text("❌ **No users found to broadcast to.**")
    
    # Show preview and confirmation
    preview_text = (
        f"📢 **Broadcast Preview**\n\n"
        f"**Message:**\n{broadcast_message}\n\n"
        f"**Will be sent to:** {total_users} users\n\n"
        f"**Are you sure you want to send this broadcast?**"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Send", callback_data=f"broadcast_confirm_{m.id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"broadcast_cancel_{m.id}"),
        ]
    ]
    
    # Store broadcast data temporarily
    broadcast_data[m.id] = {
        'message': broadcast_message,
        'admin_id': m.from_user.id,
        'admin_name': m.from_user.first_name
    }
    
    await m.reply_text(preview_text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.command("clear"))
async def cmd_clear(_, m: Message):
    user_id = m.from_user.id
    
    if is_admin(user_id):
        # Admin version with confirmation
        queue_length = len(task_queue)
        
        if queue_length == 0:
            return await m.reply_text("✅ **Queue is already empty.**")
        
        # Show confirmation for admin
        confirmation_text = (
            f"🗑️ **Admin Clear Queue Confirmation**\n\n"
            f"**Current queue length:** {queue_length} files\n\n"
            f"**Are you sure you want to clear ALL pending files?**\n"
            f"This action cannot be undone!"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Clear All Queue", callback_data=f"clear_confirm_{m.id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"clear_cancel_{m.id}"),
            ]
        ]
        
        return await m.reply_text(confirmation_text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    else:
        # User version - remove only their files without confirmation
        user_files_removed = 0
        remaining_queue = deque()
        
        # Filter out user's files from queue
        while task_queue:
            msg = task_queue.popleft()
            if msg.from_user.id == user_id:
                user_files_removed += 1
            else:
                remaining_queue.append(msg)
        
        # Replace queue with filtered queue
        task_queue.clear()
        task_queue.extend(remaining_queue)
        
        if user_files_removed == 0:
            await m.reply_text("✅ **You have no files in the queue.**")
        else:
            await m.reply_text(
                f"✅ **Cleared your files from queue!**\n\n"
                f"**Removed:** {user_files_removed} file{'s' if user_files_removed != 1 else ''}\n"
                f"**Remaining in queue:** {len(task_queue)} files"
            )

@app.on_message(filters.photo)
async def set_thumbnail(client: Client, m: Message):
    status = await m.reply_text("📸 Saving thumbnail…")
    try:
        # get largest size
        photo = m.photo
        file_id = photo.file_id
        path = THUMBDIR / f"{m.chat.id}.jpg"
        ctx = ProgressCtx()
        async def cb(current, total):
            await update_status(m, status, "Downloading thumbnail", current, total, ctx)
        await client.download_media(file_id, file_name=str(path), progress=cb)
        user_thumbnail[m.chat.id] = path
        await status.edit_text("✅ Thumbnail saved! It will be used for your next uploads.")
    except Exception as e:
        await status.edit_text(f"❌ Failed to save thumbnail:\n`{e}`")

# Accept documents, videos, audios, animations, voices
media_filter = (
    filters.document |
    filters.video |
    filters.audio |
    filters.animation |
    filters.voice
)

@app.on_message(media_filter)
async def enqueue_media(_, m: Message):
    if is_banned(m.from_user.id):
        return await m.reply_text(
            "🚫 **You are banned from using this bot.**\n\n"
            "Contact an administrator if you think this is a mistake."
        )
    
    task_queue.append(m)
    pos = len(task_queue)
    await m.reply_text(f"📥 Added to queue. Position: **#{pos}**")
    # Kick dispatcher
    asyncio.create_task(dispatcher())

async def get_leaderboard_text(period: str, limit: int) -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        now = datetime.now()
        
        if period == "daily":
            date_filter = now.strftime("%Y-%m-%d")
            cursor.execute('''
                SELECT u.username, u.first_name, COALESCE(d.files_count, 0) as total
                FROM user_stats u
                LEFT JOIN daily_stats d ON u.user_id = d.user_id AND d.date = ?
                WHERE COALESCE(d.files_count, 0) > 0
                ORDER BY total DESC
                LIMIT ?
            ''', (date_filter, limit))
            title = "📊 **Daily Leaderboard**"
            
        elif period == "weekly":
            week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            cursor.execute('''
                SELECT u.username, u.first_name, COALESCE(SUM(d.files_count), 0) as total
                FROM user_stats u
                LEFT JOIN daily_stats d ON u.user_id = d.user_id AND d.date >= ?
                GROUP BY u.user_id
                HAVING total > 0
                ORDER BY total DESC
                LIMIT ?
            ''', (week_start, limit))
            title = "📊 **Weekly Leaderboard**"
            
        elif period == "monthly":
            month_start = now.replace(day=1).strftime("%Y-%m-%d")
            cursor.execute('''
                SELECT u.username, u.first_name, COALESCE(SUM(d.files_count), 0) as total
                FROM user_stats u
                LEFT JOIN daily_stats d ON u.user_id = d.user_id AND d.date >= ?
                GROUP BY u.user_id
                HAVING total > 0
                ORDER BY total DESC
                LIMIT ?
            ''', (month_start, limit))
            title = "📊 **Monthly Leaderboard**"
            
        else:  # yearly
            year_start = now.replace(month=1, day=1).strftime("%Y-%m-%d")
            cursor.execute('''
                SELECT u.username, u.first_name, COALESCE(SUM(d.files_count), 0) as total
                FROM user_stats u
                LEFT JOIN daily_stats d ON u.user_id = d.user_id AND d.date >= ?
                GROUP BY u.user_id
                HAVING total > 0
                ORDER BY total DESC
                LIMIT ?
            ''', (year_start, limit))
            title = "📊 **Yearly Leaderboard**"
        
        results = cursor.fetchall()
        conn.close()
        
        if not results:
            return f"{title}\n\n❌ **No data available for this period**"
        
        text = f"{title}\n\n"
        
        medals = ["🥇", "🥈", "🥉"]
        for i, (username, first_name, count) in enumerate(results, 1):
            medal = medals[i-1] if i <= 3 else f"{i}."
            name = first_name or username or "Unknown"
            if username:
                name = f"@{username}"
            text += f"{medal} **{name}** - {count} files\n"
        
        return text
    except Exception as e:
        await send_error_log(f"Leaderboard error: {e}")
        return "❌ **Error loading leaderboard. Please try again later.**"

async def get_admin_stats():
    """Get admin statistics"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Total users
        cursor.execute('SELECT COUNT(*) FROM user_stats')
        total_users = cursor.fetchone()[0]
        
        # Total files
        cursor.execute('SELECT SUM(files_renamed) FROM user_stats')
        total_files = cursor.fetchone()[0] or 0
        
        # Active today
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute('SELECT COUNT(*) FROM daily_stats WHERE date = ?', (today,))
        active_today = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'total_users': total_users,
            'total_files': total_files,
            'active_today': active_today
        }
    except Exception as e:
        await send_error_log(f"Admin stats error: {e}")
        return {
            'total_users': 0,
            'total_files': 0,
            'active_today': 0
        }

# ---------- Core processing ----------
async def dispatcher():
    # Ensure only one dispatcher loop at a time
    global dispatcher_started
    if dispatcher_started:
        return
    dispatcher_started = True
    try:
        while task_queue:
            # Launch up to available slots
            await asyncio.sleep(0)  # yield
            msg = task_queue.popleft()
            asyncio.create_task(process_one(msg))
            # Small delay to let tasks grab the semaphore fairly
            await asyncio.sleep(0.1)
        # Wait for all running tasks to finish slots
        # (we don't track tasks list; a small wait ensures finish)
        await asyncio.sleep(0.2)
    finally:
        dispatcher_started = False

async def process_one(m: Message):
    async with semaphore:
        chat_id = m.chat.id
        fmt = user_format[chat_id]
        
        # Get original filename
        original_filename = ""
        d = m.document or m.video or m.audio or m.voice or m.animation
        if d and d.file_name:
            original_filename = d.file_name
        
        # Extract variables based on mode setting
        mode = user_mode[chat_id]
        if mode == "caption" and m.caption:
            vars_from_filename = extract_variables_from_filename(m.caption)
        else:
            vars_from_filename = extract_variables_from_filename(original_filename)

        # Build variables
        stem = original_stem(m)
        ext = media_extension(m)
        base_vars = {"filename": stem}
        base_vars.update(vars_from_filename)

        # Render final filename
        new_name = render_filename(fmt, base_vars, ext)
        temp_path = TEMPDIR / f"{m.id}{ext}"
        out_path = OUTDIR / new_name

        status = await m.reply_text(
            f"🟡 Queued…\nTarget: `{new_name}`"
        )

        # ---------- Download ----------
        dl_ctx = ProgressCtx()
        try:
            async def dl_cb(cur, tot):
                await update_status(m, status, "Downloading", cur, tot, dl_ctx)
            await app.download_media(m, file_name=str(temp_path), progress=dl_cb)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return await process_one(m)
        except Exception as e:
            return await status.edit_text(f"❌ Download failed:\n`{e}`")

        # ---------- Rename ----------
        try:
            await status.edit_text("🛠 Renaming…")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # If a file with same name exists, add (1), (2), ...
            final_path = out_path
            n = 1
            while final_path.exists():
                stem = Path(out_path).stem
                final_path = out_path.with_name(f"{stem} ({n}){out_path.suffix}")
                n += 1
            shutil.move(str(temp_path), str(final_path))
        except Exception as e:
            return await status.edit_text(f"❌ Rename failed:\n`{e}`")

        # ---------- Apply Metadata ----------
        if user_metadata_enabled[chat_id]:
            try:
                await status.edit_text("🏷️ Applying metadata…")
                final_path = await apply_metadata(final_path, user_metadata[chat_id])
            except Exception as e:
                await send_error_log(f"Metadata application failed: {e}", m.from_user.id)
                # Continue with upload even if metadata fails

        # ---------- Upload ----------
        ul_ctx = ProgressCtx()
        thumb = user_thumbnail.get(chat_id)
        thumb_param = None
        if thumb and thumb.exists():
            thumb_param = str(thumb)

        try:
            async def ul_cb(cur, tot):
                await update_status(m, status, "Uploading", cur, tot, ul_ctx)

            # Generate caption using format
            caption_format = user_caption_format[chat_id]
            file_metadata = extract_file_metadata(m, new_name, final_path)
            
            # Render caption
            class SafeDict(dict):
                def __missing__(self, key):
                    return ""  # drop unknown keys instead of KeyError
            
            formatted_caption = caption_format.format_map(SafeDict(file_metadata))
            if not formatted_caption.strip():
                formatted_caption = new_name  # fallback

            # Send based on media_type setting
            sent: Optional[Message] = None
            media_type = user_media_type[chat_id]
            
            if media_type == "video" and (m.video or final_path.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov', '.wmv']):
                # Send as video
                if thumb_param:
                    sent = await app.send_video(
                        chat_id,
                        video=str(final_path),
                        caption=formatted_caption,
                        thumb=thumb_param,
                        progress=ul_cb
                    )
                else:
                    sent = await app.send_video(
                        chat_id,
                        video=str(final_path),
                        caption=formatted_caption,
                        progress=ul_cb
                    )
            else:
                # Send as document (default)
                if thumb_param:
                    sent = await app.send_document(
                        chat_id,
                        document=str(final_path),
                        caption=formatted_caption,
                        thumb=thumb_param,
                        progress=ul_cb
                    )
                else:
                    sent = await app.send_document(
                        chat_id,
                        document=str(final_path),
                        caption=formatted_caption,
                        progress=ul_cb
                    )

            # Show the actual filename from the sent message
            actual_filename = new_name
            if sent:
                if sent.video and sent.video.file_name:
                    actual_filename = sent.video.file_name
                elif sent.document and sent.document.file_name:
                    actual_filename = sent.document.file_name
                elif sent.audio and sent.audio.file_name:
                    actual_filename = sent.audio.file_name
                elif sent.animation and sent.animation.file_name:
                    actual_filename = sent.animation.file_name
            
            # Update user statistics
            user = m.from_user
            if user:
                update_user_stats(
                    user.id, 
                    user.username or "", 
                    user.first_name or ""
                )
            
            # Send to user dump channel if set
            dump_channel_id = user_dump_channel.get(chat_id)
            if dump_channel_id and sent:
                asyncio.create_task(send_to_dump_channel(
                    dump_channel_id, final_path, formatted_caption, thumb_param, sent.video is not None
                ))
            
            # Send to admin dump channel if set
            if admin_dump_channel and sent:
                user = m.from_user
                admin_caption = f"**User:** {user.first_name} (@{user.username or 'None'}) | ID: {user.id}\n\n{formatted_caption}"
                asyncio.create_task(send_to_dump_channel(
                    admin_dump_channel, final_path, admin_caption, thumb_param, sent.video is not None
                ))
            
            await status.edit_text(f"✅ **Done!**\n\n📁 **Renamed to:** `{actual_filename}`")
            await log_to_admin(f"File processed: {actual_filename} by {m.from_user.first_name} ({m.from_user.id})")
        except Exception as e:
            await status.edit_text(f"❌ Upload failed:\n`{e}`")
            await send_error_log(f"Upload failed: {e}", m.from_user.id)
        finally:
            # Clean disk (keep out file if you prefer; currently we keep a copy in OUTDIR)
            try:
                # Remove the local file we uploaded (OUTDIR copy is the one we keep)
                if final_path.exists():
                    # If you want zero retention, uncomment next line to delete:
                    # final_path.unlink()
                    pass
            except Exception:
                pass

async def apply_metadata(file_path: Path, metadata: Dict[str, str]) -> Path:
    """Apply metadata to a media file using FFmpeg"""
    try:
        # Create temporary output file
        output_path = file_path.parent / f"meta_{file_path.name}"
        
        # Build FFmpeg command
        cmd = [
            "ffmpeg", "-i", str(file_path),
            "-c", "copy",  # Copy without re-encoding
            "-metadata", f"title={metadata['title']}",
            "-metadata", f"author={metadata['author']}",
            "-metadata", f"artist={metadata['artist']}",
            "-metadata", f"album_artist={metadata['artist']}",
            "-metadata", f"audio_title={metadata['audio']}",
            "-metadata", f"subtitle_title={metadata['subtitle']}",
            "-metadata", f"video_title={metadata['video']}",
            "-y",  # Overwrite output file
            str(output_path)
        ]
        
        # Run FFmpeg
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            # Replace original file with metadata-enhanced file
            file_path.unlink()  # Remove original
            output_path.rename(file_path)  # Rename new file to original name
            return file_path
        else:
            # If FFmpeg fails, clean up and return original
            if output_path.exists():
                output_path.unlink()
            await send_error_log(f"FFmpeg metadata failed: {result.stderr}")
            return file_path
            
    except Exception as e:
        await send_error_log(f"Metadata application error: {e}")
        return file_path

async def send_to_dump_channel(channel_id: int, file_path: Path, caption: str, thumb_param: str, is_video: bool):
    """Asynchronously send file to dump channel"""
    try:
        if is_video:
            await app.send_video(
                channel_id,
                video=str(file_path),
                caption=caption,
                thumb=thumb_param
            )
        else:
            await app.send_document(
                channel_id,
                document=str(file_path),
                caption=caption,
                thumb=thumb_param
            )
    except Exception as e:
        await send_error_log(f"Failed to send to dump channel {channel_id}: {e}")

# ---------- Run ----------
if __name__ == "__main__":
    print("Starting Auto Rename Bot…")
    app.run()