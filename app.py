from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import yt_dlp
import os
import requests

app = FastAPI(title="Social Media Downloader API")

# Konfigurasi Telegram Bot (Ambil dari Environment Variable Railway atau isi langsung)
TELEGRAM_BOT_TOKEN = os.getenv("AAFzDqYEw6CtozJ6ClB-oiNDlTHv6oFoaUM", "AAFzDqYEw6CtozJ6ClB-oiNDlTHv6oFoaUM")
TELEGRAM_CHAT_ID = os.getenv("8752870096", "8752870096")

def send_telegram_notification(message: str):
    """Fungsi pembantu untuk mengirim pesan ke Telegram Bot"""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "MASUKKAN_BOT_TOKEN_ANDA":
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Gagal mengirim notifikasi Telegram: {e}")

# WAJIB: tanpa ini, fetch() dari browser selalu diblok CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

ALLOWED_QUALITIES = [144, 240, 360, 480, 720, 1080, 1440, 2160]


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
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
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


@app.get("/")
async def root():
    return {
        "message": "yt-dlp API is running. Support TikTok, YouTube, Instagram, Facebook, Twitter, dan 1000+ platform lainnya.",
        "endpoints": ["/download?url=...&quality=720", "/health"],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/download")
async def download_video(
    url: str = Query(..., description="Link video dari TikTok/YouTube/Instagram/FB/dll"),
    quality: int = Query(720, description="Tinggi video maksimum, mis. 360/720/1080"),
):
    if quality not in ALLOWED_QUALITIES:
        quality = min(ALLOWED_QUALITIES, key=lambda q: abs(q - quality))

    ydl_opts = build_ydl_opts(quality)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        send_telegram_notification(f"❌ *Gagal Memproses Link!*\n• URL: {url}\n• Error: `{error_msg}`")
        raise HTTPException(status_code=422, detail=f"Gagal memproses link: {error_msg}")
    except Exception as e:
        error_msg = str(e)
        send_telegram_notification(f"⚠️ *Kesalahan Server (Internal Error)*\n• URL: {url}\n• Error: `{error_msg}`")
        raise HTTPException(status_code=500, detail=f"Terjadi kesalahan server: {error_msg}")

    video_url = extract_video_url(info, quality)
    if not video_url:
        send_telegram_notification(f"⚠️ *Format Tidak Ditemukan*\n• URL: {url}\n• Kualitas: {quality}p")
        raise HTTPException(status_code=404, detail="Tidak ada format video yang cocok ditemukan untuk kualitas ini.")

    title = info.get("title", "Video")
    platform = info.get("extractor", "unknown")

    # Kirim notifikasi sukses ke Telegram
    send_telegram_notification(f"📥 *Unduhan Berhasil!*\n• Judul: {title}\n• Platform: {platform.upper()}\n• Kualitas: {quality}p\n• URL: {url}")

    return {
        "status": "success",
        "title": title,
        "video_url": video_url,
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration", 0),
        "platform": platform,
        "quality": quality,
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": exc.detail},
    )
