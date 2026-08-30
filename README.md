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

## 📋 Daftar Command Bot

### User Commands (Untuk Member)
| Command | Fungsi |
|---------|--------|
| `/start` | Lihat greeting dan bantuan downloader |
| `/help` | Lihat bantuan (sama seperti `/start`) |
| Kirim link video | Downloader akan minta pilih kualitas |

### User Commands (Semua Orang)
| Command | Fungsi |
|---------|--------|
| `/myid` | Lihat Telegram ID sendiri (tidak perlu bot lain) |

### Owner/Admin Commands (Khusus Pemilik Bot)
| Command | Fungsi |
|---------|--------|
| `/menu` | Buka menu admin (lebih mudah) |
| `/adminmenu` | Buka menu admin (alias `/menu`) |
| `/addmember <user_id> [paket]` | Add user sebagai member. Paket: `trial` (7 hari), `bulan` (1 bulan), atau kosongkan untuk permanen |
| `/removemember <user_id>` | Remove user dari member |
| `/members` | Lihat daftar semua members |
| `/stats` | Lihat statistik bot (jumlah member, link diproses) |
| `/broadcast <pesan>` | Kirim pengumuman ke semua member |

---

## 🎯 Cara Pakai sebagai Owner

### Cara 1: Pakai Menu (Recommended)
```
/menu
```
Bot akan show tombol:
- 📋 Lihat Members
- ➕ Add Member  
- ➖ Remove Member

Klik tombol untuk action apa yang mau dilakukan.

### Cara 2: Direct Command
```
/addmember 987654321
/removemember 987654321
/members
```

---

## ✨ Fitur Bot Telegram

### User Features (Member Only)
- Kirim 1 link → tombol pilihan kualitas 360p/720p/1080p atau audio MP3.
- Kirim beberapa link sekaligus (maks 5) → diproses otomatis di 720p, dengan
  **pesan progres batch** yang terus di-update ("Progres: 2/5 • ✅ 2 berhasil")
  sampai semua link selesai, plus ringkasan akhir.
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

### Membership dengan Masa Berlaku (Trial / Bulanan / Permanen)
- `/addmember <user_id> trial` — kasih akses 7 hari, otomatis expired setelahnya.
- `/addmember <user_id> bulan` — kasih akses 1 bulan, otomatis expired setelahnya.
- `/addmember <user_id>` (tanpa paket) — member permanen seperti sebelumnya.
- Expiry dicek otomatis ("lazy check") tiap kali user itu interaksi dengan bot —
  begitu masa aktifnya habis, dia otomatis kehilangan akses tanpa perlu cron job.
- `/members` sekarang menampilkan sisa waktu tiap member (`sisa 5 hari 3 jam`,
  `permanen`, dll).

### Admin Features (Owner Only)
- `/menu` atau `/adminmenu` — buka menu admin dengan inline buttons
- `/addmember <user_id> [paket]` — add user ke approved members list
- `/removemember <user_id>` — remove user dari approved members
- `/members` — lihat daftar semua approved members beserta sisa masa aktifnya

### Monitor Lonjakan Traffic & Error (Notifikasi ke Owner)
- Kalau jumlah request ke endpoint publik (`/download`, `/proxy`, dll) melonjak
  melebihi ambang batas dalam waktu singkat, OWNER_USER_ID otomatis dapat pesan
  peringatan lewat bot.
- Kalau jumlah error dari endpoint tersebut melonjak (banyak link gagal diproses
  beruntun), owner juga dapat notifikasi terpisah.
- Ada cooldown 10 menit antar notifikasi jenis yang sama, biar owner nggak
  kebanjiran pesan kalau kondisinya berlangsung lama.
- Diatur lewat env var (semua opsional): `TRAFFIC_WINDOW_SECONDS` (default 60),
  `TRAFFIC_SPIKE_THRESHOLD` (default 40 request/window), `ERROR_SPIKE_THRESHOLD`
  (default 8 error/window), `ALERT_COOLDOWN_SECONDS` (default 600).

### Non-Member Response
Kalau orang yang belum di-add coba kirim link:
```
❌ lu bukan member tod, member dulu sana ke admin gantenkk @asololeeeee
```

---

## 🌐 Fitur Web (Frontend)

- **Preview sebelum download** — setelah link diproses, tampil kartu pratinjau
  (thumbnail, judul, durasi, perkiraan ukuran file) dulu, baru user menekan
  tombol Download untuk benar-benar mengunduh.
- **Progress bar unduhan real** — saat menekan Download, progress bar terisi
  sesuai persentase byte yang sudah terunduh (kalau server sumbernya mendukung),
  fallback ke mode indeterminate + buka tab baru kalau progress real tidak bisa
  dibaca (mis. dibatasi CORS oleh CDN pihak ketiga).
- **Estimasi ukuran file** — ditampilkan di kartu preview & di tombol Download,
  diambil dari metadata `filesize`/`filesize_approx` yt-dlp (endpoint
  `/download` & `/download-audio` sekarang menyertakan `filesize_bytes` dan
  `filesize_label`).
- **Playlist YouTube** — kalau user tempel link playlist YouTube (mengandung
  `list=`), web akan menampilkan checklist video di dalamnya (maks 25,
  lewat endpoint baru `/playlist-info`) supaya user bisa pilih beberapa video
  sekaligus (maks 5 sesuai batas biasa) alih-alih harus salin link satu-satu.
- **Rate-limit yang lebih ramah** — kalau kena batas request, web menampilkan
  banner dengan hitung mundur detik sampai boleh coba lagi, bukan cuma pesan
  error generik.
- **Badge "Baru!"** di tombol platform — gampang dipindah ke platform lain
  dengan menambahkan atribut `data-badge="new"` + elemen
  `<span class="platform-new-badge">Baru!</span>` di `index.html`.

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
- `APPROVED_MEMBERS` — initial member list (format: `"123456,789012,345678"`) — komma-separated user IDs. Sekarang cuma dipakai untuk **migrasi sekali** ke database; setelahnya member disimpan permanen di DB, bukan lagi bergantung ke env var ini.
  - Note: `OWNER_USER_ID` otomatis di-add, jadi nggak perlu dimasukkan di sini
- `DB_PATH` (default `bot_data.db`) — lokasi file database SQLite untuk member list & statistik.
  - ⚠️ Filesystem Railway itu ephemeral (hilang saat redeploy). Untuk member list yang **benar-benar** persisten lintas redeploy, attach **Railway Volume** dan arahkan `DB_PATH` ke folder di dalam volume tsb (misal `DB_PATH=/data/bot_data.db`).
- `ALLOWED_WEB_ORIGINS` — comma-separated domain web yang boleh akses endpoint publik (`/download`, `/proxy`, dll), contoh: `ALLOWED_WEB_ORIGINS=https://arvirmdn.github.io`. Kalau kosong, pengecekan ini di-skip (semua origin boleh, seperti sebelumnya).

---

## Setup Webhook (Run Once Setelah Deploy)

Setelah first deployment, buka di browser:
```
https://my-app.up.railway.app/set-webhook
```

Kalau berhasil, return: `{"ok": true, "result": true}`

---

## Cara Dapetin User ID Target (untuk /addmember)

**Via Bot:**
1. Add @userinfobot atau @getmyid_bot ke Telegram
2. Forward pesan dari user target ke bot
3. Bot bakal return user ID-nya

**Via Group Chat:**
1. Add target user ke group yang ada bot
2. Kirim `/getid @username`
3. Bot return user ID

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

### /menu hanya show untuk owner?
Ya, itu benar. `/menu` hanya bisa diakses owner. User biasa hanya bisa pakai downloader features.

### Member list hilang saat restart
Sejak update terbaru, member list disimpan di database SQLite (`DB_PATH`), jadi **bertahan** untuk restart proses biasa. Kalau masih hilang juga:
1. Pastikan `DB_PATH` menunjuk ke lokasi yang sama tiap kali deploy (default `bot_data.db` di root project).
2. Kalau kamu redeploy (bukan cuma restart), Railway bisa mengganti filesystem — attach **Railway Volume** dan arahkan `DB_PATH` ke folder di dalamnya supaya benar-benar permanen.
3. Sebagai fallback cepat, set `APPROVED_MEMBERS` env var (comma-separated) — otomatis dimigrasikan ke database saat startup.

---

## Catatan Teknis

- **ffmpeg wajib ada di server** — diatur lewat `nixpacks.toml` dan `railway.toml`
- **Member list disimpan di SQLite** (`DB_PATH`, default `bot_data.db`) — bertahan lintas restart proses biasa. Untuk persisten lintas redeploy, attach Railway Volume (lihat bagian Environment Variables).
- **Endpoint publik (`/download`, `/proxy`, dll) bisa dibatasi ke domain web sendiri** lewat `ALLOWED_WEB_ORIGINS` — kalau tidak diset, tetap terbuka seperti sebelumnya.
- Fitur Spotify tergantung pencarian YouTube — kualitas tergantung ada versi di YouTube atau nggak
- Pilihan kualitas video (`PENDING`) tetap in-memory dan kedaluwarsa otomatis setelah 15 menit tidak dipilih — kirim ulang linknya kalau expired.
- `yt-dlp` tidak dikunci versi, selalu dapat versi terbaru saat redeploy

---

## Quick Setup Checklist

- [ ] Get user ID dari @getmyid_bot
- [ ] Set `OWNER_USER_ID` di Railway Variables
- [ ] Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PUBLIC_DOMAIN`
- [ ] Redeploy
- [ ] Buka `/set-webhook` di browser
- [ ] Test `/start` di bot
- [ ] Test `/menu` untuk admin menu
- [ ] Test `/addmember` untuk add member
- [ ] Test download dengan link YouTube

---

## Testing Flow

### 1. Test as Owner
```
/start       → Lihat greeting
/menu        → Buka admin menu
/members     → Lihat member list (hanya owner)
/addmember <id> → Add member baru
```

### 2. Test as New Member (setelah di-add)
```
/start           → Bisa akses downloader
Send YouTube URL → Bot minta pilih kualitas
```

### 3. Test as Non-Member
```
/start    → Bot kasih pesan "bukan member"
Send link → Bot tolak dengan pesan error
```

---

**Dibuat untuk arvirmdn bot** 🤖 — Membership-only downloader yang rapi!
