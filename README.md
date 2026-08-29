# Social Media Downloader API

Backend sederhana berbasis FastAPI + yt-dlp untuk mengambil link video langsung
(TikTok, YouTube, Instagram, Facebook, Twitter, dan platform lain yang didukung yt-dlp).

## Endpoint

- `GET /` — info instance
- `GET /health` — cek instance hidup
- `GET /download?url=<link>&quality=<144|240|360|480|720|1080|1440|2160>` — ambil link video langsung

Contoh respons sukses:
```json
{
  "status": "success",
  "title": "Judul video",
  "video_url": "https://...",
  "thumbnail": "https://...",
  "duration": 30,
  "platform": "tiktok",
  "quality": 720
}
```

Contoh respons gagal:
```json
{ "status": "error", "message": "Gagal memproses link: ..." }
```

## Deploy

Bisa dideploy ke Railway, Render, atau Koyeb — semuanya mendeteksi `requirements.txt`
dan `Procfile` secara otomatis. Tidak butuh database atau storage.

**Wajib**: setelah deploy, generate/catat domain publiknya, lalu isi domain itu
di `YTDLP_API_URL` pada `script.js` frontend.

## Catatan

- `yt-dlp` sengaja tidak dikunci ke versi tertentu di `requirements.txt`, supaya
  setiap kali di-build ulang, pip mengambil versi terbaru — ini penting karena
  yt-dlp perlu update rutin mengikuti perubahan di TikTok/YouTube/dll. Kalau
  situs berubah dan API mulai gagal total, coba redeploy dulu (biar dapat
  yt-dlp versi terbaru) sebelum curiga ada bug di kode ini.
- YouTube kadang menampilkan error "Sign in to confirm you're not a bot" —
  ini pembatasan dari YouTube, bukan bug di API ini. Kode sudah mencoba
  workaround (`player_client: android`) yang membantu di sebagian kasus,
  tapi tidak 100% terjamin.
- Instagram: video publik biasanya bisa, tapi story/reel privat butuh login
  dan tidak didukung di sini.
