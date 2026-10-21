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

Every feature/guide/roundup post and every Substack amplification is
also mirrored to the fleet's Mastodon account on mastodon.social
(added 2026-10-21 at Dee's order) when MASTODON_ACCESS_TOKEN is
present. Dee's Mastodon rule (2026-10-21): EVERY Mastodon bot post
carries all three websites — churnmilk.com, overpepper.com and
gravytoast.com — plus the Happy Foods Substack blog, appended as a
footer (see MASTO_SITES_FOOTER); new Happy Foods Substack posts are
themselves amplified to Mastodon by the watcher, like Bluesky.
Mastodon's 500-char limit leaves room the 300-char Bluesky text
doesn't. Mastodon post ids are tracked per site in state exactly
like the Tumblr mirror; a failed mirror logs loudly and never
blocks the Bluesky post. The reader stays Bluesky-only.

Every feature/guide/roundup post and every Substack amplification is
also mirrored to Nostr (added 2026-10-21 at Dee's order) when
NOSTR_PRIVATE_KEY is present. The fleet's Nostr identity is
@happyfoods: Nostr has no email/password accounts — the identity is
a keypair, and the private key (hex or nsec) lives only in the repo
secrets, like the Bluesky and Mastodon credentials. Dee's footer
rule applies exactly as on Mastodon (all three websites plus the
Happy Foods Substack blog, reusing MASTO_SITES_FOOTER); a feature
item's photo rides as its URL, which Nostr clients render inline.
Events are BIP-340 Schnorr-signed and published to public relays
with a minimal pure-stdlib WebSocket client, so the workflows need
no extra packages. Event ids are tracked per site in state exactly
like the other mirrors; a failed mirror logs loudly and never
blocks the Bluesky post. The reader stays Bluesky-only.

Env: BSKY_HANDLE, BSKY_APP_PASSWORD (either absent -> dry-run),
     TUMBLR_CONSUMER_KEY, TUMBLR_CONSUMER_SECRET, TUMBLR_TOKEN,
     TUMBLR_TOKEN_SECRET, TUMBLR_BLOG (absent -> no Tumblr mirror),
     MASTODON_ACCESS_TOKEN, MASTODON_BASE (absent -> no Mastodon
     mirror; base defaults to https://mastodon.social),
     NOSTR_PRIVATE_KEY, NOSTR_RELAYS (absent -> no Nostr mirror;
     relays default to NOSTR_RELAYS below),
     DRY_RUN=1 forces dry-run. State lives in state/<site>.json and is
     committed back by the GitHub Actions workflows.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
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
    st.setdefault("mastodon", {"posted": {}})
    st.setdefault("nostr", {"posted": {}})
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


# ------------------------------------------------------------------ Mastodon

MASTODON_LIMIT = 500
# Dee's rule (2026-10-21): every Mastodon bot post includes the
# websites for all three quiet sites, not just the featured one,
# plus the Happy Foods Substack blog (his addendum, same day).
MASTO_SITES_FOOTER = ("Sites: https://churnmilk.com · "
                      "https://overpepper.com · https://gravytoast.com\n"
                      "Blog: https://happyfoods.substack.com")
MASTO_REQUIRED = ("churnmilk.com", "overpepper.com", "gravytoast.com",
                  "happyfoods.substack.com")


def mastodon_enabled():
    return bool(os.environ.get("MASTODON_ACCESS_TOKEN"))


def mastodon_base():
    return (os.environ.get("MASTODON_BASE") or "https://mastodon.social").rstrip("/")


def mastodon_text(text):
    """Append the sites+blog footer unless every required domain is
    already in the text; trim to the Mastodon limit if ever needed."""
    if all(d in text for d in MASTO_REQUIRED):
        return text
    out = text.rstrip() + "\n\n" + MASTO_SITES_FOOTER
    if len(out) > MASTODON_LIMIT:
        room = MASTODON_LIMIT - len(MASTO_SITES_FOOTER) - 3
        out = text[:max(room, 0)].rstrip(" ,;—-") + "…\n\n" + MASTO_SITES_FOOTER
    return out


def _mstdn_request(method, path, fields=None, media=None):
    """One Mastodon API call. `fields` posts form-encoded data;
    `media` = (filename, bytes, mime, description) posts multipart
    to /api/v2/media. Returns parsed JSON."""
    token = os.environ["MASTODON_ACCESS_TOKEN"]
    headers = {"User-Agent": UA, "Authorization": f"Bearer {token}"}
    body = None
    if media is not None:
        import uuid
        fname, blob, mime, desc = media
        boundary = "pushbots" + uuid.uuid4().hex
        parts = []
        if desc:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="description"\r\n\r\n{desc}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="file"; filename="{fname}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'.encode() + blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif fields is not None:
        body = urllib.parse.urlencode(fields, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(mastodon_base() + path, data=body,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def post_mastodon_status(text, item=None):
    """Post one status to the fleet Mastodon account. `text` gets the
    three-sites footer via mastodon_text(); a feature item's photo is
    uploaded and attached when present. Returns the status id."""
    fields = {"status": mastodon_text(text), "visibility": "public",
              "language": "en"}
    if item is not None and item.get("photo_url"):
        try:
            img, mime = download(item["photo_url"])
            ext = {"image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
            media = _mstdn_request(
                "POST", "/api/v2/media",
                media=(f"{item['slug']}.{ext}", img, mime, item["name"]))
            if media.get("id"):
                fields["media_ids[]"] = media["id"]
        except Exception as e:  # noqa: BLE001 — text-only fallback
            log(f"  mastodon photo upload failed ({e}); posting text-only")
    out = _mstdn_request("POST", "/api/v1/statuses", fields=fields)
    return str(out.get("id", ""))


def mirror_mastodon(site, cfg, st, kind, key, text, item=None, page=None):
    """Post the Mastodon mirror once per (kind, key); record in state.
    Never raises — a failed mirror must not sink the Bluesky post.
    (cfg/page are accepted for symmetry with mirror_tumblr; the item
    URL already rides inside `text`, and the footer adds all three
    homepages.)"""
    if not mastodon_enabled():
        return
    posted = st["mastodon"]["posted"]
    mkey = f"{kind}:{key}"
    if mkey in posted:
        log(f"  mastodon: {mkey} already mirrored — skipping")
        return
    try:
        pid = post_mastodon_status(
            text, item=item if kind == "feature" else None)
        posted[mkey] = {"at": now().isoformat(), "id": pid}
        log(f"  mastodon mirrored OK: status {pid}")
    except Exception as e:  # noqa: BLE001 — mirror failure is logged, not fatal
        log(f"  mastodon mirror FAILED for {mkey}: {e}")



# ------------------------------------------------------------------ Nostr

NOSTR_RELAYS = ("wss://relay.damus.io", "wss://nos.lol",
                "wss://relay.nostr.band", "wss://relay.primal.net")

# secp256k1 domain parameters + BIP-340 Schnorr, pure stdlib.
_SP = 2**256 - 2**32 - 977
_SN = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SG = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
       0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _sp_add(Pt, Qt):
    if Pt is None:
        return Qt
    if Qt is None:
        return Pt
    if Pt[0] == Qt[0] and Pt[1] != Qt[1]:
        return None
    if Pt == Qt:
        lam = 3 * Pt[0] * Pt[0] * pow(2 * Pt[1], _SP - 2, _SP) % _SP
    else:
        lam = (Qt[1] - Pt[1]) * pow(Qt[0] - Pt[0], _SP - 2, _SP) % _SP
    x = (lam * lam - Pt[0] - Qt[0]) % _SP
    return (x, (lam * (Pt[0] - x) - Pt[1]) % _SP)


def _sp_mul(k, Pt):
    R = None
    while k:
        if k & 1:
            R = _sp_add(R, Pt)
        Pt = _sp_add(Pt, Pt)
        k >>= 1
    return R


def _b32(i):
    return i.to_bytes(32, "big")


def _tagged_hash(tag, msg):
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def _lift_x(x):
    if x >= _SP:
        raise ValueError("x out of range")
    y_sq = (pow(x, 3, _SP) + 7) % _SP
    y = pow(y_sq, (_SP + 1) // 4, _SP)
    if pow(y, 2, _SP) != y_sq:
        raise ValueError("x is not on the curve")
    return (x, y if y % 2 == 0 else _SP - y)


def _schnorr_sign(msg32, sk_bytes, aux32):
    """BIP-340 Schnorr signature over a 32-byte message."""
    d0 = int.from_bytes(sk_bytes, "big")
    if not 1 <= d0 < _SN:
        raise ValueError("bad secret key")
    Pt = _sp_mul(d0, _SG)
    d = d0 if Pt[1] % 2 == 0 else _SN - d0
    t = _b32(d ^ int.from_bytes(_tagged_hash("BIP0340/aux", aux32), "big"))
    rand = _tagged_hash("BIP0340/nonce", t + _b32(Pt[0]) + msg32)
    k0 = int.from_bytes(rand, "big") % _SN
    if k0 == 0:
        raise RuntimeError("BIP-340 nonce failure (never in practice)")
    Rt = _sp_mul(k0, _SG)
    k = k0 if Rt[1] % 2 == 0 else _SN - k0
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", _b32(Rt[0]) + _b32(Pt[0]) + msg32),
        "big") % _SN
    return _b32(Rt[0]) + _b32((k + e * d) % _SN)


def _schnorr_verify(msg32, pub_hex, sig):
    try:
        Pt = _lift_x(int(pub_hex, 16))
    except ValueError:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= _SP or s >= _SN:
        return False
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", sig[:32] + bytes.fromhex(pub_hex)
                     + msg32), "big") % _SN
    Rt = _sp_add(_sp_mul(s, _SG), _sp_mul(_SN - e, Pt))
    return Rt is not None and Rt[1] % 2 == 0 and Rt[0] == r


_BECH = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    chk = 1
    gen = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= gen[i]
    return chk


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _bech32_hrp_expand(hrp):
    """BIP-173 HRP expansion: high 3 bits of each char, a zero, then
    the low 5 bits. (Bug caught 2026-10-21 by njump's validator: the
    first cut fed raw ord() values into the polymod — self-consistent
    for round-trips but rejected by every real bech32 checker.)"""
    return ([ord(c) >> 5 for c in hrp] + [0]
            + [ord(c) & 31 for c in hrp])


def bech32_encode(hrp, data_bytes):
    """BIP-173 bech32 (Nostr NIP-19 uses it for npub/nsec)."""
    data = _convertbits(data_bytes, 8, 5)
    values = _bech32_hrp_expand(hrp) + data
    poly = _bech32_polymod(values + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH[d] for d in data + checksum)


def bech32_decode(s):
    s = s.strip().lower()
    pos = s.rfind("1")
    hrp = s[:pos]
    data = [_BECH.index(c) for c in s[pos + 1:]]
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise ValueError("bad bech32 checksum")
    return hrp, bytes(_convertbits(data[:-6], 5, 8, pad=False))


def nostr_enabled():
    return bool(os.environ.get("NOSTR_PRIVATE_KEY"))


def _nostr_keys():
    """(secret key bytes, x-only pubkey hex) from NOSTR_PRIVATE_KEY,
    which may be 64-char hex or an nsec."""
    raw = os.environ["NOSTR_PRIVATE_KEY"].strip()
    if raw.startswith("nsec1"):
        _, sk = bech32_decode(raw)
    else:
        sk = bytes.fromhex(raw)
    if len(sk) != 32:
        raise ValueError("NOSTR_PRIVATE_KEY must be 32 bytes (hex or nsec)")
    Pt = _sp_mul(int.from_bytes(sk, "big"), _SG)
    return sk, "%064x" % Pt[0]


def nostr_npub():
    _, pub = _nostr_keys()
    return bech32_encode("npub", bytes.fromhex(pub))


def _nostr_event(kind, content, tags=None):
    """Build + sign a Nostr event (NIP-01). Returns the event dict."""
    sk, pub = _nostr_keys()
    created = int(now().timestamp())
    tags = tags or []
    ser = json.dumps([0, pub, created, kind, tags, content],
                     ensure_ascii=False, separators=(",", ":"))
    eid = hashlib.sha256(ser.encode("utf-8")).digest()
    return {"id": eid.hex(), "pubkey": pub, "created_at": created,
            "kind": kind, "tags": tags, "content": content,
            "sig": _schnorr_sign(eid, sk, os.urandom(32)).hex()}


def _ws_tcp(host, port, timeout):
    """TCP connection to host:port, honoring the standard *_proxy
    env vars via an HTTP CONNECT tunnel when one is configured
    (Nova's VM egresses through a proxy; GitHub Actions does not, and
    there this is a plain direct connection). urllib gets this for
    free; a raw socket must ask."""
    proxies = urllib.request.getproxies()
    no_proxy = [h.strip() for h in proxies.get("no", "").split(",")]
    proxy = None
    if host not in no_proxy:
        proxy = proxies.get("https") or proxies.get("all")
    if not proxy:
        return socket.create_connection((host, port), timeout=timeout)
    pm = urllib.parse.urlparse(proxy)
    sock = socket.create_connection(
        (pm.hostname, pm.port or 3128), timeout=timeout)
    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if pm.username:
        cred = base64.b64encode(
            f"{urllib.parse.unquote(pm.username)}:"
            f"{urllib.parse.unquote(pm.password or '')}".encode()).decode()
        lines.append(f"Proxy-Authorization: Basic {cred}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("proxy closed during CONNECT")
        buf += chunk
    if b" 200" not in buf.split(b"\r\n")[0]:
        raise ConnectionError(
            f"proxy CONNECT failed: {buf.splitlines()[0]!r}")
    return sock


class _WS:
    """Minimal WebSocket client (RFC 6455) — just enough to publish
    one event and read the relay's OK. Client frames are masked as
    the spec requires."""

    def __init__(self, url, timeout=10):
        m = urllib.parse.urlparse(url)
        host = m.hostname
        port = m.port or (443 if m.scheme == "wss" else 80)
        path = m.path or "/"
        if m.query:
            path += "?" + m.query
        raw = _ws_tcp(host, port, timeout)
        if m.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(
                raw, server_hostname=host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"User-Agent: {UA}\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("relay closed during handshake")
            buf += chunk
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        if b" 101" not in head.split(b"\r\n")[0]:
            raise ConnectionError(
                f"relay handshake failed: {head.splitlines()[0]!r}")

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        ln = len(payload)
        if ln < 126:
            head = bytes([0x80 | opcode, 0x80 | ln])
        elif ln < 65536:
            head = bytes([0x80 | opcode, 0xFE]) + struct.pack(">H", ln)
        else:
            head = bytes([0x80 | opcode, 0xFF]) + struct.pack(">Q", ln)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send_text(self, text):
        self._send_frame(1, text.encode("utf-8"))

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("relay closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_msg(self, timeout):
        self.sock.settimeout(timeout)
        frag = b""
        while True:
            b0, b1 = self._read_exact(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            if b1 & 0x80:
                self._read_exact(4)
            payload = self._read_exact(ln) if ln else b""
            if op == 9:  # ping -> pong, keep waiting
                self._send_frame(10, payload)
                continue
            if op == 8:
                raise ConnectionError("relay closed the socket")
            frag += payload
            if fin:
                return frag.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


def nostr_relays():
    raw = os.environ.get("NOSTR_RELAYS", "").strip()
    if raw:
        return tuple(r.strip() for r in raw.split(",") if r.strip())
    return NOSTR_RELAYS


def nostr_publish(ev):
    """Publish one signed event to every configured relay; returns
    how many accepted it. Raises if none did."""
    ok = 0
    errors = []
    for url in nostr_relays():
        ws = None
        try:
            ws = _WS(url)
            ws.send_text(json.dumps(["EVENT", ev]))
            deadline = time.time() + 10
            while time.time() < deadline:
                arr = json.loads(ws.recv_msg(max(1.0, deadline - time.time())))
                if arr and arr[0] == "OK" and arr[1] == ev["id"]:
                    if len(arr) > 2 and arr[2]:
                        ok += 1
                    else:
                        errors.append(
                            f"{url}: rejected "
                            f"({arr[3] if len(arr) > 3 else 'no reason'})")
                    break
        except Exception as e:  # noqa: BLE001 — try the next relay
            errors.append(f"{url}: {e}")
        finally:
            if ws is not None:
                ws.close()
    if ok == 0:
        raise RuntimeError("no relay accepted the event ("
                           + "; ".join(errors) + ")")
    return ok


def nostr_text(text, item=None):
    """Dee's footer rule, same as Mastodon: append the three-sites +
    Substack footer unless every required domain is already present.
    A feature item's photo rides as its URL (Nostr clients render
    bare image URLs inline). No length trim — relays take long
    notes, unlike Mastodon's 500 chars."""
    out = text.rstrip()
    if not all(d in out for d in MASTO_REQUIRED):
        out += "\n\n" + MASTO_SITES_FOOTER
    if item is not None and item.get("photo_url") \
            and item["photo_url"] not in out:
        out += "\n" + item["photo_url"]
    return out


def post_nostr_note(text, item=None):
    """Publish one kind-1 note. Returns the event id."""
    ev = _nostr_event(1, nostr_text(text, item=item))
    got = nostr_publish(ev)
    log(f"  nostr: accepted by {got} relay(s)")
    return ev["id"]


def mirror_nostr(site, cfg, st, kind, key, text, item=None, page=None):
    """Post the Nostr mirror once per (kind, key); record in state.
    Never raises — a failed mirror must not sink the Bluesky post.
    (cfg/page accepted for symmetry with the other mirrors.)"""
    if not nostr_enabled():
        return
    posted = st["nostr"]["posted"]
    mkey = f"{kind}:{key}"
    if mkey in posted:
        log(f"  nostr: {mkey} already mirrored — skipping")
        return
    try:
        eid = post_nostr_note(text, item=item if kind == "feature" else None)
        posted[mkey] = {"at": now().isoformat(), "id": eid}
        log(f"  nostr mirrored OK: event {eid}")
    except Exception as e:  # noqa: BLE001 — mirror failure is logged, not fatal
        log(f"  nostr mirror FAILED for {mkey}: {e}")


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
    mirror_mastodon(site, cfg, st, "feature", item["slug"], text, item=item)
    mirror_nostr(site, cfg, st, "feature", item["slug"], text, item=item)
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
    mirror_mastodon(site, cfg, st, "guide", page["label"], text, page=page)
    mirror_nostr(site, cfg, st, "guide", page["label"], text, page=page)
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
    mirror_mastodon(site, cfg, st, "roundup", now().strftime("%Y-%m-%d"), text)
    mirror_nostr(site, cfg, st, "roundup", now().strftime("%Y-%m-%d"), text)
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


def _substack_feed():
    """Load the watcher state and fetch the Happy Foods feed.
    The fetch is retry-hardened (2026-10-20, Dee's order): Substack
    intermittently 403s datacenter/WARP egress IPs; a single flake
    must not kill the run — retry the fetch+parse with backoff.
    Returns (state, items, seeded_flag, state_path)."""
    import html as html_mod
    import re
    import time
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

    root = None
    last_err = None
    for attempt, wait in enumerate((0, 5, 15, 30), start=1):
        if wait:
            log(f"substack: fetch retry {attempt} after {wait}s "
                f"(last error: {last_err})")
            time.sleep(wait)
        try:
            req = urllib.request.Request(feed_url,
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                root = ET.fromstring(r.read())
            break
        except Exception as e:  # noqa: BLE001 - retry any fetch/parse flake
            last_err = e
    if root is None:
        raise RuntimeError(f"substack feed fetch failed after retries: "
                           f"{last_err}")
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
    return sst, items, seeded, spath


def _substack_seed(sst, items, spath, dry):
    """First-ever run: mark the existing catalog seen, no blast."""
    for it in items:
        sst["seen"][it["guid"]] = {"at": now().isoformat(), "seeded": True}
    if not dry:
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        with open(spath, "w", encoding="utf-8") as f:
            json.dump(sst, f, indent=2, ensure_ascii=False)
            f.write("\n")
    log(f"substack: seeded {len(items)} existing post(s) — no blast")


def mode_substack(sess, dry):
    """Happy Foods Substack watcher (added 2026-10-20, Dee's order):
    read happyfoods.substack.com/feed; any NEW post is amplified to
    the fleet's Bluesky + Tumblr. First-ever run seeds state with the
    existing catalog WITHOUT posting (no back-catalog blast — the same
    seeding rule as the 90s Foodie crossposter). An item is marked
    seen only after its Bluesky post succeeds."""
    sst, items, seeded, spath = _substack_feed()

    if not seeded:
        _substack_seed(sst, items, spath, dry)
        return 0

    new = [it for it in reversed(items) if it["guid"] not in sst["seen"]]
    if not new:
        log("substack: nothing new")
        return 0
    _substack_amplify(sess, sst, spath, new, dry)
    return 0


def _substack_amplify(sess, sst, spath, new, dry):
    """Post up to 2 new items to Bluesky (+ Tumblr mirror); an item is
    marked seen only after its Bluesky post succeeds.
    Returns the list of guids actually posted."""
    import time
    posted = []
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
        # Post with retries (hardened 2026-10-20, Dee's order): a
        # single read timeout killed run #7's post after the fetch
        # had already succeeded — retry the post with backoff.
        uri = None
        last_err = None
        for attempt, wait in enumerate((0, 10, 20), start=1):
            if wait:
                log(f"substack: bsky post retry {attempt} after {wait}s "
                    f"(last error: {last_err})")
                time.sleep(wait)
            try:
                uri = sess.post(text, embed)
                break
            except Exception as e:  # noqa: BLE001 - retry any post flake
                last_err = e
        if uri is None:
            raise RuntimeError(f"bsky post failed after retries: {last_err}")
        entry = {"at": now().isoformat(), "bsky": uri}
        if tumblr_enabled():
            try:
                entry["tumblr"] = post_tumblr_link(
                    it["title"], it["link"], text,
                    ["happy foods", "food blog", "substack"])
                log(f"  tumblr mirrored OK: post {entry['tumblr']}")
            except Exception as e:  # noqa: BLE001
                log(f"  tumblr mirror FAILED: {e}")
        if mastodon_enabled():
            try:
                entry["mastodon"] = post_mastodon_status(text)
                log(f"  mastodon mirrored OK: status {entry['mastodon']}")
            except Exception as e:  # noqa: BLE001
                log(f"  mastodon mirror FAILED: {e}")
        if nostr_enabled():
            try:
                entry["nostr"] = post_nostr_note(text)
                log(f"  nostr mirrored OK: event {entry['nostr']}")
            except Exception as e:  # noqa: BLE001
                log(f"  nostr mirror FAILED: {e}")
        sst["seen"][it["guid"]] = entry
        with open(spath, "w", encoding="utf-8") as f:
            json.dump(sst, f, indent=2, ensure_ascii=False)
            f.write("\n")
        log(f"  amplified OK: {uri}")
        posted.append(it["guid"])
    return posted


def mode_substack_fetch(dry):
    """Phase 1 (runs under the WARP tunnel in the workflow): fetch the
    feed and stash new items in state/pending.json. No Bluesky session
    is created — Bluesky traffic must NOT ride the tunnel (TLS
    handshake and read timeouts there killed runs #7 and #8,
    2026-10-20)."""
    sst, items, seeded, spath = _substack_feed()
    ppath = os.path.join(HERE, "state", "pending.json")
    if not seeded:
        _substack_seed(sst, items, spath, dry)
        pending = []
    else:
        pending = [it for it in reversed(items)
                   if it["guid"] not in sst["seen"]]
    if dry:
        log(f"substack fetch: {len(pending)} pending (dry-run, "
            f"pending.json untouched)")
        return 0
    os.makedirs(os.path.dirname(ppath), exist_ok=True)
    with open(ppath, "w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2, ensure_ascii=False)
        f.write("\n")
    log(f"substack fetch: {len(pending)} pending item(s) stashed")
    return 0


def mode_substack_post(sess, dry):
    """Phase 2 (runs on the open network): amplify whatever the fetch
    phase stashed in state/pending.json, then prune what posted."""
    spath = os.path.join(HERE, "state", "substack.json")
    ppath = os.path.join(HERE, "state", "pending.json")
    if os.path.exists(spath):
        with open(spath, encoding="utf-8") as f:
            sst = json.load(f)
    else:
        sst = {"seen": {}}
    sst.setdefault("seen", {})
    pending = []
    if os.path.exists(ppath):
        with open(ppath, encoding="utf-8") as f:
            pending = json.load(f)
    pending = [it for it in pending if it["guid"] not in sst["seen"]]
    if not pending:
        log("substack post: nothing pending")
        return 0
    posted = _substack_amplify(sess, sst, spath, pending, dry)
    if not dry:
        done = set(posted)
        remaining = [it for it in pending if it["guid"] not in done]
        with open(ppath, "w", encoding="utf-8") as f:
            json.dump(remaining, f, indent=2, ensure_ascii=False)
            f.write("\n")
        log(f"substack post: {len(posted)} amplified, "
            f"{len(remaining)} still pending")
    return 0


def mode_keeper():
    lines = [f"# Push-bots fleet report — {now().strftime('%Y-%m-%d %H:%M UTC')}", ""]
    for site in ("churnmilk", "gravytoast", "overpepper"):
        st = load_state(site)
        feats = [h for h in st["history"] if h["kind"] == "feature"]
        last = feats[-1] if feats else None
        mirrors = len(st.get("tumblr", {}).get("posted", {}))
        m_mirrors = len(st.get("mastodon", {}).get("posted", {}))
        n_mirrors = len(st.get("nostr", {}).get("posted", {}))
        if last:
            age_h = (now() - datetime.fromisoformat(last["at"])).total_seconds() / 3600
            flag = "STALE (>48h)" if age_h > 48 else "ok"
            lines.append(f"- **{site}**: {len(feats)} features posted; last "
                         f"{last['name']} {age_h:.0f}h ago — {flag}; "
                         f"{mirrors} Tumblr mirrors; {m_mirrors} Mastodon mirrors; "
                         f"{n_mirrors} Nostr mirrors")
        else:
            lines.append(f"- **{site}**: no features posted yet; "
                         f"{mirrors} Tumblr mirrors; {m_mirrors} Mastodon mirrors; "
                         f"{n_mirrors} Nostr mirrors")
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
        if "--fetch" in args:
            # Fetch phase never creates a Bluesky session, so the
            # missing-secrets dry-run rule must NOT apply to it (bug
            # in run #9, 2026-10-20: fetch ran dry and stashed
            # nothing). Only an explicit DRY_RUN makes fetch dry.
            return mode_substack_fetch(truthy(os.environ.get("DRY_RUN")))
        if dry_s and not truthy(os.environ.get("DRY_RUN")):
            log("no Bluesky secrets present — dry-run mode")
        sess = None if dry_s else Session()
        if "--post" in args:
            return mode_substack_post(sess, dry_s)
        return mode_substack(sess, dry_s)
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
