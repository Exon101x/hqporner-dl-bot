"""Instant-run HQPorner Telegram Bot (Multi-User Pro Edition) for Kaggle / VPS / Termux.

Features:
- Telethon (MTProto) for instant 2 GB uploads without compiling telegram-bot-api
- Per-user rate limiting: command cooldown, download cooldown, per-user concurrency cap, & daily quota
- Admin suite (ADMIN_USER_IDS): bypass limits, /ban, /unban, /users, /broadcast, /cancelall, /queue
- Modern Telegram HTML <blockquote> cards, in-place menu navigation, & compact numbered grids
- Preview thumbnail cards with lazy Video hydration + HTML og:image fallback
- Animated live download & upload progress cards (▰▱ bar, spinner, size, speed, ETA, cancel)
- Rich /settings (Default Quality, Auto-Download, Video/Document mode, Thumbnails, Spoiler, Page Size)
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from html import escape
import inspect
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlparse, urlunparse
import urllib.request

from hqporner_api import Client, DownloadConfigRAW
from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.types import DocumentAttributeVideo

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hqporner_bot")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
API_ID = int(os.environ["TELEGRAM_API_ID"].strip())
API_HASH = os.environ["TELEGRAM_API_HASH"].strip()
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "2000"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
MAX_USER_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_USER_CONCURRENT_DOWNLOADS", "1"))
COMMAND_COOLDOWN_SEC = float(os.getenv("COMMAND_COOLDOWN_SEC", "3"))
DOWNLOAD_COOLDOWN_SEC = float(os.getenv("DOWNLOAD_COOLDOWN_SEC", "30"))
DAILY_DOWNLOAD_LIMIT = int(os.getenv("DAILY_DOWNLOAD_LIMIT", "15"))  # 0 = unlimited
DEFAULT_PAGE_SIZE = int(os.getenv("PAGE_SIZE", "5"))
MAX_FETCH_RESULTS = int(os.getenv("MAX_FETCH_RESULTS", "25"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "").strip() or None
DATA_FILE = Path(os.getenv("BOT_DATA_FILE", "bot_data.json"))

if DOWNLOAD_DIR:
    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)


def _parse_id_set(env_name: str) -> set[int]:
    raw = os.getenv(env_name, "").strip()
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip().isdigit()}


ALLOWED_USER_IDS = _parse_id_set("ALLOWED_USER_IDS")
ADMIN_USER_IDS = _parse_id_set("ADMIN_USER_IDS")

HQPORNER_URL_RE = re.compile(
    r"(?:https?://)?(?:(?:www|m)\.)?hqporner\.com/[^\s<>\"']+",
    re.IGNORECASE,
)
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
QUALITY_CHOICES = ("best", "1080", "720", "480", "360")
PAGE_SIZE_CHOICES = (3, 5, 8, 10)
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://hqporner.com/",
}

api_client = Client()
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
active_jobs: dict[str, dict[str, Any]] = {}
bot_start_time = time.monotonic()
_logged_video_schema = False


# ---------------------------------------------------------------------------
# Persistent User Store & Rate Limiter
# ---------------------------------------------------------------------------


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class UserDataStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "users": {},
            "banned": [],
            "stats": {"total_downloads": 0, "total_bytes": 0},
        }
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load %s: %s", self.path, exc)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save %s: %s", self.path, exc)

    def is_banned(self, user_id: int) -> bool:
        return user_id in set(self.data.get("banned", []))

    def set_banned(self, user_id: int, banned: bool) -> None:
        current = set(self.data.get("banned", []))
        if banned:
            current.add(user_id)
        else:
            current.discard(user_id)
        self.data["banned"] = sorted(current)
        self.save()

    def user(self, user_id: int) -> dict[str, Any]:
        uid = str(user_id)
        users = self.data.setdefault("users", {})
        u = users.setdefault(uid, {})
        u.setdefault("favorites", [])
        u.setdefault("history", [])
        u.setdefault("default_quality", "best")
        u.setdefault("auto_download", False)
        u.setdefault("send_as_document", False)
        u.setdefault("show_thumbnails", True)
        u.setdefault("spoiler_blur", False)
        u.setdefault("page_size", DEFAULT_PAGE_SIZE)
        u.setdefault("daily_date", today_utc())
        u.setdefault("daily_count", 0)
        u.setdefault("total_downloads", 0)
        if u["daily_date"] != today_utc():
            u["daily_date"] = today_utc()
            u["daily_count"] = 0
        return u

    def touch_user(self, user_id: int) -> dict[str, Any]:
        u = self.user(user_id)
        u["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return u

    def is_favorite(self, user_id: int, url: str) -> bool:
        norm = normalize_url(url)
        return any(item.get("url") == norm for item in self.user(user_id)["favorites"])

    def toggle_favorite(self, user_id: int, meta: dict[str, Any]) -> bool:
        norm = normalize_url(meta["url"])
        favs = self.user(user_id)["favorites"]
        for idx, item in enumerate(favs):
            if item.get("url") == norm:
                favs.pop(idx)
                self.save()
                return False
        favs.insert(
            0,
            {
                "title": meta["title"][:120],
                "url": norm,
                "duration": meta.get("duration", "N/A"),
                "views": meta.get("views", "N/A"),
            },
        )
        del favs[50:]
        self.save()
        return True

    def record_download(self, user_id: int, title: str, url: str, quality: str, size_bytes: int) -> None:
        u = self.user(user_id)
        u["daily_count"] = int(u.get("daily_count", 0)) + 1
        u["total_downloads"] = int(u.get("total_downloads", 0)) + 1
        hist = u["history"]
        hist.insert(
            0,
            {
                "title": title[:100],
                "url": normalize_url(url),
                "quality": quality,
                "size": format_bytes(size_bytes),
                "at": datetime.now(timezone.utc).strftime("%d %b %H:%M UTC"),
            },
        )
        del hist[20:]
        stats = self.data.setdefault("stats", {"total_downloads": 0, "total_bytes": 0})
        stats["total_downloads"] = int(stats.get("total_downloads", 0)) + 1
        stats["total_bytes"] = int(stats.get("total_bytes", 0)) + int(size_bytes)
        self.save()


class RateLimiter:
    """Enforces per-user command cooldowns, download cooldowns, concurrency caps, and daily quotas."""

    def __init__(self) -> None:
        self._last_cmd: dict[int, float] = {}
        self._last_dl: dict[int, float] = {}

    def check_command(self, user_id: int) -> float:
        if user_id in ADMIN_USER_IDS or COMMAND_COOLDOWN_SEC <= 0:
            return 0.0
        now = time.monotonic()
        rem = COMMAND_COOLDOWN_SEC - (now - self._last_cmd.get(user_id, 0.0))
        if rem > 0:
            return rem
        self._last_cmd[user_id] = now
        return 0.0

    def check_download(self, user_id: int) -> str | None:
        if user_id in ADMIN_USER_IDS:
            return None

        user_active = sum(
            1 for job in active_jobs.values()
            if job["user_id"] == user_id and not job["task"].done()
        )
        if user_active >= MAX_USER_CONCURRENT_DOWNLOADS:
            return (
                f"⏳ You already have <b>{user_active}/{MAX_USER_CONCURRENT_DOWNLOADS}</b> "
                "active download(s). Wait for it to finish or cancel it first."
            )

        if DAILY_DOWNLOAD_LIMIT > 0:
            u = store.user(user_id)
            if int(u["daily_count"]) >= DAILY_DOWNLOAD_LIMIT:
                return (
                    f"🚫 Daily download limit reached (<code>{u['daily_count']}/{DAILY_DOWNLOAD_LIMIT}</code>).\n"
                    "Quota resets at <code>00:00 UTC</code>."
                )

        if DOWNLOAD_COOLDOWN_SEC > 0:
            now = time.monotonic()
            rem = DOWNLOAD_COOLDOWN_SEC - (now - self._last_dl.get(user_id, 0.0))
            if rem > 0:
                return f"⏳ Slow down! Please wait <code>{int(rem) + 1}s</code> before starting another download."
            self._last_dl[user_id] = now

        return None


store = UserDataStore(DATA_FILE)
rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Formatting Helpers & Bounded Caches
# ---------------------------------------------------------------------------


def normalize_url(raw_url: str) -> str:
    cleaned = raw_url.strip().rstrip(".,);]>")
    if not cleaned.lower().startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"
    parsed = urlparse(cleaned)
    host = parsed.netloc.lower()
    if host in {"m.hqporner.com", "www.hqporner.com", "hqporner.com"}:
        host = "hqporner.com"
    return urlunparse(("https", host, parsed.path, "", parsed.query, ""))


def title_from_url_slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    slug = re.sub(r"\.html?$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"^\d+[-_]", "", slug)
    cleaned = re.sub(r"[_-]+", " ", slug).strip()
    return cleaned.title() if cleaned else "Untitled Video"


def format_bytes(num_bytes: float) -> str:
    units = ("B", "KB", "MB", "GB")
    size = max(0.0, float(num_bytes))
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}" if unit in {"MB", "GB"} else f"{size:.0f} {unit}"
        size /= 1024.0
    return f"{size:.2f} GB"


def format_duration_sec(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def render_bar(pct: float, width: int = 12) -> str:
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * width))
    return "▰" * filled + "▱" * (width - filled)


def render_pulse_bar(tick: int, width: int = 12) -> str:
    pos = tick % width
    chars = ["▱"] * width
    for offset in range(3):
        chars[(pos + offset) % width] = "▰"
    return "".join(chars)


def read_memory_usage() -> str:
    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        if total > 0:
            return f"{format_bytes(max(0, total - avail))} / {format_bytes(total)}"
    except Exception:  # noqa: BLE001
        pass
    return "N/A"


class BoundedCache:
    def __init__(self, prefix: str, max_size: int = 500) -> None:
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._prefix = prefix
        self._max_size = max_size
        self._counter = 0

    def put(self, value: Any) -> str:
        self._counter += 1
        token = f"{self._prefix}{self._counter:x}"
        self._store[token] = value
        self._store.move_to_end(token)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)
        return token

    def get(self, token: str) -> Any | None:
        return self._store.get(token)


url_store = BoundedCache("v", max_size=800)
page_store = BoundedCache("p", max_size=150)


def is_admin(sender_id: int | None) -> bool:
    return sender_id is not None and sender_id in ADMIN_USER_IDS


def is_authorized(sender_id: int | None) -> bool:
    if sender_id is None:
        return False
    if is_admin(sender_id):
        return True
    if store.is_banned(sender_id):
        return False
    if not ALLOWED_USER_IDS:
        return True
    return sender_id in ALLOWED_USER_IDS


async def check_access_and_rate(event: Any, is_callback: bool = False) -> bool:
    uid = event.sender_id
    if not is_authorized(uid):
        msg = "🚫 You are banned or not authorized to use this bot."
        if is_callback:
            await event.answer(msg, alert=True)
        return False
    store.touch_user(uid)
    rem = rate_limiter.check_command(uid)
    if rem > 0:
        notice = f"⏳ Please wait {rem:.1f}s…"
        if is_callback:
            await event.answer(notice, alert=False)
        else:
            await event.respond(notice)
        return False
    return True


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    try:
        await target.edit(text, **kwargs)
    except MessageNotModifiedError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("safe_edit skipped: %s", exc)


async def send_file_compat(bot: TelegramClient, chat_id: int, file_path: str, **kwargs: Any) -> Any:
    try:
        return await bot.send_file(chat_id, file_path, **kwargs)
    except TypeError:
        kwargs.pop("spoiler", None)
        return await bot.send_file(chat_id, file_path, **kwargs)


async def resolve_attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if not hasattr(obj, name):
            continue
        try:
            value = getattr(obj, name)
            if callable(value):
                try:
                    sig = inspect.signature(value)
                    required = [
                        p
                        for p in sig.parameters.values()
                        if p.default is inspect.Parameter.empty
                        and p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY,
                        )
                    ]
                    if required:
                        continue
                except (TypeError, ValueError):
                    pass
                value = value()
            if inspect.isawaitable(value):
                value = await value
            if value is not None and value != "" and value != [] and value != {}:
                return value
        except Exception as exc:  # noqa: BLE001
            logger.debug("resolve_attr(%s) failed: %s", name, exc)
    return default


async def collect_videos(result: Any, limit: int) -> list[Any]:
    if inspect.isawaitable(result):
        result = await result
    videos: list[Any] = []
    if hasattr(result, "__aiter__"):
        async for item in result:
            videos.append(item)
            if len(videos) >= limit:
                break
    elif isinstance(result, (list, tuple)):
        videos = list(result[:limit])
    elif result is not None:
        try:
            for item in result:
                videos.append(item)
                if len(videos) >= limit:
                    break
        except TypeError:
            videos = [result]
    return videos


def _normalize_list_field(val: Any, limit: int = 5) -> str:
    if not val:
        return ""
    if isinstance(val, (list, tuple, set)):
        items = [str(x).strip() for x in val if str(x).strip()]
        return ", ".join(items[:limit])
    return str(val).strip()


def _extract_thumb_url(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, dict):
        for v in val.values():
            found = _extract_thumb_url(v)
            if found:
                return found
        return ""
    if isinstance(val, (list, tuple)):
        for item in val:
            found = _extract_thumb_url(item)
            if found:
                return found
        return ""
    s = str(val).strip()
    if s.startswith("//"):
        return f"https:{s}"
    return s if s.startswith(("http://", "https://")) else ""


def _scrape_thumb_from_html(html_text: str) -> str:
    if not html_text:
        return ""
    patterns = (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'poster=["\']([^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
        r'(?:https?:)?//[^"\'\s<>]+/(?:main|\d+)\.(?:jpg|jpeg|webp)',
    )
    for pat in patterns:
        m = re.search(pat, html_text, re.IGNORECASE)
        if m:
            raw = m.group(1) if m.lastindex else m.group(0)
            url = _extract_thumb_url(raw)
            if url:
                return url
    return ""


def fetch_page_thumb_sync(page_url: str) -> str:
    if not page_url:
        return ""
    try:
        req = urllib.request.Request(page_url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html_text = resp.read().decode("utf-8", errors="ignore")
        return _scrape_thumb_from_html(html_text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Fallback HTML thumbnail scrape failed for %s: %s", page_url, exc)
        return ""


async def extract_meta(video: Any, hydrate: bool = True, need_thumb: bool = False) -> dict[str, Any]:
    global _logged_video_schema

    if isinstance(video, dict) and "url" in video and "title" in video:
        return {
            "title": str(video.get("title") or title_from_url_slug(video["url"])),
            "url": normalize_url(str(video["url"])) if video.get("url") else "",
            "duration": str(video.get("duration", "N/A")),
            "views": str(video.get("views", "N/A")),
            "publish_date": str(video.get("publish_date", "N/A")),
            "rating": str(video.get("rating", "")),
            "actresses": str(video.get("actresses", "")),
            "tags": str(video.get("tags", "")),
            "qualities": list(video.get("qualities", [])),
            "thumb_url": str(video.get("thumb_url", "")),
        }

    if isinstance(video, str):
        norm = normalize_url(video)
        if hydrate:
            try:
                video = await api_client.get_video(norm)
            except Exception:  # noqa: BLE001
                return {
                    "title": title_from_url_slug(norm),
                    "url": norm,
                    "duration": "N/A",
                    "views": "N/A",
                    "publish_date": "N/A",
                    "rating": "",
                    "actresses": "",
                    "tags": "",
                    "qualities": [],
                    "thumb_url": "",
                }

    if not _logged_video_schema:
        _logged_video_schema = True
        public_attrs = [a for a in dir(video) if not a.startswith("_")]
        logger.info("Detected Video object type=%s attrs=%s", type(video).__name__, public_attrs)

    url = str(await resolve_attr(video, "url", "video_url", "link", default=""))
    norm_url = normalize_url(url) if url else ""
    title = await resolve_attr(video, "title", "name", default="")

    if hydrate and not title and norm_url:
        try:
            video = await api_client.get_video(norm_url)
            title = await resolve_attr(video, "title", "name", default="")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Hydrating lazy video %s failed: %s", norm_url, exc)

    if not title and norm_url:
        title = title_from_url_slug(norm_url)

    duration = await resolve_attr(video, "length", "duration", "video_length", default="N/A")
    views = await resolve_attr(video, "views", "view_count", default="N/A")
    publish_date = await resolve_attr(video, "publish_date", "date", "upload_date", default="N/A")
    rating = await resolve_attr(video, "rating", default="")
    actresses = await resolve_attr(video, "pornstars", "actresses", "models", "actors", default=None)
    tags = await resolve_attr(video, "tags", "categories", default=None)
    qualities = await resolve_attr(video, "video_qualities", "qualities", "quality", default=None)

    raw_thumb = await resolve_attr(
        video,
        "get_thumbnails",
        "thumbnails",
        "image_url",
        "thumbnail",
        "thumb",
        "image",
        "preview_image",
        "poster",
        default="",
    )
    thumb_url = _extract_thumb_url(raw_thumb)

    if not thumb_url and need_thumb:
        html_content = await resolve_attr(video, "html_content", "html", default="")
        if isinstance(html_content, str) and html_content:
            thumb_url = _scrape_thumb_from_html(html_content)
        if not thumb_url and norm_url:
            thumb_url = await asyncio.to_thread(fetch_page_thumb_sync, norm_url)

    q_list: list[str] = []
    if isinstance(qualities, (list, tuple, set)):
        q_list = [str(q).strip() for q in qualities if str(q).strip()]
    elif qualities:
        q_list = [str(qualities).strip()]

    return {
        "title": str(title or "Untitled Video"),
        "url": norm_url,
        "duration": str(duration),
        "views": str(views),
        "publish_date": str(publish_date),
        "rating": str(rating).strip() if rating else "",
        "actresses": _normalize_list_field(actresses, limit=4),
        "tags": _normalize_list_field(tags, limit=6),
        "qualities": q_list,
        "thumb_url": thumb_url,
    }


def download_thumbnail_file(thumb_url: str, dest: Path) -> Path | None:
    if not thumb_url:
        return None
    try:
        req = urllib.request.Request(thumb_url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
        if data and len(data) > 500:
            dest.write_bytes(data)
            return dest
    except Exception as exc:  # noqa: BLE001
        logger.warning("Thumbnail download failed (%s): %s", thumb_url, exc)
    return None


def probe_video_info(file_path: Path, thumb_out: Path) -> tuple[int, int, int, Path | None]:
    duration, width, height = 0, 0, 0
    if shutil.which("ffprobe"):
        try:
            out = subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height:format=duration",
                    "-of",
                    "json",
                    str(file_path),
                ],
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            info = json.loads(out.decode("utf-8", errors="ignore"))
            streams = info.get("streams") or []
            if streams:
                width = int(streams[0].get("width") or 0)
                height = int(streams[0].get("height") or 0)
            fmt = info.get("format") or {}
            if fmt.get("duration"):
                duration = int(float(fmt["duration"]))
        except Exception:  # noqa: BLE001
            pass

    generated_thumb: Path | None = None
    if not thumb_out.exists() and shutil.which("ffmpeg"):
        try:
            seek = max(1, min(10, duration // 5)) if duration else 3
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(seek),
                    "-i",
                    str(file_path),
                    "-vframes",
                    "1",
                    "-vf",
                    "scale=320:-1",
                    str(thumb_out),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=25,
                check=False,
            )
            if thumb_out.exists() and thumb_out.stat().st_size > 500:
                generated_thumb = thumb_out
        except Exception:  # noqa: BLE001
            pass
    elif thumb_out.exists():
        generated_thumb = thumb_out

    return duration, width, height, generated_thumb


def find_video_file(directory: Path) -> Path | None:
    candidates = [
        p for p in directory.rglob("*")
        if p.is_file() and (p.suffix.lower() in VIDEO_EXTENSIONS or p.stat().st_size > 0)
    ]
    if not candidates:
        return None
    video_files = [p for p in candidates if p.suffix.lower() in VIDEO_EXTENSIONS]
    pool = video_files if video_files else candidates
    return max(pool, key=lambda p: p.stat().st_size)


def dir_total_bytes(directory: Path) -> int:
    total = 0
    for p in directory.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# UI Templates (Telegram HTML <blockquote> Cards & Compact Grids)
# ---------------------------------------------------------------------------


def render_home_payload(user_id: int) -> tuple[str, list[list[Any]]]:
    u = store.user(user_id)
    send_mode = "Document" if u["send_as_document"] else "Video"
    quota_str = (
        "Unlimited (Admin)"
        if is_admin(user_id) or DAILY_DOWNLOAD_LIMIT <= 0
        else f"{u['daily_count']}/{DAILY_DOWNLOAD_LIMIT} today"
    )
    text = (
        "✨ <b>HQPorner Bot</b>  <code>2 GB MTProto</code>\n\n"
        "<blockquote>"
        "📎 Paste 1–5 <b>hqporner.com</b> or <b>m.hqporner.com</b> links directly.\n\n"
        "<b>Commands</b>\n"
        "• <code>/search &lt;query&gt;</code> — Search videos\n"
        "• <code>/category &lt;name&gt;</code> • <code>/actress &lt;name&gt;</code>\n"
        "• <code>/top</code> • <code>/random</code> • <code>/brazzers</code> • <code>/queue</code>\n\n"
        f"🎞 <code>{escape(u['default_quality'].upper())}</code>  •  "
        f"📤 <code>{send_mode}</code>  •  "
        f"📊 <code>{quota_str}</code>"
        "</blockquote>"
    )
    buttons = [
        [
            Button.inline("🔥 Top Week", data=b"menu:top:week"),
            Button.inline("🏆 Top Month", data=b"menu:top:month"),
        ],
        [
            Button.inline("🎲 Random", data=b"menu:random"),
            Button.inline("💎 Brazzers", data=b"menu:brazzers"),
        ],
        [
            Button.inline("⭐ Favorites", data=b"menu:favorites"),
            Button.inline("🕒 History", data=b"menu:history"),
        ],
        [
            Button.inline("⚙️ Settings", data=b"menu:settings"),
            Button.inline("📊 Status", data=b"menu:stats"),
        ],
        [
            Button.inline("📋 Active Queue", data=b"menu:queue"),
            Button.inline("👤 My Profile", data=b"menu:profile"),
        ],
    ]
    return text, buttons


def render_profile_payload(user_id: int) -> tuple[str, list[list[Any]]]:
    u = store.user(user_id)
    role = "👑 Admin (No Limits)" if is_admin(user_id) else "👤 Standard User"
    quota = (
        "Unlimited"
        if is_admin(user_id) or DAILY_DOWNLOAD_LIMIT <= 0
        else f"{u['daily_count']} / {DAILY_DOWNLOAD_LIMIT} used today"
    )
    text = (
        "👤 <b>Your Profile</b>\n\n"
        "<blockquote>"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"🛡 <b>Role:</b> <code>{role}</code>\n"
        f"📅 <b>Daily Quota:</b> <code>{quota}</code>\n"
        f"📥 <b>Total Downloads:</b> <code>{u['total_downloads']}</code>\n"
        f"⭐ <b>Saved Favorites:</b> <code>{len(u['favorites'])}</code>\n"
        f"⏱ <b>Cooldown:</b> <code>{int(DOWNLOAD_COOLDOWN_SEC)}s</code> • "
        f"<b>Max Concurrent:</b> <code>{MAX_USER_CONCURRENT_DOWNLOADS}</code>"
        "</blockquote>"
    )
    return text, [[Button.inline("⚙️ Settings", data=b"menu:settings"), Button.inline("⬅️ Home", data=b"menu:home")]]


def render_queue_payload(user_id: int) -> tuple[str, list[list[Any]]]:
    running = [
        (jid, info) for jid, info in active_jobs.items() if not info["task"].done()
    ]
    if not running:
        text = (
            "📋 <b>Active Download Queue</b>\n\n"
            f"<blockquote>No active downloads right now.\n"
            f"Slots available: <code>{MAX_CONCURRENT_DOWNLOADS} / {MAX_CONCURRENT_DOWNLOADS}</code></blockquote>"
        )
        return text, [[Button.inline("🔄 Refresh", data=b"menu:queue"), Button.inline("⬅️ Home", data=b"menu:home")]]

    lines = []
    for idx, (jid, info) in enumerate(running, start=1):
        owner = "You" if info["user_id"] == user_id else f"User <code>{info['user_id']}</code>"
        elapsed = format_duration_sec(time.monotonic() - info["started_at"])
        lines.append(
            f"<b>{idx}.</b> {escape(info['label'][:42])}\n"
            f"    {owner}  •  <code>{escape(info['quality'].upper())}</code>  •  ⏱ <code>{elapsed}</code>"
        )

    text = (
        f"📋 <b>Active Queue</b>  <code>{len(running)}/{MAX_CONCURRENT_DOWNLOADS}</code>\n\n"
        f"<blockquote>" + "\n\n".join(lines) + "</blockquote>"
    )
    return text, [[Button.inline("🔄 Refresh", data=b"menu:queue"), Button.inline("⬅️ Home", data=b"menu:home")]]


def format_video_card(meta: dict[str, Any]) -> str:
    stats = [f"⏱ <code>{escape(meta['duration'])}</code>"]
    if meta["views"] != "N/A":
        stats.append(f"👁 <code>{escape(meta['views'])}</code>")
    if meta["rating"]:
        stats.append(f"⭐ <code>{escape(meta['rating'])}</code>")

    details = ["  •  ".join(stats)]
    if meta["publish_date"] != "N/A":
        details.append(f"📅 <b>Date:</b> <code>{escape(meta['publish_date'])}</code>")
    if meta["actresses"]:
        details.append(f"👤 <b>Cast:</b> {escape(meta['actresses'])}")
    if meta["tags"]:
        details.append(f"🏷 <b>Tags:</b> <i>{escape(meta['tags'])}</i>")
    if meta["qualities"]:
        details.append(f"🎞 <b>Available:</b> <code>{escape(', '.join(meta['qualities']))}</code>")

    return (
        f"🎬 <b>{escape(meta['title'])}</b>\n\n"
        f"<blockquote>" + "\n".join(details) + "</blockquote>\n"
        "⬇️ <i>Choose quality to download &amp; upload:</i>"
    )


def download_buttons(meta: dict[str, Any], user_id: int) -> list[list[Any]]:
    video_url = meta["url"]
    token = url_store.put(meta)
    saved = store.is_favorite(user_id, video_url)
    fav_label = "💔 Saved" if saved else "⭐ Save"

    return [
        [
            Button.inline("⚡ Best", data=f"dl:{token}:best".encode()),
            Button.inline("1080p", data=f"dl:{token}:1080".encode()),
            Button.inline("720p", data=f"dl:{token}:720".encode()),
        ],
        [
            Button.inline("480p", data=f"dl:{token}:480".encode()),
            Button.inline("360p", data=f"dl:{token}:360".encode()),
            Button.inline(fav_label, data=f"fav:{token}".encode()),
        ],
        [
            Button.url("🌐 Open on HQPorner", video_url),
        ],
    ]


async def send_details(bot: TelegramClient, chat_id: int, user_id: int, video: Any) -> None:
    u = store.user(user_id)
    want_thumb = bool(u["show_thumbnails"])
    meta = await extract_meta(video, hydrate=True, need_thumb=want_thumb)
    card = format_video_card(meta)
    buttons = download_buttons(meta, user_id) if meta["url"] else None

    if want_thumb and meta["thumb_url"] and len(card) <= 1020:
        with tempfile.TemporaryDirectory(prefix="hqthumb_") as tmpdir:
            thumb_file = await asyncio.to_thread(
                download_thumbnail_file, meta["thumb_url"], Path(tmpdir) / "preview.jpg"
            )
            if thumb_file is not None:
                try:
                    await send_file_compat(
                        bot,
                        chat_id,
                        str(thumb_file),
                        caption=card,
                        parse_mode="html",
                        buttons=buttons,
                        spoiler=bool(u["spoiler_blur"]),
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Sending preview image failed, falling back to text: %s", exc)

    await bot.send_message(chat_id, card, parse_mode="html", buttons=buttons, link_preview=True)


async def render_page_payload(page_token: str, page_idx: int, user_id: int) -> tuple[str, list[list[Any]]]:
    entry = page_store.get(page_token)
    if not entry:
        return "⚠️ This list expired. Run the command again.", [[Button.inline("🏠 Home", data=b"menu:home")]]

    u = store.user(user_id)
    page_size = int(u.get("page_size", DEFAULT_PAGE_SIZE))
    heading = entry["heading"]
    raw_items: list[Any] = entry["items"]
    total = len(raw_items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page_idx = max(0, min(page_idx, total_pages - 1))

    start = page_idx * page_size
    end = min(total, start + page_size)

    hydrated_slice = await asyncio.gather(
        *(extract_meta(raw_items[i], hydrate=True, need_thumb=False) for i in range(start, end))
    )
    for offset, meta in enumerate(hydrated_slice):
        raw_items[start + offset] = meta

    default_q = u["default_quality"]
    item_lines: list[str] = []
    preview_btns: list[Any] = []
    quick_dl_btns: list[Any] = []

    for offset, meta in enumerate(hydrated_slice):
        num = start + offset + 1
        title = meta["title"][:54] + ("…" if len(meta["title"]) > 54 else "")
        sub = f"⏱ <code>{escape(meta.get('duration', 'N/A'))}</code>"
        if meta.get("views", "N/A") != "N/A":
            sub += f"  •  👁 <code>{escape(meta['views'])}</code>"
        item_lines.append(f"<b>{num:02d}.</b> {escape(title)}\n      {sub}")

        if meta.get("url"):
            token = url_store.put(meta)
            preview_btns.append(Button.inline(f"🔎 {num}", data=f"info:{token}".encode()))
            quick_dl_btns.append(Button.inline(f"⚡ {num}", data=f"dl:{token}:{default_q}".encode()))

    text = (
        f"<b>{escape(heading)}</b>\n\n"
        f"<blockquote>" + "\n\n".join(item_lines) + "</blockquote>\n"
        f"<i>🔎 Preview  •  ⚡ Quick Download (<code>{escape(default_q.upper())}</code>)</i>"
    )

    buttons: list[list[Any]] = []
    for i in range(0, len(preview_btns), 5):
        buttons.append(preview_btns[i : i + 5])
    for i in range(0, len(quick_dl_btns), 5):
        buttons.append(quick_dl_btns[i : i + 5])

    nav_row: list[Any] = []
    if page_idx > 0:
        nav_row.append(Button.inline("⬅️ Prev", data=f"pg:{page_token}:{page_idx - 1}".encode()))
    nav_row.append(Button.inline(f"📄 {page_idx + 1}/{total_pages}", data=b"noop"))
    if page_idx < total_pages - 1:
        nav_row.append(Button.inline("Next ➡️", data=f"pg:{page_token}:{page_idx + 1}".encode()))
    buttons.append(nav_row)
    buttons.append([Button.inline("🏠 Home", data=b"menu:home")])

    return text, buttons


async def send_paginated_list(
    bot: TelegramClient,
    chat_id: int,
    user_id: int,
    heading: str,
    videos: list[Any],
    edit_msg: Any = None,
) -> None:
    if not videos:
        text = "❌ <b>No videos found.</b>"
        btns = [[Button.inline("🏠 Home", data=b"menu:home")]]
        if edit_msg:
            await safe_edit(edit_msg, text, parse_mode="html", buttons=btns)
        else:
            await bot.send_message(chat_id, text, parse_mode="html", buttons=btns)
        return

    page_token = page_store.put({"heading": heading, "items": list(videos)})
    text, buttons = await render_page_payload(page_token, 0, user_id)
    if edit_msg:
        await safe_edit(edit_msg, text, parse_mode="html", buttons=buttons, link_preview=False)
    else:
        await bot.send_message(chat_id, text, parse_mode="html", buttons=buttons, link_preview=False)


def render_settings_payload(user_id: int) -> tuple[str, list[list[Any]]]:
    u = store.user(user_id)
    q = u["default_quality"]
    auto = bool(u["auto_download"])
    as_doc = bool(u["send_as_document"])
    thumbs = bool(u["show_thumbnails"])
    spoiler = bool(u["spoiler_blur"])
    psize = int(u["page_size"])

    text = (
        "⚙️ <b>Bot Settings</b>\n\n"
        "<blockquote>"
        f"🎞 <b>Default Quality:</b> <code>{escape(q.upper())}</code>\n"
        f"📤 <b>Upload Format:</b> <code>{'Document (Raw)' if as_doc else 'Streamable Video'}</code>\n"
        f"⚡ <b>Auto-Download on Paste:</b> <code>{'ON' if auto else 'OFF'}</code>\n"
        f"🖼 <b>Preview Thumbnails:</b> <code>{'ON' if thumbs else 'OFF'}</code>\n"
        f"🔒 <b>Spoiler Blur Cover:</b> <code>{'ON' if spoiler else 'OFF'}</code>\n"
        f"📄 <b>Results Per Page:</b> <code>{psize}</code>"
        "</blockquote>\n"
        "<i>Tap any option below to customize:</i>"
    )
    q_row1 = [
        Button.inline(f"{'● ' if q == c else ''}{c.upper()}", data=f"set:q:{c}".encode())
        for c in ("best", "1080", "720")
    ]
    q_row2 = [
        Button.inline(f"{'● ' if q == c else ''}{c.upper()}", data=f"set:q:{c}".encode())
        for c in ("480", "360")
    ]
    toggles_1 = [
        Button.inline(f"⚡ Auto-DL: {'ON' if auto else 'OFF'}", data=b"set:auto:toggle"),
        Button.inline(f"📤 Mode: {'Doc' if as_doc else 'Video'}", data=b"set:doc:toggle"),
    ]
    toggles_2 = [
        Button.inline(f"🖼 Thumbs: {'ON' if thumbs else 'OFF'}", data=b"set:thumb:toggle"),
        Button.inline(f"🔒 Spoiler: {'ON' if spoiler else 'OFF'}", data=b"set:spoiler:toggle"),
    ]
    misc_row = [
        Button.inline(f"📄 Per Page: {psize}", data=b"set:psize:next"),
        Button.inline("🧹 Clear History", data=b"set:clear:hist"),
        Button.inline("🗑 Clear Favs", data=b"set:clear:favs"),
    ]
    return text, [
        q_row1,
        q_row2,
        toggles_1,
        toggles_2,
        misc_row,
        [Button.inline("⬅️ Back to Home", data=b"menu:home")],
    ]


def render_stats_payload() -> tuple[str, list[list[Any]]]:
    uptime = format_duration_sec(time.monotonic() - bot_start_time)
    disk_target = Path(DOWNLOAD_DIR) if DOWNLOAD_DIR else Path(".")
    try:
        disk_target.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(disk_target)
        disk_line = f"<code>{format_bytes(disk.free)}</code> free / <code>{format_bytes(disk.total)}</code>"
    except Exception:  # noqa: BLE001
        disk = shutil.disk_usage(".")
        disk_line = f"<code>{format_bytes(disk.free)}</code> free / <code>{format_bytes(disk.total)}</code>"

    ram_line = read_memory_usage()
    stats = store.data.get("stats", {})
    total_dl = int(stats.get("total_downloads", 0))
    total_bytes = int(stats.get("total_bytes", 0))
    total_users = len(store.data.get("users", {}))
    active_count = sum(1 for j in active_jobs.values() if not j["task"].done())
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    text = (
        "📊 <b>Bot &amp; Server Status</b>\n\n"
        "<blockquote>"
        f"⏱ <b>Uptime:</b> <code>{uptime}</code>\n"
        f"👥 <b>Users:</b> <code>{total_users}</code>  •  "
        f"🔄 <b>Active Jobs:</b> <code>{active_count}/{MAX_CONCURRENT_DOWNLOADS}</code>\n"
        f"✅ <b>Downloads:</b> <code>{total_dl}</code> (<code>{format_bytes(total_bytes)}</code>)\n"
        f"🧠 <b>RAM Used:</b> <code>{ram_line}</code>\n"
        f"💾 <b>Disk:</b> {disk_line}\n"
        f"🛡 <b>Limits:</b> <code>{int(DOWNLOAD_COOLDOWN_SEC)}s</code> cd • "
        f"<code>{DAILY_DOWNLOAD_LIMIT or '∞'}/day</code>\n"
        f"🚀 <b>Max Upload:</b> <code>{MAX_UPLOAD_MB:.0f} MB</code> • 🕒 <code>{now_str}</code>"
        "</blockquote>"
    )
    buttons = [
        [
            Button.inline("🔄 Refresh", data=b"menu:stats"),
            Button.inline("⬅️ Back to Home", data=b"menu:home"),
        ]
    ]
    return text, buttons


def render_history_payload(user_id: int) -> tuple[str, list[list[Any]]]:
    hist = store.user(user_id)["history"]
    if not hist:
        return (
            "🕒 <b>Recent Downloads</b>\n\n<blockquote>No downloads recorded yet.</blockquote>",
            [[Button.inline("⬅️ Back to Home", data=b"menu:home")]],
        )

    lines: list[str] = []
    open_row: list[Any] = []
    for idx, item in enumerate(hist[:5], start=1):
        t = item["title"][:48] + ("…" if len(item["title"]) > 48 else "")
        lines.append(
            f"<b>{idx:02d}.</b> {escape(t)}\n"
            f"      📦 <code>{escape(item['size'])}</code>  •  🎞 <code>{escape(item['quality'])}</code>  •  <i>{escape(item['at'])}</i>"
        )
        token = url_store.put({"title": item["title"], "url": item["url"]})
        open_row.append(Button.inline(f"🔎 {idx}", data=f"info:{token}".encode()))

    text = "🕒 <b>Recent Downloads</b>\n\n<blockquote>" + "\n\n".join(lines) + "</blockquote>"
    return text, [open_row, [Button.inline("⬅️ Back to Home", data=b"menu:home")]]


# ---------------------------------------------------------------------------
# Fetchers & Download Job
# ---------------------------------------------------------------------------


async def fetch_category(bot: TelegramClient, chat_id: int, user_id: int, category: str) -> None:
    msg = await bot.send_message(chat_id, f"⏳ Loading category <code>{escape(category)}</code>…", parse_mode="html")
    try:
        fn = getattr(api_client, "get_videos_by_category", None)
        if fn is None:
            raise RuntimeError("Client.get_videos_by_category is not available.")
        videos = await collect_videos(fn(category), MAX_FETCH_RESULTS)
        await send_paginated_list(bot, chat_id, user_id, f"📂 Category • {category}", videos, edit_msg=msg)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(msg, f"❌ Failed to fetch category: <code>{escape(str(exc))}</code>", parse_mode="html")


async def fetch_actress(bot: TelegramClient, chat_id: int, user_id: int, actress: str) -> None:
    msg = await bot.send_message(chat_id, f"⏳ Loading <code>{escape(actress)}</code>…", parse_mode="html")
    try:
        fn = getattr(api_client, "get_videos_by_actress", None)
        if fn is None:
            raise RuntimeError("Client.get_videos_by_actress is not available.")
        videos = await collect_videos(fn(actress), MAX_FETCH_RESULTS)
        await send_paginated_list(bot, chat_id, user_id, f"👤 Cast • {actress}", videos, edit_msg=msg)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(msg, f"❌ Failed to fetch actress videos: <code>{escape(str(exc))}</code>", parse_mode="html")


async def fetch_brazzers(bot: TelegramClient, chat_id: int, user_id: int, edit_msg: Any = None) -> None:
    msg = edit_msg or await bot.send_message(chat_id, "⏳ Loading Brazzers videos…")
    if edit_msg:
        await safe_edit(msg, "⏳ Loading Brazzers videos…")
    try:
        fn = getattr(api_client, "get_brazzers_videos", None)
        if fn is None:
            raise RuntimeError("Client.get_brazzers_videos is not available.")
        videos = await collect_videos(fn(), MAX_FETCH_RESULTS)
        await send_paginated_list(bot, chat_id, user_id, "💎 Brazzers", videos, edit_msg=msg)
    except Exception as exc:  # noqa: BLE001
        await safe_edit(msg, f"❌ Failed to fetch Brazzers: <code>{escape(str(exc))}</code>", parse_mode="html")


def spawn_download(
    bot: TelegramClient,
    chat_id: int,
    user_id: int,
    url: str,
    quality: str,
    label: str,
) -> str | None:
    """Validate rate limits & start a tracked download task. Returns error HTML or None."""
    err = rate_limiter.check_download(user_id)
    if err:
        return err
    job_id = f"j{int(time.time() * 1000) % 1000000}_{user_id % 1000}"
    task = asyncio.create_task(run_download_job(bot, chat_id, user_id, url, quality, job_id))
    active_jobs[job_id] = {
        "task": task,
        "user_id": user_id,
        "quality": quality,
        "label": label or title_from_url_slug(url),
        "started_at": time.monotonic(),
    }
    return None


async def handle_site_url(
    bot: TelegramClient,
    chat_id: int,
    user_id: int,
    raw_url: str,
    force_preview: bool = False,
) -> None:
    url = normalize_url(raw_url)
    path = urlparse(url).path.strip("/")
    if path.startswith("category/"):
        slug = path.split("/", 1)[1].strip("/")
        if slug:
            await fetch_category(bot, chat_id, user_id, slug)
            return
    if path.startswith("actress/"):
        slug = path.split("/", 1)[1].strip("/")
        if slug:
            await fetch_actress(bot, chat_id, user_id, slug)
            return

    u = store.user(user_id)
    if u.get("auto_download") and not force_preview:
        err = spawn_download(bot, chat_id, user_id, url, u["default_quality"], title_from_url_slug(url))
        if err:
            await bot.send_message(chat_id, err, parse_mode="html")
        return

    msg = await bot.send_message(chat_id, "⏳ <b>Loading preview &amp; details…</b>", parse_mode="html")
    try:
        video = await api_client.get_video(url)
        await msg.delete()
        await send_details(bot, chat_id, user_id, video)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to fetch video %s", url)
        await safe_edit(msg, f"❌ Failed to fetch video: <code>{escape(str(exc))}</code>", parse_mode="html")


async def run_download_job(
    bot: TelegramClient,
    chat_id: int,
    user_id: int,
    url: str,
    quality: str,
    job_id: str,
) -> None:
    u = store.user(user_id)
    cancel_btn = [[Button.inline("✖ Cancel", data=f"cancel:{job_id}".encode())]]
    status = await bot.send_message(
        chat_id,
        f"🕒 <b>Queued</b>  •  <code>{escape(quality.upper())}</code>",
        parse_mode="html",
        buttons=cancel_btn,
    )

    try:
        async with download_semaphore:
            video = await api_client.get_video(normalize_url(url))
            meta = await extract_meta(video, hydrate=True, need_thumb=True)
            title = meta["title"]
            if job_id in active_jobs:
                active_jobs[job_id]["label"] = title
            short_title = title[:52] + ("…" if len(title) > 52 else "")

            if DOWNLOAD_DIR:
                Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(prefix="hqbot_", dir=DOWNLOAD_DIR) as tmpdir:
                tmp_path = Path(tmpdir)
                dl_start = time.monotonic()
                stop_poll = asyncio.Event()

                async def poll_download_progress() -> None:
                    tick = 0
                    prev_bytes = 0
                    prev_time = dl_start
                    while not stop_poll.is_set():
                        await asyncio.sleep(2.5)
                        if stop_poll.is_set():
                            break
                        tick += 1
                        cur_bytes = dir_total_bytes(tmp_path)
                        now = time.monotonic()
                        elapsed = now - dl_start
                        dt = max(0.1, now - prev_time)
                        inst_speed = max(0.0, (cur_bytes - prev_bytes) / dt)
                        avg_speed = cur_bytes / max(0.1, elapsed)
                        speed = inst_speed if inst_speed > 0 else avg_speed
                        prev_bytes, prev_time = cur_bytes, now

                        spin = SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]
                        bar = render_pulse_bar(tick)
                        text = (
                            f"{spin} <b>Downloading</b>  •  <code>{escape(quality.upper())}</code>\n\n"
                            f"<blockquote>"
                            f"🎬 <b>{escape(short_title)}</b>\n"
                            f"<code>{bar}</code>\n"
                            f"📦 <code>{format_bytes(cur_bytes)}</code>  •  "
                            f"⚡ <code>{format_bytes(speed)}/s</code>  •  "
                            f"⏱ <code>{format_duration_sec(elapsed)}</code>"
                            f"</blockquote>"
                        )
                        await safe_edit(status, text, parse_mode="html", buttons=cancel_btn)

                await safe_edit(
                    status,
                    f"⠋ <b>Starting Download</b>  •  <code>{escape(quality.upper())}</code>\n\n"
                    f"<blockquote>🎬 <b>{escape(short_title)}</b></blockquote>",
                    parse_mode="html",
                    buttons=cancel_btn,
                )

                poller = asyncio.create_task(poll_download_progress())
                try:
                    config = DownloadConfigRAW(quality=quality, path=tmpdir)
                    await video.download(config)
                finally:
                    stop_poll.set()
                    poller.cancel()
                    try:
                        await poller
                    except asyncio.CancelledError:
                        pass

                dl_elapsed = max(0.1, time.monotonic() - dl_start)
                file_path = find_video_file(tmp_path)
                if file_path is None:
                    await safe_edit(status, "❌ Download finished, but no video file was found.")
                    return

                file_bytes = file_path.stat().st_size
                size_mb = file_bytes / (1024 * 1024)
                dl_avg_speed = file_bytes / dl_elapsed

                if size_mb > MAX_UPLOAD_MB:
                    await safe_edit(
                        status,
                        f"⚠️ <b>File exceeds limit</b>\n\n"
                        f"<blockquote>Size: <code>{format_bytes(file_bytes)}</code>  •  Limit: <code>{MAX_UPLOAD_MB:.0f} MB</code>\n"
                        "Try a lower quality (720p, 480p, or 360p).</blockquote>",
                        parse_mode="html",
                    )
                    return

                thumb_path = tmp_path / "thumb.jpg"
                if meta["thumb_url"]:
                    await asyncio.to_thread(download_thumbnail_file, meta["thumb_url"], thumb_path)
                vid_dur, vid_w, vid_h, final_thumb = await asyncio.to_thread(
                    probe_video_info, file_path, thumb_path
                )

                up_start = time.monotonic()
                last_edit = 0.0
                tick = 0

                async def upload_progress(current: int, total: int) -> None:
                    nonlocal last_edit, tick
                    now = time.monotonic()
                    if now - last_edit < 2.5 and current < total:
                        return
                    last_edit = now
                    tick += 1
                    elapsed = max(0.1, now - up_start)
                    speed = current / elapsed
                    pct = (current / total * 100.0) if total else 0.0
                    eta = ((total - current) / speed) if speed > 0 and total > current else 0.0
                    spin = SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]
                    bar = render_bar(pct)

                    text = (
                        f"{spin} <b>Uploading</b>  •  <code>{pct:5.1f}%</code>\n\n"
                        f"<blockquote>"
                        f"🎬 <b>{escape(short_title)}</b>\n"
                        f"<code>{bar}</code>\n"
                        f"📦 <code>{format_bytes(current)} / {format_bytes(total)}</code>\n"
                        f"🚀 <code>{format_bytes(speed)}/s</code>  •  "
                        f"⏳ ETA <code>{format_duration_sec(eta)}</code>"
                        f"</blockquote>"
                    )
                    await safe_edit(status, text, parse_mode="html", buttons=cancel_btn)

                await safe_edit(
                    status,
                    f"🚀 <b>Preparing Upload</b>\n\n"
                    f"<blockquote>"
                    f"🎬 <b>{escape(short_title)}</b>\n"
                    f"📦 <code>{format_bytes(file_bytes)}</code>  •  ⬇️ <code>{format_bytes(dl_avg_speed)}/s</code>"
                    f"</blockquote>",
                    parse_mode="html",
                    buttons=cancel_btn,
                )

                as_doc = bool(u["send_as_document"])
                attrs = []
                if not as_doc and (vid_dur or vid_w or vid_h):
                    attrs.append(
                        DocumentAttributeVideo(
                            duration=vid_dur,
                            w=vid_w,
                            h=vid_h,
                            supports_streaming=True,
                        )
                    )

                meta_parts = [
                    f"📦 <code>{format_bytes(file_bytes)}</code>",
                    f"🎞 <code>{escape(quality.upper())}</code>",
                    f"⏱ <code>{escape(meta['duration'])}</code>",
                ]
                if meta["views"] != "N/A":
                    meta_parts.append(f"👁 <code>{escape(meta['views'])}</code>")

                final_caption = (
                    f"🎬 <b>{escape(title[:400])}</b>\n"
                    f"<blockquote>{'  •  '.join(meta_parts)}</blockquote>"
                )

                await send_file_compat(
                    bot,
                    chat_id,
                    str(file_path),
                    caption=final_caption[:1024],
                    parse_mode="html",
                    thumb=str(final_thumb) if final_thumb else None,
                    attributes=attrs or None,
                    force_document=as_doc,
                    supports_streaming=not as_doc,
                    spoiler=bool(u["spoiler_blur"]) and not as_doc,
                    progress_callback=upload_progress,
                )
                store.record_download(user_id, title, url, quality, file_bytes)
                await status.delete()

    except asyncio.CancelledError:
        await safe_edit(status, "🛑 <b>Cancelled.</b>", parse_mode="html")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Download job failed for %s", url)
        await safe_edit(status, f"❌ <b>Failed:</b> <code>{escape(str(exc))}</code>", parse_mode="html")
    finally:
        active_jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# Bot Entrypoint & Handlers
# ---------------------------------------------------------------------------


async def main() -> None:
    bot = TelegramClient("hqporner_bot_session", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info(
        "Bot started as @%s (Multi-User Pro Edition | admins=%s | cd=%ss | daily=%s)",
        me.username,
        sorted(ADMIN_USER_IDS),
        int(DOWNLOAD_COOLDOWN_SEC),
        DAILY_DOWNLOAD_LIMIT,
    )

    @bot.on(events.NewMessage(pattern=r"^/(start|help)(?:@\w+)?$"))
    async def on_start(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        text, buttons = render_home_payload(event.sender_id)
        await event.respond(text, parse_mode="html", buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"^/profile(?:@\w+)?$"))
    async def on_profile(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        text, buttons = render_profile_payload(event.sender_id)
        await event.respond(text, parse_mode="html", buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"^/queue(?:@\w+)?$"))
    async def on_queue(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        text, buttons = render_queue_payload(event.sender_id)
        await event.respond(text, parse_mode="html", buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"^/video(?:@\w+)?(?:\s+(.+))?$"))
    async def on_video(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.respond("Usage: <code>/video &lt;hqporner_site_url&gt;</code>", parse_mode="html")
            return
        await handle_site_url(bot, event.chat_id, event.sender_id, arg, force_preview=True)

    @bot.on(events.NewMessage(pattern=r"^/search(?:@\w+)?(?:\s+(.+))?$"))
    async def on_search(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        query = (event.pattern_match.group(1) or "").strip()
        if not query:
            await event.respond("Usage: <code>/search &lt;query&gt;</code>", parse_mode="html")
            return
        msg = await event.respond(f"🔍 Searching <code>{escape(query)}</code>…", parse_mode="html")
        try:
            videos = await collect_videos(api_client.search_videos(query), MAX_FETCH_RESULTS)
            await send_paginated_list(bot, event.chat_id, event.sender_id, f"🔍 Search • {query}", videos, edit_msg=msg)
        except Exception as exc:  # noqa: BLE001
            await safe_edit(msg, f"❌ Search failed: <code>{escape(str(exc))}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/top(?:@\w+)?(?:\s+(.+))?$"))
    async def on_top(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        timeframe = (event.pattern_match.group(1) or "week").strip().lower()
        msg = await event.respond(f"⏳ Loading top videos (<code>{escape(timeframe)}</code>)…", parse_mode="html")
        try:
            videos = await collect_videos(api_client.get_top_porn(timeframe), MAX_FETCH_RESULTS)
            await send_paginated_list(bot, event.chat_id, event.sender_id, f"🔥 Top • {timeframe.title()}", videos, edit_msg=msg)
        except Exception as exc:  # noqa: BLE001
            await safe_edit(msg, f"❌ Failed: <code>{escape(str(exc))}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/random(?:@\w+)?$"))
    async def on_random(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        msg = await event.respond("🎲 Picking a random video…")
        try:
            video = api_client.get_random_video()
            if inspect.isawaitable(video):
                video = await video
            await msg.delete()
            await send_details(bot, event.chat_id, event.sender_id, video)
        except Exception as exc:  # noqa: BLE001
            await safe_edit(msg, f"❌ Failed: <code>{escape(str(exc))}</code>", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/brazzers(?:@\w+)?$"))
    async def on_brazzers(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        await fetch_brazzers(bot, event.chat_id, event.sender_id)

    @bot.on(events.NewMessage(pattern=r"^/category(?:@\w+)?(?:\s+(.+))?$"))
    async def on_category(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.respond("Usage: <code>/category &lt;name&gt;</code>", parse_mode="html")
            return
        await fetch_category(bot, event.chat_id, event.sender_id, arg)

    @bot.on(events.NewMessage(pattern=r"^/actress(?:@\w+)?(?:\s+(.+))?$"))
    async def on_actress(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.respond("Usage: <code>/actress &lt;name&gt;</code>", parse_mode="html")
            return
        await fetch_actress(bot, event.chat_id, event.sender_id, arg)

    @bot.on(events.NewMessage(pattern=r"^/favorites(?:@\w+)?$"))
    async def on_favorites(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        favs = store.user(event.sender_id)["favorites"]
        if not favs:
            await event.respond("⭐ No favorites saved yet. Tap <b>Save</b> on any video preview!", parse_mode="html")
            return
        await send_paginated_list(bot, event.chat_id, event.sender_id, "⭐ Favorites", favs)

    @bot.on(events.NewMessage(pattern=r"^/history(?:@\w+)?$"))
    async def on_history(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        text, buttons = render_history_payload(event.sender_id)
        await event.respond(text, parse_mode="html", buttons=buttons, link_preview=False)

    @bot.on(events.NewMessage(pattern=r"^/settings(?:@\w+)?$"))
    async def on_settings(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        text, buttons = render_settings_payload(event.sender_id)
        await event.respond(text, parse_mode="html", buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"^/stats(?:@\w+)?$"))
    async def on_stats(event: Any) -> None:
        if not await check_access_and_rate(event):
            return
        text, buttons = render_stats_payload()
        await event.respond(text, parse_mode="html", buttons=buttons)

    # ---- Admin Commands ----

    @bot.on(events.NewMessage(pattern=r"^/admin(?:@\w+)?$"))
    async def on_admin(event: Any) -> None:
        if not is_admin(event.sender_id):
            await event.respond("🚫 Admin only. Set your Telegram user ID in <code>ADMIN_USER_IDS</code>.", parse_mode="html")
            return
        await event.respond(
            "👑 <b>Admin Commands</b>\n\n"
            "<blockquote>"
            "• <code>/users</code> — Total users &amp; top activity\n"
            "• <code>/ban &lt;user_id&gt;</code> — Ban a user\n"
            "• <code>/unban &lt;user_id&gt;</code> — Unban a user\n"
            "• <code>/broadcast &lt;text&gt;</code> — Message all users\n"
            "• <code>/cancelall</code> — Cancel all active jobs\n"
            "• <code>/queue</code> — Inspect active downloads"
            "</blockquote>",
            parse_mode="html",
        )

    @bot.on(events.NewMessage(pattern=r"^/users(?:@\w+)?$"))
    async def on_users(event: Any) -> None:
        if not is_admin(event.sender_id):
            return
        users = store.data.get("users", {})
        banned = store.data.get("banned", [])
        ranked = sorted(
            users.items(),
            key=lambda kv: int(kv[1].get("total_downloads", 0)),
            reverse=True,
        )[:10]
        rows = [
            f"• <code>{uid}</code> — <code>{u.get('total_downloads', 0)}</code> dl (today: <code>{u.get('daily_count', 0)}</code>)"
            for uid, u in ranked
        ]
        body = "\n".join(rows) if rows else "No users yet."
        await event.respond(
            f"👥 <b>Users:</b> <code>{len(users)}</code>  •  🚫 <b>Banned:</b> <code>{len(banned)}</code>\n\n"
            f"<blockquote>{body}</blockquote>",
            parse_mode="html",
        )

    @bot.on(events.NewMessage(pattern=r"^/ban(?:@\w+)?(?:\s+(\d+))?$"))
    async def on_ban(event: Any) -> None:
        if not is_admin(event.sender_id):
            return
        target = event.pattern_match.group(1)
        if not target:
            await event.respond("Usage: <code>/ban &lt;user_id&gt;</code>", parse_mode="html")
            return
        uid = int(target)
        if is_admin(uid):
            await event.respond("⚠️ Cannot ban an admin.")
            return
        store.set_banned(uid, True)
        await event.respond(f"🚫 Banned user <code>{uid}</code>.", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/unban(?:@\w+)?(?:\s+(\d+))?$"))
    async def on_unban(event: Any) -> None:
        if not is_admin(event.sender_id):
            return
        target = event.pattern_match.group(1)
        if not target:
            await event.respond("Usage: <code>/unban &lt;user_id&gt;</code>", parse_mode="html")
            return
        uid = int(target)
        store.set_banned(uid, False)
        await event.respond(f"✅ Unbanned user <code>{uid}</code>.", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/cancelall(?:@\w+)?$"))
    async def on_cancelall(event: Any) -> None:
        if not is_admin(event.sender_id):
            return
        count = 0
        for job in list(active_jobs.values()):
            if not job["task"].done():
                job["task"].cancel()
                count += 1
        await event.respond(f"🛑 Cancelled <code>{count}</code> active job(s).", parse_mode="html")

    @bot.on(events.NewMessage(pattern=r"^/broadcast(?:@\w+)?(?:\s+([\s\S]+))?$"))
    async def on_broadcast(event: Any) -> None:
        if not is_admin(event.sender_id):
            return
        text = (event.pattern_match.group(1) or "").strip()
        if not text:
            await event.respond("Usage: <code>/broadcast &lt;message&gt;</code>", parse_mode="html")
            return
        user_ids = [int(uid) for uid in store.data.get("users", {}).keys() if uid.isdigit()]
        sent, failed = 0, 0
        for uid in user_ids:
            if store.is_banned(uid):
                continue
            try:
                await bot.send_message(uid, f"📢 <b>Announcement</b>\n\n<blockquote>{escape(text)}</blockquote>", parse_mode="html")
                sent += 1
                await asyncio.sleep(0.1)
            except Exception:  # noqa: BLE001
                failed += 1
        await event.respond(f"📢 Broadcast complete: <code>{sent}</code> sent, <code>{failed}</code> failed.", parse_mode="html")

    # ---- Plain URL Paste ----

    @bot.on(events.NewMessage)
    async def on_plain_urls(event: Any) -> None:
        text = event.raw_text or ""
        if text.startswith("/"):
            return
        matches = HQPORNER_URL_RE.findall(text)
        if not matches:
            return
        if not await check_access_and_rate(event):
            return
        unique_urls = list(dict.fromkeys(normalize_url(m) for m in matches))[:5]
        for u in unique_urls:
            await handle_site_url(bot, event.chat_id, event.sender_id, u)

    # ---- Inline Callbacks ----

    @bot.on(events.CallbackQuery)
    async def on_callback(event: Any) -> None:
        data = event.data.decode("utf-8", errors="ignore")
        if data == "noop":
            await event.answer()
            return

        if not await check_access_and_rate(event, is_callback=True):
            return

        parts = data.split(":")
        action = parts[0]

        if action == "menu":
            if len(parts) == 2 and parts[1] == "home":
                await event.answer()
                text, buttons = render_home_payload(event.sender_id)
                await safe_edit(event, text, parse_mode="html", buttons=buttons)
            elif len(parts) == 3 and parts[1] == "top":
                await event.answer("Loading…")
                tf = parts[2]
                await safe_edit(event, f"⏳ Loading top videos (<code>{tf}</code>)…", parse_mode="html")
                try:
                    videos = await collect_videos(api_client.get_top_porn(tf), MAX_FETCH_RESULTS)
                    await send_paginated_list(
                        bot, event.chat_id, event.sender_id, f"🔥 Top • {tf.title()}", videos, edit_msg=event
                    )
                except Exception as exc:  # noqa: BLE001
                    await safe_edit(event, f"❌ Failed: <code>{escape(str(exc))}</code>", parse_mode="html")
            elif len(parts) == 2 and parts[1] == "random":
                await event.answer("Picking random…")
                msg = await bot.send_message(event.chat_id, "🎲 Picking a random video…")
                try:
                    video = api_client.get_random_video()
                    if inspect.isawaitable(video):
                        video = await video
                    await msg.delete()
                    await send_details(bot, event.chat_id, event.sender_id, video)
                except Exception as exc:  # noqa: BLE001
                    await safe_edit(msg, f"❌ Failed: <code>{escape(str(exc))}</code>", parse_mode="html")
            elif len(parts) == 2 and parts[1] == "brazzers":
                await event.answer("Loading…")
                await fetch_brazzers(bot, event.chat_id, event.sender_id, edit_msg=event)
            elif len(parts) == 2 and parts[1] == "favorites":
                await event.answer()
                favs = store.user(event.sender_id)["favorites"]
                await send_paginated_list(bot, event.chat_id, event.sender_id, "⭐ Favorites", favs, edit_msg=event)
            elif len(parts) == 2 and parts[1] == "history":
                await event.answer()
                text, buttons = render_history_payload(event.sender_id)
                await safe_edit(event, text, parse_mode="html", buttons=buttons, link_preview=False)
            elif len(parts) == 2 and parts[1] == "settings":
                await event.answer()
                text, buttons = render_settings_payload(event.sender_id)
                await safe_edit(event, text, parse_mode="html", buttons=buttons)
            elif len(parts) == 2 and parts[1] == "stats":
                await event.answer("Updated!")
                text, buttons = render_stats_payload()
                await safe_edit(event, text, parse_mode="html", buttons=buttons)
            elif len(parts) == 2 and parts[1] == "queue":
                await event.answer("Updated!")
                text, buttons = render_queue_payload(event.sender_id)
                await safe_edit(event, text, parse_mode="html", buttons=buttons)
            elif len(parts) == 2 and parts[1] == "profile":
                await event.answer()
                text, buttons = render_profile_payload(event.sender_id)
                await safe_edit(event, text, parse_mode="html", buttons=buttons)
            return

        if action == "pg" and len(parts) == 3:
            await event.answer("Loading page…")
            text, buttons = await render_page_payload(parts[1], int(parts[2]), event.sender_id)
            await safe_edit(event, text, parse_mode="html", buttons=buttons or None, link_preview=False)
            return

        if action == "set" and len(parts) == 3:
            u = store.user(event.sender_id)
            key, val = parts[1], parts[2]
            if key == "q" and val in QUALITY_CHOICES:
                u["default_quality"] = val
                store.save()
                await event.answer(f"Default quality: {val.upper()}")
            elif key == "auto":
                u["auto_download"] = not bool(u["auto_download"])
                store.save()
                await event.answer(f"Auto-Download: {'ON' if u['auto_download'] else 'OFF'}")
            elif key == "doc":
                u["send_as_document"] = not bool(u["send_as_document"])
                store.save()
                await event.answer(f"Mode: {'Document (Raw)' if u['send_as_document'] else 'Streamable Video'}")
            elif key == "thumb":
                u["show_thumbnails"] = not bool(u["show_thumbnails"])
                store.save()
                await event.answer(f"Preview Thumbnails: {'ON' if u['show_thumbnails'] else 'OFF'}")
            elif key == "spoiler":
                u["spoiler_blur"] = not bool(u["spoiler_blur"])
                store.save()
                await event.answer(f"Spoiler Blur: {'ON' if u['spoiler_blur'] else 'OFF'}")
            elif key == "psize":
                cur = int(u.get("page_size", DEFAULT_PAGE_SIZE))
                idx = PAGE_SIZE_CHOICES.index(cur) if cur in PAGE_SIZE_CHOICES else 1
                nxt = PAGE_SIZE_CHOICES[(idx + 1) % len(PAGE_SIZE_CHOICES)]
                u["page_size"] = nxt
                store.save()
                await event.answer(f"Results per page: {nxt}")
            elif key == "clear" and val == "hist":
                u["history"] = []
                store.save()
                await event.answer("History cleared!", alert=True)
            elif key == "clear" and val == "favs":
                u["favorites"] = []
                store.save()
                await event.answer("Favorites cleared!", alert=True)
            text, buttons = render_settings_payload(event.sender_id)
            await safe_edit(event, text, parse_mode="html", buttons=buttons)
            return

        if action == "fav" and len(parts) == 2:
            meta = url_store.get(parts[1])
            if not isinstance(meta, dict) or not meta.get("url"):
                await event.answer("Expired.", alert=True)
                return
            added = store.toggle_favorite(event.sender_id, meta)
            await event.answer("⭐ Saved to Favorites!" if added else "💔 Removed from Favorites")
            try:
                await event.edit(buttons=download_buttons(meta, event.sender_id))
            except Exception:  # noqa: BLE001
                pass
            return

        if action == "info" and len(parts) == 2:
            await event.answer("Loading preview…")
            item = url_store.get(parts[1])
            url = item["url"] if isinstance(item, dict) else item
            if not url:
                await bot.send_message(event.chat_id, "⚠️ Button expired. Please search again.")
                return
            await handle_site_url(bot, event.chat_id, event.sender_id, url, force_preview=True)
            return

        if action == "cancel" and len(parts) == 2:
            job_id = parts[1]
            job = active_jobs.get(job_id)
            if not job or job["task"].done():
                await event.answer("Already finished.")
                return
            if job["user_id"] != event.sender_id and not is_admin(event.sender_id):
                await event.answer("You can only cancel your own download.", alert=True)
                return
            job["task"].cancel()
            await event.answer("Cancelling…")
            return

        if action == "dl" and len(parts) == 3:
            token, quality = parts[1], parts[2]
            item = url_store.get(token)
            url = item["url"] if isinstance(item, dict) else item
            label = item.get("title", "") if isinstance(item, dict) else ""
            if not url:
                await event.answer("Expired.", alert=True)
                await bot.send_message(event.chat_id, "⚠️ Button expired. Please fetch the video again.")
                return
            err = spawn_download(bot, event.chat_id, event.sender_id, url, quality, label)
            if err:
                await event.answer("Rate limit reached!", alert=False)
                await bot.send_message(event.chat_id, err, parse_mode="html")
                return
            await event.answer(f"Queued ({quality.upper()})!")

    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
