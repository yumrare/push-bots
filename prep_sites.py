#!/usr/bin/env python3
"""prep_sites.py — build sites/*.json snapshots from each site's own data.

Run locally whenever a catalog changes; the snapshots are committed to
the repo and the bots read only the snapshots. Photo URL rules:
  ChurnMilk / GravyToast: the item's `photo` field is already a path
  ("photos/<slug>.jpg") on the site, or absent/empty for photo-less
  entries. Overpepper: `photo` is a bare stem -> /photos/<stem>.jpg.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)


def load_items(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"] if isinstance(data, dict) else data
    return list(items.values()) if isinstance(items, dict) else items


def first_sentence(text):
    text = (text or "").strip()
    if not text:
        return ""
    m = re.split(r"(?<=[.!?])\s+", text)
    return m[0].strip()


def build_resource(site, data_dir, files, pages, hashtags, keywords):
    items = []
    for fn in files:
        for it in load_items(os.path.join(data_dir, fn)):
            photo = it.get("photo") or ""
            items.append({
                "slug": it["slug"],
                "name": it["name"],
                "hook": first_sentence(it.get("intro") or it.get("about")),
                "url": f"https://{site}.com/item-{it['slug']}.html",
                "photo_url": f"https://{site}.com/{photo}" if photo else None,
            })
    return {
        "site": site,
        "brand": {"churnmilk": "ChurnMilk", "gravytoast": "GravyToast"}[site],
        "tagline": "All things dairy." if site == "churnmilk" else "All things gravy.",
        "base": f"https://{site}.com",
        "items": items,
        "pages": pages,
        "hashtags": hashtags,
        "keywords": keywords,
    }


def build_overpepper():
    items = []
    for it in load_items(os.path.join(WS, "overpepper", "site", "data", "items.json")):
        hook = (it.get("hook") or it.get("blurb") or "").strip()
        tail = f"{it['name']} — {hook}" if hook else it["name"]
        items.append({
            "slug": it["slug"],
            "name": it["name"],
            "hook": hook,
            "price": it.get("price"),
            "shu": it.get("shu"),
            "url": f"https://overpepper.com/item-{it['slug']}.html",
            "photo_url": f"https://overpepper.com/photos/{it.get('photo') or it['slug']}.jpg",
            "_tail": tail,
        })
    pages = [
        {"label": "The Heat Guide", "url": "https://overpepper.com/heat-guide.html",
         "blurb": "Every pepper on the shelf, ranked by Scoville heat — from jalapeño mild to scorpion fire."},
        {"label": "Habanero, profiled", "url": "https://overpepper.com/heat-habanero.html",
         "blurb": "100,000–350,000 SHU of fruity fire. The habanero, profiled."},
        {"label": "Ghost pepper, profiled", "url": "https://overpepper.com/heat-ghost-pepper.html",
         "blurb": "855,000–1,041,427 SHU. The ghost pepper, profiled."},
        {"label": "Scorpion pepper, profiled", "url": "https://overpepper.com/heat-scorpion.html",
         "blurb": "1.2M–2M SHU — the sting is real. The scorpion pepper, profiled."},
        {"label": "The Warm shelf", "url": "https://overpepper.com/warm.html",
         "blurb": "Eight import hot sauces with warmth, not warfare."},
        {"label": "The Overpeppered shelf", "url": "https://overpepper.com/overpeppered.html",
         "blurb": "The eight hottest bottles in the shop. Proceed happily."},
    ]
    return {
        "site": "overpepper",
        "brand": "Overpepper",
        "tagline": "Rare import hot sauces.",
        "base": "https://overpepper.com",
        "items": items,
        "pages": pages,
        "hashtags": ["hotsauce", "spicyfood", "chilihead"],
        "keywords": ["hot sauce", "scoville", "chili pepper", "spicy food"],
    }


def main():
    out = os.path.join(HERE, "sites")
    os.makedirs(out, exist_ok=True)
    churn = build_resource(
        "churnmilk", os.path.join(WS, "churnmilk", "site", "data"),
        ["items-cheese.json", "items-butter.json", "items-milk.json",
         "items-yogurt.json", "items-icecream.json"],
        [
            {"label": "The Dairy Storage Chart", "url": "https://churnmilk.com/guide-storage-chart.html",
             "blurb": "How long every dairy staple keeps, and where it belongs in the fridge."},
            {"label": "The Melting Cheese Guide", "url": "https://churnmilk.com/guide-melting-cheese-guide.html",
             "blurb": "Which cheeses melt smooth, which seize, and why."},
            {"label": "All dairy guides", "url": "https://churnmilk.com/guides.html",
             "blurb": "The ChurnMilk guide shelf — storage, melting, and more."},
        ],
        ["cheese", "dairy", "foodfacts"],
        ["cheese", "cheddar", "butter", "ice cream", "dairy"],
    )
    gravy = build_resource(
        "gravytoast", os.path.join(WS, "gravytoast", "site", "data"),
        ["items-gravies.json", "items-bases.json", "items-plates.json"],
        [
            {"label": "The Gravy Chart", "url": "https://gravytoast.com/guide-gravy-chart.html",
             "blurb": "Every gravy on the site, one chart — bases, colors, and what they cover."},
            {"label": "Roux & lumps, solved", "url": "https://gravytoast.com/guide-roux-and-lumps.html",
             "blurb": "Why gravy lumps, and how to never let it happen again."},
            {"label": "All gravy guides", "url": "https://gravytoast.com/guides.html",
             "blurb": "The GravyToast guide shelf."},
        ],
        ["gravy", "southernfood", "comfortfood"],
        ["gravy", "biscuits and gravy", "southern cooking", "sawmill gravy"],
    )
    over = build_overpepper()
    for cfg in (churn, gravy, over):
        p = os.path.join(out, cfg["site"] + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f'{cfg["site"]}: {len(cfg["items"])} items, '
              f'{sum(1 for i in cfg["items"] if i["photo_url"])} with photos, '
              f'{len(cfg["pages"])} guide pages -> {p}')


if __name__ == "__main__":
    main()
