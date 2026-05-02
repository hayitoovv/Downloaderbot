import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN .env faylida o'rnatilmagan")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

_ffmpeg_env = os.getenv("FFMPEG_PATH", "").strip()
FFMPEG_PATH = _ffmpeg_env or shutil.which("ffmpeg")

COOKIES_BROWSER = os.getenv("COOKIES_BROWSER", "").strip().lower() or None

_PROJECT_ROOT = Path(__file__).parent.resolve()
_cookies_file_env = os.getenv("COOKIES_FILE", "").strip()


def _discover_cookies() -> Path | None:
    if _cookies_file_env:
        explicit = Path(_cookies_file_env)
        if not explicit.is_absolute():
            explicit = _PROJECT_ROOT / explicit
        if explicit.exists():
            return explicit.resolve()
        print(f"[ogohlantirish] COOKIES_FILE topilmadi: {explicit}")

    candidates = sorted({
        *_PROJECT_ROOT.glob("www.*_cookies.txt"),
        *_PROJECT_ROOT.glob("*.cookies.txt"),
    })
    candidates = [c for c in candidates if c.name != "_cookies_merged.txt"]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    merged = _PROJECT_ROOT / "_cookies_merged.txt"
    lines = ["# Netscape HTTP Cookie File"]
    for f in candidates:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#"):
                    lines.append(line)
        except (OSError, UnicodeDecodeError):
            continue
    merged.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[cookies] {len(candidates)} ta fayldan birlashtirildi: {[c.name for c in candidates]}")
    return merged


COOKIES_FILE = _discover_cookies()
