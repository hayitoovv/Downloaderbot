import asyncio
import http.cookiejar
import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import instaloader
from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget

from config import COOKIES_BROWSER, COOKIES_FILE, DOWNLOAD_DIR, FFMPEG_PATH

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _ffprobe_path() -> str:
    if not FFMPEG_PATH:
        return "ffprobe"
    p = Path(FFMPEG_PATH)
    bin_dir = p if p.is_dir() else p.parent
    candidate = bin_dir / "ffprobe.exe"
    return str(candidate) if candidate.exists() else "ffprobe"


def get_video_info(path: Path) -> dict:
    try:
        result = subprocess.run(
            [
                _ffprobe_path(),
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-select_streams", "v:0",
                str(path),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            return {}
        streams = json.loads(result.stdout).get("streams", [])
        if not streams:
            return {}
        s = streams[0]
        info = {"width": s.get("width"), "height": s.get("height")}
        if duration := s.get("duration"):
            info["duration"] = int(float(duration))
        return info
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}


@dataclass
class MediaResult:
    kind: Literal["video", "audio", "images"]
    files: list[Path]
    cleanup_dirs: list[Path] = field(default_factory=list)


_INSTAGRAM_SHORTCODE_RE = re.compile(r"/(?:p|reel|tv)/([^/?#]+)")


def _is_instagram_post(url: str) -> bool:
    return "instagram.com" in url.lower() and bool(_INSTAGRAM_SHORTCODE_RE.search(url))


def is_youtube(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


@dataclass
class Quality:
    height: int
    format_id: str
    size_mb: float
    label: str


def _resolution_label(h: int) -> str:
    if h >= 4320:
        return "8K"
    if h >= 2160:
        return "4K"
    if h >= 1440:
        return "2K"
    return f"{h}p"


def get_youtube_qualities(url: str) -> list[Quality]:
    opts = _common_opts(str(DOWNLOAD_DIR / "dummy.%(ext)s"))
    opts["quiet"] = True
    opts["no_warnings"] = True
    opts["skip_download"] = True

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])
    audio_only = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    best_audio_size = max(
        ((f.get("filesize") or f.get("filesize_approx") or 0) for f in audio_only),
        default=0,
    )

    video_only = [
        f for f in formats
        if f.get("acodec") == "none"
        and (f.get("vcodec") or "").startswith("avc1")
        and f.get("height")
    ]

    by_height: dict[int, dict] = {}
    for f in video_only:
        h = f["height"]
        cur = by_height.get(h)
        size = f.get("filesize") or f.get("filesize_approx") or 0
        cur_size = (cur.get("filesize") or cur.get("filesize_approx") or 0) if cur else 0
        if not cur or size > cur_size:
            by_height[h] = f

    qualities: list[Quality] = []
    for h in sorted(by_height.keys()):
        f = by_height[h]
        v_size = f.get("filesize") or f.get("filesize_approx") or 0
        total_mb = (v_size + best_audio_size) / 1024 / 1024
        qualities.append(Quality(
            height=h,
            format_id=f["format_id"],
            size_mb=total_mb,
            label=_resolution_label(h),
        ))
    return qualities


def _common_opts(out_template: str) -> dict:
    opts = {
        "outtmpl": out_template,
        "quiet": False,
        "no_warnings": False,
        "noprogress": False,
        "retries": 3,
        "socket_timeout": 60,
        "geo_bypass": True,
        "js_runtimes": {"node": {}},
        "impersonate": ImpersonateTarget("chrome"),
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    if COOKIES_FILE:
        opts["cookiefile"] = str(COOKIES_FILE)
    elif COOKIES_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    return opts


def _video_opts(out_template: str, format_id: str | None = None) -> dict:
    if format_id:
        fmt = f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio"
    else:
        fmt = (
            "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
            "bv*[vcodec^=avc1]+ba/"
            "b[vcodec^=avc1]/"
            "bv*+ba/b"
        )
    return {
        **_common_opts(out_template),
        "format": fmt,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 4,
        "postprocessor_args": {
            "merger": [
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-af", "aresample=async=1",
                "-movflags", "+faststart",
            ],
        },
    }


def _audio_opts(out_template: str) -> dict:
    return {
        **_common_opts(out_template),
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        }],
    }


def _download_via_gallery_dl(url: str) -> tuple[list[Path], Path]:
    target_dir = DOWNLOAD_DIR / f"gd_{uuid.uuid4().hex}"
    target_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "gallery_dl",
        "-d", str(target_dir),
        "--no-mtime",
        "--quiet",
    ]
    if COOKIES_FILE:
        cmd.extend(["--cookies", str(COOKIES_FILE)])
    cmd.append(url)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("gallery-dl timeout (120s)") from None

    images = sorted(
        f for f in target_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in _IMAGE_EXTS
    )

    if not images:
        stderr = (result.stderr or "").strip()
        if "no extractor" in stderr.lower():
            raise RuntimeError("gallery-dl: bu sayt qo'llab-quvvatlanmaydi")
        if stderr:
            raise RuntimeError(f"gallery-dl: {stderr[:200]}")
        raise RuntimeError("no media found via gallery-dl")

    return images, target_dir


def _inject_instagram_cookies(L: instaloader.Instaloader) -> bool:
    if not COOKIES_FILE or not COOKIES_FILE.exists():
        return False
    jar = http.cookiejar.MozillaCookieJar(str(COOKIES_FILE))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return False
    found = False
    for cookie in jar:
        if "instagram" in (cookie.domain or "").lower():
            L.context._session.cookies.set(
                cookie.name, cookie.value, domain=cookie.domain,
            )
            found = True
    return found


def _download_instagram_images(url: str) -> tuple[list[Path], Path]:
    match = _INSTAGRAM_SHORTCODE_RE.search(url)
    if not match:
        raise ValueError("Instagram shortcode topilmadi")
    shortcode = match.group(1)

    target_dir = DOWNLOAD_DIR / f"ig_{uuid.uuid4().hex}"
    target_dir.mkdir(parents=True, exist_ok=True)

    L = instaloader.Instaloader(
        dirname_pattern=str(target_dir),
        download_pictures=True,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        download_geotags=False,
        download_comments=False,
        post_metadata_txt_pattern="",
        max_connection_attempts=1,
        request_timeout=15,
        quiet=True,
    )
    if _inject_instagram_cookies(L):
        print("[instaloader] Instagram cookies'i yuklandi (burner hisob)")

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        L.download_post(post, target="")
    except instaloader.exceptions.QueryReturnedForbiddenException as e:
        raise RuntimeError("Instagram 403 forbidden") from e
    except instaloader.exceptions.LoginRequiredException as e:
        raise RuntimeError("Instagram login required") from e

    images = sorted(
        f for f in target_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    )
    return images, target_dir


def _download_blocking(
    url: str,
    mode: str,
    format_id: str | None = None,
    progress_hook=None,
) -> MediaResult:
    file_id = uuid.uuid4().hex
    out_template = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")
    if mode == "video":
        opts = _video_opts(out_template, format_id)
    else:
        opts = _audio_opts(out_template)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    yt_error: Exception | None = None
    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        yt_error = e

    candidates = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))

    if not candidates and mode == "video":
        if _is_instagram_post(url):
            try:
                print("[fallback] yt-dlp video topa olmadi, instaloader bilan urinib ko'ramiz")
                images, target_dir = _download_instagram_images(url)
                if images:
                    return MediaResult(kind="images", files=images, cleanup_dirs=[target_dir])
            except Exception as ig_error:
                print(f"[fallback] instaloader xato: {ig_error}")

        try:
            print("[fallback] gallery-dl bilan rasmlar uchun urinib ko'ramiz")
            images, target_dir = _download_via_gallery_dl(url)
            if images:
                return MediaResult(kind="images", files=images, cleanup_dirs=[target_dir])
        except Exception as gd_error:
            print(f"[fallback] gallery-dl xato: {gd_error}")

    if yt_error and not candidates:
        raise yt_error
    if not candidates:
        raise RuntimeError("no media in post")

    preferred = ".mp3" if mode == "audio" else ".mp4"
    chosen = next(
        (c for c in candidates if c.suffix.lower() == preferred),
        candidates[0],
    )
    return MediaResult(kind=mode, files=[chosen])


async def download(
    url: str,
    mode: str,
    format_id: str | None = None,
    progress_hook=None,
) -> MediaResult:
    return await asyncio.to_thread(_download_blocking, url, mode, format_id, progress_hook)
