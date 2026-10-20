#!/usr/bin/env python3
"""
bot.py — Push-bots fleet engine for ChurnMilk, GravyToast, Overpepper.

One Bluesky account (@happyfoods.bsky.social, Dee's umbrella curator
account) pushes all three sites. Pure stdlib (AT Protocol over urllib),
modeled on the proven yumrare-bots find_of_day.py reliability design:
per-site state files; an item is marked posted only after its post
succeeds; rotations never duplicate.

Modes:
  feature --site S   next catalog item in rotation -> post (photo when
                     the entry has one, link card when it doesn't)
  guide   --site S   next guide/chart page in rotation -> post
  roundup --site S   "This week on <Brand>" from the last 7 days of
                     feature history -> post (skips if <2 features)
  reader  --site S   keyword search -> like <=8 posts, follow <=3 new
                     authors (daily caps in state); no comments
  keeper             fleet health report -> REPORT.md (no posting)
  substack           watch happyfoods.substack.com/feed -> amplify any
                     NEW post to the fleet's Bluesky + Tumblr (first
                     run seeds existing posts without posting)

Every feature/guide/roundup post is mirrored to the fleet's Tumblr blog
(@happyfoods1, added 2026-10-20 at Dee's order) when the TUMBLR_* env
vars are present — same text (website link included, per Dee's rule),
photo posts by source URL, link posts otherwise. Tumblr post ids are
tracked per site in state so a mirror never duplicates; a failed mirror
logs loudly and never blocks the Bluesky post. The reader stays
Bluesky-only.

Env: BSKY_HANDLE, BSKY_APP_PASSWORD (either absent -> dry-run),
     TUMBLR_CONSUMER_KEY, TUMBLR_CONSUMER_SECRET, TUMBLR_TOKEN,
     TUMBLR_TOKEN_SECRET, TUMBLR_BLOG (absent -> no Tumblr mirror),
     DRY_RUN=1 forces dry-run. State lives in state/<site>.json and is
     committed back by the GitHub Actions workflows.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LIMIT = 300
UA = "push-bots/1.0 (+https://churnmilk.com)"


def log(msg):
    print(f"[push-bots] {msg}", flush=True)


def truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def now():
    return datetime.now(timezone.utc)


def load_site(site):
    with open(os.path.join(HERE, "sites", f"{site}.json"), encoding="utf-8") as f:
        return json.load(f)


def state_path(site):
    return os.path.join(HERE, "state", f"{site}.json")


def load_state(site):
    p = state_path(site)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
    else:
        st = {}
    st.setdefault("feature", {"last_slug": None, "posted": {}})
    st.setdefault("guide", {"last_label": None, "posted": {}})
    st.setdefault("history", [])
    st.setdefault("reader", {"days": {}})
    st.setdefault("tumblr", {"posted": {}})
    return st


def save_state(site, st):
    os.makedirs(os.path.dirname(state_path(site)), exist_ok=True)
    with open(state_path(site), "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ------------------------------------------------------------------ XRPC

def xrpc(method, url, token=None, data=None, content_type="application/json"):
    body = None
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        body = bytes(data) if isinstance(data, (bytes, bytearray)) else json.dumps(data).encode()
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


class Session:
    def __init__(self):
        self.handle = os.environ["BSKY_HANDLE"]
        s = xrpc("POST", "https://bsky.social/xrpc/com.atproto.server.createSession",
                 data={"identifier": self.handle, "password": os.environ["BSKY_APP_PASSWORD"]})
        self.token, self.did = s["accessJwt"], s["did"]

    def post(self, text, embed=None):
        facets = build_facets(text)
        rec = {"$type": "app.bsky.feed.post", "text": text,
               "createdAt": now().isoformat().replace("+00:00", "Z"), "langs": ["en"]}
        if facets:
            rec["facets"] = facets
        if embed:
            rec["embed"] = embed
        out = xrpc("POST", "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                   token=self.token,
                   data={"repo": self.did, "collection": "app.bsky.feed.post", "record": rec})
        return out.get("uri", "")

    def upload(self, img, mime):
        return xrpc("POST", "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
                    token=self.token, data=img, content_type=mime)["blob"]


def build_facets(text):
    """Link facets for URLs and tag facets for #hashtags in the text."""
    facets = []
    import re
    for m in re.finditer(r"https?://[^\s]+", text):
        u = m.group(0).rstrip(".,)")
        facets.append({"index": {"byteStart": len(text[:m.start()].encode()),
                                 "byteEnd": len(text[:m.start()].encode()) + len(u.encode())},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": u}]})
    for m in re.finditer(r"#(\w+)", text):
        facets.append({"index": {"byteStart": len(text[:m.start()].encode()),
                                 "byteEnd": len(text[:m.end()].encode())},
                       "features": [{"$type": "app.bsky.richtext.facet#tag",
                                      "tag": m.group(1)}]})
    return facets


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), r.headers.get_content_type() or "image/jpeg"


def trim_head(head, tail):
    text = head + tail
    if len(text) <= LIMIT:
        return text
    room = LIMIT - len(tail) - 1
    return head[:max(room, 0)].rstrip(" ,;—-") + "…" + tail


def ensure_site_link(text, cfg):
    """Dee's rule (2026-10-19): the website is in EVERY post. If the
    site's domain somehow fell out of a composed post, append the
    site's homepage before it can ship."""
    domain = cfg["site"] + ".com"
    if domain in text:
        return text
    suffix = " " + cfg["base"]
    if len(text) + len(suffix) > LIMIT:
        text = text[: LIMIT - len(suffix) - 1].rstrip() + "…"
    return text + suffix


# ------------------------------------------------------------------ Tumblr

TUMBLR_ENV = ("TUMBLR_CONSUMER_KEY", "TUMBLR_CONSUMER_SECRET",
              "TUMBLR_TOKEN", "TUMBLR_TOKEN_SECRET", "TUMBLR_BLOG")


def tumblr_enabled():
    return all(os.environ.get(k) for k in TUMBLR_ENV)


def post_tumblr(cfg, kind, text, item=None, page=None):
    """Mirror one fleet post to the Happy Foods Tumblr blog via the
    legacy v2 post API (OAuth1), the same pattern as yumrare-bots.
    `text` already carries the site's link (ensure_site_link ran on
    it); the caption also gets a real anchor for the item/page URL."""
    try:
        import requests
        from requests_oauthlib import OAuth1
    except ImportError as e:
        raise RuntimeError(
            "Tumblr mirroring needs `requests` and `requests-oauthlib`"
        ) from e
    auth = OAuth1(*(os.environ[k] for k in TUMBLR_ENV[:4]))
    blog = os.environ["TUMBLR_BLOG"]
    tags = ",".join(list(cfg["hashtags"]) + [cfg["brand"], "happy foods"])
    data = {"tags": tags}
    if kind == "feature" and item is not None:
        caption = text + f'<br><br><a href="{item["url"]}">{item["url"]}</a>'
        if item.get("photo_url"):
            data.update({"type": "photo", "source": item["photo_url"],
                         "caption": caption, "link": item["url"]})
        else:
            data.update({"type": "link", "url": item["url"],
                         "title": item["name"], "description": caption})
    elif kind == "guide" and page is not None:
        data.update({"type": "link", "url": page["url"], "title": page["label"],
                     "description": text +
                     f'<br><br><a href="{page["url"]}">{page["url"]}</a>'})
    else:  # roundup
        data.update({"type": "text", "title": f"This week on {cfg['brand']}",
                     "body": text +
                     f'<br><br><a href="{cfg["base"]}">{cfg["base"]}</a>'})
    r = requests.post(f"https://api.tumblr.com/v2/blog/{blog}/post",
                      auth=auth, data=data, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Tumblr post failed: HTTP {r.status_code} {r.text[:200]}")
    return str(r.json().get("response", {}).get("id", ""))


def tumblr_post_data(data):
    """POST one legacy-API post dict to the fleet Tumblr blog."""
    try:
        import requests
        from requests_oauthlib import OAuth1
    except ImportError as e:
        raise RuntimeError(
            "Tumblr posting needs `requests` and `requests-oauthlib`"
        ) from e
    auth = OAuth1(*(os.environ[k] for k in TUMBLR_ENV[:4]))
    r = requests.post(
        f"https://api.tumblr.com/v2/blog/{os.environ['TUMBLR_BLOG']}/post",
        auth=auth, data=data, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Tumblr post failed: HTTP {r.status_code} {r.text[:200]}")
    return str(r.json().get("response", {}).get("id", ""))


def post_tumblr_link(title, url, text, tags):
    return tumblr_post_data({"type": "link", "url": url, "title": title,
                             "description": text +
                             f'<br><br><a href="{url}">{url}</a>',
                             "tags": ",".join(tags)})


def mirror_tumblr(site, cfg, st, kind, key, text, item=None, page=None):
    """Post the Tumblr mirror once per (kind, key); record in state.
    Never raises — a failed mirror must not sink the Bluesky post."""
    if not tumblr_enabled():
        return
    posted = st["tumblr"]["posted"]
    mkey = f"{kind}:{key}"
    if mkey in posted:
        log(f"  tumblr: {mkey} already mirrored — skipping")
        return
    try:
        pid = post_tumblr(cfg, kind, text, item=item, page=page)
        posted[mkey] = {"at": now().isoformat(), "id": pid}
        log(f"  tumblr mirrored OK: post {pid}")
    except Exception as e:  # noqa: BLE001 — mirror failure is logged, not fatal
        log(f"  tumblr mirror FAILED for {mkey}: {e}")


# ------------------------------------------------------------------ modes

def mode_feature(site, sess, dry):
    cfg, st = load_site(site), load_state(site)
    items = cfg["items"]
    slugs = [i["slug"] for i in items]
    rot = st["feature"]
    if rot["last_slug"] in slugs:
        item = items[(slugs.index(rot["last_slug"]) + 1) % len(items)]
    else:
        item = items[0]
    if item["slug"] in rot["posted"]:
        log(f"{site}: {item['slug']} already posted — rotation inconsistency, skipping")
        return 0
    tags = " ".join("#" + t for t in cfg["hashtags"][:2])
    if site == "overpepper":
        extra = []
        if item.get("price"):
            extra.append(f"${float(item['price']):.2f}")
        if item.get("shu"):
            extra.append(f"{item['shu']} SHU")
        head = f"From Overpepper: {item['name']} — {item['hook']} "
        tail = (" · ".join(extra) + " " if extra else "") + f"{item['url']} {tags}"
    else:
        head = f"From {cfg['brand']}: {item['name']} — {item['hook']} "
        tail = f"{item['url']} {tags}"
    text = ensure_site_link(trim_head(head, tail), cfg)
    log(f"{site} feature target: {item['slug']} ({len(text)} chars): {text}")
    if dry:
        log(f"  photo: {item.get('photo_url') or 'none (link card)'}; dry-run, state untouched")
        return 0
    embed = None
    if item.get("photo_url"):
        try:
            img, mime = download(item["photo_url"])
            blob = sess.upload(img, mime)
            embed = {"$type": "app.bsky.embed.images",
                     "images": [{"alt": item["name"], "image": blob}]}
        except Exception as e:  # noqa: BLE001 — fall back to a link card
            log(f"  photo fetch failed ({e}); using link card")
    if embed is None:
        embed = {"$type": "app.bsky.embed.external",
                 "external": {"uri": item["url"], "title": item["name"],
                              "description": item["hook"][:200]}}
    uri = sess.post(text, embed)
    mirror_tumblr(site, cfg, st, "feature", item["slug"], text, item=item)
    rot["posted"][item["slug"]] = {"at": now().isoformat(), "ref": uri}
    rot["last_slug"] = item["slug"]
    st["history"].append({"kind": "feature", "slug": item["slug"],
                          "name": item["name"], "at": now().isoformat(), "ref": uri})
    save_state(site, st)
    log(f"  posted OK: {uri}")
    return 0


def mode_guide(site, sess, dry):
    cfg, st = load_site(site), load_state(site)
    pages = cfg["pages"]
    labels = [p["label"] for p in pages]
    rot = st["guide"]
    if rot["last_label"] in labels:
        page = pages[(labels.index(rot["last_label"]) + 1) % len(pages)]
    else:
        page = pages[0]
    tags = " ".join("#" + t for t in cfg["hashtags"][:2])
    text = ensure_site_link(trim_head(f"From {cfg['brand']}: {page['label']} — {page['blurb']} ",
                     f"{page['url']} {tags}"), cfg)
    log(f"{site} guide target: {page['label']} ({len(text)} chars): {text}")
    if dry:
        log("  dry-run, state untouched")
        return 0
    embed = {"$type": "app.bsky.embed.external",
             "external": {"uri": page["url"], "title": page["label"],
                          "description": page["blurb"][:200]}}
    uri = sess.post(text, embed)
    mirror_tumblr(site, cfg, st, "guide", page["label"], text, page=page)
    rot["posted"][page["label"]] = {"at": now().isoformat(), "ref": uri}
    rot["last_label"] = page["label"]
    st["history"].append({"kind": "guide", "slug": page["label"],
                          "name": page["label"], "at": now().isoformat(), "ref": uri})
    save_state(site, st)
    log(f"  posted OK: {uri}")
    return 0


def mode_roundup(site, sess, dry):
    cfg, st = load_site(site), load_state(site)
    cutoff = now() - timedelta(days=7)
    recent = [h for h in st["history"]
              if h["kind"] == "feature" and datetime.fromisoformat(h["at"]) >= cutoff]
    if len(recent) < 2:
        log(f"{site} roundup: only {len(recent)} feature(s) this week — skipping")
        return 0
    names = ", ".join(h["name"] for h in recent[-5:])
    tags = " ".join("#" + t for t in cfg["hashtags"][:2])
    text = ensure_site_link(trim_head(f"This week on {cfg['brand']}: {names}. ",
                     f"Catch up: {cfg['base']} {tags}"), cfg)
    log(f"{site} roundup ({len(text)} chars): {text}")
    if dry:
        log("  dry-run, state untouched")
        return 0
    uri = sess.post(text)
    mirror_tumblr(site, cfg, st, "roundup", now().strftime("%Y-%m-%d"), text)
    st["history"].append({"kind": "roundup", "slug": "weekly", "name": "Weekly roundup",
                          "at": now().isoformat(), "ref": uri})
    save_state(site, st)
    log(f"  posted OK: {uri}")
    return 0


def mode_reader(site, sess, dry):
    cfg, st = load_site(site), load_state(site)
    day = now().strftime("%Y-%m-%d")
    counters = st["reader"]["days"].setdefault(day, {"likes": 0, "follows": 0})
    LIKE_CAP, FOLLOW_CAP = 8, 3
    if dry:
        log(f"{site} reader: dry-run (caps: {LIKE_CAP} likes, {FOLLOW_CAP} follows/day; "
            f"today so far: {counters})")
        return 0
    liked, followed = counters["likes"], counters["follows"]
    for kw in cfg["keywords"]:
        if liked >= LIKE_CAP and followed >= FOLLOW_CAP:
            break
        q = urllib.parse.quote(kw)
        try:
            res = xrpc("GET", f"https://bsky.social/xrpc/app.bsky.feed.searchPosts?q={q}&limit=15",
                       token=sess.token)
        except Exception as e:  # noqa: BLE001
            log(f"  search '{kw}' failed: {e}")
            continue
        for p in res.get("posts", []):
            if p["author"]["did"] == sess.did:
                continue
            if liked < LIKE_CAP:
                try:
                    xrpc("POST", "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                         token=sess.token,
                         data={"repo": sess.did, "collection": "app.bsky.feed.like",
                               "record": {"$type": "app.bsky.feed.like",
                                          "subject": {"uri": p["uri"], "cid": p["cid"]},
                                          "createdAt": now().isoformat().replace("+00:00", "Z")}})
                    liked += 1
                except Exception as e:  # noqa: BLE001
                    log(f"  like failed: {e}")
            if followed < FOLLOW_CAP and not p["author"].get("viewer", {}).get("following"):
                try:
                    xrpc("POST", "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                         token=sess.token,
                         data={"repo": sess.did, "collection": "app.bsky.graph.follow",
                               "record": {"$type": "app.bsky.graph.follow",
                                          "subject": p["author"]["did"],
                                          "createdAt": now().isoformat().replace("+00:00", "Z")}})
                    followed += 1
                except Exception as e:  # noqa: BLE001
                    log(f"  follow failed: {e}")
            if liked >= LIKE_CAP and followed >= FOLLOW_CAP:
                break
    counters["likes"], counters["follows"] = liked, followed
    save_state(site, st)
    log(f"{site} reader: liked {liked} total today, followed {followed} today")
    return 0


def mode_substack(sess, dry):
    """Happy Foods Substack watcher (added 2026-10-20, Dee's order):
    read happyfoods.substack.com/feed; any NEW post is amplified to
    the fleet's Bluesky + Tumblr. First-ever run seeds state with the
    existing catalog WITHOUT posting (no back-catalog blast — the same
    seeding rule as the 90s Foodie crossposter). An item is marked
    seen only after its Bluesky post succeeds."""
    import html as html_mod
    import re
    import xml.etree.ElementTree as ET

    feed_url = "https://happyfoods.substack.com/feed"
    spath = os.path.join(HERE, "state", "substack.json")
    seeded = os.path.exists(spath)
    if seeded:
        with open(spath, encoding="utf-8") as f:
            sst = json.load(f)
    else:
        sst = {"seen": {}}
    sst.setdefault("seen", {})

    req = urllib.request.Request(feed_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):
        def txt(tag):
            el = it.find(tag)
            return (el.text or "").strip() if el is not None else ""
        guid = txt("guid") or txt("link")
        desc = re.sub(r"<[^>]+>", " ", txt("description"))
        desc = html_mod.unescape(re.sub(r"\s+", " ", desc)).strip()
        items.append({"guid": guid, "title": txt("title"),
                      "link": txt("link"), "desc": desc})
    log(f"substack: feed carries {len(items)} post(s); "
        f"{len(sst['seen'])} already seen")

    if not seeded:
        for it in items:
            sst["seen"][it["guid"]] = {"at": now().isoformat(), "seeded": True}
        if not dry:
            os.makedirs(os.path.dirname(spath), exist_ok=True)
            with open(spath, "w", encoding="utf-8") as f:
                json.dump(sst, f, indent=2, ensure_ascii=False)
                f.write("\n")
        log(f"substack: seeded {len(items)} existing post(s) — no blast")
        return 0

    new = [it for it in reversed(items) if it["guid"] not in sst["seen"]]
    if not new:
        log("substack: nothing new")
        return 0
    for it in new[:2]:
        head = f"New from Happy Foods: {it['title']} — {it['desc']} "
        text = trim_head(head, it["link"])
        log(f"substack new post: {it['title']} ({len(text)} chars): {text}")
        if dry:
            log("  dry-run, state untouched")
            continue
        embed = {"$type": "app.bsky.embed.external",
                 "external": {"uri": it["link"], "title": it["title"],
                              "description": it["desc"][:200]}}
        uri = sess.post(text, embed)
        entry = {"at": now().isoformat(), "bsky": uri}
        if tumblr_enabled():
            try:
                entry["tumblr"] = post_tumblr_link(
                    it["title"], it["link"], text,
                    ["happy foods", "food blog", "substack"])
                log(f"  tumblr mirrored OK: post {entry['tumblr']}")
            except Exception as e:  # noqa: BLE001
                log(f"  tumblr mirror FAILED: {e}")
        sst["seen"][it["guid"]] = entry
        with open(spath, "w", encoding="utf-8") as f:
            json.dump(sst, f, indent=2, ensure_ascii=False)
            f.write("\n")
        log(f"  amplified OK: {uri}")
    return 0


def mode_keeper():
    lines = [f"# Push-bots fleet report — {now().strftime('%Y-%m-%d %H:%M UTC')}", ""]
    for site in ("churnmilk", "gravytoast", "overpepper"):
        st = load_state(site)
        feats = [h for h in st["history"] if h["kind"] == "feature"]
        last = feats[-1] if feats else None
        mirrors = len(st.get("tumblr", {}).get("posted", {}))
        if last:
            age_h = (now() - datetime.fromisoformat(last["at"])).total_seconds() / 3600
            flag = "STALE (>48h)" if age_h > 48 else "ok"
            lines.append(f"- **{site}**: {len(feats)} features posted; last "
                         f"{last['name']} {age_h:.0f}h ago — {flag}; "
                         f"{mirrors} Tumblr mirrors")
        else:
            lines.append(f"- **{site}**: no features posted yet; "
                         f"{mirrors} Tumblr mirrors")
    report = "\n".join(lines) + "\n"
    with open(os.path.join(HERE, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        log("usage: bot.py <feature|guide|roundup|reader|keeper|substack> [--site S]")
        return 2
    mode = args[0]
    if mode == "keeper":
        return mode_keeper()
    if mode == "substack":
        dry_s = truthy(os.environ.get("DRY_RUN")) or not (
            os.environ.get("BSKY_HANDLE") and os.environ.get("BSKY_APP_PASSWORD"))
        if dry_s and not truthy(os.environ.get("DRY_RUN")):
            log("no Bluesky secrets present — dry-run mode")
        return mode_substack(None if dry_s else Session(), dry_s)
    site = args[args.index("--site") + 1] if "--site" in args else None
    if site not in ("churnmilk", "gravytoast", "overpepper"):
        log("a valid --site is required (churnmilk|gravytoast|overpepper)")
        return 2
    dry = truthy(os.environ.get("DRY_RUN")) or not (
        os.environ.get("BSKY_HANDLE") and os.environ.get("BSKY_APP_PASSWORD"))
    if dry and not truthy(os.environ.get("DRY_RUN")):
        log("no Bluesky secrets present — dry-run mode")
    sess = None
    if not dry:
        sess = Session()
    fn = {"feature": mode_feature, "guide": mode_guide,
          "roundup": mode_roundup, "reader": mode_reader}[mode]
    return fn(site, sess, dry)


if __name__ == "__main__":
    sys.exit(main())
