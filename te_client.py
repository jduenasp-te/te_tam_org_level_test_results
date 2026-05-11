"""ThousandEyes API v7 client.

Uses decorator patterns:
- ``retry`` — exponential-backoff retry decorator factory.
- ``require_token`` — guards methods that need a token.
- Rate-limit headers (``x-organization-rate-limit-*``) drive an adaptive
  per-call delay.
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Base API delay in seconds (lower bound between calls).
API_DELAY = 0.1


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def retry(
    max_attempts: int = 3,
    backoff: float = 1.0,
    allowed_exceptions: Tuple = (requests.RequestException,),
):
    """Retry a function that may raise network-related exceptions."""

    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 1
            while True:
                try:
                    return func(*args, **kwargs)
                except allowed_exceptions as exc:
                    if attempt >= max_attempts:
                        raise
                    sleep_time = backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "[retry] %s. Retrying in %.1fs (attempt %d/%d)",
                        exc,
                        sleep_time,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(sleep_time)
                    attempt += 1

        return wrapper

    return decorator


def require_token(func: Callable):
    """Ensure the client has a token set before calling *func*."""

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        token = getattr(self, "token", None)
        if not token:
            raise ValueError("API token is missing.")
        return func(self, *args, **kwargs)

    return wrapper


def _rate_limit_sleep(response: requests.Response) -> None:
    """Adaptive sleep based on remaining org rate-limit budget."""
    headers = response.headers
    try:
        rate_limit = int(headers.get("x-organization-rate-limit-limit", 0))
        remaining = int(headers.get("x-organization-rate-limit-remaining", 0))
    except (TypeError, ValueError):
        time.sleep(API_DELAY)
        return

    if not rate_limit:
        time.sleep(API_DELAY)
        return

    pct = (remaining * 100.0) / rate_limit

    # Tiered back-off — the lower the headroom, the longer we sleep.
    tiers = (
        (5, 60, "WARNING: rate-limit < 5%"),
        (10, 20, "WARNING: rate-limit < 10%"),
        (20, 10, "WARNING: rate-limit < 20%"),
        (50, 2, "NOTICE: rate-limit < 50%"),
        (80, 1, "INFO: rate-limit < 80%"),
    )
    for threshold, extra, msg in tiers:
        if pct < threshold:
            if threshold <= 50:
                logger.warning(
                    "%s — sleeping %.1fs (remaining=%d/%d)",
                    msg, API_DELAY + extra, remaining, rate_limit,
                )
            time.sleep(API_DELAY + extra)
            return
    time.sleep(API_DELAY)


# ---------------------------------------------------------------------------
# ThousandEyes API client
# ---------------------------------------------------------------------------
class ThousandEyesClient:
    """Thin API client for ThousandEyes API v7."""

    BASE_URL = "https://api.thousandeyes.com/v7"

    def __init__(self, token: str, timeout: int = 20):
        self.token = (token or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            }
        )

    # -- core HTTP -------------------------------------------------------
    @require_token
    @retry(max_attempts=3, backoff=1.0)
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # ``path`` may be either a relative path ("/foo") or an absolute URL
        # returned by a HAL ``_links.next.href``. Both are supported so that
        # paginated endpoints can be walked transparently.
        if path.startswith("http://") or path.startswith("https://"):
            url = path
            request_params = params or {}
        else:
            url = f"{self.BASE_URL}{path}"
            request_params = params or {}
        resp = self.session.get(url, params=request_params, timeout=self.timeout)
        # Surface 401/403 as auth errors clearly.
        if resp.status_code in (401, 403):
            raise PermissionError(f"Auth failed for {path} (HTTP {resp.status_code})")
        resp.raise_for_status()
        _rate_limit_sleep(resp)
        try:
            return resp.json()
        except ValueError:
            return {}

    def _get_paginated_results(self, path: str, params: Dict[str, Any],
                               max_pages: int = 50) -> Dict[str, Any]:
        """Fetch a paginated ``/test-results/...`` endpoint and concatenate
        every page's ``results`` array into the first payload.

        ThousandEyes returns results in chronological order within the
        requested ``window``. The first page therefore covers the *oldest*
        slice of the window — without following ``_links.next`` we never
        see anything newer than what fits in one page, which manifests as
        a growing gap between the cache's newest round and "now".

        ``max_pages`` is a safety cap so a pathological response cannot
        loop forever; 50 pages is enough for several days of high-cadence
        tests with many agents.
        """
        first = self._get(path, params=params)
        combined_results = list(first.get("results") or [])
        next_link = (((first.get("_links") or {}).get("next") or {}).get("href"))
        pages = 1
        seen_next: set = set()
        while next_link and pages < max_pages:
            if next_link in seen_next:
                logger.warning("Pagination loop detected for %s; stopping.", path)
                break
            seen_next.add(next_link)
            # ``next.href`` is an absolute URL already carrying every query
            # parameter (including ``aid`` and the cursor). Pass it through
            # ``_get`` with no extra params.
            page = self._get(next_link, params=None)
            page_results = page.get("results") or []
            if not page_results:
                break
            combined_results.extend(page_results)
            next_link = (((page.get("_links") or {}).get("next") or {}).get("href"))
            pages += 1
        if pages > 1:
            logger.debug("Fetched %d pages (%d rows) from %s",
                         pages, len(combined_results), path)
        first["results"] = combined_results
        # Drop the now-stale pagination links so callers don't try to walk
        # them again later.
        if "_links" in first and isinstance(first["_links"], dict):
            first["_links"].pop("next", None)
        return first

    # -- public endpoints ------------------------------------------------
    def validate_token(self) -> bool:
        """Returns True if token can list account groups."""
        try:
            self._get("/account-groups")
            return True
        except (PermissionError, requests.RequestException):
            return False

    def get_account_groups(self) -> List[Dict[str, Any]]:
        data = self._get("/account-groups")
        return data.get("accountGroups", []) or data.get("account_groups", []) or []

    def get_current_user(self) -> Dict[str, Any]:
        try:
            return self._get("/users/current")
        except Exception:  # noqa: BLE001 — non-critical
            return {}

    def list_page_load_tests(self, aid: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"aid": aid} if aid else None
        data = self._get("/tests/page-load", params=params)
        return data.get("tests", []) or []

    def list_web_transaction_tests(self, aid: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"aid": aid} if aid else None
        data = self._get("/tests/web-transactions", params=params)
        return data.get("tests", []) or []

    def page_load_results(self, test_id: str, aid: Optional[str] = None,
                          window: str = "1d") -> Dict[str, Any]:
        params: Dict[str, Any] = {"window": window}
        if aid:
            params["aid"] = aid
        return self._get_paginated_results(
            f"/test-results/{test_id}/page-load", params=params,
        )

    def web_transaction_results(self, test_id: str, aid: Optional[str] = None,
                                window: str = "1d") -> Dict[str, Any]:
        params: Dict[str, Any] = {"window": window}
        if aid:
            params["aid"] = aid
        return self._get_paginated_results(
            f"/test-results/{test_id}/web-transactions", params=params,
        )
