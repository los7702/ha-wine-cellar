"""Vivino API diagnostic — run inside GitHub Actions with the VIVINO secret.

Prints ONLY structural facts (status codes, JSON key names, string lengths,
booleans). It never prints the credentials, cookies, or any token value, so
the Actions log stays safe to share.

VIVINO secret formats accepted:
  - JSON: {"email": "...", "password": "..."}
  - "email:password"
  - two lines: email\npassword
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import aiohttp

UA = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
H = {"User-Agent": UA, "Accept": "application/json"}
API = "https://api.vivino.com"
WWW = "https://www.vivino.com"
TOKEN_KEYS = (
    "token", "api_token", "access_token", "jwt",
    "oauth_token", "bearer_token", "session_token",
)


def parse_creds(raw: str):
    """Return ('creds', email, password) or ('token', token, None)."""
    raw = raw.strip()
    if raw.startswith("{"):
        d = json.loads(raw)
        tokv = d.get("token") or d.get("api_token") or d.get("access_token")
        if tokv and not d.get("password"):
            return "token", tokv, None
        return "creds", d["email"], d["password"]
    # Try common email/password separators
    for sep in ("\n", "\t", "|", ";", ",", " "):
        if sep in raw:
            a, b = raw.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a and b and "@" in a:
                return "creds", a, b
    if ":" in raw and "@" in raw.split(":", 1)[0]:
        a, b = raw.split(":", 1)
        return "creds", a.strip(), b.strip()
    # Single value with no pair → treat as a bearer token
    return "token", raw, None


def shape(raw: str) -> str:
    """Non-revealing structural description of the secret."""
    r = raw.strip()
    return (
        f"len={len(r)} has_at={'@' in r} has_space={' ' in r} "
        f"has_colon={':' in r} has_newline={chr(10) in r} "
        f"dot_segments={len(r.split('.'))} "
        f"alnum_only={r.replace('.', '').replace('_', '').replace('-', '').isalnum()}"
    )


def tok(t) -> str:
    """Describe a token without revealing it."""
    if not isinstance(t, str):
        return f"<{type(t).__name__}>"
    return f"len={len(t)} head={t[:4]!r}"


def find_tokens(data: dict) -> list[tuple[str, str]]:
    """Return (source_key, token) pairs from the login response."""
    out = []
    containers = [("root", data)]
    for k in ("user", "session", "auth"):
        if isinstance(data.get(k), dict):
            containers.append((k, data[k]))
    for cname, c in containers:
        for k in TOKEN_KEYS:
            v = c.get(k)
            if isinstance(v, str) and len(v) > 10:
                out.append((f"{cname}.{k}", v))
    return out


def structure(obj, depth=0):
    """Compact structural summary of a JSON object."""
    if isinstance(obj, dict):
        return "{" + ", ".join(sorted(obj.keys())) + "}"
    if isinstance(obj, list):
        head = structure(obj[0], depth + 1) if obj else "empty"
        return f"[{len(obj)} items; first={head}]"
    return type(obj).__name__


async def probe(session, method, url, headers=None, **kw):
    try:
        async with session.request(method, url, headers=headers, timeout=aiohttp.ClientTimeout(total=25), **kw) as r:
            body = None
            try:
                body = await r.json(content_type=None)
            except Exception:
                pass
            return r.status, body
    except Exception as e:
        return None, f"EXC {type(e).__name__}: {e}"


async def main() -> int:
    raw = os.environ.get("VIVINO")
    if not raw:
        print("VIVINO secret not present in env")
        return 1
    print(f"Secret shape: {shape(raw)}")
    kind, a, b = parse_creds(raw)
    print(f"Interpreted as: {kind}")

    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as s:
        # ── Token-only mode: test the provided bearer token directly ──
        if kind == "token":
            token = a
            print(f"\n[T] Testing provided token ({tok(token)}) against api.vivino.com")
            st, body = await probe(s, "GET", f"{API}/users/me", headers={**H, "Authorization": f"Bearer {token}"})
            print(f"    GET api/users/me -> {st}; shape={structure(body) if isinstance(body,(dict,list)) else body}")
            uid = None
            if isinstance(body, dict):
                u = body.get("user") if isinstance(body.get("user"), dict) else body
                uid = u.get("id")
                print(f"    resolved user id: {uid}  alias: {u.get('alias')!r}")
            if not uid:
                # Some deployments key /me differently; try the user object at root
                print("    could not resolve user id from /users/me")
            for path in ([f"users/{uid}/cellar", "cellars", f"users/{uid}/vintages", f"users/{uid}/wishlist"] if uid else ["cellars"]):
                st6, b6 = await probe(s, "GET", f"{API}/{path}", headers={**H, "Authorization": f"Bearer {token}"}, params={"page": 1, "limit": 5})
                shp = structure(b6) if isinstance(b6, (dict, list)) else b6
                print(f"    GET api/{path} -> {st6}; shape={shp}")
                if isinstance(b6, dict) and st6 == 200:
                    for lk in ("cellar", "wishlist", "user_vintages", "vintages", "records", "items"):
                        if isinstance(b6.get(lk), list) and b6[lk]:
                            print(f"      first {lk} record: {structure(b6[lk][0])}")
                            rec = b6[lk][0]
                            for sub in ("vintage", "wine", "review", "price"):
                                if isinstance(rec.get(sub), dict):
                                    print(f"        .{sub}: {structure(rec[sub])}")
                            break
            print("\nDONE (token mode)")
            return 0 if st == 200 else 1

        email, password = a, b
        # ── 1. Website login ─────────────────────────────────────────
        st, body = await probe(
            s, "POST", f"{WWW}/api/login",
            headers={**H, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        print(f"\n[1] POST www.vivino.com/api/login -> {st}")
        if not isinstance(body, dict):
            print(f"    body: {body}")
            return 1
        print(f"    response keys: {sorted(body.keys())}")
        user = body.get("user") if isinstance(body.get("user"), dict) else body
        uid = user.get("id") or body.get("id") or body.get("user_id")
        print(f"    user id: {uid}   alias: {user.get('alias')!r}")
        cookies = [c.key for c in jar]
        print(f"    cookies set: {cookies}")

        login_tokens = find_tokens(body)
        print(f"    token fields in login: {[(k, tok(v)) for k, v in login_tokens]}")

        # ── 2. Scrape token from logged-in web pages ─────────────────
        scraped = []
        for url in (f"{WWW}/wines", f"{WWW}/", f"{WWW}/myvivino/my-wines"):
            st2, _ = await probe(s, "GET", url, headers={**H, "Accept": "text/html"})
            try:
                async with s.get(url, headers={**H, "Accept": "text/html"}, timeout=aiohttp.ClientTimeout(total=25)) as r:
                    html = await r.text()
            except Exception:
                html = ""
            m = re.findall(r'"(api_token|apiToken|access_token|accessToken|oauth_token|bearer_token)"\s*:\s*"([A-Za-z0-9._\-]{20,})"', html)
            api_hosts = sorted(set(re.findall(r'api\.vivino\.com/[a-z_]+', html)))
            print(f"\n[2] GET {url} -> {st2}; token matches in html: {[(k, tok(v)) for k, v in m]}")
            if api_hosts:
                print(f"    api.vivino.com refs: {api_hosts[:12]}")
            for _, v in m:
                scraped.append(v)

        # ── 3. Probe api.vivino.com token validity ───────────────────
        candidates = login_tokens + [("page", v) for v in scraped]
        cookie_toks = [(f"cookie.{c.key}", c.value) for c in jar if "token" in c.key.lower() and len(c.value) > 20]
        candidates += cookie_toks
        print(f"\n[3] candidate tokens: {[(k, tok(v)) for k, v in candidates]}")

        working = None
        for name, t in candidates:
            st3, _ = await probe(s, "GET", f"{API}/users/{uid}", headers={**H, "Authorization": f"Bearer {t}"})
            print(f"    probe {name}: GET api/users/{uid} -> {st3}")
            if st3 == 200 and working is None:
                working = t

        # ── 4. Try the mobile OAuth password grant ───────────────────
        # Well-known Vivino Android client id (public in the app); harmless to try.
        for cid in ("t6mmpN7xLxV92Y2Fv7fCn6h1ePfR9zX6RtcNXuFn", ):
            st4, b4 = await probe(
                s, "POST", f"{API}/oauth/token",
                headers={**H, "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "password", "username": email, "password": password, "client_id": cid},
            )
            keys = sorted(b4.keys()) if isinstance(b4, dict) else b4
            print(f"\n[4] POST api/oauth/token (client tail {cid[-4:]}) -> {st4}; keys={keys}")
            if isinstance(b4, dict):
                ot = find_tokens(b4)
                if ot:
                    print(f"    oauth token fields: {[(k, tok(v)) for k, v in ot]}")
                    if working is None:
                        # validate it
                        _t = ot[0][1]
                        st5, _ = await probe(s, "GET", f"{API}/users/{uid}", headers={**H, "Authorization": f"Bearer {_t}"})
                        print(f"    validate oauth token: GET api/users/{uid} -> {st5}")
                        if st5 == 200:
                            working = _t

        # ── 5. Fetch user data with the working token ────────────────
        print(f"\n[5] data endpoints (working token: {'yes' if working else 'NO'})")
        auth = {**H, "Authorization": f"Bearer {working}"} if working else H
        for path in (f"users/{uid}/cellar", "cellars", f"users/{uid}/vintages", f"users/{uid}/wishlist"):
            st6, b6 = await probe(s, "GET", f"{API}/{path}", headers=auth, params={"page": 1, "limit": 5})
            shp = structure(b6) if isinstance(b6, (dict, list)) else b6
            print(f"    GET api/{path} -> {st6}; shape={shp}")
            # If we got records, show the first record's nested keys to fix parsing
            if isinstance(b6, dict) and st6 == 200:
                for lk in ("cellar", "wishlist", "user_vintages", "vintages", "records", "items"):
                    if isinstance(b6.get(lk), list) and b6[lk]:
                        print(f"      first {lk} record: {structure(b6[lk][0])}")
                        rec = b6[lk][0]
                        if isinstance(rec, dict):
                            for sub in ("vintage", "wine", "review", "price"):
                                if isinstance(rec.get(sub), dict):
                                    print(f"        .{sub}: {structure(rec[sub])}")
                        break

    print("\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
