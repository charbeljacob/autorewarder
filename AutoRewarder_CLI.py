"""
AutoRewarder headless / scheduled runner (multi-account aware).

…

VPN note: per-account VPN setup is applied inside `AutoRewarderAPI.main()`
(the same funnel the GUI Start button uses), not here. `_run_scheduled`
merely checks `main()`'s bool return value to bail out of its batch loop
when a batch was skipped — e.g. because VPN setup for this account failed.
"""

import argparse
import math
import os
import random
import sys
import time
from datetime import date, datetime

from src.api import AutoRewarderAPI
from src.accounts import AccountMetaManager
from src.config import LOG_FILE_PATH, LOG_MAX_SIZE


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _iso_now():
    return datetime.now().isoformat(timespec="seconds")


def console_log(message):
    line = f"[{_iso_now()}] {message}"
    print(line)
    try:
        if (os.path.exists(LOG_FILE_PATH)
                and os.path.getsize(LOG_FILE_PATH) >= LOG_MAX_SIZE):
            try:
                os.remove(LOG_FILE_PATH)
            except OSError:
                pass
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as e:
        print(f"[ERROR] Can't write log file: {e}")


# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------

def _run_once(api, pc, mobile):
    """Single burst: PC then Mobile, in one go."""
    console_log(f"Single run: PC={pc}, Mobile={mobile}")
    try:
        ok = api.main(int(pc), int(mobile))
        if ok is False:
            console_log("[WARNING] api.main() reported the run was skipped "
                        "(e.g. VPN failure).")
    except Exception as e:
        console_log(f"[ERROR] Run failed: {e}")


def _run_scheduled(api, pc, mobile, duration_hours, queries_per_hour):
    """Drip-feed `pc + mobile` queries across `duration_hours`."""
    pc = int(pc)
    mobile = int(mobile)
    total = pc + mobile
    duration_hours = float(duration_hours)
    qph = int(queries_per_hour) if queries_per_hour else 0

    console_log(
        f"Scheduled run: PC={pc}, Mobile={mobile} over {duration_hours}h (qph={qph})"
    )

    if total <= 0:
        console_log("Nothing scheduled (PC + Mobile = 0).")
        return

    if qph > 0:
        raw_batch = qph // 6
    else:
        raw_batch = total // max(1, int(duration_hours * 2))
    per_batch = max(1, min(10, raw_batch))

    num_batches = math.ceil(total / per_batch)
    total_seconds = duration_hours * 3600
    interval = total_seconds / max(num_batches, 1)

    console_log(
        f"Planning {num_batches} batches of ~{per_batch} queries, interval ~{interval:.1f}s"
    )

    pc_left = pc
    mobile_left = mobile

    for i in range(num_batches):
        if pc_left > 0:
            batch_pc = min(per_batch, pc_left)
            batch_mobile = 0
        else:
            batch_pc = 0
            batch_mobile = min(per_batch, mobile_left)

        if batch_pc == 0 and batch_mobile == 0:
            break

        console_log(
            f"Batch {i+1}/{num_batches}: PC={batch_pc}, Mobile={batch_mobile} "
            f"(PC left {pc_left}, Mobile left {mobile_left})"
        )

        # ── NEW: respect api.main()'s return value ────────────────────
        # A False return means the batch was skipped entirely (e.g. VPN
        # setup failed for this account). Nothing was consumed, and the
        # condition won't resolve itself mid-loop, so abort the schedule.
        try:
            ok = api.main(batch_pc, batch_mobile)
        except Exception as e:
            console_log(f"[ERROR] Batch {i+1} failed: {e}")
            ok = True  # treat unexpected errors as "continue" (existing behavior)

        if ok is False:
            console_log(
                f"Batch {i+1} was skipped (see above) — aborting this schedule "
                f"for this account."
            )
            return
        # ──────────────────────────────────────────────────────────────

        pc_left -= batch_pc
        mobile_left -= batch_mobile

        if pc_left <= 0 and mobile_left <= 0:
            break

        sleep_time = max(5.0, interval * random.uniform(0.75, 1.25))
        console_log(f"Sleeping {sleep_time:.1f}s until next batch")
        time.sleep(sleep_time)

    console_log("Scheduled run complete.")


# ---------------------------------------------------------------------------
# API bootstrap
# ---------------------------------------------------------------------------

def _create_headless_api():
    api = AutoRewarderAPI()
    api.log = console_log
    api._safe_log = console_log

    api.hide_browser = True
    if api.driver_manager is not None:
        api.driver_manager.hide_browser = True

    if api.history is not None:
        api.history._logger = console_log
    if api.daily_set is not None:
        api.daily_set.logger = console_log
    if api.search_engine is not None:
        api.search_engine._logger = console_log
    if api.stats is not None:
        api.stats._logger = console_log

    return api


def _resolve_account(api, token):
    if not token:
        return None
    for acc in api.account_manager.list():
        if acc["id"] == token or acc["label"].strip().lower() == token.strip().lower():
            return acc
    return None


def _mark_triggered_today(account_id):
    meta = AccountMetaManager(account_id)
    sched = meta.get_schedule()
    sched["last_triggered_date"] = date.today().isoformat()
    meta.set_schedule(sched)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _run_account(api, acc, pc_override=None, mobile_override=None, force=False):
    """Run a single account. Returns True if something executed."""
    aid = acc["id"]
    label = acc["label"]

    if not acc["first_setup_done"]:
        console_log(f"Skipping '{label}': First Setup not completed.")
        return False

    meta = AccountMetaManager(aid)
    sched = meta.get_schedule()

    if pc_override is None and mobile_override is None:
        if not sched.get("enabled"):
            console_log(f"Skipping '{label}': schedule disabled.")
            return False
        today = date.today().isoformat()
        if not force and sched.get("last_triggered_date") == today:
            console_log(f"Skipping '{label}': already triggered today.")
            return False

    pc = int(pc_override if pc_override is not None else sched.get("queries_pc", 0))
    mobile = int(
        mobile_override
        if mobile_override is not None
        else sched.get("queries_mobile", 0)
    )

    if pc + mobile <= 0:
        console_log(f"Skipping '{label}': both PC and Mobile counts are 0.")
        return False

    # (VPN runs inside api.main() — see _apply_vpn_for_current_account.)

    if api.account_manager.current_id() != aid:
        console_log(f"Switching to account '{label}'.")
        api.account_manager.select(aid)
        api._rebuild_account_context()
        if api.history is not None:
            api.history._logger = console_log
        if api.daily_set is not None:
            api.daily_set.logger = console_log
        if api.search_engine is not None:
            api.search_engine._logger = console_log
        if api.stats is not None:
            api.stats._logger = console_log

    if pc_override is None and mobile_override is None:
        _mark_triggered_today(aid)

    if sched.get("advancedScheduling") and (
        pc_override is None and mobile_override is None
    ):
        _run_scheduled(
            api, pc, mobile,
            sched.get("runDuration", 3),
            sched.get("queriesPerHour", 10),
        )
    else:
        _run_once(api, pc, mobile)

    return True


def main():
    """Parse CLI args and run scheduled or targeted accounts."""
    parser = argparse.ArgumentParser(
        description="AutoRewarder headless / scheduled runner (multi-account aware)"
    )
    parser.add_argument("--account", help="Run only this account (by id or label).")
    parser.add_argument("--pc", type=int, help="Override PC queries for this run.")
    parser.add_argument("--mobile", type=int, help="Override Mobile queries for this run.")
    parser.add_argument("--force", action="store_true",
                        help="Run even if already triggered today.")
    args = parser.parse_args()

    if args.pc is not None and args.pc < 0:
        parser.error("--pc must be >= 0")
    if args.mobile is not None and args.mobile < 0:
        parser.error("--mobile must be >= 0")

    api = _create_headless_api()

    accounts = api.account_manager.list()
    if not accounts:
        console_log("No accounts configured. Nothing to do.")
        return

    if args.account:
        acc = _resolve_account(api, args.account)
        if acc is None:
            console_log(f"[ERROR] No account matches '{args.account}'.")
            return
        _run_account(api, acc, pc_override=args.pc,
                     mobile_override=args.mobile, force=args.force)
        return

    ran_any = False
    for acc in accounts:
        if _run_account(api, acc, force=args.force):
            ran_any = True
    if not ran_any:
        console_log("No schedules matched today.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console_log("Interrupted by user; exiting.")
        sys.exit(0)