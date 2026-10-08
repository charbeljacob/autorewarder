#!/usr/bin/env python3
"""
src/vpn_manager.py

Python port of the per-account VPN orchestration from
check-points-realchrome.js (Node.js/Playwright version).

Provides:
  - Per-account VPN server selection (VPN_SERVER)
  - Local-IP sentinel support ("disconnect", "none", "local", "off")
  - Single-VPN-for-all override (USE_SINGLE_VPN_FOR_ALL)
  - ExpressVPN CLI orchestration (connect / disconnect / status)
  - Public-IP verification against expected GEO_LOCALE
    - Primary source: ipinfo.io/json
    - Fallback source: api.ip.sb/geoip
    - Cross-check when primary reports a mismatched country
  - System timezone synchronization (Linux: timedatectl, Windows: tzutil)
  - Country -> timezone fallback tables for both platforms

Importable as: `from src.vpn_manager import ensure_vpn_for_account`
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional, Union
from urllib import request as urllib_request

# ─── Config ──────────────────────────────────────────────────────────────

IS_WINDOWS = platform.system().lower().startswith("win")

VPN_CLI_BIN = "expressvpnctl"

VPN_CONNECT_WAIT_MS = 8_000
VPN_VERIFY_RETRIES  = 4
VPN_VERIFY_WAIT_MS  = 4_000

USE_SINGLE_VPN_FOR_ALL = False
SINGLE_VPN_SERVER      = "usa-new-jersey-1"

ADJUST_SYSTEM_TIMEZONE = True

VPN_DISCONNECT_SENTINELS = {"disconnect", "none", "local", "off"}

# ─── Country → timezone fallbacks ────────────────────────────────────────

COUNTRY_TIMEZONE_FALLBACK = {
    "GB": "Europe/London", "IE": "Europe/Dublin", "FR": "Europe/Paris",
    "DE": "Europe/Berlin", "ES": "Europe/Madrid", "IT": "Europe/Rome",
    "NL": "Europe/Amsterdam", "BE": "Europe/Brussels", "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo", "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki",
    "PL": "Europe/Warsaw", "AT": "Europe/Vienna", "CH": "Europe/Zurich",
    "PT": "Europe/Lisbon", "GR": "Europe/Athens", "TR": "Europe/Istanbul",
    "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "SG": "Asia/Singapore",
    "HK": "Asia/Hong_Kong", "IN": "Asia/Kolkata", "PH": "Asia/Manila",
    "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh", "MY": "Asia/Kuala_Lumpur",
    "NZ": "Pacific/Auckland", "ZA": "Africa/Johannesburg",
    "AE": "Asia/Dubai", "IL": "Asia/Jerusalem", "US": "America/New_York",
}

WINDOWS_COUNTRY_TIMEZONE_FALLBACK = {
    "GB": "GMT Standard Time", "IE": "GMT Standard Time", "PT": "GMT Standard Time",
    "FR": "Romance Standard Time", "ES": "Romance Standard Time",
    "BE": "Romance Standard Time", "DK": "Romance Standard Time",
    "DE": "W. Europe Standard Time", "IT": "W. Europe Standard Time",
    "NL": "W. Europe Standard Time", "SE": "W. Europe Standard Time",
    "NO": "W. Europe Standard Time", "AT": "W. Europe Standard Time",
    "CH": "W. Europe Standard Time",
    "FI": "FLE Standard Time", "PL": "Central European Standard Time",
    "GR": "GTB Standard Time", "TR": "Turkey Standard Time",
    "JP": "Tokyo Standard Time", "KR": "Korea Standard Time",
    "SG": "Singapore Standard Time", "MY": "Singapore Standard Time",
    "HK": "China Standard Time", "IN": "India Standard Time",
    "PH": "Singapore Standard Time",
    "TH": "SE Asia Standard Time", "VN": "SE Asia Standard Time",
    "NZ": "New Zealand Standard Time", "ZA": "South Africa Standard Time",
    "AE": "Arabian Standard Time", "IL": "Israel Standard Time",
    "US": "Eastern Standard Time",
}


# ─── Account normalization ───────────────────────────────────────────────

@dataclass
class _AccountView:
    email: str
    vpn_server: Optional[str]
    geo_locale: Optional[str]


def adapt_account(account: Any) -> _AccountView:
    """Normalize a dict or object into an _AccountView."""
    def _get(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    return _AccountView(
        email=_get(account, "email") or "",
        vpn_server=_get(account, "vpn_server") or _get(account, "VPN_SERVER"),
        geo_locale=_get(account, "geo_locale") or _get(account, "GEO_LOCALE"),
    )


# ─── Low-level helpers ───────────────────────────────────────────────────

def _sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000.0)


def vpn_cli(args: str) -> str:
    """Run `expressvpnctl <args>`; return combined stdout/stderr (or "")."""
    if shutil.which(VPN_CLI_BIN) is None:
        return ""
    try:
        result = subprocess.run(
            f'{VPN_CLI_BIN} {args}',
            shell=True, capture_output=True, text=True, timeout=20,
        )
        return (result.stdout or result.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""


def vpn_cli_available() -> bool:
    if shutil.which(VPN_CLI_BIN) is None:
        return False
    try:
        subprocess.run(
            f"{VPN_CLI_BIN} --version",
            shell=True, capture_output=True, text=True, timeout=6,
        )
        return True
    except Exception:
        return False


def get_vpn_status() -> dict:
    """
    Return {'connected': bool, 'location': Optional[str]}.

    Newer versions of the ExpressVPN Linux app print extra info after the
    "Connected to <server>" line, e.g.:

        Connected to australia-sydney-2
        protocol in use: lightwayudp
        network lock: enabled when connected
        split tunnel: disabled

    We only want the server name from the first line. The old
    `rsplit("connected to")` approach swallowed all the extra lines, which
    caused `is_same_server()` to always fail and the account to be skipped.
    """
    out = vpn_cli("status")
    if not out:
        return {"connected": False, "location": None}

    for raw_line in out.splitlines():
        line = raw_line.strip()
        low = line.lower()
        if "connected to" in low:
            # Take the substring after the marker on this single line only.
            server = line.split("connected to", 1)[1].strip()
            if server:
                return {"connected": True, "location": server}

    return {"connected": False, "location": None}

def is_same_server(current_location: Optional[str], target_server: str) -> bool:
    if not current_location or not target_server:
        return False
    return current_location.strip().lower() == target_server.strip().lower()


def is_vpn_disconnect_sentinel(vpn_server: Optional[str]) -> bool:
    if not vpn_server:
        return False
    return str(vpn_server).strip().lower() in VPN_DISCONNECT_SENTINELS


# ─── IP verification ─────────────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _http_get_json(url: str, timeout: int = 10) -> dict:
    req = urllib_request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_ip_info_io() -> dict:
    try:
        info = _http_get_json("https://ipinfo.io/json", timeout=10)
        if not info or "country" not in info:
            raise ValueError('ipinfo.io response missing "country" field')
        return {
            "ok": True, "ip": info.get("ip"), "country": info.get("country"),
            "city": info.get("city"), "org": info.get("org"),
            "timezone": info.get("timezone"), "source": "ipinfo.io",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "source": "ipinfo.io"}


def check_ip_sb() -> dict:
    try:
        info = _http_get_json("https://api.ip.sb/geoip", timeout=10)
        if not info or "country_code" not in info:
            raise ValueError('api.ip.sb response missing "country_code" field')
        return {
            "ok": True, "ip": info.get("ip"), "country": info.get("country_code"),
            "city": info.get("city"), "org": info.get("isp"),
            "source": "api.ip.sb",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "source": "api.ip.sb"}


def verify_ip_location(expected_country: str) -> dict:
    expected_country = expected_country.upper()
    primary = check_ip_info_io()
    if not primary["ok"]:
        print(f"[VPN] ipinfo.io failed to respond ({primary['error']}) — "
              f"falling back to api.ip.sb/geoip")
        return check_ip_sb()
    if primary["country"] == expected_country:
        return primary
    print(f'[VPN] ipinfo.io reports "{primary["country"]}" (expected '
          f'"{expected_country}") — cross-checking with api.ip.sb/geoip')
    cross = check_ip_sb()
    if cross["ok"] and cross["country"] == expected_country:
        print(f'[VPN] api.ip.sb/geoip confirms "{expected_country}" — '
              f"ipinfo.io's GeoIP data for this IP was stale/wrong")
        return cross
    return primary


# ─── Timezone sync ───────────────────────────────────────────────────────

def resolve_timezone(ip_check: dict, expected_country: str) -> Optional[str]:
    expected_country = (expected_country or "").upper()
    if IS_WINDOWS:
        return WINDOWS_COUNTRY_TIMEZONE_FALLBACK.get(expected_country)
    if ip_check.get("source") == "ipinfo.io" and ip_check.get("timezone"):
        return ip_check["timezone"]
    return COUNTRY_TIMEZONE_FALLBACK.get(expected_country)


def get_current_timezone() -> Optional[str]:
    try:
        if IS_WINDOWS:
            r = subprocess.run(["tzutil", "/g"], capture_output=True, text=True, timeout=5)
            return r.stdout.strip() or None
        r = subprocess.run(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def sync_system_timezone(ip_check: dict, expected_country: str, label: str) -> None:
    tz = resolve_timezone(ip_check, expected_country)
    if not tz:
        print(f'{label} [TZ] No timezone mapping available for '
              f'"{expected_country}" — leaving system timezone unchanged')
        return
    current = get_current_timezone()
    if current == tz:
        print(f"{label} [TZ] Already set to {tz}")
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["tzutil", "/s", tz], capture_output=True, check=True, timeout=8)
        else:
            subprocess.run(
                ["sudo", "-n", "timedatectl", "set-timezone", tz],
                capture_output=True, check=True, timeout=8,
            )
        print(f"{label} [TZ] ✓ System timezone set to {tz} (was {current or 'unknown'})")
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or b"").decode(errors="replace").strip() or str(e)
        print(f"{label} [TZ] Failed to set timezone to {tz}: {msg}")
    except Exception as e:
        print(f"{label} [TZ] Failed to set timezone to {tz}: {e}")


# ─── VPN orchestration ───────────────────────────────────────────────────

def resolve_vpn_server(account: _AccountView) -> Optional[str]:
    if USE_SINGLE_VPN_FOR_ALL:
        return SINGLE_VPN_SERVER
    return account.vpn_server


def vpn_use_local_ip(account: _AccountView, label: str) -> tuple[bool, Optional[str]]:
    print(f"{label} [VPN] Local-IP sentinel set — using machine's local IP (no VPN)")
    if not vpn_cli_available():
        print(f"{label} [VPN] VPN CLI not found — assuming VPN is already off")
        return True, None
    status = get_vpn_status()
    if status["connected"]:
        print(f"{label} [VPN] Disconnecting from {status['location']}")
        vpn_cli("disconnect")
        _sleep_ms(2000)
        after = get_vpn_status()
        if after["connected"]:
            print(f"{label} [VPN] Still showing connected to {after['location']} "
                  f"after disconnect — proceeding anyway")
        else:
            print(f"{label} [VPN] ✓ Disconnected — VPN off")
    else:
        print(f"{label} [VPN] Already disconnected — VPN off")

    if not account.geo_locale:
        print(f"{label} [VPN] No GEO_LOCALE configured — skipping IP verification")
        return True, None
    expected = account.geo_locale.strip().upper()
    ip_check = verify_ip_location(expected)
    if not ip_check["ok"]:
        print(f"{label} [VPN] Could not verify local IP — proceeding")
        return True, None
    if ip_check["country"] != expected:
        reason = (f'Local IP is in "{ip_check["country"]}" (via {ip_check["source"]}), '
                  f'but GEO_LOCALE is "{expected}" (IP: {ip_check["ip"]}).')
        print(f"{label} [VPN] ✗ {reason} — SKIPPING account")
        return False, reason
    print(f'{label} [VPN] ✓ Local IP confirmed in {ip_check["country"]} '
          f'via {ip_check["source"]} | {ip_check["ip"]}')
    if ADJUST_SYSTEM_TIMEZONE:
        sync_system_timezone(ip_check, expected, label)
    return True, None


def ensure_vpn_for_account(account: Union[dict, Any]) -> tuple[bool, Optional[str]]:
    """
    Ensure the correct VPN is active. Returns (ok, reason).
    ok=True → proceed; ok=False → skip this account.
    """
    acct = adapt_account(account)
    label = f"[{acct.email or 'unknown'}]"
    vpn_server = resolve_vpn_server(acct)

    if is_vpn_disconnect_sentinel(vpn_server):
        return vpn_use_local_ip(acct, label)

    if not vpn_server:
        reason = "No VPN_SERVER configured for this account"
        print(f"{label} [VPN] {reason} — SKIPPING account")
        return False, reason

    if USE_SINGLE_VPN_FOR_ALL:
        print(f"{label} [VPN] USE_SINGLE_VPN_FOR_ALL is on — using "
              f"{vpn_server} regardless of per-account config")

    if not vpn_cli_available():
        print(f'{label} [VPN] "{VPN_CLI_BIN}" not found or not runnable — '
              f"skipping VPN check.")
        return True, None

    status = get_vpn_status()
    if status["connected"] and is_same_server(status["location"], vpn_server):
        print(f"{label} [VPN] ✓ Already connected to {status['location']} — "
              f"skipping disconnect/connect")
    else:
        if status["connected"]:
            print(f"{label} [VPN] Connected to {status['location']}, but need "
                  f"{vpn_server} — switching")
            vpn_cli("disconnect")
            _sleep_ms(2000)
        else:
            print(f"{label} [VPN] Not connected — connecting to {vpn_server}...")
        vpn_cli(f'connect "{vpn_server}"')
        _sleep_ms(VPN_CONNECT_WAIT_MS)
        confirmed = False
        for attempt in range(1, VPN_VERIFY_RETRIES + 1):
            status = get_vpn_status()
            if status["connected"] and is_same_server(status["location"], vpn_server):
                print(f"{label} [VPN] ✓ Connected to {status['location']}")
                confirmed = True
                break
            if attempt < VPN_VERIFY_RETRIES:
                _sleep_ms(VPN_VERIFY_WAIT_MS)
        if not confirmed:
            reason = f'Could not connect to VPN server "{vpn_server}" via {VPN_CLI_BIN}'
            print(f"{label} [VPN] ✗ {reason} — SKIPPING account")
            return False, reason

    if not acct.geo_locale:
        print(f"{label} [VPN] No GEO_LOCALE configured — skipping IP geolocation verification")
        return True, None

    expected = acct.geo_locale.strip().upper()
    print(f"{label} [VPN] Verifying real public IP location...")
    ip_check = verify_ip_location(expected)
    if not ip_check["ok"]:
        print(f'{label} [VPN] Could not verify IP location: {ip_check["error"]} '
              f"— proceeding without confirmation")
        return True, None
    if ip_check["country"] == expected:
        print(f'{label} [VPN] ✓ Public IP confirmed in {ip_check["country"]} '
              f'({ip_check.get("city") or "?"}) via {ip_check["source"]} '
              f'| IP: {ip_check["ip"]}')
        if ADJUST_SYSTEM_TIMEZONE:
            sync_system_timezone(ip_check, expected, label)
        return True, None
    reason = (f'Public IP is in "{ip_check["country"]}" (via {ip_check["source"]}), '
              f'expected "{expected}" (IP: {ip_check["ip"]})')
    print(f"{label} [VPN] ✗ {reason} — SKIPPING account")
    return False, reason


# ─── Standalone test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.vpn_manager <vpn_server> [geo_locale] [email]")
        sys.exit(1)
    demo = {
        "email": sys.argv[3] if len(sys.argv) > 3 else "demo@example.com",
        "vpn_server": sys.argv[1],
        "geo_locale": sys.argv[2] if len(sys.argv) > 2 else None,
    }
    ok, reason = ensure_vpn_for_account(demo)
    print(f"\nResult: ok={ok} reason={reason}")
    sys.exit(0 if ok else 2)
