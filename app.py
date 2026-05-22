"""Flask app for the Page-Load & Transaction tests errors monitor.

Run with::

    python app.py
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
import datetime as dt
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

from analyzer import (aggregate, cache_filename, merge_results_file,
                      newest_cache_timestamp, read_ignored, sanitize_org_id,
                      write_ignored)
from te_client import ThousandEyesClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("monitor")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT_DIR, "test_results")
os.makedirs(CACHE_DIR, exist_ok=True)

ALLOWED_INTERVALS = (5, 10, 15, 20, 30, 60)

# ---------------------------------------------------------------------------
# Time-range presets for the timeline widgets
# ---------------------------------------------------------------------------
# Each entry: hours -> (label, blocks, block_minutes, api_window)
# - blocks * block_minutes == hours * 60.
# - Ranges <= 1 day use 15-minute blocks and the standard 1d API window.
# - 2d / 7d use hourly blocks and a wider API window so the first cycle
#   backfills the cache far enough.
RANGE_PRESETS: Dict[int, Dict[str, Any]] = {
    1:   {"label": "1 hour",  "blocks": 4,   "block_minutes": 15, "api_window": "1d"},
    2:   {"label": "2 hours", "blocks": 8,   "block_minutes": 15, "api_window": "1d"},
    4:   {"label": "4 hours", "blocks": 16,  "block_minutes": 15, "api_window": "1d"},
    8:   {"label": "8 hours", "blocks": 32,  "block_minutes": 15, "api_window": "1d"},
    12:  {"label": "12 hours","blocks": 48,  "block_minutes": 15, "api_window": "1d"},
    24:  {"label": "1 day",   "blocks": 96,  "block_minutes": 15, "api_window": "1d"},
    48:  {"label": "2 days",  "blocks": 48,  "block_minutes": 60, "api_window": "2d"},
    168: {"label": "7 days",  "blocks": 168, "block_minutes": 60, "api_window": "7d"},
}
ALLOWED_RANGES = tuple(RANGE_PRESETS.keys())
DEFAULT_RANGE_HOURS = 8


def _range_preset(hours: int) -> Dict[str, Any]:
    return RANGE_PRESETS.get(int(hours), RANGE_PRESETS[DEFAULT_RANGE_HOURS])


# ThousandEyes API supports these ``window`` values for /test-results endpoints.
# Listed in increasing order so we can pick the smallest one that covers a
# given gap on disk.
TE_WINDOW_LADDER: List[Tuple[int, str]] = [
    (1,    "1h"),
    (2,    "2h"),
    (4,    "4h"),
    (12,   "12h"),
    (24,   "1d"),
    (48,   "2d"),
    (168,  "7d"),
    (336,  "14d"),
    (720,  "30d"),
    (1440, "60d"),
    (2160, "90d"),
]
# Hard cap so we never request more than 90 days (TE max for many endpoints).
_MAX_WINDOW_HOURS = TE_WINDOW_LADDER[-1][0]


def _pick_api_window(cache_path: str, min_hours: int) -> str:
    """Pick the smallest TE ``window`` that covers both the requested display
    range and any gap on disk.

    ``min_hours`` is the display-range floor (e.g. 24h for ranges <=1d, 48h
    for 2d, 168h for 7d). We then widen the window if:

    - The newest cached round is older than ``min_hours`` (leading-edge gap),
      so the cycle backfills what was missed while the app was offline.
    - There is an *internal* gap (a contiguous stretch of >2 h with no rounds)
      anywhere in the most recent 30 days of the cache. We then enlarge the
      window so the next API call reaches past the older side of that gap and
      ``merge_results_file`` can fill it in (its dedup logic makes the
      overlapping rounds a no-op).

    Capped at the longest TE-supported window so we never request beyond
    what the API will serve.
    """
    required = max(1, int(min_hours))
    newest_local = newest_cache_timestamp(cache_path)
    if newest_local is None:
        # Empty cache: just honor the display-range floor.
        for hours, label in TE_WINDOW_LADDER:
            if hours >= min(required, _MAX_WINDOW_HOURS):
                return label
        return TE_WINDOW_LADDER[-1][1]

    now_local = dt.datetime.now(tz=newest_local.tzinfo)

    # 1) Leading-edge gap: how far back the most recent round sits.
    lead_gap_hours = (now_local - newest_local).total_seconds() / 3600.0
    if lead_gap_hours > 0:
        required = max(required, int(lead_gap_hours) + 2)

    # 2) Internal gaps in the last 30 days of the cache.
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    horizon = now_local - dt.timedelta(days=30)
    from analyzer import _parse_start  # local import to avoid private leak above
    stamps = []
    for r in data.get("results") or []:
        parsed = _parse_start(r.get("startTime"))
        if parsed is not None and parsed >= horizon:
            stamps.append(parsed)
    stamps.sort()  # oldest first
    for prev, curr in zip(stamps, stamps[1:]):
        gap_h = (curr - prev).total_seconds() / 3600.0
        if gap_h > 2:  # likely a real outage, not normal cadence
            # We need a window that reaches at least to ``prev`` so the next
            # call returns rounds inside the gap.
            need = (now_local - prev).total_seconds() / 3600.0 + 2
            required = max(required, int(need))

    required = min(required, _MAX_WINDOW_HOURS)
    for hours, label in TE_WINDOW_LADDER:
        if hours >= required:
            return label
    return TE_WINDOW_LADDER[-1][1]


# ---------------------------------------------------------------------------
# Server-side session store
# ---------------------------------------------------------------------------
# Flask's default session is a signed cookie capped at ~4 KB. With multiple
# tokens, organizations and account groups, that quickly overflows and the
# browser silently drops the cookie — so the user appears logged out on every
# POST. Keep all bulky structures here, server-side, and only round-trip a
# short ``sid`` in the cookie.
_SESSION_STORE: Dict[str, Dict[str, Any]] = {}
_SESSION_LOCK = threading.Lock()


def _store() -> Dict[str, Any]:
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_hex(16)
        session["sid"] = sid
    with _SESSION_LOCK:
        return _SESSION_STORE.setdefault(sid, {})


def _clear_store() -> None:
    sid = session.get("sid")
    if not sid:
        return
    with _SESSION_LOCK:
        _SESSION_STORE.pop(sid, None)


def _format_elapsed(seconds: float) -> str:
    """Human-friendly elapsed time: '<1m' -> '12.3s', '>=1m' -> '2m 5s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rem = int(round(seconds - minutes * 60))
    return f"{minutes}m {rem}s"


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------
class MonitorScheduler:
    """Singleton-ish scheduler that runs a data collection cycle on an
    interval. The cycle re-uses the bearer token captured at login.
    """

    def __init__(self) -> None:
        # orgs: List[{orgKey, orgId, organizationName, token, account_groups: [...]}]
        self.orgs: List[Dict[str, Any]] = []
        self.interval_minutes: int = 5
        self.range_hours: int = DEFAULT_RANGE_HOURS
        self.last_cycle_started: Optional[datetime] = None
        self.last_cycle_finished: Optional[datetime] = None
        self.last_cycle_seconds: Optional[float] = None
        self.last_error: Optional[str] = None
        # Per-org aggregate cache, keyed by orgKey.
        self.aggregates: Dict[str, Dict[str, Any]] = {}
        self.first_cycle_done = False

        self._stop_event = threading.Event()
        self._cycle_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------
    def configure(self, orgs: List[Dict[str, Any]],
                  interval_minutes: int = 5,
                  range_hours: int = DEFAULT_RANGE_HOURS) -> None:
        with self._lock:
            self.orgs = orgs
            self.interval_minutes = interval_minutes
            self.range_hours = range_hours
            self.first_cycle_done = False
            self.aggregates = {}

        # Preload aggregates from any cache that already exists on disk so the
        # dashboard renders immediately with the most recent saved data while
        # the first live cycle runs in the background.
        preset = _range_preset(range_hours)
        preloaded = False
        for org in orgs:
            org_id = org.get("orgId", "")
            prefix = sanitize_org_id(org_id) + "_"
            try:
                has_files = any(
                    fn.startswith(prefix) and fn.endswith(".json")
                    for fn in os.listdir(CACHE_DIR)
                )
            except OSError:
                has_files = False
            if not has_files:
                continue
            ignored = read_ignored(CACHE_DIR, org_id)
            agg = aggregate(
                CACHE_DIR, org_id=org_id,
                blocks=preset["blocks"],
                block_minutes=preset["block_minutes"],
                ignored_test_ids=ignored,
            )
            with self._lock:
                self.aggregates[org["orgKey"]] = agg
            preloaded = True

        if preloaded:
            # Flip the readiness flag so /api/dashboard serves the cached
            # aggregate right away instead of "Getting data…".
            with self._lock:
                self.first_cycle_done = True
            logger.info("Preloaded aggregates from cache for %d/%d org(s).",
                        len(self.aggregates), len(orgs))

        # Start the worker. On a cold boot it will enter its loop and run
        # the first cycle immediately — no explicit trigger() needed. We
        # only trigger when the worker is already running and asleep
        # between cycles (e.g. user re-selected orgs without restarting),
        # otherwise the trigger would fire DURING the first cycle and cause
        # back-to-back loops with no interval wait.
        worker_was_alive = bool(self._thread and self._thread.is_alive())
        self.start()
        if worker_was_alive and not self.is_cycle_running():
            self.trigger()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="monitor-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._cycle_event.set()

    def trigger(self) -> None:
        self._cycle_event.set()

    def is_cycle_running(self) -> bool:
        """True while a data-gathering cycle is in progress."""
        started = self.last_cycle_started
        finished = self.last_cycle_finished
        if started is None:
            # Worker hasn't started yet — treat as "running" so the UI shows
            # the stale-data banner until the first cycle completes.
            return bool(self._thread and self._thread.is_alive())
        if finished is None:
            return True
        return started > finished

    def set_interval(self, minutes: int) -> None:
        if minutes not in ALLOWED_INTERVALS:
            raise ValueError(f"Interval must be one of {ALLOWED_INTERVALS}.")
        with self._lock:
            self.interval_minutes = minutes
        self.trigger()

    def set_range(self, hours: int) -> None:
        if hours not in ALLOWED_RANGES:
            raise ValueError(f"Range must be one of {ALLOWED_RANGES}.")
        with self._lock:
            previous = self.range_hours
            self.range_hours = hours
        # Recompute every org's aggregate against the new range immediately so
        # the UI updates without waiting for the next cycle.
        for org in self.orgs:
            self.recompute_org(org["orgKey"])
        # If we widened the range to 2d/7d we need a fresh cycle so the API
        # window backfills the cache. A narrower range never needs that.
        if (RANGE_PRESETS[hours]["api_window"]
                != RANGE_PRESETS[previous]["api_window"]):
            self.trigger()

    def recompute_org(self, org_key: str) -> bool:
        """Recompute the aggregate for one org without re-fetching from API."""
        target = next((o for o in self.orgs if o["orgKey"] == org_key), None)
        if target is None:
            return False
        org_id = target.get("orgId", "")
        ignored = read_ignored(CACHE_DIR, org_id)
        preset = _range_preset(self.range_hours)
        with self._lock:
            self.aggregates[org_key] = aggregate(
                CACHE_DIR, org_id=org_id,
                blocks=preset["blocks"],
                block_minutes=preset["block_minutes"],
                ignored_test_ids=ignored,
            )
        return True

    # -- worker ----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._cycle()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.exception("Cycle failed: %s", exc)
            self.first_cycle_done = True
            # Discard any trigger requests that were emitted DURING the
            # cycle (the cycle we just finished already produced fresh
            # data, so they would only cause an immediate redundant run).
            # Triggers that arrive DURING the wait() below will still wake
            # the worker, which is the desired behavior for user actions
            # such as changing the interval or range while idle.
            self._cycle_event.clear()

            # Auto-bump the interval if the last cycle took longer than the
            # configured interval. Pick the closest higher allowed value so
            # the next wait isn't skipped entirely.
            elapsed = self.last_cycle_seconds or 0.0
            interval_secs = self.interval_minutes * 60
            if elapsed > interval_secs:
                higher = next((m for m in ALLOWED_INTERVALS
                               if m * 60 > elapsed), None)
                if higher is None:
                    higher = ALLOWED_INTERVALS[-1]
                if higher != self.interval_minutes:
                    logger.warning(
                        "Last cycle took %s, longer than the configured "
                        "%d-minute interval — auto-bumping interval to "
                        "%d minutes.",
                        _format_elapsed(elapsed),
                        self.interval_minutes, higher)
                    with self._lock:
                        self.interval_minutes = higher
                    interval_secs = higher * 60

            # Sleep for (interval - last cycle time) so cycles start at a
            # consistent cadence regardless of how long each one takes.
            wait_secs = max(0.0, interval_secs - elapsed)
            logger.info("Next cycle in %s (interval=%dm, last cycle=%s).",
                        _format_elapsed(wait_secs),
                        self.interval_minutes, _format_elapsed(elapsed))
            if wait_secs > 0:
                self._cycle_event.wait(timeout=wait_secs)

    def _cycle(self) -> None:
        if not self.orgs:
            logger.warning("Cycle skipped: no orgs configured.")
            return
        self.last_cycle_started = datetime.now()
        org_summary = ", ".join(o.get("organizationName", o.get("orgKey", "?"))
                                for o in self.orgs)
        logger.info("Starting data collection cycle (orgs=%d [%s], interval=%dm)",
                    len(self.orgs), org_summary, self.interval_minutes)

        # One client per org — each org may use a different bearer token.
        # Log the per-org token fingerprint so it's obvious when two orgs
        # accidentally share the same token (or one is missing).
        clients: Dict[str, ThousandEyesClient] = {}
        for org in self.orgs:
            tok = org.get("token") or ""
            ag_count = len(org.get("account_groups") or [])
            if tok:
                clients[org["orgKey"]] = ThousandEyesClient(tok)
                logger.info("Org configured: key=%s id=%s name=%s "
                            "account_groups=%d token=…%s",
                            org["orgKey"], org.get("orgId", ""),
                            org.get("organizationName", ""),
                            ag_count, tok[-4:])
            else:
                logger.warning("Org has no token, will be skipped: key=%s "
                               "name=%s account_groups=%d",
                               org["orgKey"],
                               org.get("organizationName", ""), ag_count)

        # Flatten the AGs and tag each with its org so we can show progress and
        # write the right cache file.
        flat: List[Dict[str, Any]] = []
        for org in self.orgs:
            ags = org.get("account_groups") or []
            if not ags:
                logger.warning("Org has zero account groups, nothing to "
                               "gather: key=%s name=%s",
                               org["orgKey"],
                               org.get("organizationName", ""))
            for ag in ags:
                if not ag.get("aid"):
                    continue
                flat.append({
                    "aid": str(ag["aid"]),
                    "accountGroupName": ag.get("accountGroupName", str(ag["aid"])),
                    "orgKey": org["orgKey"],
                    "orgId": org.get("orgId", ""),
                    "organizationName": org.get("organizationName", ""),
                })
        total = len(flat)
        logger.info("Flattened %d account groups across %d org(s).",
                    total, len(self.orgs))

        for idx, ag in enumerate(flat, start=1):
            pct = (idx * 100.0 / total) if total else 100.0
            logger.info("Gathering data for account group %d/%d (%.1f%%) — "
                        "org=%s aid=%s name=%s",
                        idx, total, pct,
                        ag["organizationName"] or ag["orgKey"],
                        ag["aid"], ag["accountGroupName"])
            client = clients.get(ag["orgKey"])
            if client is None:
                logger.warning("No client for org=%s, skipping aid=%s",
                               ag["orgKey"], ag["aid"])
                continue
            # IMPORTANT: isolate each AG behind its own try/except. Without
            # this guard, a single failure (HTTP 429/5xx, network blip,
            # malformed response, etc.) propagates out of the inner loop and
            # aborts the entire cycle — which made it look like the script
            # was only fetching data for the first organization.
            try:
                self._process_aid(client, ag, "page-load")
            except Exception as exc:  # noqa: BLE001
                logger.warning("page-load gather failed for org=%s aid=%s: "
                               "%s — continuing with next AG.",
                               ag["organizationName"] or ag["orgKey"],
                               ag["aid"], exc)
            try:
                self._process_aid(client, ag, "web-transactions")
            except Exception as exc:  # noqa: BLE001
                logger.warning("web-transactions gather failed for org=%s "
                               "aid=%s: %s — continuing with next AG.",
                               ag["organizationName"] or ag["orgKey"],
                               ag["aid"], exc)

        # Recompute per-org aggregates (with ignored-tests filter).
        preset = _range_preset(self.range_hours)
        new_aggs: Dict[str, Dict[str, Any]] = {}
        for org in self.orgs:
            org_id = org.get("orgId", "")
            ignored = read_ignored(CACHE_DIR, org_id)
            new_aggs[org["orgKey"]] = aggregate(
                CACHE_DIR, org_id=org_id,
                blocks=preset["blocks"],
                block_minutes=preset["block_minutes"],
                ignored_test_ids=ignored,
            )
        self.aggregates = new_aggs

        self.last_cycle_finished = datetime.now()
        self.last_error = None
        elapsed = (self.last_cycle_finished - self.last_cycle_started).total_seconds()
        self.last_cycle_seconds = elapsed
        logger.info("Cycle complete in %s", _format_elapsed(elapsed))

    @staticmethod
    def _is_eligible(test: Dict[str, Any]) -> bool:
        return (test.get("liveShare") is False
                and test.get("enabled") is True
                and test.get("savedEvent") is False)

    def _process_aid(self, client: ThousandEyesClient, ag: Dict[str, Any],
                     kind: str) -> None:
        aid = ag["aid"]
        ag_name = ag["accountGroupName"]
        org_id = ag.get("orgId", "")
        try:
            if kind == "page-load":
                tests = client.list_page_load_tests(aid=aid)
            else:
                tests = client.list_web_transaction_tests(aid=aid)
        except PermissionError as exc:
            logger.warning("Skipping aid=%s (%s): %s", aid, kind, exc)
            return
        except Exception as exc:  # noqa: BLE001
            # Network blip / HTTP 5xx / timeout — log and move on so the
            # rest of the cycle still runs for the other AGs and orgs.
            logger.warning("Failed listing %s tests for aid=%s: %s — "
                           "skipping this AG for this cycle.",
                           kind, aid, exc)
            return

        eligible = [t for t in tests if self._is_eligible(t)]
        logger.info("aid=%s kind=%s eligible=%d/%d",
                    aid, kind, len(eligible), len(tests))

        for test in eligible:
            test_id = str(test.get("testId") or test.get("id") or "")
            if not test_id:
                continue
            cache_path = os.path.join(CACHE_DIR, cache_filename(org_id, test_id))
            # Floor the window to the configured display range, but bump it
            # higher if the cache has stale data (e.g. the app was offline
            # for several days) so the gap is backfilled in one call.
            preset = _range_preset(self.range_hours)
            min_hours_for_range = {
                "1h": 1, "2h": 2, "4h": 4, "12h": 12,
                "1d": 24, "2d": 48, "7d": 168,
            }.get(preset["api_window"], 24)
            api_window = _pick_api_window(cache_path, min_hours_for_range)
            try:
                if kind == "page-load":
                    payload = client.page_load_results(test_id, aid=aid, window=api_window)
                else:
                    payload = client.web_transaction_results(test_id, aid=aid, window=api_window)
            except PermissionError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("Result fetch failed for %s: %s", test_id, exc)
                continue

            # ``interval`` (in seconds — the configured cadence between
            # rounds) is the same value on both the test-list response and
            # the ``test`` block returned with the results. We prefer the
            # results-side copy because that's the one the user asked for,
            # and fall back to the test-list value if absent.
            payload_test = (payload or {}).get("test") or {}
            interval = payload_test.get("interval") or test.get("interval")
            test_meta = {
                "testId": test_id,
                "testName": test.get("testName") or f"Test {test_id}",
                "aid": aid,
                "accountGroupName": ag_name,
                "orgId": org_id,
                "organizationName": ag.get("organizationName", ""),
                "interval": interval,
            }
            merge_results_file(cache_path, payload, test_meta, kind)


SCHEDULER = MonitorScheduler()


# ---------------------------------------------------------------------------
# Token / org pool helpers
# ---------------------------------------------------------------------------
def _token_id(token: str) -> str:
    """Stable short id for a bearer token (used as a session key)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _normalize_groups(raw_groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "aid": str(ag.get("aid") or ag.get("id") or ""),
            "accountGroupName": ag.get("accountGroupName") or ag.get("name") or "—",
            "isDefault": bool(ag.get("isDefaultAccountGroup") or ag.get("isDefault")),
            "orgId": str(ag.get("orgId") or ""),
            "organizationName": ag.get("organizationName") or "",
        }
        for ag in raw_groups
        if ag.get("aid") or ag.get("id")
    ]


def _groups_to_orgs(normalized: List[Dict[str, Any]],
                    token_id: str, source_label: str) -> List[Dict[str, Any]]:
    """Group normalized account groups into org entries tagged with the token id."""
    organizations: Dict[str, Dict[str, Any]] = {}
    for ag in normalized:
        key = ag["orgId"] or ag["organizationName"] or "__default__"
        org = organizations.setdefault(key, {
            "orgKey": key,
            "orgId": ag["orgId"],
            "organizationName": ag["organizationName"] or "Default Organization",
            "tokenId": token_id,
            "sourceLabel": source_label,
            "account_groups": [],
        })
        org["account_groups"].append(ag)
    return sorted(
        organizations.values(),
        key=lambda o: (o["organizationName"] or "").lower(),
    )


def _get_pool() -> List[Dict[str, Any]]:
    return list(_store().get("org_pool") or [])


def _save_pool(pool: List[Dict[str, Any]]) -> None:
    _store()["org_pool"] = pool


def _ensure_primary_pool() -> Optional[str]:
    """Make sure the primary token has been loaded into the org pool.

    Returns an error string or ``None`` on success.
    """
    s = _store()
    tokens = s.get("tokens") or {}
    if not tokens:
        return "No token in session."
    pool = _get_pool()
    loaded_token_ids = {o["tokenId"] for o in pool}
    primary_tid = s.get("primary_token_id")
    if primary_tid and primary_tid not in loaded_token_ids:
        tok_entry = tokens.get(primary_tid)
        if not tok_entry:
            return "Primary token entry missing."
        client = ThousandEyesClient(tok_entry["token"])
        try:
            raw = client.get_account_groups()
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        new_orgs = _groups_to_orgs(_normalize_groups(raw),
                                   primary_tid, tok_entry.get("label", ""))
        # Dedupe by orgKey — primary orgs win first.
        existing_keys = {o["orgKey"] for o in pool}
        for o in new_orgs:
            if o["orgKey"] not in existing_keys:
                pool.append(o)
                existing_keys.add(o["orgKey"])
        _save_pool(pool)
    return None


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
def _load_or_create_secret() -> str:
    """Return a stable Flask session secret.

    Priority:
      1. ``FLASK_SECRET`` env var (lets you override in deployments).
      2. ``.flask_secret`` file persisted next to ``app.py``.
      3. Generate a new one and persist it.

    Persisting matters because Flask's signed-cookie sessions are signed
    with this key. If the key rotated on every restart, every existing
    dashboard tab would fail the next ``/api/dashboard`` poll with HTTP
    401 and the JS would bounce the user back to ``/login`` — which
    looks exactly like "the script logged me out after a cycle" when the
    process happens to restart in the background.
    """
    env_secret = os.environ.get("FLASK_SECRET")
    if env_secret:
        return env_secret
    secret_path = os.path.join(ROOT_DIR, ".flask_secret")
    try:
        with open(secret_path, "r", encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    new_secret = secrets.token_hex(32)
    try:
        with open(secret_path, "w", encoding="utf-8") as fh:
            fh.write(new_secret)
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
    except OSError as exc:  # pragma: no cover — fall back to in-memory
        logger.warning("Could not persist Flask secret to %s: %s — "
                       "sessions will not survive a restart.",
                       secret_path, exc)
    return new_secret


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = _load_or_create_secret()
    # Session cookie hardening — make sessions long-lived (30 days) so a
    # browser tab kept open across restarts/network blips stays logged
    # in instead of getting bounced to /login between collection cycles.
    app.permanent_session_lifetime = dt.timedelta(days=30)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_REFRESH_EACH_REQUEST=True,
    )

    @app.before_request
    def _make_session_permanent():
        # Mark every request's session as permanent so Flask emits a
        # ``Max-Age``/``Expires`` cookie rather than a browser-session
        # cookie. Without this, some browsers (and some restart paths)
        # silently drop the cookie, which manifests as the user being
        # "logged out" right after a data-collection loop.
        session.permanent = True

    # Cache-bust static assets (dashboard.js / styles.css) using their file
    # mtime so the browser always picks up the latest version after a code
    # change + server restart instead of running a stale cached copy.
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static")

    @app.context_processor
    def _inject_static_v():
        def static_v(filename: str) -> str:
            try:
                mtime = int(os.path.getmtime(os.path.join(static_dir, filename)))
            except OSError:
                mtime = 0
            return f"{url_for('static', filename=filename)}?v={mtime}"
        return {"static_v": static_v}

    # -- auth helpers ----------------------------------------------------
    def _logged_in() -> bool:
        return bool(_store().get("tokens"))

    def _token_label(token: str, user: Dict[str, Any]) -> str:
        name = (user or {}).get("name") or (user or {}).get("email")
        if name:
            return str(name)
        # Last 4 chars of the token, for visual reference only.
        return f"token …{token[-4:]}"

    # -- routes ----------------------------------------------------------
    @app.route("/", methods=["GET"])
    def index():
        if not _logged_in():
            return redirect(url_for("login"))
        if not _store().get("orgs_set"):
            return redirect(url_for("select_org"))
        return redirect(url_for("dashboard"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            token = (request.form.get("token") or "").strip()
            if not token:
                error = "Token is required."
            else:
                client = ThousandEyesClient(token)
                if not client.validate_token():
                    error = "Invalid token or insufficient permissions."
                else:
                    _clear_store()
                    session.clear()
                    user = client.get_current_user()
                    user_name = (user.get("name") or user.get("email")
                                 or "ThousandEyes user")
                    session["user_name"] = user_name
                    tid = _token_id(token)
                    s = _store()
                    s["tokens"] = {
                        tid: {"token": token, "label": _token_label(token, user)},
                    }
                    s["primary_token_id"] = tid
                    s["org_pool"] = []
                    return redirect(url_for("select_org"))
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        _clear_store()
        session.clear()
        SCHEDULER.stop()
        return redirect(url_for("login"))

    @app.route("/select-org", methods=["GET", "POST"])
    def select_org():
        if not _logged_in():
            return redirect(url_for("login"))

        load_err = _ensure_primary_pool()
        if load_err:
            return render_template("select_org.html", error=load_err,
                                   organizations=[],
                                   intervals=ALLOWED_INTERVALS,
                                   user_name=session.get("user_name", ""))

        organizations_list = _get_pool()

        if request.method == "POST":
            chosen_keys = request.form.getlist("orgKey")
            interval = int(request.form.get("interval") or 5)
            if interval not in ALLOWED_INTERVALS:
                interval = 5
            s = _store()
            tokens = s.get("tokens") or {}
            selected: List[Dict[str, Any]] = []
            for org in organizations_list:
                if org["orgKey"] in chosen_keys:
                    tok_entry = tokens.get(org.get("tokenId", ""))
                    selected.append({
                        **org,
                        "token": (tok_entry or {}).get("token", ""),
                    })
            if not selected:
                return render_template(
                    "select_org.html",
                    error="Please select at least one organization to monitor.",
                    organizations=organizations_list,
                    intervals=ALLOWED_INTERVALS,
                    user_name=session.get("user_name", ""))
            # Don't store tokens twice — the dashboard only needs orgKey/name.
            s["selected_orgs"] = [
                {k: v for k, v in o.items() if k != "token"} for o in selected
            ]
            s["active_orgKey"] = selected[0]["orgKey"]
            s["orgs_set"] = True
            session["interval"] = interval
            session["range_hours"] = DEFAULT_RANGE_HOURS
            SCHEDULER.configure(orgs=selected, interval_minutes=interval,
                                range_hours=DEFAULT_RANGE_HOURS)
            return redirect(url_for("dashboard"))

        return render_template("select_org.html",
                               organizations=organizations_list,
                               intervals=ALLOWED_INTERVALS,
                               user_name=session.get("user_name", ""))

    @app.route("/select-org/add-token", methods=["POST"])
    def select_org_add_token():
        """Validate an additional bearer token and merge its orgs into the pool."""
        if not _logged_in():
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        if not token:
            return jsonify({"error": "Token is required."}), 400
        client = ThousandEyesClient(token)
        if not client.validate_token():
            return jsonify({"error": "Invalid token or insufficient permissions."}), 400
        try:
            raw = client.get_account_groups()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 502
        user = client.get_current_user()
        tid = _token_id(token)
        s = _store()
        tokens = dict(s.get("tokens") or {})
        tokens[tid] = {"token": token, "label": _token_label(token, user)}
        s["tokens"] = tokens
        new_orgs = _groups_to_orgs(_normalize_groups(raw), tid, tokens[tid]["label"])
        pool = _get_pool()
        existing_keys = {o["orgKey"] for o in pool}
        added: List[Dict[str, Any]] = []
        for o in new_orgs:
            if o["orgKey"] in existing_keys:
                continue
            pool.append(o)
            existing_keys.add(o["orgKey"])
            added.append(o)
        _save_pool(pool)
        return jsonify({
            "ok": True,
            "tokenId": tid,
            "label": tokens[tid]["label"],
            "added": added,
            "skipped": len(new_orgs) - len(added),
        })

    @app.route("/dashboard")
    def dashboard():
        s = _store()
        if not _logged_in() or not s.get("orgs_set"):
            return redirect(url_for("login"))
        selected = s.get("selected_orgs", [])
        active_key = s.get("active_orgKey") or (selected[0]["orgKey"] if selected else "")
        active_org = next((o for o in selected if o["orgKey"] == active_key),
                           selected[0] if selected else None)
        ranges = [{"hours": h, "label": p["label"]}
                  for h, p in RANGE_PRESETS.items()]
        return render_template(
            "dashboard.html",
            user_name=session.get("user_name", ""),
            interval=session.get("interval", 5),
            intervals=ALLOWED_INTERVALS,
            range_hours=session.get("range_hours", DEFAULT_RANGE_HOURS),
            ranges=ranges,
            active_orgKey=active_key,
            active_org_name=(active_org or {}).get("organizationName", ""),
            account_groups=(active_org or {}).get("account_groups", []),
            selected_orgs=selected,
        )

    # -- API -------------------------------------------------------------
    @app.route("/api/dashboard")
    def api_dashboard():
        s = _store()
        if not _logged_in() or not s.get("orgs_set"):
            return jsonify({"error": "unauthenticated"}), 401
        active_key = (request.args.get("org")
                      or s.get("active_orgKey") or "")
        # Don't gate on first_cycle_done — if the scheduler already has a
        # cached aggregate for this org (from a previous cycle or from the
        # preload-from-disk step in configure()), return it immediately so
        # the dashboard never sits on "Getting data" while real data exists.
        data = SCHEDULER.aggregates.get(active_key)
        ready = bool(data) or SCHEDULER.first_cycle_done

        # Safety net: if the scheduler hasn't published an aggregate for this
        # org yet (first cycle still running, page reloaded before preload,
        # etc.) but cached JSON files exist on disk for THIS org, compute
        # the aggregate right now from disk and return it. We deliberately
        # do NOT fall back to another selected org's cache — doing so would
        # silently render metrics for an organization the user did not pick.
        if not data:
            selected = s.get("selected_orgs") or []
            target_org = next((o for o in selected
                               if o.get("orgKey") == active_key), None)
            try:
                cached_files = os.listdir(CACHE_DIR)
            except OSError:
                cached_files = []
            if target_org is not None:
                cand_org_id = target_org.get("orgId", "")
                prefix = sanitize_org_id(cand_org_id) + "_"
                has_cache = any(fn.startswith(prefix) and fn.endswith(".json")
                                for fn in cached_files)
                if has_cache:
                    preset = _range_preset(SCHEDULER.range_hours)
                    ignored = read_ignored(CACHE_DIR, cand_org_id)
                    data = aggregate(
                        CACHE_DIR, org_id=cand_org_id,
                        blocks=preset["blocks"],
                        block_minutes=preset["block_minutes"],
                        ignored_test_ids=ignored,
                    )
                    # Cache it on the scheduler so subsequent polls are cheap.
                    SCHEDULER.aggregates[target_org["orgKey"]] = data
                    ready = True
                else:
                    logger.info("No cached files for active org %s "
                                "(prefix=%s); dashboard will show "
                                "'Getting data' until first cycle finishes.",
                                active_key, prefix)
            else:
                logger.warning("Active org key %s not found in selected "
                               "orgs; cannot serve dashboard data.",
                               active_key)

        return jsonify({
            "ready": ready,
            "status": "ready" if ready else "Getting data",
            "interval": SCHEDULER.interval_minutes,
            "range_hours": SCHEDULER.range_hours,
            "active_orgKey": active_key,
            "last_cycle_started": (SCHEDULER.last_cycle_started.isoformat()
                                    if SCHEDULER.last_cycle_started else None),
            "last_cycle_finished": (SCHEDULER.last_cycle_finished.isoformat()
                                     if SCHEDULER.last_cycle_finished else None),
            "last_cycle_seconds": SCHEDULER.last_cycle_seconds,
            "last_cycle_duration": (_format_elapsed(SCHEDULER.last_cycle_seconds)
                                     if SCHEDULER.last_cycle_seconds is not None else None),
            "cycle_in_progress": SCHEDULER.is_cycle_running(),
            "data_age_seconds": (data or {}).get("data_age_seconds"),
            "last_error": SCHEDULER.last_error,
            "data": data or {},
        })

    @app.route("/api/active-org", methods=["POST"])
    def api_active_org():
        s = _store()
        if not _logged_in() or not s.get("orgs_set"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        key = str(body.get("orgKey") or "")
        selected = s.get("selected_orgs", [])
        if not any(o["orgKey"] == key for o in selected):
            return jsonify({"error": "unknown org"}), 400
        s["active_orgKey"] = key
        return jsonify({"ok": True, "active_orgKey": key})

    @app.route("/api/interval", methods=["POST"])
    def api_interval():
        if not _logged_in():
            return jsonify({"error": "unauthenticated"}), 401
        try:
            minutes = int((request.get_json(silent=True) or {}).get("minutes", 0))
            SCHEDULER.set_interval(minutes)
            session["interval"] = minutes
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "interval": minutes})

    @app.route("/api/range", methods=["POST"])
    def api_range():
        if not _logged_in():
            return jsonify({"error": "unauthenticated"}), 401
        try:
            hours = int((request.get_json(silent=True) or {}).get("hours", 0))
            SCHEDULER.set_range(hours)
            session["range_hours"] = hours
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "range_hours": hours})

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        if not _logged_in():
            return jsonify({"error": "unauthenticated"}), 401
        SCHEDULER.trigger()
        return jsonify({"ok": True})

    # -- Ignored tests --------------------------------------------------
    def _resolve_org(org_key: str) -> Optional[Dict[str, Any]]:
        for o in _store().get("selected_orgs") or []:
            if o.get("orgKey") == org_key:
                return o
        return None

    def _ignored_mutation(action: str):
        s = _store()
        if not _logged_in() or not s.get("orgs_set"):
            return jsonify({"error": "unauthenticated"}), 401
        body = request.get_json(silent=True) or {}
        org_key = str(body.get("orgKey") or s.get("active_orgKey") or "")
        org = _resolve_org(org_key)
        if not org:
            return jsonify({"error": "unknown org"}), 400
        ids_in = body.get("testIds") or []
        if not isinstance(ids_in, list):
            return jsonify({"error": "testIds must be a list"}), 400
        ids = {str(i).strip() for i in ids_in if str(i).strip()}
        org_id = org.get("orgId", "")
        current = read_ignored(CACHE_DIR, org_id)
        if action == "add":
            current |= ids
        else:
            current -= ids
        write_ignored(CACHE_DIR, org_id, current)
        SCHEDULER.recompute_org(org_key)
        return jsonify({"ok": True, "ignored": sorted(current)})

    @app.route("/api/ignored/add", methods=["POST"])
    def api_ignored_add():
        return _ignored_mutation("add")

    @app.route("/api/ignored/remove", methods=["POST"])
    def api_ignored_remove():
        return _ignored_mutation("remove")

    return app


if __name__ == "__main__":
    app = create_app()
    # Local-only by default — this is a dashboard for one TAM.
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
