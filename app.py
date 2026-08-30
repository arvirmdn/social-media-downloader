from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask
from urllib.parse import quote
import yt_dlp
import os
import re
import glob
import shutil
import tempfile
import uuid
import requests

app = FastAPI(title="Social Media Downloader API")

# Konfigurasi Telegram Bot — WAJIB diisi lewat Environment Variables di Railway
# (Settings > Variables), JANGAN pernah ditulis langsung di kode ini.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # dipakai khusus notifikasi ke kamu sendiri
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or ""

TELEGRAM_MAX_UPLOAD_MB = 50  # batas resmi Telegram Bot API untuk upload langsung
MAX_LINKS_PER_MESSAGE = 5    # batas link sekaligus dalam 1 pesan, biar server nggak keteteran

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

ALLOWED_QUALITIES = [144, 240, 360, 480, 720, 1080, 1440, 2160]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
URL_REGEX = re.compile(r"https?://\S+")

# Penyimpanan sementara link yang lagi nunggu dipilih kualitasnya (in-memory —
# kalau server redeploy/restart, pilihan yang belum ditekan jadi hilang, user
# tinggal kirim ulang linknya).
PENDING: dict[str, str] = {}


# ---------- yt-dlp helpers ----------

def build_ydl_opts(quality: int) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "format": f"best[height<={quality}]/best",
        "noplaylist": True,
        "socket_timeout": 20,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "http_headers": {"User-Agent": USER_AGENT},
    }


def extract_video_url(info: dict, quality: int) -> str | None:
    video_url = info.get("url")
    if video_url:
        return video_url
    candidates = [
        f for f in info.get("formats", [])
        if f.get("url") and f.get("vcodec") != "none" and (f.get("height") or 0) <= quality
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.get("height") or 0).get("url")


def sanitize_filename(name: str) -> str:
    keep = "-_.() " + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    cleaned = "".join(c for c in name if c in keep).strip()
    return (cleaned or "video")[:60]


def build_download_url(source_url: str, title: str, quality: int) -> str | None:
    if not PUBLIC_DOMAIN:
        return None
    filename = sanitize_filename(title) + ".mp4"
    return (
        f"https://{PUBLIC_DOMAIN}/proxy"
        f"?source={quote(source_url, safe='')}&quality={quality}&filename={quote(filename)}"
    )


def fetch_video_info(url: str, quality: int = 720):
    if quality not in ALLOWED_QUALITIES:
        quality = min(ALLOWED_QUALITIES, key=lambda q: abs(q - quality))
    try:
        with yt_dlp.YoutubeDL(build_ydl_opts(quality)) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return None, f"Gagal memproses link: {e}"
    except Exception as e:
        return None, f"Terjadi kesalahan server: {e}"

    video_url = extract_video_url(info, quality)
    if not video_url:
        return None, "Tidak ada format video yang cocok ditemukan untuk kualitas ini."

    title = info.get("title", "Video")
    platform = info.get("extractor", "unknown")
    return {
        "status": "success",
        "title": title,
        "video_url": video_url,
        "download_url": build_download_url(url, title, quality),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "platform": platform,
        "quality": quality,
    }, None


def download_video_file(source_url: str, quality: int):
    if quality not in ALLOWED_QUALITIES:
        quality = min(ALLOWED_QUALITIES, key=lambda q: abs(q - quality))
    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    ydl_opts = build_ydl_opts(quality)
    ydl_opts["outtmpl"] = os.path.join(tmp_dir, "%(id)s.%(ext)s")
    ydl_opts["noprogress"] = True
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([source_url])
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    files = [f for f in glob.glob(os.path.join(tmp_dir, "*")) if os.path.isfile(f)]
    if not files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("File hasil download tidak ditemukan di server.")
    return files[0], tmp_dir


def download_audio_file(source_url: str):
    """Unduh & convert ke MP3. Butuh ffmpeg terpasang di server (lihat nixpacks.toml)."""
    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "socket_timeout": 20,
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "http_headers": {"User-Agent": USER_AGENT},
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([source_url])
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    files = [f for f in glob.glob(os.path.join(tmp_dir, "*")) if os.path.isfile(f)]
    mp3s = [f for f in files if f.lower().endswith(".mp3")]
    chosen = mp3s or files
    if not chosen:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("File audio hasil download tidak ditemukan di server.")
    return chosen[0], tmp_dir


# ---------- Telegram low-level helpers ----------

def tg_call(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json()
        if not data.get("ok"):
            print(f"Telegram {method} gagal:", data)
        return data
    except Exception as e:
        print(f"Telegram {method} error: {e}")
        return None


def send_bot_message(chat_id, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", payload)


def edit_bot_message(chat_id, message_id, text: str):
    return tg_call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})


def answer_callback(callback_id: str, text: str | None = None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    return tg_call("answerCallbackQuery", payload)


def send_telegram_notification(message: str):
    """Notifikasi ke akun pribadimu (TELEGRAM_CHAT_ID) — log sukses/gagal dari endpoint web."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    tg_call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})


def send_bot_download_link(chat_id, title: str, platform: str, quality: int, download_url: str | None, video_url: str):
    caption = f"🎬 *{title}*\nPlatform: {platform.upper()} • {quality}p"
    link = download_url or video_url
    keyboard = {"inline_keyboard": [[{"text": "⬇️ Download Video", "url": link}]]}
    send_bot_message(chat_id, caption, reply_markup=keyboard)


def send_bot_video_file(chat_id, filepath: str, title: str, platform: str, quality: int) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    caption = f"🎬 {title}\nPlatform: {platform.upper()} • {quality}p"[:1024]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "supports_streaming": True},
                files={"video": f},
                timeout=180,
            )
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"Gagal upload video: {e}")
        return False


def send_bot_audio_file(chat_id, filepath: str, title: str, platform: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    caption = f"🎵 {title}\nPlatform: {platform.upper()}"[:1024]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendAudio"
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "title": title[:64]},
                files={"audio": f},
                timeout=180,
            )
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"Gagal upload audio: {e}")
        return False


# ---------- Endpoint web (dipakai frontend Blogspot/Vercel) ----------

@app.get("/")
async def root():
    return {
        "message": "yt-dlp API is running. Support TikTok, YouTube, Instagram, Facebook, Twitter, dan 1000+ platform lainnya.",
        "endpoints": ["/download?url=...&quality=720", "/proxy?source=...&quality=720", "/health", "/telegram-webhook (POST)"],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/download")
async def download_video(
    url: str = Query(...),
    quality: int = Query(720),
):
    result, error = fetch_video_info(url, quality)
    if error:
        send_telegram_notification(f"❌ *Gagal Memproses Link!*\n• URL: {url}\n• Error: `{error}`")
        status_code = 404 if "Tidak ada format" in error else 422
        raise HTTPException(status_code=status_code, detail=error)

    send_telegram_notification(
        f"📥 *Unduhan Berhasil!*\n• Judul: {result['title']}\n"
        f"• Platform: {result['platform'].upper()}\n• Kualitas: {quality}p\n• URL: {url}"
    )
    return result


@app.get("/proxy")
async def proxy_download(
    source: str = Query(...),
    quality: int = Query(720),
    filename: str = Query("video.mp4"),
):
    try:
        filepath, tmp_dir = download_video_file(source, quality)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gagal mengunduh video: {e}")

    return FileResponse(
        filepath,
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(lambda: shutil.rmtree(tmp_dir, ignore_errors=True)),
    )


# ---------- Bot Telegram ----------

def extract_urls(text: str) -> list[str]:
    return URL_REGEX.findall(text or "")


def process_and_deliver(chat_id, url: str, quality: int, audio_only: bool):
    """Ambil metadata, download (video atau audio), lalu kirim ke chat — fallback ke link kalau kegedean."""
    result, error = fetch_video_info(url, quality)
    if error:
        send_bot_message(chat_id, f"❌ Gagal memproses {url}\n{error}")
        return

    send_bot_message(chat_id, f"⏳ Mengunduh \"{result['title']}\"...")

    tmp_dir = None
    try:
        if audio_only:
            filepath, tmp_dir = download_audio_file(url)
        else:
            filepath, tmp_dir = download_video_file(url, quality)

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if size_mb > TELEGRAM_MAX_UPLOAD_MB:
            send_bot_message(
                chat_id,
                f"⚠️ File {size_mb:.1f}MB kelewat besar buat dikirim langsung "
                f"(maks {TELEGRAM_MAX_UPLOAD_MB}MB dari Telegram). Ini link download-nya:"
            )
            send_bot_download_link(chat_id, result["title"], result["platform"], quality, result["download_url"], result["video_url"])
            return

        if audio_only:
            ok = send_bot_audio_file(chat_id, filepath, result["title"], result["platform"])
        else:
            ok = send_bot_video_file(chat_id, filepath, result["title"], result["platform"], quality)

        if not ok:
            send_bot_message(chat_id, "⚠️ Gagal kirim langsung, ini link download-nya sebagai gantinya:")
            send_bot_download_link(chat_id, result["title"], result["platform"], quality, result["download_url"], result["video_url"])
    except Exception as e:
        send_bot_message(chat_id, f"❌ Gagal mengunduh {url}\n{e}")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def handle_incoming_message(message: dict):
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if text in ("/start", "/help"):
        send_bot_message(
            chat_id,
            "Halo! Kirim link TikTok/YouTube/Instagram/Facebook/Twitter ke sini.\n\n"
            "• 1 link → aku tanya dulu mau kualitas berapa / audio MP3.\n"
            f"• Beberapa link sekaligus (maks {MAX_LINKS_PER_MESSAGE}) → langsung diproses semua di 720p.\n\n"
            f"Video di atas {TELEGRAM_MAX_UPLOAD_MB}MB dikirim sebagai link download (batas dari Telegram)."
        )
        return

    urls = extract_urls(text)
    if not urls:
        send_bot_message(chat_id, "Kirim link video yang valid ya (harus diawali http/https).")
        return

    if len(urls) == 1:
        req_id = uuid.uuid4().hex[:10]
        PENDING[req_id] = urls[0]
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "360p", "callback_data": f"dl|{req_id}|360"},
                    {"text": "720p", "callback_data": f"dl|{req_id}|720"},
                    {"text": "1080p", "callback_data": f"dl|{req_id}|1080"},
                ],
                [{"text": "🎵 Audio (MP3)", "callback_data": f"dl|{req_id}|mp3"}],
            ]
        }
        send_bot_message(chat_id, "Pilih kualitas / format yang kamu mau:", reply_markup=keyboard)
        return

    urls = urls[:MAX_LINKS_PER_MESSAGE]
    send_bot_message(chat_id, f"📋 Terdeteksi {len(urls)} link, aku proses semua di kualitas 720p ya...")
    for u in urls:
        process_and_deliver(chat_id, u, quality=720, audio_only=False)


def handle_callback_query(callback_query: dict):
    callback_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query.get("data", "")

    parts = data.split("|")
    if len(parts) != 3 or parts[0] != "dl":
        answer_callback(callback_id)
        return

    _, req_id, choice = parts
    url = PENDING.pop(req_id, None)
    answer_callback(callback_id)

    if not url:
        edit_bot_message(chat_id, message_id, "⚠️ Pilihan ini sudah kedaluwarsa, kirim ulang linknya ya.")
        return

    audio_only = choice == "mp3"
    quality = 720 if audio_only else int(choice)
    label = "🎵 Audio MP3" if audio_only else f"{quality}p"
    edit_bot_message(chat_id, message_id, f"⏳ Memproses ({label})...")

    process_and_deliver(chat_id, url, quality=quality, audio_only=audio_only)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    body = await request.json()

    if "callback_query" in body:
        handle_callback_query(body["callback_query"])
        return {"ok": True}

    message = body.get("message") or body.get("channel_post")
    if message:
        handle_incoming_message(message)

    return {"ok": True}


@app.get("/set-webhook")
async def set_webhook():
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN belum diset.")
    if not PUBLIC_DOMAIN:
        raise HTTPException(
            status_code=400,
            detail="Set env var PUBLIC_DOMAIN dulu (isi domain Railway kamu, tanpa https://), lalu redeploy."
        )
    webhook_url = f"https://{PUBLIC_DOMAIN}/telegram-webhook"
    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
        params={"url": webhook_url},
        timeout=10,
    )
    return resp.json()


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "message": exc.detail})
