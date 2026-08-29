from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import yt_dlp
import os
import re

app = FastAPI()

# Support 1000+ site termasuk TikTok, YouTube, Instagram, Facebook, Twitter, Vimeo, dll
@app.get("/download")
async def download_video(url: str = Query(...)):
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'best[height<=720]',
            'extract_flat': False,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Ambil video URL
            video_url = info.get('url')
            if not video_url:
                # Fallback: cari di formats
                for f in info.get('formats', []):
                    if f.get('url') and f.get('height', 0) <= 720:
                        video_url = f.get('url')
                        break
            
            return {
                "status": "success",
                "title": info.get('title', 'Video'),
                "video_url": video_url,
                "thumbnail": info.get('thumbnail', ''),
                "duration": info.get('duration', 0),
                "platform": info.get('extractor', 'unknown')
            }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"message": "yt-dlp API is running. Support TikTok, YouTube, Instagram, Facebook, Twitter, dan 1000+ platform lainnya."}