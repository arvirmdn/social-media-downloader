from fastapi import FastAPI, Query, HTTPException, Request, BackgroundTasks, Body, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
from urllib.parse import quote
import yt_dlp
import asyncio
import os
import re
import glob
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import requests
import zipfile

app = FastAPI(title="Social Media Downloader API")

# Konfigurasi via Environment Variables di Railway (Settings > Variables) —
# JANGAN pernah ditulis langsung di kode ini.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # notifikasi ke akun pribadimu (dari endpoint web)
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or ""

# TELEGRAM_WEBHOOK_SECRET — opsional tapi SANGAT disarankan diisi.
# Tanpa ini, siapa pun yang tahu URL /telegram-webhook kamu bisa kirim payload
# palsu dan bikin bot ini bertingkah aneh (impersonate update Telegram).
# Isi bebas (string acak panjang), lalu dipakai juga saat /set-webhook dipanggil.
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# OWNER_USER_ID — WAJIB diset di Railway Settings > Variables
# Bisa dapat dari @getmyid_bot atau @userinfobot
try:
    OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))
except ValueError:
    OWNER_USER_ID = 0

TELEGRAM_MAX_UPLOAD_MB = 50  # batas resmi Telegram Bot API untuk upload langsung
MAX_LINKS_PER_MESSAGE = 5    # batas link sekaligus dalam 1 pesan
MAX_DURATION_MINUTES = int(os.getenv("MAX_DURATION_MINUTES", "15"))

# ---------- Member storage (SQLite, persisten) ----------
# Sebelumnya member list cuma disimpan di memory (dict/set Python biasa),
# jadi hilang total tiap kali server restart/redeploy. Sekarang disimpan di
# file SQLite supaya bertahan selama proses restart biasa.
#
# CATATAN PENTING (Railway): filesystem Railway itu EPHEMERAL — file akan
# ikut hilang kalau kamu redeploy / pindah instance, KECUALI kamu attach
# "Volume" di Railway dan arahkan DB_PATH ke path di dalam volume itu
# (Settings > Volumes > mount ke folder, lalu set env var DB_PATH ke
# folder tsb, misal DB_PATH=/data/bot_data.db). Tanpa volume, ini tetap
# jauh lebih baik daripada in-memory murni (survive restart proses biasa),
# tapi belum 100% persisten lintas redeploy.
DB_PATH = os.getenv("DB_PATH", "bot_data.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS members (user_id INTEGER PRIMARY KEY, expires_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)"
    )
    # Migrasi lembut untuk DB lama yang tabel members-nya belum punya kolom expires_at.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(members)").fetchall()]
        if "expires_at" not in cols:
            conn.execute("ALTER TABLE members ADD COLUMN expires_at INTEGER")
            conn.commit()
    except Exception as e:
        print(f"⚠️  Migrasi kolom expires_at gagal (boleh diabaikan kalau kolom sudah ada): {e}")
    return conn


def load_members_from_db() -> dict:
    """Return dict {user_id: expires_at_unix_or_None}."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT user_id, expires_at FROM members").fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def db_add_member(user_id: int, expires_at: int | None = None):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO members (user_id, expires_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET expires_at = excluded.expires_at",
            (user_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def db_remove_member(user_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM members WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def db_increment_stat(key: str, by: int = 1):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO stats (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
            (key, by),
        )
        conn.commit()
    finally:
        conn.close()


def db_get_stat(key: str) -> int:
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# Member list di-load dari DB saat startup, lalu dipegang di memory sebagai
# dict {user_id: expires_at_unix_or_None} biar pengecekan is_member() tetap
# cepat — setiap perubahan (add/remove) langsung ditulis ke DB juga lewat
# db_add_member()/db_remove_member().
# expires_at None = member permanen (nggak pernah expired).
APPROVED_MEMBERS: dict[int, int | None] = load_members_from_db()

# Durasi paket membership yang bisa dipakai lewat /addmember <id> <paket>
MEMBER_DURATIONS = {
    "trial": 7 * 24 * 3600,       # 7 hari
    "bulan": 30 * 24 * 3600,      # 1 bulan
    "permanent": None,
}

# Migrasi dari env var APPROVED_MEMBERS lama (kalau masih dipakai) ke DB,
# supaya member yang sebelumnya cuma "hidup" di env var ikut tersimpan permanen
# (sebagai member permanen/expires_at=None).
if os.getenv("APPROVED_MEMBERS"):
    try:
        legacy_ids = set(int(uid.strip()) for uid in os.getenv("APPROVED_MEMBERS", "").split(",") if uid.strip())
        for uid in legacy_ids:
            if uid not in APPROVED_MEMBERS:
                db_add_member(uid)
                APPROVED_MEMBERS[uid] = None
    except ValueError:
        pass

# Add owner ke approved members (selalu permanen)
if OWNER_USER_ID > 0:
    if OWNER_USER_ID not in APPROVED_MEMBERS or APPROVED_MEMBERS.get(OWNER_USER_ID) is not None:
        db_add_member(OWNER_USER_ID, expires_at=None)
    APPROVED_MEMBERS[OWNER_USER_ID] = None  # pemilik otomatis approved & permanen
    print(f"✅ Owner (ID: {OWNER_USER_ID}) added to approved members")
else:
    print("⚠️  WARNING: OWNER_USER_ID tidak di-set! Set env var OWNER_USER_ID di Railway Settings > Variables")
    print("   Membership system DISABLED untuk sekarang (semua user bisa akses)")

if not TELEGRAM_WEBHOOK_SECRET:
    print("⚠️  WARNING: TELEGRAM_WEBHOOK_SECRET belum diset! Endpoint /telegram-webhook masih bisa")
    print("   dipanggil siapa saja tanpa verifikasi. Set env var TELEGRAM_WEBHOOK_SECRET (string acak),")
    print("   lalu panggil ulang /set-webhook.")

if APPROVED_MEMBERS:
    print(f"✅ Approved members ({len(APPROVED_MEMBERS)}, dari DB: {DB_PATH}): {sorted(APPROVED_MEMBERS.keys())}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

ALLOWED_QUALITIES = [144, 240, 360, 480, 720, 1080, 1440, 2160]

# ---------- Konfigurasi fitur "Status WA HD" ----------
# Video status WhatsApp otomatis dikompres ulang sama WA sendiri saat diupload,
# dan itu yang bikin hasilnya burik walau file aslinya sudah bagus. Trik yang
# dipakai di sini: video di-encode ULANG dulu di server pakai setting (resolusi,
# bitrate, profile H.264) yang sudah mendekati/ berada di bawah ambang batas
# kompresi WA, sehingga saat WA "mengompres ulang" hasilnya nyaris tidak
# berubah lagi. ⚠️ Ini best-effort berdasarkan kebiasaan kompresi WA saat ini,
# BUKAN jaminan mutlak — algoritma kompresi WA bisa berubah kapan saja.
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"}
MAX_STATUS_UPLOAD_MB = int(os.getenv("MAX_STATUS_UPLOAD_MB", "150"))
# Kalau auto_trim dimatikan user, tetap kasih batas atas durasi sumber biar
# server nggak dipaksa encode video super panjang (mis. film utuh).
MAX_STATUS_SOURCE_SECONDS = int(os.getenv("MAX_STATUS_SOURCE_SECONDS", "600"))  # 10 menit
STATUS_HD_FFMPEG_TIMEOUT_SECONDS = int(os.getenv("STATUS_HD_FFMPEG_TIMEOUT_SECONDS", "300"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
URL_REGEX = re.compile(r"https?://\S+")

# ---------- Rate limiting sederhana untuk endpoint publik (/download, /proxy) ----------
# Endpoint ini bisa dipanggil siapa saja yang tahu URL-nya (bukan cuma dari web kita),
# jadi dibatasi per-IP biar server nggak dibanjiri/di-abuse orang lain.
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "10"))  # per window
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
_rate_limit_hits: dict[str, list[float]] = {}


def check_rate_limit(request: Request):
    """Raise HTTPException 429 kalau IP ini sudah melebihi batas request dalam window waktu.
    Pesan error menyertakan sisa detik tunggu (retry_after) biar frontend bisa
    tampilkan countdown yang jelas, bukan cuma teks generik."""
    client_ip = request.headers.get("x-forwarded-for", "")
    client_ip = client_ip.split(",")[0].strip() if client_ip else (request.client.host if request.client else "unknown")

    record_traffic_event()

    now = time.time()
    hits = [t for t in _rate_limit_hits.get(client_ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]

    if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
        oldest = min(hits)
        retry_after = max(1, int(RATE_LIMIT_WINDOW_SECONDS - (now - oldest)))
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"Terlalu banyak request. Coba lagi dalam {retry_after} detik ya.",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    hits.append(now)
    _rate_limit_hits[client_ip] = hits

    # Bersih-bersih sesekali biar dict nggak membengkak terus (IP lain yang lama nganggur)
    if len(_rate_limit_hits) > 500:
        for ip in list(_rate_limit_hits.keys()):
            _rate_limit_hits[ip] = [t for t in _rate_limit_hits[ip] if now - t < RATE_LIMIT_WINDOW_SECONDS]
            if not _rate_limit_hits[ip]:
                del _rate_limit_hits[ip]


# ---------- Proteksi Origin untuk endpoint publik (/download, /proxy, dst) ----------
# Sebelumnya endpoint ini bisa dipanggil siapa saja yang tahu domain Railway-nya,
# langsung, tanpa lewat web kita sama sekali (cuma dibatasi rate-limit per-IP).
# Sekarang ditambah pengecekan header Origin/Referer: request HARUS datang dari
# domain web resmi kita. Ini BUKAN proteksi sempurna (header bisa dipalsukan
# lewat curl/Postman), tapi cukup untuk mencegah orang random/bot nemenin
# nyedot API kita langsung dari browser/script kasual, dan tetap gratis dipakai
# dari web kita sendiri.
#
# Set env var ALLOWED_WEB_ORIGINS di Railway, comma-separated, contoh:
#   ALLOWED_WEB_ORIGINS=https://arvirmdn.github.io,https://arvirmdn.com
# Kalau env var ini KOSONG, pengecekan di-skip (biar tidak tiba-tiba mem-block
# semua orang kalau lupa di-set) — tapi akan muncul warning di log saat startup.
ALLOWED_WEB_ORIGINS = set(
    o.strip().rstrip("/") for o in os.getenv("ALLOWED_WEB_ORIGINS", "").split(",") if o.strip()
)
if ALLOWED_WEB_ORIGINS:
    print(f"✅ ALLOWED_WEB_ORIGINS aktif: {sorted(ALLOWED_WEB_ORIGINS)}")
else:
    print("⚠️  WARNING: ALLOWED_WEB_ORIGINS belum di-set — endpoint publik (/download, /proxy, dll) "
          "masih bisa diakses dari domain manapun. Set env var ini di Railway untuk membatasinya ke web kamu sendiri.")


def check_web_origin(request: Request):
    """Raise HTTPException 403 kalau request bukan dari domain web yang diizinkan.
    Di-skip total kalau ALLOWED_WEB_ORIGINS belum di-set (mode kompatibel/lama)."""
    if not ALLOWED_WEB_ORIGINS:
        return

    origin = (request.headers.get("origin") or "").rstrip("/")
    referer = request.headers.get("referer") or ""

    if origin and origin in ALLOWED_WEB_ORIGINS:
        return
    if referer and any(referer.startswith(o + "/") or referer.rstrip("/") == o for o in ALLOWED_WEB_ORIGINS):
        return

    raise HTTPException(
        status_code=403,
        detail="Akses ditolak: endpoint ini hanya bisa dipanggil dari web resminya.",
    )

# ---------- Monitor lonjakan traffic & error rate (notifikasi ke owner) ----------
# Sengaja sederhana (in-memory, per-instance) — cukup untuk kasih tahu owner kalau
# ada sesuatu yang nggak wajar (traffic melonjak / error bertubi-tubi), tanpa perlu
# infra monitoring terpisah. Ada cooldown biar owner nggak dispam notifikasi.
TRAFFIC_WINDOW_SECONDS = int(os.getenv("TRAFFIC_WINDOW_SECONDS", "60"))
TRAFFIC_SPIKE_THRESHOLD = int(os.getenv("TRAFFIC_SPIKE_THRESHOLD", "40"))   # request per window
ERROR_SPIKE_THRESHOLD = int(os.getenv("ERROR_SPIKE_THRESHOLD", "8"))        # error per window
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "600"))    # 10 menit

_traffic_hits: list[float] = []
_error_hits: list[float] = []
_last_alert_at: dict[str, float] = {"traffic": 0.0, "error": 0.0}


def _prune(hits: list[float], now: float, window: float) -> list[float]:
    return [t for t in hits if now - t < window]


def _notify_owner(message: str):
    """Kirim notifikasi khusus ke OWNER_USER_ID lewat bot (bukan TELEGRAM_CHAT_ID),
    supaya owner-nya pasti kebagian meski TELEGRAM_CHAT_ID beda akun."""
    if OWNER_USER_ID:
        send_bot_message(OWNER_USER_ID, message)
    else:
        send_telegram_notification(message)


def record_traffic_event():
    """Panggil tiap ada request masuk ke endpoint publik. Kirim alert sekali
    kalau jumlah request dalam window melebihi ambang batas."""
    now = time.time()
    global _traffic_hits
    _traffic_hits.append(now)
    _traffic_hits = _prune(_traffic_hits, now, TRAFFIC_WINDOW_SECONDS)

    if len(_traffic_hits) >= TRAFFIC_SPIKE_THRESHOLD and now - _last_alert_at["traffic"] > ALERT_COOLDOWN_SECONDS:
        _last_alert_at["traffic"] = now
        _notify_owner(
            f"🚨 *Lonjakan Traffic Terdeteksi!*\n"
            f"`{len(_traffic_hits)}` request dalam {TRAFFIC_WINDOW_SECONDS} detik terakhir "
            f"(ambang batas: {TRAFFIC_SPIKE_THRESHOLD})."
        )


def record_error_event(context: str = ""):
    """Panggil tiap ada error di endpoint publik. Kirim alert sekali kalau
    jumlah error dalam window melebihi ambang batas."""
    now = time.time()
    global _error_hits
    _error_hits.append(now)
    _error_hits = _prune(_error_hits, now, TRAFFIC_WINDOW_SECONDS)

    if len(_error_hits) >= ERROR_SPIKE_THRESHOLD and now - _last_alert_at["error"] > ALERT_COOLDOWN_SECONDS:
        _last_alert_at["error"] = now
        extra = f"\nContoh terakhir: `{context}`" if context else ""
        _notify_owner(
            f"⚠️ *Error Rate Tinggi Terdeteksi!*\n"
            f"`{len(_error_hits)}` error dalam {TRAFFIC_WINDOW_SECONDS} detik terakhir "
            f"(ambang batas: {ERROR_SPIKE_THRESHOLD}).{extra}"
        )


# Link yang lagi nunggu dipilih kualitasnya. Disimpan sebagai (url, created_at)
# supaya entry basi (user tidak pernah klik tombol kualitas) bisa dibersihkan —
# tanpa ini dict-nya cuma membesar terus selama server hidup (memory leak kecil).
PENDING: dict[str, tuple[str, float]] = {}
PENDING_TTL_SECONDS = 15 * 60  # 15 menit


def pending_add(url: str) -> str:
    """Simpan link ke PENDING, sekalian buang entry lama yang sudah kedaluwarsa."""
    now = time.time()
    expired = [k for k, (_, ts) in PENDING.items() if now - ts > PENDING_TTL_SECONDS]
    for k in expired:
        PENDING.pop(k, None)

    req_id = uuid.uuid4().hex[:10]
    PENDING[req_id] = (url, now)
    return req_id


def pending_pop(req_id: str) -> str | None:
    entry = PENDING.pop(req_id, None)
    return entry[0] if entry else None


def is_member(user_id: int) -> bool:
    """Check apakah user sudah di-approve untuk akses downloader.
    Kalau OWNER_USER_ID = 0, membership system disabled (semua user bisa akses).
    Kalau membership user ini punya expires_at dan sudah lewat, dia otomatis
    di-remove (lazy expiry — dicek tiap kali user interaksi, tanpa perlu cron)."""
    if OWNER_USER_ID == 0:  # membership system disabled
        return True
    if user_id not in APPROVED_MEMBERS:
        return False
    expires_at = APPROVED_MEMBERS[user_id]
    if expires_at is not None and time.time() > expires_at:
        remove_member(user_id)  # sudah kedaluwarsa, bersihkan otomatis
        return False
    return True


def format_expiry(expires_at: int | None) -> str:
    if expires_at is None:
        return "permanen"
    remaining = int(expires_at - time.time())
    if remaining <= 0:
        return "kedaluwarsa"
    days, rem = divmod(remaining, 86400)
    hours = rem // 3600
    if days > 0:
        return f"sisa {days} hari {hours} jam"
    return f"sisa {hours} jam"


def add_member(user_id: int, expires_at: int | None = None):
    """Add user ke approved members list (memory + DB persisten).
    expires_at: unix timestamp kapan membership habis, atau None untuk permanen."""
    APPROVED_MEMBERS[user_id] = expires_at
    db_add_member(user_id, expires_at)


def remove_member(user_id: int):
    """Remove user dari approved members list (memory + DB persisten)."""
    APPROVED_MEMBERS.pop(user_id, None)
    db_remove_member(user_id)


# ---------- util ----------

def md_escape(text: str) -> str:
    """Escape karakter spesial Markdown (legacy) supaya judul video sembarang
    nggak bikin Telegram menolak pesannya (parse error)."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def format_duration(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


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


def is_tiktok_photo_url(url: str) -> bool:
    """Post foto/slide (carousel) TikTok pakai path /photo/<id> di URL-nya,
    beda dari /video/<id> untuk video biasa. Ini cuma dipakai sebagai deteksi
    CEPAT lewat pola URL (buat skip langsung ke alur foto tanpa nyoba yt-dlp
    dulu) — bukan satu-satunya jalan deteksi, karena link pendek TikTok
    (vt.tiktok.com/xxx) tidak akan cocok pola ini sebelum di-resolve. Lihat
    fetch_video_info() untuk fallback kedua yang menangkap kasus link pendek."""
    lower = url.lower()
    return "tiktok.com" in lower and "/photo/" in lower


def estimate_filesize_bytes(info: dict, quality: int | None = None) -> int | None:
    """Cari estimasi ukuran file dari metadata yt-dlp. yt-dlp kadang punya
    'filesize' (pasti) atau cuma 'filesize_approx' (perkiraan) tergantung platform.
    Kalau ada beberapa format (belum dipilih quality-nya), ambil yang paling
    cocok dengan quality yang diminta."""
    direct = info.get("filesize") or info.get("filesize_approx")
    if direct:
        return int(direct)
    formats = info.get("formats") or []
    if not formats:
        return None
    candidates = [f for f in formats if f.get("filesize") or f.get("filesize_approx")]
    if not candidates:
        return None
    if quality:
        candidates = [f for f in candidates if (f.get("height") or 0) <= quality] or candidates
        chosen = max(candidates, key=lambda f: f.get("height") or 0)
    else:
        chosen = max(candidates, key=lambda f: (f.get("filesize") or f.get("filesize_approx") or 0))
    size = chosen.get("filesize") or chosen.get("filesize_approx")
    return int(size) if size else None


def format_filesize(num_bytes: int | None) -> str | None:
    if not num_bytes:
        return None
    mb = num_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


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


def build_audio_download_url(source_query: str, title: str) -> str | None:
    if not PUBLIC_DOMAIN:
        return None
    filename = sanitize_filename(title) + ".mp3"
    return (
        f"https://{PUBLIC_DOMAIN}/proxy-audio"
        f"?source={quote(source_query, safe='')}&filename={quote(filename)}"
    )


def resolve_spotify_title(spotify_url: str) -> str | None:
    """Ambil judul lagu dari link Spotify lewat oEmbed (Spotify sendiri
    memproteksi audionya/DRM, jadi nggak bisa diunduh langsung dari sana)."""
    try:
        oembed = requests.get("https://open.spotify.com/oembed", params={"url": spotify_url}, timeout=10).json()
        return (oembed.get("title") or "").strip() or None
    except Exception:
        return None


MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "25"))


def is_youtube_playlist(url: str) -> bool:
    """Link YouTube dianggap playlist kalau ada parameter 'list=' dan BUKAN
    cuma video biasa yang kebetulan dibuka dari dalam playlist (watch?v=...&list=...
    tetap dianggap video tunggal, biar tidak mengejutkan user yang paste link video biasa)."""
    lower = url.lower()
    if "list=" not in lower:
        return False
    if "youtube.com/playlist" in lower:
        return True
    # watch?v=xxx&list=yyy -> masih video tunggal, bukan mode playlist
    if "watch" in lower and "v=" in lower:
        return False
    return "youtube.com" in lower or "youtu.be" in lower


def fetch_playlist_entries(url: str):
    """Ambil daftar video dalam sebuah playlist YouTube (flat, tanpa resolve
    format tiap video satu-satu biar cepat). Dibatasi MAX_PLAYLIST_ITEMS entri."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": MAX_PLAYLIST_ITEMS,
        "socket_timeout": 20,
        "http_headers": {"User-Agent": USER_AGENT},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return None, f"Gagal membaca playlist: {e}"
    except Exception as e:
        return None, f"Terjadi kesalahan server: {e}"

    entries = info.get("entries") or []
    if not entries:
        return None, "Playlist ini kosong atau tidak bisa diakses (mungkin private)."

    items = []
    for e in entries[:MAX_PLAYLIST_ITEMS]:
        if not e:
            continue
        video_id = e.get("id")
        items.append({
            "title": e.get("title") or "Video",
            "url": e.get("url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None),
            "duration": e.get("duration") or 0,
            "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url") if e.get("thumbnails") else None,
        })
    items = [it for it in items if it["url"]]
    if not items:
        return None, "Tidak ada video valid yang bisa dibaca dari playlist ini."

    return {
        "status": "success",
        "playlist_title": info.get("title") or "Playlist",
        "total_found": len(items),
        "truncated": len(entries) > MAX_PLAYLIST_ITEMS,
        "items": items,
    }, None


def fetch_video_info(url: str, quality: int = 720):
    if quality not in ALLOWED_QUALITIES:
        quality = min(ALLOWED_QUALITIES, key=lambda q: abs(q - quality))

    # Deteksi cepat lewat pola URL (skip yt-dlp sama sekali kalau sudah jelas
    # post foto/slide, biar tidak buang waktu nyoba yt-dlp yang pasti gagal).
    if is_tiktok_photo_url(url):
        return fetch_tiktok_photo_info(url)

    try:
        with yt_dlp.YoutubeDL(build_ydl_opts(quality)) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        # yt-dlp TIDAK PUNYA extractor untuk pola URL /photo/ TikTok sama
        # sekali (selalu "Unsupported URL", bukan soal pilihan format) —
        # jadi kalau link pendek (vt.tiktok.com/xxx) ternyata resolve ke post
        # foto, baru ketahuan di sini (bukan dari pola URL di atas, karena
        # link pendek belum kelihatan /photo/-nya). Coba sebagai foto sebelum
        # nyerah total.
        if "tiktok.com" in url.lower() and "Unsupported URL" in str(e):
            photo_result, photo_error = fetch_tiktok_photo_info(url)
            if photo_result:
                return photo_result, None
        return None, f"Gagal memproses link: {e}"
    except Exception as e:
        return None, f"Terjadi kesalahan server: {e}"

    video_url = extract_video_url(info, quality)
    if not video_url:
        return None, "Tidak ada format video yang cocok ditemukan untuk kualitas ini."

    duration = info.get("duration", 0)
    if duration and duration > MAX_DURATION_MINUTES * 60:
        return None, (
            f"Video ini berdurasi {format_duration(duration)}, melebihi batas "
            f"{MAX_DURATION_MINUTES} menit yang ditetapkan biar server nggak kebebanan."
        )

    title = info.get("title", "Video")
    platform = info.get("extractor", "unknown")
    filesize_bytes = estimate_filesize_bytes(info, quality)
    return {
        "status": "success",
        "type": "video",
        "title": title,
        "video_url": video_url,
        "download_url": build_download_url(url, title, quality),
        "thumbnail": info.get("thumbnail", ""),
        "duration": duration,
        "platform": platform,
        "quality": quality,
        "filesize_bytes": filesize_bytes,
        "filesize_label": format_filesize(filesize_bytes),
    }, None


def fetch_tiktok_photo_info(url: str):
    """Khusus post TikTok mode foto/slide (carousel). PAKAI TikWM, BUKAN
    yt-dlp — sudah dikonfirmasi lewat riset (termasuk laporan bug yt-dlp
    sendiri) bahwa yt-dlp belum punya extractor untuk pola URL /photo/ TikTok
    sama sekali, jadi selalu gagal dengan 'Unsupported URL' apapun opsinya.
    TikWM sudah lama mendukung mode foto/slide ini (field 'images' di
    response-nya), termasuk resolve link pendek (vt.tiktok.com) sendiri."""
    try:
        resp = requests.get(
            "https://www.tikwm.com/api/",
            params={"url": url},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return None, f"Gagal memproses link: {e}"

    data = payload.get("data") or {}
    photos = data.get("images") or []
    if not photos:
        return None, "Tidak ada foto yang ditemukan pada postingan TikTok ini (atau link ini bukan post foto/slide)."

    title = data.get("title") or "TikTok Photo"
    audio_url = data.get("music") or None
    return {
        "status": "success",
        "type": "photo",
        "title": title,
        "photos": photos,
        "photo_count": len(photos),
        "audio_url": audio_url,
        "thumbnail": photos[0],
        "platform": "tiktok",
    }, None


def fetch_audio_info(url: str):
    """Ambil info audio (dipakai endpoint web /download-audio). Mendukung link
    Spotify juga: dicari otomatis versi audionya di YouTube via judul oEmbed."""
    display_title = None
    platform = "audio"

    if is_spotify(url):
        track_title = resolve_spotify_title(url)
        if not track_title:
            return None, "Gagal membaca info lagu dari link Spotify ini. Pastikan link-nya valid & publik."
        search_query = f"ytsearch1:{track_title} audio"
        display_title = track_title
        platform = "spotify (via youtube)"
    else:
        search_query = url

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "socket_timeout": 20,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "http_headers": {"User-Agent": USER_AGENT},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if info.get("entries"):
                info = info["entries"][0]
    except yt_dlp.utils.DownloadError as e:
        return None, f"Gagal memproses link: {e}"
    except Exception as e:
        return None, f"Terjadi kesalahan server: {e}"

    duration = info.get("duration", 0)
    if duration and duration > MAX_DURATION_MINUTES * 60:
        return None, (
            f"Audio ini berdurasi {format_duration(duration)}, melebihi batas "
            f"{MAX_DURATION_MINUTES} menit yang ditetapkan biar server nggak kebebanan."
        )

    title = display_title or info.get("title", "Audio")
    audio_url = info.get("url")
    if not audio_url:
        candidates = [f for f in info.get("formats", []) if f.get("url") and f.get("acodec") != "none"]
        audio_url = max(candidates, key=lambda f: f.get("abr") or 0).get("url") if candidates else None
    if not audio_url:
        return None, "Tidak ada format audio yang cocok ditemukan."

    filesize_bytes = estimate_filesize_bytes(info)
    return {
        "status": "success",
        "title": title,
        "audio_url": audio_url,
        "download_url": build_audio_download_url(search_query, title),
        "thumbnail": info.get("thumbnail", ""),
        "duration": duration,
        "platform": platform if not is_spotify(url) else platform,
        "filesize_bytes": filesize_bytes,
        "filesize_label": format_filesize(filesize_bytes),
    }, None


def download_video_file(source_url: str, quality: int, progress_hook=None):
    if quality not in ALLOWED_QUALITIES:
        quality = min(ALLOWED_QUALITIES, key=lambda q: abs(q - quality))
    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    ydl_opts = build_ydl_opts(quality)
    ydl_opts["outtmpl"] = os.path.join(tmp_dir, "%(id)s.%(ext)s")
    ydl_opts["noprogress"] = True
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]
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


def download_audio_file(source_url: str, progress_hook=None):
    """Unduh & convert ke MP3. source_url boleh URL biasa ATAU query pencarian
    yt-dlp seperti 'ytsearch1:judul lagu' (dipakai untuk fitur Spotify)."""
    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "socket_timeout": 20,
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "http_headers": {"User-Agent": USER_AGENT},
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]
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


# ---------- Status WA HD (upload video user, re-encode di server) ----------

def ffprobe_duration_seconds(path: str) -> float | None:
    """Return durasi video dalam detik, atau None kalau file bukan video valid."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except Exception:
        return None


def convert_to_status_hd(input_path: str, output_path: str, trim_seconds: int | None = None):
    """Re-encode video dengan setting yang diracik biar WA nggak perlu
    mengompres ulang secara agresif saat diupload ke Status:
    - Lebar dibatasi maks 720px (tinggi menyesuaikan, kelipatan 2) — status WA
      toh ditampilkan nggak lebih besar dari itu di layar HP.
    - H.264 profile "main" + CRF rendah (detail tetap tajam) tapi bitrate
      di-cap (maxrate/bufsize) supaya file nggak digelembungkan sia-sia.
    - Audio AAC 128kbps, +faststart supaya video langsung bisa diputar/
      diupload tanpa perlu "moov atom" dipindah dulu.
    """
    cmd = ["ffmpeg", "-y", "-i", input_path]
    if trim_seconds:
        cmd += ["-t", str(trim_seconds)]
    cmd += [
        "-vf", "scale='min(720,iw)':-2:flags=lanczos",
        "-c:v", "libx264",
        "-profile:v", "main",
        "-level", "4.0",
        "-preset", "medium",
        "-crf", "20",
        "-maxrate", "1300k",
        "-bufsize", "2600k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=STATUS_HD_FFMPEG_TIMEOUT_SECONDS)


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


def send_bot_message(chat_id, text: str, reply_markup: dict | None = None) -> int | None:
    """Kirim pesan, return message_id-nya (atau None kalau gagal)."""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = tg_call("sendMessage", payload)
    if resp and resp.get("ok"):
        return resp["result"]["message_id"]
    return None


def edit_bot_message(chat_id, message_id, text: str):
    if not message_id:
        return
    tg_call("editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown",
    })


def delete_bot_message(chat_id, message_id):
    if not message_id:
        return
    tg_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def answer_callback(callback_id: str, text: str | None = None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    tg_call("answerCallbackQuery", payload)


def send_telegram_notification(message: str):
    """Notifikasi ke akun pribadimu (TELEGRAM_CHAT_ID) — log sukses/gagal dari endpoint web."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    tg_call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})


def send_bot_download_link(chat_id, title: str, platform: str, quality: int, download_url: str | None, video_url: str):
    caption = f"🎬 *{md_escape(title)}*\nPlatform: {platform.upper()} • {quality}p"
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


def send_bot_photo_album(chat_id, photo_urls: list[str], title: str) -> bool:
    """Kirim beberapa foto sekaligus sebagai album via sendMediaGroup.
    Telegram membatasi maks 10 media per pemanggilan, jadi di-chunk kalau
    postingannya lebih dari 10 foto (caption cuma ditaruh di foto pertama
    biar tidak dobel)."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    caption = f"🖼️ {title}"[:1024]
    any_ok = False
    for i in range(0, len(photo_urls), 10):
        chunk = photo_urls[i:i + 10]
        media = []
        for j, u in enumerate(chunk):
            item = {"type": "photo", "media": u}
            if i == 0 and j == 0:
                item["caption"] = caption
            media.append(item)
        resp = tg_call("sendMediaGroup", {"chat_id": chat_id, "media": media})
        if resp and resp.get("ok"):
            any_ok = True
    return any_ok


def send_bot_audio_file(chat_id, filepath: str, title: str, platform: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    caption = f"🎵 {title}\nSumber: {platform}"[:1024]
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


def make_progress_hook(chat_id, message_id, base_text: str):
    """Update pesan status secara berkala (maks tiap 3 detik, biar nggak kena rate limit Telegram)."""
    state = {"last_edit": 0.0}

    def hook(d):
        if not message_id:
            return
        status = d.get("status")
        if status == "downloading":
            now = time.time()
            if now - state["last_edit"] < 3:
                return
            state["last_edit"] = now
            percent = (d.get("_percent_str") or "").strip()
            speed = (d.get("_speed_str") or "").strip()
            eta = (d.get("_eta_str") or "").strip()
            extra = " • ".join(x for x in [percent, speed, f"ETA {eta}" if eta else ""] if x)
            text = f"{base_text}\n\n📊 {extra}" if extra else base_text
            edit_bot_message(chat_id, message_id, text)
        elif status == "finished":
            edit_bot_message(chat_id, message_id, f"{base_text}\n\n🔄 Memproses file akhir...")

    return hook


# ---------- Maintenance: cleanup tmp dir & sweep member kedaluwarsa ----------

def cleanup_orphaned_tmp_dirs():
    """Hapus folder temp bekas download yang nyangkut kalau server sebelumnya
    crash/force-restart di tengah proses (jadi finally/BackgroundTask cleanup-nya
    nggak sempat jalan). Aman dipanggil saat startup karena di titik ini belum
    ada download yang sedang berjalan sama sekali."""
    base = tempfile.gettempdir()
    prefixes = ("dl_", "zip_", "photozip_")
    removed = 0
    for prefix in prefixes:
        for path in glob.glob(os.path.join(base, f"{prefix}*")):
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
    if removed:
        print(f"🧹 Startup cleanup: {removed} folder temp sisa sesi sebelumnya dihapus.")


EXPIRED_MEMBER_SWEEP_INTERVAL_SECONDS = int(os.getenv("EXPIRED_MEMBER_SWEEP_INTERVAL_SECONDS", str(6 * 3600)))


def sweep_expired_members():
    """Hapus member yang expires_at-nya sudah lewat, walau dia nggak pernah
    interaksi lagi ke bot (is_member() cuma cek expiry pas dipakai, jadi kalau
    member itu diam aja, datanya nyangkut terus tanpa sweep ini)."""
    now = time.time()
    expired_ids = [uid for uid, exp in list(APPROVED_MEMBERS.items()) if exp is not None and now > exp]
    for uid in expired_ids:
        remove_member(uid)
    if expired_ids:
        print(f"🧹 Sweep member kedaluwarsa: {len(expired_ids)} dihapus -> {expired_ids}")
        _notify_owner(f"🧹 *Sweep otomatis*\n{len(expired_ids)} member kedaluwarsa sudah dihapus dari daftar.")


async def expired_member_sweep_loop():
    while True:
        try:
            sweep_expired_members()
        except Exception as e:
            print(f"⚠️  Sweep member kedaluwarsa gagal: {e}")
        await asyncio.sleep(EXPIRED_MEMBER_SWEEP_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup():
    cleanup_orphaned_tmp_dirs()
    sweep_expired_members()  # sekali di awal, biar nggak nunggu interval pertama
    asyncio.create_task(expired_member_sweep_loop())


# ---------- Endpoint web (dipakai frontend Blogspot/Vercel) ----------

@app.get("/")
async def root():
    return {
        "message": "yt-dlp API is running. Support TikTok, YouTube, Instagram, Facebook, Twitter, Vimeo, Spotify(via YouTube), dan 1000+ platform lainnya.",
        "endpoints": [
            "/download?url=...&quality=720",
            "/proxy?source=...&quality=720",
            "/download-audio?url=...",
            "/proxy-audio?source=...",
            "/download-photo?url=... (TikTok foto/slide)",
            "/proxy-image?source=...&filename=... (TikTok foto/slide)",
            "/download-photos-zip (POST, body: {url}) (TikTok foto/slide)",
            "/playlist-info?url=... (YouTube playlist)",
            "/download-zip (POST, body: [{url, quality}])",
            "/status-hd (POST, form-data: file, query: auto_trim=true/false) — Status WA HD",
            "/health",
            "/telegram-webhook (POST)",
        ],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/download")
async def download_video(request: Request, url: str = Query(...), quality: int = Query(720)):
    check_web_origin(request)
    check_rate_limit(request)
    result, error = fetch_video_info(url, quality)
    if error:
        record_error_event(error)
        send_telegram_notification(f"❌ *Gagal Memproses Link!*\n• URL: {url}\n• Error: `{error}`")
        status_code = 404 if "Tidak ada format" in error else 422
        raise HTTPException(status_code=status_code, detail=error)

    send_telegram_notification(
        f"📥 *Unduhan Berhasil!*\n• Judul: {result['title']}\n"
        f"• Platform: {result['platform'].upper()}\n• Kualitas: {quality}p\n• URL: {url}"
    )
    return result


@app.get("/playlist-info")
async def playlist_info(request: Request, url: str = Query(...)):
    """Dipakai frontend ketika mendeteksi link playlist YouTube: mengembalikan
    daftar video di dalamnya (maks MAX_PLAYLIST_ITEMS) supaya user bisa pilih
    beberapa video sekaligus, alih-alih cuma bisa 1 link penuh manual."""
    check_web_origin(request)
    check_rate_limit(request)
    result, error = fetch_playlist_entries(url)
    if error:
        record_error_event(error)
        raise HTTPException(status_code=422, detail=error)
    return result


@app.get("/download-photo")
async def download_photo(request: Request, url: str = Query(...)):
    """Khusus post TikTok mode foto/slide (carousel) — balikin daftar URL
    foto (bukan satu file video), dipakai frontend saat link terdeteksi
    /photo/ di URL TikTok-nya."""
    check_web_origin(request)
    check_rate_limit(request)
    result, error = fetch_tiktok_photo_info(url)
    if error:
        record_error_event(error)
        send_telegram_notification(f"❌ *Gagal Memproses Foto TikTok!*\n• URL: {url}\n• Error: `{error}`")
        status_code = 404 if "Tidak ada" in error else 422
        raise HTTPException(status_code=status_code, detail=error)

    send_telegram_notification(
        f"🖼️ *Unduhan Foto TikTok Berhasil!*\n• Judul: {result['title']}\n"
        f"• Jumlah foto: {result['photo_count']}\n• URL: {url}"
    )
    return result


@app.get("/proxy-image")
async def proxy_image(request: Request, source: str = Query(...), filename: str = Query("photo.jpg")):
    """Stream 1 foto TikTok lewat server kita, biar tombol Download di web
    beneran men-trigger unduhan file (bukan cuma buka tab baru) dan supaya
    permintaan tetap kebawa header Referer yang benar ke CDN TikTok."""
    check_web_origin(request)
    check_rate_limit(request)
    try:
        resp = requests.get(
            source,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.tiktok.com/"},
            timeout=30,
            stream=True,
        )
        resp.raise_for_status()
    except Exception as e:
        record_error_event(str(e))
        raise HTTPException(status_code=502, detail=f"Gagal mengunduh foto: {e}")

    return StreamingResponse(
        resp.iter_content(chunk_size=65536),
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class PhotoZipRequest(BaseModel):
    url: str


@app.post("/download-photos-zip")
async def download_photos_zip(request: Request, body: PhotoZipRequest = Body(...)):
    """Bungkus semua foto dari 1 post TikTok foto/slide jadi satu file .zip."""
    check_web_origin(request)
    check_rate_limit(request)

    result, error = fetch_tiktok_photo_info(body.url)
    if error:
        record_error_event(error)
        raise HTTPException(status_code=404 if "Tidak ada" in error else 422, detail=error)

    zip_tmp_dir = tempfile.mkdtemp(prefix="photozip_")
    zip_path = os.path.join(zip_tmp_dir, "photos.zip")
    base_name = sanitize_filename(result["title"])
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, photo_url in enumerate(result["photos"], start=1):
                try:
                    r = requests.get(
                        photo_url,
                        headers={"User-Agent": USER_AGENT, "Referer": "https://www.tiktok.com/"},
                        timeout=30,
                    )
                    r.raise_for_status()
                    zf.writestr(f"{base_name}-{i}.jpg", r.content)
                except Exception as e:
                    zf.writestr(f"GAGAL-{i}.txt", f"Gagal mengunduh foto {i}: {e}")
    except Exception as e:
        shutil.rmtree(zip_tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"Gagal membuat ZIP foto: {e}")

    send_telegram_notification(
        f"🗜️ *Unduhan ZIP Foto TikTok*\n• Judul: {result['title']}\n• Jumlah: {result['photo_count']}"
    )

    return FileResponse(
        zip_path, media_type="application/zip", filename=f"{base_name}.zip",
        background=BackgroundTask(lambda: shutil.rmtree(zip_tmp_dir, ignore_errors=True)),
    )


@app.get("/proxy")
async def proxy_download(request: Request, source: str = Query(...), quality: int = Query(720), filename: str = Query("video.mp4")):
    check_web_origin(request)
    check_rate_limit(request)
    try:
        filepath, tmp_dir = download_video_file(source, quality)
    except Exception as e:
        record_error_event(str(e))
        raise HTTPException(status_code=502, detail=f"Gagal mengunduh video: {e}")
    return FileResponse(
        filepath, media_type="video/mp4", filename=filename,
        background=BackgroundTask(lambda: shutil.rmtree(tmp_dir, ignore_errors=True)),
    )


@app.get("/download-audio")
async def download_audio(request: Request, url: str = Query(...)):
    """Versi web dari fitur '🎵 Audio (MP3)' bot Telegram. Mendukung link biasa
    (TikTok/YouTube/IG/FB/Twitter/Vimeo) maupun Spotify (dicari otomatis di YouTube)."""
    check_web_origin(request)
    check_rate_limit(request)
    result, error = fetch_audio_info(url)
    if error:
        record_error_event(error)
        send_telegram_notification(f"❌ *Gagal Memproses Link Audio!*\n• URL: {url}\n• Error: `{error}`")
        status_code = 404 if "Tidak ada format" in error else 422
        raise HTTPException(status_code=status_code, detail=error)

    send_telegram_notification(
        f"🎵 *Unduhan Audio Berhasil!*\n• Judul: {result['title']}\n"
        f"• Platform: {result['platform']}\n• URL: {url}"
    )
    return result


@app.get("/proxy-audio")
async def proxy_download_audio(request: Request, source: str = Query(...), filename: str = Query("audio.mp3")):
    check_web_origin(request)
    check_rate_limit(request)
    try:
        filepath, tmp_dir = download_audio_file(source)
    except Exception as e:
        record_error_event(str(e))
        raise HTTPException(status_code=502, detail=f"Gagal mengunduh audio: {e}")
    return FileResponse(
        filepath, media_type="audio/mpeg", filename=filename,
        background=BackgroundTask(lambda: shutil.rmtree(tmp_dir, ignore_errors=True)),
    )


# ---------- Batch ZIP download (web) ----------

class ZipItem(BaseModel):
    url: str
    quality: str = "720"  # "360" / "720" / "1080" / "1440" / "2160" / "mp3"


@app.post("/download-zip")
async def download_zip(request: Request, items: list[ZipItem] = Body(...)):
    """Unduh beberapa link sekaligus (maks MAX_LINKS_PER_MESSAGE) dan bungkus
    jadi satu file .zip. Item yang gagal tidak menggagalkan seluruh proses —
    statusnya dicatat di manifest.txt di dalam ZIP-nya."""
    check_web_origin(request)
    check_rate_limit(request)

    if not items:
        raise HTTPException(status_code=400, detail="Tidak ada link yang dikirim.")
    items = items[:MAX_LINKS_PER_MESSAGE]

    zip_tmp_dir = tempfile.mkdtemp(prefix="zip_")
    zip_path = os.path.join(zip_tmp_dir, "downloads.zip")
    cleanup_dirs = [zip_tmp_dir]
    manifest_lines = []
    used_names: set[str] = set()

    def unique_arcname(base: str) -> str:
        name = base
        i = 2
        while name in used_names:
            root, ext = os.path.splitext(base)
            name = f"{root} ({i}){ext}"
            i += 1
        used_names.add(name)
        return name

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, item in enumerate(items, start=1):
                url = item.url.strip()
                quality = (item.quality or "720").strip().lower()
                try:
                    if is_spotify(url) or quality == "mp3":
                        info, error = fetch_audio_info(url)
                        if error:
                            manifest_lines.append(f"[GAGAL] {url} — {error}")
                            continue
                        source = f"ytsearch1:{info['title']} audio" if is_spotify(url) else url
                        filepath, tmp_dir = download_audio_file(source)
                    else:
                        try:
                            q = int(quality)
                        except ValueError:
                            q = 720
                        info, error = fetch_video_info(url, q)
                        if error:
                            manifest_lines.append(f"[GAGAL] {url} — {error}")
                            continue
                        if info.get("type") == "photo":
                            manifest_lines.append(
                                f"[GAGAL] {url} — Link ini post foto/slide TikTok, belum didukung di ZIP batch video. "
                                "Pakai tombol Download khusus foto TikTok di halaman utama."
                            )
                            continue
                        filepath, tmp_dir = download_video_file(url, q)

                    cleanup_dirs.append(tmp_dir)
                    ext = os.path.splitext(filepath)[1]
                    safe_name = unique_arcname(sanitize_filename(info["title"]) + ext)
                    zf.write(filepath, arcname=safe_name)
                    manifest_lines.append(f"[OK] {safe_name}")
                except Exception as e:
                    manifest_lines.append(f"[GAGAL] {url} — {e}")

            zf.writestr("manifest.txt", "\n".join(manifest_lines))
    except Exception as e:
        for d in cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"Gagal membuat ZIP: {e}")

    ok_count = sum(1 for l in manifest_lines if l.startswith("[OK]"))
    send_telegram_notification(
        f"🗜️ *Unduhan ZIP (Web)*\n• Berhasil: {ok_count}/{len(items)} link"
    )

    def cleanup_all():
        for d in cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)

    return FileResponse(
        zip_path, media_type="application/zip", filename="downloads.zip",
        background=BackgroundTask(cleanup_all),
    )


@app.post("/status-hd")
async def status_hd(request: Request, file: UploadFile = File(...), auto_trim: bool = Query(True)):
    """Upload video dari HP/PC user → di-encode ulang di server dengan setting
    yang diracik biar hasil forward ke Status WhatsApp nggak dikompres ulang
    sampai burik (lihat catatan di ALLOWED_VIDEO_EXTENSIONS di atas).

    auto_trim=True (default): video >30 detik otomatis dipotong ke 30 detik
    pertama, sesuai batas maksimal 1 segmen Status WA. Set auto_trim=false
    kalau mau full-length (tetap dibatasi MAX_STATUS_SOURCE_SECONDS)."""
    check_web_origin(request)
    check_rate_limit(request)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Format file tidak didukung. Gunakan salah satu: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}",
        )

    tmp_dir = tempfile.mkdtemp(prefix="statushd_")
    input_path = os.path.join(tmp_dir, f"input{ext}")
    output_path = os.path.join(tmp_dir, "status_wa_hd.mp4")

    size = 0
    max_bytes = MAX_STATUS_UPLOAD_MB * 1024 * 1024
    try:
        with open(input_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File terlalu besar (maks {MAX_STATUS_UPLOAD_MB}MB).",
                    )
                f.write(chunk)
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        record_error_event(f"status-hd upload: {e}")
        raise HTTPException(status_code=400, detail="Gagal membaca file yang diupload.")

    duration = ffprobe_duration_seconds(input_path)
    if duration is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail="File bukan video yang valid atau rusak.")

    trim_seconds = None
    if auto_trim and duration > 30:
        trim_seconds = 30
    elif not auto_trim and duration > MAX_STATUS_SOURCE_SECONDS:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Video {int(duration)} detik terlalu panjang untuk diproses tanpa potong otomatis "
                f"(maks {MAX_STATUS_SOURCE_SECONDS // 60} menit). Aktifkan opsi potong ke 30 detik pertama."
            ),
        )

    try:
        convert_to_status_hd(input_path, output_path, trim_seconds=trim_seconds)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        record_error_event(f"status-hd ffmpeg gagal: {e}")
        raise HTTPException(status_code=500, detail="Gagal memproses video di server.")
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        record_error_event("status-hd ffmpeg timeout")
        raise HTTPException(status_code=504, detail="Proses video terlalu lama dan dihentikan.")

    if not os.path.exists(output_path):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Video hasil konversi tidak ditemukan di server.")

    send_telegram_notification(
        f"📼 *Konversi Status WA HD (Web)*\n• Durasi asli: {int(duration)}s"
        + (" (dipotong ke 30s)" if trim_seconds else "")
    )

    def cleanup():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return FileResponse(
        output_path, media_type="video/mp4", filename="status_wa_hd.mp4",
        background=BackgroundTask(cleanup),
    )


# ---------- Bot Telegram ----------

def extract_urls(text: str) -> list[str]:
    return URL_REGEX.findall(text or "")


def is_spotify(url: str) -> bool:
    return "spotify.com" in url.lower()


def process_spotify(chat_id, spotify_url: str, status_message_id: int | None) -> bool:
    if status_message_id:
        edit_bot_message(chat_id, status_message_id, "🎧 Spotify terdeteksi — mencari versi audionya di YouTube...")
    else:
        status_message_id = send_bot_message(chat_id, "🎧 Spotify terdeteksi — mencari versi audionya di YouTube...")

    try:
        oembed = requests.get("https://open.spotify.com/oembed", params={"url": spotify_url}, timeout=10).json()
        track_title = (oembed.get("title") or "").strip()
    except Exception:
        track_title = ""

    if not track_title:
        text = "❌ Gagal membaca info lagu dari link Spotify ini. Pastikan link-nya valid & publik."
        if status_message_id:
            edit_bot_message(chat_id, status_message_id, text)
        else:
            send_bot_message(chat_id, text)
        return False

    esc_title = md_escape(track_title)
    base_text = f"📥 Mengunduh audio\n*{esc_title}*\n_dicari otomatis di YouTube_"
    edit_bot_message(chat_id, status_message_id, base_text)

    hook = make_progress_hook(chat_id, status_message_id, base_text)
    tmp_dir = None
    try:
        filepath, tmp_dir = download_audio_file(f"ytsearch1:{track_title} audio", progress_hook=hook)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)

        if size_mb > TELEGRAM_MAX_UPLOAD_MB:
            delete_bot_message(chat_id, status_message_id)
            send_bot_message(
                chat_id,
                f"⚠️ File audio untuk *{esc_title}* ternyata {size_mb:.1f}MB, kelewat besar buat dikirim langsung "
                f"(maks {TELEGRAM_MAX_UPLOAD_MB}MB). Spotify sendiri tidak menyediakan link download, jadi coba cari "
                "manual versi lebih pendek di YouTube ya."
            )
            return False

        edit_bot_message(chat_id, status_message_id, f"📤 Mengirim *{esc_title}*...")
        ok = send_bot_audio_file(chat_id, filepath, track_title, "Spotify (dicari via YouTube)")
        delete_bot_message(chat_id, status_message_id)
        if not ok:
            send_bot_message(chat_id, f"⚠️ Gagal mengirim audio untuk *{esc_title}*, coba lagi beberapa saat lagi.")
        return ok
    except Exception as e:
        edit_bot_message(chat_id, status_message_id, f"❌ Gagal mengunduh audio untuk *{esc_title}*:\n{e}")
        return False
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def process_and_deliver(chat_id, url: str, quality: int, audio_only: bool, status_message_id: int | None = None) -> bool:
    if status_message_id:
        edit_bot_message(chat_id, status_message_id, "🔎 Mengambil info video...")
    else:
        status_message_id = send_bot_message(chat_id, "🔎 Mengambil info video...")

    result, error = fetch_video_info(url, quality)
    if error:
        text = f"❌ Gagal memproses link:\n{error}"
        if status_message_id:
            edit_bot_message(chat_id, status_message_id, text)
        else:
            send_bot_message(chat_id, text)
        return False

    if result.get("type") == "photo":
        # Ternyata link pendek TikTok ini resolve ke post foto/slide (baru
        # ketahuan di sini, bukan dari pola URL, karena link pendek belum
        # kelihatan /photo/-nya sebelum di-resolve) — alihkan ke pengiriman
        # album foto, pakai hasil yang sudah didapat (tidak perlu fetch ulang).
        return process_tiktok_photo(chat_id, url, status_message_id, result=result)

    duration = result.get("duration") or 0
    if duration and duration > MAX_DURATION_MINUTES * 60:
        text = (
            f"⚠️ Video *{md_escape(result['title'])}* berdurasi {format_duration(duration)}, "
            f"melebihi batas {MAX_DURATION_MINUTES} menit yang aku tetapkan biar server nggak kebebanan.\n"
            "Coba video lain yang lebih pendek ya."
        )
        if status_message_id:
            edit_bot_message(chat_id, status_message_id, text)
        else:
            send_bot_message(chat_id, text)
        return False

    esc_title = md_escape(result["title"])
    label = "🎵 Audio MP3" if audio_only else f"🎬 Video {quality}p"
    dur_txt = f" • {format_duration(duration)}" if duration else ""
    base_text = f"📥 Mengunduh {label}\n*{esc_title}*{dur_txt}"
    edit_bot_message(chat_id, status_message_id, base_text)

    hook = make_progress_hook(chat_id, status_message_id, base_text)
    tmp_dir = None
    try:
        if audio_only:
            filepath, tmp_dir = download_audio_file(url, progress_hook=hook)
        else:
            filepath, tmp_dir = download_video_file(url, quality, progress_hook=hook)

        size_mb = os.path.getsize(filepath) / (1024 * 1024)

        if size_mb > TELEGRAM_MAX_UPLOAD_MB:
            delete_bot_message(chat_id, status_message_id)
            send_bot_message(
                chat_id,
                f"⚠️ File *{esc_title}* ternyata {size_mb:.1f}MB, kelewat besar buat dikirim langsung "
                f"(maks {TELEGRAM_MAX_UPLOAD_MB}MB dari Telegram). Ini link download-nya:"
            )
            send_bot_download_link(chat_id, result["title"], result["platform"], quality, result["download_url"], result["video_url"])
            return True

        edit_bot_message(chat_id, status_message_id, f"📤 Mengirim *{esc_title}*...")

        if audio_only:
            ok = send_bot_audio_file(chat_id, filepath, result["title"], result["platform"])
        else:
            ok = send_bot_video_file(chat_id, filepath, result["title"], result["platform"], quality)

        delete_bot_message(chat_id, status_message_id)

        if not ok:
            send_bot_message(chat_id, f"⚠️ Gagal kirim *{esc_title}* langsung, ini link download-nya sebagai gantinya:")
            send_bot_download_link(chat_id, result["title"], result["platform"], quality, result["download_url"], result["video_url"])
        return True
    except Exception as e:
        edit_bot_message(chat_id, status_message_id, f"❌ Gagal mengunduh *{esc_title}*:\n{e}")
        return False
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def process_tiktok_photo(chat_id, url: str, status_message_id: int | None = None, result: dict | None = None) -> bool:
    if result is None:
        if status_message_id:
            edit_bot_message(chat_id, status_message_id, "🖼️ Mengambil foto-foto TikTok...")
        else:
            status_message_id = send_bot_message(chat_id, "🖼️ Mengambil foto-foto TikTok...")

        result, error = fetch_tiktok_photo_info(url)
        if error:
            text = f"❌ Gagal memproses link:\n{error}"
            if status_message_id:
                edit_bot_message(chat_id, status_message_id, text)
            else:
                send_bot_message(chat_id, text)
            return False
    elif not status_message_id:
        status_message_id = send_bot_message(chat_id, "🖼️ Menyiapkan foto-foto TikTok...")

    esc_title = md_escape(result["title"])
    photos = result["photos"]
    edit_bot_message(chat_id, status_message_id, f"📤 Mengirim {len(photos)} foto...\n*{esc_title}*")

    ok = send_bot_photo_album(chat_id, photos, result["title"])
    delete_bot_message(chat_id, status_message_id)

    if not ok:
        send_bot_message(chat_id, f"⚠️ Gagal mengirim foto untuk *{esc_title}*, coba lagi beberapa saat lagi.")
    return ok


def process_link(chat_id, url: str, quality: int = 720, audio_only: bool = False, status_message_id: int | None = None) -> bool:
    # Dihitung untuk /stats. Ini menghitung "link yang diproses" (bukan jaminan
    # berhasil terkirim), tapi cukup untuk gambaran kasar seberapa aktif bot dipakai.
    db_increment_stat("bot_links_processed")
    if is_spotify(url):
        return process_spotify(chat_id, url, status_message_id)
    elif is_tiktok_photo_url(url):
        return process_tiktok_photo(chat_id, url, status_message_id)
    else:
        return process_and_deliver(chat_id, url, quality, audio_only, status_message_id)


def handle_incoming_message(message: dict):
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()

    # --- ADMIN COMMANDS ---
    if text in ("/menu", "/adminmenu"):
        if OWNER_USER_ID == 0:
            send_bot_message(chat_id, "❌ Membership system belum aktif. Set `OWNER_USER_ID` di Railway Settings dulu.")
            return
        if user_id != OWNER_USER_ID:
            send_bot_message(chat_id, "❌ Hanya pemilik bot yang bisa akses admin menu.")
            return
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "📋 Lihat Members", "callback_data": "admin|members"}],
                [{"text": "➕ Add Member", "callback_data": "admin|addmember"}],
                [{"text": "➖ Remove Member", "callback_data": "admin|removemember"}],
            ]
        }
        send_bot_message(chat_id, "⚙️ *Admin Menu*\n\nPilih aksi yang ingin dilakukan:", reply_markup=keyboard)
        return

    if text.startswith("/addmember "):
        if OWNER_USER_ID == 0:
            send_bot_message(chat_id, "❌ Membership system belum aktif. Set `OWNER_USER_ID` di Railway Settings dulu.")
            return
        if user_id != OWNER_USER_ID:
            send_bot_message(chat_id, "❌ Hanya pemilik bot yang bisa pakai command ini.")
            return
        parts_cmd = text.split()
        try:
            target_uid = int(parts_cmd[1])
        except (IndexError, ValueError):
            send_bot_message(
                chat_id,
                "Format: `/addmember <user_id> [paket]`\n\n"
                "Paket (opsional):\n"
                "• `trial` — 7 hari\n"
                "• `bulan` — 1 bulan\n"
                "• tanpa paket = permanen\n\n"
                "Contoh: `/addmember 123456789 trial`"
            )
            return
        paket = parts_cmd[2].lower() if len(parts_cmd) > 2 else "permanent"
        if paket not in MEMBER_DURATIONS:
            send_bot_message(chat_id, "❌ Paket tidak dikenali. Pakai `trial`, `bulan`, atau kosongkan untuk permanen.")
            return
        duration = MEMBER_DURATIONS[paket]
        expires_at = int(time.time() + duration) if duration else None
        add_member(target_uid, expires_at)
        label = format_expiry(expires_at)
        send_bot_message(chat_id, f"✅ User `{target_uid}` berhasil di-add sebagai member ({label}).")
        return

    if text.startswith("/removemember "):
        if OWNER_USER_ID == 0:
            send_bot_message(chat_id, "❌ Membership system belum aktif. Set `OWNER_USER_ID` di Railway Settings dulu.")
            return
        if user_id != OWNER_USER_ID:
            send_bot_message(chat_id, "❌ Hanya pemilik bot yang bisa pakai command ini.")
            return
        try:
            target_uid = int(text.split()[1])
            if target_uid == OWNER_USER_ID:
                send_bot_message(chat_id, "❌ Nggak bisa remove pemilik bot dari member list.")
                return
            remove_member(target_uid)
            send_bot_message(chat_id, f"✅ User `{target_uid}` berhasil di-remove dari member.")
        except (IndexError, ValueError):
            send_bot_message(chat_id, "Format: `/removemember <user_id>`")
        return

    if text == "/members":
        if OWNER_USER_ID == 0:
            send_bot_message(chat_id, "❌ Membership system belum aktif. Set `OWNER_USER_ID` di Railway Settings dulu.")
            return
        if user_id != OWNER_USER_ID:
            send_bot_message(chat_id, "❌ Hanya pemilik bot yang bisa lihat member list.")
            return
        if not APPROVED_MEMBERS:
            send_bot_message(chat_id, "📋 Member list kosong.")
            return
        members_str = "\n".join(
            f"• `{uid}` — {format_expiry(APPROVED_MEMBERS[uid])}" for uid in sorted(APPROVED_MEMBERS)
        )
        send_bot_message(chat_id, f"📋 *Member List ({len(APPROVED_MEMBERS)}):*\n{members_str}")
        return

    if text == "/stats":
        if OWNER_USER_ID == 0:
            send_bot_message(chat_id, "❌ Membership system belum aktif. Set `OWNER_USER_ID` di Railway Settings dulu.")
            return
        if user_id != OWNER_USER_ID:
            send_bot_message(chat_id, "❌ Hanya pemilik bot yang bisa lihat statistik.")
            return
        links_processed = db_get_stat("bot_links_processed")
        send_bot_message(
            chat_id,
            "📊 *Statistik Bot*\n\n"
            f"👥 Total member: `{len(APPROVED_MEMBERS)}`\n"
            f"🔗 Total link diproses: `{links_processed}`\n"
            f"⏳ Link pending pilih kualitas: `{len(PENDING)}`"
        )
        return

    if text.startswith("/broadcast "):
        if OWNER_USER_ID == 0:
            send_bot_message(chat_id, "❌ Membership system belum aktif. Set `OWNER_USER_ID` di Railway Settings dulu.")
            return
        if user_id != OWNER_USER_ID:
            send_bot_message(chat_id, "❌ Hanya pemilik bot yang bisa broadcast.")
            return
        broadcast_text = text[len("/broadcast "):].strip()
        if not broadcast_text:
            send_bot_message(chat_id, "Format: `/broadcast <pesan>`")
            return
        sent, failed = 0, 0
        for member_id in sorted(APPROVED_MEMBERS):
            if member_id == OWNER_USER_ID:
                continue
            msg_id = send_bot_message(member_id, f"📢 *Pengumuman*\n\n{broadcast_text}")
            if msg_id:
                sent += 1
            else:
                failed += 1
        send_bot_message(chat_id, f"✅ Broadcast terkirim ke `{sent}` member" + (f", gagal ke `{failed}` member." if failed else "."))
        return

    # --- USER COMMANDS (tersedia untuk semua orang, bukan cuma member) ---
    if text == "/myid":
        send_bot_message(chat_id, f"🆔 Telegram ID kamu: `{user_id}`")
        return

    # --- USER COMMANDS ---
    if text in ("/start", "/help"):
        # Kalau OWNER_USER_ID belum di-set, tampilkan warning ke owner
        if OWNER_USER_ID == 0 and user_id:
            send_bot_message(
                chat_id,
                "👋 *Halo tod ! Aku bot downloader arvirmdn.*\n\n"
                "⚠️ *SETUP REQUIRED:*\n"
                f"Your Telegram ID: `{user_id}`\n\n"
                "Set env var di Railway:\n"
                f"`OWNER_USER_ID={user_id}`\n\n"
                "Setelah itu redeploy, baru membership system aktif. "
                "Untuk sekarang, semua orang bisa akses downloader."
            )
        elif user_id and is_member(user_id):
            send_bot_message(
                chat_id,
                "👋 *Halo tod ! Aku bot downloader arvirmdn.*\n\n"
                "Kirim link dari:\n"
                "🎵 TikTok (video & foto/slide)  ▶️ YouTube  📸 Instagram  📘 Facebook  🐦 Twitter\n"
                "🎧 Spotify _(otomatis dicari versi audionya di YouTube, karena Spotify tidak menyediakan unduhan langsung)_\n\n"
                "*Cara pakai:*\n"
                "• Kirim 1 link video → aku tanya dulu mau kualitas / format apa\n"
                "• Kirim 1 link foto/slide TikTok → langsung diproses & dikirim sebagai album foto\n"
                f"• Kirim beberapa link sekaligus (maks {MAX_LINKS_PER_MESSAGE}) → langsung diproses semua (video di 720p, foto TikTok otomatis terdeteksi)\n\n"
                f"⚠️ Video di atas {MAX_DURATION_MINUTES} menit atau {TELEGRAM_MAX_UPLOAD_MB}MB akan dikasih link download "
                "(bukan dikirim langsung), karena itu batas dari Telegram sendiri."
            )
        else:
            send_bot_message(
                chat_id,
                "❌ *lu bukan member tod*, member dulu sana ke admin gantenkk @asololeeeee"
            )
        return

    # Check membership sebelum proses link
    if not user_id or not is_member(user_id):
        send_bot_message(
            chat_id,
            "❌ *lu bukan member tod*, member dulu sana ke admin gantenkk @asololeeeee"
        )
        return

    urls = extract_urls(text)
    if not urls:
        send_bot_message(chat_id, "Kirim link video yang valid ya (harus diawali http/https) 🙏")
        return

    if len(urls) == 1:
        url = urls[0]
        if is_spotify(url):
            process_link(chat_id, url)
            return
        if is_tiktok_photo_url(url):
            process_link(chat_id, url)
            return
        req_id = pending_add(url)
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
        send_bot_message(chat_id, "🎬 Pilih kualitas video atau format audio:", reply_markup=keyboard)
        return

    urls = urls[:MAX_LINKS_PER_MESSAGE]
    total = len(urls)
    summary_msg_id = send_bot_message(
        chat_id, f"📋 Terdeteksi {total} link, aku proses satu-satu ya (video di 720p)...\n\n⏳ Progres: 0/{total}"
    )
    success_count = 0
    for idx, u in enumerate(urls, start=1):
        ok = process_link(chat_id, u, quality=720, audio_only=False)
        if ok:
            success_count += 1
        if summary_msg_id:
            edit_bot_message(
                chat_id, summary_msg_id,
                f"📋 Memproses {total} link (video di 720p)...\n\n"
                f"⏳ Progres: {idx}/{total} link diproses • ✅ {success_count} berhasil"
            )

    if summary_msg_id:
        failed_count = total - success_count
        result_line = f"✅ Selesai: {success_count}/{total} link berhasil diproses"
        if failed_count:
            result_line += f", ❌ {failed_count} gagal (lihat pesan error di atas)."
        else:
            result_line += "."
        edit_bot_message(chat_id, summary_msg_id, result_line)


def handle_callback_query(callback_query: dict):
    callback_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query.get("data", "")
    user_id = callback_query.get("from", {}).get("id")

    parts = data.split("|")
    
    # --- ADMIN CALLBACKS ---
    if len(parts) >= 2 and parts[0] == "admin":
        answer_callback(callback_id)
        
        if user_id != OWNER_USER_ID:
            send_bot_message(chat_id, "❌ Hanya owner yang bisa pakai ini.")
            return
        
        action = parts[1]
        
        if action == "members":
            if not APPROVED_MEMBERS:
                edit_bot_message(chat_id, message_id, "📋 Member list kosong.")
            else:
                members_str = "\n".join(
                    f"• `{uid}` — {format_expiry(APPROVED_MEMBERS[uid])}" for uid in sorted(APPROVED_MEMBERS)
                )
                edit_bot_message(chat_id, message_id, f"📋 *Member List ({len(APPROVED_MEMBERS)}):*\n{members_str}")
            return
        
        elif action == "addmember":
            edit_bot_message(
                chat_id, message_id,
                "📝 *Tambah Member*\n\nReply dengan format:\n`/addmember <user_id> [paket]`\n\n"
                "Paket (opsional): `trial` (7 hari), `bulan` (1 bulan), atau kosongkan untuk permanen.\n\n"
                "Contoh:\n`/addmember 123456789 trial`\n`/addmember 123456789`"
            )
            return
        
        elif action == "removemember":
            if not APPROVED_MEMBERS or len(APPROVED_MEMBERS) == 1:
                edit_bot_message(chat_id, message_id, "❌ Nggak ada member yang bisa di-remove.")
            else:
                members_str = "\n".join(f"• `{uid}`" for uid in sorted(APPROVED_MEMBERS) if uid != OWNER_USER_ID)
                edit_bot_message(chat_id, message_id, f"📝 *Remove Member*\n\n{members_str}\n\nReply dengan format:\n`/removemember <user_id>`")
            return
    
    # --- DOWNLOAD CALLBACKS ---
    if len(parts) != 3 or parts[0] != "dl":
        answer_callback(callback_id)
        return

    _, req_id, choice = parts
    url = pending_pop(req_id)
    answer_callback(callback_id)

    if not url:
        edit_bot_message(chat_id, message_id, "⚠️ Pilihan ini sudah kedaluwarsa, kirim ulang linknya ya.")
        return

    audio_only = choice == "mp3"
    quality = 720 if audio_only else int(choice)
    process_link(chat_id, url, quality=quality, audio_only=audio_only, status_message_id=message_id)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    # Verifikasi request ini beneran dari Telegram (bukan orang lain yang
    # nembak endpoint ini langsung). Telegram selalu menyertakan header ini
    # kalau secret_token di-set waktu setWebhook.
    if TELEGRAM_WEBHOOK_SECRET:
        incoming_secret = request.headers.get("x-telegram-bot-api-secret-token", "")
        if incoming_secret != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret.")

    body = await request.json()

    # Proses beneran (download video/audio pakai yt-dlp+ffmpeg) bisa makan waktu lama.
    # Kalau dijalankan langsung di sini, webhook ini nge-block dan Telegram bisa anggap
    # request "lambat merespons" lalu kirim ulang update-nya (download dobel). Makanya
    # kita balas Telegram DULUAN ({"ok": true}), baru proses beratnya jalan di background.
    if "callback_query" in body:
        background_tasks.add_task(handle_callback_query, body["callback_query"])
        return {"ok": True}

    message = body.get("message") or body.get("channel_post")
    if message:
        background_tasks.add_task(handle_incoming_message, message)

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
    params = {"url": webhook_url}
    if TELEGRAM_WEBHOOK_SECRET:
        params["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
        params=params,
        timeout=10,
    )
    return resp.json()


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        content = {"status": "error", **exc.detail}
    else:
        content = {"status": "error", "message": exc.detail}
    return JSONResponse(status_code=exc.status_code, content=content, headers=getattr(exc, "headers", None) or {})
