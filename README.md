# Social Media Downloader API + Telegram Bot

Backend FastAPI + yt-dlp untuk mengambil link video langsung (TikTok, YouTube,
Instagram, Facebook, Twitter, dll), dipakai oleh web downloader DAN bot Telegram.

## Endpoint

- `GET /` — info instance
- `GET /health` — cek instance hidup
- `GET /download?url=<link>&quality=<144|240|360|480|720|1080|1440|2160>` — dipakai frontend web
- `POST /telegram-webhook` — dipanggil Telegram tiap ada pesan masuk ke bot
- `GET /set-webhook` — dipanggil SEKALI lewat browser setelah deploy untuk mendaftarkan webhook

## Environment Variables (Railway > Settings > Variables)

- `TELEGRAM_BOT_TOKEN` — token bot dari @BotFather (WAJIB untuk fitur bot & notifikasi)
- `TELEGRAM_CHAT_ID` — chat id pribadimu (dipakai khusus notifikasi sukses/gagal dari web,
  BUKAN dipakai bot untuk membalas user lain)
- `PUBLIC_DOMAIN` — domain publik Railway kamu TANPA `https://`, misal
  `web-production-0c5698.up.railway.app` (dipakai `/set-webhook` untuk tahu URL webhook-nya sendiri)

## Cara mengaktifkan fitur bot Telegram

1. Set ketiga env var di atas di Railway, redeploy.
2. Buka `https://<domain-kamu>/set-webhook` sekali lewat browser — kalau responsnya
   `{"ok": true, "result": true, ...}` berarti webhook berhasil terpasang.
3. Buka bot kamu di Telegram, kirim link TikTok/YouTube/dll — bot akan balas dengan
   tombol "⬇️ Download Video".
4. Ketik `/start` di bot untuk lihat pesan bantuan.

Catatan: bot membalas dengan **link download langsung** (bukan mengirim file video),
supaya tidak kena batas ukuran file dari Telegram Bot API dan tidak membebani bandwidth
server kamu.

## Deploy

Bisa dideploy ke Railway, Render, atau Koyeb — semuanya mendeteksi `requirements.txt`
dan `Procfile` secara otomatis.

## Catatan lain

- `yt-dlp` sengaja tidak dikunci ke versi tertentu di `requirements.txt`, supaya
  setiap kali di-build ulang, pip mengambil versi terbaru.
- YouTube kadang menampilkan error "Sign in to confirm you're not a bot" — pembatasan
  dari YouTube, bukan bug di API ini.
- TikTok kadang gagal diproses kalau IP server (Railway/cloud) diblokir TikTok —
  bukan bug di kode, ini pembatasan dari sisi TikTok terhadap IP datacenter.
