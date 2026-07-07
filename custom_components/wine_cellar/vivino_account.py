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
_TOKEN_KEYS = ("token", "api_token", "access_token", "jwt")
# Keys that may hold the record list in cellar/wishlist responses
_LIST_KEYS = (
    "cellar", "cellar_wines", "user_cellar", "wishlist", "user_wines",
    "records", "items", "matches", "vintages",
)


class VivinoAuthError(Exception):
    """Raised when Vivino login fails or the session is rejected."""


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

    @property
    def user_id(self) -> int | None:
        """Return the Vivino user ID once logged in."""
        return self._user_id

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
        except VivinoAuthError:
            raise
        except Exception as err:
            raise VivinoAuthError(f"Vivino login request failed: {err}") from err

        user = data.get("user") if isinstance(data.get("user"), dict) else data
        self._token = self._extract_token(data)
        user_id = user.get("id") or data.get("id") or data.get("user_id")
        if isinstance(user_id, int):
            self._user_id = user_id
        self.alias = str(user.get("alias") or user.get("seo_name") or "")

        if not self._user_id:
            raise VivinoAuthError(
                "Vivino login succeeded but no user id was returned"
            )
        if not self._token:
            _LOGGER.debug(
                "Vivino login response had no token field (keys: %s); "
                "falling back to cookie session",
                list(data.keys()),
            )

        _LOGGER.debug("Logged in to Vivino as user %s (%s)", self._user_id, self.alias)
        return user

    @staticmethod
    def _extract_token(data: dict[str, Any]) -> str | None:
        """Find an API token in the login response, wherever Vivino put it."""
        candidates: list[dict[str, Any]] = [data]
        for key in ("user", "session", "auth"):
            nested = data.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        for container in candidates:
            for key in _TOKEN_KEYS:
                token = container.get(key)
                if isinstance(token, str) and len(token) > 10:
                    return token
        return None

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

            async with session.get(
                f"{API_BASE}/{path.lstrip('/')}",
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403) and attempt == 1:
                    _LOGGER.debug(
                        "Vivino API %s returned %s, re-authenticating",
                        path, resp.status,
                    )
                    self._token = None
                    await self.async_login()
                    continue
                if resp.status in (401, 403):
                    raise VivinoAuthError(
                        f"Vivino rejected the session for {path} "
                        f"(HTTP {resp.status})"
                    )
                if resp.status != 200:
                    raise RuntimeError(
                        f"Vivino API {path} returned HTTP {resp.status}"
                    )
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

    async def async_get_cellar(self) -> list[dict[str, Any]]:
        """Return the user's Vivino cellar as parsed wine dicts."""
        raw = await self._fetch_paginated(f"users/{self._user_id}/cellar")
        wines = [w for w in (_parse_user_wine(r) for r in raw) if w]
        _log_parse_outcome("cellar", raw, wines)
        return wines

    async def async_get_wishlist(self) -> list[dict[str, Any]]:
        """Return the user's Vivino wishlist as parsed wine dicts."""
        raw = await self._fetch_paginated(f"users/{self._user_id}/wishlist")
        wines = [w for w in (_parse_user_wine(r) for r in raw) if w]
        _log_parse_outcome("wishlist", raw, wines)
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

        bottle_count = record.get("bottle_count") or record.get("count") or 1
        if not isinstance(bottle_count, int) or bottle_count < 1:
            bottle_count = 1

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
            "vivino_id": str(vivino_id) if vivino_id is not None else "",
            "bottle_count": bottle_count,
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
) -> dict[str, Any]:
    """Import the Vivino cellar into wines and the wishlist into the buy list.

    Existing entries are never modified or removed; only missing bottles are
    added (matched by Vivino vintage id, else by name+winery+vintage).
    Returns a result summary dict, also stored for the status sensor.
    """
    result: dict[str, Any] = {
        "cellar_total": 0,
        "cellar_imported": 0,
        "wishlist_total": 0,
        "wishlist_imported": 0,
        "errors": [],
    }

    if sync_cellar:
        try:
            cellar = await client.async_get_cellar()
            result["cellar_total"] = sum(w.get("bottle_count", 1) for w in cellar)

            existing_ids: dict[str, int] = {}
            existing_keys: dict[tuple, int] = {}
            for wine in storage.wines:
                vid = wine.get("vivino_id")
                if vid:
                    existing_ids[vid] = existing_ids.get(vid, 0) + 1
                key = _wine_key(wine)
                existing_keys[key] = existing_keys.get(key, 0) + 1

            for entry in cellar:
                wanted = entry.get("bottle_count", 1)
                vid = entry.get("vivino_id", "")
                key = _wine_key(entry)
                have = existing_ids.get(vid, 0) if vid else existing_keys.get(key, 0)
                for _ in range(max(0, wanted - have)):
                    wine_data = {k: v for k, v in entry.items() if k != "bottle_count"}
                    wine_data["cabinet_id"] = ""  # lands in the Unassigned tab
                    storage.add_wine(wine_data)
                    result["cellar_imported"] += 1
                # Track additions so a duplicate entry later in this same
                # sync doesn't import its bottles again
                if vid:
                    existing_ids[vid] = max(have, wanted)
                else:
                    existing_keys[key] = max(have, wanted)
        except VivinoAuthError as err:
            result["errors"].append(f"Cellar: {err}")
        except Exception as err:
            _LOGGER.warning("Vivino cellar sync failed: %s", err)
            result["errors"].append(f"Cellar: {err}")

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
        "Vivino sync: %d/%d cellar bottles imported, %d/%d wishlist wines "
        "imported, %d errors",
        result["cellar_imported"], result["cellar_total"],
        result["wishlist_imported"], result["wishlist_total"],
        len(result["errors"]),
    )
    return result
