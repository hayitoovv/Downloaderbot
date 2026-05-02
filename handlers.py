import asyncio
import re
import shutil
import time
import traceback
from dataclasses import dataclass, field

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.media_group import MediaGroupBuilder

from config import MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB
from downloader import (
    MediaResult,
    Quality,
    download,
    get_video_info,
    get_youtube_qualities,
    is_youtube,
)

router = Router()

URL_RE = re.compile(r"https?://\S+")


@dataclass
class Pending:
    url: str
    qualities: list[Quality] = field(default_factory=list)


_pending: dict[int, Pending] = {}


def _friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "sign in to confirm" in msg or "not a bot" in msg:
        return (
            "❌ YouTube tasdiqlash so'rayapti.\n"
            "Bot egasi cookies'ni yangilashi kerak."
        )
    if "you need to log in" in msg or "login required" in msg or "private" in msg:
        return (
            "❌ Bu kontent yopiq yoki login talab qiladi.\n"
            "Faqat public postlar va reels'larni yuklab beraman."
        )
    if "fayl topilmadi" in msg or "no video formats" in msg or "no media" in msg:
        return (
            "❌ Bu post'da video yo'q.\n"
            "Ehtimol bu rasmli post (carousel) yoki yopiq akkaunt. "
            "Reels va video postlarni yuborib ko'ring."
        )
    if "403" in msg or "forbidden" in msg:
        return (
            "❌ Instagram bu postni anonim foydalanuvchilarga bermayapti.\n"
            "✅ Instagram reels (instagram.com/reel/...) ishlaydi."
        )
    if "video unavailable" in msg or "removed" in msg:
        return "❌ Video o'chirilgan yoki mavjud emas."
    if "unsupported url" in msg:
        return "❌ Bu sayt qo'llab-quvvatlanmaydi."
    if "10054" in msg or "forcibly closed" in msg or "connection reset" in msg:
        return (
            "❌ Sayt javob bermayapti.\n"
            "Ehtimol sayt sizning mintaqangizda bloklangan. VPN kerak bo'lishi mumkin."
        )
    if "getaddrinfo failed" in msg or "name or service not known" in msg:
        return "❌ Internet ulanishida muammo. DNS ishlamayotgan bo'lishi mumkin."
    return "❌ Yuklab bo'lmadi. Link to'g'riligini tekshiring."


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Salom! Menga YouTube, Instagram yoki TikTok'dan link yuboring.\n"
        "Men sizga yuqori sifatda video yoki mp3 yuklab beraman."
    )


@router.message(F.text.regexp(URL_RE))
async def on_link(message: Message) -> None:
    match = URL_RE.search(message.text or "")
    if not match:
        return
    url = match.group(0)
    print(f"[LINK QABUL QILINDI] chat={message.chat.id} url={url}")

    _pending[message.chat.id] = Pending(url=url)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 Video", callback_data="dl:video"),
        InlineKeyboardButton(text="🎵 Audio (mp3)", callback_data="dl:audio"),
    ]])
    await message.answer("Qaysi formatda yuklab beray?", reply_markup=kb)


@router.callback_query(F.data.startswith("dl:"))
async def on_choice(cb: CallbackQuery) -> None:
    mode = cb.data.split(":", 1)[1]
    p = _pending.get(cb.message.chat.id)
    if not p:
        await cb.answer("Link eskirdi, qaytadan yuboring.", show_alert=True)
        return

    print(f"[TUGMA BOSILDI] chat={cb.message.chat.id} mode={mode} url={p.url}")
    await cb.answer()

    if mode == "video" and is_youtube(p.url):
        await _show_quality_selector(cb, p)
        return

    _pending.pop(cb.message.chat.id, None)
    status = await cb.message.edit_text("⏳ Yuklab olinmoqda...")
    await _do_download(cb.message, status, p.url, mode, format_id=None)


async def _show_quality_selector(cb: CallbackQuery, p: Pending) -> None:
    status = await cb.message.edit_text("🔍 Sifatlar tekshirilmoqda...")
    try:
        qualities = await asyncio.to_thread(get_youtube_qualities, p.url)
    except Exception as e:
        print(f"[SIFAT XATOSI] {e}")
        await _do_download(cb.message, status, p.url, "video", format_id=None)
        return

    available = [q for q in qualities if q.size_mb <= MAX_FILE_SIZE_MB]
    if not available:
        await status.edit_text(
            f"⚠️ Hech qanday sifat {MAX_FILE_SIZE_MB} MB limitiga sig'maydi.\n"
            "Audio (mp3) variantini sinab ko'ring."
        )
        _pending.pop(cb.message.chat.id, None)
        return

    p.qualities = available
    rows = [[
        InlineKeyboardButton(
            text=f"{q.label} • {q.size_mb:.1f} MB",
            callback_data=f"q:{i}",
        )
    ] for i, q in enumerate(available)]
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back")])

    await status.edit_text(
        "🎬 Qaysi sifatda yuklab beray?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("q:"))
async def on_quality(cb: CallbackQuery) -> None:
    idx = int(cb.data.split(":", 1)[1])
    p = _pending.pop(cb.message.chat.id, None)
    if not p or idx >= len(p.qualities):
        await cb.answer("Link eskirdi, qaytadan yuboring.", show_alert=True)
        return

    q = p.qualities[idx]
    print(f"[SIFAT TANLANDI] {q.label} fmt={q.format_id} url={p.url}")
    await cb.answer()
    status = await cb.message.edit_text(f"⏳ {q.label} yuklab olinmoqda...")
    await _do_download(cb.message, status, p.url, "video", format_id=q.format_id)


@router.callback_query(F.data == "back")
async def on_back(cb: CallbackQuery) -> None:
    p = _pending.get(cb.message.chat.id)
    if not p:
        await cb.answer("Link eskirdi.", show_alert=True)
        return
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 Video", callback_data="dl:video"),
        InlineKeyboardButton(text="🎵 Audio (mp3)", callback_data="dl:audio"),
    ]])
    await cb.message.edit_text("Qaysi formatda yuklab beray?", reply_markup=kb)


_BAR_LENGTH = 20


def _render_bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 100 * _BAR_LENGTH)
    return "█" * filled + "░" * (_BAR_LENGTH - filled)


def _make_progress_hook(loop, status: Message):
    last = {"t": 0.0, "text": ""}

    def hook(info: dict) -> None:
        if info.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - last["t"] < 2.5:
            return
        last["t"] = now

        downloaded = info.get("downloaded_bytes") or 0
        total = info.get("total_bytes") or info.get("total_bytes_estimate") or 0
        pct = (downloaded / total * 100) if total else 0.0

        bar = _render_bar(pct)
        speed = (info.get("_speed_str") or "?").strip()
        eta = (info.get("_eta_str") or "?").strip()
        size_mb = total / 1024 / 1024 if total else 0
        done_mb = downloaded / 1024 / 1024

        text = (
            "⏳ <b>Yuklab olinmoqda...</b>\n\n"
            f"<code>[{bar}]</code>\n"
            f"<b>{pct:.1f}%</b>  ({done_mb:.1f} / {size_mb:.1f} MB)\n"
            f"⚡ {speed}  ⏱ {eta}"
        )
        if text == last["text"]:
            return
        last["text"] = text

        async def update():
            try:
                await status.edit_text(text, parse_mode="HTML")
            except TelegramBadRequest:
                pass
            except Exception as e:
                print(f"[progress] update xatosi: {e}")

        asyncio.run_coroutine_threadsafe(update(), loop)

    return hook


async def _do_download(
    message: Message,
    status: Message,
    url: str,
    mode: str,
    format_id: str | None,
) -> None:
    loop = asyncio.get_running_loop()
    hook = _make_progress_hook(loop, status)

    result: MediaResult | None = None
    try:
        result = await download(url, mode, format_id=format_id, progress_hook=hook)

        if result.kind == "images":
            await _send_images(message, status, result.files)
            return

        file_path = result.files[0]
        size = file_path.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            await status.edit_text(
                f"⚠️ Fayl juda katta: {size / 1024 / 1024:.1f} MB.\n"
                f"Telegram bot limiti: {MAX_FILE_SIZE_MB} MB."
            )
            return

        await status.edit_text("📤 Telegram'ga yuborilmoqda...")
        if result.kind == "video":
            info = get_video_info(file_path)
            await message.answer_video(
                FSInputFile(file_path),
                duration=info.get("duration"),
                width=info.get("width"),
                height=info.get("height"),
                supports_streaming=True,
            )
        else:
            await message.answer_audio(FSInputFile(file_path))
        await status.delete()
    except Exception as e:
        print("=" * 60)
        print(f"[BOT XATOSI] URL={url} mode={mode} fmt={format_id}")
        traceback.print_exc()
        print("=" * 60)
        try:
            await status.edit_text(_friendly_error(e))
        except TelegramBadRequest:
            pass
    finally:
        _cleanup(result)


async def _send_images(message: Message, status: Message, images: list) -> None:
    images = images[:10]
    await status.edit_text(f"📤 {len(images)} ta rasm yuborilmoqda...")

    if len(images) == 1:
        await message.answer_photo(FSInputFile(images[0]))
    else:
        group = MediaGroupBuilder()
        for img in images:
            group.add_photo(media=FSInputFile(img))
        await message.answer_media_group(media=group.build())

    await status.delete()


def _cleanup(result: MediaResult | None) -> None:
    if not result:
        return
    for f in result.files:
        try:
            f.unlink()
        except OSError:
            pass
    for d in result.cleanup_dirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
