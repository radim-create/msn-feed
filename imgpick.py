"""Vyber nahledoveho obrazku pro polozku MSN feedu.

Clanek nesmi nikdy vyjit bez fotky — polozka bez obrazku ma na MSN vyrazne
horsi vysledky. Kontrola nasili proto uz obrazek NEODSTRANUJE, jen rozhoduje,
KTERY z dostupnych kandidatu se pouzije:

    clean  (bez nasili)          -> 1. volba
    unknown (nepodarilo se overit) -> 2. volba
    violent (oznacen jako nasilny) -> pouzije se, kdyz neni nic lepsiho

Kandidati oznaceni jinak nez "clean" se zapisou do stats["images_removed"],
odkud je build log vypise, aby sly zpetne zkontrolovat.
"""

import re

RANK = {"clean": 0, "unknown": 1, "violent": 2}

MEDIA_RE = re.compile(
    r'<media:content url="([^"]+)"[^>]*>(.*?)</media:content>', re.S)


def pick_image(item, aid, title, cache, stats, thumbs, vet_image, field):
    """Vrati <media:content> XML pro polozku (nebo "" kdyz zdroj nema obrazek)."""
    cands = MEDIA_RE.findall(item)
    if not cands:
        stats.setdefault("images_missing", []).append(title)
        return ""

    scored = []
    for img_url, inner in cands:
        verdict = vet_image(img_url, cache)
        scored.append((RANK.get(verdict, 1), verdict, img_url, inner))
        if verdict == "clean":
            break  # cistsi uz to nebude, dalsi kandidaty netreba overovat

    scored.sort(key=lambda s: s[0])
    _, verdict, img_url, inner = scored[0]

    thumbs[aid] = img_url
    if verdict != "clean":
        stats["images_removed"].append((title, img_url, verdict))

    credit = field(inner, "media:credit") or "Kinobox.cz"
    return (
        f'<media:content url="{img_url}" type="image/jpeg" '
        f'medium="image"><media:credit>{credit}</media:credit>'
        f"</media:content>")
