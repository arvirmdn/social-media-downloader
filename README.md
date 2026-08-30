# Social Media Downloader API + Telegram Bot (with Membership System)

Backend FastAPI + yt-dlp untuk web downloader dan bot Telegram dengan **membership-only access**.

## ✨ Fitur Bot Telegram

### User Features (Member Only)
- Kirim 1 link → tombol pilihan kualitas 360p/720p/1080p atau audio MP3.
- Kirim beberapa link sekaligus (maks 5) → diproses otomatis di 720p.
- Video/audio dikirim LANGSUNG ke chat kalau ≤ 50MB (batas Telegram Bot API),
  otomatis fallback ke link download kalau lebih besar.
- Pesan status ("Mengambil info...", "Mengunduh...") jadi satu pesan yang terus
  di-update (dengan progress % / kecepatan / ETA), lalu OTOMATIS TERHAPUS begitu hasil
  akhir terkirim — chat jadi bersih, cuma nyisa hasil akhirnya saja.
- Video di atas `MAX_DURATION_MINUTES` (default 15 menit) ditolak otomatis sebelum
  diunduh, biar server nggak kebebanan.
- Link Spotify otomatis dicari versi audionya di YouTube (judul diambil dari
  oEmbed Spotify), karena Spotify sendiri memproteksi audionya (DRM) dan tidak
  bisa diunduh langsung dari sana.
- `/start` atau `/help` → pesan bantuan (member only).

### Admin Features (Owner Only)
- `/addmember <user_id>` — add user ke approved members list
- `/removemember <user_id>` — remove user dari approved members
- `/members` — lihat daftar semua approved members

### Non-Member Response
Kalau orang yang belum di-add coba kirim link:
```
❌ lu bukan member tod, member dulu sana ke admin gantenkk @asololeeeee
```

## Environment Variables (Railway > Settings > Variables)

**Required:**
- `TELEGRAM_BOT_TOKEN` — token dari @BotFather
- `TELEGRAM_CHAT_ID` — chat id pribadimu (notifikasi dari endpoint web)
- `PUBLIC_DOMAIN` — domain publik Railway TANPA `https://`
- `OWNER_USER_ID` — user_id Telegram kamu (pemilik bot, untuk admin commands)

**Optional:**
- `MAX_DURATION_MINUTES` (default `15`) — batas durasi video yang boleh diproses
- `APPROVED_MEMBERS` — initial member list (format: `"123456,789012,345678"`) — komma-separated user IDs
  - Note: `OWNER_USER_ID` otomatis di-add, jadi nggak perlu dimasukkan di sini

## Cara Setup

### 1. Deploy di Railway
Pastikan struktur repo ini persis di root:
```
.
├── app.py
├── Procfile
├── nixpacks.toml
├── railway.toml
├── requirements.txt
└── README.md
```

### 2. Set Environment Variables (Railway Console)
Go to **Settings > Variables** dan isi:
```
TELEGRAM_BOT_TOKEN=123456:ABCDEFGHIJK...
TELEGRAM_CHAT_ID=987654321
PUBLIC_DOMAIN=my-app.up.railway.app
OWNER_USER_ID=123456789
MAX_DURATION_MINUTES=15
APPROVED_MEMBERS=111111,222222,333333
```

### 3. Setup Webhook (Run Once)
Setelah first deployment, buka di browser (replace dengan domain kamu):
```
https://my-app.up.railway.app/set-webhook
```

Kalau berhasil, return: `{"ok": true, "result": true, "description": "Webhook was set"}`

### 4. Test Bot
1. Add bot di Telegram: `/start`
2. Bot bakal reply dengan error membership (karena belum di-add)
3. Pakai `/addmember` dari akun pemilik bot untuk add diri sendiri:
   ```
   /addmember 123456789
   ```
4. Coba `/start` lagi — sekarang harus bisa akses fitur downloader

## Cara Pakai sebagai Owner

### Lihat Member List
```
/members
```

### Add Member
```
/addmember 987654321
```
(ganti dengan user_id target)

### Remove Member
```
/removemember 987654321
```

## Cara Dapetin User ID

**Via Bot:**
1. Add @userinfobot atau @getmyid_bot ke Telegram
2. Forward pesan dari user target ke bot
3. Bot bakal return user ID-nya

**Via Bot Telegram Kamu:**
Edit `app.py`, tambah ini di `handle_incoming_message()`:
```python
user_id = message.get("from", {}).get("id")
send_bot_message(chat_id, f"Your user ID: `{user_id}`")
```
Kemudian user bisa ketik `/myid` untuk lihat ID mereka.

## Catatan Teknis

- **ffmpeg wajib ada di server** (untuk MP3 & audio dari Spotify) — diatur lewat
  `nixpacks.toml` (`nixPkgs = ["...", "ffmpeg"]`) dan `railway.toml`
  (`builder = "NIXPACKS"`). Pastikan KEDUA file ini ada persis di ROOT repo
  (sejajar dengan `app.py`), bukan di dalam subfolder.
- **Member list in-memory** — hilang kalau server restart. Kalau mau persistent,
  perlu database (Redis, PostgreSQL, dll). Buat now, `APPROVED_MEMBERS` env var
  bisa load initial list saat startup.
- Fitur Spotify bergantung pada pencarian judul di YouTube — kualitas hasilnya
  tergantung ada/tidaknya versi resmi/cocok di YouTube, bukan audio asli dari
  Spotify (karena itu memang tidak mungkin diambil langsung).
- Pilihan kualitas per-link & progress hook disimpan sementara di memori server
  (bukan database) — kalau server restart pas user lagi mikir, dia perlu kirim
  ulang linknya.
- `yt-dlp` sengaja tidak dikunci versi di `requirements.txt`, biar selalu dapat
  versi terbaru tiap redeploy.

## Greeting Message

Default greeting (untuk member):
```
👋 Halo tod ! Aku bot downloader arvirmdn.

Kirim link dari:
🎵 TikTok  ▶️ YouTube  📸 Instagram  📘 Facebook  🐦 Twitter
🎧 Spotify ...
```

Bisa diedit langsung di `app.py` di function `handle_incoming_message()`.

## Troubleshooting

### Bot nggak reply
1. Cek `TELEGRAM_BOT_TOKEN` valid
2. Buka https://my-domain.up.railway.app/set-webhook lagi
3. Check Railway logs untuk error

### Orang add tapi tetep "bukan member"
1. Pastikan `OWNER_USER_ID` di Railway settings udah set dengan user_id pemilik bot
2. Cek di `/members` command apakah user ID-nya udah muncul

### Member list hilang saat restart
1. Set `APPROVED_MEMBERS` env var dengan initial list (comma-separated)
2. Atau wait for database integration di next iteration

---

**Dibuat untuk arvirmdn bot** 🤖 — Membership-only downloader yang rapi!
