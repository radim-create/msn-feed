"""Vyber a kontrola obrazku pro MSN feed.

MSN zamita cely clanek, kdyz JAKYKOLIV obrazek v nem obsahuje graficke nasili
("At least one image depicts graphic violence") — tyka se to nahledove fotky
i fotek uvnitr textu. Tenhle modul resi oboji:

  pick_image()            nahledova fotka: pouzije prvni cistou. Kdyz je
                          fotka z feedu oznacena jako nasilna, dohleda dalsi
                          fotky v galerii clanku na Kinoboxu a vezme prvni
                          cistou z nich. Kdyz cista neni zadna, pouzije se
                          i tak ta nejmene zavadna — clanek nesmi zustat
                          bez fotky.

  strip_violent_images()  fotky v tele clanku: nasilne se z HTML odstrani,
                          ostatni zustavaji. Odebranim fotky z tela clanek
                          o nahledovku neprijde.

Vysledky kontroly se cachuji ve vetted.json, takze kazdy obrazek se overuje
jen jednou za celou dobu zivota feedu.
"""

import json
import re

RANK = {"clean": 0, "unknown": 1, "violent": 2}

MEDIA_RE = re.compile(
    r'<media:content url="([^"]+)"[^>]*>(.*?)</media:content>', re.S)
IMG_RE = re.compile(r'<img\b[^>]*?\bsrc="([^"]+)"[^>]*>', re.I)
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _log(stats, key, value):
    stats.setdefault(key, []).append(value)


# ------------------------------------------------------------ galerie clanku

def gallery_urls(link, http_get):
    """Adresy fotek z galerie clanku na Kinoboxu (poradi jako na webu)."""
    if not (link and http_get):
        return []
    try:
        html = http_get(link).decode("utf-8", "replace")
        m = NEXT_DATA_RE.search(html)
        if not m:
            return []
        data = json.loads(m.group(1))
        gallery = data["props"]["pageProps"]["articleOut"].get("gallery") or []
        return [g["url"] for g in gallery if g.get("url")]
    except Exception:
        return []


# ------------------------------------------------------------ nahledova fotka

def pick_image(item, aid, title, cache, stats, thumbs, vet_image, field,
               link=None, http_get=None):
    """Vrati <media:content> XML pro polozku ("" kdyz zdroj nema zadnou fotku)."""
    cands = MEDIA_RE.findall(item)
    if not cands:
        _log(stats, "images_missing", title)
        return ""

    scored = []
    for img_url, inner in cands:
        verdict = vet_image(img_url, cache)
        scored.append((RANK.get(verdict, 1), verdict, img_url, inner))
        if verdict == "clean":
            break

    scored.sort(key=lambda s: s[0])
    best_rank, verdict, img_url, inner = scored[0]

    # nahledovka z feedu je zavadna -> zkusit galerii clanku na Kinoboxu
    if best_rank > 0:
        known = {u for _, _, u, _ in scored}
        for alt_url in gallery_urls(link, http_get):
            if alt_url in known:
                continue
            alt_verdict = vet_image(alt_url, cache)
            if alt_verdict == "clean":
                _log(stats, "thumb_from_gallery", (title, alt_url))
                verdict, img_url = "clean", alt_url
                inner = ""      # kredit z galerie neznamy -> vychozi Kinobox.cz
                break

    thumbs[aid] = img_url
    if verdict != "clean":
        stats.setdefault("images_removed", []).append((title, img_url, verdict))

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
