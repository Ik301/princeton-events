#!/usr/bin/env python3
"""princeton_events.py -- scrape Princeton public event calendars into .ics feeds.

Sources (all public, no login):
  chapel       chapel.princeton.edu/events                      (Drupal, Cloudflare)
  careers      careerdevelopment.princeton.edu/.../workshops-events (Drupal, Cloudflare)
  campusrec    campusrec.princeton.edu/events                   (Drupal, Cloudflare)
  university   www.princeton.edu/feed/events/                   (RSS, plain HTTP)

The Cloudflare-fronted department sites block plain curl, so those are fetched with
`opencli web read` (real Chrome) and parsed from the Markdown it emits. The university
feed is plain RSS whose <pubDate> is the event START time.

Outputs:
  ~/.calendars/<cal>/<uid>.ics        one event per file, khal-readable
  ~/.hermes/princeton_events/events.json
  ~/.hermes/princeton_events/site/*.ics    per-source + combined feeds
  ~/.hermes/princeton_events/site/index.html
  ~/.hermes/princeton_events/md/<src>.md   raw scrape cache (debugging)
  ~/.hermes/princeton_events/state.json

Designed for a `no_agent` weekly cron job: prints NOTHING when nothing changed.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
OUT = HOME / ".hermes" / "princeton_events"
SITE = OUT / "site"
MDCACHE = OUT / "md"
STATE = OUT / "state.json"
EVENTS_JSON = OUT / "events.json"
TZ = ZoneInfo("America/New_York")
KEEP_DAYS_PAST = 1
HORIZON_DAYS = 120

SOURCES = [
    {
        "key": "chapel",
        "label": "Princeton Chapel",
        "cal": "princeton_chapel",
        "color": "#6a1b9a",
        "kind": "md",
        "url": "https://chapel.princeton.edu/events",
    },
    {
        "key": "careers",
        "label": "Princeton Career Dev",
        "cal": "princeton_careers",
        "color": "#00695c",
        "kind": "md",
        "url": "https://careerdevelopment.princeton.edu/advising-programs/workshops-events",
    },
    {
        "key": "campusrec",
        "label": "Princeton Campus Rec",
        "cal": "princeton_campusrec",
        "color": "#c62828",
        "kind": "md",
        "url": "https://campusrec.princeton.edu/events",
    },
    {
        "key": "university",
        "label": "Princeton Events",
        "cal": "princeton_events",
        "color": "#ef6c00",
        "kind": "rss",
        "url": "https://www.princeton.edu/feed/events/",
    },
]

WEEKDAYS = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
         "Oct", "Nov", "Dec"]
    )
}
TITLE_LINK = re.compile(r"^\[([^\]]+)\]\((https?://[^\s)]+)\)$")
# some sources render the link inline, e.g. "Sep 21 : [Drop-in Advising](https://.../events/x)"
LINK_IN_LINE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+/events?/[^\s)]*)\)")
DATE_FULL = re.compile(
    rf"^({WEEKDAYS}), ([A-Z][a-z]{{2}}) (\d{{1,2}}), (\d{{4}})"
    r"(?:, (\d{1,2}):(\d{2}) (am|pm) [–-] (\d{1,2}):(\d{2}) (am|pm))?"
    rf"$"
)
DATE_RANGE = re.compile(
    rf"^({WEEKDAYS}), ([A-Z][a-z]{{2}}) (\d{{1,2}}), (\d{{4}})"
    rf" [–-] ({WEEKDAYS}), ([A-Z][a-z]{{2}}) (\d{{1,2}}), (\d{{4}})$"
)


def log(*a):
    print(*a, file=sys.stderr)


def fetch_md(url: str, save: bool = True) -> str:
    """Fetch via opencli's real browser (gets past Cloudflare)."""
    out = MDCACHE / (hashlib.sha1(url.encode()).hexdigest()[:10] + ".md")
    # IMPORTANT: keep the real environment (HOME etc.) -- opencli needs ~/.opencli and
    # its Chrome profile; a scrubbed env launches a profileless browser that CF blocks.
    env = dict(os.environ)
    env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "/usr/bin:/bin")
    cmd = ["opencli", "web", "read", "--url", url, "--stdout", "true",
           "-f", "md", "--download-images", "false", "--wait", "5"]
    last = ""
    for attempt in (1, 2, 3):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
            text = p.stdout or ""
        except subprocess.TimeoutExpired:
            text, p = "", None
        if "Just a moment" not in text and "Performing security verification" not in text \
                and len(text) > 500:
            MDCACHE.mkdir(parents=True, exist_ok=True)
            if save:
                out.write_text(text, encoding="utf-8")
            return text
        last = (f"attempt {attempt}: cloudflare interstitial ({len(text)} bytes)"
                + (f"; stderr={p.stderr.strip()[:160]}" if p is not None and p.stderr else ""))
        log("  ", last)
        time.sleep(10)
    raise RuntimeError(f"opencli web read failed for {url}: {last}")


def fetch_rss(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def to24(hh: str, mm: str, ap: str) -> tuple[int, int]:
    h = int(hh) % 12
    if ap.lower() == "pm":
        h += 12
    return h, int(mm)


def mk_dt(y: int, mon: str, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, MONTHS[mon], d, hh, mm, tzinfo=TZ)


def parse_md(text: str, source: dict) -> list[dict]:
    """Parse opencli markdown: title-link line, optional category, date line, body, Location."""
    lines = [l.rstrip() for l in text.splitlines()]
    events: list[dict] = []
    for i, line in enumerate(lines):
        s = line.strip()
        mfull, mrange = DATE_FULL.match(s), DATE_RANGE.match(s)
        if not (mfull or mrange):
            continue
        # nearest preceding title link
        title = url = None
        cat = None
        for j in range(i - 1, max(-1, i - 8), -1):
            t = lines[j].strip()
            if not t:
                continue
            tm = TITLE_LINK.match(t) or LINK_IN_LINE.search(t)
            if tm:
                title, url = html.unescape(tm.group(1)).strip(), tm.group(2)
                break
            if cat is None and len(t) < 60:
                cat = t
        if not title:
            continue
        # forward: description until 'Location', then location
        desc_parts, loc = [], ""
        k = i + 1
        while k < len(lines) and k < i + 40:
            t = lines[k].strip()
            if LINK_IN_LINE.search(t):
                break
            if t.lower() == "location":
                for k2 in range(k + 1, min(len(lines), k + 4)):
                    if lines[k2].strip():
                        loc = lines[k2].strip()
                        break
                break
            if t and not t.startswith("!["):
                desc_parts.append(t)
            k += 1
        desc = re.sub(r"\s+", " ", " ".join(desc_parts)).strip().rstrip("…").strip()

        if mrange:
            sd = mk_dt(int(mrange.group(4)), mrange.group(2), int(mrange.group(3)), 0, 0)
            ed = mk_dt(int(mrange.group(8)), mrange.group(6), int(mrange.group(7)), 0, 0)
            start, end, allday = sd, ed + timedelta(days=1), True
        else:
            g = mfull.groups()
            if g[4]:
                sh, sm = to24(g[4], g[5], g[6])
                eh, em = to24(g[7], g[8], g[9])
                start = mk_dt(int(g[3]), g[1], int(g[2]), sh, sm)
                end = mk_dt(int(g[3]), g[1], int(g[2]), eh, em)
                if end <= start:
                    end += timedelta(days=1)
            else:
                start = mk_dt(int(g[3]), g[1], int(g[2]), 0, 0)
                end = start + timedelta(days=1)
            allday = False
        events.append(
            dict(source=source["key"], title=title, url=url, start=start.isoformat(),
                 end=end.isoformat(), allday=allday, location=loc, category=cat or "",
                 description=desc[:600])
        )
    return events


FC_EVENT = re.compile(
    r'<a class="[^"]*fc-daygrid-event[^"]*"[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.S)
FC_VIS = re.compile(r'<span class="visually-hidden">([^<]+)</span>')
FC_TIME = re.compile(r'<div class="fc-event-time"[^>]*>([^<]*)</div>')
FC_TITLE = re.compile(r'<div class="fc-event-title">(.*?)</div>', re.S)
GRID_DATE = re.compile(
    r"^([A-Z][a-z]{2}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}) ([ap])\.m\. [–-] "
    r"(\d{1,2}):(\d{2}) ([ap])\.m\.$"
)


def parse_fc_grid(text: str, source: dict) -> list[dict]:
    """FullCalendar month grid embedded in some pages (richer than the list tab)."""
    events = []
    for href, block in FC_EVENT.findall(text):
        vis = FC_VIS.search(block)
        tm = FC_TIME.search(block)
        ti = FC_TITLE.search(block)
        if not (ti and vis):
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", ti.group(1))).strip()
        vis_txt = html.unescape(vis.group(1)).strip()
        m = GRID_DATE.match(vis_txt)
        if m:
            h1 = int(m.group(4)) % 12 + (12 if m.group(6) == "p" else 0)
            h2 = int(m.group(7)) % 12 + (12 if m.group(9) == "p" else 0)
            start = mk_dt(int(m.group(3)), m.group(1), int(m.group(2)), h1, int(m.group(5)))
            end = mk_dt(int(m.group(3)), m.group(1), int(m.group(2)), h2, int(m.group(8)))
            allday = False
        else:  # e.g. "All day" grid entries
            dm = re.match(r"^([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", vis_txt)
            if not dm:
                continue
            start = mk_dt(int(dm.group(3)), dm.group(1), int(dm.group(2)), 0, 0)
            end = start + timedelta(days=1)
            allday = True
        if end <= start:
            end += timedelta(days=1)
        events.append(
            dict(source=source["key"], title=title, url=href, start=start.isoformat(),
                 end=end.isoformat(), allday=allday, location="", category="",
                 description="")
        )
    return events


def parse_rss(text: str, source: dict) -> list[dict]:
    events = []
    for item in re.findall(r"<item>(.*?)</item>", text, re.S):
        def tag(name):
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", item, re.S)
            return html.unescape(m.group(1)).strip() if m else ""
        title, link, pub = tag("title"), tag("link"), tag("pubDate")
        if not (title and pub):
            continue
        try:
            start = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(TZ)
        except ValueError:
            continue
        desc = re.sub(r"<[^>]+>", " ", tag("description"))
        desc = re.sub(r"\s+", " ", html.unescape(desc)).strip()[:600]
        # the feed encodes "no time given" as midnight -- render those as all-day
        allday = start.hour == 0 and start.minute == 0
        events.append(
            dict(source=source["key"], title=title, url=link, start=start.isoformat(),
                 end=((start + timedelta(days=1)) if allday
                      else (start + timedelta(hours=1))).isoformat(),
                 allday=allday, location="", category="", description=desc)
        )
    return events


def uid_for(ev: dict) -> str:
    raw = f"{ev['source']}|{ev['url']}|{ev['start']}"
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


def esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,") \
        .replace("\n", r"\n")


def fold(line: str) -> str:
    out, cur = [], line
    while len(cur.encode()) > 73:
        cut = 73
        while len(cur[:cut].encode()) > 73:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


def ics_for(ev: dict, uid: str, stamp: str) -> str:
    fmt = "%Y%m%d"
    if ev["allday"]:
        sd = datetime.fromisoformat(ev["start"]).strftime(fmt)
        ed = datetime.fromisoformat(ev["end"]).strftime(fmt)
        dt_lines = [f"DTSTART;VALUE=DATE:{sd}", f"DTEND;VALUE=DATE:{ed}"]
    else:
        sd = datetime.fromisoformat(ev["start"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ed = datetime.fromisoformat(ev["end"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dt_lines = [f"DTSTART:{sd}", f"DTEND:{ed}"]
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//hermes//princeton-events//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        *dt_lines,
        fold(f"SUMMARY:{esc(ev['title'])}"),
    ]
    if ev.get("location"):
        body.append(fold(f"LOCATION:{esc(ev['location'])}"))
    parts = []
    if ev.get("category"):
        parts.append(f"[{ev['category']}]")
    if ev.get("description"):
        parts.append(ev["description"])
    if ev.get("audience"):
        parts.append(f"Audience: {ev['audience']}")
    if ev.get("register"):
        parts.append(f"Register: {ev['register']}")
    if ev.get("url"):
        parts.append(ev["url"])
    if parts:
        body.append(fold(f"DESCRIPTION:{esc(chr(10).join(parts))}"))
    if ev.get("url"):
        body.append(fold(f"URL:{ev['url']}"))
    body += ["STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(body)


STAMP_LINE = re.compile(r"^DTSTAMP:.*$", re.M)

def _norm(text: str) -> str:
    """Normalize for comparison: ignore DTSTAMP (regenerated per run) and newlines."""
    return STAMP_LINE.sub("DTSTAMP:X", text.replace("\r\n", "\n"))


def write_cal(dirpath: Path, pairs: list[tuple[str, dict, str]]) -> tuple[int, int]:
    """pairs: (uid, event, ics_text). Returns (written, deleted).

    A file is only touched when its event content actually changed, so an unchanged
    weekly run leaves every .ics byte-identical (no churn in synced calendars).
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    wanted = {f"{uid}.ics": ics for uid, _, ics in pairs}
    written = deleted = 0
    for name, text in wanted.items():
        p = dirpath / name
        if p.exists() and _norm(p.read_text(encoding="utf-8", errors="replace")) == _norm(text):
            continue
        p.write_bytes(text.encode("utf-8"))
        written += 1
    for p in dirpath.glob("*.ics"):
        if p.name not in wanted:
            p.unlink()
            deleted += 1
    return written, deleted


def combined_feed(events: list[dict], stamps: dict[str, list[str]]) -> str:
    head = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//hermes//princeton-events//EN",
            "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Princeton Events (all)"]
    body = []
    for ev in events:
        uid = uid_for(ev)
        stamp = (stamps.get(uid) or [None, "19700101T000000Z"])[1]
        one = ics_for(ev, uid, stamp).splitlines()
        body += [l for l in one if l not in ("BEGIN:VCALENDAR", "VERSION:2.0",
                 "PRODID:-//hermes//princeton-events//EN", "CALSCALE:GREGORIAN",
                 "METHOD:PUBLISH") and l != ""]
    return "\r\n".join(head + body + ["END:VCALENDAR", ""])


PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Princeton Events</title>
<style>
  :root{
    --bg:#0f1115; --card:#161a22; --card2:#1c2130; --line:#2a3040;
    --fg:#e8eaf0; --muted:#98a2b3; --accent:#e77500;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{padding:20px 22px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;
    background:rgba(15,17,21,.96);backdrop-filter:blur(6px);z-index:5}
  h1{margin:0 0 2px;font-size:20px;letter-spacing:.2px}
  h1 span{color:var(--accent)}
  .sub{color:var(--muted);font-size:12.5px}
  .bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}
  .chip{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;
    border:1px solid var(--line);background:var(--card);color:var(--muted);cursor:pointer;
    font-size:12.5px;user-select:none;transition:.12s}
  .chip.on{color:var(--fg);background:var(--card2);border-color:var(--dot)}
  .chip .dot{width:9px;height:9px;border-radius:50%;background:var(--dot);opacity:.35}
  .chip.on .dot{opacity:1}
  .chip .n{color:var(--muted);font-size:11.5px}
  .seg{margin-left:auto;display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .seg button{background:var(--card);color:var(--muted);border:0;padding:6px 13px;cursor:pointer;font-size:12.5px}
  .seg button.on{background:var(--card2);color:var(--fg)}
  main{padding:16px 22px 44px}
  .monthnav{display:flex;align-items:center;gap:12px;margin:2px 0 12px}
  .monthnav b{font-size:15.5px}
  .monthnav button{background:var(--card);border:1px solid var(--line);color:var(--fg);
    border-radius:7px;padding:4px 11px;cursor:pointer}
  .grid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}
  .dow{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.6px;padding:0 4px 4px}
  .day{background:var(--card);border:1px solid var(--line);border-radius:9px;min-height:92px;padding:6px 6px 7px}
  .day.out{opacity:.42}
  .day.today{border-color:var(--accent)}
  .dnum{font-size:11.5px;color:var(--muted);margin-bottom:5px;display:flex;justify-content:space-between}
  .day.today .dnum{color:var(--accent);font-weight:600}
  .ev{display:block;text-decoration:none;color:var(--fg);background:var(--card2);
    border-left:3px solid var(--dot);border-radius:5px;padding:3px 6px;margin-bottom:4px;font-size:11.5px}
  .ev:hover{background:#232a3b}
  .ev .tm{color:var(--muted);font-size:10.5px;display:block}
  .ev.all .tm{color:var(--dot)}
  .list .daygroup{background:var(--card);border:1px solid var(--line);border-radius:10px;
    padding:12px 14px;margin-bottom:12px}
  .list .daygroup h3{margin:0 0 9px;font-size:13px;color:var(--accent);text-transform:uppercase;letter-spacing:.7px}
  .list .row{display:flex;gap:12px;padding:7px 0;border-top:1px solid var(--line)}
  .list .row:first-of-type{border-top:0}
  .list .time{flex:0 0 108px;color:var(--muted);font-size:12.5px;padding-top:1px}
  .list .body{flex:1;min-width:0}
  .list a{color:var(--fg);text-decoration:none}
  .list a:hover{color:var(--accent)}
  .list .meta{color:var(--muted);font-size:12px;margin-top:2px}
  .src{display:inline-block;font-size:10.5px;padding:1px 7px;border-radius:999px;margin-top:4px;
    border:1px solid var(--dot);color:var(--dot)}
  .list .chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px}
  .src.reg{text-decoration:none;border-color:var(--muted);color:var(--muted);cursor:pointer}
  .src.reg:hover{border-color:var(--accent);color:var(--accent)}
  .list .desc{color:var(--muted);font-size:12px;margin-top:4px}
  .empty{color:var(--muted);padding:28px 4px}
  footer{color:var(--muted);font-size:11.5px;padding:0 22px 26px}
  @media(max-width:760px){ .grid{grid-template-columns:repeat(2,1fr)} .dow{display:none}
    .list .time{flex-basis:78px} }
</style>
</head>
<body>
<header>
  <h1>Princeton <span>Events</span></h1>
  <div class="sub" id="sub"></div>
  <div class="bar">
    <div id="chips" style="display:flex;flex-wrap:wrap;gap:8px"></div>
    <div class="seg">
      <button id="bMonth" class="on">Month</button>
      <button id="bList">List</button>
    </div>
  </div>
</header>
<main>
  <div id="month">
    <div class="monthnav">
      <button id="prev">&larr;</button><b id="mtitle"></b><button id="next">&rarr;</button>
      <button id="today">Today</button>
    </div>
    <div class="grid" id="dows"></div>
    <div class="grid" id="grid" style="margin-top:6px"></div>
  </div>
  <div id="list" class="list" style="display:none"></div>
</main>
<footer id="foot"></footer>
<script>
const DATA = __PAYLOAD__;
const SRC = Object.fromEntries(DATA.sources.map(s => [s.key, s]));
const EV = DATA.events.map(e => ({...e, d: new Date(e.start), e2: new Date(e.end)}));
const on = new Set(DATA.sources.map(s => s.key));
const chips = document.getElementById('chips');
DATA.sources.forEach(s => {
  const c = document.createElement('button');
  c.className = 'chip on'; c.style.setProperty('--dot', s.color);
  const n = EV.filter(e => e.source === s.key).length;
  c.innerHTML = `<span class="dot"></span>${s.label}<span class="n">${n}</span>`;
  c.onclick = () => { c.classList.toggle('on'); on.has(s.key) ? on.delete(s.key) : on.add(s.key); render(); };
  chips.appendChild(c);
});
const dows = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
document.getElementById('dows').innerHTML = dows.map(d => `<div class="dow">${d}</div>`).join('');
let view = 'month', cursor = new Date(Date.now());
const fmtT = d => d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}).replace(':00','');
const sameDay = (a,b) => a.toDateString() === b.toDateString();
const vis = () => EV.filter(e => on.has(e.source));

function render(){
  const ev = vis();
  document.getElementById('sub').textContent =
    `${ev.length} upcoming events · ${DATA.sources.length} sources · refreshed ${DATA.generated_label}`;
  document.getElementById('foot').textContent =
    'Scraped from chapel / career development / campus rec / university events. Toggles filter this page only.';
  document.getElementById('month').style.display = view === 'month' ? '' : 'none';
  document.getElementById('list').style.display = view === 'list' ? '' : 'none';
  document.getElementById('bMonth').className = view === 'month' ? 'on' : '';
  document.getElementById('bList').className = view === 'list' ? 'on' : '';
  view === 'month' ? renderMonth(ev) : renderList(ev);
}
function renderMonth(ev){
  const y = cursor.getFullYear(), m = cursor.getMonth();
  document.getElementById('mtitle').textContent =
    cursor.toLocaleDateString([], {month:'long', year:'numeric'});
  const first = new Date(y, m, 1), start = new Date(first); start.setDate(1 - first.getDay());
  const today = new Date(); let html = '';
  for (let i = 0; i < 42; i++){
    const d = new Date(start); d.setDate(start.getDate() + i);
    const dayEv = ev.filter(e => {
      const a = new Date(e.d.getFullYear(), e.d.getMonth(), e.d.getDate());
      const b = new Date(e.e2.getFullYear(), e.e2.getMonth(), e.e2.getDate());
      const t = new Date(d.getFullYear(), d.getMonth(), d.getDate());
      return t >= a && t <= (e.allday ? new Date(b - 86400000) : b);
    }).sort((a,b) => a.d - b.d);
    const cls = 'day' + (d.getMonth() !== m ? ' out' : '') + (sameDay(d, today) ? ' today' : '');
    html += `<div class="${cls}"><div class="dnum"><span>${d.getDate()}</span></div>`;
    dayEv.slice(0,4).forEach(e => {
      const col = SRC[e.source] ? SRC[e.source].color : '#888';
      html += `<a class="ev${e.allday?' all':''}" style="--dot:${col}" href="${e.url}" target="_blank"
        title="${e.title.replace(/"/g,'&quot;')}${e.location ? ' — ' + e.location : ''}">
        <span class="tm">${e.allday ? 'all day' : fmtT(e.d)}</span>${e.title.slice(0,58)}</a>`;
    });
    if (dayEv.length > 4) html += `<div class="tm" style="color:var(--muted);font-size:10.5px">+${dayEv.length-4} more</div>`;
    html += '</div>';
  }
  document.getElementById('grid').innerHTML = html;
}
function renderList(ev){
  const groups = {};
  ev.forEach(e => { const k = e.d.toDateString(); (groups[k] = groups[k] || []).push(e); });
  const keys = Object.keys(groups).sort((a,b) => new Date(a) - new Date(b)).slice(0, 45);
  if (!keys.length){ document.getElementById('list').innerHTML = '<div class="empty">Nothing ahead for the selected sources.</div>'; return; }
  document.getElementById('list').innerHTML = keys.map(k => {
    const d = new Date(k);
    const rows = groups[k].sort((a,b) => a.d - b.d).map(e => {
      const col = SRC[e.source] ? SRC[e.source].color : '#888';
      const lbl = SRC[e.source] ? SRC[e.source].label : e.source;
      const time = e.allday ? 'All day' : fmtT(e.d) + (e.end !== e.start ? ' – ' + fmtT(e.e2) : '');
      const meta = [e.location, e.audience].filter(Boolean).join(' · ');
      const desc = (e.description || '').slice(0, 190);
      return `<div class="row"><div class="time">${time}</div><div class="body">
        <a href="${e.url}" target="_blank">${e.title}</a>
        ${meta ? `<div class="meta">${meta}</div>` : ''}
        ${desc ? `<div class="desc">${desc}${(e.description || '').length > 190 ? '…' : ''}</div>` : ''}
        <div class="chips"><span class="src" style="--dot:${col}">${lbl}</span>
          <a class="src reg" href="${e.url}" target="_blank">event page</a>
          ${e.register ? `<a class="src reg" href="${e.register}" target="_blank">register</a>` : ''}
        </div>
      </div></div>`;
    }).join('');
    return `<div class="daygroup"><h3>${d.toLocaleDateString([], {weekday:'long', month:'long', day:'numeric'})}</h3>${rows}</div>`;
  }).join('');
}
document.getElementById('prev').onclick = () => { cursor.setMonth(cursor.getMonth() - 1); render(); };
document.getElementById('next').onclick = () => { cursor.setMonth(cursor.getMonth() + 1); render(); };
document.getElementById('today').onclick = () => { cursor = new Date(); render(); };
document.getElementById('bMonth').onclick = () => { view = 'month'; render(); };
document.getElementById('bList').onclick = () => { view = 'list'; render(); };
render();
</script>
</body>
</html>
"""


def write_page(events: list[dict], last_change: str) -> None:
    # the page's "refreshed" label reflects when the EVENT DATA last changed, so an
    # unchanged week produces a byte-identical page instead of a new timestamp
    try:
        when = datetime.strptime(last_change, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc).astimezone(TZ)
    except (ValueError, TypeError):
        when = datetime.now(TZ)
    payload = {
        "generated": when.isoformat(),
        "generated_label": when.strftime("%b %-d, %-I:%M %p"),
        "sources": [{k: s[k] for k in ("key", "label", "color")} for s in SOURCES],
        "events": [
            {
                "title": e["title"], "source": e["source"], "start": e["start"], "end": e["end"],
                "allday": e["allday"], "location": e.get("location", ""),
                "category": e.get("category", ""), "url": e["url"],
                "register": e.get("register", ""), "audience": e.get("audience", ""),
                "description": (e.get("description") or "")[:400],
            }
            for e in events
        ],
    }
    html = PAGE_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    (SITE / "index.html").write_text(html, encoding="utf-8")


# ------------------------------------------------------------------ detail enrichment
# Each event gets its own page fetched once (cached by URL, so only NEW events cost a
# fetch) to fill in LOCATION, the real description, audience and any registration link.
DETAIL_CACHE = OUT / "details.json"
DETAIL_BUDGET = 40          # max browser fetches per run: opencli pages are slow
DETAIL_MAX_AGE_DAYS = 90

TAG = re.compile(r"<[^>]+>")


def _txt(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", s or ""))).strip()


def _subheader(page: str, name: str) -> str:
    m = re.search(rf'<h3 class="subheader">{name}</h3>\s*([^<]*)', page)
    return _txt(m.group(1)) if m else ""


def _aud_ok(s: str) -> bool:
    """Audience values are short labels; reject dates/times that some pages list nearby."""
    return bool(s) and len(s) < 60 and not re.search(r"\d{4}|\d{1,2}:\d{2}|\b(am|pm)\b", s, re.I)


def _clean_audience(s: str) -> str:
    return ", ".join(p.strip() for p in (s or "").split(",") if _aud_ok(p.strip()))


def _ul_after(page: str, name: str) -> list[str]:
    m = re.search(rf'<h3 class="subheader">{name}</h3>\s*<ul[^>]*>(.*?)</ul>', page, re.S)
    return [_txt(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), re.S)] if m else []


def detail_university(url: str) -> dict:
    """www.princeton.edu event pages need no browser and carry the good fields."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    with urllib.request.urlopen(req, timeout=45) as r:
        page = r.read().decode("utf-8", "replace")
    body = re.search(r'field--name-field-event-details.*?field__item">(.*?)</div>', page, re.S)
    reg = re.search(r'href="(https?://[^"]*(?:handshake|forms\.gle|zoom)[^"]*)"', page, re.I)
    return {
        "location": _subheader(page, "Location"),
        "audience": ", ".join(_ul_after(page, "Audience")),
        "description": _txt(body.group(1))[:900] if body else "",
        "register": reg.group(1) if reg else "",
        "at": datetime.now(timezone.utc).isoformat(),
    }


LINKY = re.compile(r"\[([^\]]+?)(?: Link is external[^\]]*)?\]\((https?://[^\s)]+)\)")


def _md_text(s: str) -> str:
    s = LINKY.sub(lambda m: f"{m.group(1).strip()} ({m.group(2)})", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    return re.sub(r"\s+", " ", s.replace("**", "")).strip(" ,")


def parse_detail_md(text: str) -> dict:
    """Department event pages (Cloudflare) fetched as markdown via the browser."""
    lines = [l.rstrip() for l in text.splitlines()]
    out = {"location": "", "audience": "", "description": "",
           "register": "", "at": datetime.now(timezone.utc).isoformat()}
    for i, line in enumerate(lines):
        s, low = line.strip(), line.strip().lower()
        if low == "location":
            for j in range(i + 1, min(len(lines), i + 5)):
                if lines[j].strip():
                    loc = _md_text(lines[j]).replace("Link opens in new window", "")
                    loc = re.sub(r"\s*\(https?://[^)]*\)\s*$", "", loc)  # drop the map link
                    out["location"] = loc.strip(" ,")
                    break
        elif low == "audience":
            aud = []
            for j in range(i + 1, min(len(lines), i + 8)):
                t = lines[j].strip()
                if t.startswith("- "):
                    v = _md_text(t[2:])
                    if _aud_ok(v):
                        aud.append(v)
                elif aud:
                    break
            out["audience"] = ", ".join(aud)
        elif low in ("event description", "details", "description"):
            parts = []
            for j in range(i + 1, min(len(lines), i + 40)):
                t = lines[j].strip()
                if re.match(r"^(share on|##|---|>\s|#\s)", t, re.I):
                    break
                if t:
                    parts.append(_md_text(t))
            if parts:
                out["description"] = " ".join(parts)[:900]
    m = re.search(r"\((https?://[^\s)]*(?:handshake|forms\.gle)[^\s)]*)\)", text, re.I)
    if m:
        out["register"] = m.group(1)
    return out


def enrich(events: list[dict], budget: int = DETAIL_BUDGET) -> tuple[int, int, list[str]]:
    """Fill location/description/audience/register from each event's own page.

    Two cost tiers: www.princeton.edu pages are plain HTTP (threaded, no budget), while
    the Cloudflare department pages need the browser (one at a time, budgeted).
    """
    cache: dict = {}
    if DETAIL_CACHE.exists():
        try:
            cache = json.loads(DETAIL_CACHE.read_text())
        except ValueError:
            cache = {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DETAIL_MAX_AGE_DAYS)).isoformat()
    live = {e["url"] for e in events if e.get("url")}
    cache = {k: v for k, v in cache.items() if k in live and v.get("at", "") > cutoff}

    fetches = browser_fetches = misses = 0
    problems: list[str] = []
    todo = [e for e in sorted(events, key=lambda e: e["start"])
            if e.get("url") and (not e.get("location")
                                 or len(e.get("description") or "") < 40)]
    plain = [e for e in todo if e["source"] == "university" and e["url"] not in cache]
    browser = [e for e in todo if e["source"] != "university" and e["url"] not in cache]

    if plain:  # fast path: no browser, fetch concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(detail_university, e["url"]): e for e in plain}
            for fut in concurrent.futures.as_completed(futs):
                ev = futs[fut]
                try:
                    cache[ev["url"]] = fut.result()
                    fetches += 1
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{ev['source']} detail "
                                    f"({ev['url'].rsplit('/', 1)[-1][:36]}): {type(e).__name__}")

    for ev in browser:  # slow path: one browser page at a time, budgeted
        if browser_fetches >= budget:
            misses += 1
            continue
        try:
            cache[ev["url"]] = parse_detail_md(fetch_md(ev["url"], save=False))
            fetches += 1
            browser_fetches += 1
        except Exception as e:  # noqa: BLE001
            problems.append(f"{ev['source']} detail "
                            f"({ev['url'].rsplit('/', 1)[-1][:36]}): {type(e).__name__}")

    for ev in todo:
        det = cache.get(ev["url"])
        if not det:
            continue
        ev["location"] = ev.get("location") or det.get("location", "")
        if len(det.get("description") or "") > len(ev.get("description") or ""):
            ev["description"] = det["description"]
        ev["audience"] = _clean_audience(det.get("audience", ""))
        ev["register"] = det.get("register", "")
    try:
        DETAIL_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except OSError:
        pass
    return fetches, misses, problems


def report_lines(changes: list[str], adds: dict[str, list[dict]], problems: list[str],
                 total: int) -> list[str]:
    """Human-facing weekly report: what changed (by name), plus any broken source."""
    lines = ["Princeton events refresh"]
    if not changes and not problems:
        lines.append(f"  no changes; {total} events tracked")
        return lines
    lines += [f"  {c}" for c in changes]
    for key, evs in adds.items():
        if not evs:
            continue
        label = next((s["label"] for s in SOURCES if s["key"] == key), key)
        shown = sorted(evs, key=lambda e: e["start"])[:6]
        lines.append(f"  new in {label}:")
        for e in shown:
            when = datetime.fromisoformat(e["start"]).strftime("%a %b %-d, %-I:%M%p")
            lines.append(f"    {when}  {e['title'][:70]}")
        if len(evs) > len(shown):
            lines.append(f"    ...and {len(evs) - len(shown)} more")
    lines += [f"  PROBLEM {p}" for p in problems]
    return lines


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    SITE.mkdir(parents=True, exist_ok=True)
    MDCACHE.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}

    all_events: list[dict] = []
    problems: list[str] = []
    per_source: dict[str, list[dict]] = {}

    # pass 1: fetch + parse every source (nothing is written yet)
    for src in SOURCES:
        try:
            if src["kind"] == "md":
                raw = fetch_md(src["url"])
                evs = parse_md(raw, src) + parse_fc_grid(raw, src)
            else:
                raw = fetch_rss(src["url"])
                evs = parse_rss(raw, src)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{src['label']}: {type(e).__name__}: {e}")
            continue
        seen, uniq = set(), []
        for ev in evs:
            u = uid_for(ev)
            if u in seen:
                continue
            seen.add(u)
            end = datetime.fromisoformat(ev["end"])
            if end < now - timedelta(days=KEEP_DAYS_PAST):
                continue
            if datetime.fromisoformat(ev["start"]) > now + timedelta(days=HORIZON_DAYS):
                continue
            ev["uid"] = u
            uniq.append(ev)
        uniq.sort(key=lambda e: e["start"])
        per_source[src["key"]] = uniq
        all_events += uniq
        if not uniq:
            problems.append(f"{src['label']}: parsed 0 events (page layout may have changed)")

    # pass 1b: pull location / description / audience / register from each event page
    n_fetch, n_miss, det_problems = enrich(all_events)
    problems += det_problems
    log(f"detail enrichment: {n_fetch} page(s) fetched, {len(det_problems)} failed"
        + (f", {n_miss} deferred (budget)" if n_miss else ""))

    # pass 2: per-event DTSTAMP. An event keeps its original stamp until its content
    # actually changes, so an unchanged week regenerates byte-identical files and feeds
    # (no churn in synced calendars, no pointless commits in the published repo).
    prev_stamps = prev.get("stamps") or {}
    stamps: dict[str, list[str]] = {}
    data_changed = False
    for ev in all_events:
        h = hashlib.sha1(ics_for(ev, ev["uid"], "X").encode()).hexdigest()
        old = prev_stamps.get(ev["uid"])
        if old and len(old) == 2 and old[0] == h:
            stamps[ev["uid"]] = [h, old[1]]
        else:
            stamps[ev["uid"]] = [h, stamp]
            data_changed = True
    last_change = (stamp if (data_changed or not prev.get("last_change"))
                   else prev["last_change"])

    # pass 3: write everything
    written_total = deleted_total = 0
    for src in SOURCES:
        uniq = per_source.get(src["key"])
        if uniq is None:
            continue
        w, d = write_cal(HOME / ".calendars" / src["cal"],
                         [(e["uid"], e, ics_for(e, e["uid"], stamps[e["uid"]][1]))
                          for e in uniq])
        written_total += w
        deleted_total += d
        (SITE / f"{src['key']}.ics").write_text(
            combined_feed(uniq, stamps), encoding="utf-8")

    all_events.sort(key=lambda e: e["start"])
    (SITE / "all.ics").write_text(combined_feed(all_events, stamps), encoding="utf-8")
    write_page(all_events, last_change)

    # change detection: per-source fingerprint of (uid, title, start, end)
    fp = {k: hashlib.sha1(json.dumps(
        [[e["uid"], e["title"], e["start"], e["end"]] for e in v]).encode()).hexdigest()
        for k, v in per_source.items()}
    changes = []
    adds: dict[str, list[dict]] = {}
    for src in SOURCES:
        k = src["key"]
        if k not in per_source:
            continue
        old = (prev.get("fp") or {}).get(k)
        if old and old != fp[k]:
            oldset = set(map(tuple, (prev.get("items") or {}).get(k, [])))
            newset = set((e["uid"], e["title"], e["start"], e["end"]) for e in per_source[k])
            added = [e for e in per_source[k]
                     if (e["uid"], e["title"], e["start"], e["end"]) not in oldset]
            gone = len([1 for x in oldset if x not in newset])
            changes.append(f"{src['label']}: +{len(added)} new, -{gone} gone")
            adds[k] = added
    state = {"last_run": now.isoformat(), "fp": fp, "last_change": last_change,
             "stamps": stamps,
             "items": {k: [[e["uid"], e["title"], e["start"], e["end"]] for e in v]
                       for k, v in per_source.items()}}
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")

    EVENTS_JSON.write_text(json.dumps(
        {"generated": now.isoformat(), "sources": SOURCES, "events": all_events},
        indent=1), encoding="utf-8")

    log(f"total events: {len(all_events)} "
        + " ".join(f"{k}={len(v)}" for k, v in per_source.items()))
    log(f"ics written={written_total} deleted={deleted_total}")

    if prev and (changes or problems):
        print("\n".join(report_lines(changes, adds, problems, len(all_events))))
    elif not prev:
        log("first run: baseline recorded (no report)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
