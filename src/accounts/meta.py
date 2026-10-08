"""Per-account metadata persistence (first_setup_done, schedule, VPN)."""

import json
import os

from ..config import account_dir, account_meta_path

DEFAULT_ACCOUNT_SCHEDULE = {
    "enabled": False,
    "advancedScheduling": False,
    "runDuration": 3,
    "queriesPerHour": 10,
    "queries_pc": 30,
    "queries_mobile": 20,
    "last_triggered_date": None,
    "run_time": "09:00",
}

# ── NEW: per-account VPN configuration ──────────────────────────────────
# Stored under meta["vpn"] in the account's meta.json.
#   server     — ExpressVPN alias, e.g. "usa-new-jersey-1".
#                Leave unset to skip VPN handling entirely.
#                Sentinels "disconnect"/"none"/"local"/"off" force VPN off.
#   geo_locale — ISO-3166 alpha-2 code expected from the exit IP
#                ("US", "GB", "PH"). When set, the exit IP is verified
#                and the system timezone synced to the VPN's country.
DEFAULT_ACCOUNT_VPN = {
    "server": None,
    "geo_locale": None,
}
# ────────────────────────────────────────────────────────────────────────

DASHBOARD_VARIANTS = ("auto", "legacy", "new")
DEFAULT_DASHBOARD_VARIANT = "auto"


def default_account_schedule():
    """Return a fresh copy of the default per-account schedule."""
    return dict(DEFAULT_ACCOUNT_SCHEDULE)


def default_account_vpn():
    """Return a fresh copy of the default per-account VPN config."""
    return dict(DEFAULT_ACCOUNT_VPN)


def _read_json(path, default):
    """Read a JSON file. On any parse/IO failure, back it up and return default."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError):
        backup_path = path + ".backup"
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass
        try:
            os.replace(path, backup_path)
        except OSError:
            pass
        return default


def _write_json(path, data):
    """Atomically write JSON via a temp file rename, retrying transient Windows locks."""
    import time as _time

    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"

    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except OSError:
            pass

    last_err = None
    for attempt in range(4):
        try:
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=4)
            os.replace(temp_path, path)
            return
        except PermissionError as e:
            last_err = e
            _time.sleep(0.15 * (attempt + 1))
        except OSError as e:
            last_err = e
            _time.sleep(0.1)
    raise last_err if last_err else OSError(f"Could not write {path}")


class AccountMetaManager:
    """
    Per-account metadata (first_setup_done, schedule, VPN, dashboard variant).
    Stored at accounts/<account_id>/meta.json.
    """

    def __init__(self, account_id):
        self.account_id = account_id
        self.path = account_meta_path(account_id)

    def get_meta(self):
        """Return per-account meta merged with defaults."""
        defaults = {"first_setup_done": False}

        if not os.path.exists(account_dir(self.account_id)):
            try:
                os.makedirs(account_dir(self.account_id), exist_ok=True)
            except OSError:
                pass

        if not os.path.exists(self.path):
            try:
                self.save_meta(defaults)
            except OSError:
                pass
            return defaults

        meta = _read_json(self.path, None)
        if not isinstance(meta, dict):
            try:
                self.save_meta(defaults)
            except OSError:
                pass
            return defaults

        return {**defaults, **meta}

    def save_meta(self, meta):
        """Persist per-account meta to disk."""
        _write_json(self.path, meta)

    def is_first_setup_done(self):
        return bool(self.get_meta().get("first_setup_done"))

    def mark_up_as_done(self):
        meta = self.get_meta()
        meta["first_setup_done"] = True
        self.save_meta(meta)

    def get_schedule(self):
        meta = self.get_meta()
        sched = meta.get("schedule") if isinstance(meta, dict) else None
        merged = default_account_schedule()
        if isinstance(sched, dict):
            merged.update({k: sched.get(k, v) for k, v in merged.items()})
        return merged

    def set_schedule(self, sched):
        meta = self.get_meta()
        meta["schedule"] = sched
        self.save_meta(meta)

    # ── NEW: per-account VPN ───────────────────────────────────────────

    def get_vpn_config(self):
        """
        Return this account's VPN config with defaults for missing keys:
            {"server": Optional[str], "geo_locale": Optional[str]}
        """
        meta = self.get_meta()
        vpn = meta.get("vpn") if isinstance(meta, dict) else None
        merged = default_account_vpn()
        if isinstance(vpn, dict):
            merged.update({k: vpn.get(k, v) for k, v in merged.items()})
        return merged

    def set_vpn_config(self, vpn):
        """Persist this account's VPN config (missing keys default to None)."""
        merged = default_account_vpn()
        if isinstance(vpn, dict):
            merged.update({k: vpn.get(k, v) for k, v in merged.items()})
        meta = self.get_meta()
        meta["vpn"] = merged
        self.save_meta(meta)

    def get_vpn_server(self):
        return self.get_vpn_config().get("server")

    def set_vpn_server(self, server):
        cfg = self.get_vpn_config()
        cfg["server"] = server
        self.set_vpn_config(cfg)

    def get_geo_locale(self):
        return self.get_vpn_config().get("geo_locale")

    def set_geo_locale(self, geo_locale):
        cfg = self.get_vpn_config()
        cfg["geo_locale"] = geo_locale
        self.set_vpn_config(cfg)

    # ───────────────────────────────────────────────────────────────────

    def get_dashboard_variant(self):
        variant = self.get_meta().get("dashboard_variant")
        if variant in DASHBOARD_VARIANTS:
            return variant
        return DEFAULT_DASHBOARD_VARIANT

    def set_dashboard_variant(self, variant):
        if variant not in DASHBOARD_VARIANTS:
            return False
        meta = self.get_meta()
        meta["dashboard_variant"] = variant
        self.save_meta(meta)
        return True