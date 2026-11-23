#!/usr/bin/env python3
"""Curio Drift: free daily educational-facts video generator.

Pipeline (all free):
  1. Ideas   : trending topics from the web (Wikipedia, Google Trends, Reddit TIL) + Gemini -> 10 fact ideas
  2. Script  : Google Gemini API              -> scenes, caption, hashtags, fact-check list
  3. Voice   : edge-tts (free, no key)        -> one mp3 per scene (exact timing)
  4. Visuals : Pexels or Pixabay (free key, optional) -> stock clips; otherwise FFmpeg animated backgrounds
  5. Video   : FFmpeg + Pillow (local)        -> 1080x1920 mp4 with on-screen text
Output goes to output/<date>/<NN-title>/ (video.mp4, caption.txt, review.txt).
You post the videos yourself.
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
HISTORY_FILE = ROOT / "history.json"
CACHE_DIR = ROOT / ".cache"

W, H, FPS = 1080, 1920, 30


# --------------------------------------------------------------------------- config
def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
NICHE = os.environ.get("NICHE", "surprising, verifiable educational facts (science, history, space, human body, nature)")
VOICE = os.environ.get("VOICE", "en-US-AndrewNeural")
HANDLE = os.environ.get("HANDLE", "@curiodrift.facts")
VIDEOS_PER_DAY = int(os.environ.get("VIDEOS_PER_DAY", "3"))


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- http
def http_json(url, payload=None, headers=None, retries=6):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=150) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = min(60, 5 * 2 ** attempt)
                log(f"    (Google busy, HTTP {e.code}; retrying in {wait}s)")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {body}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:  # includes read timeouts
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP timeout/network error from {url.split('?')[0]}: {e}")


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "curio-facts/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


# --------------------------------------------------------------------------- trending
UA = {"User-Agent": "curio-facts/1.0 (educational video generator; contact: local user)"}
SKIP_PREFIXES = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "Help:", "File:", "Category:",
                 "Template:", "Talk:", "User:", "-")


def _get(url, as_json=True):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if as_json else raw


def trending_wikipedia(limit=40):
    """Most-read English Wikipedia pages (what the world is curious about), free, no key."""
    from datetime import timedelta
    for back in (1, 2, 3):
        d = date.today() - timedelta(days=back)
        try:
            data = _get(f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/"
                        f"{d.year}/{d.month:02d}/{d.day:02d}")
            arts = data["items"][0]["articles"]
            names = [urllib.parse.unquote(a["article"]).replace("_", " ") for a in arts
                     if not a["article"].startswith(SKIP_PREFIXES)]
            return names[:limit]
        except Exception:
            continue
    return []


def trending_google(limit=20):
    """Google Trends daily searches (public RSS feed)."""
    import xml.etree.ElementTree as ET
    for url in ("https://trends.google.com/trending/rss?geo=US",
                "https://trends.google.com/trends/trendingsearches/daily/rss?geo=US"):
        try:
            root = ET.fromstring(_get(url, as_json=False))
            titles = [t.text.strip() for t in root.iter("title") if t.text and t.text.strip()]
            titles = [t for t in titles if "trends" not in t.lower()][:limit]
            if titles:
                return titles
        except Exception:
            continue
    return []


def trending_reddit(limit=15):
    """Top 'Today I Learned' posts of the day: facts people currently find fascinating."""
    try:
        data = _get(f"https://www.reddit.com/r/todayilearned/top.json?t=day&limit={limit}")
        return [re.sub(r"^TIL( that)?,?\s*", "", c["data"]["title"], flags=re.I)[:140]
                for c in data["data"]["children"]]
    except Exception:
        return []


def get_trending():
    out = {}
    for name, fn in (("Most-read Wikipedia pages", trending_wikipedia),
                     ("Google Trends searches", trending_google),
                     ("Top Today-I-Learned posts", trending_reddit)):
        items = fn()
        if items:
            out[name] = items
            log(f"    trending: {len(items)} items from {name}")
    if not out:
        log("    (could not reach any trending source; using evergreen ideas)")
    return out


# --------------------------------------------------------------------------- gemini
def extract_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        raise ValueError("No JSON in model reply: " + text[:200])
    start = min(starts)
    end = max(text.rfind("]"), text.rfind("}"))
    return json.loads(text[start:end + 1])


def available_models():
    """Text models this key can use, newest first; full 'flash' models before 'lite' ones."""
    res = http_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                    headers={"x-goog-api-key": GEMINI_KEY})
    full, lite = [], []
    for m in res.get("models", []):
        n = m.get("name", "").replace("models/", "")
        if "flash" in n and "generateContent" in m.get("supportedGenerationMethods", []) \
                and not any(x in n for x in ("image", "tts", "live", "audio", "robotics", "embedding")):
            (lite if "lite" in n else full).append(n)
    return sorted(full, reverse=True) + sorted(lite, reverse=True)


_MODEL_CACHE = []


def gemini(prompt):
    """Call Gemini. When Google is busy (503/429) or a model is retired (404) rotate through every
    available model, cool down, and go round again, so one overloaded model never stops the run."""
    global GEMINI_MODEL
    if not GEMINI_KEY:
        raise SystemExit("Missing GEMINI_API_KEY (see README).")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
    }
    order, last = [GEMINI_MODEL], None
    for rnd in range(6):
        for model in order:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                res = http_json(url, body, headers={"x-goog-api-key": GEMINI_KEY}, retries=1)
            except RuntimeError as e:
                last = e
                msg = str(e)
                if any(c in msg for c in ("HTTP 400", "HTTP 401", "HTTP 403")):
                    raise  # bad key or bad request: retrying will not help
                log(f"    ({model}: {msg.split(':')[0]})")
                continue
            try:
                text = res["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                last = RuntimeError("Unexpected Gemini reply: " + json.dumps(res)[:300])
                continue
            if model != GEMINI_MODEL:
                log(f"    (now using {model})")
                GEMINI_MODEL = model
            time.sleep(4)  # stay under free-tier rate limits
            return extract_json(text)
        if not _MODEL_CACHE:
            try:
                _MODEL_CACHE.extend(available_models())
            except RuntimeError:
                pass
            order = [GEMINI_MODEL] + [m for m in _MODEL_CACHE if m != GEMINI_MODEL]
            if len(order) > 1:
                continue  # try the other models right away, no waiting
        wait = 15 * (rnd + 1)
        log(f"    (all models busy, waiting {wait}s then trying again, round {rnd + 2}/6)")
        time.sleep(wait)
    raise last or RuntimeError("Gemini unavailable")


def make_ideas(used, trending=None):
    if trending:
        block = "\n".join(f"- {k}: " + "; ".join(v) for k, v in trending.items())
        trend_rules = f"""
TRENDING RIGHT NOW (from the web today):
{block}

Use the trends as inspiration: at least 6 of the 10 ideas should connect something trending to a
timeless, verifiable educational fact (for example a trending astronaut or planet -> a space fact,
a trending animal or place -> a nature fact). Teach the underlying science or history; do NOT make
claims about recent news events or about living people. The other ideas can be evergreen."""
    else:
        trend_rules = ""
    prompt = f"""You are the idea agent for a faceless Instagram Reels channel about: {NICHE}.

Come up with 10 NEW video ideas for today. Rules:
- Each idea must be a well-established, verifiable fact. No rumors, no disputed claims, no medical advice.
- Strong curiosity hook, understandable in under 45 seconds.
- Variety across topics.
- Do NOT repeat or closely match any of these earlier topics: {' | '.join(used[-100:]) if used else '(none yet)'}
{trend_rules}
Return ONLY a JSON array in this shape:
[{{"title":"short working title","hook":"the first spoken line","angle":"what the video explains","trend":"the trending item it connects to, or empty"}}]"""
    ideas = gemini(prompt)
    if not isinstance(ideas, list) or not ideas:
        raise RuntimeError("Idea agent returned no ideas")
    return ideas


def make_script(idea):
    if isinstance(idea.get("script"), dict):
        return idea["script"]  # pre-written (content.json): no Gemini needed
    prompt = f"""You are the scriptwriting agent for a faceless Instagram Reels channel about: {NICHE}.

Write a 30-45 second voice-over script (about 85-110 words total) for this idea:
Title: {idea.get('title')}
Hook: {idea.get('hook')}
Angle: {idea.get('angle')}

Rules:
- Open with a hook in the first sentence. End with a short closing line that invites a follow.
- Only well-established facts. If you are not sure of a number, leave it out.
- Plain spoken English, short sentences, no emojis, no stage directions.
- Split the script into 6-9 scenes. For each scene give: narration (what is spoken),
  on_screen_text (max 6 words, punchy), and visual_keyword (1-3 words to search vertical stock footage;
  concrete and visual, e.g. "ocean waves", "night sky").
- Write an Instagram caption (max 300 characters, no hashtags in it) and 5-8 hashtags.
- Add a fact_check list: the specific factual claims made, so a human can verify them.

Return ONLY JSON:
{{"scenes":[{{"narration":"","on_screen_text":"","visual_keyword":""}}],"caption":"","hashtags":["#tag"],"fact_check":["claim"]}}"""
    script = gemini(prompt)
    scenes = script.get("scenes") if isinstance(script, dict) else None
    if not scenes or len(scenes) < 3:
        raise RuntimeError("Script has too few scenes")
    for s in scenes:
        s["narration"] = str(s.get("narration", "")).strip()
        s["on_screen_text"] = str(s.get("on_screen_text", "")).strip()
        s["visual_keyword"] = str(s.get("visual_keyword", "nature")).strip() or "nature"
    return script


# --------------------------------------------------------------------------- voice
async def _tts(text, voice, path):
    import edge_tts  # pip install edge-tts
    await edge_tts.Communicate(text, voice).save(str(path))


def synth_voice(text, path):
    asyncio.run(_tts(text, VOICE, path))


# --------------------------------------------------------------------------- pexels
def pexels_clip(keyword, used_ids):
    if not PEXELS_KEY:
        return None, None
    q = urllib.parse.quote(keyword)
    url = f"https://api.pexels.com/videos/search?query={q}&orientation=portrait&per_page=15"
    res = http_json(url, headers={"Authorization": PEXELS_KEY})
    for v in res.get("videos", []):
        if v["id"] in used_ids:
            continue
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"
                 and f.get("height", 0) > f.get("width", 0)]
        good = [f for f in files if 540 <= f["width"] <= 1100] or files
        if good:
            used_ids.add(v["id"])
            return v["id"], good[0]["link"]
    return None, None


def pixabay_clip(keyword, used_ids):
    """Pixabay has no portrait filter; landscape clips are centre-cropped to 9:16 by FFmpeg."""
    if not PIXABAY_KEY:
        return None, None
    q = urllib.parse.quote(keyword)
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_KEY}&q={q}&per_page=20&safesearch=true"
    res = http_json(url)
    for v in res.get("hits", []):
        vid = f"pb{v['id']}"
        if vid in used_ids:
            continue
        for size in ("medium", "large", "small"):
            f = (v.get("videos") or {}).get(size) or {}
            if f.get("url") and f.get("width", 0) >= 640:
                used_ids.add(vid)
                return vid, f["url"]
    return None, None


GRADIENTS = [("0x1b2a49", "0x7a3cff", "0x00c2ff"), ("0x0f2027", "0x2c5364", "0x2ecc71"),
             ("0x42275a", "0x734b6d", "0xf6d365"), ("0x141e30", "0x243b55", "0xff6a88"),
             ("0x232526", "0x414345", "0x00d2ff"), ("0x0b486b", "0xf56217", "0xffd166")]


def gradient_clip(index):
    """Animated colour background made by FFmpeg: no key, no download."""
    c0, c1, c2 = GRADIENTS[index % len(GRADIENTS)]
    dest = CACHE_DIR / f"gradient_{index % len(GRADIENTS)}.mp4"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             f"gradients=s={W}x{H}:d=10:speed=0.03:c0={c0}:c1={c1}:c2={c2}:nb_colors=3:rate={FPS}",
             "-pix_fmt", "yuv420p", str(dest)])
    return dest


def fetch_clip(keyword, used_ids, index=0):
    """Pexels -> Pixabay -> generated gradient. Footage problems never stop a video."""
    for provider in (pexels_clip, pixabay_clip):
        for kw in (keyword, keyword.split()[0]):
            try:
                vid, link = provider(kw, used_ids)
            except Exception as e:
                log(f"    ({provider.__name__} failed: {e})")
                break
            if link:
                dest = CACHE_DIR / f"{vid}.mp4"
                if not dest.exists():
                    download(link, dest)
                return dest
    return gradient_clip(index)


# --------------------------------------------------------------------------- video
def need_tool(name):
    if not shutil.which(name):
        raise SystemExit(f"'{name}' not found on PATH. Install FFmpeg (see README).")


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(map(str, cmd[:3]))}...\n{r.stderr[-800:]}")
    return r.stdout


def audio_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip())


def find_font(size):
    from PIL import ImageFont
    candidates = [
        os.environ.get("FONT_FILE", ""),
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return ImageFont.truetype(c, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_overlay(text, path):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = find_font(96)
    small = find_font(40)

    words, lines, line = text.upper().split(), [], ""
    for w in words:
        trial = (line + " " + w).strip()
        if d.textlength(trial, font=font) <= W - 160:
            line = trial
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)

    line_h = 118
    y = int(H * 0.68) - (len(lines) * line_h) // 2
    for ln in lines:
        x = (W - d.textlength(ln, font=font)) / 2
        d.text((x, y), ln, font=font, fill="white", stroke_width=9, stroke_fill="black")
        y += line_h
    if HANDLE:
        x = (W - d.textlength(HANDLE, font=small)) / 2
        d.text((x, H - 170), HANDLE, font=small, fill=(255, 255, 255, 215), stroke_width=4, stroke_fill="black")
    img.save(path)


def render_scene(clip, overlay, audio, dur, out):
    flt = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,"
        f"eq=brightness=-0.06[bg];"
        f"[1:v]format=rgba,fade=t=in:st=0.1:d=0.35:alpha=1[ov];"
        f"[bg][ov]overlay=0:0:shortest=1,format=yuv420p[v];"
        f"[2:a]apad,aresample=44100[a]"
    )
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-stream_loop", "-1", "-i", str(clip),
         "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(overlay),
         "-i", str(audio),
         "-filter_complex", flt, "-map", "[v]", "-map", "[a]",
         "-t", f"{dur:.3f}", "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", str(out)])


def concat_scenes(scene_files, out):
    work = scene_files[0].parent
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in scene_files), encoding="utf-8")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
         "-c", "copy", "-movflags", "+faststart", str(out)], cwd=work)


# --------------------------------------------------------------------------- build one video
def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "video"


def build_video(idea, script, folder, selftest=False):
    work = folder / "_work"
    work.mkdir(parents=True, exist_ok=True)
    used_ids, scene_files, clips = set(), [], []
    for i, s in enumerate(script["scenes"]):
        audio = work / f"a{i}.mp3"
        clip = work / f"clip{i}.mp4"
        if selftest:
            audio = work / f"a{i}.wav"
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"sine=frequency={200 + 40 * i}:duration=2.5", str(audio)])
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc2=size=540x960:rate=30:duration=2", "-pix_fmt", "yuv420p", str(clip)])
        else:
            synth_voice(s["narration"], audio)
            clip = fetch_clip(s["visual_keyword"], used_ids, i)
        clips.append(clip)
        dur = audio_duration(audio) + 0.25
        overlay = work / f"o{i}.png"
        make_overlay(s["on_screen_text"] or s["narration"][:40], overlay)
        out = work / f"scene{i:02d}.mp4"
        render_scene(clip, overlay, audio, dur, out)
        scene_files.append(out)
        log(f"    scene {i + 1}/{len(script['scenes'])} done ({dur:.1f}s)")
    script["footage_sources"] = sorted({"Pixabay" if c.name.startswith("pb") else "Pexels"
                                        for c in clips if c.name[:2] in ("pb",) or c.name[0].isdigit()})
    final = folder / "video.mp4"
    concat_scenes(scene_files, final)
    shutil.rmtree(work, ignore_errors=True)
    return final


def write_texts(folder, idea, script):
    tags = " ".join(script.get("hashtags", [])[:8])
    credit = ("\nFootage: " + " & ".join(script["footage_sources"])) if script.get("footage_sources") else ""
    (folder / "caption.txt").write_text(f"{script.get('caption', '')}\n\n{tags}{credit}\n", encoding="utf-8")
    review = [f"Title: {idea.get('title')}", "", "Narration:", " ".join(s["narration"] for s in script["scenes"]),
              "", "Check these claims before posting:"]
    review += [f"- {c}" for c in script.get("fact_check", [])]
    (folder / "review.txt").write_text("\n".join(review) + "\n", encoding="utf-8")
    (folder / "script.json").write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos", type=int, default=VIDEOS_PER_DAY, help="how many ideas to turn into videos")
    ap.add_argument("--ideas-only", action="store_true", help="only write today's 10 ideas")
    ap.add_argument("--gemini", action="store_true", help="ignore content.json and ask Gemini for new ideas and scripts")
    ap.add_argument("--fresh", action="store_true", help="ignore today's saved ideas and make new ones")
    ap.add_argument("--selftest", action="store_true", help="test FFmpeg/Pillow rendering offline, no API keys")
    args = ap.parse_args()

    need_tool("ffmpeg")
    need_tool("ffprobe")
    day = OUT_DIR / date.today().isoformat()
    day.mkdir(parents=True, exist_ok=True)

    if args.selftest:
        log("Self-test: rendering a fake 3-scene video...")
        idea = {"title": "Self test"}
        script = {"scenes": [{"narration": "x", "on_screen_text": t, "visual_keyword": "x"}
                             for t in ("Hello curious world", "This is a render test", "It works")],
                  "caption": "test", "hashtags": ["#test"], "fact_check": []}
        folder = day / "selftest"
        folder.mkdir(exist_ok=True)
        final = build_video(idea, script, folder, selftest=True)
        log(f"OK: {final}")
        return

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else {"used": []}
    ideas_file = day / "ideas.json"
    content_file = Path(__file__).parent / "content.json"
    if content_file.exists() and not args.gemini and not ideas_file.exists():
        bundled = json.loads(content_file.read_text(encoding="utf-8"))
        ideas = [x for x in bundled if x["title"] not in history["used"]]
        if ideas:
            log(f"Using {len(ideas)} ready-made ideas and scripts from content.json (no waiting on Gemini)")
            ideas_file.write_text(json.dumps(ideas, indent=2, ensure_ascii=False), encoding="utf-8")
    if ideas_file.exists() and not args.fresh:
        ideas = json.loads(ideas_file.read_text(encoding="utf-8"))
        log(f"Reusing today's {len(ideas)} ideas (use --fresh for new ones)")
    else:
        log("Looking at what is trending on the web...")
        trending = get_trending()
        log("Generating today's 10 ideas...")
        ideas = make_ideas(history["used"], trending)
        ideas_file.write_text(json.dumps(ideas, indent=2, ensure_ascii=False), encoding="utf-8")
    (day / "ideas.md").write_text(
        "# Ideas for " + date.today().isoformat() + "\n\n" +
        "\n".join(f"{i + 1}. **{x.get('title')}**: {x.get('hook')}" +
                  (f"  _(trend: {x['trend']})_" if x.get("trend") else "") for i, x in enumerate(ideas)) + "\n",
        encoding="utf-8")
    log(f"Saved {len(ideas)} ideas to {day / 'ideas.md'}")
    if args.ideas_only:
        return

    made = 0
    for n, idea in enumerate(ideas[:max(1, args.videos)], start=1):
        title = idea.get("title", f"idea {n}")
        log(f"[{n}/{args.videos}] {title}")
        folder = day / f"{n:02d}-{slugify(title)}"
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / "video.mp4").exists():
            log("    already made, skipping")
            made += 1
            continue
        try:
            script = make_script(idea)
            build_video(idea, script, folder)
            write_texts(folder, idea, script)
            history["used"].append(title)
            HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
            made += 1
            log(f"    ready: {folder / 'video.mp4'}")
        except Exception as e:  # keep going with the next idea
            log(f"    FAILED: {e}")
    log(f"Done: {made} video(s) in {day}")
    if made == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
