"""Vyber a kontrola obrazku pro MSN feed.

MSN zamitne cely clanek, kdyz nahledova fotka nesplni jejich pravidla:
  * "At least one image depicts graphic violence"  -> nasili na jakemkoliv
    obrazku, tedy i na fotkach uvnitr textu,
  * "Thumbnail image is too small ... at least 300 x 300 px" -> maly nahled.

Modul resi oboji:

  pick_image()            nahledova fotka: hleda kandidata, ktery je zaroven
                          bez nasili a aspon 300x300 px. Kdyz fotka z feedu
                          nevyhovuje, dohleda dalsi v galerii clanku na
                          Kinoboxu (galerie nese rozmery, takze male fotky
                          jdou vyradit bez stahovani). Kdyz nic idealniho
                          neni, pouzije se nejmene zavadny kandidat —
                          clanek nesmi zustat bez fotky.

  strip_violent_images()  fotky v tele clanku: nasilne se z HTML odstrani,
                          ostatni zustavaji. Rozmer se u nich neresi, MSN
                          limit 300x300 plati jen pro nahled.

Verdikty i zmerene rozmery se cachuji ve vetted.json (rozmery pod klicem
"size:<url>"), takze kazdy obrazek se stahuje a overuje jen jednou.
"""

import json
import re
import struct

MIN_SIDE = 300          # minimalni rozmer nahledu pozadovany MSN
RANK = {"clean": 0, "unknown": 1, "violent": 2}

MEDIA_RE = re.compile(
    r'<media:content url="([^"]+)"[^>]*>(.*?)</media:content>', re.S)
IMG_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"[^>]*>', re.I)
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _log(stats, key, value):
    stats.setdefault(key, []).append(value)


# ------------------------------------------------------------ rozmery obrazku

def _dimensions(data):
    """(sirka, vyska) z hlavicky PNG / JPEG / WEBP, jinak None."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w, h

        if data[:2] == b"\xff\xd8":                      # JPEG
            i, n = 2, len(data)
            while i < n - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                if marker in (0xC4, 0xC8, 0xCC):         # tabulky, ne rozmery
                    i += 2 + seg_len
                    continue
                if 0xC0 <= marker <= 0xCF:               # SOFn
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + seg_len

        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h
            if chunk == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    except Exception:
        pass
    return None


def image_size(url, cache, http_get):
    """(sirka, vyska) obrazku, nebo None kdyz se nepodari zjistit."""
    key = f"size:{url}"
    if key in cache:
        val = cache[key]
        return tuple(val) if val else None
    if not http_get:
        return None
    try:
        dims = _dimensions(http_get(url))
    except Exception:
        dims = None
    cache[key] = list(dims) if dims else None
    return dims


def too_small(url, cache, http_get):
    """True jen kdyz rozmer opravdu zname a je pod limitem MSN."""
    dims = image_size(url, cache, http_get)
    if not dims:
        return False        # nezname rozmer -> nediskvalifikujeme
    return dims[0] < MIN_SIDE or dims[1] < MIN_SIDE


# ------------------------------------------------------------ galerie clanku

def gallery_candidates(link, http_get):
    """[(url, sirka, vyska)] z galerie clanku na Kinoboxu."""
    if not (link and http_get):
        return []
    try:
        html = http_get(link).decode("utf-8", "replace")
        m = NEXT_DATA_RE.search(html)
        if not m:
            return []
        data = json.loads(m.group(1))
        gallery = data["props"]["pageProps"]["articleOut"].get("gallery") or []
        return [(g["url"], g.get("width") or 0, g.get("height") or 0)
                for g in gallery if g.get("url")]
    except Exception:
        return []


# ------------------------------------------------------------ nahledova fotka

def pick_image(item, aid, title, cache, stats, thumbs, vet_image, field,
               link=None, http_get=None):
    """Vrati <media:content> XML pro polozku ("" kdyz zdroj nema zadnou fotku).

    Skore kandidata = nasili (0 ciste / 1 neoveritelne / 2 nasilne)
                      + 1 kdyz je fotka mensi nez 300x300.
    Vitezi nejnizsi skore; 0 znamena fotku, kterou MSN prijme.
    """
    cands = MEDIA_RE.findall(item)
    if not cands:
        _log(stats, "images_missing", title)
        return ""

    scored = []
    for img_url, inner in cands:
        verdict = vet_image(img_url, cache)
        small = too_small(img_url, cache, http_get)
        score = RANK.get(verdict, 1) + (1 if small else 0)
        scored.append((score, verdict, small, img_url, inner))
        if score == 0:
            break

    scored.sort(key=lambda s: s[0])
    score, verdict, small, img_url, inner = scored[0]

    # fotka z feedu nevyhovuje -> zkusit galerii clanku na Kinoboxu
    if score > 0:
        known = {u for _, _, _, u, _ in scored}
        for alt_url, gw, gh in gallery_candidates(link, http_get):
            if alt_url in known:
                continue
            if gw and gh and (gw < MIN_SIDE or gh < MIN_SIDE):
                continue        # rozmer z galerie -> male preskocit bez stahovani
            if vet_image(alt_url, cache) != "clean":
                continue
            if too_small(alt_url, cache, http_get):
                continue
            _log(stats, "thumb_from_gallery", (title, alt_url))
            score, verdict, small, img_url, inner = 0, "clean", False, alt_url, ""
            break

    thumbs[aid] = img_url
    if verdict != "clean":
        stats.setdefault("images_removed", []).append((title, img_url, verdict))
    if small:
        _log(stats, "thumbs_too_small", (title, img_url))

    credit = (field(inner, "media:credit") if inner else "") or "Kinobox.cz"
    return (
        f'<media:content url="{img_url}" type="image/jpeg" '
        f'medium="image"><media:credit>{credit}</media:credit>'
        f"</media:content>")


# ------------------------------------------------------------ fotky v tele

def strip_violent_images(content, cache, stats, vet_image, title):
    """Odstrani z HTML clanku <img> tagy, ktere kontrola oznaci za nasilne."""
    if "<img" not in content:
        return content

    removed = []

    def repl(m):
        url = m.group(1)
        if vet_image(url, cache) == "violent":
            removed.append(url)
            return ""
        return m.group(0)

    content = IMG_RE.sub(repl, content)
    for url in removed:
        _log(stats, "body_images_removed", (title, url))
    return content
