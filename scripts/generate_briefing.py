#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Briefing Generator
- Haiku for all research (web search -> JSON)
- Zero LLM calls for HTML -- pure Python template renderer
- ~$0.02-0.03/day for 3 editions
"""

import anthropic
import os
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html import escape

# Models
MODEL_RESEARCH = "claude-haiku-4-5"

# Date / edition helpers
def get_cst_now():
    cst = timezone(timedelta(hours=-6))
    now = datetime.now(cst)
    date_display = now.strftime("%A, %B %-d, %Y")
    date_slug    = now.strftime("%Y-%m-%d")
    time_str     = now.strftime("%I:%M %p")
    hour         = now.hour
    if hour < 12:
        edition_label, edition_slug = "Morning", "morning"
    elif hour < 18:
        edition_label, edition_slug = "Midday",  "midday"
    else:
        edition_label, edition_slug = "Evening", "evening"
    return date_display, date_slug, time_str, edition_label, edition_slug

# JSON extraction
def extract_json(text):
    if not text or not text.strip():
        raise ValueError("Empty response -- no JSON to extract")
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    s, e = text.find('{'), text.rfind('}')
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e+1])
        except json.JSONDecodeError:
            pass
    return json.loads(text.strip())

# Retry helper
def with_retry(fn, max_retries=4, base_delay=65):
    for attempt in range(max_retries):
        try:
            return fn()
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print("    Rate limit. Waiting {}s (attempt {}/{})...".format(delay, attempt+2, max_retries))
            time.sleep(delay)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                if attempt == max_retries - 1:
                    raise
                delay = base_delay * (2 ** attempt)
                print("    Overloaded. Waiting {}s...".format(delay))
                time.sleep(delay)
            else:
                raise

# Agentic loop for multi-turn tool use
def run_with_tools(client, prompt, tools, max_tokens=2500, max_turns=6):
    messages = [{"role": "user", "content": prompt}]
    for turn in range(max_turns):
        response = with_retry(lambda: client.messages.create(
            model=MODEL_RESEARCH,
            max_tokens=max_tokens,
            tools=tools,
            messages=messages
        ))
        text_parts = [b.text for b in response.content if b.type == "text"]
        if response.stop_reason == "end_turn":
            final = "".join(text_parts)
            if final.strip():
                return final
            raise ValueError("end_turn with no text on turn {}".format(turn+1))
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            continue
        final = "".join(text_parts)
        if final.strip():
            return final
        raise ValueError("stop_reason={} with no text".format(response.stop_reason))
    raise ValueError("Exceeded {} turns without a final response".format(max_turns))

# Research: World / National / Finance
def research_general(client, date_str):
    print("  [1a] Searching world, national, finance (Haiku)...")
    prompt = (
        "Today is {}. Search for top news.\n\n".format(date_str) +
        "Find 3 stories each: WORLD NEWS, US NATIONAL NEWS, FINANCE & MARKETS.\n"
        "Get current: DOW, S&P 500, NASDAQ, WTI crude, national avg gas price.\n\n"
        "CRITICAL: Respond with RAW JSON only. No markdown. No preamble. Start with {\n\n"
        '{"market_snapshot":{'
        '"dow":{"value":"47,250","change":"+120 (+0.25%)","direction":"up"},'
        '"sp500":{"value":"6,800","change":"+15 (+0.22%)","direction":"up"},'
        '"nasdaq":{"value":"22,100","change":"+45 (+0.20%)","direction":"up"},'
        '"wti":{"value":"$88.50","change":"-1.20 (-1.3%)","direction":"down"},'
        '"gas_avg":{"value":"$3.89","change":"+0.05"}},'
        '"breaking_alert":"",'
        '"world":[{"headline":"...","summary":"...","source":"...","url":"...","severity":"normal"}],'
        '"national":[{"headline":"...","summary":"...","source":"...","url":"...","severity":"normal"}],'
        '"finance":[{"headline":"...","summary":"...","source":"...","url":"...","severity":"normal"}]}'
    )
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    return extract_json(run_with_tools(client, prompt, tools))

# Research: Cyber + Weather
def research_cyber_weather(client, date_str):
    print("  [1b] Searching cybersecurity + weather (Haiku)...")
    prompt = (
        "Today is {}. Search for two things:\n\n".format(date_str) +
        "1. TOP 4 CYBERSECURITY stories: threats, CVEs, CISA advisories, "
        "nation-state, ransomware, breaches.\n"
        "2. WEATHER Spring TX 77379: high, low, condition, rain chance, wind, NWS alerts.\n\n"
        "CRITICAL: Respond with RAW JSON only. No markdown. No preamble. Start with {\n\n"
        '{"weather":{"high":"84F","low":"68F","condition":"Partly Cloudy",'
        '"rain_chance":"20%","alerts":"None","wind":"S 10 mph"},'
        '"cyber":[{"headline":"...","summary":"...","source":"...","url":"...","severity":"critical"}]}'
    )
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    return extract_json(run_with_tools(client, prompt, tools))

# Merge research results
def research_news(client, date_str):
    print("[1/2] Researching {}...".format(date_str))
    general  = research_general(client, date_str)
    print("  Pausing 20s...")
    time.sleep(20)
    cyber_wx = research_cyber_weather(client, date_str)
    merged = {
        "date_display":    date_str,
        "market_snapshot": general.get("market_snapshot", {}),
        "weather":         cyber_wx.get("weather", {}),
        "breaking_alert":  general.get("breaking_alert", ""),
        "sections": {
            "world":    general.get("world",    []),
            "national": general.get("national", []),
            "finance":  general.get("finance",  []),
            "cyber":    cyber_wx.get("cyber",   []),
        }
    }
    total = sum(len(v) for v in merged["sections"].values())
    print("[1/2] Done. {} stories.".format(total))
    return merged

# HTML renderer constants
SEVERITY_CLASS = {
    "critical": "badge-critical",
    "high":     "badge-high",
    "watch":    "badge-watch",
    "normal":   "badge-normal",
}

SECTION_META = {
    "world":    ("&#x1F30D;", "World News"),
    "national": ("&#x1F1FA;&#x1F1F8;", "National News"),
    "finance":  ("&#x1F4B0;", "Finance &amp; Markets"),
    "cyber":    ("&#x1F510;", "Cybersecurity"),
}

WEATHER_ICONS = [
    ("storm",   "&#x26C8;"),
    ("thunder", "&#x26C8;"),
    ("rain",    "&#x1F327;"),
    ("snow",    "&#x2744;"),
    ("fog",     "&#x1F32B;"),
    ("cloud",   "&#x2601;"),
    ("partly",  "&#x26C5;"),
    ("clear",   "&#x2600;"),
    ("sun",     "&#x2600;"),
]

def weather_icon(condition):
    c = condition.lower()
    for k, v in WEATHER_ICONS:
        if k in c:
            return v
    return "&#x1F324;"

def dir_cls(d):
    return "up" if str(d).lower() == "up" else "down"

def dir_arr(d):
    return "&#x25B2;" if str(d).lower() == "up" else "&#x25BC;"

def render_ticker(mkt):
    rows = [
        ("DOW",    mkt.get("dow",     {})),
        ("S&amp;P",mkt.get("sp500",   {})),
        ("NASDAQ", mkt.get("nasdaq",  {})),
        ("WTI",    mkt.get("wti",     {})),
        ("Gas",    mkt.get("gas_avg", {})),
    ]
    parts = []
    for name, m in rows:
        val = escape(str(m.get("value",  "--")))
        chg = escape(str(m.get("change", "")))
        dc  = dir_cls(m.get("direction", "up"))
        arr = dir_arr(m.get("direction", "up"))
        parts.append(
            '<div class="ticker-item">'
            '<span class="ticker-name">{}</span>'
            '<span class="ticker-val">{}</span>'
            '<span class="ticker-chg {}">{} {}</span>'
            '</div>'.format(name, val, dc, arr, chg)
        )
    return "\n      ".join(parts)

def render_stories(stories):
    out = []
    for s in stories:
        sev   = s.get("severity", "normal").lower()
        cls   = SEVERITY_CLASS.get(sev, "badge-normal")
        label = sev.capitalize()
        hl    = escape(s.get("headline", ""))
        summ  = escape(s.get("summary",  ""))
        src   = escape(s.get("source",   ""))
        url   = escape(s.get("url",      "#"))
        out.append(
            '<div class="story">'
            '<div class="story-meta"><span class="badge {}">{}</span></div>'
            '<div class="story-headline">{}</div>'
            '<div class="story-summary">{}</div>'
            '<a href="{}" class="story-source" target="_blank" rel="noopener">&rarr; {}</a>'
            '</div>'.format(cls, label, hl, summ, url, src)
        )
    return "\n      ".join(out)

def render_sections(sections):
    out = []
    for key in ("world", "national", "finance", "cyber"):
        stories = sections.get(key, [])
        icon, title = SECTION_META[key]
        count = len(stories)
        noun  = "story" if count == 1 else "stories"
        out.append(
            '<div class="section">'
            '<div class="section-header">'
            '<span class="section-icon">{}</span>'
            '<span class="section-title">{}</span>'
            '<span class="section-count">{} {}</span>'
            '</div>'
            '{}'
            '</div>'.format(icon, title, count, noun, render_stories(stories))
        )
    return "\n    ".join(out)

def render_weather(wx):
    high  = escape(wx.get("high",       "--"))
    low   = escape(wx.get("low",        "--"))
    cond  = escape(wx.get("condition",  "--"))
    rain  = escape(wx.get("rain_chance","--"))
    wind  = escape(wx.get("wind",       "--"))
    alert = escape(wx.get("alerts",     "None"))
    icon  = weather_icon(cond)
    alert_html = ""
    if alert and alert.lower() not in ("none", "no alerts", ""):
        alert_html = '<div class="weather-alert">&#x26A0; {}</div>'.format(alert)
    return (
        '<div class="weather-main">'
        '<div><div class="weather-temp">{}</div><div class="weather-condition">{}</div></div>'
        '<div class="weather-icon">{}</div>'
        '</div>'
        '<div class="weather-row"><span class="weather-label">Low</span><span class="weather-val">{}</span></div>'
        '<div class="weather-row"><span class="weather-label">Rain</span><span class="weather-val">{}</span></div>'
        '<div class="weather-row"><span class="weather-label">Wind</span><span class="weather-val">{}</span></div>'
        '{}'
    ).format(high, cond, icon, low, rain, wind, alert_html)

def render_markets(mkt):
    rows = [
        ("DOW",     mkt.get("dow",     {})),
        ("S&amp;P 500", mkt.get("sp500",   {})),
        ("NASDAQ",  mkt.get("nasdaq",  {})),
        ("WTI",     mkt.get("wti",     {})),
        ("Gas Avg", mkt.get("gas_avg", {})),
    ]
    out = []
    for name, m in rows:
        val = escape(str(m.get("value",  "--")))
        chg = escape(str(m.get("change", "")))
        dc  = dir_cls(m.get("direction", "up"))
        arr = dir_arr(m.get("direction", "up"))
        out.append(
            '<div class="market-row">'
            '<span class="market-name">{}</span>'
            '<span class="market-val {}">{}</span>'
            '<span class="market-chg {}">{} {}</span>'
            '</div>'.format(name, dc, val, dc, arr, chg)
        )
    return "\n      ".join(out)

QUICK_LINKS = [
    ("BleepingComputer",  "https://www.bleepingcomputer.com/"),
    ("The Hacker News",   "https://thehackernews.com/"),
    ("CISA Advisories",   "https://www.cisa.gov/news-events/cybersecurity-advisories"),
    ("Yahoo Finance",     "https://finance.yahoo.com/"),
    ("Spring TX Weather", "https://www.foxweather.com/local-weather/texas/spring"),
    ("NPR News",          "https://www.npr.org/sections/news"),
]

def render_quick_links():
    return "\n      ".join(
        '<a href="{}" class="quick-link" target="_blank" rel="noopener">{}</a>'.format(url, escape(name))
        for name, url in QUICK_LINKS
    )

CSS = (
    ":root{"
    "--bg:#0d0f12;--surface:#13161b;--surface2:#1a1e25;--border:#252a33;"
    "--text:#c8cdd6;--muted:#636b78;--gold:#c8a96e;--blue:#4e9af1;"
    "--red:#e05555;--amber:#e09955;--green:#4caf79;--cyan:#4ecfcf;"
    "}"
    "*{box-sizing:border-box;margin:0;padding:0}"
    "body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}"
    ".ticker-bar{background:#080a0d;border-bottom:1px solid var(--border);overflow:hidden;position:sticky;top:0;z-index:100}"
    ".ticker-inner{display:flex;align-items:center;height:38px}"
    ".ticker-label{background:var(--gold);color:#000;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:.12em;padding:0 14px;height:100%;display:flex;align-items:center;flex-shrink:0;text-transform:uppercase}"
    ".ticker-items{display:flex;gap:32px;padding:0 24px;overflow-x:auto;scrollbar-width:none}"
    ".ticker-items::-webkit-scrollbar{display:none}"
    ".ticker-item{display:flex;align-items:center;gap:8px;white-space:nowrap}"
    ".ticker-name{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}"
    ".ticker-val{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;color:var(--text)}"
    ".ticker-chg{font-family:'IBM Plex Mono',monospace;font-size:11px}"
    ".up{color:var(--green)}.down{color:var(--red)}"
    ".alert-banner{background:#1a0e0e;border-bottom:2px solid var(--red);padding:10px 24px;display:flex;align-items:center;gap:12px}"
    ".alert-tag{background:var(--red);color:#fff;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:.15em;padding:3px 10px;border-radius:2px;flex-shrink:0}"
    ".alert-text{font-size:13px;color:#f08080}"
    ".main-header{padding:36px 32px 28px;border-bottom:1px solid var(--border)}"
    ".header-label{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--gold);letter-spacing:.25em;text-transform:uppercase;margin-bottom:10px}"
    ".header-title{font-family:'Playfair Display',serif;font-size:42px;font-weight:900;color:#fff;line-height:1.1}"
    ".header-meta{display:flex;align-items:center;gap:12px;margin-top:10px;flex-wrap:wrap}"
    ".header-date{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:.08em}"
    ".edition-badge{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;padding:3px 10px;border-radius:2px}"
    ".edition-morning{background:rgba(200,169,110,.15);color:var(--gold);border:1px solid rgba(200,169,110,.3)}"
    ".edition-midday{background:rgba(78,154,241,.12);color:var(--blue);border:1px solid rgba(78,154,241,.25)}"
    ".edition-evening{background:rgba(78,207,207,.1);color:var(--cyan);border:1px solid rgba(78,207,207,.25)}"
    ".layout{display:grid;grid-template-columns:1fr 340px;gap:0;max-width:1400px}"
    ".main-col{padding:28px 32px;border-right:1px solid var(--border)}"
    ".sidebar{padding:28px 24px}"
    ".section{margin-bottom:40px}"
    ".section-header{display:flex;align-items:center;gap:10px;margin-bottom:18px;padding-bottom:12px;border-bottom:1px solid var(--border)}"
    ".section-icon{font-size:18px}"
    ".section-title{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;color:var(--gold)}"
    ".section-count{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);margin-left:auto}"
    ".story{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:16px 18px;margin-bottom:12px;animation:fadeIn .4s ease;transition:border-color .2s}"
    ".story:hover{border-color:#3a4050}"
    "@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}"
    ".story-meta{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}"
    ".badge{font-family:'IBM Plex Mono',monospace;font-size:9px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;padding:2px 8px;border-radius:2px}"
    ".badge-critical{background:rgba(224,85,85,.18);color:var(--red);border:1px solid rgba(224,85,85,.35)}"
    ".badge-high{background:rgba(224,153,85,.15);color:var(--amber);border:1px solid rgba(224,153,85,.3)}"
    ".badge-watch{background:rgba(78,154,241,.12);color:var(--blue);border:1px solid rgba(78,154,241,.25)}"
    ".badge-normal{background:rgba(100,110,125,.12);color:var(--muted);border:1px solid var(--border)}"
    ".story-headline{font-family:'Playfair Display',serif;font-size:17px;font-weight:700;color:#e8ecf2;line-height:1.3;margin-bottom:8px}"
    ".story-summary{font-size:13px;color:var(--text);line-height:1.65}"
    ".story-source{display:inline-block;margin-top:10px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--blue);text-decoration:none;letter-spacing:.05em}"
    ".story-source:hover{color:var(--gold)}"
    ".sidebar-card{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:16px;margin-bottom:20px}"
    ".sidebar-card-title{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)}"
    ".weather-main{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px}"
    ".weather-temp{font-family:'Playfair Display',serif;font-size:48px;font-weight:700;color:#fff;line-height:1}"
    ".weather-condition{font-size:13px;color:var(--muted);margin-top:4px}"
    ".weather-icon{font-size:40px}"
    ".weather-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}"
    ".weather-row:last-of-type{border-bottom:none}"
    ".weather-label{color:var(--muted);font-family:'IBM Plex Mono',monospace;font-size:10px}"
    ".weather-val{color:var(--text);font-family:'IBM Plex Mono',monospace;font-size:11px}"
    ".weather-alert{background:rgba(224,153,85,.12);border:1px solid rgba(224,153,85,.3);border-radius:4px;padding:8px 10px;margin-top:10px;font-size:11px;color:#f0c070}"
    ".market-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}"
    ".market-row:last-child{border-bottom:none}"
    ".market-name{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted)}"
    ".market-val{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600}"
    ".market-chg{font-family:'IBM Plex Mono',monospace;font-size:10px}"
    ".quick-link{display:block;padding:8px 0;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--blue);text-decoration:none;transition:color .15s}"
    ".quick-link:last-child{border-bottom:none}"
    ".quick-link:hover{color:var(--gold)}"
    "footer{padding:24px 32px;border-top:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}"
    ".archive-link{color:var(--blue);text-decoration:none}"
    ".archive-link:hover{color:var(--gold)}"
    "@media(max-width:900px){"
    ".layout{grid-template-columns:1fr}"
    ".main-col{border-right:none;padding:20px 16px}"
    ".sidebar{padding:20px 16px;border-top:1px solid var(--border)}"
    ".header-title{font-size:28px}"
    ".main-header{padding:24px 16px}"
    "footer{padding:16px}"
    "}"
)

GF_URL = (
    "https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900"
    "&family=IBM+Plex+Mono:wght@400;500;600"
    "&family=IBM+Plex+Sans:wght@300;400;500&display=swap"
)

def build_html(data, date_display, time_str, edition_label, edition_slug):
    mkt      = data.get("market_snapshot", {})
    wx       = data.get("weather", {})
    sections = data.get("sections", {})
    alert    = data.get("breaking_alert", "")

    alert_html = ""
    if alert and alert.strip():
        alert_html = (
            '<div class="alert-banner">'
            '<span class="alert-tag">&#x26A1; Breaking</span>'
            '<span class="alert-text">{}</span>'
            '</div>'.format(escape(alert))
        )

    ed_cls = "edition-{}".format(edition_slug)

    parts = [
        '<!DOCTYPE html>',
        '<html lang="en">',
        '<head>',
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '<title>Daily Briefing &mdash; {} &mdash; {}</title>'.format(escape(date_display), edition_label),
        '<link href="{}" rel="stylesheet">'.format(GF_URL),
        '<style>{}</style>'.format(CSS),
        '</head>',
        '<body>',
        '<div class="ticker-bar"><div class="ticker-inner">',
        '<div class="ticker-label">Markets</div>',
        '<div class="ticker-items">{}</div>'.format(render_ticker(mkt)),
        '</div></div>',
        alert_html,
        '<div class="main-header">',
        '<div class="header-label">// Daily Intelligence Briefing</div>',
        '<div class="header-title">{}</div>'.format(escape(date_display)),
        '<div class="header-meta">',
        '<span class="header-date">Spring, TX &nbsp;|&nbsp; {} CDT</span>'.format(escape(time_str)),
        '<span class="edition-badge {}">{} Edition</span>'.format(ed_cls, edition_label),
        '</div></div>',
        '<div class="layout">',
        '<div class="main-col">{}</div>'.format(render_sections(sections)),
        '<div class="sidebar">',
        '<div class="sidebar-card">',
        '<div class="sidebar-card-title">// Weather &middot; Spring TX</div>',
        render_weather(wx),
        '</div>',
        '<div class="sidebar-card">',
        '<div class="sidebar-card-title">// Markets</div>',
        render_markets(mkt),
        '</div>',
        '<div class="sidebar-card">',
        '<div class="sidebar-card-title">// Quick Links</div>',
        render_quick_links(),
        '</div>',
        '</div>',  # sidebar
        '</div>',  # layout
        '<footer>',
        '<span>Generated {} at {} CDT</span>'.format(escape(date_display), escape(time_str)),
        '<a href="./archive/" class="archive-link">&larr; Previous briefings</a>',
        '</footer>',
        '</body>',
        '</html>',
    ]
    return "\n".join(parts)

# Save output
def save_output(html, date_slug, edition_slug):
    print("[2/2] Saving...")
    out = Path("output")
    arc = out / "archive"
    out.mkdir(exist_ok=True)
    arc.mkdir(exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "{}.html".format(edition_slug)).write_text(html, encoding="utf-8")
    archive_name = "{}-{}.html".format(date_slug, edition_slug)
    (arc / archive_name).write_text(html, encoding="utf-8")
    write_archive_index(arc)
    print("[2/2] Saved output/index.html + output/{}.html + output/archive/{}".format(
        edition_slug, archive_name))

def write_archive_index(arc):
    files = sorted(arc.glob("????-??-??-*.html"), reverse=True)
    items = ""
    for f in files:
        parts = f.stem.rsplit('-', 1)
        date_part    = parts[0] if len(parts) == 2 else f.stem
        edition_part = parts[1].capitalize() if len(parts) == 2 else ""
        try:
            label = datetime.strptime(date_part, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
        except ValueError:
            label = date_part
        display = "{} &mdash; {}".format(label, edition_part) if edition_part else label
        items += '<li><a href="{}">{}</a></li>\n'.format(f.name, display)

    gf2 = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500"
           "&family=IBM+Plex+Sans:wght@300;400&display=swap")
    arc_css = (
        "body{background:#0d0f12;color:#d4d8df;font-family:'IBM Plex Sans',sans-serif;padding:40px}"
        "h1{font-family:'IBM Plex Mono',monospace;color:#c8a96e;font-size:14px;"
        "letter-spacing:.2em;text-transform:uppercase;margin-bottom:30px}"
        "ul{list-style:none}li{padding:12px 0;border-bottom:1px solid #252a33}"
        "a{color:#4e9af1;text-decoration:none;font-family:'IBM Plex Mono',monospace;font-size:13px}"
        "a:hover{color:#c8a96e}"
        ".back{display:block;margin-bottom:30px;color:#636b78;font-size:11px;text-decoration:none}"
    )
    html = (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        '<title>Briefing Archive</title>\n'
        '<link href="' + gf2 + '" rel="stylesheet">\n'
        '<style>' + arc_css + '</style></head><body>\n'
        '<a href="../" class="back">&larr; Back to today\'s briefing</a>\n'
        '<h1>// Briefing Archive</h1>\n'
        '<ul>' + items + '</ul>\n'
        '</body></html>'
    )
    (arc / "index.html").write_text(html, encoding="utf-8")

# Main
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    date_display, date_slug, time_str, edition_label, edition_slug = get_cst_now()

    print("=== Daily Briefing Generator ===")
    print("Date:    {}".format(date_display))
    print("Time:    {}".format(time_str))
    print("Edition: {}".format(edition_label))
    print("Model:   {} (research only -- HTML is templated)\n".format(MODEL_RESEARCH))

    data = research_news(client, date_display)
    html = build_html(data, date_display, time_str, edition_label, edition_slug)
    print("HTML rendered ({:,} chars) -- no LLM call.".format(len(html)))
    save_output(html, date_slug, edition_slug)
    print("\nDone: {} Edition -- {}".format(edition_label, date_display))

if __name__ == "__main__":
    main()
