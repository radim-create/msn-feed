#!/usr/bin/env python3
"""
Kinobox → MSN Partner Hub RSS transform.

Reads https://www.kinobox.cz/api/rss-centrum, applies Kinobox→MSN rules and
writes docs/feed.xml (served by GitHub Pages, polled by MSN every ~15 min).

Rules:
  1. Skip "Recenzujte a vyhrajte" giveaway articles entirely.
  2. Skip articles published before CUTOFF_DATE (default 2026-07-10).
  3. Strip <iframe> embeds (MSN forbids them). If the article had a Kinobox
     video embed, append a bold, linked closing line
     "Video si můžete přehrát na Kinoboxu." pointing to the article.
  4. Quiz articles ("Kvíz" in title): insert a linked line
     "Kvíz můžete vyplnit na Kinoboxu" just before the last paragraph.
  5. Vet every image with Claude (Haiku, vision) for visible violence/weapons.
     Flagged images are removed from the feed item. Results are cached in
     vetted.json so each image is only checked once. If ANTHROPIC_API_KEY is
     not set, images pass through unvetted (a warning is printed).
  6. Keep guid/link/pubDate/title/description/content:encoded/media:credit
     from the source feed — MSN dedups on guid, so nothing is ever
     published twice.

No third-party dependencies (urllib only), so it runs on a bare GitHub runner.
"""

import base64
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SOURCE_URL = os.environ.get("SOURCE_URL", "https://www.kinobox.cz/api/rss-centrum")
OUTPUT = Path(os.environ.get("OUTPUT", "docs/feed.xml"))
VET_CACHE = Path(os.environ.get("VET_CACHE", "vetted.json"))
CUTOFF_DATE = datetime.fromisoformat(
    os.environ.get("CUTOFF_DATE", "2026-07-10")
).replace(tzinfo=timezone.utc)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VET_MODEL = os.environ.get("VET_MODEL", "claude-haiku-4-5")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

VIDEO_LINE = "Video si můžete přehrát na Kinoboxu."
QUIZ_LINE = "Kvíz můžete vyplnit na Kinoboxu"


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def field(item: str, tag: str) -> str:
    """Extract tag content, unwrapping CDATA."""
    m = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", item, re.S)
    if not m:
        return ""
    val = m.group(1).strip()
    cd = re.match(r"^<!\[CDATA\[(.*)\]\]>$", val, re.S)
    return cd.group(1) if cd else val


# ---------------------------------------------------------------- image vetting

def load_cache() -> dict:
    if VET_CACHE.exists():
        try:
            return json.loads(VET_CACHE.read_text())
        except Exception:
            pass
    return {}


def vet_image(url: str, cache: dict) -> str:
    """Return 'clean', 'violent' or 'unknown' (download/API failure)."""
    if url in cache:
        return cache[url]
    if not ANTHROPIC_API_KEY:
        return "clean"  # pass-through mode, warned in main()
    try:
        img = http_get(url)
    except Exception as e:
        print(f"  ! image download failed ({e}): {url}", file=sys.stderr)
        return "unknown"
    media_type = "image/png" if url.lower().endswith(".png") else "image/jpeg"
    body = json.dumps({
        "model": VET_MODEL,
        "max_tokens": 10,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type,
                    "data": base64.b64encode(img).decode()}},
                {"type": "text", "text":
                    "Does this image show visible violence, fighting, blood, "
                    "guns, weapons, or war combat? Answer with exactly one "
                    "word: YES or NO."},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            answer = json.loads(r.read())["content"][0]["text"].strip().upper()
        verdict = "violent" if answer.startswith("YES") else "clean"
    except Exception as e:
        print(f"  ! vetting API failed ({e}): {url}", file=sys.stderr)
        return "unknown"
    cache[url] = verdict
    return verdict


# ---------------------------------------------------------------- content rules

def is_giveaway(title: str, content: str) -> bool:
    return "recenzujte a vyhrajte" in (title + " " + content[:500]).lower()


def is_quiz(title: str) -> bool:
    return bool(re.search(r"\bkvíz\b", title, re.I))


def clean_content(content: str, link: str, quiz: bool) -> str:
    had_video = bool(re.search(r"<iframe[^>]*kinobox\.cz/embed", content, re.I))
    # strip all iframes and scripts (MSN forbids them)
    content = re.sub(r"<iframe[^>]*>.*?</iframe>", "", content, flags=re.S | re.I)
    content = re.sub(r"<iframe[^>]*/?>", "", content, flags=re.I)
    content = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.S | re.I)
    if had_video:
        content += f'<p><b><a href="{link}">{VIDEO_LINE}</a></b></p>'
    if quiz:
        quiz_p = f'<p><a href="{link}">{QUIZ_LINE}</a></p>'
        last_p = content.rfind("<p>")
        if last_p > 0:
            content = content[:last_p] + quiz_p + content[last_p:]
        else:
            content += quiz_p
    return content


def transform_item(item: str, cache: dict, stats: dict) -> str | None:
    title = field(item, "title")
    link = field(item, "link")
    guid = field(item, "guid")
    pub = field(item, "pubDate")
    desc = field(item, "description")
    content = field(item, "content:encoded")

    if not (title and link and guid and pub):
        stats["skipped_malformed"].append(title or "(bez názvu)")
        return None
    try:
        pub_dt = parsedate_to_datetime(pub)
    except Exception:
        stats["skipped_malformed"].append(title)
        return None
    if pub_dt < CUTOFF_DATE:
        stats["skipped_old"].append(title)
        return None
    if is_giveaway(title, content):
        stats["skipped_giveaway"].append(title)
        return None

    quiz = is_quiz(title)
    content = clean_content(content, link, quiz)

    # image: vet, drop if violent or unverifiable
    media_xml = ""
    m = re.search(
        r'<media:content url="([^"]+)"[^>]*>(.*?)</media:content>', item, re.S)
    if m:
        img_url, inner = m.group(1), m.group(2)
        verdict = vet_image(img_url, cache)
        if verdict == "clean":
            credit = field(inner, "media:credit") or "Kinobox.cz"
            media_xml = (
                f'<media:content url="{img_url}" type="image/jpeg" '
                f'medium="image"><media:credit>{credit}</media:credit>'
                f"</media:content>")
        else:
            stats["images_removed"].append((title, img_url, verdict))

    if quiz:
        stats["quiz"].append(title)
    if VIDEO_LINE in content:
        stats["video"].append(title)
    stats["published"].append(title)

    def cdata(s: str) -> str:
        return "<![CDATA[" + s.replace("]]>", "]]]]><![CDATA[>") + "]]>"

    return (
        "    <item>\n"
        f"      <title>{cdata(title)}</title>\n"
        f"      <description>{cdata(desc)}</description>\n"
        f"      <content:encoded>{cdata(content)}</content:encoded>\n"
        f"      {media_xml}\n"
        f"      <pubDate>{pub}</pubDate>\n"
        f"      <link>{link}</link>\n"
        f'      <guid isPermaLink="true">{guid}</guid>\n'
        "    </item>")


def main() -> int:
    if not ANTHROPIC_API_KEY:
        print("WARNING: ANTHROPIC_API_KEY not set — images are NOT vetted "
              "for violence and pass through unchanged.", file=sys.stderr)

    src = os.environ.get("SOURCE_FILE")
    xml = Path(src).read_text() if src else http_get(SOURCE_URL).decode("utf-8")

    parts = re.split(r"<item>", xml)
    items = [p[: p.find("</item>")] for p in parts[1:] if "</item>" in p]
    if not items:
        print("ERROR: no <item> elements found in source feed", file=sys.stderr)
        return 1

    cache = load_cache()
    stats = {k: [] for k in ("published", "skipped_old", "skipped_giveaway",
                             "skipped_malformed", "images_removed",
                             "quiz", "video")}
    out_items = [x for x in (transform_item(i, cache, stats) for i in items) if x]

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>Kinobox.cz</title>\n"
        "    <link>https://www.kinobox.cz</link>\n"
        "    <description>Kinobox.cz - filmové recenze, novinky v kinech, "
        "aktuality ze světa českého i světového filmu</description>\n"
        "    <language>cs</language>\n"
        f"    <lastBuildDate>{now}</lastBuildDate>\n"
        + "\n".join(out_items)
        + "\n  </channel>\n</rss>\n")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(feed, encoding="utf-8")
    VET_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))

    print(f"published={len(stats['published'])} "
          f"skipped_old={len(stats['skipped_old'])} "
          f"skipped_giveaway={len(stats['skipped_giveaway'])} "
          f"malformed={len(stats['skipped_malformed'])} "
          f"images_removed={len(stats['images_removed'])} "
          f"quiz={len(stats['quiz'])} video={len(stats['video'])}")
    for t, u, v in stats["images_removed"]:
        print(f"  image removed ({v}): {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
