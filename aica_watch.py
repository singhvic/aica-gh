#!/usr/bin/env python3
"""
AICA Level-1 batch watcher.

Fetches the ICAI AICA page, finds every batch whose registration is OPEN
(i.e. its card does NOT carry the "reg_close" marker), groups them by city,
prints a report, writes a GitHub Step Summary, and raises an alert
(GitHub issue + optional Telegram) whenever a *watched* city has an open slot.

Watch-list precedence:
    1. WATCH_CITIES env var (comma-separated)  <- workflow_dispatch input or repo variable
    2. config.json "watch_cities"
"""

import json
import os
import re
import sys
from urllib.request import Request, urlopen

CONFIG_PATH = "config.json"
DEFAULT_URL = "https://ai.icai.org/aica.php"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


# ----------------------------- config -----------------------------
def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    url = cfg.get("page_url", DEFAULT_URL)

    env_watch = (os.environ.get("WATCH_CITIES") or "").strip()
    if env_watch:
        watch = [c.strip() for c in env_watch.split(",") if c.strip()]
    else:
        watch = cfg.get("watch_cities", [])
    return url, watch


# ----------------------------- fetch -----------------------------
def fetch(url):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ----------------------------- parse -----------------------------
def extract_city(title):
    """Pull a clean city name out of a batch heading like
    'Batch720 - Surat: 29,30,31 May 2026' or 'Batch768-AHMEDABAD-16,17,18 JUNE 2026'."""
    t = title.strip()
    # drop the leading "Batch<number>" and any separators after it
    t = re.sub(r"^Batch\s*\d+\s*[-:–]*\s*", "", t, flags=re.I)
    # prefer a colon as the city/date delimiter
    if ":" in t:
        city = t.split(":", 1)[0]
    else:
        # otherwise cut at the first "-<digit>" (start of the date)
        m = re.search(r"-\s*\d", t)
        if m:
            city = t[: m.start()]
        else:
            # last resort: cut at the first digit
            m2 = re.search(r"\d", t)
            city = t[: m2.start()] if m2 else t
    return city.strip(" -–:,").strip()


def parse_batches(html):
    """Return list of dicts: {id, title, city, open(bool)}."""
    batches = []
    # Heading anchors look like: <a href="...course_details.php?id=775">Batch768-AHMEDABAD-...</a>
    # The inner text starts with "Batch", which distinguishes it from the image anchor.
    pattern = re.compile(
        r'href="[^"]*course_details\.php\?id=(\d+)"[^>]*>\s*(Batch[^<]+?)\s*</a>',
        re.I,
    )
    seen = set()
    for m in pattern.finditer(html):
        bid, title = m.group(1), m.group(2).strip()
        if bid in seen:
            continue
        seen.add(bid)

        # Closed-status: find this card's image anchor (the previous occurrence of the
        # same id before the heading) and check whether reg_close sits inside that card.
        head_pos = m.start()
        prior = html.rfind(f"id={bid}", 0, head_pos)
        window = html[prior:head_pos] if prior != -1 else html[max(0, head_pos - 600):head_pos]
        is_open = "reg_close" not in window.lower()

        batches.append(
            {"id": bid, "title": title, "city": extract_city(title), "open": is_open}
        )
    return batches


def group_open_cities(batches):
    """city_display -> list of open batch dicts."""
    cities = {}
    for b in batches:
        if b["open"]:
            cities.setdefault(b["city"], []).append(b)
    return dict(sorted(cities.items(), key=lambda kv: kv[0].lower()))


def is_watched(city, watch):
    c = city.lower()
    return any(w.lower() in c or c in w.lower() for w in watch)


# ----------------------------- report -----------------------------
def build_report(open_cities, watch):
    lines = []
    watched_hits = []
    lines.append(f"# AICA Level-1 — open batches\n")
    lines.append(f"_Watching: {', '.join(watch) if watch else '(none configured)'}_\n")

    if not open_cities:
        lines.append("No batches are currently open for registration.")
        return "\n".join(lines), watched_hits

    lines.append(f"**{len(open_cities)} cities have open slots:**\n")
    lines.append("| City | Open batches | Dates |")
    lines.append("|------|--------------|-------|")
    for city, items in open_cities.items():
        flag = " 🔴 **WATCHED**" if is_watched(city, watch) else ""
        if flag:
            watched_hits.extend(items)
        def date_part(i):
            rest = re.sub(r"^Batch\s*\d+\s*[-:–]*\s*", "", i["title"], flags=re.I)
            # drop the leading city token to leave just the date
            rest = rest[len(i["city"]):].strip(" -–:,")
            return rest or rest
        dates = "; ".join(date_part(i) for i in items)
        lines.append(f"| {city}{flag} | {len(items)} | {dates} |")
    return "\n".join(lines), watched_hits


# ----------------------------- alerts -----------------------------
def gh_api(method, path, payload=None):
    import urllib.request

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GH_REPO")
    if not token or not repo:
        return None
    url = f"https://api.github.com/repos/{repo}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "aica-watcher")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def existing_alert_ids():
    """Read open issues labelled 'aica-alert' and collect already-alerted batch ids."""
    issues = gh_api("GET", "/issues?state=open&labels=aica-alert&per_page=100")
    ids = set()
    if not issues:
        return ids
    for iss in issues:
        for mm in re.findall(r"<!--\s*aica:batch:(\d+)\s*-->", iss.get("body", "")):
            ids.add(mm)
    return ids


def create_issue(batch, url):
    title = f"🔔 AICA open: {batch['city']} (Batch{batch['id']})"
    body = (
        f"An open registration slot was detected for **{batch['city']}**.\n\n"
        f"- {batch['title']}\n"
        f"- Register: {url}\n\n"
        f"<!-- aica:batch:{batch['id']} -->"
    )
    gh_api("POST", "/issues", {"title": title, "body": body, "labels": ["aica-alert"]})


def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    import urllib.parse, urllib.request

    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "false"}
    ).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)


# ----------------------------- main -----------------------------
def main():
    url, watch = load_config()
    html = fetch(url)
    batches = parse_batches(html)
    open_cities = group_open_cities(batches)
    report, watched_hits = build_report(open_cities, watch)

    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(report + "\n")

    # Alert only for newly-seen watched batches.
    if watched_hits:
        already = existing_alert_ids()
        new = [b for b in watched_hits if b["id"] not in already]
        for b in new:
            create_issue(b, url)
        if new:
            msg = "🔔 AICA batch open!\n" + "\n".join(
                f"{b['city']}: {b['title']}" for b in new
            ) + f"\n{url}"
            telegram(msg)
            print(f"\nALERT: {len(new)} new watched batch(es) -> issue + telegram sent.")
        else:
            print("\nWatched city open, but already alerted earlier (no duplicate sent).")
    else:
        print("\nNo watched city open right now.")


if __name__ == "__main__":
    main()
