import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from telegram import Update
from telegram.constants import MessageEntityType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import yt_dlp

# ------------ Settings ------------
MAX_TG_BYTES = int(os.getenv("MAX_TG_BYTES", str(1_900_000_000)))  # ~1.9 GB
NO_FFMPEG = os.getenv("NO_FFMPEG", "0") == "1"

# where we remember per-chat quality
Q_STORE_PATH = Path(os.getenv("QUALITY_STORE_PATH", "quality_store.json"))

# Supported quality presets
QUALITIES = ("360", "480", "720")  # 720 only meaningful when ffmpeg available

# Default formats (soft, with generous fallbacks)
# Item (2): softer fallback chains to reduce "format not available"
FORMAT_WITH_FFMPEG_720 = (
    "bv*[ext=mp4][height<=720]+ba[ext=m4a]/"
    "bv*+ba/b[ext=mp4][height<=720]/b/best"
)
FORMAT_WITH_FFMPEG_480 = (
    "bv*[ext=mp4][height<=480]+ba[ext=m4a]/"
    "bv*+ba/b[ext=mp4][height<=480]/b/best"
)
FORMAT_WITH_FFMPEG_360 = (
    "bv*[ext=mp4][height<=360]+ba[ext=m4a]/"
    "bv*+ba/b[ext=mp4][height<=360]/b/best"
)

# No-ffmpeg (single-file) options
FORMAT_NO_FFMPEG_720 = "best[ext=mp4][height<=720]/best[height<=720]/best"
FORMAT_NO_FFMPEG_480 = "best[ext=mp4][height<=480]/best[height<=480]/best"
FORMAT_NO_FFMPEG_360 = "best[ext=mp4][height<=360]/best[height<=360]/best"

# ----------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("tg_video_bot")

URL_RE = re.compile(r"https?://\S+")

SUPPORTED_DOMAINS = (
    "youtube.com", "youtu.be", "m.youtube.com",
    "instagram.com", "instagr.am", "www.instagram.com",
    "tiktok.com", "vm.tiktok.com", "facebook.com", "fb.watch",
    "twitter.com", "x.com", "vimeo.com"
)

# ------------- tiny quality store -------------
def _load_qstore() -> dict:
    try:
        return json.loads(Q_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_qstore(store: dict) -> None:
    try:
        Q_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # best-effort; Railway’s FS is ephemeral anyway

QSTORE = _load_qstore()

def get_quality(chat_id: int) -> str:
    """Return preferred quality for a chat."""
    q = QSTORE.get(str(chat_id))
    if q in QUALITIES:
        return q
    # default depends on ffmpeg availability
    return "720" if not NO_FFMPEG else "480"

def set_quality(chat_id: int, q: str) -> None:
    if q not in QUALITIES:
        return
    QSTORE[str(chat_id)] = q
    _save_qstore(QSTORE)

# ----------------------------------------------

def is_supported_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host.endswith(d) for d in SUPPORTED_DOMAINS)

def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")

def _format_for_quality(q: str) -> str:
    if NO_FFMPEG:
        return {"720": FORMAT_NO_FFMPEG_720, "480": FORMAT_NO_FFMPEG_480, "360": FORMAT_NO_FFMPEG_360}[q]
    return {"720": FORMAT_WITH_FFMPEG_720, "480": FORMAT_WITH_FFMPEG_480, "360": FORMAT_WITH_FFMPEG_360}[q]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = get_quality(update.effective_chat.id)
    txt = [
        "<b>Hi!</b> Send me a link and I'll fetch the video for you.",
        "Supported: YouTube, Instagram, TikTok, Facebook, Twitter/X, Vimeo",
        f"Mode: {'NO_FFMPEG (≤480p single-file)' if NO_FFMPEG else 'FFmpeg (merging, ≤720p)'}",
        f"Current quality preference for this chat: <b>{q}p</b>",
        "Use /quality to change it.",
    ]
    await update.effective_message.reply_html("\n".join(txt))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)

async def quality_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Item (1): /quality [360|480|720] per-chat setting."""
    chat_id = update.effective_chat.id
    args = [a.strip().lower() for a in context.args] if context.args else []
    if not args:
        await update.message.reply_html(
            "Send <code>/quality 360</code>, <code>/quality 480</code>, or <code>/quality 720</code>.\n"
            f"Current: <b>{get_quality(chat_id)}p</b>"
        )
        return

    choice = args[0].replace("p", "")
    if choice not in QUALITIES:
        await update.message.reply_text("Please choose 360, 480, or 720.")
        return

    if choice == "720" and NO_FFMPEG:
        await update.message.reply_text("720p requires ffmpeg mode. I’m currently running without ffmpeg.")
        return

    set_quality(chat_id, choice)
    await update.message.reply_text(f"Quality preference saved: {choice}p")

def _write_cookies_if_needed(tmp: Path) -> str | None:
    IG_SESSIONID = os.getenv("IG_SESSIONID")
    if not IG_SESSIONID:
        return None
    cookies_path = tmp / "cookies.txt"
    content = (
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t" + IG_SESSIONID + "\n"
    )
    cookies_path.write_text(content, encoding="utf-8")
    return str(cookies_path)

def _pick_final_file(dirpath: Path) -> Path | None:
    candidates = list(dirpath.glob("**/*"))
    media = [p for p in candidates if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]
    if not media:
        return None
    mp4s = [p for p in media if p.suffix.lower() == ".mp4"]
    pool = mp4s or media
    pool.sort(key=lambda p: (p.stat().st_size, p.stat().st_mtime), reverse=True)
    return pool[0]

def _make_ydl_opts(tmpdir: Path, fmt: str, cookiefile: str | None = None):
    ydl_opts = {
        "outtmpl": str(tmpdir / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "nocheckcertificate": True,
        "retries": 3,
        "concurrent_fragment_downloads": 4,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        },
        "progress_hooks": [],
        "format": fmt,
    }
    if not NO_FFMPEG:
        ydl_opts["postprocessors"] = [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    return ydl_opts

def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def _estimated_too_big(info: dict) -> bool:
    size_keys = ("filesize", "filesize_approx")
    for k in size_keys:
        v = info.get(k)
        if isinstance(v, (int, float)) and v > MAX_TG_BYTES:
            return True
    total = 0
    if "requested_formats" in info and isinstance(info["requested_formats"], list):
        for f in info["requested_formats"]:
            for k in size_keys:
                v = f.get(k)
                if isinstance(v, (int, float)):
                    total += int(v)
        if total and total > MAX_TG_BYTES:
            return True
    return False

def _extract_and_download(url: str, tmpdir: Path, fmt_pref: str, cookiefile: str | None = None):
    """
    Returns: (filepath, title)
    Implements (2): try preferred format then downshift if too big or unavailable.
    """
    # build ordered fallbacks
    fallbacks = [fmt_pref]
    # add downshifts + a very generic best
    if NO_FFMPEG:
        chain = [FORMAT_NO_FFMPEG_480, FORMAT_NO_FFMPEG_360, "best"]
    else:
        chain = [FORMAT_WITH_FFMPEG_480, FORMAT_WITH_FFMPEG_360, "b/best"]
    for f in chain:
        if f not in fallbacks:
            fallbacks.append(f)

    chosen_fmt = fallbacks[0]
    title = "video"
    with yt_dlp.YoutubeDL(_make_ydl_opts(tmpdir, chosen_fmt, cookiefile)) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            title = info.get("title") or title
            if _estimated_too_big(info):
                # try smaller ones
                for f in fallbacks[1:]:
                    ydl.params["format"] = f
                    info2 = ydl.extract_info(url, download=False)
                    if not _estimated_too_big(info2):
                        chosen_fmt = f
                        info = info2
                        break
        except Exception:
            # try other formats if preferred isn't available
            for f in fallbacks[1:]:
                try:
                    ydl.params["format"] = f
                    info = ydl.extract_info(url, download=False)
                    title = info.get("title") or title
                    chosen_fmt = f
                    break
                except Exception:
                    continue

    # actual download with chosen format
    with yt_dlp.YoutubeDL(_make_ydl_opts(tmpdir, chosen_fmt, cookiefile)) as ydl:
        ydl.extract_info(url, download=True)

    out_file = _pick_final_file(tmpdir)
    if not out_file or not out_file.exists():
        raise RuntimeError("Downloaded file not found (post-processing may have failed).")
    return out_file, title

async def _download_and_send(url: str, update: Update) -> None:
    msg = update.effective_message
    tmpdir = Path(tempfile.mkdtemp(prefix="dl_"))
    cookiefile = None
    try:
        if "instagram." in url and os.getenv("IG_SESSIONID"):
            cookiefile = _write_cookies_if_needed(tmpdir)

        chat_q = get_quality(update.effective_chat.id)
        fmt_pref = _format_for_quality(chat_q)

        status = await msg.reply_text(f"⬇️ Downloading:\n{url}\nPreference: {chat_q}p")
        filepath, title = await asyncio.to_thread(_extract_and_download, url, tmpdir, fmt_pref, cookiefile)

        size = filepath.stat().st_size
        if size > MAX_TG_BYTES:
            await status.edit_text(
                f"⚠️ File is {_human(size)} which exceeds the configured max ({_human(MAX_TG_BYTES)}). "
                "Try a shorter video or lower resolution with /quality."
            )
            return

        await status.edit_text("📤 Uploading to Telegram…")
        # (3) try sending as video first; fallback to document
        try:
            await msg.reply_video(video=filepath.open("rb"), caption=f"{title}", supports_streaming=True)
        except Exception:
            await msg.reply_document(document=filepath.open("rb"), caption=f"{title}")
        await status.edit_text("✅ Done")
    except yt_dlp.utils.DownloadError as e:
        await msg.reply_text(f"❌ Download error:\n{e}")
    except Exception as e:
        await msg.reply_text(f"❌ Error: {e}")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    urls = [u for u in find_urls(text) if is_supported_url(u)]
    if not urls:
        await update.effective_message.reply_text("Please send a supported video link.")
        return
    for url in urls:
        await _download_and_send(url, update)

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN environment variable is required")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("quality", quality_cmd))  # new command
    app.add_handler(
        MessageHandler(
            filters.TEXT & (filters.Entity(MessageEntityType.URL) | filters.Entity(MessageEntityType.TEXT_LINK) | filters.Regex(URL_RE)),
            handle_message,
        )
    )

    log.info("Bot is starting… (NO_FFMPEG=%s)", NO_FFMPEG)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
