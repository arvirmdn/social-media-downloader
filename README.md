# Social Media Downloader API + Telegram Bot (with Membership System)

Backend FastAPI + yt-dlp untuk web downloader dan bot Telegram dengan **membership-only access**.

## ⚙️ SETUP YANG PENTING

### Step 0: Dapatkan User ID Telegram Kamu
1. Buka @getmyid_bot atau @userinfobot di Telegram
2. Kirim `/start`
3. Bot akan kasih: `Your user ID is: 123456789`
4. **Copy angka itu** — ini `OWNER_USER_ID` kamu

### Step 1: Set Environment Variable di Railway
1. Go to **Railway Console > Project > Settings > Variables**
2. Tambahkan (minimal):
   ```
   TELEGRAM_BOT_TOKEN=123456:ABCDEFGHIJK...
   TELEGRAM_CHAT_ID=987654321
   PUBLIC_DOMAIN=my-app.up.railway.app
   OWNER_USER_ID=YOUR_USER_ID_DARI_STEP_0
   ```

3. **PENTING:** Kalau kamu set `OWNER_USER_ID`, redeploy bot!

### Step 2: Test Bot
1. Buka bot Telegram kamu
2. Kirim `/start`
3. Kalau berhasil: bot show greeting "Halo tod ! Aku bot downloader arvirmdn."

**Kalau masih error:**
- Cek Railway logs: `OWNER_USER_ID` sudah di-set?
- Cek user ID-mu sudah bener?
- Coba `/addmember YOUR_USER_ID` sebagai owner

---

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

---

## Environment Variables (Railway > Settings > Variables)

**REQUIRED (Membership System):**
- `OWNER_USER_ID` — user_id Telegram kamu (pemilik bot, untuk admin commands)
  - **WAJIB diset biar membership system berfungsi!**
  - Bisa dapat dari @getmyid_bot

**REQUIRED (Basic Config):**
- `TELEGRAM_BOT_TOKEN` — token dari @BotFather
- `TELEGRAM_CHAT_ID` — chat id pribadimu (notifikasi dari endpoint web)
- `PUBLIC_DOMAIN` — domain publik Railway TANPA `https://` (contoh: `my-app.up.railway.app`)

**OPTIONAL:**
- `MAX_DURATION_MINUTES` (default `15`) — batas durasi video yang boleh diproses
- `APPROVED_MEMBERS` — initial member list (format: `"123456,789012,345678"`) — komma-separated user IDs
  - Note: `OWNER_USER_ID` otomatis di-add, jadi nggak perlu dimasukkan di sini

---

## Cara Pakai sebagai Owner

### Setup Webhook (Run Once Setelah Deploy)
Setelah first deployment, buka di browser:
```
https://my-app.up.railway.app/set-webhook
```

Kalau berhasil, return: `{"ok": true, "result": true}`

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

### Test Sendiri
1. Kirim `/start` sebagai member
2. Kirim link (contoh: YouTube URL)
3. Pilih kualitas
4. Bot proses & kirim video

---

## Troubleshooting

### "lu bukan member tod" padahal aku owner?
**PENYEBAB:** `OWNER_USER_ID` belum di-set atau salah

**SOLUSI:**
1. Cek user ID kamu pakai @getmyid_bot (harus angka)
2. Set di Railway: `OWNER_USER_ID=123456789` (ganti dengan ID kamu)
3. **REDEPLOY** bot
4. Kirim `/start` lagi

### Bot nggak reply sama sekali
1. Cek `TELEGRAM_BOT_TOKEN` valid di BotFather
2. Buka https://my-domain.up.railway.app/set-webhook
3. Check Railway Logs untuk error

### Orang add tapi tetep "bukan member"
1. Pastikan user ID target bener (pakai @getmyid_bot)
2. Kirim `/members` untuk verifikasi user ID-nya muncul
3. Kalau nggak muncul, coba `/addmember` lagi

### Member list hilang saat restart
1. Set `APPROVED_MEMBERS` env var dengan initial list (comma-separated)
   Contoh: `APPROVED_MEMBERS=111111,222222,333333`
2. Atau: add members lagi via `/addmember` command

---

## Catatan Teknis

- **ffmpeg wajib ada di server** — diatur lewat `nixpacks.toml` dan `railway.toml`
- **Member list in-memory** — kalau mau persistent across restart, perlu database (Redis/PostgreSQL)
- Fitur Spotify tergantung pencarian YouTube — kualitas tergantung ada versi di YouTube atau nggak
- Pilihan kualitas hilang kalau server restart (need database)
- `yt-dlp` tidak dikunci versi, selalu dapat versi terbaru saat redeploy

---

## Quick Setup Checklist

- [ ] Get user ID dari @getmyid_bot
- [ ] Set `OWNER_USER_ID` di Railway Variables
- [ ] Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PUBLIC_DOMAIN`
- [ ] Redeploy
- [ ] Buka `/set-webhook` di browser
- [ ] Test `/start` di bot
- [ ] Test download dengan link YouTube
- [ ] Add member lain dengan `/addmember <id>`

---

**Dibuat untuk arvirmdn bot** 🤖 — Membership-only downloader yang rapi!
