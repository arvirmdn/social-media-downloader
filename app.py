from fastapi import FastAPI, Query, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask
from urllib.parse import quote
import yt_dlp
import os
import re
import glob
import shutil
import sqlite3
import tempfile
import time
import uuid
import requests

app = FastAPI(title="Social Media Downloader API")

# Konfigurasi via Environment Variables di Railway (Settings > Variables) —
# JANGAN pernah ditulis langsung di kode ini.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # notifikasi ke akun pribadimu (dari endpoint web)
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or ""

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
        "CREATE TABLE IF NOT EXISTS members (user_id INTEGER PRIMARY KEY)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)"
    )
    return conn


def load_members_from_db() -> set:
    conn = get_db()
    try:
        rows = conn.execute("SELECT user_id FROM members").fetchall()
        return set(r[0] for r in rows)
    finally:
        conn.close()


def db_add_member(user_id: int):
    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO members (user_id) VALUES (?)", (user_id,))
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


# Member list di-load dari DB saat startup, lalu dipegang di memory (set) biar
# pengecekan is_member() tetap cepat — setiap perubahan (add/remove) langsung
# ditulis ke DB juga lewat db_add_member()/db_remove_member().
APPROVED_MEMBERS = load_members_from_db()

# Migrasi dari env var APPROVED_MEMBERS lama (kalau masih dipakai) ke DB,
# supaya member yang sebelumnya cuma "hidup" di env var ikut tersimpan permanen.
if os.getenv("APPROVED_MEMBERS"):
    try:
        legacy_ids = set(int(uid.strip()) for uid in os.getenv("APPROVED_MEMBERS", "").split(",") if uid.strip())
        for uid in legacy_ids:
            if uid not in APPROVED_MEMBERS:
                db_add_member(uid)
        APPROVED_MEMBERS |= legacy_ids
    except ValueError:
        pass

# Add owner ke approved members
if OWNER_USER_ID > 0:
    if OWNER_USER_ID not in APPROVED_MEMBERS:
        db_add_member(OWNER_USER_ID)
    APPROVED_MEMBERS.add(OWNER_USER_ID)  # pemilik otomatis approved
    print(f"✅ Owner (ID: {OWNER_USER_ID}) added to approved members")
else:
    print("⚠️  WARNING: OWNER_USER_ID tidak di-set! Set env var OWNER_USER_ID di Railway Settings > Variables")
    print("   Membership system DISABLED untuk sekarang (semua user bisa akses)")

if APPROVED_MEMBERS:
    print(f"✅ Approved members ({len(APPROVED_MEMBERS)}, dari DB: {DB_PATH}): {sorted(APPROVED_MEMBERS)}")

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

# ---------- Rate limiting sederhana untuk endpoint publik (/download, /proxy) ----------
# Endpoint ini bisa dipanggil siapa saja yang tahu URL-nya (bukan cuma dari web kita),
# jadi dibatasi per-IP biar server nggak dibanjiri/di-abuse orang lain.
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "10"))  # per window
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
_rate_limit_hits: dict[str, list[float]] = {}


def check_rate_limit(request: Request):
    """Raise HTTPException 429 kalau IP ini sudah melebihi batas request dalam window waktu."""
    client_ip = request.headers.get("x-forwarded-for", "")
    client_ip = client_ip.split(",")[0].strip() if client_ip else (request.client.host if request.client else "unknown")

    now = time.time()
    hits = [t for t in _rate_limit_hits.get(client_ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]

    if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail=f"Terlalu banyak request. Maks {RATE_LIMIT_MAX_REQUESTS} request per {RATE_LIMIT_WINDOW_SECONDS} detik, coba lagi sebentar lagi.",
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
    Kalau OWNER_USER_ID = 0, membership system disabled (semua user bisa akses)."""
    if OWNER_USER_ID == 0:  # membership system disabled
        return True
    return user_id in APPROVED_MEMBERS


def add_member(user_id: int):
    """Add user ke approved members list (memory + DB persisten)."""
    APPROVED_MEMBERS.add(user_id)
    db_add_member(user_id)


def remove_member(user_id: int):
    """Remove user dari approved members list (memory + DB persisten)."""
    APPROVED_MEMBERS.discard(user_id)
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

    duration = info.get("duration", 0)
    if duration and duration > MAX_DURATION_MINUTES * 60:
        return None, (
            f"Video ini berdurasi {format_duration(duration)}, melebihi batas "
            f"{MAX_DURATION_MINUTES} menit yang ditetapkan biar server nggak kebebanan."
        )

    title = info.get("title", "Video")
    platform = info.get("extractor", "unknown")
    return {
        "status": "success",
        "title": title,
        "video_url": video_url,
        "download_url": build_download_url(url, title, quality),
        "thumbnail": info.get("thumbnail", ""),
        "duration": duration,
        "platform": platform,
        "quality": quality,
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

    return {
        "status": "success",
        "title": title,
        "audio_url": audio_url,
        "download_url": build_audio_download_url(search_query, title),
        "thumbnail": info.get("thumbnail", ""),
        "duration": duration,
        "platform": platform if not is_spotify(url) else platform,
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
        send_telegram_notification(f"❌ *Gagal Memproses Link!*\n• URL: {url}\n• Error: `{error}`")
        status_code = 404 if "Tidak ada format" in error else 422
        raise HTTPException(status_code=status_code, detail=error)

    send_telegram_notification(
        f"📥 *Unduhan Berhasil!*\n• Judul: {result['title']}\n"
        f"• Platform: {result['platform'].upper()}\n• Kualitas: {quality}p\n• URL: {url}"
    )
    return result


@app.get("/proxy")
async def proxy_download(request: Request, source: str = Query(...), quality: int = Query(720), filename: str = Query("video.mp4")):
    check_web_origin(request)
    check_rate_limit(request)
    try:
        filepath, tmp_dir = download_video_file(source, quality)
    except Exception as e:
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
        raise HTTPException(status_code=502, detail=f"Gagal mengunduh audio: {e}")
    return FileResponse(
        filepath, media_type="audio/mpeg", filename=filename,
        background=BackgroundTask(lambda: shutil.rmtree(tmp_dir, ignore_errors=True)),
    )


# ---------- Bot Telegram ----------

def extract_urls(text: str) -> list[str]:
    return URL_REGEX.findall(text or "")


def is_spotify(url: str) -> bool:
    return "spotify.com" in url.lower()


def process_spotify(chat_id, spotify_url: str, status_message_id: int | None):
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
        return

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
            return

        edit_bot_message(chat_id, status_message_id, f"📤 Mengirim *{esc_title}*...")
        ok = send_bot_audio_file(chat_id, filepath, track_title, "Spotify (dicari via YouTube)")
        delete_bot_message(chat_id, status_message_id)
        if not ok:
            send_bot_message(chat_id, f"⚠️ Gagal mengirim audio untuk *{esc_title}*, coba lagi beberapa saat lagi.")
    except Exception as e:
        edit_bot_message(chat_id, status_message_id, f"❌ Gagal mengunduh audio untuk *{esc_title}*:\n{e}")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def process_and_deliver(chat_id, url: str, quality: int, audio_only: bool, status_message_id: int | None = None):
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
        return

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
        return

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
            return

        edit_bot_message(chat_id, status_message_id, f"📤 Mengirim *{esc_title}*...")

        if audio_only:
            ok = send_bot_audio_file(chat_id, filepath, result["title"], result["platform"])
        else:
            ok = send_bot_video_file(chat_id, filepath, result["title"], result["platform"], quality)

        delete_bot_message(chat_id, status_message_id)

        if not ok:
            send_bot_message(chat_id, f"⚠️ Gagal kirim *{esc_title}* langsung, ini link download-nya sebagai gantinya:")
            send_bot_download_link(chat_id, result["title"], result["platform"], quality, result["download_url"], result["video_url"])
    except Exception as e:
        edit_bot_message(chat_id, status_message_id, f"❌ Gagal mengunduh *{esc_title}*:\n{e}")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def process_link(chat_id, url: str, quality: int = 720, audio_only: bool = False, status_message_id: int | None = None):
    # Dihitung untuk /stats. Ini menghitung "link yang diproses" (bukan jaminan
    # berhasil terkirim), tapi cukup untuk gambaran kasar seberapa aktif bot dipakai.
    db_increment_stat("bot_links_processed")
    if is_spotify(url):
        process_spotify(chat_id, url, status_message_id)
    else:
        process_and_deliver(chat_id, url, quality, audio_only, status_message_id)


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
        try:
            target_uid = int(text.split()[1])
            add_member(target_uid)
            send_bot_message(chat_id, f"✅ User `{target_uid}` berhasil di-add sebagai member.")
        except (IndexError, ValueError):
            send_bot_message(chat_id, "Format: `/addmember <user_id>`")
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
        members_str = "\n".join(f"• `{uid}`" for uid in sorted(APPROVED_MEMBERS))
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
                "🎵 TikTok  ▶️ YouTube  📸 Instagram  📘 Facebook  🐦 Twitter\n"
                "🎧 Spotify _(otomatis dicari versi audionya di YouTube, karena Spotify tidak menyediakan unduhan langsung)_\n\n"
                "*Cara pakai:*\n"
                "• Kirim 1 link → aku tanya dulu mau kualitas / format apa\n"
                f"• Kirim beberapa link sekaligus (maks {MAX_LINKS_PER_MESSAGE}) → langsung diproses semua di 720p\n\n"
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
    send_bot_message(chat_id, f"📋 Terdeteksi {len(urls)} link, aku proses semua satu-satu ya (video di 720p)...")
    for u in urls:
        process_link(chat_id, u, quality=720, audio_only=False)


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
                members_str = "\n".join(f"• `{uid}`" for uid in sorted(APPROVED_MEMBERS))
                edit_bot_message(chat_id, message_id, f"📋 *Member List ({len(APPROVED_MEMBERS)}):*\n{members_str}")
            return
        
        elif action == "addmember":
            edit_bot_message(chat_id, message_id, "📝 *Tambah Member*\n\nReply dengan format:\n`/addmember <user_id>`\n\nContoh: `/addmember 123456789`")
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
    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
        params={"url": webhook_url},
        timeout=10,
    )
    return resp.json()


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "message": exc.detail})
