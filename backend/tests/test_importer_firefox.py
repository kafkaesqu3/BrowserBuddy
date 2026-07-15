import sqlite3

import pytest

from browserbuddy.importer.firefox import find_default_profile, import_places


def make_places(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
        CREATE TABLE moz_historyvisits (
            id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER
        );
        """
    )
    for i, (url, title, visit_date_us) in enumerate(rows, start=1):
        conn.execute("INSERT INTO moz_places VALUES (?, ?, ?)", (i, url, title))
        conn.execute(
            "INSERT INTO moz_historyvisits VALUES (?, ?, ?)", (i, i, visit_date_us)
        )
    conn.commit()
    conn.close()


def test_import_places(tmp_path, storage):
    places = tmp_path / "places.sqlite"
    # visit_date is in MICROseconds in Firefox; importer must convert to ms.
    make_places(
        places,
        [
            ("https://arxiv.org/abs/3", "Attention is all you need", 1720000000000000),
            ("https://example.com", None, 1720000001000000),  # NULL title
        ],
    )
    count = import_places(places, storage)
    assert count == 2
    results = storage.search_history("attention")
    assert results[0]["visit_time"] == 1720000000000  # µs -> ms


def test_import_is_idempotent(tmp_path, storage):
    places = tmp_path / "places.sqlite"
    make_places(places, [("https://a.com", "A", 1720000000000000)])
    assert import_places(places, storage) == 1
    assert import_places(places, storage) == 0


def test_import_skips_corrupt_rows(tmp_path, storage):
    places = tmp_path / "places.sqlite"
    make_places(
        places,
        [
            ("https://good.com", "Good", 1720000000000000),
            (None, "No URL", 1720000001000000),  # NULL url
            ("https://no-date.com", "No date", None),  # NULL visit_date
        ],
    )
    assert import_places(places, storage) == 1
    assert len(storage.search_history("good")) == 1


def test_find_default_profile(tmp_path):
    (tmp_path / "profiles.ini").write_text(
        "[Install0123ABCD]\n"
        "Default=Profiles/xyz.default-release\n"
        "\n"
        "[Profile0]\n"
        "Name=default-release\n"
        "IsRelative=1\n"
        "Path=Profiles/xyz.default-release\n"
    )
    (tmp_path / "Profiles" / "xyz.default-release").mkdir(parents=True)
    assert find_default_profile(tmp_path) == tmp_path / "Profiles" / "xyz.default-release"


def test_find_default_profile_absolute_path(tmp_path):
    profile_dir = tmp_path / "elsewhere" / "abs.profile"
    profile_dir.mkdir(parents=True)
    root = tmp_path / "firefox"
    root.mkdir()
    (root / "profiles.ini").write_text(
        "[Profile0]\n"
        "Name=abs\n"
        "IsRelative=0\n"
        f"Path={profile_dir}\n"
        "Default=1\n"
    )
    assert find_default_profile(root) == profile_dir


def test_find_default_profile_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_default_profile(tmp_path)
