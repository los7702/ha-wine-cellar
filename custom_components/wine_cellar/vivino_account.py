"""Vivino account connection: sync your Vivino cellar and wishlist.

Vivino has no official public API. This client uses the same endpoints the
Vivino web app uses:

- ``POST https://www.vivino.com/api/login`` with ``{email, password}``
  authenticates and returns the user profile with an API token.
- ``https://api.vivino.com`` serves user data (``/users/me``,
  ``/users/{id}/cellar``, ``/users/{id}/wishlist``) with a Bearer token.

Endpoint shapes are unofficial and may change, so all parsing is defensive:
records are matched against several known layouts and unknown fields are
ignored rather than raising.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

LOGIN_URL = "https://www.vivino.com/api/login"
API_BASE = "https://api.vivino.com"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
PAGE_SIZE = 50
MAX_PAGES = 40  # safety cap: 2000 records

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "application/json",
}

# Keys that may hold the API token in the login response
_TOKEN_KEYS = (
    "token", "api_token", "access_token", "jwt",
    "oauth_token", "bearer_token", "session_token",
)
# Keys that may hold the record list in cellar/wishlist responses
_LIST_KEYS = (
    "cellar", "cellar_wines", "user_cellar", "wishlist", "user_wines",
    "user_vintages", "records", "items", "matches", "vintages",
)


class VivinoAuthError(Exception):
    """Raised when Vivino login fails or the session is rejected."""


class VivinoConnectionError(Exception):
    """Raised when Vivino cannot be reached (network/server trouble)."""


class VivinoAccountClient:
    """Authenticated client for a user's Vivino account."""

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        """Initialize with account credentials."""
        self._hass = hass
        self._email = email
        self._password = password
        # Dedicated session: login sets Vivino cookies which must not leak
        # into (or be polluted by) HA's shared client session.
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._user_id: int | None = None
        self.alias: str = ""
        # True once a freshly logged-in session was still rejected; stops
        # every subsequent request from hammering the login endpoint again.
        self._session_rejected = False

    @property
    def user_id(self) -> int | None:
        """Return the Vivino user ID once logged in."""
        return self._user_id

    def reset_session_backoff(self) -> None:
        """Allow one fresh re-login attempt (called at the start of a sync)."""
        self._session_rejected = False

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the dedicated aiohttp session, creating it if needed."""
        if self._session is None or self._session.closed:
            self._session = async_create_clientsession(self._hass)
        return self._session

    # ── Authentication ───────────────────────────────────────────────

    async def async_login(self) -> dict[str, Any]:
        """Log in to Vivino and capture the API token and user id.

        Returns the user profile dict from the login response.
        Raises VivinoAuthError on bad credentials or unexpected responses.
        """
        session = self._get_session()
        payload = {"email": self._email, "password": self._password}

        try:
            async with session.post(
                LOGIN_URL, json=payload, headers=HEADERS, timeout=REQUEST_TIMEOUT
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = {}

                if resp.status >= 500:
                    raise VivinoConnectionError(
                        f"Vivino is unreachable (HTTP {resp.status})"
                    )
                if resp.status != 200 or not isinstance(data, dict):
                    message = ""
                    if isinstance(data, dict):
                        err = data.get("error")
                        if isinstance(err, dict):
                            message = err.get("message", "")
                        elif isinstance(err, str):
                            message = err
                    raise VivinoAuthError(
                        message or f"Vivino login failed (HTTP {resp.status})"
                    )
        except (VivinoAuthError, VivinoConnectionError):
            raise
        except Exception as err:
            raise VivinoConnectionError(
                f"Vivino login request failed: {err}"
            ) from err

        user = data.get("user") if isinstance(data.get("user"), dict) else data
        user_id = user.get("id") or data.get("id") or data.get("user_id")
        if isinstance(user_id, int):
            self._user_id = user_id
        self.alias = str(user.get("alias") or user.get("seo_name") or "")

        if not self._user_id:
            raise VivinoAuthError(
                "Vivino login succeeded but no user id was returned"
            )

        # A Vivino login can hand back several token-like fields, and only
        # some are valid Bearer tokens for api.vivino.com. Rather than guess
        # which field/source is right, collect every candidate and use the
        # first one that actually authenticates against a probe endpoint.
        self._token = await self._async_resolve_token(data)
        if not self._token:
            _LOGGER.warning(
                "Vivino login succeeded but no working API token was found "
                "(login response keys: %s); cellar/wishlist requests will "
                "be rejected",
                list(data.keys()),
            )

        _LOGGER.debug(
            "Logged in to Vivino as user %s (%s), token: %s",
            self._user_id, self.alias, "yes" if self._token else "no",
        )
        return user

    async def _async_resolve_token(self, data: dict[str, Any]) -> str | None:
        """Return the first candidate token that authenticates, or None.

        Candidates are gathered from the login response fields, then the
        logged-in web app pages, then session cookies. Each is probed against
        the user endpoint; the first that returns 200 wins. If none can be
        validated (e.g. Vivino is rate-limiting), fall back to the
        highest-priority candidate so a transient probe failure doesn't
        discard an otherwise-good token.
        """
        candidates: list[str] = []

        def _add(token: str | None) -> None:
            if token and token not in candidates:
                candidates.append(token)

        for token in self._extract_tokens(data):
            _add(token)
        _add(await self._async_scrape_token())
        for token in self._cookie_tokens():
            _add(token)

        if not candidates:
            return None

        for token in candidates:
            if await self._async_token_works(token):
                _LOGGER.debug("Validated Vivino API token (%d candidates)", len(candidates))
                return token

        _LOGGER.debug(
            "No Vivino token could be validated; using best-effort candidate"
        )
        return candidates[0]

    async def _async_token_works(self, token: str) -> bool:
        """Probe a token against the user endpoint; True if accepted."""
        session = self._get_session()
        headers = {**HEADERS, "Authorization": f"Bearer {token}"}
        try:
            async with session.get(
                f"{API_BASE}/users/{self._user_id}",
                headers=headers, timeout=REQUEST_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except Exception as err:
            _LOGGER.debug("Token probe failed: %s", err)
            return False

    def _cookie_tokens(self) -> list[str]:
        """Return token-like values from the session cookie jar."""
        tokens: list[str] = []
        try:
            for cookie in self._get_session().cookie_jar:
                if "token" in cookie.key.lower() and len(cookie.value) > 20:
                    tokens.append(cookie.value)
        except Exception as err:
            _LOGGER.debug("Cookie jar token scan failed: %s", err)
        return tokens

    async def _async_scrape_token(self) -> str | None:
        """Extract the API bearer token from a logged-in Vivino web page."""
        session = self._get_session()
        headers = {**HEADERS, "Accept": "text/html"}
        for url in ("https://www.vivino.com/wines", "https://www.vivino.com/"):
            try:
                async with session.get(
                    url, headers=headers, timeout=REQUEST_TIMEOUT,
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
                match = re.search(
                    r'"(?:api_token|apiToken|access_token|accessToken'
                    r'|oauth_token|oauthToken|bearer_token|bearerToken)"'
                    r'\s*:\s*"([A-Za-z0-9._\-]{20,})"',
                    html,
                )
                if match:
                    _LOGGER.debug("Scraped Vivino API token from %s", url)
                    return match.group(1)
            except Exception as err:
                _LOGGER.debug("Token scrape from %s failed: %s", url, err)
        return None

    @staticmethod
    def _extract_tokens(data: dict[str, Any]) -> list[str]:
        """Return all token-like values from the login response, in order."""
        tokens: list[str] = []
        containers: list[dict[str, Any]] = [data]
        for key in ("user", "session", "auth"):
            nested = data.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
        for container in containers:
            for key in _TOKEN_KEYS:
                token = container.get(key)
                if isinstance(token, str) and len(token) > 10 and token not in tokens:
                    tokens.append(token)
        return tokens

    async def async_verify(self) -> dict[str, Any]:
        """Verify credentials work; returns the user profile."""
        return await self.async_login()

    # ── Authenticated requests ───────────────────────────────────────

    async def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET from api.vivino.com with auth, re-logging-in once on 401/403."""
        if self._user_id is None:
            await self.async_login()

        for attempt in (1, 2):
            session = self._get_session()
            headers = dict(HEADERS)
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"

            had_token = self._token is not None
            async with session.get(
                f"{API_BASE}/{path.lstrip('/')}",
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if (
                    resp.status in (401, 403)
                    and attempt == 1
                    and not self._session_rejected
                ):
                    _LOGGER.debug(
                        "Vivino API %s returned %s, re-authenticating",
                        path, resp.status,
                    )
                    self._token = None
                    await self.async_login()
                    continue
                if resp.status in (401, 403):
                    self._session_rejected = True
                    # Include everything needed to diagnose remotely: did we
                    # even have a token, and what did Vivino answer?
                    server_msg = ""
                    try:
                        body = await resp.json(content_type=None)
                        err = body.get("error") if isinstance(body, dict) else None
                        if isinstance(err, dict):
                            server_msg = err.get("message", "")
                        elif isinstance(err, str):
                            server_msg = err
                    except Exception:
                        pass
                    detail = f"HTTP {resp.status}"
                    detail += ", bearer token attached" if had_token else (
                        ", NO bearer token available"
                    )
                    if server_msg:
                        detail += f", Vivino says: {server_msg!r}"
                    raise VivinoAuthError(
                        f"Vivino rejected the session for {path} ({detail})"
                    )
                if resp.status == 429:
                    raise RuntimeError(
                        f"Vivino is rate-limiting requests ({path} returned "
                        "HTTP 429) — wait a while before syncing again"
                    )
                if resp.status != 200:
                    raise RuntimeError(
                        f"Vivino API {path} returned HTTP {resp.status}"
                    )
                self._session_rejected = False
                return await resp.json(content_type=None)
        return None

    async def _fetch_paginated(self, path: str) -> list[dict[str, Any]]:
        """Fetch all pages of a user list endpoint."""
        records: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._api_get(
                path, params={"page": page, "limit": PAGE_SIZE}
            )
            batch = _extract_records(data)
            if not batch:
                if page == 1 and data:
                    # Response wasn't empty but had no recognizable record
                    # list — log its shape so parse failures are diagnosable.
                    _LOGGER.warning(
                        "Vivino %s response had no recognizable records "
                        "(type: %s, keys: %s)",
                        path,
                        type(data).__name__,
                        list(data.keys()) if isinstance(data, dict) else "n/a",
                    )
                break
            records.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return records

    async def _fetch_first_working(self, paths: tuple[str, ...]) -> list[dict[str, Any]]:
        """Fetch records from the first endpoint variant that responds.

        An endpoint that answers successfully with zero records is a valid
        outcome (an empty cellar/list) and stops the fallback chain; only
        rejected/failed endpoints advance to the next variant.
        """
        last_err: Exception | None = None
        for path in paths:
            try:
                return await self._fetch_paginated(path)
            except (VivinoAuthError, RuntimeError) as err:
                _LOGGER.debug("Vivino endpoint %s failed: %s", path, err)
                last_err = err
        if last_err:
            raise last_err
        return []

    async def async_get_cellar(self) -> list[dict[str, Any]]:
        """Return the user's Vivino cellar as parsed wine dicts."""
        raw = await self._fetch_first_working(
            (f"users/{self._user_id}/cellar", "cellars")
        )
        wines = [w for w in (_parse_user_wine(r) for r in raw) if w]
        _log_parse_outcome("cellar", raw, wines)
        return wines

    async def async_get_wishlist(self) -> list[dict[str, Any]]:
        """Return the user's Vivino wishlist as parsed wine dicts."""
        raw = await self._fetch_paginated(f"users/{self._user_id}/wishlist")
        wines = [w for w in (_parse_user_wine(r) for r in raw) if w]
        _log_parse_outcome("wishlist", raw, wines)
        return wines

    async def async_get_my_wines(self) -> list[dict[str, Any]]:
        """Return the user's rated/scanned wines ("My Wines").

        Unlike the cellar (a Vivino Premium feature), this covers every wine
        the user has rated or scanned. Two endpoint variants exist; try the
        user-scoped one first.
        """
        raw = await self._fetch_first_working(
            (f"users/{self._user_id}/vintages", "user_vintages")
        )
        wines = [w for w in (_parse_user_wine(r) for r in raw) if w]
        _log_parse_outcome("my wines", raw, wines)
        return wines


# ── Response parsing ─────────────────────────────────────────────────


def _log_parse_outcome(
    what: str, raw: list[dict[str, Any]], wines: list[dict[str, Any]]
) -> None:
    """Log fetch results, loudly when records existed but none parsed."""
    if raw and not wines:
        _LOGGER.warning(
            "Vivino %s returned %d records but none could be parsed; "
            "first record keys: %s — please report this structure",
            what, len(raw), list(raw[0].keys()),
        )
    else:
        _LOGGER.debug("Fetched %d %s wines from Vivino", len(wines), what)


def _extract_records(data: Any) -> list[dict[str, Any]]:
    """Pull the record list out of a cellar/wishlist response."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for key in _LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    # Single-key wrapper object, e.g. {"whatever": [...]}
    if len(data) == 1:
        value = next(iter(data.values()))
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def _parse_user_wine(record: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a Vivino cellar/wishlist record into our wine dict shape.

    Records typically nest as {bottle_count, vintage: {year, wine: {...}}}
    but may also put the vintage/wine at the top level.
    """
    try:
        vintage = record.get("vintage")
        if not isinstance(vintage, dict):
            vintage = record if record.get("wine") else {}
        wine = vintage.get("wine")
        if not isinstance(wine, dict):
            wine = record.get("wine") if isinstance(record.get("wine"), dict) else {}
        if not wine and not vintage:
            return None

        name = wine.get("name") or vintage.get("name") or record.get("name") or ""
        if not name:
            return None

        winery = wine.get("winery") or {}
        region = wine.get("region") or {}
        country = region.get("country") if isinstance(region, dict) else {}
        if not isinstance(country, dict):
            country = {}

        year = vintage.get("year") or record.get("year")
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None
        # Vivino uses year 0 / "N.V." for non-vintage wines
        if not year or year < 1800:
            year = None

        stats = wine.get("statistics") or vintage.get("statistics") or {}
        rating = stats.get("ratings_average")
        if not (isinstance(rating, (int, float)) and rating > 0):
            rating = None
        else:
            rating = round(float(rating), 1)
        ratings_count = stats.get("ratings_count")
        if not isinstance(ratings_count, int):
            ratings_count = None

        image_url = ""
        image = vintage.get("image") or wine.get("image") or {}
        if isinstance(image, dict):
            image_url = image.get("location") or ""
            if not image_url:
                variations = image.get("variations")
                if isinstance(variations, dict):
                    image_url = (
                        variations.get("bottle_medium")
                        or variations.get("bottle_large")
                        or ""
                    )
        if image_url.startswith("//"):
            image_url = "https:" + image_url

        grape = ""
        grapes = wine.get("grapes")
        if isinstance(grapes, list):
            grape = ", ".join(
                g.get("name", "") for g in grapes
                if isinstance(g, dict) and g.get("name")
            )

        alcohol = ""
        alc = wine.get("alcohol")
        if isinstance(alc, (int, float)) and alc > 0:
            alcohol = f"{alc}%"

        # How many bottles the user actually owns. None when Vivino sent no
        # count at all — importers treat that the same as owning zero.
        owned_count = None
        for key in ("cellar_count", "bottle_count", "count"):
            val = record.get(key)
            if isinstance(val, int) and val >= 0:
                owned_count = val
                break
        bottle_count = owned_count if owned_count and owned_count > 0 else 1

        # The user's own rating/note (present on "My Wines" records)
        user_rating = None
        user_note = ""
        review = record.get("review")
        if isinstance(review, dict):
            ur = review.get("rating")
            if isinstance(ur, (int, float)) and 0 < ur <= 5:
                user_rating = round(float(ur) * 2) / 2  # half-star steps
            note = review.get("note")
            if isinstance(note, str):
                user_note = note.strip()
        if user_rating is None:
            ur = record.get("rating")
            if isinstance(ur, (int, float)) and 0 < ur <= 5:
                user_rating = round(float(ur) * 2) / 2

        # Stable Vivino identity for dedupe across syncs: prefer vintage id
        vivino_id = vintage.get("id") or wine.get("id") or record.get("id")

        wine_type_id = wine.get("type_id")
        type_map = {1: "red", 2: "white", 3: "sparkling", 4: "rosé", 7: "dessert"}
        wine_type = type_map.get(wine_type_id, "red")

        return {
            "name": str(name),
            "winery": winery.get("name", "") if isinstance(winery, dict) else "",
            "region": region.get("name", "") if isinstance(region, dict) else "",
            "country": country.get("name", ""),
            "vintage": year,
            "type": wine_type,
            "grape_variety": grape,
            "rating": rating,
            "ratings_count": ratings_count,
            "image_url": image_url,
            "alcohol": alcohol,
            "user_rating": user_rating,
            "notes": user_note,
            "vivino_id": str(vivino_id) if vivino_id is not None else "",
            "bottle_count": bottle_count,
            "owned_count": owned_count,
            "source": "vivino_account",
        }
    except Exception as err:
        _LOGGER.debug("Could not parse Vivino record: %s", err)
        return None


# ── Sync into local storage ──────────────────────────────────────────


def _wine_key(wine: dict[str, Any]) -> tuple[str, str, Any]:
    """Fallback identity when a wine has no vivino_id."""
    return (
        (wine.get("name") or "").strip().lower(),
        (wine.get("winery") or "").strip().lower(),
        wine.get("vintage"),
    )


async def async_sync_from_vivino(
    hass: HomeAssistant,
    storage: Any,
    client: VivinoAccountClient,
    sync_cellar: bool = True,
    sync_wishlist: bool = True,
    sync_my_wines: bool = False,
) -> dict[str, Any]:
    """Import Vivino data: cellar and/or "My Wines" into the wine inventory,
    and the wishlist into the buy list.

    Existing entries are never modified or removed; only missing bottles are
    added (matched by Vivino vintage id, else by name+winery+vintage).
    Returns a result summary dict, also stored for the status sensor.
    """
    result: dict[str, Any] = {
        "cellar_total": 0,
        "cellar_imported": 0,
        "cellar_skipped_no_bottles": 0,
        "wishlist_total": 0,
        "wishlist_imported": 0,
        "my_wines_total": 0,
        "my_wines_imported": 0,
        "my_wines_skipped_no_bottles": 0,
        "errors": [],
    }

    client.reset_session_backoff()

    # Shared bookkeeping of what's already in the local cellar, so cellar
    # and My Wines imports in the same run dedupe against each other too.
    existing_ids: dict[str, int] = {}
    existing_keys: dict[tuple, int] = {}
    for wine in storage.wines:
        vid = wine.get("vivino_id")
        if vid:
            existing_ids[vid] = existing_ids.get(vid, 0) + 1
        key = _wine_key(wine)
        existing_keys[key] = existing_keys.get(key, 0) + 1

    def _import_wines(entries: list[dict[str, Any]], source: str) -> tuple[int, int]:
        """Add missing owned bottles to the local cellar.

        Entries whose Vivino bottle count is zero or missing are skipped —
        rated/drunk wines without bottles must not become inventory.
        Returns (imported, skipped) counts.
        """
        imported = 0
        skipped = 0
        for entry in entries:
            owned = entry.get("owned_count")
            if not owned or owned <= 0:
                skipped += 1
                continue
            wanted = entry.get("bottle_count", 1)
            vid = entry.get("vivino_id", "")
            key = _wine_key(entry)
            have = existing_ids.get(vid, 0) if vid else existing_keys.get(key, 0)
            for _ in range(max(0, wanted - have)):
                wine_data = {
                    k: v for k, v in entry.items()
                    if k not in ("bottle_count", "owned_count")
                }
                wine_data["cabinet_id"] = ""  # lands in the Unassigned tab
                wine_data["source"] = source
                storage.add_wine(wine_data)
                imported += 1
            # Track additions so a duplicate entry later in this same
            # sync doesn't import its bottles again
            if vid:
                existing_ids[vid] = max(have, wanted)
            existing_keys[key] = max(existing_keys.get(key, 0), wanted)
        if skipped:
            _LOGGER.debug(
                "Vivino %s: skipped %d wines with no bottles", source, skipped
            )
        return imported, skipped

    if sync_cellar:
        try:
            cellar = await client.async_get_cellar()
            # Total = bottles the user owns per Vivino, not raw record count
            result["cellar_total"] = sum(w.get("owned_count") or 0 for w in cellar)
            (
                result["cellar_imported"],
                result["cellar_skipped_no_bottles"],
            ) = _import_wines(cellar, "vivino_cellar")
        except VivinoAuthError as err:
            result["errors"].append(f"Cellar: {err}")
        except Exception as err:
            _LOGGER.warning("Vivino cellar sync failed: %s", err)
            result["errors"].append(f"Cellar: {err}")

    if sync_my_wines:
        try:
            my_wines = await client.async_get_my_wines()
            # Total = bottles the user owns per Vivino; rated wines without
            # bottles are counted separately as skipped
            result["my_wines_total"] = sum(
                w.get("owned_count") or 0 for w in my_wines
            )
            (
                result["my_wines_imported"],
                result["my_wines_skipped_no_bottles"],
            ) = _import_wines(my_wines, "vivino_my_wines")
        except VivinoAuthError as err:
            result["errors"].append(f"My Wines: {err}")
        except Exception as err:
            _LOGGER.warning("Vivino My Wines sync failed: %s", err)
            result["errors"].append(f"My Wines: {err}")

    if sync_wishlist:
        try:
            wishlist = await client.async_get_wishlist()
            result["wishlist_total"] = len(wishlist)

            buy_ids = {i.get("vivino_id") for i in storage.buy_list if i.get("vivino_id")}
            buy_keys = {_wine_key(i) for i in storage.buy_list}
            # Wines already in the cellar shouldn't be re-suggested to buy
            cellar_ids = {w.get("vivino_id") for w in storage.wines if w.get("vivino_id")}

            for entry in wishlist:
                vid = entry.get("vivino_id", "")
                if vid and (vid in buy_ids or vid in cellar_ids):
                    continue
                if _wine_key(entry) in buy_keys:
                    continue
                item = {k: v for k, v in entry.items() if k != "bottle_count"}
                storage.add_buy_list_item(item)
                result["wishlist_imported"] += 1
                if vid:
                    buy_ids.add(vid)
                buy_keys.add(_wine_key(entry))
        except VivinoAuthError as err:
            result["errors"].append(f"Wishlist: {err}")
        except Exception as err:
            _LOGGER.warning("Vivino wishlist sync failed: %s", err)
            result["errors"].append(f"Wishlist: {err}")

    result["last_sync"] = datetime.now(timezone.utc).isoformat()
    result["user_id"] = client.user_id
    result["alias"] = client.alias

    # Persist the result so the sync sensor survives restarts
    storage.set_vivino_sync_status(result)
    await storage.async_save()
    hass.data.setdefault(DOMAIN, {})["vivino_sync_status"] = result

    # Always fire so the card and the Vivino sync sensor refresh
    hass.bus.async_fire(f"{DOMAIN}_updated")

    _LOGGER.info(
        "Vivino sync: %d/%d cellar bottles, %d/%d my-wines, %d/%d wishlist "
        "imported, %d errors",
        result["cellar_imported"], result["cellar_total"],
        result["my_wines_imported"], result["my_wines_total"],
        result["wishlist_imported"], result["wishlist_total"],
        len(result["errors"]),
    )
    return result
