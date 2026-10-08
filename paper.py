#!/usr/bin/env python3
"""The Morning Paper: a free, automatic daily news podcast.

Every morning GitHub Actions runs this script. It pulls weather from the
National Weather Service (Open-Meteo outside the US) and headlines from free
news feeds, has Google's Gemini (free tier) write a radio-style script, reads
it aloud with Gemini's free text-to-speech voice (with Kokoro, a free
open-source voice, as a backup), and updates the podcast feed that GitHub Pages
serves.

Everything you're likely to change is in SETTINGS below.
"""

import argparse
import base64
import datetime as dt
import email.utils
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

# ─── SETTINGS ────────────────────────────────────────────────────────────────
SHOW_TITLE = "The Morning Paper"
TIMEZONE = "America/Chicago"      # your time zone; "America/Los_Angeles" after the move
VOICE = "Kore"                    # Gemini voice; "Charon" or "Puck" for a man's voice
VOICE_STYLE = ("like a relaxed, confident morning radio host: warm, conversational, "
               "natural pace, upbeat but not over the top")
BACKUP_VOICE = "af_heart"         # free backup voice, used only if Gemini's voice is down
SPEED = 1.0                       # backup voice speed; 1.1 reads a little faster
KEEP_EPISODES = 7                 # older episodes get deleted
EPISODE_WORDS = 1350              # length target; the voice reads about 150 words a minute
GEMINI_MODELS = [                 # tried in order; newer Flash models get added automatically
    "gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
    "gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
]


def weather(lat, lon, timezone):
    """A weather report for one spot. US spots use the National Weather Service;
    anywhere else uses Open-Meteo. Both are free."""
    return {"lat": lat, "lon": lon, "timezone": timezone}


def gnews(query, portuguese=False):
    """A Google News search feed covering the last day."""
    q = urllib.parse.quote(f"{query} when:1d")
    edition = "hl=pt-BR&gl=BR&ceid=BR:pt-419" if portuguese else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={q}&{edition}"


# The paper, in order. Weather reports can go anywhere in the lineup.
# News sections list (source name, feed). A source of None means the outlet is
# named on each item, which is how Google News feeds work.
SECTIONS = {
    "Arkadelphia weather": weather(34.1209, -93.0538, "America/Chicago"),
    "Top headlines": [
        ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        (None, "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"),
    ],
    "Chiefs and NFL": [
        (None, gnews('"Kansas City Chiefs"')),
        ("Arrowhead Pride", "https://www.arrowheadpride.com/rss/index.xml"),
        ("ESPN", "https://www.espn.com/espn/rss/nfl/news"),
    ],
    "Jayhawks basketball": [
        (None, gnews('"Kansas Jayhawks" basketball -football')),
        ("Rock Chalk Talk", "https://www.rockchalktalk.com/rss/index.xml"),
    ],
    "Fantasy football": [
        (None, gnews("fantasy football waiver wire")),
        (None, gnews("fantasy football injury update")),
    ],
    "Los Angeles weather": weather(34.0522, -118.2437, "America/Los_Angeles"),
    "Hollywood and screenwriting": [
        ("Deadline", "https://deadline.com/feed/"),
        ("Variety", "https://variety.com/feed/"),
        ("The Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
        (None, gnews('screenwriter OR screenplay OR "spec script" OR WGA')),
    ],
    "AI and tech": [
        ("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        (None, gnews('"AI model" OR "AI tool" launch')),
    ],
    "Rio de Janeiro weather": weather(-22.9068, -43.1729, "America/Sao_Paulo"),
    "Brazil and Rio": [
        (None, gnews("Brazil")),
        ("The Rio Times", "https://www.riotimesonline.com/feed/"),
        (None, gnews('"Rio de Janeiro"', portuguese=True)),
    ],
}

# Headlines to drop from a section, matched against the title.
SKIP_TITLES = {
    "Jayhawks basketball": r"\bfootball\b",
}

# Names the voice gets wrong, respelled the way they should sound.
SAY_AS = {
    "Mahomes": "Muh-homes",
    "Kelce": "Kelsey",
    "Pacheco": "Puh-chay-ko",
    "Arkadelphia": "Arka-delphia",
    "Flamengo": "Fla-mengo",
    "TDs": "touchdowns",
    "TD": "touchdown",
    "mph": "miles per hour",
}

MAX_WORDS = EPISODE_WORDS + 150    # about ten minutes
SHORT_WORDS = EPISODE_WORDS - 250  # a draft shorter than this gets one rewrite

HOST_BRIEF = f"""You write the script for {SHOW_TITLE}, a personal daily news podcast for one listener who usually hears it while driving. A text-to-speech voice reads your script word for word, so write only what should be said out loud.

Style
- Sound like a sharp morning radio host: casual, direct, no filler, no hype.
- Plain spoken sentences only. No headings, bullet points, lists, symbols, emojis, links, or stage directions.
- Write numbers, scores, times, money, and percentages as words ("seventy-eight degrees", "a twenty-seven to twenty-four win", "seven oh two a.m.", "five million dollars").
- Give temperatures in Fahrenheit.
- Name sources in passing ("per ESPN", "Deadline reports").
- Straight news. No opinions or spin, especially on politics.
- Put a blank line between sections, and move between them with a quick spoken transition instead of a heading.

Rules
- Use only the weather data and news items provided. Never add facts, names, numbers, or details that aren't in them. If an item is only a headline, say only what the headline says.
- Length matters: write about {EPISODE_WORDS:,} words, which reads in about {round(EPISODE_WORDS / 150)} minutes, and never more than {MAX_WORDS:,}. Only come in shorter when the news itself is thin.
- In each news section, cover three or four stories when there's enough news, in two or three sentences each, using the details in each item's summary. Give the biggest stories a little more. A full news section runs about 150 to 200 words, and each weather report about 50 to 70 words.
- Pick the most important items and skip duplicates, listicles, reviews, and promotional pieces.
- If a section has nothing worthwhile, give it one line or skip it.
- Translate anything in Portuguese into natural English.
- Open with a one-line greeting that includes the day and date. End with exactly: That's the paper.

Lineup, in this order
1. Arkadelphia weather: today's high and low, rain chances, anything notable such as alerts, plus sunrise and sunset.
2. Top headlines: the three to five biggest national and world stories.
3. Chiefs and NFL: Chiefs first (latest game, injuries, what's next), then the biggest league news.
4. Jayhawks basketball: Kansas men's basketball only. Skip other schools' basketball, Kansas football, and every other sport, even when those stories show up in the items. If there's no real Kansas basketball news, say so in one line. In season, results and what's next. In the offseason or preseason, only real news like rankings, recruiting, or roster moves.
5. Fantasy football: injuries, role changes, and waiver pickups worth grabbing.
6. Los Angeles weather: the same rundown as Arkadelphia.
7. Hollywood and screenwriting: deals, spec and pitch sales, greenlights, staffing, and WGA news.
8. AI and tech: new tools or models worth knowing about, especially for writing or automation.
9. Rio de Janeiro weather: the same rundown, with sunrise and sunset in Rio's local time.
10. Brazil: national news, plus local news from Rio de Janeiro.
"""

SAMPLE = ("This is a test of The Morning Paper. If you can hear this in your podcast app, "
          "everything is working. Tomorrow morning you get the real thing: weather, headlines, "
          "the Chiefs, and the rest of your lineup.\n\nThat's the paper.")

MODEL_DIR = Path("models")
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def log(message):
    print(message, flush=True)


def fetch(url, headers=None, timeout=25):
    request = urllib.request.Request(url, headers={**BROWSER, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ─── News ────────────────────────────────────────────────────────────────────
def strip_html(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"The post .*? appeared first on .*$", "", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip()


def summary_of(entry):
    """The fullest text the feed gives for an item, trimmed to a few sentences."""
    texts = [strip_html(part.get("value")) for part in entry.get("content") or []]
    texts.append(strip_html(entry.get("summary")))
    text = max(texts, key=len)
    if len(text) > 600:
        text = text[:600].rsplit(" ", 1)[0] + "..."
    return text


def feed_items(source, url, cutoff):
    import feedparser

    parsed = feedparser.parse(fetch(url))
    items = []
    for entry in parsed.entries:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        when = dt.datetime(*stamp[:6], tzinfo=dt.timezone.utc) if stamp else None
        if when and when < cutoff:
            continue
        title, outlet = strip_html(entry.get("title")), source
        if source is None:  # Google News titles end with " - Outlet"
            outlet = (entry.get("source") or {}).get("title") or ""
            if " - " in title:
                title, tail = title.rsplit(" - ", 1)
                outlet = outlet or tail
        if not title:
            continue
        items.append({
            "title": title,
            "source": outlet or "news reports",
            "summary": summary_of(entry) if source else "",
            "when": when,
            "portuguese": "hl=pt-BR" in url,
        })
    items.sort(key=lambda item: item["when"] or cutoff, reverse=True)
    return items[:12]


def gather(now):
    """Collect every section in lineup order: weather lines or news items."""
    cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(hours=26)
    report = {}
    for section, spec in SECTIONS.items():
        if isinstance(spec, dict):
            report[section] = {"weather": get_weather(spec, now)}
            log(f"  {section}: {len(report[section]['weather'])} lines")
            continue
        items, seen = [], set()
        skip = SKIP_TITLES.get(section)
        for source, url in spec:
            label = source or "Google News"
            try:
                found = feed_items(source, url, cutoff)
            except Exception as err:
                log(f"  {section}: skipped {label} ({err})")
                continue
            log(f"  {section}: {len(found)} from {label}")
            for item in found:
                if skip and re.search(skip, item["title"], re.I):
                    continue
                key = re.sub(r"\W+", " ", item["title"].lower()).strip()[:70]
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        report[section] = {"news": items[:30]}
    return report


# ─── Weather ─────────────────────────────────────────────────────────────────
NWS = {"User-Agent": "MorningPaper/1.0 (personal podcast)", "Accept": "application/geo+json"}
SKIES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
         51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle",
         57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
         67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow",
         80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers",
         86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
         99: "thunderstorms with hail"}


def nws_forecast(spot):
    where = f"{spot['lat']:.4f},{spot['lon']:.4f}"
    point = json.loads(fetch(f"https://api.weather.gov/points/{where}", NWS))
    forecast = json.loads(fetch(point["properties"]["forecast"], NWS))
    lines = []
    for period in forecast["properties"]["periods"][:3]:
        rain = (period.get("probabilityOfPrecipitation") or {}).get("value")
        rain = f" Chance of rain: {rain}%." if rain is not None else ""
        lines.append(f"{period['name']}: {period['detailedForecast']}{rain}")
    try:
        alerts = json.loads(fetch(f"https://api.weather.gov/alerts/active?point={where}", NWS))
        events = sorted({f["properties"].get("event") or "" for f in alerts.get("features", [])} - {""})
        lines.append("Active weather alerts: " + (", ".join(events) if events else "none") + ".")
    except Exception as err:
        log(f"    weather alerts unavailable ({err})")
    return lines


def open_meteo_forecast(spot):
    query = urllib.parse.urlencode({
        "latitude": spot["lat"], "longitude": spot["lon"], "timezone": spot["timezone"],
        "forecast_days": 1, "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,wind_gusts_10m_max,uv_index_max"})
    day = json.loads(fetch(f"https://api.open-meteo.com/v1/forecast?{query}"))["daily"]
    value = lambda key: (day.get(key) or [None])[0]
    parts = [f"Today: {SKIES.get(value('weather_code'), 'mixed skies')}"]
    if value("temperature_2m_max") is not None:
        parts.append(f"high near {round(value('temperature_2m_max'))} degrees Fahrenheit")
    if value("temperature_2m_min") is not None:
        parts.append(f"low around {round(value('temperature_2m_min'))}")
    if value("precipitation_probability_max") is not None:
        parts.append(f"{round(value('precipitation_probability_max'))}% chance of rain")
    if value("wind_gusts_10m_max") is not None:
        parts.append(f"gusts up to {round(value('wind_gusts_10m_max'))} mph")
    if value("uv_index_max") is not None:
        parts.append(f"UV index {round(value('uv_index_max'))}")
    return [", ".join(parts) + "."]


def get_weather(spot, now):
    try:
        lines = nws_forecast(spot)          # works for US spots
    except Exception:
        try:
            lines = open_meteo_forecast(spot)  # everywhere else, or if NWS is down
        except Exception as err:
            log(f"    forecast unavailable ({err})")
            lines = []
    try:
        from astral import Observer
        from astral.sun import sun

        zone = ZoneInfo(spot["timezone"])
        times = sun(Observer(spot["lat"], spot["lon"]), date=now.astimezone(zone).date(), tzinfo=zone)
        clock = lambda t: t.strftime("%-I:%M ") + ("a.m." if t.hour < 12 else "p.m.")
        lines.append(f"Sunrise {clock(times['sunrise'])}, sunset {clock(times['sunset'])}, local time")
    except Exception as err:
        log(f"    sunrise and sunset unavailable ({err})")
    return lines


# ─── Script ──────────────────────────────────────────────────────────────────
def build_packet(now, report):
    lines = [f"Today is {now:%A, %B} {now.day}, {now.year}. Local time is {now:%-I:%M %p}."]
    utc_now = now.astimezone(dt.timezone.utc)
    for section, content in report.items():
        lines += ["", section.upper()]
        if "weather" in content:
            lines += [f"- {line}" for line in content["weather"]] or ["- unavailable"]
            continue
        if not content["news"]:
            lines.append("- nothing in the last day")
        for item in content["news"]:
            age = ""
            if item["when"]:
                age = f", {max(0, (utc_now - item['when']).total_seconds() / 3600):.0f}h ago"
            line = f"- [{item['source']}{age}] {item['title']}"
            if item["summary"] and item["summary"].lower() != item["title"].lower():
                line += f" | {item['summary']}"
            lines.append(line)
    return "\n".join(lines)


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_API = f"{GEMINI_BASE}/models"
BUSY_WAITS = (2, 5, 10)           # minutes to wait before trying again when Gemini is busy
GEMINI_DEADLINE = 25 * 60         # seconds to keep trying before falling back to headlines


def gemini_models(key):
    """The models from SETTINGS, plus any other current Flash models Google lists."""
    models = list(GEMINI_MODELS)
    try:
        listing = json.loads(fetch(f"{GEMINI_API}?pageSize=1000", {"x-goog-api-key": key}))
    except Exception as err:
        log(f"  Couldn't list Gemini models ({err})")
        return models
    found = []
    for entry in listing.get("models", []):
        match = re.fullmatch(r"models/(gemini-(\d+)(?:\.(\d+))?-flash(-lite)?)", entry.get("name", ""))
        if match and "generateContent" in entry.get("supportedGenerationMethods", []):
            name, major, minor, lite = match.groups()
            found.append((bool(lite), -int(major), -int(minor or 0), name))
    models += [name for *_, name in sorted(found) if name not in models]
    return models[:10]


def ask_gemini(key, model, prompt):
    """One request to one model. Returns (text, worth trying again later)."""
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": HOST_BRIEF}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6},
        "safetySettings": [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in (
            "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")],
    }).encode()
    request = urllib.request.Request(f"{GEMINI_API}/{model}:generateContent", data=body, headers={
        "Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.load(response)
    except urllib.error.HTTPError as err:
        detail = " ".join(err.read().decode(errors="replace").split())[:300]
        log(f"  Gemini {model}: HTTP {err.code} {detail}")
        return None, err.code == 429 or err.code >= 500  # busy or rate-limited
    except Exception as err:
        log(f"  Gemini {model}: {err}")
        return None, True
    candidate = (data.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if len(text.split()) >= 150:
        return text, False
    reason = candidate.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason")
    log(f"  Gemini {model}: unusable reply ({reason})")
    return None, False


def write_script(packet):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log("No GEMINI_API_KEY secret found, so using the basic headline read.")
        return None
    models = gemini_models(key)
    give_up = time.monotonic() + GEMINI_DEADLINE
    for wait in (0, *BUSY_WAITS):
        if wait:
            if time.monotonic() + wait * 60 > give_up:
                break
            log(f"Gemini is busy, so trying again in {wait} minutes...")
            time.sleep(wait * 60)
        busy = []
        for model in models:
            if time.monotonic() > give_up:
                break
            text, retry = ask_gemini(key, model, packet)
            if text:
                log(f"Script written by {model}: {len(text.split())} words")
                return fill_out(key, model, packet, text)
            if retry:
                busy.append(model)
        if not busy:
            break
        models = busy  # models that are gone or refused don't get asked again
    log("Gemini wasn't available, so using the basic headline read.")
    return None


def fill_out(key, model, packet, text):
    """Give a draft that runs short one rewrite to bring it up to length."""
    words = len(text.split())
    if words >= SHORT_WORDS:
        return text
    log("  That runs short, so asking for a fuller version...")
    fuller, _ = ask_gemini(key, model, (
        f"{packet}\n\n---\n\nHere is a draft script built from the items above. It's only "
        f"{words} words, so it runs short. Rewrite it at about {EPISODE_WORDS:,} words by "
        "covering more of the stories above and using more of the detail in their summaries. "
        "Every rule still applies: only facts from the items, the same lineup and order, "
        "and end with exactly: That's the paper.\n\nDRAFT\n" + text))
    if fuller and words < len(fuller.split()) <= MAX_WORDS + 100:
        log(f"  Fuller version: {len(fuller.split())} words")
        return fuller
    log("  Kept the first draft")
    return text


def basic_script(now, report):
    """Backup script if Gemini is down: weather plus the top headlines, in lineup order."""
    parts = [f"Good morning. It's {now:%A, %B} {now.day}. "
             "The writer is out today, so here are the headlines."]
    for section, content in report.items():
        if content.get("weather"):
            parts.append(f"{section}. " + " ".join(content["weather"]))
            continue
        reads = [f"From {i['source']}: {i['title'].rstrip('.')}."
                 for i in content.get("news", []) if not i["portuguese"]][:3]
        if reads:
            parts.append(f"{section}. " + " ".join(reads))
    parts.append("That's the paper.")
    return "\n\n".join(parts)


def strip_markup(text):
    """Turn whatever the writer returns into clean paragraphs of plain sentences."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        lines = []
        for line in block.splitlines():
            line = re.sub(r"^[ \t]*(?:#+|>|[-*•]|\d+[.)])[ \t]+", "", line)
            line = re.sub(r"#(?=\d)", "number ", line)
            line = " ".join(re.sub(r"[*_`#]", "", line).split())
            if not line:
                continue
            if len(line.split()) <= 6 and not re.search(r"[.!?:;,\"'\u201d\u2019)]$", line):
                line += "."  # a heading like "Weather" becomes a spoken signpost
            lines.append(line)
        if lines:
            paragraphs.append(" ".join(lines))
    return "\n\n".join(paragraphs)


def spoken_form(text):
    """Small fixes so the voice reads things the way a person would."""
    text = re.sub(r"\bvs\.?(?=\s)", "versus", text)
    for word, spoken in SAY_AS.items():
        text = re.sub(rf"\b{re.escape(word)}\b", spoken, text)
    text = re.sub(r"\b(\d{1,2}):00\b", r"\1", text)
    text = re.sub(r"\b(\d{1,2}):0(\d)\b", r"\1 oh \2", text)
    text = re.sub(r"\b(\d{1,2}):(\d\d)\b", r"\1 \2", text)
    text = re.sub(r"(?<=\d)[ \t]*[-–][ \t]*(?=\d)", " to ", text)
    text = re.sub(r"\$(\d[\d,.]*)(?:[ \t]*(million|billion|thousand))?",
                  lambda m: " ".join(filter(None, [m.group(1), m.group(2), "dollars"])), text)
    text = re.sub(r"°\s*[FC]\b", " degrees", text)
    text = text.replace("&", " and ").replace("%", " percent").replace("°", " degrees")
    text = re.sub(r"[ \t]*[—–][ \t]*", ", ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r" ([,.!?])", r"\1", text).strip()


# ─── Audio ───────────────────────────────────────────────────────────────────
def ensure_models():
    MODEL_DIR.mkdir(exist_ok=True)
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        path = MODEL_DIR / name
        if not path.exists() or path.stat().st_size < 1_000_000:
            log(f"Downloading voice model {name}...")
            partial = path.with_suffix(".part")
            urllib.request.urlretrieve(f"{MODEL_URL}/{name}", partial)
            partial.rename(path)


def level(audio):
    """Even out the volume so it holds up over road noise, without clipping."""
    import numpy as np

    speech = audio[np.abs(audio) > 0.02]
    if speech.size:
        audio = audio * (0.1 / float(np.sqrt(np.mean(speech ** 2))))
    loud = np.abs(audio) > 0.9
    audio[loud] = np.sign(audio[loud]) * (0.9 + 0.1 * np.tanh((np.abs(audio[loud]) - 0.9) / 0.1))
    return audio


_backup = None


def backup_voice():
    global _backup
    if _backup is None:
        from kokoro_onnx import Kokoro

        ensure_models()
        _backup = Kokoro(str(MODEL_DIR / "kokoro-v1.0.onnx"), str(MODEL_DIR / "voices-v1.0.bin"))
    return _backup


def synthesize(script):
    """Read text with the free backup voice (Kokoro)."""
    import numpy as np

    rate, pieces = 24000, []
    for paragraph in [p for p in script.split("\n\n") if p.strip()]:
        audio, rate = backup_voice().create(paragraph, voice=BACKUP_VOICE, speed=SPEED, lang="en-us")
        pieces += [audio, np.zeros(int(rate * 0.7), dtype=np.float32)]
    audio = np.concatenate(pieces) if pieces else np.zeros(rate, dtype=np.float32)
    return level(audio.astype(np.float32)), rate


TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
TTS_CHUNK_WORDS = 600             # about four minutes of audio per request


def speech_chunks(script, limit=TTS_CHUNK_WORDS):
    """Split the script at paragraph breaks into a few pieces for the voice to read."""
    chunks, current = [], []
    for paragraph in [p for p in script.split("\n\n") if p.strip()]:
        if current and len(" ".join(current + [paragraph]).split()) > limit:
            chunks.append("\n\n".join(current))
            current = []
        current.append(paragraph)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def wav_audio(raw):
    """Gemini sends a WAV file (or bare 16-bit PCM). Returns 24 kHz mono samples."""
    import numpy as np

    rate, channels = 24000, 1
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        pos = 12
        while pos + 8 <= len(raw):
            kind, size = raw[pos:pos + 4], int.from_bytes(raw[pos + 4:pos + 8], "little")
            if kind == b"fmt ":
                channels = int.from_bytes(raw[pos + 10:pos + 12], "little") or 1
                rate = int.from_bytes(raw[pos + 12:pos + 16], "little") or 24000
            elif kind == b"data":
                end = pos + 8 + size
                raw = raw[pos + 8:end if end <= len(raw) else len(raw)]  # size can be a placeholder
                break
            pos += 8 + size + (size & 1)
    samples = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
    if channels > 1:
        samples = samples[:len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != 24000 and samples.size:
        steps = np.arange(0, len(samples), rate / 24000)
        samples = np.interp(steps, np.arange(len(samples)), samples).astype(np.float32)
    return samples


def audio_parts(node):
    """Every base64 audio clip in a Gemini reply, in order."""
    if isinstance(node, dict):
        if node.get("type") == "audio" and isinstance(node.get("data"), str):
            yield node["data"]
        for value in node.values():
            yield from audio_parts(value)
    elif isinstance(node, list):
        for value in node:
            yield from audio_parts(value)


def gemini_speech(key, model, text):
    """One part of the script read by Gemini's voice, or None if it didn't work."""
    body = json.dumps({
        "model": model,
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": re.sub(r"[<>]", " ", text),  # angle brackets are cues to the voice
                "annotations": [{"type": "speech_metadata", "style": VOICE_STYLE}],
            }],
        }],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": VOICE}]},
    }).encode()
    for wait in (0, 30, 90):
        if wait:
            log(f"  Trying the voice again in {wait} seconds...")
            time.sleep(wait)
        request = urllib.request.Request(f"{GEMINI_BASE}/interactions", data=body, headers={
            "Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                data = json.load(response)
        except urllib.error.HTTPError as err:
            detail = " ".join(err.read().decode(errors="replace").split())[:300]
            log(f"  Voice {model}: HTTP {err.code} {detail}")
            if err.code == 429 or err.code >= 500:
                continue
            return None
        except Exception as err:
            log(f"  Voice {model}: {err}")
            continue
        clips = list(audio_parts(data))
        if not clips:
            log(f"  Voice {model}: no audio in the reply {json.dumps(data)[:200]}")
            return None
        audio = wav_audio(base64.b64decode(clips[-1]))
        if len(audio) / 24000 < len(text.split()) / 2.6 * 0.6:  # well under 150 words a minute
            log(f"  Voice {model}: the audio came back cut short")
            return None
        return audio
    return None


def record(script):
    """Read the script with Gemini's voice. The backup voice covers anything it can't."""
    import numpy as np

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    chunks = speech_chunks(script)
    pieces, use_gemini, backup_parts = [], bool(key), 0
    for number, chunk in enumerate(chunks, 1):
        audio = None
        if use_gemini:
            if number > 1:
                time.sleep(20)  # stays under the free tier's per-minute limit
            for model in TTS_MODELS:
                audio = gemini_speech(key, model, chunk)
                if audio is not None:
                    break
            if audio is None:
                use_gemini = False
                log("Gemini's voice isn't available, so the backup voice reads the rest.")
        if audio is None:
            audio, _ = synthesize(spoken_form(chunk))
            backup_parts += 1
        pieces += [audio, np.zeros(int(24000 * 0.6), dtype=np.float32)]
    log(f"Recorded {len(chunks)} parts, {len(chunks) - backup_parts} with Gemini voice {VOICE}")
    return level(np.concatenate(pieces).astype(np.float32)), 24000


def to_mp3(audio, rate):
    import lameenc
    import numpy as np

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(64)
    encoder.set_in_sample_rate(rate)
    encoder.set_out_sample_rate(44100)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return bytes(encoder.encode(pcm.tobytes()) + encoder.flush())


# ─── Podcast feed and page ───────────────────────────────────────────────────
def site_url():
    if os.environ.get("PAGES_URL"):
        return os.environ["PAGES_URL"].rstrip("/")
    owner, repo = os.environ.get("GITHUB_REPOSITORY", "your-name/morning-paper").split("/", 1)
    owner = owner.lower()
    if repo.lower() == f"{owner}.github.io":
        return f"https://{owner}.github.io"
    return f"https://{owner}.github.io/{repo}"


def minutes(seconds):
    return f"{max(1, round(seconds / 60))} min"


def render_feed(base, episodes):
    items = []
    for ep in episodes:
        notes = "".join(f"<p>{html.escape(p, quote=False)}</p>" for p in ep["script"].split("\n\n"))[:3900]
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description><![CDATA[{notes.replace(']]>', ']] >')}]]></description>
      <enclosure url="{base}/{ep['file']}" length="{ep['bytes']}" type="audio/mpeg"/>
      <guid isPermaLink="false">morning-paper-{ep['date']}</guid>
      <pubDate>{ep['published']}</pubDate>
      <itunes:duration>{time.strftime('%H:%M:%S', time.gmtime(ep['seconds']))}</itunes:duration>
    </item>""")
    newest = episodes[0]["published"] if episodes else email.utils.format_datetime(
        dt.datetime.now(dt.timezone.utc))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(SHOW_TITLE)}</title>
    <link>{base}/</link>
    <atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>A personal daily news briefing.</description>
    <language>en-us</language>
    <lastBuildDate>{newest}</lastBuildDate>
    <itunes:author>{escape(SHOW_TITLE)}</itunes:author>
    <itunes:image href="{base}/cover.png"/>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""


def render_page(base, episodes):
    feed = f"{base}/feed.xml"
    if episodes:
        top = episodes[0]
        hero = (f'<p class="date">{html.escape(top["title"]).replace(", ", ",<br>", 1)}</p>\n'
                f'<audio controls preload="none" src="{top["file"]}"></audio>\n'
                f'<p class="len">{minutes(top["seconds"])}</p>')
    else:
        hero = '<p class="date">No episodes yet</p>'
    earlier = "\n".join(
        f'<li><span>{html.escape(ep["title"])}</span><span class="len">{minutes(ep["seconds"])}</span>'
        f'<audio controls preload="none" src="{ep["file"]}"></audio></li>' for ep in episodes[1:])
    earlier = f"<h2>Earlier</h2>\n<ul>\n{earlier}\n</ul>" if earlier else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{escape(SHOW_TITLE)}</title>
<link rel="alternate" type="application/rss+xml" title="{escape(SHOW_TITLE)}" href="{feed}">
<link rel="icon" href="cover.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{{--bg:#16202c;--ink:#e7ebf0;--muted:#93a0b3;--amber:#f2b84b;--on-amber:#16202c;--rule:#273445;color-scheme:dark light}}
@media (prefers-color-scheme:light){{:root{{--bg:#f4f6f9;--ink:#16202c;--muted:#566273;--amber:#8a5700;--on-amber:#fff;--rule:#d8dee6}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:1.0625rem/1.55 "Atkinson Hyperlegible",system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:34rem;margin:0 auto;padding:3rem 1.25rem 4rem}}
h1{{font-size:1rem;color:var(--muted);margin:0 0 1.25rem}}
.date{{font-size:clamp(2.4rem,10.5vw,4rem);line-height:1.02;font-weight:700;margin:0;padding-bottom:1.25rem;border-bottom:3px solid var(--amber)}}
audio{{display:block;width:100%;margin-top:1.25rem}}
.len{{color:var(--muted);font-size:.95rem;margin:.4rem 0 0}}
h2{{font-size:1.125rem;margin:3rem 0 .75rem}}
.feed{{display:flex;gap:.5rem}}
.feed input{{flex:1;min-width:0;font:inherit;font-size:.95rem;padding:.7rem .8rem;border:1px solid var(--rule);border-radius:.5rem;background:transparent;color:var(--ink)}}
button{{font:inherit;font-weight:700;padding:.7rem 1rem;border:0;border-radius:.5rem;background:var(--amber);color:var(--on-amber);cursor:pointer}}
button:focus-visible,input:focus-visible{{outline:3px solid var(--amber);outline-offset:2px}}
.hint{{color:var(--muted);font-size:.95rem;margin:.6rem 0 0}}
ul{{list-style:none;margin:0;padding:0}}
li{{display:grid;grid-template-columns:1fr auto;gap:.25rem 1rem;padding:1rem 0;border-top:1px solid var(--rule)}}
li .len{{margin:0}}
li audio{{grid-column:1/-1;margin-top:.5rem}}
</style>
</head>
<body>
<main>
<h1>{escape(SHOW_TITLE)}</h1>
{hero}
<h2>Add it to your podcast app</h2>
<div class="feed"><input id="feed" value="{feed}" readonly aria-label="Podcast feed link"><button id="copy" type="button">Copy link</button></div>
<p class="hint">In Apple Podcasts, choose Follow a Show by URL and paste this link.</p>
{earlier}
</main>
<script>
document.getElementById("copy").addEventListener("click", async (e) => {{
  const box = document.getElementById("feed");
  try {{ await navigator.clipboard.writeText(box.value); }} catch {{ box.select(); document.execCommand("copy"); }}
  e.target.textContent = "Copied";
  setTimeout(() => (e.target.textContent = "Copy link"), 2000);
}});
</script>
</body>
</html>
"""


def make_cover(path):
    """Square show art: the title over a sun coming up on the horizon."""
    from PIL import Image, ImageDraw, ImageFont

    size = 1400
    img = Image.new("RGB", (size, size), "#16202c")
    draw = ImageDraw.Draw(img)
    horizon = 1040
    draw.pieslice([880, horizon - 230, 1340, horizon + 230], 180, 360, fill="#f2b84b")
    draw.rectangle([0, horizon, size, horizon + 12], fill="#f2b84b")
    font = None
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, 190)
            break
    font = font or ImageFont.load_default(size=190)
    for i, word in enumerate(("The", "Morning", "Paper")):
        draw.text((110, 130 + i * 230), word, font=font, fill="#e7ebf0")
    img.save(path, optimize=True)


def publish(site, now, mp3, seconds, script):
    (site / "episodes").mkdir(parents=True, exist_ok=True)
    date = now.date().isoformat()
    file = f"episodes/{date}.mp3"
    (site / file).write_bytes(mp3)
    try:
        episodes = json.loads((site / "episodes.json").read_text())
    except Exception:
        episodes = []
    episodes = [ep for ep in episodes if ep.get("date") != date]
    episodes.append({"date": date, "title": f"{now:%A, %B} {now.day}", "file": file,
                     "bytes": len(mp3), "seconds": round(seconds),
                     "published": email.utils.format_datetime(now), "script": script})
    episodes = sorted(episodes, key=lambda ep: ep["date"], reverse=True)[:KEEP_EPISODES]
    keep = {ep["file"] for ep in episodes}
    for old in (site / "episodes").glob("*.mp3"):
        if f"episodes/{old.name}" not in keep:
            old.unlink()
    (site / "episodes.json").write_text(json.dumps(episodes, indent=1))
    base = site_url()
    (site / "feed.xml").write_text(render_feed(base, episodes), encoding="utf-8")
    (site / "index.html").write_text(render_page(base, episodes), encoding="utf-8")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (site / ".nojekyll").write_text("")
    if not (site / "cover.png").exists():
        make_cover(site / "cover.png")
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="site", help="folder that GitHub Pages serves")
    parser.add_argument("--sample", action="store_true", help="skip the news and voice a short test")
    args = parser.parse_args()

    now = dt.datetime.now(ZoneInfo(TIMEZONE))
    log(f"{SHOW_TITLE} for {now:%A, %B} {now.day}, {now.year}")
    if args.sample:
        raw = SAMPLE
    else:
        log("Gathering the weather and news...")
        report = gather(now)
        log("Writing the script...")
        raw = write_script(build_packet(now, report)) or basic_script(now, report)
    notes = strip_markup(raw)
    log("Recording...")
    audio, rate = record(notes)
    mp3 = to_mp3(audio, rate)
    seconds = len(audio) / rate
    base = publish(Path(args.site), now, mp3, seconds, notes)
    log(f"Done: {seconds / 60:.1f} minute episode.")
    log(f"Podcast feed: {base}/feed.xml")


if __name__ == "__main__":
    main()
