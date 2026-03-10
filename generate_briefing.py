#!/usr/bin/env python3
"""
Daily Briefing Generator
Calls Claude to research and generate a full HTML briefing page.
Outputs to ./output/index.html and ./output/archive/YYYY-MM-DD.html
"""

import anthropic
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

LOCATION = "Spring, Texas"
TIMEZONE_NAME = "CST/CDT"
OWNER_CONTEXT = """
The reader is a senior penetration tester with 25+ years in cybersecurity, 
who also co-owns a high-end architectural design firm in Spring, TX.
Prioritize: cyber/infosec news, national security, financial markets, 
local Houston/Spring TX weather, and top world/national headlines.
Be direct, technical where appropriate, and don't over-explain basics.
"""

# ── Date helpers ───────────────────────────────────────────────────────────────

def get_cst_date():
    """Return current date in CST/CDT."""
    cst = timezone(timedelta(hours=-6))
    now = datetime.now(cst)
    return now.strftime("%A, %B %-d, %Y"), now.strftime("%Y-%m-%d")

# ── Claude API calls ───────────────────────────────────────────────────────────

def research_news(client, date_str):
    """Ask Claude to research today's news using web search."""
    print(f"[1/3] Researching news for {date_str}...")
    
    research_prompt = f"""
Today is {date_str}. You are researching news for a daily briefing for a 
cybersecurity professional in Spring, Texas.

Please search the web and gather the TOP stories in each of these categories:

1. WORLD NEWS (3-4 stories) - Major international events, wars, geopolitics
2. NATIONAL NEWS (3 stories) - Top US domestic news, politics, policy  
3. FINANCE & MARKETS (3-4 stories) - Markets, economy, oil prices, key indicators
4. CYBERSECURITY (4-5 stories) - Active threats, breaches, CVEs, CISA advisories, 
   nation-state activity, ransomware, notable vulnerabilities
5. LOCAL - Spring/Houston TX weather forecast for today with temps, 
   precipitation chance, any severe weather alerts

For each story provide:
- Headline
- 2-3 sentence summary
- Source name
- URL

Be specific with numbers, names, and facts. For cyber, include CVE numbers 
where relevant. For weather, include actual temps and precip %.

Format your response as JSON with this structure:
{{
  "date_display": "{date_str}",
  "market_snapshot": {{
    "dow": {{"value": "...", "change": "...", "direction": "up|down"}},
    "sp500": {{"value": "...", "change": "...", "direction": "up|down"}},
    "nasdaq": {{"value": "...", "change": "...", "direction": "up|down"}},
    "wti": {{"value": "...", "change": "...", "direction": "up|down"}},
    "gas_avg": {{"value": "...", "change": "..."}}
  }},
  "weather": {{
    "high": "...",
    "low": "...",
    "condition": "...",
    "rain_chance": "...",
    "alerts": "...",
    "wind": "..."
  }},
  "breaking_alert": "One sentence breaking news alert if warranted, else empty string",
  "sections": {{
    "world": [
      {{"headline": "...", "summary": "...", "source": "...", "url": "...", "severity": "critical|high|watch|normal"}}
    ],
    "national": [...],
    "finance": [...],
    "cyber": [...]
  }}
}}

Return ONLY valid JSON, no markdown fences, no preamble.
"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": research_prompt}]
    )
    
    # Extract the text response from content blocks
    response_text = ""
    for block in message.content:
        if block.type == "text":
            response_text += block.text
    
    # Parse JSON
    try:
        # Strip any accidental markdown fences
        clean = response_text.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        data = json.loads(clean.strip())
        print(f"[1/3] Research complete. {sum(len(v) for v in data['sections'].values())} stories gathered.")
        return data
    except json.JSONDecodeError as e:
        print(f"[1/3] JSON parse error: {e}")
        print(f"Raw response: {response_text[:500]}")
        raise

def generate_html(client, news_data, date_display, date_slug):
    """Ask Claude to render the news data into a polished HTML briefing."""
    print("[2/3] Generating HTML briefing...")

    html_prompt = f"""
You are generating a daily intelligence briefing HTML page.

Here is today's researched news data as JSON:
{json.dumps(news_data, indent=2)}

Generate a complete, self-contained HTML page for a daily briefing with these requirements:

DESIGN:
- Dark theme (#0d0f12 background)  
- IBM Plex Mono for labels/meta, Playfair Display for headlines, IBM Plex Sans for body
- Load fonts from Google Fonts CDN
- Sticky top bar with market ticker (DOW, S&P, NASDAQ, WTI, Gas)
- Large "Daily Briefing" header with date
- If breaking_alert is non-empty, show a red alert banner
- Main 2-column layout: primary content (left) + 340px sidebar (right)
- Sidebar: weather card, markets card, quick links card
- Each section has a colored header with icon and story count
- Each story is a card with: category flag, severity badge (critical=red, high=amber, watch=blue), headline (Playfair), summary text, source link
- Footer with date and sources
- Smooth fade-in animations on load
- Fully mobile responsive (single column under 900px)
- Color palette: bg #0d0f12, surface #13161b, border #252a33, accent gold #c8a96e, blue #4e9af1, red #e05555, green #4caf79

SECTIONS (in order in main column):
1. 🌍 World News
2. 🇺🇸 National News  
3. 💰 Finance & Markets
4. 🔐 Cybersecurity

QUICK LINKS in sidebar (always include these):
- BleepingComputer: https://www.bleepingcomputer.com/
- The Hacker News: https://thehackernews.com/
- CISA Advisories: https://www.cisa.gov/news-events/cybersecurity-advisories
- Yahoo Finance: https://finance.yahoo.com/
- Spring TX Weather: https://www.foxweather.com/local-weather/texas/spring
- NPR News: https://www.npr.org/sections/news

Also add a small archive link at the bottom: "← Previous briefings" linking to ./archive/

Output ONLY the complete HTML. No explanation. No markdown fences. Start with <!DOCTYPE html>.
"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=8000,
        messages=[{"role": "user", "content": html_prompt}]
    )
    
    html = ""
    for block in message.content:
        if hasattr(block, "text"):
            html += block.text
    
    # Clean up any accidental fences
    html = html.strip()
    if html.startswith("```"):
        lines = html.split("\n")
        html = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])
    
    print(f"[2/3] HTML generated ({len(html):,} chars).")
    return html

def save_output(html, date_slug):
    """Save HTML to output directory."""
    print("[3/3] Saving output files...")
    
    output_dir = Path("output")
    archive_dir = output_dir / "archive"
    output_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)
    
    # Write today's briefing as index.html (the homepage)
    index_path = output_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    
    # Also write to archive with date stamp
    archive_path = archive_dir / f"{date_slug}.html"
    archive_path.write_text(html, encoding="utf-8")
    
    # Write/update the archive index
    write_archive_index(archive_dir)
    
    print(f"[3/3] Saved to {index_path} and {archive_path}")

def write_archive_index(archive_dir):
    """Generate a simple archive listing page."""
    files = sorted(archive_dir.glob("????-??-??.html"), reverse=True)
    
    items = ""
    for f in files:
        date_str = f.stem  # YYYY-MM-DD
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            label = dt.strftime("%A, %B %-d, %Y")
        except ValueError:
            label = date_str
        items += f'<li><a href="{f.name}">{label}</a></li>\n'
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Briefing Archive</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400&display=swap" rel="stylesheet">
<style>
  body {{ background:#0d0f12; color:#d4d8df; font-family:'IBM Plex Sans',sans-serif; padding:40px; }}
  h1 {{ font-family:'IBM Plex Mono',monospace; color:#c8a96e; font-size:14px; letter-spacing:.2em; text-transform:uppercase; margin-bottom:30px; }}
  ul {{ list-style:none; }}
  li {{ padding:12px 0; border-bottom:1px solid #252a33; }}
  a {{ color:#4e9af1; text-decoration:none; font-family:'IBM Plex Mono',monospace; font-size:13px; }}
  a:hover {{ color:#c8a96e; }}
  .back {{ display:block; margin-bottom:30px; color:#636b78; font-family:'IBM Plex Mono',monospace; font-size:11px; }}
  .back:hover {{ color:#c8a96e; }}
</style>
</head>
<body>
<a href="../" class="back">← Back to today's briefing</a>
<h1>// Briefing Archive</h1>
<ul>
{items}
</ul>
</body>
</html>"""
    
    (archive_dir / "index.html").write_text(html, encoding="utf-8")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    
    client = anthropic.Anthropic(api_key=api_key)
    date_display, date_slug = get_cst_date()
    
    print(f"=== Daily Briefing Generator ===")
    print(f"Date: {date_display}")
    print(f"Slug: {date_slug}")
    print()
    
    # Step 1: Research news via web search
    news_data = research_news(client, date_display)
    
    # Step 2: Generate polished HTML
    html = generate_html(client, news_data, date_display, date_slug)
    
    # Step 3: Save output
    save_output(html, date_slug)
    
    print()
    print(f"✅ Briefing for {date_display} complete.")

if __name__ == "__main__":
    main()
