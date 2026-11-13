#!/usr/bin/env python3
"""SaveServ: save sync server for game launchers, recomps and emulators.

Accounts own saves. A device pairs to an account with a PIN:
  1. device  POST /api/pair {"name": "RG35XXSP"}  -> {"id", "pin", "expires"}
  2. the user logs in on the website, clicks the + cover, types the PIN
  3. device  GET /api/pair/<id>  -> 202 while pending, 200 {"token"} once paired
Then every device call carries "Authorization: Bearer <token>".

Saves are addressed as <game>/<slot>: a recomp uses its game id and version
(ffta/remix); an emulator folder uses the system and ROM name (gba/Golden Sun
(USA)). Every upload is kept as a new revision (the last KEEP per slot):
  GET  /api/saves/<game>               {"slot": {"rev","sha256","size","updated","device"}}
  GET  /api/saves/<game>/<slot>        bytes (headers X-Rev, X-Sha256)
  PUT  /api/saves/<game>/<slot>?base=N bytes; 409 unless N is the current rev (0 = none);
                                       410 if these exact bytes were deleted on the website
                                       and the slot is still empty (&undelete=1 overrides)
  PUT  /api/meta/<game>                {"title", "system", "rom", "slots": {"remix": "Remix"}}
       optional: how the website names the game and finds its box art

A memory card folder (PCSX2's folder cards, Dolphin's GCI folders) is a game
whose meta system is "ps2" or "gc": each save on the card is a slot (the PS2
save's folder name, or the GameCube save's maker-gamecode-filename), named by
the save's own title, and shown as its own cover.

The website (server/web/) is a single page that talks to /web/* with the
session cookie. Covers come from, in order: an uploaded cover for the save,
an uploaded cover for the game, libretro box art (system + ROM name), a search
by title (Steam, SteamGridDB, IGDB; exact titles only), and a generated tile.

Standard library only. Configuration (environment):
  SAVESERV_DATA       data directory (default /data)
  SAVESERV_PORT       listen port (default 8787)
  SAVESERV_REGISTER   "open" lets anyone register; "first" (default) only the
                      first account, later accounts are added by the owner
  SAVESERV_KEEP       revisions kept per slot (default 50)
  SAVESERV_THUMBNAILS "0" turns off every online cover lookup
  SAVESERV_STEAMGRIDDB_KEY          adds SteamGridDB to the title search
  SAVESERV_IGDB_ID, SAVESERV_IGDB_SECRET   a Twitch app's: adds IGDB to it
  SAVESERV_URL        the address devices should use (a reverse proxy's name);
                      announced in /api/hello and to discovery probes

Discovery: a UDP datagram "SAVESERV?" to the listen port (UDP) is answered
with {"service": "saveserv", "version", "url", "port"}. Devices send it to
every address of their network, so it works through Docker's port publishing
("8787:8787/udp"), where broadcasts and multicast do not arrive.
"""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

DATA = os.environ.get("SAVESERV_DATA", "/data")
PORT = int(os.environ.get("SAVESERV_PORT", "8787"))
REGISTER = os.environ.get("SAVESERV_REGISTER", "first")
KEEP = int(os.environ.get("SAVESERV_KEEP", "50"))
THUMBNAILS = os.environ.get("SAVESERV_THUMBNAILS", "1") != "0"
# The address devices should use (a reverse proxy's name), announced in /api/hello so a device that found the server
# by scanning the network switches to it. Empty: the address the device used.
URL = os.environ.get("SAVESERV_URL", "").strip().rstrip("/")
STEAMGRIDDB_KEY = os.environ.get("SAVESERV_STEAMGRIDDB_KEY", "")
IGDB_ID = os.environ.get("SAVESERV_IGDB_ID", "")
IGDB_SECRET = os.environ.get("SAVESERV_IGDB_SECRET", "")
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_SAVE = 16 << 20
MAX_IMAGE = 3 << 20
MAX_DELETED = 200  # hashes remembered per slot, newest kept
PIN_TTL = 600
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Slots may be ROM-style file names ("Golden Sun (USA)"), but no path parts.
SLOT = re.compile(r"^(?!\.\.?$)[^/\\\x00-\x1f]{1,200}$")
USER = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

# libretro-thumbnails system folders for common save "games".
SYSTEMS = {
    "gba": "Nintendo - Game Boy Advance", "gb": "Nintendo - Game Boy",
    "gbc": "Nintendo - Game Boy Color", "nes": "Nintendo - Nintendo Entertainment System",
    "snes": "Nintendo - Super Nintendo Entertainment System", "n64": "Nintendo - Nintendo 64",
    "nds": "Nintendo - Nintendo DS", "md": "Sega - Mega Drive - Genesis",
    "genesis": "Sega - Mega Drive - Genesis", "sms": "Sega - Master System - Mark III",
    "gg": "Sega - Game Gear", "psx": "Sony - PlayStation", "pce": "NEC - PC Engine - TurboGrafx 16",
}

# Memory card systems (a game's meta system): badge, libretro-thumbnails folder.
CARDS = {"ps2": ("PS2", "Sony - PlayStation 2"), "gc": ("GC", "Nintendo - GameCube")}

os.makedirs(os.path.join(DATA, "blobs"), exist_ok=True)
DB_LOCK = threading.Lock()
DB = sqlite3.connect(os.path.join(DATA, "saveserv.db"), check_same_thread=False)
DB.executescript("""
create table if not exists users(id integer primary key, name text unique, pw text, created int);
create table if not exists sessions(token text primary key, user int, created int);
create table if not exists devices(id integer primary key, user int, name text, token text unique,
                                   created int, seen int);
create table if not exists revs(user int, game text, slot text, rev int, sha text, size int,
                                device text, created int, primary key(user, game, slot, rev));
create table if not exists meta(user int, game text, slot text, title text, cover text,
                                primary key(user, game, slot));
create table if not exists games(user int, game text, title text, system text, rom text,
                                 primary key(user, game));
create table if not exists thumbs(key text primary key, sha text, fetched int);
create table if not exists deleted(user int, game text, slot text, sha text, created int,
                                   primary key(user, game, slot, sha));
""")
for table, column in (("users", "display text"), ("users", "avatar text"),
                      ("sessions", "agent text"), ("sessions", "seen int")):
    try:
        DB.execute(f"alter table {table} add column {column}")
    except sqlite3.OperationalError:
        pass  # already there
DB.commit()
# Pending pairings live in memory: id -> {pin, name, expires, token, account}
PAIRS = {}
PAIRS_LOCK = threading.Lock()


def db(sql, args=(), one=False):
    with DB_LOCK:
        cur = DB.execute(sql, args)
        rows = cur.fetchall()
        DB.commit()
    return (rows[0] if rows else None) if one else rows


def sha(b):
    return hashlib.sha256(b).hexdigest()


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def blob_path(digest):
    return os.path.join(DATA, "blobs", digest)


def put_blob(data):
    digest = sha(data)
    if not os.path.exists(blob_path(digest)):
        tmp = blob_path(digest) + f".{secrets.token_hex(4)}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, blob_path(digest))
    return digest


def drop_blob_if_unused(digest):
    if not digest:
        return
    for q in ("select 1 from revs where sha=? limit 1", "select 1 from meta where cover=? limit 1",
              "select 1 from users where avatar=? limit 1", "select 1 from thumbs where sha=? limit 1"):
        if db(q, (digest,)):
            return
    try:
        os.remove(blob_path(digest))
    except FileNotFoundError:
        pass


def prune(user, game, slot):
    old = db("select rev, sha from revs where user=? and game=? and slot=? order by rev desc limit -1 offset ?",
             (user, game, slot, KEEP))
    for rev, digest in old:
        db("delete from revs where user=? and game=? and slot=? and rev=?", (user, game, slot, rev))
        drop_blob_if_unused(digest)


def delete_slot(user, game, slot):
    """A save and every revision of it, gone for good. Returns how many revisions
    went, 0 if there was no such save. Once a game has no saves left, its name
    and covers go too. Website only: a device token can't do this."""
    shas = [r[0] for r in db("select sha from revs where user=? and game=? and slot=?", (user, game, slot))]
    if not shas:
        return 0
    covers = [r[0] for r in db("select cover from meta where user=? and game=? and slot=?", (user, game, slot))]
    # Only the hashes stay, so a device still holding one of these saves doesn't
    # bring it straight back. A new save has a new hash, and uploads as normal.
    now = int(time.time())
    for digest in set(shas):
        db("insert or replace into deleted values(?,?,?,?,?)", (user, game, slot, digest, now))
    db("""delete from deleted where user=? and game=? and slot=? and sha not in
          (select sha from deleted where user=? and game=? and slot=? order by created desc limit ?)""",
       (user, game, slot, user, game, slot, MAX_DELETED))
    db("delete from revs where user=? and game=? and slot=?", (user, game, slot))
    db("delete from meta where user=? and game=? and slot=?", (user, game, slot))
    if not db("select 1 from revs where user=? and game=? limit 1", (user, game)):
        covers += [r[0] for r in db("select cover from meta where user=? and game=?", (user, game))]
        db("delete from meta where user=? and game=?", (user, game))
        db("delete from games where user=? and game=?", (user, game))
    for digest in set(shas + covers):
        drop_blob_if_unused(digest)
    return len(shas)


IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
               (b"GIF8", "image/gif"), (b"RIFF", "image/webp"))


def image_type(data):
    for magic, kind in IMAGE_MAGIC:
        if data.startswith(magic) and (kind != "image/webp" or data[8:12] == b"WEBP"):
            return kind
    return None


INDEX = {}  # libretro folder -> (fetched, [names])
INDEX_LOCK = threading.Lock()


def fetch(url, limit, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "SaveServ", **(headers or {})})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read(limit + 1)


def base_title(name):
    return re.sub(r"\s*[(\[][^)\]]*[)\]]", "", name).strip().lower()


def best_match(folder, rom):
    """Closest box art name when the exact ROM name has none: same title
    without tags, preferring USA, World, Europe, Japan; never Virtual Console."""
    with INDEX_LOCK:
        when, names = INDEX.get(folder, (0, []))
        if time.time() - when > 86400:
            try:
                page = fetch(f"https://thumbnails.libretro.com/{quote(folder)}/Named_Boxarts/", 16 << 20).decode("utf-8", "replace")
                names = sorted({unquote(n) for n in re.findall(r'href="([^"/?]+)\.png"', page)})
            except Exception as e:
                print(f"thumbnail index {folder}: {e}", flush=True)
            INDEX[folder] = (time.time(), names)
    want = base_title(rom)
    cands = [n for n in names if base_title(n) == want and "Virtual Console" not in n]
    for region in ("(USA", "(World", "(Europe", "(Japan", ""):
        hit = next((n for n in cands if region in n), None)
        if hit:
            return hit
    return None


def libretro_art(system, rom):
    """Box art from thumbnails.libretro.com, cached (also misses, for a day)."""
    folder = SYSTEMS.get(system) or CARDS.get(system, (None, None))[1]
    if not (THUMBNAILS and folder and rom):
        return None
    key = f"{folder}/{rom}"
    row = db("select sha, fetched from thumbs where key=?", (key,), one=True)
    if row and (row[0] or time.time() - row[1] < 86400):
        return row[0] or None
    digest = ""
    for name in (rom, None):
        if name is None:
            name = best_match(folder, rom)
            if not name or name == rom:
                break
        url = f"https://thumbnails.libretro.com/{quote(folder)}/Named_Boxarts/{quote(re.sub(r'[&*/:`<>?|\\\"]', '_', name))}.png"
        try:
            data = fetch(url, MAX_IMAGE)
            if len(data) <= MAX_IMAGE and image_type(data):
                digest = put_blob(data)
                break
        except Exception as e:  # offline, 404: try the closest name, then a generated tile
            print(f"thumbnail {url}: {e}", flush=True)
    db("insert or replace into thumbs values(?,?,?)", (key, digest, int(time.time())))
    return digest or None


# ---- covers by title --------------------------------------------------------
# Only the game's title leaves this server, and a cover is used only when a
# source's name for the game is the same title: a wrong cover is worse than initials.

SCRAPE_LOCK = threading.Lock()  # one lookup at a time: slots of a game share it, and a new page can't hammer anyone
SOURCE_DOWN = {}  # source -> time to try it again (so an outage costs one timeout, not one per cover)
IGDB_TOKEN = ("", 0)  # token, expires


def title_key(title):
    """What two names must share to be the same game: no accents, case or punctuation."""
    t = re.sub("[\u2122\u2120\u00ae\u00a9]", "", title)  # (before NFKD turns the TM sign into letters)
    t = unicodedata.normalize("NFKD", t).casefold().replace("&", "and")
    return re.sub(r"[\W_]+", "", t)


def same_title(name, title):
    return bool(name) and title_key(name) == title_key(title)


def fetch_json(url, headers=None, data=None):
    try:
        return json.loads(fetch(url, 1 << 20, headers, data))
    except urllib.error.HTTPError as e:
        if e.code == 404:  # (SteamGridDB's answer for no such game)
            return {}
        raise


def first_image(urls, host_suffix):
    """The first of these that's really an image of a sane size. They come from
    the source's answer, so only its own image host over https is fetched."""
    for url in urls:
        parsed = urlparse(url or "")
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host == host_suffix or host.endswith("." + host_suffix)):
            continue
        try:
            data = fetch(url, MAX_IMAGE)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # (not every game has every picture)
                continue
            raise
        if len(data) <= MAX_IMAGE and image_type(data):
            return data
    return None


def steam_cover(title):
    found = fetch_json(f"https://store.steampowered.com/api/storesearch/?term={quote(title, safe='')}&l=english&cc=US")
    for item in found.get("items", []):
        if item.get("type") == "app" and same_title(item.get("name"), title):
            # (The search's own picture has a hash in its path that's that picture's alone,
            # so it can't lead to the others. It's small and wide: the last resort.)
            base = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{int(item['id'])}"
            return first_image([f"{base}/library_600x900.jpg", f"{base}/header.jpg", item.get("tiny_image")], "steamstatic.com")
    return None


def steamgriddb_cover(title):
    api, auth = "https://www.steamgriddb.com/api/v2", {"Authorization": f"Bearer {STEAMGRIDDB_KEY}"}
    for game in fetch_json(f"{api}/search/autocomplete/{quote(title, safe='')}", auth).get("data", []):
        if same_title(game.get("name"), title):
            for shape in ("&dimensions=512x512,1024x1024", ""):  # the tiles are square; else whatever exists
                grids = fetch_json(f"{api}/grids/game/{int(game['id'])}?types=static&nsfw=false&humor=false{shape}", auth).get("data", [])
                art = first_image([u for g in grids[:3] for u in (g.get("url"), g.get("thumb"))], "steamgriddb.com")
                if art:
                    return art
            return None
    return None


def igdb_cover(title):
    global IGDB_TOKEN
    if time.time() >= IGDB_TOKEN[1]:
        # (The secret goes in the body: URLs end up in logs.)
        form = f"client_id={quote(IGDB_ID, safe='')}&client_secret={quote(IGDB_SECRET, safe='')}&grant_type=client_credentials"
        got = fetch_json("https://id.twitch.tv/oauth2/token", data=form.encode())
        IGDB_TOKEN = (got["access_token"], time.time() + int(got.get("expires_in", 3600)) - 60)
    term = re.sub(r'["\\\x00-\x1f]', " ", title)
    query = f'search "{term}"; fields name,alternative_names.name,cover.image_id; where cover != null; limit 20;'
    try:
        games = fetch_json("https://api.igdb.com/v4/games", {"Client-ID": IGDB_ID, "Authorization": f"Bearer {IGDB_TOKEN[0]}"}, query.encode())
    except urllib.error.HTTPError as e:
        if e.code == 401:  # revoked early: ask again next time
            IGDB_TOKEN = ("", 0)
        raise
    for game in games if isinstance(games, list) else []:
        names = [game.get("name")] + [a.get("name") for a in game.get("alternative_names", [])]
        image = (game.get("cover") or {}).get("image_id")
        if image and any(same_title(n, title) for n in names):
            return first_image([f"https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{quote(image, safe='')}.jpg"], "igdb.com")
    return None


def title_sources(retro):
    """In order. Steam has no key to set, but has no old console games: for those,
    its same-named remake or port would be the wrong cover."""
    sources = [] if retro else [("steam", steam_cover)]
    if STEAMGRIDDB_KEY:
        sources.append(("steamgriddb", steamgriddb_cover))
    if IGDB_ID and IGDB_SECRET:
        sources.append(("igdb", igdb_cover))
    return sources


def title_art(title, retro=False):
    """A cover found by title, cached (misses for a day, or an hour when a source
    failed). The sources are part of the key: adding one looks again at once."""
    sources = title_sources(retro)
    wanted = title_key(title)
    if not (THUMBNAILS and sources and wanted):
        return None
    key = f"title/{'+'.join(n for n, _ in sources)}/{wanted}"

    def cached():
        row = db("select sha, fetched from thumbs where key=?", (key,), one=True)
        return row and (row[0] or time.time() - row[1] < 86400), row[0] if row else None

    known, digest = cached()
    if known:
        return digest or None
    with SCRAPE_LOCK:
        known, digest = cached()
        if known:
            return digest or None
        digest, failed, now = "", False, time.time()
        for name, find in sources:
            if SOURCE_DOWN.get(name, 0) > now:
                failed = True
                continue
            try:
                art = find(title)
            except Exception as e:  # offline, rate limited, a bad key: the next source, then initials
                print(f"cover {name}: {type(e).__name__}: {e}", flush=True)
                SOURCE_DOWN[name], failed = now + 300, True
                continue
            if art:
                digest = put_blob(art)
                break
        retry_at = now - 86400 + 3600 if failed and not digest else now
        db("insert or replace into thumbs values(?,?,?)", (key, digest, int(retry_at)))
    return digest or None


def placeholder_svg(title, seed):
    hue = int(sha(seed.encode())[:4], 16) % 360
    words = [w for w in re.split(r"[\s_\-:()]+", title) if w and w[0].isalnum()]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="hsl({hue},45%,38%)"/><stop offset="1" stop-color="hsl({(hue + 40) % 360},50%,22%)"/>
</linearGradient></defs><rect width="256" height="256" fill="url(#g)"/>
<text x="128" y="150" font-family="system-ui,sans-serif" font-size="96" font-weight="700"
 text-anchor="middle" fill="rgba(255,255,255,.85)">{initials}</text></svg>"""


def pretty(word):
    return re.sub(r"[_-]+", " ", word).strip().title() if word.islower() else word


class Handler(BaseHTTPRequestHandler):
    server_version = "saveserv/2"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    # ---- helpers -----------------------------------------------------------
    def send(self, code, body=b"", ctype="application/json", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def err(self, code, msg):
        return self.send(code, {"error": msg})

    def redirect(self, where, headers=()):
        self.send(303, b"", "text/plain", [("Location", where), *headers])

    def body(self, limit=MAX_SAVE):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            # Read it away (up to a bound), so the client, still sending, hears why instead of a reset connection.
            left = n if n <= 4 * MAX_SAVE else 0
            while left > 0:
                got = self.rfile.read(min(left, 1 << 20))
                if not got:
                    break
                left -= len(got)
            if n > 4 * MAX_SAVE:
                self.close_connection = True
            raise ValueError("too large")
        return self.rfile.read(n) if n else b""

    def json_body(self):
        try:
            v = json.loads(self.body(65536) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("bad JSON")
        if not isinstance(v, dict):
            raise ValueError("expected an object")
        return v

    def session(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        if "sid" not in c:
            return None
        tok = sha(c["sid"].value.encode())
        row = db("select u.id, u.name, u.display, u.avatar from sessions s join users u on u.id=s.user "
                 "where s.token=?", (tok,), one=True)
        if row:
            db("update sessions set seen=? where token=?", (int(time.time()), tok))
            return {"id": row[0], "name": row[1], "display": row[2] or row[1], "avatar": row[3], "token": tok}
        return None

    def device(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        row = db("select id, user, name from devices where token=?", (sha(auth[7:].strip().encode()),), one=True)
        if row:
            db("update devices set seen=? where id=?", (int(time.time()), row[0]))
        return row

    def login(self, uid):
        tok = secrets.token_urlsafe(32)
        agent = (self.headers.get("User-Agent") or "")[:200]
        now = int(time.time())
        db("insert into sessions(token, user, created, agent, seen) values(?,?,?,?,?)",
           (sha(tok.encode()), uid, now, agent, now))
        return ("Set-Cookie", f"sid={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000")

    def static(self, name):
        path = os.path.realpath(os.path.join(WEB, name))
        if not path.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(path):
            return self.send(404, "not found", "text/plain")
        with open(path, "rb") as f:
            data = f.read()
        kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind.endswith("javascript"):
            kind += "; charset=utf-8"
        self.send(200, data, kind, [("Cache-Control", "no-cache")])

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if parts[:1] == ["api"]:
                return self.api(method, parts[1:], q)
            if parts[:1] == ["web"]:
                return self.web_api(method, parts[1:], q)
            if parts[:1] == ["static"] and len(parts) == 2:
                return self.static(parts[1])
            if parts[:1] == ["img"] and len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[1]):
                return self.image(parts[1])
            if parts[:1] in (["cover"], ["download"], ["avatar"]):
                user = self.session()
                if not user:
                    return self.send(401, "log in first", "text/plain")
                return {"cover": self.cover, "download": self.download, "avatar": self.avatar}[parts[0]](user, parts[1:])
            if method == "GET":
                return self.static("index.html")  # the single page handles every other path
            return self.err(404, "not found")
        except ValueError as e:
            self.err(400, str(e))

    # ---- device API ---------------------------------------------------------
    def api(self, method, p, q):
        now = int(time.time())
        if p == ["hello"]:
            hello = {"service": "saveserv", "version": 2}
            if URL:
                hello["url"] = URL
            return self.send(200, hello)
        if p == ["pair"] and method == "POST":
            req = json.loads(self.body(4096) or b"{}")
            name = str(req.get("name") or "device")[:40]
            with PAIRS_LOCK:
                for k in [k for k, v in PAIRS.items() if v["expires"] < now]:
                    del PAIRS[k]
                used = {v["pin"] for v in PAIRS.values()}
                pin = f"{secrets.randbelow(10**6):06d}"
                while pin in used:
                    pin = f"{secrets.randbelow(10**6):06d}"
                pid = secrets.token_urlsafe(24)
                PAIRS[pid] = {"pin": pin, "name": name, "expires": now + PIN_TTL, "token": None}
            return self.send(200, {"id": pid, "pin": pin, "expires": PIN_TTL})
        if len(p) == 2 and p[0] == "pair" and method == "GET":
            with PAIRS_LOCK:
                pr = PAIRS.get(p[1])
                if not pr or (pr["expires"] < now and not pr["token"]):
                    return self.err(404, "expired")
                if not pr["token"]:
                    return self.send(202, {"status": "pending"})
                del PAIRS[p[1]]
            return self.send(200, {"token": pr["token"], "account": pr["account"]})

        dev = self.device()
        if not dev:
            return self.err(401, "not paired")
        _, uid, dname = dev
        if p == ["whoami"]:
            u = db("select name from users where id=?", (uid,), one=True)
            return self.send(200, {"account": u[0], "device": dname})
        if len(p) == 2 and p[0] == "meta" and NAME.match(p[1]) and method == "PUT":
            m = self.json_body()
            text = lambda k, n=120: str(m.get(k) or "")[:n] or None  # noqa: E731
            db("insert or replace into games values(?,?,?,?,?)",
               (uid, p[1], text("title"), text("system", 16), text("rom", 200)))
            for slot, name in (m.get("slots") or {}).items():
                # Device names only fill blanks: a rename on the website wins.
                if SLOT.match(str(slot)) and name:
                    db("insert or ignore into meta values(?,?,?,?,null)", (uid, p[1], slot, str(name)[:80]))
                    db("update meta set title=? where user=? and game=? and slot=? and title is null",
                       (str(name)[:80], uid, p[1], slot))
            return self.send(200, {"ok": True})
        if p[:1] != ["saves"] or len(p) not in (2, 3) or not NAME.match(p[1]) or (len(p) == 3 and not SLOT.match(p[2])):
            return self.err(404, "not found")
        game = p[1]
        if len(p) == 2 and method == "GET":
            rows = db("""select slot, rev, sha, size, created, device from revs r where user=? and game=?
                         and rev=(select max(rev) from revs where user=r.user and game=r.game and slot=r.slot)""",
                      (uid, game))
            return self.send(200, {s: {"rev": r, "sha256": h, "size": n, "updated": t, "device": d}
                                   for s, r, h, n, t, d in rows})
        slot = p[2]
        cur = db("select rev, sha from revs where user=? and game=? and slot=? order by rev desc limit 1",
                 (uid, game, slot), one=True)
        if method == "GET":
            if not cur:
                return self.err(404, "no save")
            with open(blob_path(cur[1]), "rb") as f:
                data = f.read()
            return self.send(200, data, "application/octet-stream",
                             [("X-Rev", str(cur[0])), ("X-Sha256", cur[1])])
        if method == "PUT":
            base = int(q.get("base", "0"))
            data = self.body()
            if not data:
                return self.err(400, "empty save")
            with DB_LOCK:  # check-and-insert must be atomic
                row = DB.execute("select rev, sha from revs where user=? and game=? and slot=? order by rev desc limit 1",
                                 (uid, game, slot)).fetchone()
                rev = row[0] if row else 0
                if rev != base:
                    return self.send(409, {"error": "conflict", "rev": rev})
                digest = sha(data)
                if row and row[1] == digest:
                    return self.send(200, {"rev": rev})
                if not row:
                    gone = DB.execute("select 1 from deleted where user=? and game=? and slot=? and sha=?",
                                      (uid, game, slot, digest)).fetchone()
                    if gone and q.get("undelete") != "1":
                        return self.send(410, {"error": "this save was deleted on the website"})
                    if gone:
                        DB.execute("delete from deleted where user=? and game=? and slot=? and sha=?",
                                   (uid, game, slot, digest))
                if not os.path.exists(blob_path(digest)):
                    tmp = blob_path(digest) + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, blob_path(digest))
                DB.execute("insert into revs values(?,?,?,?,?,?,?,?)",
                           (uid, game, slot, rev + 1, digest, len(data), dname, now))
                DB.commit()
            prune(uid, game, slot)
            return self.send(200, {"rev": rev + 1})
        return self.err(405, "method")

    # ---- files for the website -------------------------------------------------
    def image(self, digest):
        path = blob_path(digest)
        if not os.path.exists(path):
            return self.send(404, "not found", "text/plain")
        with open(path, "rb") as f:
            data = f.read(MAX_IMAGE + 1)
        kind = image_type(data)
        if not kind:
            return self.send(404, "not an image", "text/plain")
        self.send(200, data, kind, [("Cache-Control", "public, max-age=31536000, immutable")])

    def cover_sha(self, uid, game, slot):
        for s in (slot, "*"):
            row = db("select cover from meta where user=? and game=? and slot=?", (uid, game, s), one=True)
            if row and row[0]:
                return row[0]
        g = db("select system, rom, title from games where user=? and game=?", (uid, game), one=True)
        if g and g[0] in CARDS:  # a memory card: the save's own title, if it has one
            named = db("select title from meta where user=? and game=? and slot=?", (uid, game, slot), one=True)
            if not (named and named[0]):
                return None
            return libretro_art(g[0], named[0]) or title_art(named[0], True)
        art = None
        if g and g[0] and g[1]:
            art = libretro_art(g[0], g[1])
        elif game in SYSTEMS:  # emulator folder: the slot is the ROM name
            art = libretro_art(game, slot)
        if art:
            return art
        title = self.game_title(uid, game, slot)
        # A title nobody gave, from a short id like "za", isn't worth searching for.
        if not (g and g[2]) and game not in SYSTEMS and len(title_key(title)) < 4:
            return None
        return title_art(title, game in SYSTEMS or bool(g and g[0] in SYSTEMS))

    def cover(self, user, p):
        if len(p) != 2 or not NAME.match(p[0]) or not SLOT.match(p[1]):
            return self.send(404, "not found", "text/plain")
        digest = self.cover_sha(user["id"], p[0], p[1])
        if digest:
            return self.redirect(f"/img/{digest}")
        title = self.game_title(user["id"], p[0], p[1])
        self.send(200, placeholder_svg(title, p[0]), "image/svg+xml", [("Cache-Control", "no-cache")])

    def avatar(self, user, p):
        uid = int(p[0]) if p and p[0].isdigit() else user["id"]
        row = db("select avatar, name, display from users where id=?", (uid,), one=True)
        if row and row[0]:
            return self.redirect(f"/img/{row[0]}")
        self.send(200, placeholder_svg(row[2] or row[1] if row else "?", f"user:{uid}"), "image/svg+xml",
                  [("Cache-Control", "no-cache")])

    def download(self, user, p):
        if len(p) != 3 or not NAME.match(p[0]) or not SLOT.match(p[1]) or not p[2].isdigit():
            return self.send(404, "not found", "text/plain")
        row = db("select sha from revs where user=? and game=? and slot=? and rev=?",
                 (user["id"], p[0], p[1], int(p[2])), one=True)
        if not row:
            return self.send(404, "not found", "text/plain")
        with open(blob_path(row[0]), "rb") as f:
            data = f.read()
        ext = {"ps2": "tar", "gc": "gci"}.get(self.card_system(user["id"], p[0]), "sav")
        fn = re.sub(r"[^A-Za-z0-9 _.()-]", "_", f"{p[0]}-{p[1]}-r{p[2]}.{ext}")
        self.send(200, data, "application/octet-stream", [("Content-Disposition", f'attachment; filename="{fn}"')])

    def card_system(self, uid, game):
        """"ps2" or "gc" when this game is a memory card folder."""
        g = db("select system from games where user=? and game=?", (uid, game), one=True)
        return g[0] if g and g[0] in CARDS else None

    def game_title(self, uid, game, slot):
        if self.card_system(uid, game):
            named = db("select title from meta where user=? and game=? and slot=?", (uid, game, slot), one=True)
            return named[0] if named and named[0] else slot
        g = db("select title from games where user=? and game=?", (uid, game), one=True)
        if g and g[0]:
            return g[0]
        if game in SYSTEMS:  # ROM name: drop region/version tags like "(USA, Europe)"
            return re.sub(r"\s*[(\[][^)\]]*[)\]]", "", slot).strip() or slot
        return pretty(game)

    # ---- JSON API for the website ------------------------------------------------
    def web_api(self, method, p, q):
        # Cross-site forms can't set custom headers: require one on every change.
        if method != "GET" and self.headers.get("X-SaveServ") != "1":
            return self.err(403, "missing X-SaveServ header")
        now = int(time.time())
        users = db("select count(*) from users", one=True)[0]
        if p == ["status"]:
            return self.send(200, {"users": users, "register": REGISTER == "open" or users == 0})
        if p == ["register"] and method == "POST":
            if not (REGISTER == "open" or users == 0):
                return self.err(403, "Registration is closed. Ask the owner to add you.")
            f = self.json_body()
            name, pw = str(f.get("name", "")).strip(), str(f.get("password", ""))
            if not USER.match(name):
                return self.err(400, "Use letters, numbers, dot, dash or underscore.")
            if len(pw) < 8:
                return self.err(400, "Password must be at least 8 characters.")
            if db("select 1 from users where name=?", (name,)):
                return self.err(400, "That name is taken.")
            db("insert into users(name, pw, created) values(?,?,?)", (name, hash_pw(pw), now))
            uid = db("select id from users where name=?", (name,), one=True)[0]
            return self.send(200, {"ok": True}, headers=[self.login(uid)])
        if p == ["login"] and method == "POST":
            f = self.json_body()
            row = db("select id, pw from users where name=?", (str(f.get("name", "")).strip(),), one=True)
            if not row or not check_pw(str(f.get("password", "")), row[1]):
                time.sleep(0.5)
                return self.err(401, "Wrong name or password.")
            return self.send(200, {"ok": True}, headers=[self.login(row[0])])

        user = self.session()
        if not user:
            return self.err(401, "log in first")
        uid = user["id"]
        owner = db("select min(id) from users", one=True)[0] == uid

        if p == ["logout"] and method == "POST":
            db("delete from sessions where token=?", (user["token"],))
            return self.send(200, {"ok": True}, headers=[("Set-Cookie", "sid=; Path=/; Max-Age=0")])
        if p == ["me"]:
            row = db("select avatar from users where id=?", (uid,), one=True)
            return self.send(200, {"id": uid, "name": user["name"], "display": user["display"], "owner": owner,
                                   "avatar": f"/avatar/{uid}?v={(row[0] or 'none')[:8]}"})

        if p == ["saves"]:
            rows = db("""select game, slot, rev, size, created, device,
                           (select count(*) from revs x where x.user=r.user and x.game=r.game and x.slot=r.slot)
                         from revs r where user=?
                         and rev=(select max(rev) from revs where user=r.user and game=r.game and slot=r.slot)""",
                      (uid,))
            out = []
            for g, s, r, n, t, d, count in rows:
                out.append(self.save_card(uid, g, s) | {
                    "rev": r, "size": n, "updated": t, "device": d, "revisions": count})
            return self.send(200, out)
        if len(p) == 2 and p[0] == "saves" and NAME.match(p[1]) and method == "DELETE":
            slots = [r[0] for r in db("select distinct slot from revs where user=? and game=?", (uid, p[1]))]
            if not slots:
                return self.err(404, "no such game")
            return self.send(200, {"saves": len(slots), "revisions": sum(delete_slot(uid, p[1], x) for x in slots)})
        if len(p) >= 3 and p[0] == "saves" and NAME.match(p[1]) and SLOT.match(p[2]):
            g, s = p[1], p[2]
            if len(p) == 3 and method == "DELETE":
                n = delete_slot(uid, g, s)
                return self.send(200, {"revisions": n}) if n else self.err(404, "no such save")
            if len(p) == 3 and method == "GET":
                rows = db("select rev, size, created, device, sha from revs where user=? and game=? and slot=? "
                          "order by rev desc", (uid, g, s))
                if not rows:
                    return self.err(404, "no such save")
                revs = [{"rev": r, "size": n, "updated": t, "device": d, "sha256": h} for r, n, t, d, h in rows]
                return self.send(200, self.save_card(uid, g, s) | {"current": revs[0], "history": revs[1:]})
            if p[3:] == ["restore"] and method == "POST":
                rev = int(self.json_body().get("rev", 0))
                with DB_LOCK:
                    old = DB.execute("select sha, size from revs where user=? and game=? and slot=? and rev=?",
                                     (uid, g, s, rev)).fetchone()
                    top = DB.execute("select max(rev) from revs where user=? and game=? and slot=?",
                                     (uid, g, s)).fetchone()[0]
                    if not old:
                        return self.err(404, "no such revision")
                    DB.execute("insert into revs values(?,?,?,?,?,?,?,?)",
                               (uid, g, s, top + 1, old[0], old[1], f"restored r{rev}", now))
                    DB.commit()
                prune(uid, g, s)
                return self.send(200, {"rev": top + 1})
            if p[3:] == ["title"] and method == "POST":
                title = str(self.json_body().get("title", "")).strip()[:80] or None
                row = db("select cover from meta where user=? and game=? and slot=?", (uid, g, s), one=True)
                db("insert or replace into meta values(?,?,?,?,?)", (uid, g, s, title, row[0] if row else None))
                return self.send(200, {"ok": True})
            if p[3:4] == ["cover"]:
                target = "*" if q.get("scope") == "game" else s
                row = db("select title, cover from meta where user=? and game=? and slot=?", (uid, g, target), one=True)
                old = row[1] if row else None
                if method == "PUT":
                    data = self.body(MAX_IMAGE)
                    if not image_type(data):
                        return self.err(400, "Use a PNG, JPEG, GIF or WebP image.")
                    db("insert or replace into meta values(?,?,?,?,?)",
                       (uid, g, target, row[0] if row else None, put_blob(data)))
                elif method == "DELETE":
                    db("update meta set cover=null where user=? and game=? and slot=?", (uid, g, target))
                else:
                    return self.err(405, "method")
                if old:
                    drop_blob_if_unused(old)
                return self.send(200, {"ok": True})
            return self.err(404, "not found")

        if p == ["pair"] and method == "POST":
            pin = re.sub(r"\D", "", str(self.json_body().get("pin", "")))
            with PAIRS_LOCK:
                hit = next((k for k, v in PAIRS.items()
                            if v["pin"] == pin and v["expires"] >= now and not v["token"]), None)
                if hit:
                    tok = secrets.token_urlsafe(32)
                    pr = PAIRS[hit]
                    db("insert into devices(user, name, token, created, seen) values(?,?,?,?,?)",
                       (uid, pr["name"], sha(tok.encode()), now, now))
                    pr["token"], pr["account"] = tok, user["name"]
                    return self.send(200, {"device": pr["name"]})
            time.sleep(1)  # slow down guessing
            return self.err(404, "No device is waiting with that PIN. Check the number, or start pairing again.")

        if p == ["devices"]:
            rows = db("select id, name, created, seen from devices where user=? order by seen desc", (uid,))
            return self.send(200, [{"id": i, "name": n, "created": c, "seen": s} for i, n, c, s in rows])
        if len(p) == 3 and p[0] == "devices" and p[1].isdigit() and p[2] == "remove" and method == "POST":
            db("delete from devices where id=? and user=?", (int(p[1]), uid))
            return self.send(200, {"ok": True})

        if p == ["profile"] and method == "POST":
            display = str(self.json_body().get("display", "")).strip()[:40] or None
            db("update users set display=? where id=?", (display, uid))
            return self.send(200, {"ok": True})
        if p == ["avatar"]:
            old = db("select avatar from users where id=?", (uid,), one=True)[0]
            if method == "PUT":
                data = self.body(MAX_IMAGE)
                if not image_type(data):
                    return self.err(400, "Use a PNG, JPEG, GIF or WebP image.")
                db("update users set avatar=? where id=?", (put_blob(data), uid))
            elif method == "DELETE":
                db("update users set avatar=null where id=?", (uid,))
            else:
                return self.err(405, "method")
            drop_blob_if_unused(old)
            return self.send(200, {"ok": True})
        if p == ["password"] and method == "POST":
            f = self.json_body()
            pw = db("select pw from users where id=?", (uid,), one=True)[0]
            if not check_pw(str(f.get("current", "")), pw):
                time.sleep(0.5)
                return self.err(400, "Your current password is wrong.")
            new = str(f.get("new", ""))
            if len(new) < 8:
                return self.err(400, "The new password must be at least 8 characters.")
            db("update users set pw=? where id=?", (hash_pw(new), uid))
            db("delete from sessions where user=? and token<>?", (uid, user["token"]))
            return self.send(200, {"ok": True})
        if p == ["sessions"]:
            rows = db("select token, created, seen, agent from sessions where user=? order by seen desc", (uid,))
            return self.send(200, [{"current": t == user["token"], "created": c, "seen": s or c, "agent": a or ""}
                                   for t, c, s, a in rows])
        if p == ["sessions", "revoke-others"] and method == "POST":
            db("delete from sessions where user=? and token<>?", (uid, user["token"]))
            return self.send(200, {"ok": True})

        if p == ["accounts"] and owner:
            if method == "POST":
                f = self.json_body()
                name, pw = str(f.get("name", "")).strip(), str(f.get("password", ""))
                if not USER.match(name) or len(pw) < 8 or db("select 1 from users where name=?", (name,)):
                    return self.err(400, "Name taken or invalid, or password under 8 characters.")
                db("insert into users(name, pw, created) values(?,?,?)", (name, hash_pw(pw), now))
            rows = db("select id, name, display, created from users order by id")
            return self.send(200, [{"id": i, "name": n, "display": d or n, "created": c} for i, n, d, c in rows])
        return self.err(404, "not found")

    def save_card(self, uid, game, slot):
        """Title, version label and cover URL of one save."""
        m = db("select title, cover from meta where user=? and game=? and slot=?", (uid, game, slot), one=True)
        gm = db("select cover from meta where user=? and game=? and slot='*'", (uid, game), one=True)
        title = self.game_title(uid, game, slot)
        version = (m[0] if m and m[0] else (None if game in SYSTEMS else pretty(slot)))
        if game in SYSTEMS and m and m[0]:
            title, version = m[0], None
        stamp = ((m[1] if m else None) or (gm[0] if gm else None) or "auto")[:8]
        card = self.card_system(uid, game)
        if card:
            title, version = (m[0] if m and m[0] else slot), None
        return {"game": game, "slot": slot, "title": title, "version": version,
                "system": CARDS[card][0].lower() if card else (game if game in SYSTEMS else None),
                "cover": f"/cover/{quote(game, safe='')}/{quote(slot, safe='')}?v={stamp}"}


def discovery():
    """Answer "SAVESERV?" datagrams on the listen port (UDP) with where to find this server."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", PORT))
    except OSError as e:
        print(f"saveserv: discovery off (UDP port {PORT}: {e})", flush=True)
        return
    reply = json.dumps({"service": "saveserv", "version": 2, "url": URL or None, "port": PORT}).encode()
    while True:
        try:
            data, addr = sock.recvfrom(64)
            if data.strip() == b"SAVESERV?":
                sock.sendto(reply, addr)
        except OSError:
            time.sleep(0.1)


if __name__ == "__main__":
    print(f"saveserv listening on :{PORT} (HTTP, and UDP for discovery), data in {DATA}", flush=True)
    threading.Thread(target=discovery, name="discovery", daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
