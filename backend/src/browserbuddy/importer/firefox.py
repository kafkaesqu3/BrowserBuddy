"""Firefox history backfill: read a copy of places.sqlite into BrowserBuddy.

This is the Firefox implementation of the pluggable per-browser importer
seam described in the design spec; a Chrome importer would expose the same
two functions against Chrome's History database.
"""

import configparser
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from browserbuddy.models import HistoryVisit
from browserbuddy.storage import Storage

BATCH_SIZE = 1000


def firefox_root() -> Path:
    """Platform-default Firefox data directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


def find_default_profile(root: Path) -> Path:
    """Locate the default profile directory via profiles.ini."""
    ini_path = root / "profiles.ini"
    if not ini_path.exists():
        raise FileNotFoundError(
            f"No profiles.ini in {root} — is Firefox installed? Pass --profile explicitly."
        )
    ini = configparser.ConfigParser()
    ini.read(ini_path)

    candidates: list[str] = []
    for section in ini.sections():
        if section.startswith("Install") and ini[section].get("Default"):
            candidates.append(ini[section]["Default"])
    for section in ini.sections():
        if section.startswith("Profile") and ini[section].get("Default") == "1":
            candidates.append(ini[section]["Path"])

    for rel in candidates:
        profile = root / rel
        if profile.is_dir():
            return profile
    raise FileNotFoundError(
        f"profiles.ini in {root} lists no existing default profile. Pass --profile."
    )


def import_places(places_path: Path, storage: Storage) -> int:
    """Import all visits from a places.sqlite file. Returns rows inserted.

    Works on a temp copy so a running Firefox (which locks the DB) is never
    touched. Firefox stores visit_date in microseconds; we store milliseconds.
    """
    if not places_path.exists():
        raise FileNotFoundError(f"No such file: {places_path}")

    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "places.sqlite"
        shutil.copy2(places_path, copy)
        wal = places_path.with_name(places_path.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, copy.with_name(copy.name + "-wal"))

        conn = sqlite3.connect(copy)
        try:
            cursor = conn.execute(
                """
                SELECT p.url, IFNULL(p.title, ''), v.visit_date / 1000
                FROM moz_historyvisits v
                JOIN moz_places p ON p.id = v.place_id
                ORDER BY v.visit_date
                """
            )
            while batch := cursor.fetchmany(BATCH_SIZE):
                visits = [
                    HistoryVisit(url=url, title=title, visit_time=visit_ms)
                    for url, title, visit_ms in batch
                ]
                total += storage.insert_history_visits(visits, source="import")
        finally:
            conn.close()
    return total
