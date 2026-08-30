# Social Media Downloader API + Telegram Bot

Backend FastAPI + yt-dlp untuk web downloader dan bot Telegram.

## Fitur Bot Telegram

- Kirim 1 link → tombol pilihan kualitas 360p/720p/1080p atau audio MP3.
- Kirim beberapa link sekaligus (maks 5) → diproses otomatis di 720p.
- Video/audio dikirim LANGSUNG ke chat kalau ≤ 50MB (batas Telegram Bot API),
  otomatis fallback ke link download kalau lebih besar.
- Pesan status ("Mengambil info...", "Mengunduh...") jadi satu pesan yang terus
  di-update (dengan progress % / kecepatan / ETA), lalu OTOMATIS TERHAPUS begitu
  hasil akhir terkirim — chat jadi bersih, cuma nyisa hasil akhirnya saja.
- Video di atas `MAX_DURATION_MINUTES` (default 15 menit) ditolak otomatis sebelum
  diunduh, biar server nggak kebebanan.
- Link Spotify otomatis dicari versi audionya di YouTube (judul diambil dari
  oEmbed Spotify), karena Spotify sendiri memproteksi audionya (DRM) dan tidak
  bisa diunduh langsung dari sana.
- `/start` atau `/help` → pesan bantuan.

## Environment Variables (Railway > Settings > Variables)

- `TELEGRAM_BOT_TOKEN` — token dari @BotFather
- `TELEGRAM_CHAT_ID` — chat id pribadimu (notifikasi dari endpoint web)
- `PUBLIC_DOMAIN` — domain publik Railway TANPA `https://`
- `MAX_DURATION_MINUTES` (opsional, default `15`) — batas durasi video yang boleh diproses

## Setelah deploy

1. Pastikan `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PUBLIC_DOMAIN` terisi.
2. Buka `https://<domain-kamu>/set-webhook` sekali lewat browser.
3. Tes: `/start`, kirim link video, kirim link Spotify, kirim beberapa link sekaligus.

## Catatan

- **ffmpeg wajib ada di server** (untuk MP3 & audio dari Spotify) — diatur lewat
  `nixpacks.toml` (`nixPkgs = ["...", "ffmpeg"]`) dan `railway.toml`
  (`builder = "NIXPACKS"`). Pastikan KEDUA file ini ada persis di ROOT repo
  (sejajar dengan `app.py`), bukan di dalam subfolder.
- Fitur Spotify bergantung pada pencarian judul di YouTube — kualitas hasilnya
  tergantung ada/tidaknya versi resmi/cocok di YouTube, bukan audio asli dari
  Spotify (karena itu memang tidak mungkin diambil langsung).
- Pilihan kualitas per-link & progress hook disimpan sementara di memori server
  (bukan database) — kalau server restart pas user lagi mikir, dia perlu kirim
  ulang linknya.
- Rate limit per-user belum ada — pertimbangkan menambahkannya kalau bot mulai
  dipakai banyak orang dan kredit Railway cepat habis.
- `yt-dlp` sengaja tidak dikunci versi di `requirements.txt`, biar selalu dapat
  versi terbaru tiap redeploy.
