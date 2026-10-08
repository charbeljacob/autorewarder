"""
Configuration module for AutoRewarder.

This module defines constants and paths used throughout the AutoRewarder
application, such as version information, repository details, platform-
specific directories, and helpers to resolve per-account file paths (Edge
profile, history, status, meta).

Storage layout
--------------
User data lives under ``<project>/userdata/`` by default — next to the code
rather than in an OS-specific location (``~/.local/share`` / ``%LOCALAPPDATA%``).
This makes per-VM setup trivial: `git pull` refreshes the code while the
userdata folder stays put. Add ``userdata/`` to ``.gitignore``.

    <project>/
      AutoRewarder.py
      src/
      userdata/                    <-- APP_DIR
        settings.json
        accounts.json
        accounts/
          <account_id>/
            EdgeProfile/           (Selenium --user-data-dir, ~100-500 MB)
            meta.json
            history.json
            status.json
            stats.json
        background_log.txt

Frozen builds (PyInstaller) still honor an explicit ``config/`` folder next
to the .exe, matching the original behavior for shipped binaries.
"""

import os
import platform
import sys

CURRENT_VERSION = "v4.3"
REPO = "safarsin/AutoRewarder"

PLATFORM_NAME = platform.system()

# ---------------------------------------------------------------------------
# Base paths (computed FIRST — APP_DIR depends on BASE_DIR)
# ---------------------------------------------------------------------------

# src/config.py -> dirname = src/ -> dirname = project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI_DIR = os.path.join(BASE_DIR, "gui")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
VISUAL_SEARCH_ASSETS_DIR = os.path.join(ASSETS_DIR, "visual_search_assets")

# ---------------------------------------------------------------------------
# APP_DIR (user data root)
# ---------------------------------------------------------------------------

# Portable default: everything the app writes goes into ./userdata/ next to
# the code. On a fresh VM you `git pull` the code, restore or let the app
# create the userdata folder, and you're done — no OS-specific paths to
# remember.
APP_DIR = os.path.join(BASE_DIR, "userdata")

# Frozen (PyInstaller) builds: honor an explicit `config/` folder next to the
# .exe if one exists. This preserves the portable-exe behavior from the
# original layout, so shipped binaries still drop their data next to
# themselves if the user opts in by creating the folder.
if getattr(sys, "frozen", False):
    portable_app_dir = os.path.join(os.path.dirname(sys.executable), "config")
    if os.path.isdir(portable_app_dir):
        APP_DIR = portable_app_dir

# Create APP_DIR on import so downstream code (log writer, settings manager,
# account manager) can assume the folder exists.
if not os.path.exists(APP_DIR):
    os.makedirs(APP_DIR)

# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------
#   APP_DIR/
#     settings.json          (global: hide_browser, current_account_id,
#                             autoStartUp, LLM config, schema_version)
#     accounts.json          (index: [{id, label, created_at}])
#     accounts/
#       <account_id>/
#         EdgeProfile/
#         history.json
#         status.json
#         stats.json         (per-account: points balance + activity counters)
#         meta.json          (per-account: first_setup_done, schedule, vpn,
#                             dashboard_variant)
ACCOUNTS_DIR = os.path.join(APP_DIR, "accounts")
GLOBAL_SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
ACCOUNTS_INDEX_PATH = os.path.join(APP_DIR, "accounts.json")
JSON_FILE_PATH = os.path.join(ASSETS_DIR, "queries.json")

# Legacy single-account paths. Only used for one-shot migration detection:
# if these exist at APP_DIR root and no accounts.json is present, the
# AccountManager.migrate_legacy() move them into a fresh "Default" account.
LEGACY_EDGE_PROFILE_PATH = os.path.join(APP_DIR, "EdgeProfile")
LEGACY_HISTORY_FILE_PATH = os.path.join(APP_DIR, "history.json")
LEGACY_STATUS_FILE_PATH = os.path.join(APP_DIR, "status.json")

# Rotating background log written by AutoRewarder_CLI.py (headless mode).
LOG_FILE_PATH = os.path.join(APP_DIR, "background_log.txt")
# Size threshold in bytes; when exceeded, the log file is deleted and recreated.
LOG_MAX_SIZE = 6 * 1024 * 1024  # 6 MB


# ---------------------------------------------------------------------------
# Per-account path helpers
# ---------------------------------------------------------------------------

def account_dir(account_id):
    """Return the directory holding all files for a given account."""
    return os.path.join(ACCOUNTS_DIR, account_id)


def edge_profile_path(account_id):
    """Return the Selenium --user-data-dir path for a given account."""
    return os.path.join(account_dir(account_id), "EdgeProfile")


def history_path(account_id):
    """Return the history.json path for a given account."""
    return os.path.join(account_dir(account_id), "history.json")


def status_path(account_id):
    """Return the daily-set status.json path for a given account."""
    return os.path.join(account_dir(account_id), "status.json")


def stats_path(account_id):
    """Return the statistics stats.json path for a given account."""
    return os.path.join(account_dir(account_id), "stats.json")


def account_meta_path(account_id):
    """Return the per-account meta.json path (stores first_setup_done)."""
    return os.path.join(account_dir(account_id), "meta.json")
