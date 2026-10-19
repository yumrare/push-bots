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

Env: BSKY_HANDLE, BSKY_APP_PASSWORD (either absent -> dry-run),
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
    text = trim_head(head, tail)
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
    text = trim_head(f"From {cfg['brand']}: {page['label']} — {page['blurb']} ",
                     f"{page['url']} {tags}")
    log(f"{site} guide target: {page['label']} ({len(text)} chars): {text}")
    if dry:
        log("  dry-run, state untouched")
        return 0
    embed = {"$type": "app.bsky.embed.external",
             "external": {"uri": page["url"], "title": page["label"],
                          "description": page["blurb"][:200]}}
    uri = sess.post(text, embed)
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
    text = trim_head(f"This week on {cfg['brand']}: {names}. ",
                     f"Catch up: {cfg['base']} {tags}")
    log(f"{site} roundup ({len(text)} chars): {text}")
    if dry:
        log("  dry-run, state untouched")
        return 0
    uri = sess.post(text)
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


def mode_keeper():
    lines = [f"# Push-bots fleet report — {now().strftime('%Y-%m-%d %H:%M UTC')}", ""]
    for site in ("churnmilk", "gravytoast", "overpepper"):
        st = load_state(site)
        feats = [h for h in st["history"] if h["kind"] == "feature"]
        last = feats[-1] if feats else None
        if last:
            age_h = (now() - datetime.fromisoformat(last["at"])).total_seconds() / 3600
            flag = "STALE (>48h)" if age_h > 48 else "ok"
            lines.append(f"- **{site}**: {len(feats)} features posted; last "
                         f"{last['name']} {age_h:.0f}h ago — {flag}")
        else:
            lines.append(f"- **{site}**: no features posted yet")
    report = "\n".join(lines) + "\n"
    with open(os.path.join(HERE, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        log("usage: bot.py <feature|guide|roundup|reader|keeper> [--site S]")
        return 2
    mode = args[0]
    if mode == "keeper":
        return mode_keeper()
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
