#!/usr/bin/env python3
"""SaveServ: save sync server for game launchers, recomps and emulators.

Accounts own saves. A launcher pairs to an account with a PIN:
  1. launcher  POST /api/pair {"name": "RG35XXSP"}  -> {"id", "pin", "expires"}
  2. the user logs in on the website, opens "Pair device", types the PIN
  3. launcher  GET /api/pair/<id>  -> 202 while pending, 200 {"token"} once paired
Then every API call carries "Authorization: Bearer <token>".

Saves are addressed as <game>/<slot>: a recomp uses its game id and variant
(ffta/remix); an emulator agent uses the system and ROM name (gba/Golden Sun). Every upload is kept
as a new revision (the last KEEP per slot), so nothing is ever overwritten:
  GET  /api/saves/<game>               {"slot": {"rev","sha256","size","updated","device"}}
  GET  /api/saves/<game>/<slot>        bytes (headers X-Rev, X-Sha256)
  PUT  /api/saves/<game>/<slot>?base=N bytes; 409 unless N is the current rev (0 = none)

Standard library only. Configuration (environment):
  SAVESERV_DATA     data directory (default /data)
  SAVESERV_PORT     listen port (default 8787)
  SAVESERV_REGISTER "open" lets anyone register; "first" (default) only the
                    first account, later accounts are added from Account page
  SAVESERV_KEEP     revisions kept per slot (default 50)
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

DATA = os.environ.get("SAVESERV_DATA", "/data")
PORT = int(os.environ.get("SAVESERV_PORT", "8787"))
REGISTER = os.environ.get("SAVESERV_REGISTER", "first")
KEEP = int(os.environ.get("SAVESERV_KEEP", "50"))
MAX_SAVE = 4 << 20
PIN_TTL = 600
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Slots may be ROM-style file names ("Golden Sun (USA)"), but no path parts.
SLOT = re.compile(r"^(?!\.\.?$)[^/\\\x00-\x1f]{1,200}$")
USER = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

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
""")
# Pending pairings live in memory: id -> {pin, name, expires, token, tries}
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


def prune(user, game, slot):
    old = db("select rev, sha from revs where user=? and game=? and slot=? order by rev desc limit -1 offset ?",
             (user, game, slot, KEEP))
    for rev, digest in old:
        db("delete from revs where user=? and game=? and slot=? and rev=?", (user, game, slot, rev))
        if not db("select 1 from revs where sha=? limit 1", (digest,)):
            try:
                os.remove(blob_path(digest))
            except FileNotFoundError:
                pass


def ago(t):
    s = int(time.time()) - int(t)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


CSS = """
:root{--bg:#10182f;--panel:#1c284c;--text:#ececE4;--dim:#96a0b8;--hi:#c8a848;--bad:#d06060}
@media (prefers-color-scheme: light){:root{--bg:#f3f1ea;--panel:#fff;--text:#1a1f2e;--dim:#5b6475;--hi:#8a6a10;--bad:#a03030}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}
h1{font-size:1.3rem;margin:0}h1 span{color:var(--hi)}h2{font-size:1.05rem;margin:24px 0 8px}
nav a{color:var(--dim);margin-left:12px;text-decoration:none}nav a:hover{color:var(--text)}
section{background:var(--panel);border-radius:10px;padding:14px 16px;margin:12px 0}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid #8883}
th{color:var(--dim);font-weight:500;font-size:.85rem}
input{font:inherit;padding:8px 10px;border-radius:6px;border:1px solid #8886;background:transparent;color:inherit;width:100%;max-width:280px}
input.pin{font-size:1.6rem;letter-spacing:.3em;max-width:200px;text-align:center}
button{font:inherit;padding:8px 14px;border-radius:6px;border:0;background:var(--hi);color:#111;cursor:pointer}
button.link{background:none;color:var(--bad);padding:0}
.dim{color:var(--dim)}.msg{color:var(--hi)}.err{color:var(--bad)}form.inline{display:inline}
label{display:block;margin:8px 0 4px;color:var(--dim)}a{color:var(--hi)}
"""


def page(title, body, user=None):
    nav = ""
    if user:
        nav = ('<nav><a href="/">Saves</a><a href="/pair">Pair device</a><a href="/devices">Devices</a>'
               f'<a href="/account">{html.escape(user[1])}</a></nav>')
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>"
            f"<header><h1>Save<span>Serv</span></h1>{nav}</header>{body}</main></body></html>")


class Handler(BaseHTTPRequestHandler):
    server_version = "saveserv/1"

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
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def html(self, title, body, user=None, code=200, headers=()):
        self.send(code, page(title, body, user), "text/html; charset=utf-8", headers)

    def redirect(self, where, headers=()):
        self.send(303, b"", "text/plain", [("Location", where), *headers])

    def body(self, limit=MAX_SAVE):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n) if n else b""

    def form(self):
        return {k: v[0] for k, v in parse_qs(self.body(65536).decode()).items()}

    def session_user(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        if "sid" not in c:
            return None
        row = db("select u.id, u.name from sessions s join users u on u.id=s.user where s.token=?",
                 (sha(c["sid"].value.encode()),), one=True)
        return row

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
        db("insert into sessions values(?,?,?)", (sha(tok.encode()), uid, int(time.time())))
        return ("Set-Cookie", f"sid={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000")

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if parts[:1] == ["api"]:
                return self.api(method, parts[1:], q)
            return self.web(method, parts, q)
        except ValueError as e:
            self.send(400, {"error": str(e)})

    # ---- launcher API -------------------------------------------------------
    def api(self, method, p, q):
        now = int(time.time())
        if p == ["hello"]:
            return self.send(200, {"service": "saveserv", "version": 1})
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
                    return self.send(404, {"error": "expired"})
                if not pr["token"]:
                    return self.send(202, {"status": "pending"})
                del PAIRS[p[1]]
            return self.send(200, {"token": pr["token"], "account": pr["account"]})

        dev = self.device()
        if not dev:
            return self.send(401, {"error": "not paired"})
        _, uid, dname = dev
        if p == ["whoami"]:
            u = db("select name from users where id=?", (uid,), one=True)
            return self.send(200, {"account": u[0], "device": dname})
        if p[:1] != ["saves"] or len(p) not in (2, 3) or not NAME.match(p[1]) or (len(p) == 3 and not SLOT.match(p[2])):
            return self.send(404, {"error": "not found"})
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
                return self.send(404, {"error": "no save"})
            with open(blob_path(cur[1]), "rb") as f:
                data = f.read()
            return self.send(200, data, "application/octet-stream",
                             [("X-Rev", str(cur[0])), ("X-Sha256", cur[1])])
        if method == "PUT":
            base = int(q.get("base", "0"))
            data = self.body()
            if not data:
                return self.send(400, {"error": "empty save"})
            with DB_LOCK:  # check-and-insert must be atomic
                row = DB.execute("select rev, sha from revs where user=? and game=? and slot=? order by rev desc limit 1",
                                 (uid, game, slot)).fetchone()
                rev = row[0] if row else 0
                if rev != base:
                    return self.send(409, {"error": "conflict", "rev": rev})
                digest = sha(data)
                if row and row[1] == digest:
                    return self.send(200, {"rev": rev})
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
        return self.send(405, {"error": "method"})

    # ---- website ------------------------------------------------------------
    def web(self, method, p, q):
        user = self.session_user()
        users = db("select count(*) from users", one=True)[0]
        if p == ["register"]:
            allowed = REGISTER == "open" or users == 0
            if not allowed:
                return self.html("Register", "<section>Registration is closed. Ask the owner to add you "
                                 "from their Account page.</section>")
            err = ""
            if method == "POST":
                f = self.form()
                name, pw = f.get("name", "").strip(), f.get("pw", "")
                if not USER.match(name):
                    err = "Use letters, numbers, dot, dash or underscore."
                elif len(pw) < 6:
                    err = "Password must be at least 6 characters."
                elif db("select 1 from users where name=?", (name,)):
                    err = "That name is taken."
                else:
                    db("insert into users(name, pw, created) values(?,?,?)", (name, hash_pw(pw), int(time.time())))
                    uid = db("select id from users where name=?", (name,), one=True)[0]
                    return self.redirect("/pair", [self.login(uid)])
            return self.html("Register", self.account_form("Create account", "/register", err))
        if p == ["login"]:
            err = ""
            if method == "POST":
                f = self.form()
                row = db("select id, pw from users where name=?", (f.get("name", "").strip(),), one=True)
                if row and check_pw(f.get("pw", ""), row[1]):
                    return self.redirect(q.get("next", "/"), [self.login(row[0])])
                err = "Wrong name or password."
            if users == 0:
                return self.redirect("/register")
            extra = '<p class=dim>No account? <a href="/register">Register</a></p>' if REGISTER == "open" else ""
            return self.html("Log in", self.account_form("Log in", "/login", err) + extra)
        if p == ["logout"]:
            return self.redirect("/login", [("Set-Cookie", "sid=; Path=/; Max-Age=0")])
        if not user:
            return self.redirect("/register" if users == 0 else "/login")
        uid = user[0]

        if p == ["pair"]:
            msg = err = ""
            if method == "POST":
                pin = re.sub(r"\D", "", self.form().get("pin", ""))
                now = int(time.time())
                with PAIRS_LOCK:
                    hit = next((k for k, v in PAIRS.items()
                                if v["pin"] == pin and v["expires"] >= now and not v["token"]), None)
                    if hit:
                        tok = secrets.token_urlsafe(32)
                        pr = PAIRS[hit]
                        db("insert into devices(user, name, token, created, seen) values(?,?,?,?,?)",
                           (uid, pr["name"], sha(tok.encode()), now, now))
                        pr["token"], pr["account"] = tok, user[1]
                        msg = f"Paired {html.escape(pr['name'])}. It will finish on its own in a few seconds."
                    else:
                        err = "No device is waiting with that PIN. Check the number or start pairing again."
                        time.sleep(1)  # slow down guessing
            body = f"""<section><h2 style="margin-top:0">Pair a device</h2>
<p class=dim>On the device, choose <b>Pair</b> in its save sync menu (FFTA handheld: start screen, <b>Save Sync</b>,
<b>Pair this device</b>; command line: <code>saveserv pair</code>). Type the 6-digit PIN it shows.</p>
<form method=post><input class=pin name=pin inputmode=numeric autocomplete=off maxlength=7
 placeholder="000000" autofocus> <button>Pair</button></form>
<p class=msg>{msg}</p><p class=err>{err}</p></section>"""
            return self.html("Pair device", body, user)

        if p == ["devices"]:
            if method == "POST":
                did = self.form().get("remove")
                db("delete from devices where id=? and user=?", (did, uid))
                return self.redirect("/devices")
            rows = db("select id, name, created, seen from devices where user=? order by seen desc", (uid,))
            tr = "".join(f"<tr><td>{html.escape(n)}</td><td class=dim>paired {ago(c)}</td><td class=dim>seen {ago(s)}</td>"
                         f"<td><form class=inline method=post><button class=link name=remove value={i}>Unpair</button></form></td></tr>"
                         for i, n, c, s in rows) or "<tr><td class=dim>No devices yet. <a href='/pair'>Pair one.</a></td></tr>"
            return self.html("Devices", f"<section><h2 style='margin-top:0'>Devices</h2><table>{tr}</table></section>", user)

        if p == ["account"]:
            msg = ""
            if method == "POST":
                f = self.form()
                if f.get("action") == "adduser":
                    name, pw = f.get("name", "").strip(), f.get("pw", "")
                    if USER.match(name) and len(pw) >= 6 and not db("select 1 from users where name=?", (name,)):
                        db("insert into users(name, pw, created) values(?,?,?)", (name, hash_pw(pw), int(time.time())))
                        msg = f"Added {html.escape(name)}."
                    else:
                        msg = "Could not add: name taken or invalid, or password under 6 characters."
                elif f.get("action") == "password" and len(f.get("pw", "")) >= 6:
                    db("update users set pw=? where id=?", (hash_pw(f["pw"]), uid))
                    msg = "Password changed."
            body = f"""<section><h2 style="margin-top:0">Account: {html.escape(user[1])}</h2>
<form method=post><input type=hidden name=action value=password><label>New password</label>
<input type=password name=pw minlength=6> <button>Change</button></form></section>
<section><h2 style="margin-top:0">Add another account</h2><p class=dim>Each account has its own saves.</p>
<form method=post><input type=hidden name=action value=adduser><label>Name</label><input name=name>
<label>Password</label><input type=password name=pw minlength=6><p><button>Add account</button></p></form></section>
<p class=msg>{msg}</p><p><a href="/logout">Log out</a></p>"""
            return self.html("Account", body, user)

        if len(p) == 4 and p[0] == "download" and NAME.match(p[1]) and SLOT.match(p[2]):
            row = db("select sha from revs where user=? and game=? and slot=? and rev=?",
                     (uid, p[1], p[2], int(p[3])), one=True)
            if not row:
                return self.send(404, "not found", "text/plain")
            with open(blob_path(row[0]), "rb") as f:
                data = f.read()
            fn = re.sub(r'[^A-Za-z0-9 _.()-]', '_', f"{p[1]}-{p[2]}-r{p[3]}.sav")
            return self.send(200, data, "application/octet-stream",
                             [("Content-Disposition", f'attachment; filename="{fn}"')])

        if p == ["restore"] and method == "POST":
            # Re-publish an old revision as the newest one; devices pull it on next sync.
            f = self.form()
            game, slot, rev = f.get("game", ""), f.get("slot", ""), int(f.get("rev", "0"))
            if NAME.match(game) and SLOT.match(slot):
                with DB_LOCK:
                    old = DB.execute("select sha, size from revs where user=? and game=? and slot=? and rev=?",
                                     (uid, game, slot, rev)).fetchone()
                    top = DB.execute("select max(rev) from revs where user=? and game=? and slot=?",
                                     (uid, game, slot)).fetchone()[0]
                    if old:
                        DB.execute("insert into revs values(?,?,?,?,?,?,?,?)",
                                   (uid, game, slot, top + 1, old[0], old[1], f"restored r{rev}", int(time.time())))
                        DB.commit()
            return self.redirect(f"/?slot={quote(game, safe='')}/{quote(slot, safe='')}")

        if p == []:
            rows = db("""select game, slot, rev, size, created, device from revs r where user=?
                         and rev=(select max(rev) from revs where user=r.user and game=r.game and slot=r.slot)
                         order by game, created desc""", (uid,))
            if not rows:
                devs = db("select count(*) from devices where user=?", (uid,), one=True)[0]
                hint = "Play on a paired device and its saves appear here." if devs else \
                    "No devices yet. <a href='/pair'>Pair one</a> to start syncing."
                return self.html("Saves", f"<section>{hint}</section>", user)
            out, game = [], None
            for g, s, r, n, t, d in rows:
                if g != game:
                    if game is not None:
                        out.append("</table></section>")
                    out.append(f"<section><h2 style='margin-top:0'>{html.escape(g)}</h2><table>"
                               "<tr><th>Save</th><th>Updated</th><th>From</th><th></th></tr>")
                    game = g
                out.append(f"<tr><td><a href='/?slot={quote(g, safe='')}/{quote(s, safe='')}'>{html.escape(s)}</a></td><td>{ago(t)}</td>"
                           f"<td class=dim>{html.escape(d)}</td><td><a href='/download/{quote(g, safe='')}/{quote(s, safe='')}/{r}'>download</a></td></tr>")
            out.append("</table></section>")
            sel = q.get("slot", "")  # "<game>/<slot>"
            if "/" in sel:
                g, s = sel.split("/", 1)
                hist = db("select rev, size, created, device from revs where user=? and game=? and slot=? order by rev desc",
                          (uid, g, s))
                tr = "".join(
                    f"<tr><td>r{r}</td><td>{ago(t)}</td><td class=dim>{html.escape(d)}</td>"
                    f"<td><a href='/download/{quote(g, safe='')}/{quote(s, safe='')}/{r}'>download</a></td><td>"
                    + ("" if i == 0 else
                       f"<form class=inline method=post action=/restore><input type=hidden name=game value='{html.escape(g)}'>"
                       f"<input type=hidden name=slot value='{html.escape(s)}'><input type=hidden name=rev value={r}>"
                       f"<button class=link style='color:var(--hi)'>restore</button></form>")
                    + "</td></tr>" for i, (r, n, t, d) in enumerate(hist))
                out.append(f"<section><h2 style='margin-top:0'>History: {html.escape(g)} / {html.escape(s)}</h2>"
                           f"<p class=dim>Restore makes an old copy the newest; devices pick it up next time they sync.</p>"
                           f"<table>{tr}</table></section>")
            return self.html("Saves", "".join(out), user)
        return self.send(404, "not found", "text/plain")

    def account_form(self, title, action, err):
        return f"""<section><h2 style="margin-top:0">{title}</h2><form method=post action="{action}">
<label>Name</label><input name=name autofocus autocomplete=username>
<label>Password</label><input type=password name=pw autocomplete=current-password>
<p><button>{title}</button></p><p class=err>{err}</p></form></section>"""


if __name__ == "__main__":
    print(f"saveserv listening on :{PORT}, data in {DATA}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
