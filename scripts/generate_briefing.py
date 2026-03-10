#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily Briefing Generator
- 16 RSS feeds (BBC, Al Jazeera, Reuters, NPR, PBS, AP, MarketWatch,
  Bleeping Computer, Hacker News, CISA, Krebs) -- free, no tokens
- NWS REST API for weather -- free, no tokens
- Article fetch + HTML parse for full story text -- free, no tokens
- Haiku call 1: select stories + assign severity (explicit rules)
- Haiku call 2: write 2-sentence summaries from article text
- Zero LLM calls for HTML -- pure Python template renderer
- Target cost: ~$0.01-0.02/run
"""

import anthropic
import os
import json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html import escape

# Models
MODEL_SEARCH = "claude-haiku-4-5"   # web search calls
MODEL_FORMAT = "claude-haiku-4-5"   # JSON formatting
MODEL_RESEARCH = MODEL_SEARCH        # legacy alias

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

# ── RSS FEEDS ────────────────────────────────────────────────────────────────
# All free, no paywall, bot-permissive. 4 sources per section.
RSS_FEEDS = {
    "world": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",          # BBC World
        "https://www.aljazeera.com/xml/rss/all.xml",             # Al Jazeera
        "https://feeds.reuters.com/reuters/worldNews",           # Reuters World
        "https://feeds.npr.org/1004/rss.xml",                    # NPR World
    ],
    "national": [
        "https://feeds.npr.org/1001/rss.xml",                    # NPR News
        "https://www.pbs.org/newshour/feeds/rss/headlines",      # PBS NewsHour
        "https://feeds.npr.org/1003/rss.xml",                    # NPR Politics
        "https://rss.csmonitor.com/feeds/all",                   # Christian Science Monitor
    ],
    "finance": [
        "https://feeds.reuters.com/reuters/businessNews",        # Reuters Business
        "https://feeds.marketwatch.com/marketwatch/topstories",  # MarketWatch
        "https://feeds.npr.org/1017/rss.xml",                    # NPR Economy
        "https://feeds.reuters.com/reuters/companyNews",         # Reuters Companies
    ],
    "cyber": [
        "https://feeds.feedburner.com/TheHackersNews",           # The Hacker News
        "https://www.bleepingcomputer.com/feed/",                # Bleeping Computer
        "https://www.cisa.gov/cybersecurity-advisories/all.xml", # CISA Advisories
        "https://krebsonsecurity.com/feed/",                     # Krebs on Security
    ],
}

# ── RSS FETCH ─────────────────────────────────────────────────────────────────
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

def fetch_rss(url, max_items=5):
    """Fetch an RSS/Atom feed. Returns list of {title, summary, url, source}."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "daily-briefing/1.0 (personal news aggregator)"})
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        is_atom = root.tag.endswith("feed") or "Atom" in root.tag
        items = []

        if is_atom:
            for entry in root.findall("atom:entry", ns)[:max_items]:
                title = (entry.findtext("atom:title", "", ns) or "").strip()
                summ  = (entry.findtext("atom:summary", "", ns) or
                         entry.findtext("atom:content", "", ns) or "").strip()
                summ  = re.sub(r'<[^>]+>', '', summ)[:400]
                link  = entry.find("atom:link", ns)
                href  = (link.get("href", "") if link is not None else "")
                src   = url.split("/")[2].replace("www.", "").replace("feeds.", "")
                items.append({"title": title, "summary": summ, "url": href, "source": src})
        else:
            channel = root.find("channel") or root
            src     = (channel.findtext("title") or url.split("/")[2]).strip()
            for item in channel.findall("item")[:max_items]:
                title = (item.findtext("title") or "").strip()
                summ  = (item.findtext("description") or "").strip()
                summ  = re.sub(r'<[^>]+>', '', summ)[:400]
                link  = (item.findtext("link") or "").strip()
                items.append({"title": title, "summary": summ, "url": link, "source": src})
        return items
    except Exception as e:
        print("    RSS skip {}: {}".format(url.split("/")[2], e))
        return []

def fetch_all_rss():
    """Fetch all feeds. Returns dict section -> deduplicated item list."""
    results = {}
    for section, urls in RSS_FEEDS.items():
        items = []
        for url in urls:
            items.extend(fetch_rss(url, max_items=5))
        seen, deduped = set(), []
        for item in items:
            key = item["title"][:50].lower().strip()
            if key and key not in seen:
                seen.add(key)
                deduped.append(item)
        results[section] = deduped[:10]  # top 10 per section for LLM to choose from
        print("  [RSS] {}: {} items from {} feeds".format(
            section, len(deduped), len(urls)))
    return results

# ── ARTICLE FETCH ─────────────────────────────────────────────────────────────
class _ArticleParser(HTMLParser):
    """Minimal HTML parser that extracts visible paragraph text."""
    SKIP_TAGS = {"script","style","nav","header","footer","aside",
                 "noscript","figure","figcaption","form","button","iframe"}

    def __init__(self):
        super().__init__()
        self._skip  = 0
        self._buf   = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            t = data.strip()
            if len(t) > 40:          # skip short nav labels etc.
                self._buf.append(t)

    def get_text(self):
        return " ".join(self._buf)

def fetch_article_text(url, char_limit=2000):
    """
    Fetch a news article URL and return plain text (up to char_limit chars).
    Returns None on failure so caller can fall back to RSS description.
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; daily-briefing/1.0)",
            "Accept":     "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            # Only read if content-type is HTML
            ct = r.headers.get("Content-Type", "")
            if "html" not in ct.lower():
                return None
            raw = r.read(65536).decode("utf-8", errors="replace")
        parser = _ArticleParser()
        parser.feed(raw)
        text = parser.get_text()
        return text[:char_limit] if text else None
    except Exception:
        return None

def enrich_with_articles(stories):
    """
    For each story, attempt to fetch the article and attach body text.
    Falls back to RSS summary on failure. Modifies list in place.
    """
    for s in stories:
        url = s.get("url", "")
        if not url or url == "#":
            continue
        text = fetch_article_text(url)
        if text and len(text) > 100:
            s["article_text"] = text
        # else: article_text absent, summarize step uses RSS summary

# ── NWS WEATHER ───────────────────────────────────────────────────────────────
def fetch_nws_weather():
    """Free NWS weather for Spring TX (30.0799, -95.4172). No API key."""
    try:
        hdrs = {"User-Agent": "daily-briefing/1.0 (personal use)"}
        with urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.weather.gov/points/30.0799,-95.4172", headers=hdrs),
                timeout=10) as r:
            point = json.loads(r.read())

        with urllib.request.urlopen(
                urllib.request.Request(
                    point["properties"]["forecast"], headers=hdrs),
                timeout=10) as r:
            forecast = json.loads(r.read())

        periods = forecast["properties"]["periods"]
        today   = [p for p in periods if p.get("isDaytime", True)]
        tonight = [p for p in periods if not p.get("isDaytime", True)]
        day     = today[0]   if today   else periods[0]
        ngt     = tonight[0] if tonight else (periods[1] if len(periods) > 1 else {})

        prob     = day.get("probabilityOfPrecipitation", {})
        rain_pct = prob.get("value") if prob else None
        rain_str = "{}%".format(rain_pct) if rain_pct is not None else "N/A"
        wind     = "{} {}".format(
            day.get("windDirection",""), day.get("windSpeed","")).strip()

        with urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.weather.gov/alerts/active?point=30.0799,-95.4172",
                    headers=hdrs),
                timeout=10) as r:
            alerts_data = json.loads(r.read())

        feats     = alerts_data.get("features", [])
        alert_str = (feats[0]["properties"].get("headline","Active NWS Alert")
                     if feats else "None")

        wx = {
            "high":        "{}F".format(day.get("temperature","--")),
            "low":         "{}F".format(ngt.get("temperature","--")),
            "condition":   day.get("shortForecast","--"),
            "rain_chance": rain_str,
            "wind":        wind or "--",
            "alerts":      alert_str,
        }
        print("  [NWS] {high} / {low} / {condition} / rain {rain_chance}".format(**wx))
        return wx
    except Exception as e:
        print("  [NWS] Failed ({}), using placeholder".format(e))
        return {"high":"--","low":"--","condition":"Unavailable",
                "rain_chance":"--","alerts":"None","wind":"--"}

# ── SEVERITY RULES (injected into classify prompt) ────────────────────────────
SEVERITY_RULES = """
SEVERITY ASSIGNMENT RULES -- apply strictly:

CRITICAL:
- Active armed conflict with major escalation or mass casualties
- Terrorist attack or mass casualty event
- Major US infrastructure attack (power grid, water, financial system)
- Ransomware or cyberattack confirmed hitting critical infrastructure
- Zero-day being actively exploited in the wild with no patch
- Category 3+ hurricane or EF3+ tornado threatening populated area
- Market crash (single-day index drop >4%)

HIGH:
- Significant geopolitical development (new sanctions, treaty, diplomatic crisis)
- Major election result or political crisis
- Market move >2% on a major index
- Named CVE with CVSS score 8.0 or higher
- CISA Known Exploited Vulnerabilities (KEV) catalog addition
- Confirmed data breach affecting >1 million records
- Nation-state attribution for a cyberattack
- CVE with no patch currently available
- Severe weather watch or warning for the region

WATCH:
- Ongoing developing situation worth monitoring
- Fed announcement, major earnings, economic data release
- New malware family or threat actor identified
- Indictment, arrest, or legal action of significance
- Tropical storm formation or upgrade
- Patch Tuesday or major vendor security release
- CISA advisory (non-KEV)

NORMAL:
- Routine news, analysis, policy updates
- Minor market movement
- General cybersecurity research or commentary
"""

# ── LLM CLASSIFY ─────────────────────────────────────────────────────────────
def classify_and_select(client, rss_data):
    """
    Call 1 of 2: Send RSS headlines to Haiku.
    Picks best stories, assigns severity using explicit rules, returns JSON with URLs.
    """
    sections_text = ""
    for section in ("world", "national", "finance", "cyber"):
        items = rss_data.get(section, [])
        sections_text += "\n[{}]\n".format(section.upper())
        for i, item in enumerate(items):
            sections_text += "{}. {} [{}]\n   {}\n".format(
                i+1,
                item.get("title",""),
                item.get("source",""),
                item.get("url","")
            )

    prompt = (
        "You are a senior news editor. Select the most important stories from these "
        "RSS headlines and output ONLY raw JSON -- no markdown, no explanation.\n\n"
        + SEVERITY_RULES + "\n\n"
        "HEADLINES:\n" + sections_text + "\n\n"
        "Select: 3 world, 3 national, 3 finance, 4 cyber stories.\n"
        "For each story output: headline (use original title), source (domain name), "
        "url, severity (apply rules above strictly), "
        "and rss_summary (the RSS description, 1 sentence max).\n"
        "Also set breaking_alert to a 1-sentence string if any story is CRITICAL, else \"\".\n\n"
        "JSON:\n"
        "{\"breaking_alert\":\"\","
        "\"world\":[{\"headline\":\"\",\"rss_summary\":\"\",\"source\":\"\",\"url\":\"\",\"severity\":\"\"}],"
        "\"national\":[...],"
        "\"finance\":[...],"
        "\"cyber\":[...]}"
    )

    response = with_retry(lambda: client.messages.create(
        model=MODEL_RESEARCH,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    ))
    parts = [b.text for b in response.content if hasattr(b, "text") and b.text]
    text  = "".join(parts)
    print("  [LLM-1] classify: {} chars, stop={}".format(len(text), response.stop_reason))
    return extract_json(text)

# ── LLM SUMMARIZE ─────────────────────────────────────────────────────────────
def summarize_stories(client, all_sections):
    """
    Call 2 of 2: Given selected stories with article text (or RSS fallback),
    produce a clean 2-sentence summary for each. Single batched Haiku call.
    """
    # Build input: for each story, use article_text if available, else rss_summary
    stories_input = ""
    idx = 0
    for section in ("world", "national", "finance", "cyber"):
        for s in all_sections.get(section, []):
            idx += 1
            body = s.get("article_text") or s.get("rss_summary") or s.get("summary") or ""
            stories_input += "\n[{}] {} | {}\nTEXT: {}\n".format(
                idx,
                s.get("headline",""),
                s.get("source",""),
                body[:1500]
            )

    prompt = (
        "Write a 3-4 sentence factual summary for each numbered news story below. "
        "Cover the key facts, context, and significance. Be clear and neutral. "
        "Output ONLY a JSON object mapping the story number "
        "(as string) to its summary. No markdown, no explanation.\n\n"
        "STORIES:\n" + stories_input + "\n\n"
        "JSON: {\"1\":\"summary...\",\"2\":\"summary...\", ...}"
    )

    response = with_retry(lambda: client.messages.create(
        model=MODEL_RESEARCH,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    ))
    parts = [b.text for b in response.content if hasattr(b, "text") and b.text]
    text  = "".join(parts)
    print("  [LLM-2] summarize: {} chars, stop={}".format(len(text), response.stop_reason))
    try:
        return extract_json(text)
    except Exception:
        return {}

# ── MAIN RESEARCH ORCHESTRATOR ────────────────────────────────────────────────
def research_news(client, date_str):
    print("[1/2] Researching {}...".format(date_str))

    # Step 1: free data fetches
    print("  Fetching RSS feeds...")
    rss_data = fetch_all_rss()

    print("  Fetching NWS weather...")
    weather = fetch_nws_weather()

    # Step 2: LLM call 1 -- select + classify
    print("  [LLM-1] Selecting and classifying stories...")
    classified = classify_and_select(client, rss_data)

    all_sections = {
        "world":    [x for x in classified.get("world",    []) if x and isinstance(x, dict)],
        "national": [x for x in classified.get("national", []) if x and isinstance(x, dict)],
        "finance":  [x for x in classified.get("finance",  []) if x and isinstance(x, dict)],
        "cyber":    [x for x in classified.get("cyber",    []) if x and isinstance(x, dict)],
    }

    # Step 3: fetch article bodies (free, silent fallback on failure)
    print("  Fetching article text...")
    all_stories = []
    for section in ("world", "national", "finance", "cyber"):
        all_stories.extend(all_sections[section])
    enrich_with_articles(all_stories)
    fetched = sum(1 for s in all_stories if s.get("article_text"))
    print("  {} / {} articles fetched successfully".format(fetched, len(all_stories)))

    # Step 4: LLM call 2 -- summarize with article text
    print("  [LLM-2] Writing summaries...")
    summaries = summarize_stories(client, all_sections)

    # Inject summaries back into stories
    idx = 0
    for section in ("world", "national", "finance", "cyber"):
        for s in all_sections[section]:
            idx += 1
            s["summary"] = summaries.get(str(idx), s.get("rss_summary", ""))

    merged = {
        "date_display":    date_str,
        "market_snapshot": classified.get("market_snapshot", {}),
        "weather":         weather,
        "breaking_alert":  classified.get("breaking_alert", ""),
        "sections":        all_sections,
    }
    total = sum(len(v) for v in merged["sections"].values())
    print("[1/2] Done. {} stories ({} with full article text).".format(total, fetched))
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
        val = escape(str(m.get("value")  or "--"))
        chg = escape(str(m.get("change") or ""))
        dc  = dir_cls(m.get("direction") or "up")
        arr = dir_arr(m.get("direction") or "up")
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
    for s in [x for x in (stories or []) if x and isinstance(x, dict)]:
        sev   = (s.get("severity") or "normal").lower()
        cls   = SEVERITY_CLASS.get(sev, "badge-normal")
        label = sev.capitalize()
        hl    = escape(str(s.get("headline") or ""))
        summ  = escape(str(s.get("summary")  or ""))
        src   = escape(str(s.get("source")   or ""))
        url   = escape(str(s.get("url")      or "#"))
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
    high  = escape(str(wx.get("high")       or "--"))
    low   = escape(str(wx.get("low")        or "--"))
    cond  = escape(str(wx.get("condition")  or "--"))
    rain  = escape(str(wx.get("rain_chance")or "--"))
    wind  = escape(str(wx.get("wind")       or "--"))
    alert = escape(str(wx.get("alerts")     or "None"))
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
        val = escape(str(m.get("value")  or "--"))
        chg = escape(str(m.get("change") or ""))
        dc  = dir_cls(m.get("direction") or "up")
        arr = dir_arr(m.get("direction") or "up")
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
    print("Model:   {} (classify only -- RSS feeds + NWS weather)\n".format(MODEL_RESEARCH))

    data = research_news(client, date_display)
    html = build_html(data, date_display, time_str, edition_label, edition_slug)
    print("HTML rendered ({:,} chars) -- no LLM call.".format(len(html)))
    save_output(html, date_slug, edition_slug)
    print("\nDone: {} Edition -- {}".format(edition_label, date_display))

if __name__ == "__main__":
    main()
