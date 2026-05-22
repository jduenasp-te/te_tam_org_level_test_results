"""Aggregation logic.

Reads cached per-test result files in ``test_results/`` and produces:

- 4 organization-level series, one per dashboard widget, keyed by 15-min blocks
  (``YYYYMMDD_HHmm`` in the *local* tz).
- Per-test availability/timing series for the *outstanding* and *top-5* tables.

A 15-minute block is the local time floored to :05/:10/:15... Wait — strictly
00, 15, 30, 45.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dateutil import parser as dt_parser

logger = logging.getLogger(__name__)

LOCAL_TZ = dt.datetime.now().astimezone().tzinfo


def sanitize_org_id(org_id: Optional[str]) -> str:
    """Return a filename-safe slug for an organization id."""
    if not org_id:
        return "noorg"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(org_id))


def cache_filename(org_id: Optional[str], test_id: str) -> str:
    """Return the on-disk filename: ``{orgId}_{testId}.json``."""
    return f"{sanitize_org_id(org_id)}_{test_id}.json"


def ignored_filename(org_id: Optional[str]) -> str:
    """Return the on-disk filename for the ignored-tests list."""
    return f"ignored_tests_{sanitize_org_id(org_id)}.txt"


def read_ignored(cache_dir: str, org_id: Optional[str]) -> set:
    """Read the ignored-tests file for an organization, returning a set of ids."""
    path = os.path.join(cache_dir, ignored_filename(org_id))
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except OSError:
        return set()


def write_ignored(cache_dir: str, org_id: Optional[str], ids: Iterable[str]) -> None:
    """Persist the ignored-tests file for an organization (one id per line)."""
    path = os.path.join(cache_dir, ignored_filename(org_id))
    os.makedirs(cache_dir, exist_ok=True)
    cleaned = sorted({str(i).strip() for i in ids if str(i).strip()})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(cleaned))
        if cleaned:
            fh.write("\n")


# ---------------------------------------------------------------------------
# File cache helpers
# ---------------------------------------------------------------------------
def _result_key(result: Dict[str, Any]) -> Tuple[str, str]:
    """Unique-ness key for a test result row."""
    agent = result.get("agent") or {}
    agent_id = str(agent.get("agentId") or result.get("agentId") or "")
    round_id = str(result.get("roundId") or result.get("roundID") or
                   result.get("startTime") or "")
    return agent_id, round_id


def merge_results_file(path: str, payload: Dict[str, Any], test_meta: Dict[str, Any],
                       test_type: str) -> None:
    """Persist results, prepending only rows we don't already have on disk."""
    new_results: List[Dict[str, Any]] = list(payload.get("results", []) or [])

    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wrapper = {
            "type": test_type,
            "test": test_meta,
            "results": new_results,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(wrapper, fh, indent=2)
        return

    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, json.JSONDecodeError):
        existing = {"type": test_type, "test": test_meta, "results": []}

    existing.setdefault("type", test_type)
    existing["test"] = test_meta or existing.get("test", {})
    existing.setdefault("results", [])

    seen = {_result_key(r) for r in existing["results"]}
    fresh = [r for r in new_results if _result_key(r) not in seen]
    if fresh:
        # Pre-pend the new ones so the most recent rounds come first.
        existing["results"] = fresh + existing["results"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2)


# ---------------------------------------------------------------------------
# Time-block helpers (parameterized on block size in minutes)
# ---------------------------------------------------------------------------
def _parse_start(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).astimezone(LOCAL_TZ)
        parsed = dt_parser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(LOCAL_TZ)
    except (ValueError, TypeError):
        return None


def newest_cache_timestamp(path: str) -> Optional[dt.datetime]:
    """Return the most recent ``startTime`` found in a cache file (local tz).

    Used by the scheduler to size the API ``window`` so a multi-day gap (e.g.
    the app was offline) is fully backfilled on the next cycle.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    newest: Optional[dt.datetime] = None
    for r in data.get("results") or []:
        parsed = _parse_start(r.get("startTime"))
        if parsed is not None and (newest is None or parsed > newest):
            newest = parsed
    return newest


def _floor_to_block(local_dt: dt.datetime, block_minutes: int) -> dt.datetime:
    """Floor a local datetime down to the start of the containing block."""
    bm = max(1, int(block_minutes))
    if bm >= 60:
        # Hour-aligned floors (e.g. 60, 120). Most useful values: 60.
        hours = max(1, bm // 60)
        floored_hour = (local_dt.hour // hours) * hours
        return local_dt.replace(hour=floored_hour, minute=0, second=0, microsecond=0)
    minute = (local_dt.minute // bm) * bm
    return local_dt.replace(minute=minute, second=0, microsecond=0)


def _block_label(local_dt: dt.datetime, block_minutes: int = 15) -> str:
    return _floor_to_block(local_dt, block_minutes).strftime("%Y%m%d_%H%M")


def _label_to_dt(label: str) -> dt.datetime:
    return dt.datetime.strptime(label, "%Y%m%d_%H%M").replace(tzinfo=LOCAL_TZ)


def _last_n_block_labels(n: int = 24, block_minutes: int = 15,
                         anchor: Optional[dt.datetime] = None) -> List[str]:
    """Generate ``n`` most-recent block labels (oldest first)."""
    now = anchor or dt.datetime.now(tz=LOCAL_TZ)
    cur = _floor_to_block(now, block_minutes)
    delta = dt.timedelta(minutes=block_minutes)
    return [(cur - delta * (n - 1 - i)).strftime("%Y%m%d_%H%M")
            for i in range(n)]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _iter_cache_files(cache_dir: str,
                      org_id_filter: Optional[str] = None) -> Iterable[str]:
    """Iterate cache files, optionally restricted to one organization."""
    if not os.path.isdir(cache_dir):
        return []
    prefix = None
    if org_id_filter is not None:
        prefix = f"{sanitize_org_id(org_id_filter)}_"
    out: List[str] = []
    for n in os.listdir(cache_dir):
        if not n.endswith(".json"):
            continue
        if prefix and not n.startswith(prefix):
            continue
        out.append(os.path.join(cache_dir, n))
    return out


def _load_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h, m = divmod(seconds // 60, 60)
        return f"{h}h {m}m"
    d, rem = divmod(seconds, 86400)
    return f"{d}d {rem // 3600}h"


def aggregate(cache_dir: str, blocks: int = 32,
              block_minutes: int = 15,
              org_id: Optional[str] = None,
              ignored_test_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Build the dashboard-ready aggregate from cached files.

    ``blocks`` x ``block_minutes`` defines the visible time window. Default is
    32 x 15 min = 8 hours. Pass ``block_minutes=60`` (and a matching ``blocks``)
    when rendering wider ranges (2d / 7d).

    Pass ``org_id`` to only include files belonging to that organization
    (filename prefix ``{sanitized_org_id}_``).

    ``ignored_test_ids`` — tests in this set are excluded from every metric
    (widgets, executive summary, tables) and instead surfaced separately in
    the returned ``ignored`` list.
    """
    block_labels = _last_n_block_labels(blocks, block_minutes=block_minutes)
    label_set = set(block_labels)
    ignored_set = {str(t) for t in (ignored_test_ids or [])}
    ignored_list: List[Dict[str, Any]] = []

    # Org-level metrics ---------------------------------------------------
    tx_err_tests: Dict[str, set] = defaultdict(set)
    tx_time_sum: Dict[str, float] = defaultdict(float)
    tx_time_n: Dict[str, int] = defaultdict(int)

    pl_missing_tests: Dict[str, set] = defaultdict(set)
    pl_time_sum: Dict[str, float] = defaultdict(float)
    pl_time_n: Dict[str, int] = defaultdict(int)

    # Per-test series (dict[testId] = {"meta": {...}, "blocks": {label: stats}})
    tx_series: Dict[str, Dict[str, Any]] = {}
    pl_series: Dict[str, Dict[str, Any]] = {}

    # Track the freshest startTime we see across every file in this org so
    # the caller can decide whether the cached data is stale (>15 min old).
    newest_data_dt: Optional[dt.datetime] = None

    for path in _iter_cache_files(cache_dir, org_id_filter=org_id):
        data = _load_file(path)
        if not data:
            continue
        ttype = data.get("type")
        meta = data.get("test") or {}
        test_id = str(meta.get("testId") or os.path.splitext(os.path.basename(path))[0])
        meta = {
            "testId": test_id,
            "testName": meta.get("testName") or f"Test {test_id}",
            "accountGroupName": meta.get("accountGroupName") or meta.get("aid", ""),
            "type": ttype,
            "interval": meta.get("interval"),
        }

        if test_id in ignored_set:
            ignored_list.append({
                "testId": test_id,
                "testName": meta["testName"],
                "accountGroupName": meta["accountGroupName"],
                "type": ttype or "",
            })
            continue
        per_block_err_count: Dict[str, int] = defaultdict(int)
        per_block_total: Dict[str, int] = defaultdict(int)
        per_block_time_sum: Dict[str, float] = defaultdict(float)
        per_block_time_n: Dict[str, int] = defaultdict(int)
        latest_error_dt: Optional[dt.datetime] = None
        # Newest startTime *for this specific test* — used to decide whether
        # the test has a data-collection problem (no rounds in > 1 hour).
        test_latest_dt: Optional[dt.datetime] = None
        # "Last hour" window — the request defines outstanding as having at
        # least one error in any block within the previous 60 minutes from
        # "now" in the local time zone.
        now_local = dt.datetime.now(tz=LOCAL_TZ)
        one_day_ago = now_local - dt.timedelta(hours=24)
        had_error_last_day = False
        # Total errored result rows in the last day for this test (across
        # every agent/round). Surfaced on the dashboard as
        # "Total Errors in last day".
        errors_last_day = 0

        for r in data.get("results", []) or []:
            local_dt = _parse_start(r.get("startTime"))
            if not local_dt:
                continue
            if (newest_data_dt is None) or (local_dt > newest_data_dt):
                newest_data_dt = local_dt
            if (test_latest_dt is None) or (local_dt > test_latest_dt):
                test_latest_dt = local_dt
            label = _block_label(local_dt, block_minutes=block_minutes)
            per_block_total[label] += 1

            if ttype == "web-transactions":
                tt = r.get("transactionTime")
                if tt is not None:
                    secs = float(tt) / 1000.0
                    per_block_time_sum[label] += secs
                    per_block_time_n[label] += 1
                    if label in label_set:
                        tx_time_sum[label] += secs
                        tx_time_n[label] += 1
                if r.get("errorType"):
                    per_block_err_count[label] += 1
                    if label in label_set:
                        tx_err_tests[label].add(test_id)
                    if (latest_error_dt is None) or (local_dt > latest_error_dt):
                        latest_error_dt = local_dt
                    if local_dt >= one_day_ago:
                        had_error_last_day = True
                        errors_last_day += 1

            elif ttype == "page-load":
                plt = r.get("pageLoadTime")
                if plt is not None:
                    secs = float(plt) / 1000.0
                    per_block_time_sum[label] += secs
                    per_block_time_n[label] += 1
                    if label in label_set:
                        pl_time_sum[label] += secs
                        pl_time_n[label] += 1
                else:
                    per_block_err_count[label] += 1
                    if label in label_set:
                        pl_missing_tests[label].add(test_id)
                    if (latest_error_dt is None) or (local_dt > latest_error_dt):
                        latest_error_dt = local_dt
                    if local_dt >= one_day_ago:
                        had_error_last_day = True
                        errors_last_day += 1

        # Build per-test series for the table charts.
        availability = []
        avg_time = []
        for lbl in block_labels:
            total = per_block_total.get(lbl, 0)
            errs = per_block_err_count.get(lbl, 0)
            if total == 0:
                availability.append(None)
            else:
                availability.append(0 if errs > 0 else 100)
            n = per_block_time_n.get(lbl, 0)
            avg_time.append(round(per_block_time_sum[lbl] / n, 3) if n else None)

        # If the latest block has no data (test results older than the
        # block size, e.g. >15 minutes), fall back to the most recent block
        # that DID receive results — kept around so legacy callers that
        # used ``latest_avg`` for "current value" still work.
        latest_idx = None
        for i in range(len(block_labels) - 1, -1, -1):
            if per_block_total.get(block_labels[i], 0) > 0:
                latest_idx = i
                break
        if latest_idx is None:
            latest_idx = len(block_labels) - 1
        latest_avg = avg_time[latest_idx]

        # ``in_error_now`` — new semantics (per change-request):
        # the test had at least one error in any block within the last day.
        in_error_now = had_error_last_day

        # ``data_collection_problem`` — the cache has *no* round newer than
        # one hour ago. Tests with no data at all also qualify.
        if test_latest_dt is None:
            data_collection_problem = True
            last_data_age_seconds: Optional[float] = None
        else:
            age = (now_local - test_latest_dt).total_seconds()
            last_data_age_seconds = age
            data_collection_problem = age > 3600

        time_with_error = "—"
        if latest_error_dt is not None:
            time_with_error = _format_age(
                (now_local - latest_error_dt).total_seconds()
            )

        last_data_age_human = (
            _format_age(last_data_age_seconds)
            if last_data_age_seconds is not None else "—"
        )

        # Test Health — the percentage of rounds within the user-selected
        # time range where the test was NOT outstanding. For tx tests a
        # round counts as healthy when ``errorType`` is absent; for pl
        # tests a round counts as healthy when ``pageLoadTime`` is present.
        # Both definitions reduce to: healthy_rounds = total - errored.
        range_total = sum(per_block_total.get(l, 0) for l in block_labels)
        range_errors = sum(per_block_err_count.get(l, 0) for l in block_labels)
        if range_total > 0:
            health_pct = max(
                0.0,
                min(100.0, (range_total - range_errors) * 100.0 / range_total),
            )
            test_health = round(health_pct, 1)
        else:
            test_health = None

        entry = {
            "meta": meta,
            "availability": availability,
            "avg_time": avg_time,
            "in_error_now": in_error_now,
            "data_collection_problem": data_collection_problem,
            "last_data_age_seconds": last_data_age_seconds,
            "last_data_age": last_data_age_human,
            "latest_avg": latest_avg,
            "time_with_error": time_with_error,
            "errors_last_day": errors_last_day,
            "test_health": test_health,
            "rounds_in_range": range_total,
            "errors_in_range": range_errors,
        }
        if ttype == "web-transactions":
            tx_series[test_id] = entry
        elif ttype == "page-load":
            pl_series[test_id] = entry

    # Org-level series -----------------------------------------------------
    # Counts (widget1/widget3): zero is a meaningful value — a block with
    # data but no tests in error renders as 0. Averages (widget2/widget4)
    # use ``None`` for empty blocks so the chart draws a gap instead of a
    # misleading drop to zero.
    widget1 = [len(tx_err_tests.get(lbl, set())) for lbl in block_labels]
    widget2 = [round(tx_time_sum[lbl] / tx_time_n[lbl], 3) if tx_time_n[lbl] else None
               for lbl in block_labels]
    widget3 = [len(pl_missing_tests.get(lbl, set())) for lbl in block_labels]
    widget4 = [round(pl_time_sum[lbl] / pl_time_n[lbl], 3) if pl_time_n[lbl] else None
               for lbl in block_labels]

    # Tables ---------------------------------------------------------------
    # A test with a data-collection problem (no rounds in > 1h) is surfaced
    # in its own table rather than the "in error" one — its lack of recent
    # results would otherwise produce a misleading "in error" verdict.
    tx_no_data = sorted(
        [v for v in tx_series.values() if v["data_collection_problem"]],
        key=lambda v: v["meta"]["testName"].lower(),
    )
    tx_outstanding = sorted(
        [v for v in tx_series.values()
         if v["in_error_now"] and not v["data_collection_problem"]],
        key=lambda v: v["meta"]["testName"].lower(),
    )
    tx_top5 = sorted(
        [v for v in tx_series.values()
         if v["latest_avg"] is not None and not v["data_collection_problem"]],
        key=lambda v: v["latest_avg"], reverse=True,
    )[:5]

    pl_no_data = sorted(
        [v for v in pl_series.values() if v["data_collection_problem"]],
        key=lambda v: v["meta"]["testName"].lower(),
    )
    pl_outstanding = sorted(
        [v for v in pl_series.values()
         if v["in_error_now"] and not v["data_collection_problem"]],
        key=lambda v: v["meta"]["testName"].lower(),
    )
    pl_top5 = sorted(
        [v for v in pl_series.values()
         if v["latest_avg"] is not None and not v["data_collection_problem"]],
        key=lambda v: v["latest_avg"], reverse=True,
    )[:5]

    pretty_fmt = "%m/%d %Hh" if block_minutes >= 60 else "%H:%M"
    pretty_labels = [_label_to_dt(l).strftime(pretty_fmt) for l in block_labels]

    if newest_data_dt is not None:
        newest_iso = newest_data_dt.isoformat()
        data_age_seconds = (dt.datetime.now(tz=LOCAL_TZ)
                            - newest_data_dt).total_seconds()
    else:
        newest_iso = None
        data_age_seconds = None

    return {
        "blocks": block_labels,
        "labels": pretty_labels,
        "block_minutes": block_minutes,
        "newest_data_at": newest_iso,
        "data_age_seconds": data_age_seconds,
        "widget1": widget1,
        "widget2": widget2,
        "widget3": widget3,
        "widget4": widget4,
        "tx_outstanding": tx_outstanding,
        "tx_top5": tx_top5,
        "tx_no_data": tx_no_data,
        "pl_outstanding": pl_outstanding,
        "pl_top5": pl_top5,
        "pl_no_data": pl_no_data,
        "totals": {
            "tx_tests": len(tx_series),
            "pl_tests": len(pl_series),
            "tx_in_error": len(tx_outstanding),
            "pl_in_error": len(pl_outstanding),
            "tx_no_data": len(tx_no_data),
            "pl_no_data": len(pl_no_data),
            "ignored": len(ignored_list),
        },
        "ignored": sorted(ignored_list, key=lambda x: (x.get("testName") or "").lower()),
    }
