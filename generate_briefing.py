#!/usr/bin/env python3
"""
Daily Briefing Generator
Splits research into two API calls to stay under 30k token/min rate limit.
Outputs to ./output/index.html and ./output/archive/YYYY-MM-DD.html
"""

import anthropic
import os
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Date helpers ───────────────────────────────────────────────────────────────

def get_cst_date():
    cst = timezone(timedelta(hours=-6))
    now = datetime.now(cst)
    return now.strftime("%A, %B %-d, %Y"), now.strftime("%Y-%m-%d")

# ── JSON extraction ────────────────────────────────────────────────────────────

def extract_json(text):
    match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass
    return json.loads(text.strip())

# ── Retry helper ───────────────────────────────────────────────────────────────

def with_retry(fn, max_retries=4, base_delay=65):
    """Retry on 429 with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return fn()
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"    Rate limit. Waiting {delay}s (attempt {attempt+2}/{max_retries})...")
            time.sleep(delay)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                if attempt == max_retries - 1:
                    raise
                delay = base_delay * (2 ** attempt)
                print(f"    Overloaded. Waiting {delay}s (attempt {attempt+2}/{max_retries})...")
                time.sleep(delay)
            else:
                raise

# ── Research: call A — World / National / Finance ─────────────────────────────

def research_general(client, date_str):
    print("  [1a] Searching world, national, finance...")
    prompt = f"""Today is {date_str}. Search the web for today's top news.

Find 3 stories each for: WORLD NEWS, US NATIONAL NEWS, FINANCE & MARKETS.
Also get current stock values: DOW, S&P 500, NASDAQ, WTI crude oil price, national avg gas price.

Return RAW JSON only. No markdown. No preamble. Start with {{

{{
  "market_snapshot": {{
    "dow":    {{"value": "47,250", "change": "+120 (+0.25%)", "direction": "up"}},
    "sp500":  {{"value": "6,800",  "change": "+15 (+0.22%)",  "direction": "up"}},
    "nasdaq": {{"value": "22,100", "change": "+45 (+0.20%)",  "direction": "up"}},
    "wti":    {{"value": "$88.50", "change": "-1.20 (-1.3%)", "direction": "down"}},
    "gas_avg":{{"value": "$3.89",  "change": "+0.05"}}
  }},
  "breaking_alert": "",
  "world":    [{{"headline":"...","summary":"...","source":"...","url":"...","severity":"critical|high|watch|normal"}}],
  "national": [{{"headline":"...","summary":"...","source":"...","url":"...","severity":"normal"}}],
  "finance":  [{{"headline":"...","summary":"...","source":"...","url":"...","severity":"normal"}}]
}}"""

    msg = with_retry(lambda: client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    ))
    text = "".join(b.text for b in msg.content if b.type == "text")
    return extract_json(text)

# ── Research: call B — Cyber + Weather ────────────────────────────────────────

def research_cyber_weather(client, date_str):
    print("  [1b] Searching cybersecurity + weather...")
    prompt = f"""Today is {date_str}. Search the web for two things:

1. TOP 4 CYBERSECURITY stories: active threats, CVEs, CISA advisories, nation-state activity, ransomware, breaches. Include CVE numbers where relevant.

2. WEATHER for Spring TX (zip 77379): today's high, low, condition, rain chance %, wind, any NWS alerts.

Return RAW JSON only. No markdown. No preamble. Start with {{

{{
  "weather": {{
    "high": "84°F", "low": "68°F", "condition": "Partly Cloudy",
    "rain_chance": "20%", "alerts": "None", "wind": "S 10 mph"
  }},
  "cyber": [
    {{"headline":"...","summary":"...","source":"...","url":"...","severity":"critical|high|watch|normal"}}
  ]
}}"""

    msg = with_retry(lambda: client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    ))
    text = "".join(b.text for b in msg.content if b.type == "text")
    return extract_json(text)

# ── Merge results ──────────────────────────────────────────────────────────────

def research_news(client, date_str):
    print(f"[1/3] Researching news for {date_str}...")

    general = research_general(client, date_str)

    # Pause between calls to let the token-per-minute window reset partially
    print("  Pausing 15s between API calls...")
    time.sleep(15)

    cyber_wx = research_cyber_weather(client, date_str)

    merged = {
        "date_display": date_str,
        "market_snapshot": general.get("market_snapshot", {}),
        "weather": cyber_wx.get("weather", {}),
        "breaking_alert": general.get("breaking_alert", ""),
        "sections": {
            "world":    general.get("world", []),
            "national": general.get("national", []),
            "finance":  general.get("finance", []),
            "cyber":    cyber_wx.get("cyber", []),
        }
    }

    total = sum(len(v) for v in merged["sections"].values())
    print(f"[1/3] Research complete. {total} stories.")
    return merged

# ── Generate HTML ──────────────────────────────────────────────────────────────

def generate_html(client, news_data, date_display, date_slug):
    print("[2/3] Generating HTML...")

    prompt = f"""Generate a complete daily intelligence briefing HTML page from this JSON:

{json.dumps(news_data, indent=2)}

DESIGN:
- Dark: bg #0d0f12, surface #13161b, border #252a33, gold #c8a96e, blue #4e9af1, red #e05555, green #4caf79
- Fonts: IBM Plex Mono (labels), Playfair Display (headlines), IBM Plex Sans (body) via Google Fonts
- Sticky ticker: DOW · S&P · NASDAQ · WTI · Gas
- Red alert banner if breaking_alert non-empty
- 2-col layout: main + 340px sidebar (weather, markets, quick links)
- Story cards: severity badge (critical=red, high=amber, watch=blue), Playfair headline, summary, source link
- Mobile responsive <900px · fade-in animations

SECTIONS: 🌍 World · 🇺🇸 National · 💰 Finance · 🔐 Cybersecurity

SIDEBAR QUICK LINKS:
BleepingComputer https://www.bleepingcomputer.com/
The Hacker News https://thehackernews.com/
CISA Advisories https://www.cisa.gov/news-events/cybersecurity-advisories
Yahoo Finance https://finance.yahoo.com/
Spring TX Weather https://www.foxweather.com/local-weather/texas/spring
NPR https://www.npr.org/sections/news

Footer archive link → ./archive/

Start with <!DOCTYPE html>. No markdown fences. Output HTML only."""

    msg = with_retry(lambda: client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    ))

    html = "".join(getattr(b, "text", "") for b in msg.content).strip()
    if html.startswith("```"):
        html = re.sub(r'^```[a-z]*\n', '', html)
        html = re.sub(r'\n```$', '', html.strip())

    print(f"[2/3] HTML generated ({len(html):,} chars).")
    return html

# ── Save output ────────────────────────────────────────────────────────────────

def save_output(html, date_slug):
    print("[3/3] Saving...")
    out = Path("output")
    arc = out / "archive"
    out.mkdir(exist_ok=True)
    arc.mkdir(exist_ok=True)

    (out / "index.html").write_text(html, encoding="utf-8")
    (arc / f"{date_slug}.html").write_text(html, encoding="utf-8")
    write_archive_index(arc)
    print(f"[3/3] Saved output/index.html + output/archive/{date_slug}.html")

def write_archive_index(arc):
    files = sorted(arc.glob("????-??-??.html"), reverse=True)
    items = ""
    for f in files:
        try:
            label = datetime.strptime(f.stem, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
        except ValueError:
            label = f.stem
        items += f'<li><a href="{f.name}">{label}</a></li>\n'

    (arc / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Briefing Archive</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400&display=swap" rel="stylesheet">
<style>
body{{background:#0d0f12;color:#d4d8df;font-family:'IBM Plex Sans',sans-serif;padding:40px}}
h1{{font-family:'IBM Plex Mono',monospace;color:#c8a96e;font-size:14px;letter-spacing:.2em;text-transform:uppercase;margin-bottom:30px}}
ul{{list-style:none}}li{{padding:12px 0;border-bottom:1px solid #252a33}}
a{{color:#4e9af1;text-decoration:none;font-family:'IBM Plex Mono',monospace;font-size:13px}}
a:hover{{color:#c8a96e}}.back{{display:block;margin-bottom:30px;color:#636b78;font-size:11px}}
</style></head><body>
<a href="../" class="back">← Back to today's briefing</a>
<h1>// Briefing Archive</h1>
<ul>{items}</ul>
</body></html>""", encoding="utf-8")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    date_display, date_slug = get_cst_date()

    print("=== Daily Briefing Generator ===")
    print(f"Date: {date_display}\n")

    news_data = research_news(client, date_display)
    html      = generate_html(client, news_data, date_display, date_slug)
    save_output(html, date_slug)

    print(f"\n✅ Done: {date_display}")

if __name__ == "__main__":
    main()
