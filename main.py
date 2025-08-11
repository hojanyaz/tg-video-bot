import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from telegram import Update
from telegram.constants import MessageEntityType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import yt_dlp

# ------------ Settings ------------
MAX_TG_BYTES = int(os.getenv("MAX_TG_BYTES", str(1_900_000_000)))  # ~1.9 GB safety margin
NO_FFMPEG = os.getenv("NO_FFMPEG", "0") == "1"

# Best-quality defaults
# - With ffmpeg: prefer separate best video + best audio (merged)
# - Without ffmpeg: best single file (prefer mp4 first)
DEFAULT_FORMAT = "best[ext=mp4]/best" if NO_FFMPEG else "bv*+ba/best"

# Optional: Instagram session for your own private posts
IG_SESSIONID = os.getenv("IG_SESSIONID")

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

# ------------- helpers -------------

def is_supported_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host.endswith(d) for d in SUPPORTED_DOMAINS)

def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")

def _write_cookies_if_needed(tmp: Path) -> str | None:
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

def _extract_and_download(url: str, tmpdir: Path, cookiefile: str | None = None):
    """
    Try best quality; if Telegram size limit likely exceeded, step down.
    """
    title = "video"

    # Build a small fallback ladder (best -> ~720 -> ~480 -> ~360)
    if NO_FFMPEG:
        ladder = [
            "best[ext=mp4]/best",                     # best single file
            "best[ext=mp4][height<=720]/best[height<=720]",
            "best[ext=mp4][height<=480]/best[height<=480]",
            "best[ext=mp4][height<=360]/best[height<=360]",
        ]
    else:
        ladder = [
            "bv*+ba/best",                             # best (separate streams merged)
            "bv*[height<=720]+ba/b[height<=720]/best",
            "bv*[height<=480]+ba/b[height<=480]/best",
            "bv*[height<=360]+ba/b[height<=360]/best",
        ]

    chosen = ladder[0]
    with yt_dlp.YoutubeDL(_make_ydl_opts(tmpdir, chosen, cookiefile)) as ydl:
        # Probe and pick a format that fits under Telegram limit
        for fmt in ladder:
            try:
                ydl.params["format"] = fmt
                info = ydl.extract_info(url, download=False)
                title = info.get("title") or title
                if not _estimated_too_big(info):
                    chosen = fmt
                    break
            except Exception:
                continue

    # Download with chosen format
    with yt_dlp.YoutubeDL(_make_ydl_opts(tmpdir, chosen, cookiefile)) as ydl:
        ydl.extract_info(url, download=True)

    out_file = _pick_final_file(tmpdir)
    if not out_file or not out_file.exists():
        raise RuntimeError("Downloaded file not found (post-processing may have failed).")
    return out_file, title

# ------------- bot handlers -------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = [
        "<b>Hi!</b> Send me a link and I'll fetch the video for you.",
        "",
        "Supported: YouTube, Instagram, TikTok, Facebook, Twitter/X, Vimeo",
        f"Quality: best available ({'no-ffmpeg single-file' if NO_FFMPEG else 'ffmpeg merged'})",
        "Tip: Use this only for content you own or have permission to download.",
    ]
    await update.effective_message.reply_html("\n".join(txt))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)

async def _download_and_send(url: str, update: Update) -> None:
    msg = update.effective_message
    tmpdir = Path(tempfile.mkdtemp(prefix="dl_"))
    cookiefile = None
    try:
        if "instagram." in url and IG_SESSIONID:
            cookiefile = _write_cookies_if_needed(tmpdir)

        status = await msg.reply_text(f"⬇️ Downloading:\n{url}")
        filepath, title = await asyncio.to_thread(_extract_and_download, url, tmpdir, cookiefile)

        size = filepath.stat().st_size
        if size > MAX_TG_BYTES:
            await status.edit_text(
                f"⚠️ File is {_human(size)} which exceeds the configured max ({_human(MAX_TG_BYTES)})."
            )
            return

        await status.edit_text("📤 Uploading to Telegram…")
        try:
            await msg.reply_video(video=filepath.open("rb"), caption=f"{title}", supports_streaming=True)
        except Exception:
            # Fallback: still deliver as document if streaming fails
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
