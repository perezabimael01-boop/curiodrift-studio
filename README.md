# Curio Drift: free daily facts videos

Every day this makes 10 fact ideas, then turns the first few into finished vertical videos
(voice-over, stock footage, on-screen text) plus a caption and hashtags. You post them yourself.
Everything it uses has a free tier or is free software.

| Step | Tool | Cost |
|---|---|---|
| Ideas and scripts | Google Gemini API | free tier |
| Voice | edge-tts | free, no key |
| Footage | Pexels or Pixabay (optional) | free key; without one, animated colour backgrounds |
| Video | FFmpeg + Pillow | free, runs on your computer |

## Set up (about 10 minutes)

1. **Install Python 3.10+ and FFmpeg.**
   Windows: `winget install Python.Python.3.12` and `winget install Gyan.FFmpeg`. Mac: `brew install python ffmpeg`.
   Reopen your terminal afterwards.
2. **Get your free Gemini key:** https://aistudio.google.com/apikey
   Optional, for real stock footage: a free Pixabay key at https://pixabay.com/api/docs/ (Pexels has paused new keys).
   Without it, videos use animated colour backgrounds and still work.
3. **In this folder:**
   ```
   pip install -r requirements.txt
   copy .env.example .env      (Mac/Linux: cp .env.example .env)
   ```
   Open `.env` and paste the key(s).
4. **Test the video engine (no keys needed):** `python generate.py --selftest`
   It should print `OK` and create a short colour-bar video.
5. **Make today's videos:** `python generate.py`
   Use `python generate.py --videos 1` for a quick first try, or `--ideas-only` for just the ideas.

Results land in `output/<date>/`:
```
ideas.md                  all 10 ideas
01-some-title/video.mp4   post this
01-some-title/caption.txt paste this as the caption (hashtags included)
01-some-title/review.txt  facts to double-check before posting
```

## Post by hand
Upload `video.mp4` from the Instagram app (send it to your phone via Drive, AirDrop or USB) or from
instagram.com on your laptop. Paste in `caption.txt`. Read `review.txt` first: AI can get facts wrong.

## Run it automatically every day
- **Windows:** Task Scheduler, create a daily task that runs `python generate.py` in this folder.
- **Mac/Linux:** `crontab -e` and add `0 7 * * * cd /path/to/curio-facts && python3 generate.py`.
- **Cloud, laptop off:** put this folder in a private GitHub repo, add `GEMINI_API_KEY` (and `PIXABAY_API_KEY` if you use it) under Settings > Secrets and variables > Actions, and the included
  `.github/workflows/daily.yml` runs daily. Download the videos from the run's "videos" artifact.

## Settings (in `.env`)
`VIDEOS_PER_DAY` (default 3), `NICHE`, `VOICE` (list voices with `edge-tts --list-voices`),
`HANDLE` (shown small at the bottom), `GEMINI_MODEL`, `FONT_FILE`.

## Good to know
- Free tiers have limits and can change. If a model is retired, the script looks up a current one automatically; you can also set `GEMINI_MODEL`.
- edge-tts uses Microsoft's online voice service without an official free plan, so it could change or stop.
- Stock footage (Pexels/Pixabay) is free to use; check the license if you plan to sell the videos.
- Posting 10 near-identical Reels a day can hurt reach. Start with 1-3 and look at what performs.
- Scripts are checked against a "verifiable facts only" instruction, not against sources. Always review.

## Trending ideas and Google overload

- Each day the ideas start from what is trending on the web (most-read Wikipedia pages, Google Trends, Reddit Today-I-Learned), turned into timeless, verifiable facts. No key needed. If those sites are unreachable it falls back to evergreen ideas.
- If Google says "503 high demand", the script switches to other Gemini models automatically and waits between rounds. It only gives up after about 6 rounds.
- Running again the same day reuses today's ideas and skips videos already made. Use `python generate.py --fresh` for a new set of ideas.

- Ready-made ideas and scripts are in content.json, so the first run does not wait on Gemini. Use `--gemini` for new AI-written ones.

## Website
`docs/index.html` is the dashboard (GitHub Pages, source: main branch, /docs folder). Videos are made by the "Make videos" workflow and published into docs/videos. Add GEMINI_API_KEY and PIXABAY_API_KEY under Settings > Secrets and variables > Actions. Never commit a .env file.
