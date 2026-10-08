#!/usr/bin/env python3
"""
open_account.py — Open an account's Edge profile with the project's own
driver manager, without running any searches or activities.

Usage:
    .venv/bin/python open_account.py <account_id-or-label> [--headless] [--url URL]
"""

import argparse
import sys
import time

from src.api import AutoRewarderAPI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", help="Account id or label (as in accounts.json)")
    parser.add_argument("--headless", action="store_true",
                        help="Run headless (no window)")
    parser.add_argument("--url", default="https://www.bing.com",
                        help="Initial URL (default: https://www.bing.com)")
    parser.add_argument("--read-balance", action="store_true",
                        help="Also scrape the Rewards points balance and update stats")
    args = parser.parse_args()

    api = AutoRewarderAPI()

    # Resolve the account by id or label
    target = None
    for acc in api.account_manager.list():
        if acc["id"] == args.account or acc["label"].strip().lower() == args.account.strip().lower():
            target = acc
            break
    if target is None:
        print(f"[ERROR] No account matches '{args.account}'. Known accounts:")
        for acc in api.account_manager.list():
            print(f"  {acc['id']}  {acc['label']}")
        sys.exit(1)

    # Make this account current and rebuild the per-account managers
    if api.account_manager.current_id() != target["id"]:
        api.account_manager.select(target["id"])
        api._rebuild_account_context()
        # Apply VPN for this account (reads meta.json's vpn block)
    vpn_ok, vpn_reason = api._apply_vpn_for_current_account()
    if not vpn_ok:
        print(f"[VPN] ✗ {vpn_reason}")
        sys.exit(2)
    print(f"Opening Edge with profile for '{target['label']}' ({target['id']})...")
    driver = api.driver_manager.setup_driver(headless=args.headless)

    try:
        driver.get(args.url)

        if args.read_balance:
            print("Scraping Rewards points balance...")
            balance = api._fetch_balance_with_driver(driver)
            if balance is None:
                print("[WARN] Could not read the points balance.")
            else:
                api.stats.update_balance(balance)
                print(f"Points balance: {balance:,}")

        if args.headless:
            print("Headless mode — exiting.")
            time.sleep(3)
        else:
            print("Browser is open. Close the window (or press Ctrl+C here) when done.")
            # Wait until the user closes every window, or Ctrl+C
            try:
                while len(driver.window_handles) > 0:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nInterrupted.")

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("Done.")


if __name__ == "__main__":
    main()
