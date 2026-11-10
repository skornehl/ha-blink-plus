"""Hardened Blink OAuth v2 login for Blink Plus.

Blink moved to an OAuth v2 authorization-code + PKCE login in 2025/2026, and the
blinkpy versions shipped with Home Assistant have known bugs:

* the hard-coded hardware_id "Home Assistant" / "Blinkpy" is rejected (HTTP 406)
* Blink now answers the sign-in with HTTP 202 + JSON when it wants a 2FA code,
  but older blinkpy only treats 412 as "2FA needed"
* session cookies get lost between sign-in and 2FA verify ("Empty Cookies")
* token refresh goes through the legacy password endpoint with "Blinkpy"
* every failure is reported as the same "Invalid authentication"

This module replaces blinkpy's login/refresh while keeping the rest of blinkpy
(homescreen, cameras, commands) untouched. It is used with a dedicated aiohttp
session that has its own cookie jar.
"""

from __future__ import annotations

import base64
from collections import deque
import hashlib
from html.parser import HTMLParser
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import uuid

import aiohttp
from blinkpy.auth import (
    Auth,
    BlinkTwoFARequiredError,
    LoginError,
    TokenRefreshFailed,
    UnauthorizedError,
)

_LOGGER = logging.getLogger(__name__)

OAUTH_BASE = "https://api.oauth.blink.com"
AUTHORIZE_URL = f"{OAUTH_BASE}/oauth/v2/authorize"
SIGNIN_URL = f"{OAUTH_BASE}/oauth/v2/signin"
TWOFA_URL = f"{OAUTH_BASE}/oauth/v2/2fa/verify"
TOKEN_URL = f"{OAUTH_BASE}/oauth/token"
TIER_URL = "https://rest-prod.immedia-semi.com/api/v1/users/tier_info"

CLIENT_ID = "ios"
REDIRECT_URI = "immedia-blink://applinks.blink.com/signin/callback"
SCOPE = "client"
WEB_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.1 Mobile/15E148 Safari/604.1"
)
TOKEN_UA = "Blink/2511191620 CFNetwork/3860.200.71 Darwin/25.1.0"
REDIRECTS = (301, 302, 303, 307, 308)
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


class BlinkAuthStepError(LoginError):
    """A specific OAuth step failed (carries status and a short body)."""

    def __init__(self, step: str, status: int | None, body: str = "") -> None:
        """Initialize."""
        self.step = step
        self.status = status
        self.body = (body or "")[:300]
        super().__init__(f"{step} failed (HTTP {status}) {self.body}")


class BlinkBadCredentials(BlinkAuthStepError):
    """Blink rejected the email / password or 2FA code."""


class BlinkRateLimited(BlinkAuthStepError):
    """Blink is throttling login / 2FA attempts."""


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


class _CsrfParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.csrf: str | None = None
        self._inside = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("id") == "oauth-args":
            self._inside = True
        elif tag in ("input", "meta") and a.get("name") in ("csrf-token", "_csrf"):
            self.csrf = self.csrf or a.get("value") or a.get("content")

    def handle_data(self, data):
        if self._inside:
            try:
                self.csrf = json.loads(data).get("csrf-token") or self.csrf
            except (ValueError, AttributeError):
                pass
            self._inside = False


def _looks_like_2fa(body: str) -> bool:
    try:
        data = json.loads(body)
    except ValueError:
        return "2fa" in body.lower() or "tsv" in body.lower()
    if not isinstance(data, dict):
        return False
    return bool(
        data.get("tsv_state")
        or data.get("tsv_methods")
        or data.get("next_time_in_secs")
        or str(data.get("status", "")).lower() in ("2fa-required", "tsv-required")
    )


def _rate_limited(status: int, body: str) -> bool:
    return status == 429 or "rate_limit" in body.lower() or "too many" in body.lower()


class BlinkPlusAuth(Auth):
    """blinkpy Auth with a working OAuth v2 login and refresh."""

    def __init__(self, login_data=None, **kwargs: Any) -> None:
        """Initialize, making sure hardware_id is a stable uppercase UUID."""
        super().__init__(login_data, **kwargs)
        hwid = (login_data or {}).get("hardware_id")
        try:
            uuid.UUID(str(hwid))
            self.hardware_id = str(hwid).upper()
        except (TypeError, ValueError):
            self.hardware_id = str(uuid.uuid4()).upper()
        self._pending: dict[str, str] | None = None
        # Last Blink API calls (for diagnostics: what is keeping the sync busy?)
        self.activity: deque[dict[str, Any]] = deque(maxlen=300)

    async def query(self, url=None, data=None, headers=None, reqtype="get", **kwargs):
        """blinkpy's request entry point, with a call log."""
        started = time.time()
        entry: dict[str, Any] = {
            "time": time.strftime("%H:%M:%S", time.localtime(started)),
            "method": str(reqtype).upper(),
            "path": urlparse(str(url)).path if url else "",
        }
        try:
            result = await super().query(
                url=url, data=data, headers=headers, reqtype=reqtype, **kwargs
            )
        except Exception as err:
            entry["result"] = f"error {type(err).__name__}"
            raise
        else:
            if isinstance(result, dict):
                entry["result"] = str(
                    result.get("message") or result.get("code") or "ok"
                )[:80]
            else:
                entry["result"] = "ok" if result is not None else "no response"
            return result
        finally:
            entry["ms"] = int((time.time() - started) * 1000)
            self.activity.append(entry)
            _LOGGER.debug(
                "Blink API %s %s -> %s (%sms)",
                entry["method"], entry["path"], entry.get("result"), entry["ms"],
            )

    # ------------------------------------------------------------ public API
    async def startup(self) -> None:
        """Get a valid token: refresh if we can, else full login."""
        self.validate_login()
        if self.refresh_token:
            try:
                await self._refresh()
                return
            except (TokenRefreshFailed, UnauthorizedError) as err:
                _LOGGER.info("Blink token refresh failed (%s), doing a full login", err)
        await self._login()

    async def refresh_tokens(self, refresh: bool = False) -> bool:
        """Called by blinkpy's query() when the token is about to expire."""
        try:
            await self._refresh()
        except UnauthorizedError:
            raise
        except Exception as err:  # noqa: BLE001
            raise TokenRefreshFailed(str(err)) from err
        return True

    async def complete_2fa_login(self, twofa_code: str) -> bool:
        """Verify the 2FA code and finish the login."""
        if not self._pending:
            raise BlinkAuthStepError("2fa", None, "No login in progress, start again")
        code = (twofa_code or "").strip()
        resp = await self._post(
            TWOFA_URL,
            headers=self._form_headers(),
            data={
                "2fa_code": code,
                "csrf-token": self._pending["csrf"],
                "remember_me": "false",
            },
            allow_redirects=False,
        )
        body = await resp.text()
        _LOGGER.debug("Blink 2FA verify: HTTP %s %s", resp.status, body[:300])
        if _rate_limited(resp.status, body):
            raise BlinkRateLimited("2fa", resp.status, body)
        ok = resp.status in (200, 201, *REDIRECTS)
        if ok and resp.status in (200, 201):
            try:
                ok = json.loads(body).get("status") in ("auth-completed", "success", None)
            except ValueError:
                ok = True
        if not ok:
            if resp.status in (400, 401, 403, 422):
                raise BlinkBadCredentials("2fa", resp.status, body)
            raise BlinkAuthStepError("2fa", resp.status, body)
        await self._finish(self._pending["verifier"])
        self._pending = None
        return True

    # --------------------------------------------------------------- internals
    async def _get(self, url: str, **kwargs: Any):
        return await self.session.get(url, timeout=HTTP_TIMEOUT, **kwargs)

    async def _post(self, url: str, **kwargs: Any):
        return await self.session.post(url, timeout=HTTP_TIMEOUT, **kwargs)

    def _web_headers(self) -> dict[str, str]:
        return {
            "User-Agent": WEB_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _form_headers(self) -> dict[str, str]:
        return {
            "User-Agent": WEB_UA,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": OAUTH_BASE,
            "Referer": SIGNIN_URL,
        }

    async def _login(self) -> None:
        email = (self.data.get("username") or "").strip()
        password = self.data.get("password") or ""
        verifier, challenge = _pkce()

        resp = await self._get(
            AUTHORIZE_URL,
            headers=self._web_headers(),
            params={
                "app_brand": "blink",
                "app_version": "50.1",
                "client_id": CLIENT_ID,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "device_brand": "Apple",
                "device_model": "iPhone16,1",
                "device_os_version": "26.1",
                "hardware_id": self.hardware_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
            },
        )
        body = await resp.text()
        _LOGGER.debug("Blink authorize: HTTP %s (final %s)", resp.status, resp.url)
        if resp.status != 200:
            if _rate_limited(resp.status, body):
                raise BlinkRateLimited("authorize", resp.status, body)
            raise BlinkAuthStepError("authorize", resp.status, body)

        csrf = self._csrf_from(body)
        if not csrf:
            resp = await self._get(SIGNIN_URL, headers=self._web_headers())
            body = await resp.text()
            csrf = self._csrf_from(body)
        if not csrf:
            raise BlinkAuthStepError("signin page", resp.status, "no csrf-token found")

        resp = await self._post(
            SIGNIN_URL,
            headers=self._form_headers(),
            data={"username": email, "password": password, "csrf-token": csrf},
            allow_redirects=False,
        )
        body = await resp.text()
        _LOGGER.debug("Blink signin: HTTP %s %s", resp.status, body[:300])

        if _rate_limited(resp.status, body):
            raise BlinkRateLimited("signin", resp.status, body)
        if resp.status == 412 or (resp.status in (200, 202) and _looks_like_2fa(body)):
            self._pending = {"csrf": csrf, "verifier": verifier}
            _LOGGER.info("Blink wants a 2FA code (sent by email/SMS)")
            raise BlinkTwoFARequiredError
        if resp.status in REDIRECTS:
            await self._finish(verifier)
            return
        if resp.status in (400, 401, 403, 422) or "invalid" in body.lower():
            raise BlinkBadCredentials("signin", resp.status, body)
        raise BlinkAuthStepError("signin", resp.status, body)

    def _csrf_from(self, html: str) -> str | None:
        parser = _CsrfParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001
            return None
        return parser.csrf

    async def _finish(self, verifier: str) -> None:
        resp = await self._get(
            AUTHORIZE_URL,
            headers={"User-Agent": WEB_UA, "Accept": "*/*", "Referer": SIGNIN_URL},
            allow_redirects=False,
        )
        location = resp.headers.get("Location", "")
        code = parse_qs(urlparse(location).query).get("code", [None])[0]
        if resp.status not in REDIRECTS or not code:
            raise BlinkAuthStepError(
                "authorization code", resp.status, location or await resp.text()
            )
        # Only the redirect header was needed: hand the connection back.
        resp.release()
        token = await self._token_request(
            {
                "app_brand": "blink",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "hardware_id": self.hardware_id,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
            },
            "token exchange",
        )
        await self._apply_token(token)

    async def _refresh(self) -> None:
        if not self.refresh_token:
            raise TokenRefreshFailed("no refresh token")
        token = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "hardware_id": self.hardware_id,
            },
            "token refresh",
        )
        await self._apply_token(token)
        if self.callback:
            self.callback()

    async def _token_request(self, data: dict[str, str], step: str) -> dict[str, Any]:
        resp = await self._post(
            TOKEN_URL,
            headers={
                "User-Agent": TOKEN_UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
            },
            data=urlencode(data),
        )
        body = await resp.text()
        if resp.status == 200:
            return json.loads(body)
        _LOGGER.debug("Blink %s: HTTP %s %s", step, resp.status, body[:300])
        if step == "token refresh" and resp.status in (400, 401):
            raise UnauthorizedError
        if _rate_limited(resp.status, body):
            raise BlinkRateLimited(step, resp.status, body)
        raise BlinkAuthStepError(step, resp.status, body)

    async def _apply_token(self, token: dict[str, Any]) -> None:
        self.token = token.get("access_token")
        if token.get("refresh_token"):
            self.refresh_token = token["refresh_token"]
        self.expires_in = token.get("expires_in", 3600)
        self.expiration_date = time.time() + float(self.expires_in)
        if not (self.host and self.region_id and self.account_id):
            resp = await self._get(
                TIER_URL, headers={"Authorization": f"Bearer {self.token}"}
            )
            if resp.status != 200:
                raise BlinkAuthStepError("tier info", resp.status, await resp.text())
            self.tier_info = await resp.json(content_type=None)
            self.extract_tier_info()
            _LOGGER.debug("Blink tier %s, account %s", self.region_id, self.account_id)
