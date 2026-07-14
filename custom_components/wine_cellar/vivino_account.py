"""Vivino cellar connection via the website's Inertia.js endpoint.

Vivino has no public read API for your own cellar. The website authenticates
with a Rails/Devise **HttpOnly session cookie** (no bearer token, invisible to
page JavaScript) and serves the cellar from an **Inertia.js** endpoint on
``www.vivino.com`` — not the ``api.vivino.com`` mobile backend.

Home Assistant cannot drive a browser to log in, so the user pastes their
Vivino session cookie (copied once from their browser) plus their cellar page
URL. This client replays that cookie against the Inertia cellar endpoint with
the ``X-Inertia`` headers, pages through the results, and parses the wine
records. When the cookie expires the endpoint stops returning JSON, which is
surfaced as an auth error so the integration can prompt for a fresh cookie.

Endpoint/response shapes are unofficial and may change, so parsing is
defensive: the wine list is located structurally within the Inertia props and
unknown fields are ignored rather than raising.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

WWW_BASE = "https://www.vivino.com"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
MAX_PAGES = 60  # safety cap

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


class VivinoAuthError(Exception):
    """Raised when Vivino login fails or the session is rejected."""


class VivinoConnectionError(Exception):
    """Raised when Vivino cannot be reached (network/server trouble)."""


def _normalize_cookie(raw: str) -> str:
    """Normalize a pasted cookie into a Cookie header value.

    Accepts a full ``name=value; name2=value2`` header (best — copied from the
    browser's Network tab), a single ``name=value`` pair, or a bare session
    value (wrapped as ``_ruby-web_session=<value>`` — Vivino's Rails session
    cookie name).
    """
    raw = (raw or "").strip().strip(";").strip()
    if "=" in raw:
        return raw
    return f"_ruby-web_session={raw}"


def _cellar_path(cellar_url: str) -> str:
    """Extract the path (e.g. /sv/cellars/5398850) from a cellar URL."""
    raw = (cellar_url or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        return parsed.path or "/"
    return raw if raw.startswith("/") else "/" + raw


def _parse_data_page(html: str) -> dict[str, Any] | None:
    """Extract and decode the Inertia ``#app data-page`` JSON from cellar HTML."""
    m = re.search(r'data-page="([^"]+)"', html)
    if not m:
        return None
    raw = (m.group(1).replace("&quot;", '"').replace("&amp;", "&")
           .replace("&#39;", "'").replace("&#34;", '"'))
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _looks_like_wine(obj: Any) -> bool:
    """Heuristic: does this object look like a cellar/wine record?"""
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("wine"), dict) or isinstance(obj.get("vintage"), dict):
        return True
    return bool(obj.get("name")) and (
        bool(obj.get("winery")) or bool(obj.get("region"))
    )


def _find_wine_array(node: Any) -> list[dict[str, Any]]:
    """Recursively locate the largest array of wine-like records in Inertia props."""
    best: list[dict[str, Any]] = []

    def visit(n: Any) -> None:
        nonlocal best
        if isinstance(n, list):
            wine_like = [x for x in n if _looks_like_wine(x)]
            if n and len(wine_like) >= max(1, len(n) // 2) and len(n) > len(best):
                best = [x for x in n if isinstance(x, dict)]
            for x in n:
                visit(x)
        elif isinstance(n, dict):
            for v in n.values():
                visit(v)

    visit(node)
    return best


class VivinoAccountClient:
    """Reads a user's Vivino cellar via the Inertia endpoint using a session cookie."""

    def __init__(self, hass: HomeAssistant, session_cookie: str, cellar_url: str) -> None:
        """Initialize with a pasted session cookie and the cellar page URL."""
        self._hass = hass
        self._cookie = _normalize_cookie(session_cookie)
        self._path = _cellar_path(cellar_url)
        self._version: str | None = None
        # Kept for compatibility with the shared sync/sensor code.
        self.alias: str = ""
        self.user_id: int | None = None
        self._session_rejected = False
        self.token_diagnostics: dict[str, Any] = {}
        # Dedicated session so Vivino cookies never leak into HA's shared one.
        self._session: aiohttp.ClientSession | None = None

    def reset_session_backoff(self) -> None:
        """No-op kept for API parity with the previous client."""
        self._session_rejected = False

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the dedicated aiohttp session, creating it if needed.

        Uses a DummyCookieJar so aiohttp never stores or auto-sends cookies —
        the pasted session cookie is supplied verbatim via the Cookie header,
        avoiding duplicate/rewritten Cookie headers (which corrupt the
        percent-encoded session value).
        """
        if self._session is None or self._session.closed:
            self._session = async_create_clientsession(
                self._hass, cookie_jar=aiohttp.DummyCookieJar()
            )
        return self._session

    def _headers(self, inertia: bool = False) -> dict[str, str]:
        h = {**HEADERS, "Cookie": self._cookie}
        if inertia:
            h["X-Inertia"] = "true"
            h["X-Requested-With"] = "XMLHttpRequest"
            if self._version:
                h["X-Inertia-Version"] = self._version
        return h

    async def _get_props(self, page: int) -> dict[str, Any]:
        """Fetch one page of cellar Inertia props.

        Page 1 is read from the cellar page's rendered HTML — its embedded
        ``#app data-page`` payload carries both the Inertia asset version and
        the first page of wines. Later pages use the Inertia XHR protocol with
        that version. A redirect to the login page (or an HTML body where JSON
        was expected) means the session cookie is missing or expired.
        """
        if not self._path:
            raise VivinoAuthError("No Vivino cellar URL configured")
        session = self._get_session()

        if page == 1:
            try:
                async with session.get(
                    f"{WWW_BASE}{self._path}",
                    headers={**HEADERS, "Cookie": self._cookie,
                             "Accept": "text/html"},
                    timeout=REQUEST_TIMEOUT, allow_redirects=False,
                ) as resp:
                    _LOGGER.debug("Vivino cellar page -> HTTP %s", resp.status)
                    if resp.status in (301, 302, 303, 307, 308):
                        raise VivinoAuthError(
                            "Vivino redirected to login — the session cookie is "
                            "missing or expired; paste a fresh one"
                        )
                    if resp.status != 200:
                        raise RuntimeError(
                            f"Vivino cellar page returned HTTP {resp.status}"
                        )
                    html = await resp.text()
            except (VivinoAuthError, VivinoConnectionError, RuntimeError):
                raise
            except Exception as err:
                raise VivinoConnectionError(
                    f"Could not reach Vivino: {err}"
                ) from err

            page_obj = _parse_data_page(html)
            if not page_obj:
                raise VivinoAuthError(
                    "No cellar data found on the page — the session cookie is "
                    "likely invalid or expired; paste a fresh one"
                )
            self._version = page_obj.get("version")
            props = page_obj.get("props")
            return props if isinstance(props, dict) else {}

        # Pages 2+ via the Inertia XHR protocol
        url = f"{WWW_BASE}{self._path}?page={page}"
        for attempt in (1, 2):
            try:
                async with session.get(
                    url, headers=self._headers(inertia=True),
                    timeout=REQUEST_TIMEOUT, allow_redirects=False,
                ) as resp:
                    if resp.status == 409 and attempt == 1:
                        # Asset version drifted → re-read it from the page
                        await self._get_props(1)
                        continue
                    if resp.status in (301, 302, 303, 307, 308):
                        raise VivinoAuthError(
                            "Vivino session expired mid-sync; paste a fresh cookie"
                        )
                    if resp.status != 200:
                        raise RuntimeError(
                            f"Vivino cellar XHR returned HTTP {resp.status}"
                        )
                    text = await resp.text()
            except (VivinoAuthError, VivinoConnectionError, RuntimeError):
                raise
            except Exception as err:
                raise VivinoConnectionError(f"Vivino request failed: {err}") from err

            try:
                data = json.loads(text)
            except ValueError:
                raise VivinoAuthError(
                    "Vivino returned a web page instead of cellar data; paste a "
                    "fresh session cookie"
                )
            props = data.get("props") if isinstance(data, dict) else None
            return props if isinstance(props, dict) else {}
        return {}

    async def async_verify(self) -> dict[str, Any]:
        """Validate the cookie + cellar URL by loading the cellar page."""
        props = await self._get_props(1)
        wines = _find_wine_array(props)
        return {"reachable": True, "first_page_wines": len(wines)}

    async def async_get_cellar(self) -> list[dict[str, Any]]:
        """Return the user's Vivino cellar as parsed wine dicts."""
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            props = await self._get_props(page)
            batch = _find_wine_array(props)
            if not batch:
                if page == 1 and props:
                    _LOGGER.warning(
                        "Vivino cellar page had no recognizable wine list; "
                        "props keys: %s", list(props.keys()),
                    )
                break
            new = 0
            for rec in batch:
                rid = str(
                    rec.get("id")
                    or (rec.get("vintage") or {}).get("id")
                    or f"{page}:{rec.get('name','')}"
                )
                if rid in seen:
                    continue
                seen.add(rid)
                records.append(rec)
                new += 1
            if new == 0:  # nothing new on this page → done
                break

        wines = [w for w in (_parse_user_wine(r) for r in records) if w]
        _log_parse_outcome("cellar", records, wines)
        return wines

    async def async_get_wishlist(self) -> list[dict[str, Any]]:
        """Wishlist import is not supported via the Inertia cellar endpoint."""
        return []

    async def async_get_my_wines(self) -> list[dict[str, Any]]:
        """"My Wines" import is not supported via the Inertia cellar endpoint."""
        return []



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

        # The user's own rating/note (present on cellar / "My Wines" records)
        user_rating = None
        user_note = ""
        review = record.get("user_review") or record.get("review")
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
    result["auth"] = client.token_diagnostics

    # Persist the result so the sync sensor survives restarts
    storage.set_vivino_sync_status(result)
    await storage.async_save()
    hass.data.setdefault(DOMAIN, {})["vivino_sync_status"] = result

    # Always fire so the card and the Vivino sync sensor refresh
    hass.bus.async_fire(f"{DOMAIN}_updated")

    # Prompt the user to refresh the cookie when the session has expired;
    # clear the prompt once a sync authenticates again.
    try:
        from homeassistant.components import persistent_notification
        auth_failed = any(
            "cookie" in e.lower() or "session" in e.lower()
            for e in result["errors"]
        )
        if auth_failed and not result["cellar_imported"]:
            persistent_notification.async_create(
                hass,
                "Cork Dork could not read your Vivino cellar — the session "
                "cookie has expired. Open Settings > Devices & Services > "
                "Cork Dork > Configure and paste a fresh Vivino session cookie.",
                title="Vivino session expired",
                notification_id="wine_cellar_vivino_reauth",
            )
        elif not result["errors"]:
            persistent_notification.async_dismiss(
                hass, "wine_cellar_vivino_reauth"
            )
    except Exception:  # notifications are best-effort
        pass

    _LOGGER.info(
        "Vivino sync: %d/%d cellar bottles, %d/%d my-wines, %d/%d wishlist "
        "imported, %d errors",
        result["cellar_imported"], result["cellar_total"],
        result["my_wines_imported"], result["my_wines_total"],
        result["wishlist_imported"], result["wishlist_total"],
        len(result["errors"]),
    )
    return result
