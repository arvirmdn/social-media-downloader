from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask
from urllib.parse import quote
import yt_dlp
import os
import glob
import shutil
import tempfile
import requests

app = FastAPI(title="Social Media Downloader API")

# Konfigurasi Telegram Bot — WAJIB diisi lewat Environment Variables di Railway
# (Settings > Variables), JANGAN pernah ditulis langsung di kode ini.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # dipakai khusus notifikasi ke kamu sendiri
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or ""

# WAJIB: tanpa ini, fetch() dari browser selalu diblok CORS
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


# ---------- yt-dlp helpers (dipakai bareng oleh endpoint web & bot Telegram) ----------

def build_ydl_opts(quality: int) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "format": f"best[height<={quality}]/best",
        "noplaylist": True,
        "socket_timeout": 20,
        "extractor_args": {
            "youtube": {"player_client": ["android", "web"]},
        },
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
    best = max(candidates, key=lambda f: f.get("height") or 0)
    return best.get("url")


def sanitize_filename(name: str) -> str:
    keep = "-_.() " + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    cleaned = "".join(c for c in name if c in keep).strip()
    return (cleaned or "video")[:60]


def build_download_url(source_url: str, title: str, quality: int) -> str | None:
    """Link ke endpoint /proxy kita sendiri — dia yang akan menyuruh yt-dlp
    benar-benar mengunduh videonya (biar header/fingerprint per-platform pasti benar),
    baru diteruskan ke user. Return None kalau PUBLIC_DOMAIN belum diset (nggak ada
    cara aman untuk kasih link yang pasti jalan)."""
    if not PUBLIC_DOMAIN:
        return None
    filename = sanitize_filename(title) + ".mp4"
    return (
        f"https://{PUBLIC_DOMAIN}/proxy"
        f"?source={quote(source_url, safe='')}"
        f"&quality={quality}"
        f"&filename={quote(filename)}"
    )


def fetch_video_info(url: str, quality: int = 720):
    """Ambil metadata + link video (cepat, tanpa benar-benar download filenya).
    Return (result_dict, error_message). Salah satu selalu None."""
    if quality not in ALLOWED_QUALITIES:
        quality = min(ALLOWED_QUALITIES, key=lambda q: abs(q - quality))

    ydl_opts = build_ydl_opts(quality)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
        "video_url": video_url,  # link CDN asli — beberapa platform (TikTok, dll) memblokir ini kalau dibuka langsung
        "download_url": build_download_url(url, title, quality),  # link yang aman & pasti dicoba lewat yt-dlp
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "platform": platform,
        "quality": quality,
    }, None


# ---------- Telegram helpers ----------

def send_telegram_notification(message: str):
    """Notifikasi ke akun pribadimu (TELEGRAM_CHAT_ID) — dipakai buat log sukses/gagal dari web."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    tg_api(TELEGRAM_CHAT_ID, "sendMessage", {"text": message, "parse_mode": "Markdown"})


def tg_api(chat_id, method: str, extra: dict):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    payload = {"chat_id": chat_id, **extra}
    try:
        response = requests.post(url, json=payload, timeout=10)
        print(f"Telegram {method} response:", response.text)
    except Exception as e:
        print(f"Gagal panggil Telegram {method}: {e}")


def send_bot_message(chat_id, text: str):
    tg_api(chat_id, "sendMessage", {"text": text})


def send_bot_download_result(chat_id, result: dict):
    caption = (
        f"🎬 *{result['title']}*\n"
        f"Platform: {result['platform'].upper()} • {result['quality']}p"
    )
    download_link = result.get("download_url") or result["video_url"]
    keyboard = {
        "inline_keyboard": [[{"text": "⬇️ Download Video", "url": download_link}]]
    }
    tg_api(chat_id, "sendMessage", {
        "text": caption,
        "parse_mode": "Markdown",
        "reply_markup": keyboard,
    })


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
    url: str = Query(..., description="Link video dari TikTok/YouTube/Instagram/FB/dll"),
    quality: int = Query(720, description="Tinggi video maksimum, mis. 360/720/1080"),
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
    source: str = Query(..., description="Link asli video (halaman TikTok/YouTube/dll), BUKAN link CDN"),
    quality: int = Query(720),
    filename: str = Query("video.mp4"),
):
    if quality not in ALLOWED_QUALITIES:
        quality = min(ALLOWED_QUALITIES, key=lambda q: abs(q - quality))

    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    ydl_opts = build_ydl_opts(quality)
    ydl_opts["outtmpl"] = os.path.join(tmp_dir, "%(id)s.%(ext)s")
    ydl_opts["noprogress"] = True

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([source])
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"Gagal mengunduh video: {e}")

    files = [f for f in glob.glob(os.path.join(tmp_dir, "*")) if os.path.isfile(f)]
    if not files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=502, detail="File hasil download tidak ditemukan di server.")

    filepath = files[0]

    return FileResponse(
        filepath,
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(lambda: shutil.rmtree(tmp_dir, ignore_errors=True)),
    )


# ---------- Bot Telegram: kirim link, dapat link download (mirror fitur web) ----------

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    message = body.get("message") or body.get("channel_post")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if text in ("/start", "/help"):
        send_bot_message(
            chat_id,
            "Halo! Kirim link TikTok/YouTube/Instagram/Facebook/Twitter ke sini, "
            "nanti aku balikin link download-nya. Contoh: https://vt.tiktok.com/xxxxx/"
        )
        return {"ok": True}

    if not text.startswith("http"):
        send_bot_message(chat_id, "Kirim link video yang valid ya (harus diawali http/https).")
        return {"ok": True}

    send_bot_message(chat_id, "⏳ Memproses linknya, tunggu sebentar...")

    result, error = fetch_video_info(text, quality=720)
    if error:
        send_bot_message(chat_id, f"❌ Gagal memproses: {error}")
        return {"ok": True}

    send_bot_download_result(chat_id, result)
    return {"ok": True}


@app.get("/set-webhook")
async def set_webhook():
    """Panggil endpoint ini SEKALI lewat browser setelah deploy untuk mendaftarkan webhook ke Telegram."""
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
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": exc.detail},
    )
