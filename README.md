# Social Media Downloader API + Telegram Bot

Backend FastAPI + yt-dlp untuk web downloader dan bot Telegram (kirim video/audio langsung ke chat).

## Fitur Bot Telegram

- Kirim 1 link → bot tanya dulu: kualitas 360p/720p/1080p atau audio MP3 (tombol inline).
- Kirim beberapa link sekaligus (maks 5) dalam satu pesan → semua diproses otomatis di 720p.
- Video/audio dikirim LANGSUNG ke chat (bukan link) kalau ukurannya ≤ 50MB (batas Telegram Bot API).
  Kalau lebih besar, otomatis dikasih link download sebagai gantinya.
- `/start` atau `/help` → pesan bantuan.

## Environment Variables (Railway > Settings > Variables)

- `TELEGRAM_BOT_TOKEN` — token dari @BotFather
- `TELEGRAM_CHAT_ID` — chat id pribadimu (notifikasi dari endpoint web)
- `PUBLIC_DOMAIN` — domain publik Railway TANPA `https://`

## Setelah deploy

1. Pastikan ketiga env var di atas terisi.
2. Buka `https://<domain-kamu>/set-webhook` sekali lewat browser.
3. Tes di Telegram: `/start`, lalu kirim link video.

## Catatan penting

- **ffmpeg wajib ada di server** untuk fitur audio MP3 — sudah diatur otomatis lewat
  `nixpacks.toml` (`aptPkgs = ["ffmpeg"]`). Kalau fitur MP3 gagal terus dengan error
  yang menyebut ffmpeg, cek log build Railway apakah ffmpeg ke-install.
- Pilihan kualitas per-link disimpan sementara di memori server (bukan database).
  Kalau server restart/redeploy PAS user lagi mikir mau pilih kualitas apa, pilihannya
  hilang dan dia harus kirim ulang linknya — ini trade-off yang disengaja biar
  arsitekturnya tetap simpel (nggak perlu database tambahan).
- Rate limit belum ada di versi ini — kalau nanti bot mulai dipakai banyak orang
  dan kredit Railway cepat habis, bisa ditambahkan pembatasan per-user.
- `yt-dlp` sengaja tidak dikunci versi di `requirements.txt`, biar selalu dapat versi
  terbaru tiap redeploy.
