#!/usr/bin/env python3
"""
donate.py — Open an account's Edge profile directly (no Selenium) at the
Microsoft Rewards donate page.

Why not Selenium: the donate captcha (Arkose) detects CDP attachment and
automation flags, and refuses to solve. A direct Edge launch using the
same user-data-dir has none of those markers, so the captcha behaves
normally.

Reads the account's `vpn` block from meta.json and connects to that
ExpressVPN server first (if one is set), then launches Edge.

Usage:
    .venv/bin/python donate.py "Account 1"
    .venv/bin/python donate.py 54c4267e2641 --url https://rewards.bing.com/redeem
"""

import argparse
import os
import subprocess
import sys
import time

from src.config import edge_profile_path
from src.accounts import AccountManager, GlobalSettingsManager, AccountMetaManager


def resolve_account(am, token):
    token = token.strip().lower()
    for acc in am.list():
        if acc["id"].lower() == token or acc["label"].strip().lower() == token:
            return acc
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("account", help="Account id or label")
    parser.add_argument(
        "--url",
        default="https://rewards.bing.com/redeem",
        help="Page to open (default: Rewards redeem/donate)",
    )
    parser.add_argument(
        "--no-vpn",
        action="store_true",
        help="Skip the VPN connection step (use whatever VPN is currently active)",
    )
    args = parser.parse_args()

    am = AccountManager(GlobalSettingsManager())
    target = resolve_account(am, args.account)
    if target is None:
        print(f"[ERROR] No account matches '{args.account}'. Known accounts:")
        for acc in am.list():
            print(f"  {acc['id']}  {acc['label']}")
        sys.exit(1)

    # 1. Apply VPN (unless --no-vpn)
    if not args.no_vpn:
        vpn_cfg = AccountMetaManager(target["id"]).get_vpn_config()
        server = vpn_cfg.get("server")
        if server:
            try:
                from src.vpn_manager import ensure_vpn_for_account
                ok, reason = ensure_vpn_for_account(
                    {
                        "email": target["label"],
                        "vpn_server": server,
                        "geo_locale": vpn_cfg.get("geo_locale"),
                    }
                )
                if not ok:
                    print(f"[VPN] ✗ {reason}")
                    print("Aborting. Use --no-vpn to skip VPN handling.")
                    sys.exit(2)
            except Exception as e:
                print(f"[VPN] ✗ VPN manager failed: {e}")
                print("Aborting. Use --no-vpn to skip VPN handling.")
                sys.exit(2)
        else:
            print(f"[VPN] No server configured for '{target['label']}' — skipping.")

    # 2. Resolve the profile dir
    profile_dir = edge_profile_path(target["id"])
    if not os.path.isdir(profile_dir):
        print(f"[ERROR] Profile directory not found: {profile_dir}")
        print("Run First Setup through the GUI for this account first.")
        sys.exit(1)

    # 3. Kill any running Edge so it isn't holding the profile lock.
    #    Only Edge instances using OUR profile would conflict; the pkill is
    #    broad because Selenium's Edge is always under the same process name,
    #    and the user is running this manually between bot runs anyway.
    subprocess.run(["pkill", "-f", "microsoft-edge"], check=False)
    time.sleep(1.5)

    # 4. Launch Edge directly — no automation flags, no CDP.
    print()
    print(f"Opening Edge for '{target['label']}'")
    print(f"  Profile: {profile_dir}")
    print(f"  URL:     {args.url}")
    print()
    print("Close the Edge window when you're done donating.")
    print()

    subprocess.Popen(
        [
            "microsoft-edge",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=TranslateUI",
            args.url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    main()
