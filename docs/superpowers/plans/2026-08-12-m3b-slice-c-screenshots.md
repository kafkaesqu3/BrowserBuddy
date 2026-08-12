# BrowserBuddy M3b Slice C (Screenshots) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dwell-gated periodic viewport screenshots plus on-demand full-page capture (scroll-and-stitch), backed by a new generic REST blob-upload endpoint and backend-side perceptual-hash dedup — closing the last M3b capture gap and finally giving the long-deferred "move `gzip.compress` off the event loop" item a reason to happen.

**Architecture:** Binary bytes travel out-of-band via a new `POST /api/blobs` (content-in/key-out, same shape as the existing HTML blob store) while a new additive WS event (`page_screenshot`) carries only the resulting key(s) plus page/kind metadata — mirroring how `page_capture`'s inline `html` field already works. Scheduling is a single active-tab-gated content-script tick loop (started/stopped by the background on focus change, not a per-tab timer and not `browser.alarms`) so cost stays O(1) regardless of how many tabs are open, while still supporting sub-minute configured intervals and surviving MV3 worker suspension via message-wake. Backend-side, a dependency-free 64-bit difference-hash (Pillow for decode/resize) drives near-duplicate rejection with a tunable threshold; full-page tiles are stitched server-side before hashing. No durable retry-buffer for failed uploads — best-effort, per the "backend assumed reachable" scope decision.

**Tech Stack:** existing stacks — Python (FastAPI, Pydantic, SQLite, Typer, pytest) plus a new Pillow dependency; TypeScript (Vitest, happy-dom, Vite). No new WebExtension permissions (`tabs` + the already-requested `<all_urls>` optional host permission cover `captureVisibleTab`).

**Spec:** `docs/superpowers/specs/2026-08-12-m3b-slice-c-screenshots-design.md`

## Global Constraints

- Ms epoch UTC timestamps throughout (existing convention).
- Extension imports never use `../`; use `@src/*` in extension tests (existing convention, see `vite.config.ts`'s `@src` alias).
- Backend "no active session" signal is `NoActiveSessionError` (a `LookupError` subclass), caught by the WS loop and turned into an error frame — never let it propagate as a 500 or drop the connection.
- Protocol is additive under v1: older backends error-frame unknown event types; clients skip events the server errors on. Never bump `PROTOCOL_VERSION` for an additive change.
- Backend gate (from `backend/`): `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`.
- Extension gate (from `extension/`): `npm run check` (runs `tsc --noEmit && eslint . && vitest run`).
- Baselines before this plan: backend suite green (schema v5); extension ~80 tests green (per roadmap's Slice B merge note).
- Additive migrations only: **never edit a shipped `MIGRATIONS` entry** — append a new one.
- `Storage` methods that mutate an orphan (page never captured) follow the `mark_link_clicked`/`add_page_dwell` convention: return `False`/`None`, caller/dispatcher decides — not `NoActiveSessionError`. That error is reserved for "no active session at all," checked one layer up in `app.py`.
- **Backend assumed reachable this slice** (scope decision from brainstorming): no durable local retry-buffer for the REST upload step. A failed upload is logged and dropped. Do not add one without a spec update.
- **Scheduling mechanism is settled and non-obvious** — do not "simplify" it back to `browser.alarms` or a single background `setInterval` mid-implementation. Both were evaluated and rejected (see spec §3 rationale): alarms are too coarse (~30s+ floor) for a configurable sub-minute interval; a bare background interval only survives because WS heartbeat traffic happens to prevent worker suspension, which is fragile independent of backend uptime.

> **Env note for agentic workers:** spawned subagents in this environment are often denied Bash for `npm`/`uv`/`git`. If a command is denied, make all file edits, then STOP and report BLOCKED with the exact command — the controller runs gates and commits. `cd` does not persist between commands; use `npm --prefix extension run ...` and `uv run --project backend ...` or a single-line `( cd backend && ... )`.

---

## File structure

```
backend/src/browserbuddy/
  blobs.py       # + compress param on put()/path_for(); get() checks .gz then .bin
  db.py          # + MIGRATIONS[5] (schema v6: screenshots table)
  models.py      # + PageScreenshotEvent; union widened
  config.py      # + screenshot_dedup_threshold
  phash.py       # NEW: difference_hash, hamming_distance, stitch_vertical
  storage.py     # + add_page_screenshot, list_screenshots
  app.py         # + POST /api/blobs; _apply_event becomes async, + page_screenshot dispatch, gzip.compress off-thread
  cli.py         # + `screenshots` command
backend/pyproject.toml  # + pillow dependency
backend/tests/
  test_blobs.py (update), test_db.py (update), test_models.py (update),
  test_config.py (update), test_phash.py (NEW), test_storage_screenshots.py (NEW),
  test_app.py (update), test_cli.py (update)

extension/src/
  types.ts       # + page_screenshot EventPayload arm
  capture.ts     # + pageScreenshotEvent(url, kind, blobKeys, now)
  blobs.ts       # NEW: toHttpUrl, dataUrlToBytes, uploadBlob
  screenshot.ts  # NEW: scrollStepCount, scrollOffsetForStep, intervalElapsed
  sessions.ts    # + screenshotsEnabled/screenshotIntervalMs on SessionState, updateCapture()
  background.ts  # + active-tab tick scheduling, periodic capture, on-demand fullpage, update-capture command
  content.ts     # + tick loop (start/stop on control message), scroll-step listener
  popup/popup.html, popup.ts  # + screenshot toggle/interval controls, capture-now button
extension/tests/
  capture.test.ts (update), blobs.test.ts (NEW), screenshot.test.ts (NEW), sessions.test.ts (update)
```

---

## Task 1: Backend — `BlobStore` skip-compression support

**Files:**
- Modify: `backend/src/browserbuddy/blobs.py`
- Modify: `backend/tests/test_blobs.py`

**Interfaces:**
- Produces: `BlobStore.put(data: bytes, *, compress: bool = True) -> str`, `BlobStore.path_for(key: str, *, compressed: bool = True) -> Path`. `get()` signature unchanged (`get(key: str) -> bytes`) but now checks both suffixes. Later tasks (REST endpoint, WS dispatch) call `put(..., compress=False)` for screenshot bytes.

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_blobs.py`:

```python
def test_put_uncompressed_stores_raw_bytes_with_bin_suffix(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"\xff\xd8\xfake-jpeg-bytes"
    key = store.put(data, compress=False)
    assert key == hashlib.sha256(data).hexdigest()
    path = store.path_for(key, compressed=False)
    assert path.suffix == ".bin"
    assert path.read_bytes() == data  # not gzipped


def test_get_finds_uncompressed_blob(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    data = b"raw screenshot bytes"
    key = store.put(data, compress=False)
    assert store.get(key) == data


def test_path_for_defaults_to_compressed():
    store = BlobStore.__new__(BlobStore)  # no filesystem needed for this check
    store._root = None  # type: ignore[assignment]
    # path_for with no compressed kwarg still resolves the .gz suffix, matching
    # every existing caller (HTML blobs) which never passes the kwarg.
    import pathlib

    store._root = pathlib.Path("/tmp/unused")
    assert store.path_for("abc").suffix == ".gz"
```

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_blobs.py -v`
Expected: FAIL — `put()`/`path_for()` don't accept `compress`/`compressed` kwargs.

- [ ] **Step 3: Replace `backend/src/browserbuddy/blobs.py`**

```python
"""Content-addressed blob storage for raw page HTML and screenshots.

Keys are the SHA-256 of the UNCOMPRESSED content; identical pages/images are
stored once. Text blobs (HTML) are gzipped at rest (~10x). Image blobs
(JPEG screenshots) are already compressed, so gzip would cost CPU for ~0
size benefit — those are stored raw and use a distinct `.bin` suffix so the
two variants for the same key (a caller could theoretically compress the
same bytes once and not another time) never collide on disk. Layout:
<root>/<key[:2]>/<key[2:]>.gz|.bin — two-level fan-out keeps directories small.
"""

import gzip
import hashlib
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str, *, compressed: bool = True) -> Path:
        suffix = ".gz" if compressed else ".bin"
        return self._root / key[:2] / f"{key[2:]}{suffix}"

    def put(self, data: bytes, *, compress: bool = True) -> str:
        """Store data, returning its content key. No-op if already stored."""
        key = hashlib.sha256(data).hexdigest()
        path = self.path_for(key, compressed=compress)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(gzip.compress(data) if compress else data)
            tmp.replace(path)  # atomic: readers never see partial writes
        return key

    def get(self, key: str) -> bytes:
        gz_path = self.path_for(key, compressed=True)
        if gz_path.exists():
            return gzip.decompress(gz_path.read_bytes())
        return self.path_for(key, compressed=False).read_bytes()
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_blobs.py -v`: PASS (all, including the pre-existing `test_blobs_are_gzipped_at_rest`/`test_get_missing_raises`, unaffected by the default `compressed=True`). `uv run ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/blobs.py backend/tests/test_blobs.py
git commit -m "Add BlobStore skip-compression support for image blobs"
```

---

## Task 2: Backend — schema v6 (`screenshots` table)

**Files:**
- Modify: `backend/src/browserbuddy/db.py`
- Modify: `backend/tests/test_db.py`

**Interfaces:**
- Produces: `screenshots(id, page_visit_id, blob_key, captured_at, kind, phash)` + `idx_screenshots_page_visit` — later tasks (`add_page_screenshot`, `list_screenshots`) read/write this table.

- [ ] **Step 1: Write failing migration test** — append to `backend/tests/test_db.py`:

```python
def test_v6_migration_adds_screenshots_table_and_keeps_data(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:5])
    conn = db.connect(path)  # v1-v5 only
    conn.execute("INSERT INTO sessions (client_id, name, started_at) VALUES ('c', 's', 1)")
    conn.execute(
        "INSERT INTO page_visits (session_id, url, visited_at) VALUES (1, 'https://a.com', 10)"
    )
    conn.commit()
    conn.close()
    monkeypatch.undo()
    conn = db.connect(path)  # applies v6 (and any later migrations) on top of v1-v5 data
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 6
    conn.execute(
        "INSERT INTO screenshots (page_visit_id, blob_key, captured_at, kind, phash)"
        " VALUES (1, 'k', 100, 'periodic', '0' * 16)"
    )
    row = conn.execute("SELECT url FROM page_visits WHERE id = 1").fetchone()
    assert row["url"] == "https://a.com"  # v1-v5 data intact
    conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_db.py -v -k v6`
Expected: FAIL — `MAX(version)` is 5; `no such table: screenshots`.

- [ ] **Step 3: Append migration v6 to `MIGRATIONS` in `backend/src/browserbuddy/db.py`** (add a new string element after the v5 script; do not edit v1-v5):

```python
    # v6: screenshots (Slice C). One row per stored capture — near-duplicates
    # (Hamming distance <= a tunable threshold) are rejected before insert,
    # not stored and pruned, so this table only ever holds screenshots the
    # dedup pass judged distinct. blob_key points at the (possibly stitched,
    # for on_demand_fullpage) final image in the blob store.
    """
    CREATE TABLE screenshots (
        id INTEGER PRIMARY KEY,
        page_visit_id INTEGER NOT NULL REFERENCES page_visits (id),
        blob_key TEXT NOT NULL,
        captured_at INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('periodic', 'on_demand_viewport', 'on_demand_fullpage')),
        phash TEXT NOT NULL
    );

    CREATE INDEX idx_screenshots_page_visit ON screenshots (page_visit_id);
    """,
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_db.py -v -k v6`: PASS. Full file green.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/db.py backend/tests/test_db.py
git commit -m "Add schema v6 with the screenshots table"
```

---

## Task 3: Backend — `PageScreenshotEvent` protocol model

**Files:**
- Modify: `backend/src/browserbuddy/models.py`
- Modify: `backend/tests/test_models.py`

**Interfaces:**
- Produces: `PageScreenshotEvent { type: "page_screenshot", url: str, occurred_at: int (ge=0), kind: Literal["periodic","on_demand_viewport","on_demand_fullpage"], blob_keys: list[str] (min_length=1) }` — Task 8 (`app.py`) dispatches on this type.

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_models.py`:

```python
def test_page_screenshot_validates():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 1,
            "event": {
                "type": "page_screenshot",
                "url": "https://a.com",
                "occurred_at": 9,
                "kind": "periodic",
                "blob_keys": ["abc123"],
            },
        }
    )
    assert env.event.type == "page_screenshot"
    assert env.event.blob_keys == ["abc123"]


def test_page_screenshot_carries_multiple_tile_keys():
    env = EventEnvelope.model_validate(
        {
            "v": 1,
            "seq": 1,
            "event": {
                "type": "page_screenshot",
                "url": "https://a.com",
                "occurred_at": 9,
                "kind": "on_demand_fullpage",
                "blob_keys": ["k1", "k2", "k3"],
            },
        }
    )
    assert env.event.blob_keys == ["k1", "k2", "k3"]


def test_page_screenshot_rejects_empty_blob_keys():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {
                "v": 1,
                "seq": 1,
                "event": {
                    "type": "page_screenshot",
                    "url": "https://a.com",
                    "occurred_at": 9,
                    "kind": "periodic",
                    "blob_keys": [],
                },
            }
        )


def test_page_screenshot_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {
                "v": 1,
                "seq": 1,
                "event": {
                    "type": "page_screenshot",
                    "url": "https://a.com",
                    "occurred_at": 9,
                    "kind": "bogus",
                    "blob_keys": ["k1"],
                },
            }
        )
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_models.py -v -k page_screenshot`: FAIL (unknown event type `page_screenshot`).

- [ ] **Step 3: Edit `backend/src/browserbuddy/models.py`**

Add the new event (after `PageDwellEvent`):

```python
class PageScreenshotEvent(ProtocolModel):
    type: Literal["page_screenshot"]
    url: str = Field(min_length=1)
    occurred_at: int = Field(ge=0)
    kind: Literal["periodic", "on_demand_viewport", "on_demand_fullpage"]
    blob_keys: list[str] = Field(min_length=1)
```

Widen the envelope union:

```python
    event: (
        TabStateEvent
        | HistoryVisitsEvent
        | SessionStartEvent
        | SessionEndEvent
        | PageCaptureEvent
        | LinkClickEvent
        | NavEdgeEvent
        | PageDwellEvent
        | PageScreenshotEvent
    ) = Field(discriminator="type")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_models.py -q`: PASS. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/models.py backend/tests/test_models.py
git commit -m "Add PageScreenshotEvent protocol model"
```

---

## Task 4: Backend — `phash.py` (difference-hash, Hamming distance, tile stitching)

**Files:**
- Modify: `backend/pyproject.toml`
- Create: `backend/src/browserbuddy/phash.py`
- Create: `backend/tests/test_phash.py`

**Interfaces:**
- Produces: `difference_hash(image_bytes: bytes) -> str` (16-char hex, 64 bits), `hamming_distance(a: str, b: str) -> int`, `stitch_vertical(tiles: list[bytes]) -> bytes` — Task 6 (`storage.add_page_screenshot`) calls `hamming_distance`; Task 8 (`app.py` dispatch) calls `difference_hash` and `stitch_vertical`.

- [ ] **Step 1: Add the Pillow dependency** — edit `backend/pyproject.toml`, change:

```toml
dependencies = [
    "fastapi>=0.139.0",
    "pydantic-settings>=2.14.2",
    "typer>=0.26.8",
    "uvicorn[standard]>=0.51.0",
]
```

to:

```toml
dependencies = [
    "fastapi>=0.139.0",
    "pillow>=11.0.0",
    "pydantic-settings>=2.14.2",
    "typer>=0.26.8",
    "uvicorn[standard]>=0.51.0",
]
```

Run (from `backend/`): `uv sync`. Expected: Pillow installed into `.venv`.

- [ ] **Step 2: Write failing tests** — create `backend/tests/test_phash.py`:

```python
import io

from PIL import Image

from browserbuddy.phash import difference_hash, hamming_distance, stitch_vertical


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _gradient_jpeg(ascending: bool, width: int = 64, height: int = 64) -> bytes:
    """A smooth left-to-right gradient. Ascending (dark->light) and
    descending (light->dark) versions are near-maximally different under a
    difference-hash: every adjacent-pixel comparison flips sign."""
    img = Image.new("L", (width, height))
    for x in range(width):
        value = int(255 * x / (width - 1))
        for y in range(height):
            img.putpixel((x, y), value if ascending else 255 - value)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_difference_hash_is_64_bit_hex():
    h = difference_hash(_solid((100, 100, 100)))
    assert len(h) == 16
    int(h, 16)  # raises if not valid hex


def test_identical_image_has_zero_distance():
    a = _gradient_jpeg(ascending=True)
    assert hamming_distance(difference_hash(a), difference_hash(a)) == 0


def test_near_identical_gradients_are_near_duplicates():
    a = _gradient_jpeg(ascending=True, width=64)
    b = _gradient_jpeg(ascending=True, width=65)  # tiny resize difference only
    assert hamming_distance(difference_hash(a), difference_hash(b)) <= 4


def test_inverted_gradient_is_far_from_original():
    a = _gradient_jpeg(ascending=True)
    b = _gradient_jpeg(ascending=False)
    assert hamming_distance(difference_hash(a), difference_hash(b)) >= 40


def test_hamming_distance_counts_differing_bits():
    assert hamming_distance("0000000000000000", "0000000000000001") == 1
    assert hamming_distance("ffffffffffffffff", "0000000000000000") == 64


def test_stitch_vertical_stacks_tiles_top_to_bottom():
    tile1 = _solid((10, 10, 10), size=(20, 10))
    tile2 = _solid((20, 20, 20), size=(20, 15))
    stitched = stitch_vertical([tile1, tile2])
    img = Image.open(io.BytesIO(stitched))
    assert img.size == (20, 25)  # width unchanged, heights summed


def test_stitch_vertical_single_tile_preserves_dimensions():
    tile = _solid((1, 2, 3), size=(30, 40))
    stitched = stitch_vertical([tile])
    img = Image.open(io.BytesIO(stitched))
    assert img.size == (30, 40)
```

- [ ] **Step 3: Run to verify failure** — `uv run pytest tests/test_phash.py -v`: FAIL (module missing).

- [ ] **Step 4: Create `backend/src/browserbuddy/phash.py`**

```python
"""Perceptual hashing and tile stitching for screenshot dedup (Slice C).

Backend-side by design (parent spec's portability policy: no DOM or compute
in the extension's background — "screenshot hashing/dedup happens in the
backend... the extension ships raw JPEGs over localhost").
"""

import io

from PIL import Image

HASH_HEX_CHARS = 16  # 64-bit difference hash, as hex


def difference_hash(image_bytes: bytes) -> str:
    """8x8 grayscale difference hash (dHash): resize to 9x8, compare each
    row's adjacent pixels left-to-right. Robust to JPEG re-encoding and tiny
    resize differences; a shift in overall brightness or color doesn't
    change it, only the pattern of local light/dark transitions does."""
    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((9, 8))
    pixels = list(img.getdata())
    value = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            bit = pixels[offset + col] > pixels[offset + col + 1]
            value = (value << 1) | int(bit)
    return f"{value:0{HASH_HEX_CHARS}x}"


def hamming_distance(a: str, b: str) -> int:
    """Count of differing bits between two hex-encoded hashes."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def stitch_vertical(tiles: list[bytes]) -> bytes:
    """Stack ordered viewport tiles top-to-bottom into one JPEG. Used for
    on_demand_fullpage captures; a single-tile list is a dimension-preserving
    passthrough (re-encoded, not byte-identical)."""
    images = [Image.open(io.BytesIO(t)).convert("RGB") for t in tiles]
    width = max(img.width for img in images)
    total_height = sum(img.height for img in images)
    canvas = Image.new("RGB", (width, total_height))
    y = 0
    for img in images:
        canvas.paste(img, (0, y))
        y += img.height
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
```

- [ ] **Step 5: Run** — `uv run pytest tests/test_phash.py -v`: PASS. `uv run ruff check .` clean.

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/src/browserbuddy/phash.py backend/tests/test_phash.py
git commit -m "Add Pillow-based difference-hash, Hamming distance, and tile stitching"
```

---

## Task 5: Backend — tunable dedup threshold config

**Files:**
- Modify: `backend/src/browserbuddy/config.py`
- Modify: `backend/tests/test_config.py`

**Interfaces:**
- Produces: `Settings.screenshot_dedup_threshold: int` (default 8), overridable via `BROWSERBUDDY_SCREENSHOT_DEDUP_THRESHOLD` — Task 8 (`app.py` dispatch) reads this.

- [ ] **Step 1: Write failing test** — append to `backend/tests/test_config.py`:

```python
def test_screenshot_dedup_threshold_default_and_override(monkeypatch):
    assert Settings().screenshot_dedup_threshold == 8
    monkeypatch.setenv("BROWSERBUDDY_SCREENSHOT_DEDUP_THRESHOLD", "3")
    assert Settings().screenshot_dedup_threshold == 3
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_config.py -v -k dedup`: FAIL (`AttributeError`).

- [ ] **Step 3: Edit `backend/src/browserbuddy/config.py`** — add the field to `Settings`:

```python
    data_dir: Path = Path.home() / ".browserbuddy"
    host: str = "127.0.0.1"
    port: int = 8765
    # Max Hamming distance (of 64 bits) between a new screenshot's
    # difference-hash and its page_visit's most recent one to be treated as
    # a duplicate and discarded. 0 disables dedup; 64 keeps only the first
    # screenshot ever taken per page.
    screenshot_dedup_threshold: int = 8
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_config.py -q`: PASS. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/config.py backend/tests/test_config.py
git commit -m "Add tunable screenshot dedup threshold setting"
```

---

## Task 6: Backend — `add_page_screenshot` / `list_screenshots` storage methods

**Files:**
- Modify: `backend/src/browserbuddy/storage.py`
- Create: `backend/tests/test_storage_screenshots.py`

**Interfaces:**
- Consumes: `screenshots` table (Task 2), `hamming_distance` (Task 4).
- Produces: `Storage.add_page_screenshot(session_id, *, url, occurred_at, kind, blob_key, phash, dedup_threshold) -> int | None`, `Storage.list_screenshots(session_id, url=None) -> list[Row]` — Task 8 (`app.py`) and Task 9 (CLI) call these.

- [ ] **Step 1: Write failing tests** — create `backend/tests/test_storage_screenshots.py`:

```python
def _page(storage):
    sid = storage.start_session("ext-1", "s", started_at=1000)
    pid = storage.add_page_visit(
        sid, url="https://a.com", title="A", visited_at=1100, text="", html_key=None, container=None
    )
    return sid, pid


def test_add_page_screenshot_inserts_and_returns_id(storage):
    sid, pid = _page(storage)
    new_id = storage.add_page_screenshot(
        sid,
        url="https://a.com",
        occurred_at=1200,
        kind="periodic",
        blob_key="key1",
        phash="0000000000000000",
        dedup_threshold=8,
    )
    assert isinstance(new_id, int)
    row = storage._conn.execute(
        "SELECT page_visit_id, blob_key, kind, phash FROM screenshots WHERE id = ?", (new_id,)
    ).fetchone()
    assert row["page_visit_id"] == pid
    assert row["blob_key"] == "key1"
    assert row["kind"] == "periodic"


def test_add_page_screenshot_dedupes_near_identical_capture(storage):
    sid, _ = _page(storage)
    storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1200, kind="periodic",
        blob_key="key1", phash="0000000000000000", dedup_threshold=8,
    )
    second = storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1300, kind="periodic",
        blob_key="key2", phash="0000000000000003", dedup_threshold=8,  # distance 2, <= 8
    )
    assert second is None
    count = storage._conn.execute("SELECT COUNT(*) AS c FROM screenshots").fetchone()["c"]
    assert count == 1


def test_add_page_screenshot_keeps_capture_beyond_threshold(storage):
    sid, _ = _page(storage)
    storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1200, kind="periodic",
        blob_key="key1", phash="0000000000000000", dedup_threshold=8,
    )
    second = storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1300, kind="periodic",
        blob_key="key2", phash="ffffffffffffffff", dedup_threshold=8,  # distance 64
    )
    assert isinstance(second, int)
    count = storage._conn.execute("SELECT COUNT(*) AS c FROM screenshots").fetchone()["c"]
    assert count == 2


def test_add_page_screenshot_orphan_returns_none(storage):
    sid, _ = _page(storage)
    result = storage.add_page_screenshot(
        sid, url="https://never-seen.com", occurred_at=1, kind="periodic",
        blob_key="k", phash="0" * 16, dedup_threshold=8,
    )
    assert result is None


def test_list_screenshots_filters_by_url(storage):
    sid, _ = _page(storage)
    storage.add_page_visit(
        sid, url="https://b.com", title="B", visited_at=1150, text="", html_key=None, container=None
    )
    storage.add_page_screenshot(
        sid, url="https://a.com", occurred_at=1200, kind="periodic",
        blob_key="ka", phash="0" * 16, dedup_threshold=8,
    )
    storage.add_page_screenshot(
        sid, url="https://b.com", occurred_at=1250, kind="periodic",
        blob_key="kb", phash="f" * 16, dedup_threshold=8,
    )
    all_rows = storage.list_screenshots(sid)
    assert len(all_rows) == 2
    a_rows = storage.list_screenshots(sid, url="https://a.com")
    assert [r["blob_key"] for r in a_rows] == ["ka"]
```

- [ ] **Step 2: Run to verify failure**

Run (from `backend/`): `uv run pytest tests/test_storage_screenshots.py -v`
Expected: FAIL — `add_page_screenshot`/`list_screenshots` missing.

- [ ] **Step 3: Append to the `Storage` class in `backend/src/browserbuddy/storage.py`** (after `add_page_dwell`, at the end of the file). First add the import at the top of the file:

```python
from browserbuddy.models import HistoryVisit, OutboundLink, TabRecord
```

becomes:

```python
from browserbuddy.models import HistoryVisit, OutboundLink, TabRecord
from browserbuddy.phash import hamming_distance
```

Then append the two methods:

```python
    def add_page_screenshot(
        self,
        session_id: int,
        *,
        url: str,
        occurred_at: int,
        kind: str,
        blob_key: str,
        phash: str,
        dedup_threshold: int,
    ) -> int | None:
        """Insert a screenshots row for the latest page_visit at url in this
        session, unless it's a near-duplicate (Hamming distance <=
        dedup_threshold) of that page_visit's most recently stored
        screenshot. Returns the new row id, or None when the page was never
        captured (orphan, same convention as mark_link_clicked/
        add_page_dwell) or the capture was deduped away.
        """
        row = self._conn.execute(
            "SELECT id FROM page_visits WHERE session_id = ? AND url = ?"
            " ORDER BY visited_at DESC LIMIT 1",
            (session_id, url),
        ).fetchone()
        if row is None:
            return None
        page_visit_id = row["id"]
        latest = self._conn.execute(
            "SELECT phash FROM screenshots WHERE page_visit_id = ?"
            " ORDER BY captured_at DESC, id DESC LIMIT 1",
            (page_visit_id,),
        ).fetchone()
        if latest is not None and hamming_distance(latest["phash"], phash) <= dedup_threshold:
            return None
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO screenshots (page_visit_id, blob_key, captured_at, kind, phash)"
                " VALUES (?, ?, ?, ?, ?)",
                (page_visit_id, blob_key, occurred_at, kind, phash),
            )
        return cur.lastrowid

    def list_screenshots(self, session_id: int, url: str | None = None) -> list[sqlite3.Row]:
        query = (
            "SELECT sc.id, sc.blob_key, sc.captured_at, sc.kind, p.url"
            " FROM screenshots sc JOIN page_visits p ON p.id = sc.page_visit_id"
            " WHERE p.session_id = ?"
        )
        params: list[int | str] = [session_id]
        if url is not None:
            query += " AND p.url = ?"
            params.append(url)
        query += " ORDER BY sc.captured_at, sc.id"
        return self._conn.execute(query, params).fetchall()
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_storage_screenshots.py -q`: PASS. `uv run pytest -q` (full backend suite): PASS. `uv run ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/storage.py backend/tests/test_storage_screenshots.py
git commit -m "Add add_page_screenshot and list_screenshots storage methods"
```

---

## Task 7: Backend — `POST /api/blobs` REST endpoint

**Files:**
- Modify: `backend/src/browserbuddy/app.py`
- Modify: `backend/tests/test_app.py`

**Interfaces:**
- Consumes: `BlobStore.put(data, compress=False)` (Task 1), `verify_secret` (existing `auth.py`).
- Produces: `POST /api/blobs` — header `X-BB-Secret`, raw-bytes body, returns `{"key": "<sha256 hex>"}`. Task 15 (`background.ts`'s `blobs.ts` client) calls this.

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_app.py`:

```python
def test_upload_blob_returns_content_key(client, app):
    resp = client.post("/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=b"hello")
    assert resp.status_code == 200
    import hashlib

    assert resp.json() == {"key": hashlib.sha256(b"hello").hexdigest()}


def test_upload_blob_rejects_bad_secret(client):
    resp = client.post("/api/blobs", headers={"X-BB-Secret": "wrong"}, content=b"hello")
    assert resp.status_code == 401


def test_upload_blob_rejects_missing_secret(client):
    resp = client.post("/api/blobs", content=b"hello")
    assert resp.status_code == 401


def test_upload_blob_rejects_oversized_body(client, app):
    resp = client.post(
        "/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=b"x" * (10_000_001)
    )
    assert resp.status_code == 413


def test_upload_blob_is_idempotent_and_uncompressed(client, app, tmp_path):
    key1 = client.post(
        "/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=b"same bytes"
    ).json()["key"]
    key2 = client.post(
        "/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=b"same bytes"
    ).json()["key"]
    assert key1 == key2
    assert app.state.blobs.get(key1) == b"same bytes"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_app.py -v -k upload_blob`: FAIL (404, route doesn't exist).

- [ ] **Step 3: Edit `backend/src/browserbuddy/app.py`**

Change the imports at the top:

```python
import logging
import sqlite3

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
```

to:

```python
import asyncio
import logging
import sqlite3

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
```

Add the endpoint in `create_app`, right after the `/health` route and before `/ws`:

```python
    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "protocol": PROTOCOL_VERSION}

    MAX_BLOB_BYTES = 10_000_000

    @app.post("/api/blobs")
    async def upload_blob(request: Request) -> dict[str, str]:
        """Generic content-addressed blob upload — bytes in, key out, no
        session/tab/URL context (that travels separately over WS; see
        PageScreenshotEvent). Auth mirrors the WS handshake's shared secret,
        carried as a header since this is a stateless one-shot request."""
        if not verify_secret(app.state.secret, request.headers.get("X-BB-Secret", "")):
            raise HTTPException(status_code=401, detail="bad secret")
        body = await request.body()
        if len(body) > MAX_BLOB_BYTES:
            raise HTTPException(status_code=413, detail="blob too large")
        key = await asyncio.to_thread(app.state.blobs.put, body, compress=False)
        return {"key": key}

    @app.websocket("/ws")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_app.py -q`: PASS. `uv run ruff check .` clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/app.py backend/tests/test_app.py
git commit -m "Add POST /api/blobs REST upload endpoint"
```

---

## Task 8: Backend — `page_screenshot` WS dispatch (+ move `gzip.compress` off the event loop)

**Files:**
- Modify: `backend/src/browserbuddy/app.py`
- Modify: `backend/tests/test_app.py`

**Interfaces:**
- Consumes: `PageScreenshotEvent` (Task 3), `Storage.add_page_screenshot` (Task 6), `phash.difference_hash`/`stitch_vertical` (Task 4), `Settings.screenshot_dedup_threshold` (Task 5), `POST /api/blobs` (Task 7, used by tests to seed real blob keys).

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_app.py`:

```python
def _jpeg(color=(10, 20, 30), size=(16, 16), quality=95):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _gradient_jpeg(ascending, width=32, height=32):
    import io

    from PIL import Image

    img = Image.new("L", (width, height))
    for x in range(width):
        value = int(255 * x / (width - 1))
        for y in range(height):
            img.putpixel((x, y), value if ascending else 255 - value)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _start_session_and_capture(client, app, url="https://a.com"):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(_envelope(1, {"type": "session_start", "name": "s", "started_at": 100}))
        ws.receive_json()
        ws.send_json(
            _envelope(2, {"type": "page_capture", "url": url, "visited_at": 150, "text": "b"})
        )
        ws.receive_json()


def test_ws_page_screenshot_viewport_stores_row_and_reuses_key(client, app):
    _start_session_and_capture(client, app)
    key = client.post(
        "/api/blobs", headers={"X-BB-Secret": app.state.secret}, content=_jpeg()
    ).json()["key"]
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                3,
                {
                    "type": "page_screenshot",
                    "url": "https://a.com",
                    "occurred_at": 200,
                    "kind": "periodic",
                    "blob_keys": [key],
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 3}
    active = app.state.storage.get_active_session("ext-test")
    rows = app.state.storage.list_screenshots(active["id"])
    assert len(rows) == 1
    assert rows[0]["kind"] == "periodic"
    assert rows[0]["blob_key"] == key  # single tile: no re-upload, key reused


def test_ws_page_screenshot_fullpage_stitches_tiles_into_a_new_blob(client, app):
    _start_session_and_capture(client, app)
    secret = app.state.secret
    key1 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_jpeg((10, 10, 10), (20, 10))
    ).json()["key"]
    key2 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_jpeg((20, 20, 20), (20, 15))
    ).json()["key"]
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                3,
                {
                    "type": "page_screenshot",
                    "url": "https://a.com",
                    "occurred_at": 200,
                    "kind": "on_demand_fullpage",
                    "blob_keys": [key1, key2],
                },
            )
        )
        assert ws.receive_json() == {"type": "ack", "seq": 3}
    active = app.state.storage.get_active_session("ext-test")
    rows = app.state.storage.list_screenshots(active["id"])
    assert len(rows) == 1
    assert rows[0]["blob_key"] not in (key1, key2)  # a new stitched blob was created
    import io

    from PIL import Image

    stitched = app.state.blobs.get(rows[0]["blob_key"])
    assert Image.open(io.BytesIO(stitched)).size == (20, 25)


def test_ws_page_screenshot_dedupes_visually_identical_capture(client, app):
    _start_session_and_capture(client, app)
    secret = app.state.secret
    # Two solid-color images always difference-hash to the same value
    # (every adjacent-pixel comparison is equal->False), regardless of the
    # exact color/quality — different bytes/keys, same perceptual hash.
    key1 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_jpeg((10, 20, 30), quality=95)
    ).json()["key"]
    key2 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_jpeg((11, 21, 29), quality=80)
    ).json()["key"]
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        for seq, key in ((3, key1), (4, key2)):
            ws.send_json(
                _envelope(
                    seq,
                    {
                        "type": "page_screenshot",
                        "url": "https://a.com",
                        "occurred_at": 200 + seq,
                        "kind": "periodic",
                        "blob_keys": [key],
                    },
                )
            )
            assert ws.receive_json() == {"type": "ack", "seq": seq}  # ack either way; dedup isn't an error
    active = app.state.storage.get_active_session("ext-test")
    rows = app.state.storage.list_screenshots(active["id"])
    assert len(rows) == 1  # second capture deduped away


def test_ws_page_screenshot_keeps_visually_distinct_captures(client, app):
    _start_session_and_capture(client, app)
    secret = app.state.secret
    key1 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_gradient_jpeg(True)
    ).json()["key"]
    key2 = client.post(
        "/api/blobs", headers={"X-BB-Secret": secret}, content=_gradient_jpeg(False)
    ).json()["key"]
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        for seq, key in ((3, key1), (4, key2)):
            ws.send_json(
                _envelope(
                    seq,
                    {
                        "type": "page_screenshot",
                        "url": "https://a.com",
                        "occurred_at": 200 + seq,
                        "kind": "periodic",
                        "blob_keys": [key],
                    },
                )
            )
            assert ws.receive_json() == {"type": "ack", "seq": seq}
    active = app.state.storage.get_active_session("ext-test")
    rows = app.state.storage.list_screenshots(active["id"])
    assert len(rows) == 2


def test_ws_page_screenshot_without_session_gets_error_frame(client, app):
    with client.websocket_connect("/ws") as ws:
        _auth(ws, app)
        ws.send_json(
            _envelope(
                1,
                {
                    "type": "page_screenshot",
                    "url": "https://a.com",
                    "occurred_at": 1,
                    "kind": "periodic",
                    "blob_keys": ["deadbeef"],
                },
            )
        )
        assert ws.receive_json()["type"] == "error"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_app.py -v -k page_screenshot`: FAIL (`unhandled event type: PageScreenshotEvent`).

- [ ] **Step 3: Edit `backend/src/browserbuddy/app.py`**

Add the new imports to the models import block:

```python
from browserbuddy.models import (
    PROTOCOL_VERSION,
    AuthMessage,
    EventEnvelope,
    HistoryVisitsEvent,
    LinkClickEvent,
    NavEdgeEvent,
    PageCaptureEvent,
    PageDwellEvent,
    SessionEndEvent,
    SessionStartEvent,
    TabStateEvent,
)
```

to:

```python
from browserbuddy.models import (
    PROTOCOL_VERSION,
    AuthMessage,
    EventEnvelope,
    HistoryVisitsEvent,
    LinkClickEvent,
    NavEdgeEvent,
    PageCaptureEvent,
    PageDwellEvent,
    PageScreenshotEvent,
    SessionEndEvent,
    SessionStartEvent,
    TabStateEvent,
)
```

Add one more import line:

```python
from browserbuddy.storage import NoActiveSessionError, Storage
```

to:

```python
from browserbuddy.phash import difference_hash, stitch_vertical
from browserbuddy.storage import NoActiveSessionError, Storage
```

In `create_app`, store settings on app state — change:

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="BrowserBuddy")
    app.state.storage = Storage(db.connect(settings.db_path))
    app.state.secret = get_or_create_secret(settings.data_dir)
    app.state.blobs = BlobStore(settings.data_dir / "blobs")
```

to:

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="BrowserBuddy")
    app.state.storage = Storage(db.connect(settings.db_path))
    app.state.secret = get_or_create_secret(settings.data_dir)
    app.state.blobs = BlobStore(settings.data_dir / "blobs")
    app.state.settings = settings
```

Change the WS loop's dispatch call — replace:

```python
                if envelope.seq > last_seq:
                    try:
                        _apply_event(storage, app.state.blobs, auth.client_id, envelope)
                        storage.set_last_seq(auth.client_id, envelope.seq)
```

with:

```python
                if envelope.seq > last_seq:
                    try:
                        await _apply_event(
                            storage, app.state.blobs, app.state.settings, auth.client_id, envelope
                        )
                        storage.set_last_seq(auth.client_id, envelope.seq)
```

Replace the entire `_apply_event` function with:

```python
async def _apply_event(
    storage: Storage,
    blobs: BlobStore,
    settings: Settings,
    client_id: str,
    envelope: EventEnvelope,
) -> None:
    event = envelope.event
    if isinstance(event, TabStateEvent):
        storage.replace_open_tabs(event.tabs)
    elif isinstance(event, HistoryVisitsEvent):
        storage.insert_history_visits(event.visits, source="sync")
    elif isinstance(event, SessionStartEvent):
        storage.start_session(client_id, event.name, event.started_at)
    elif isinstance(event, SessionEndEvent):
        storage.end_session(client_id, event.ended_at)  # False = already ended; fine
    elif isinstance(event, PageCaptureEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("page_capture with no active session")
        html_key = (
            await asyncio.to_thread(blobs.put, event.html.encode()) if event.html else None
        )
        page_visit_id = storage.add_page_visit(
            active["id"],
            url=event.url,
            title=event.title,
            visited_at=event.visited_at,
            text=event.text,
            html_key=html_key,
            container=event.container,
        )
        if event.links:
            storage.add_outbound_links(page_visit_id, event.links)
    elif isinstance(event, NavEdgeEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("nav_edge with no active session")
        storage.add_nav_edge(
            active["id"],
            from_url=event.from_url,
            to_url=event.to_url,
            transition=event.transition,
            qualifiers=event.qualifiers,
            query=event.query,
            occurred_at=event.occurred_at,
        )
    elif isinstance(event, LinkClickEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("link_click with no active session")
        storage.mark_link_clicked(
            active["id"], page_url=event.page_url, href=event.href, text=event.text
        )
    elif isinstance(event, PageDwellEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("page_dwell with no active session")
        storage.add_page_dwell(active["id"], url=event.url, dwell_ms=event.dwell_ms)
    elif isinstance(event, PageScreenshotEvent):
        active = storage.get_active_session(client_id)
        if active is None:
            raise NoActiveSessionError("page_screenshot with no active session")
        tiles = [blobs.get(k) for k in event.blob_keys]
        if len(tiles) == 1:
            image_bytes, blob_key = tiles[0], event.blob_keys[0]
        else:
            image_bytes = await asyncio.to_thread(stitch_vertical, tiles)
            blob_key = await asyncio.to_thread(blobs.put, image_bytes, compress=False)
        phash = await asyncio.to_thread(difference_hash, image_bytes)
        storage.add_page_screenshot(
            active["id"],
            url=event.url,
            occurred_at=event.occurred_at,
            kind=event.kind,
            blob_key=blob_key,
            phash=phash,
            dedup_threshold=settings.screenshot_dedup_threshold,
        )
    else:  # pragma: no cover - unreachable until the union grows
        raise AssertionError(f"unhandled event type: {type(event).__name__}")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_app.py -q`: PASS. `uv run pytest -q` (full suite): PASS. `uv run ruff check . && uv run ruff format --check .`: clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/app.py backend/tests/test_app.py
git commit -m "Dispatch page_screenshot ingest; move blob writes and hashing off the event loop"
```

---

## Task 9: Backend — `buddy screenshots` CLI command

**Files:**
- Modify: `backend/src/browserbuddy/cli.py`
- Modify: `backend/tests/test_cli.py`

**Interfaces:**
- Consumes: `Storage.list_screenshots` (Task 6), `_resolve_session` (existing helper).

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_cli.py`:

```python
def _seed_screenshot_session(tmp_path):
    from browserbuddy import db
    from browserbuddy.storage import Storage

    conn = db.connect(tmp_path / "buddy.db")
    s = Storage(conn)
    sid = s.start_session("ext-1", "vector research", started_at=1720000000000)
    s.add_page_visit(
        sid, url="https://arxiv.org/abs/8", title="HNSW explained",
        visited_at=1720000001000, text="", html_key=None, container=None,
    )
    s.add_page_visit(
        sid, url="https://arxiv.org/abs/9", title="Other paper",
        visited_at=1720000002000, text="", html_key=None, container=None,
    )
    s.add_page_screenshot(
        sid, url="https://arxiv.org/abs/8", occurred_at=1720000001500,
        kind="periodic", blob_key="deadbeef", phash="0" * 16, dedup_threshold=8,
    )
    s.add_page_screenshot(
        sid, url="https://arxiv.org/abs/9", occurred_at=1720000002500,
        kind="on_demand_fullpage", blob_key="beefdead", phash="f" * 16, dedup_threshold=8,
    )
    conn.close()


def test_screenshots_command_lists_all_captures(tmp_path):
    _seed_screenshot_session(tmp_path)
    result = runner.invoke(app, ["screenshots", "vector research"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "periodic" in result.output
    assert "on_demand_fullpage" in result.output
    assert "deadbeef" in result.output
    assert "beefdead" in result.output


def test_screenshots_command_filters_by_url(tmp_path):
    _seed_screenshot_session(tmp_path)
    result = runner.invoke(
        app,
        ["screenshots", "vector research", "--url", "https://arxiv.org/abs/8"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "deadbeef" in result.output
    assert "beefdead" not in result.output


def test_screenshots_command_no_matching_session(tmp_path):
    result = runner.invoke(app, ["screenshots", "nonexistent"], env=_env(tmp_path))
    assert result.exit_code == 0
    assert "No matching session" in result.output
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_cli.py -v -k screenshots`: FAIL (no such command).

- [ ] **Step 3: Append to `backend/src/browserbuddy/cli.py`** (after the `links` command, before `tabs`):

```python
@app.command()
def screenshots(
    session: str | None = typer.Argument(  # noqa: B008 — Typer requires Argument() in defaults
        None, help="Session id or name (default: newest)."
    ),
    url: str | None = typer.Option(None, help="Filter to one page URL."),
) -> None:
    """List a session's captured screenshots."""
    conn = db.connect(get_settings().db_path)
    try:
        storage = Storage(conn)
        rows = storage.list_sessions(limit=200)
        target = _resolve_session(rows, session)
        if target is None:
            typer.echo("No matching session." if session else "No sessions recorded yet.")
            return
        shots = storage.list_screenshots(target["id"], url=url)
    finally:
        conn.close()
    typer.echo(f"Session: {target['name']} (id {target['id']})")
    if not shots:
        typer.echo("  (none)")
        return
    for shot in shots:
        typer.echo(f"  {_fmt_time(shot['captured_at'])}  [{shot['kind']}]  {shot['url']}")
        typer.echo(f"                  blob={shot['blob_key']}")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_cli.py -q`: PASS. `uv run pytest -q` (full backend suite): PASS. `uv run ruff check . && uv run ruff format --check .`: clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/browserbuddy/cli.py backend/tests/test_cli.py
git commit -m "Add buddy screenshots CLI command"
```

---

## Task 10: Extension — `page_screenshot` type + `pageScreenshotEvent` conversion

**Files:**
- Modify: `extension/src/types.ts`
- Modify: `extension/src/capture.ts`
- Modify: `extension/tests/capture.test.ts`

**Interfaces:**
- Produces: `pageScreenshotEvent(url: string, kind: "periodic"|"on_demand_viewport"|"on_demand_fullpage", blobKeys: string[], now?: () => number): EventPayload & { type: "page_screenshot" }` — Task 14 (`background.ts`) calls this.

- [ ] **Step 1: Write failing test** — in `extension/tests/capture.test.ts`, update the top-of-file import from:

```ts
import { MAX_HTML_CHARS, MAX_LINKS, linkClickEvent, pageCaptureEvent, pageDwellEvent } from "@src/capture";
```

to:

```ts
import {
  MAX_HTML_CHARS,
  MAX_LINKS,
  linkClickEvent,
  pageCaptureEvent,
  pageDwellEvent,
  pageScreenshotEvent,
} from "@src/capture";
```

Then append:

```ts
test("pageScreenshotEvent builds a page_screenshot event", () => {
  expect(pageScreenshotEvent("https://a.com", "periodic", ["k1"], () => 1_000)).toEqual({
    type: "page_screenshot",
    url: "https://a.com",
    occurred_at: 1_000,
    kind: "periodic",
    blob_keys: ["k1"],
  });
});

test("pageScreenshotEvent carries multiple ordered tile keys for fullpage capture", () => {
  const event = pageScreenshotEvent("https://a.com", "on_demand_fullpage", ["k1", "k2"], () => 1_000);
  expect(event.blob_keys).toEqual(["k1", "k2"]);
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- capture`: FAIL (`pageScreenshotEvent` missing; `page_screenshot` not on `EventPayload`).

- [ ] **Step 3a: Edit `extension/src/types.ts`** — add the `page_screenshot` arm to `EventPayload`. Replace the closing of the `page_dwell` arm:

```ts
  | {
      type: "page_dwell";
      url: string;
      dwell_ms: number;
      occurred_at: number;
    };
```

with:

```ts
  | {
      type: "page_dwell";
      url: string;
      dwell_ms: number;
      occurred_at: number;
    }
  | {
      type: "page_screenshot";
      url: string;
      occurred_at: number;
      kind: "periodic" | "on_demand_viewport" | "on_demand_fullpage";
      blob_keys: string[];
    };
```

- [ ] **Step 3b: Append to `extension/src/capture.ts`**

```ts
/** Build a page_screenshot event referencing already-uploaded blob key(s) —
 * one for a viewport capture, N ordered tiles for a full-page on-demand
 * capture the backend will stitch (see background.ts's captureFullPage). */
export function pageScreenshotEvent(
  url: string,
  kind: "periodic" | "on_demand_viewport" | "on_demand_fullpage",
  blobKeys: string[],
  now: () => number = Date.now,
): EventPayload & { type: "page_screenshot" } {
  return { type: "page_screenshot", url, occurred_at: now(), kind, blob_keys: blobKeys };
}
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- capture` (PASS), then `npm --prefix extension run check` (tsc clean).

- [ ] **Step 5: Commit**

```bash
git add extension/src/types.ts extension/src/capture.ts extension/tests/capture.test.ts
git commit -m "Add page_screenshot event type and pageScreenshotEvent conversion"
```

---

## Task 11: Extension — `blobs.ts` REST client

**Files:**
- Create: `extension/src/blobs.ts`
- Create: `extension/tests/blobs.test.ts`

**Interfaces:**
- Produces: `toHttpUrl(wsUrl: string): string`, `dataUrlToBytes(dataUrl: string): Uint8Array`, `uploadBlob(httpUrl: string, secret: string, bytes: Uint8Array, fetchImpl?: typeof fetch): Promise<string>` — Task 14 (`background.ts`) calls all three.

- [ ] **Step 1: Write failing tests** — create `extension/tests/blobs.test.ts`:

```ts
import { expect, test, vi } from "vitest";
import { dataUrlToBytes, toHttpUrl, uploadBlob } from "@src/blobs";

test("toHttpUrl converts ws to http", () => {
  expect(toHttpUrl("ws://127.0.0.1:8765/ws")).toBe("http://127.0.0.1:8765/ws");
});

test("toHttpUrl converts wss to https", () => {
  expect(toHttpUrl("wss://example.com/ws")).toBe("https://example.com/ws");
});

test("toHttpUrl leaves a non-ws url unchanged", () => {
  expect(toHttpUrl("http://already-http.com")).toBe("http://already-http.com");
});

test("dataUrlToBytes decodes the base64 payload", () => {
  // "AQID" base64-decodes to bytes [1, 2, 3]
  const bytes = dataUrlToBytes("data:image/jpeg;base64,AQID");
  expect(Array.from(bytes)).toEqual([1, 2, 3]);
});

test("uploadBlob posts bytes with the secret header and returns the key", async () => {
  const fetchImpl = vi.fn(async () => ({
    ok: true,
    json: async () => ({ key: "abc123" }),
  })) as unknown as typeof fetch;
  const bytes = new Uint8Array([1, 2, 3]);
  const key = await uploadBlob("http://127.0.0.1:8765", "s3cret", bytes, fetchImpl);
  expect(key).toBe("abc123");
  const [url, init] = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
  expect(url).toBe("http://127.0.0.1:8765/api/blobs");
  expect(init.method).toBe("POST");
  expect(init.body).toBe(bytes);
  expect((init.headers as Record<string, string>)["X-BB-Secret"]).toBe("s3cret");
});

test("uploadBlob throws on a non-ok response", async () => {
  const fetchImpl = vi.fn(async () => ({ ok: false, status: 401 })) as unknown as typeof fetch;
  await expect(uploadBlob("http://x", "s", new Uint8Array(), fetchImpl)).rejects.toThrow("401");
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- blobs`: FAIL (module missing).

- [ ] **Step 3: Create `extension/src/blobs.ts`**

```ts
/** REST client for the backend's generic content-addressed blob store — the
 * out-of-band upload path for binary screenshot bytes (WS frames stay
 * JSON-only; see capture.ts's pageScreenshotEvent, which only ever carries
 * the resulting key, never raw bytes). Best-effort: callers log-and-drop on
 * rejection (see background.ts) — no retry buffer, per the "backend
 * assumed reachable" scope decision in the design spec. */

/** Settings.backendUrl is a ws(s):// URL (the WS ingest endpoint); the REST
 * blob endpoint lives on the same host/port over plain HTTP(S). */
export function toHttpUrl(wsUrl: string): string {
  if (wsUrl.startsWith("wss://")) return `https://${wsUrl.slice(6)}`;
  if (wsUrl.startsWith("ws://")) return `http://${wsUrl.slice(5)}`;
  return wsUrl;
}

/** Decode a data: URL (as returned by tabs.captureVisibleTab) into raw bytes. */
export function dataUrlToBytes(dataUrl: string): Uint8Array {
  const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Upload bytes, returning the backend's content-addressed key. */
export async function uploadBlob(
  httpUrl: string,
  secret: string,
  bytes: Uint8Array,
  fetchImpl: typeof fetch = fetch,
): Promise<string> {
  const resp = await fetchImpl(`${httpUrl}/api/blobs`, {
    method: "POST",
    headers: { "X-BB-Secret": secret },
    body: bytes,
  });
  if (!resp.ok) throw new Error(`blob upload failed: ${resp.status}`);
  const json = (await resp.json()) as { key: string };
  return json.key;
}
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- blobs`: PASS. `npm --prefix extension run check`: clean.

- [ ] **Step 5: Commit**

```bash
git add extension/src/blobs.ts extension/tests/blobs.test.ts
git commit -m "Add REST blob-upload client (toHttpUrl, dataUrlToBytes, uploadBlob)"
```

---

## Task 12: Extension — `screenshot.ts` pure scheduling/geometry helpers

**Files:**
- Create: `extension/src/screenshot.ts`
- Create: `extension/tests/screenshot.test.ts`

**Interfaces:**
- Produces: `scrollStepCount(scrollHeight: number, viewportHeight: number): number`, `scrollOffsetForStep(step: number, viewportHeight: number): number`, `intervalElapsed(now: number, lastCapturedAt: number, intervalMs: number): boolean` — Task 14 (`background.ts`) uses all three for the periodic-capture gate and the full-page tile loop.

- [ ] **Step 1: Write failing tests** — create `extension/tests/screenshot.test.ts`:

```ts
import { expect, test } from "vitest";
import { intervalElapsed, scrollOffsetForStep, scrollStepCount } from "@src/screenshot";

test("scrollStepCount covers a page taller than one viewport", () => {
  expect(scrollStepCount(2500, 1000)).toBe(3);
});

test("scrollStepCount is at least 1 for a page shorter than one viewport", () => {
  expect(scrollStepCount(400, 1000)).toBe(1);
});

test("scrollStepCount handles an exact multiple without an extra step", () => {
  expect(scrollStepCount(2000, 1000)).toBe(2);
});

test("scrollStepCount guards a zero viewport height", () => {
  expect(scrollStepCount(2000, 0)).toBe(1);
});

test("scrollOffsetForStep multiplies step by viewport height", () => {
  expect(scrollOffsetForStep(2, 800)).toBe(1600);
});

test("scrollOffsetForStep is 0 at step 0", () => {
  expect(scrollOffsetForStep(0, 800)).toBe(0);
});

test("intervalElapsed is false before the interval and true at/after it", () => {
  expect(intervalElapsed(1500, 1000, 1000)).toBe(false);
  expect(intervalElapsed(2000, 1000, 1000)).toBe(true);
  expect(intervalElapsed(2500, 1000, 1000)).toBe(true);
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- screenshot`: FAIL (module missing).

- [ ] **Step 3: Create `extension/src/screenshot.ts`**

```ts
/** Pure scheduling/geometry helpers for screenshot capture (Slice C). No
 * browser API access — background.ts wires these into captureVisibleTab
 * calls and tabs.sendMessage scroll-step requests. */

/** Number of viewport-height steps needed to cover a page's full scroll
 * height. Always at least 1 — a page shorter than one viewport still gets
 * one capture. */
export function scrollStepCount(scrollHeight: number, viewportHeight: number): number {
  if (viewportHeight <= 0) return 1;
  return Math.max(1, Math.ceil(scrollHeight / viewportHeight));
}

/** Y-offset (px) to scroll to for a given full-page tile step index. */
export function scrollOffsetForStep(step: number, viewportHeight: number): number {
  return step * viewportHeight;
}

/** True once at least intervalMs has passed since the last capture. */
export function intervalElapsed(now: number, lastCapturedAt: number, intervalMs: number): boolean {
  return now - lastCapturedAt >= intervalMs;
}
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- screenshot`: PASS. `npm --prefix extension run check`: clean.

- [ ] **Step 5: Commit**

```bash
git add extension/src/screenshot.ts extension/tests/screenshot.test.ts
git commit -m "Add pure screenshot scheduling and full-page scroll-step helpers"
```

---

## Task 13: Extension — `sessions.ts` screenshot toggle + `updateCapture`

**Files:**
- Modify: `extension/src/sessions.ts`
- Modify: `extension/tests/sessions.test.ts`

**Interfaces:**
- Produces: `SessionState` gains `screenshotsEnabled: boolean`, `screenshotIntervalMs: number`. `SessionManager.start(name, capture?: CaptureOptions)`, `SessionManager.updateCapture(capture: CaptureOptions): Promise<void>` — Task 14 (`background.ts`) calls both.

- [ ] **Step 1: Write failing tests** — first update the existing exact-equality assertion in `extension/tests/sessions.test.ts`. Change:

```ts
  expect(await mgr.current()).toEqual({
    name: "quantum papers",
    startedAt: 1_000_000,
    status: "recording",
  });
});
```

to:

```ts
  expect(await mgr.current()).toEqual({
    name: "quantum papers",
    startedAt: 1_000_000,
    status: "recording",
    screenshotsEnabled: false,
    screenshotIntervalMs: 30_000,
  });
});
```

Then append new tests at the end of the file:

```ts
test("start accepts explicit capture options", async () => {
  const { mgr } = manager();
  await mgr.start("s", { screenshotsEnabled: true, screenshotIntervalMs: 5000 });
  expect(await mgr.current()).toEqual({
    name: "s",
    startedAt: 1_000_000,
    status: "recording",
    screenshotsEnabled: true,
    screenshotIntervalMs: 5000,
  });
});

test("updateCapture changes settings mid-session without touching status", async () => {
  const { mgr } = manager();
  await mgr.start("s", { screenshotsEnabled: false, screenshotIntervalMs: 30_000 });
  await mgr.pause();
  await mgr.updateCapture({ screenshotsEnabled: true, screenshotIntervalMs: 10_000 });
  const state = await mgr.current();
  expect(state?.screenshotsEnabled).toBe(true);
  expect(state?.screenshotIntervalMs).toBe(10_000);
  expect(state?.status).toBe("paused"); // untouched
});

test("updateCapture is a no-op with no active session", async () => {
  const { mgr } = manager();
  await mgr.updateCapture({ screenshotsEnabled: true, screenshotIntervalMs: 1000 });
  expect(await mgr.current()).toBeNull();
});
```

- [ ] **Step 2: Run to verify failure** — `npm --prefix extension run test -- sessions`: FAIL (`updateCapture` missing; `start`'s second arg unsupported; the updated `toEqual` fails against the current two-field-short shape).

- [ ] **Step 3: Edit `extension/src/sessions.ts`**

Replace the `SessionState` interface and add `CaptureOptions`:

```ts
export interface SessionState {
  name: string;
  startedAt: number;
  status: "recording" | "paused";
}
```

becomes:

```ts
export interface CaptureOptions {
  screenshotsEnabled: boolean;
  screenshotIntervalMs: number;
}

const DEFAULT_CAPTURE: CaptureOptions = { screenshotsEnabled: false, screenshotIntervalMs: 30_000 };

export interface SessionState extends CaptureOptions {
  name: string;
  startedAt: number;
  status: "recording" | "paused";
}
```

Replace `start`:

```ts
  start(name: string): Promise<{ event: EventPayload }> {
    return this.serialize(async () => {
      const startedAt = this.now();
      const state: SessionState = { name, startedAt, status: "recording" };
      await this.local.set({ [KEY]: state });
      // Starting over an active session is fine: the backend implicitly
      // closes the previous session at the new start time.
      return { event: { type: "session_start", name, started_at: startedAt } };
    });
  }
```

with:

```ts
  start(name: string, capture: CaptureOptions = DEFAULT_CAPTURE): Promise<{ event: EventPayload }> {
    return this.serialize(async () => {
      const startedAt = this.now();
      const state: SessionState = { name, startedAt, status: "recording", ...capture };
      await this.local.set({ [KEY]: state });
      // Starting over an active session is fine: the backend implicitly
      // closes the previous session at the new start time.
      return { event: { type: "session_start", name, started_at: startedAt } };
    });
  }

  /** Change the screenshot toggle/interval for the active session, mid-
   * recording — independently of pause/resume, and a no-op with no active
   * session. */
  updateCapture(capture: CaptureOptions): Promise<void> {
    return this.serialize(async () => {
      const state = await this.current();
      if (state) await this.local.set({ [KEY]: { ...state, ...capture } });
    });
  }
```

- [ ] **Step 4: Run** — `npm --prefix extension run test -- sessions`: PASS. `npm --prefix extension run check`: clean.

- [ ] **Step 5: Commit**

```bash
git add extension/src/sessions.ts extension/tests/sessions.test.ts
git commit -m "Add per-session screenshot toggle/interval and updateCapture"
```

---

## Task 14: Extension — background scheduling, periodic capture, on-demand full-page

**Files:**
- Modify: `extension/src/background.ts`

Background wiring; verified by build + smoke (pure helpers already tested in Tasks 10-12). No new unit tests, matching the precedent set for equally browser-API-heavy wiring in earlier slices.

- [ ] **Step 1: Edit `extension/src/background.ts`**

Change the capture import:

```ts
import {
  linkClickEvent,
  pageCaptureEvent,
  pageDwellEvent,
  type ClickMessage,
  type PageMessage,
} from "./capture";
```

to:

```ts
import {
  linkClickEvent,
  pageCaptureEvent,
  pageDwellEvent,
  pageScreenshotEvent,
  type ClickMessage,
  type PageMessage,
} from "./capture";
import { dataUrlToBytes, toHttpUrl, uploadBlob } from "./blobs";
import { intervalElapsed, scrollStepCount } from "./screenshot";
```

Extend `SessionCommand`:

```ts
interface SessionCommand {
  type: "bb:session";
  action: "start" | "pause" | "resume" | "stop" | "status";
  name?: string;
}
```

to:

```ts
interface SessionCommand {
  type: "bb:session";
  action: "start" | "pause" | "resume" | "stop" | "status" | "update-capture";
  name?: string;
  screenshotsEnabled?: boolean;
  screenshotIntervalMs?: number;
}
```

Add active-tab tick-control state and helpers directly after `setBadge` (before the `SessionCommand` interface):

```ts
let tickingTabId: number | null = null;

function tellTab(tabId: number, action: "start" | "stop"): void {
  void browser.tabs
    .sendMessage(tabId, { type: "bb:screenshot-tick-control", action })
    .catch(() => undefined); // no content script in this tab (e.g. a protected page) — fine
}

/** Ensure exactly the given tab (or none) is running the screenshot tick
 * loop. O(1) regardless of how many tabs are open — only ever one tab ticks
 * at a time, toggled on focus change. See the design spec's
 * scheduling-mechanism rationale for why this beats browser.alarms (too
 * coarse a floor for a configurable sub-minute interval) and a bare
 * background setInterval (only survives because WS heartbeat traffic
 * happens to prevent worker suspension — fragile independent of backend
 * uptime, and doesn't need to be relied on here). */
function setTickingTab(tabId: number | null): void {
  if (tickingTabId === tabId) return;
  if (tickingTabId !== null) tellTab(tickingTabId, "stop");
  tickingTabId = tabId;
  if (tabId !== null) tellTab(tabId, "start");
}

/** Derive ticking state from persisted session state (idempotent), mirroring
 * syncCaptureState. Call together with it wherever session lifecycle
 * changes (start/stop/pause/resume, wake reconcile). */
async function syncTickingState(): Promise<void> {
  const state = await sessionManager.current();
  if (state?.status !== "recording") {
    setTickingTab(null);
    return;
  }
  const [tab] = await browser.tabs.query({ active: true, lastFocusedWindow: true });
  setTickingTab(tab?.id ?? null);
}
```

In `handleSessionCommand`, extend the `"start"` case and add `"update-capture"` — change:

```ts
      case "start": {
        // `||` not `??`: an empty/whitespace name must fall back, or the
        // backend's min_length=1 validation error-drops the event and every
        // capture behind it.
        const name = cmd.name?.trim() || "untitled session";
        const { event } = await sessionManager.start(name);
        await queue.enqueue(event);
        await (await ensureClient()).notifyEnqueued();
        break;
      }
```

to:

```ts
      case "start": {
        // `||` not `??`: an empty/whitespace name must fall back, or the
        // backend's min_length=1 validation error-drops the event and every
        // capture behind it.
        const name = cmd.name?.trim() || "untitled session";
        const capture = {
          screenshotsEnabled: cmd.screenshotsEnabled ?? false,
          screenshotIntervalMs: cmd.screenshotIntervalMs ?? 30_000,
        };
        const { event } = await sessionManager.start(name, capture);
        await queue.enqueue(event);
        await (await ensureClient()).notifyEnqueued();
        break;
      }
      case "update-capture":
        await sessionManager.updateCapture({
          screenshotsEnabled: cmd.screenshotsEnabled ?? false,
          screenshotIntervalMs: cmd.screenshotIntervalMs ?? 30_000,
        });
        break;
```

Change the end of `handleSessionCommand`'s `serializeLifecycle` block — replace:

```ts
    await syncCaptureState();
  });
  return { session: await sessionManager.current(), connection: clientStatus };
```

with:

```ts
    await syncCaptureState();
    await syncTickingState();
  });
  return { session: await sessionManager.current(), connection: clientStatus };
```

Add the periodic-capture and full-page-capture logic directly after `sendDwell` (before `handleCommitted`):

```ts
async function captureTile(tabId: number, windowId: number): Promise<string | null> {
  let dataUrl: string;
  try {
    dataUrl = await browser.tabs.captureVisibleTab(windowId, { format: "jpeg", quality: 70 });
  } catch (e) {
    console.warn("BrowserBuddy: screenshot capture failed", e);
    return null;
  }
  const settings = await loadSettings(local);
  try {
    return await uploadBlob(toHttpUrl(settings.backendUrl), settings.secret, dataUrlToBytes(dataUrl));
  } catch (e) {
    // Best-effort, no retry buffer — see the design spec's "backend assumed
    // reachable" scope decision.
    console.warn("BrowserBuddy: screenshot upload failed", e);
    return null;
  }
}

const lastScreenshotAt = new Map<number, number>();

/** Handle a bb:screenshot-tick heartbeat from a content script. The tab is
 * necessarily the one background designated active (see setTickingTab) —
 * this re-check guards a stale tick from a tab that stopped being active a
 * moment ago, racing its own "stop" message. */
async function handleScreenshotTick(tabId: number | undefined): Promise<void> {
  if (tabId === undefined || tabId !== tickingTabId) return;
  const state = await sessionManager.current();
  if (state?.status !== "recording" || !state.screenshotsEnabled) return;
  const now = Date.now();
  const last = lastScreenshotAt.get(tabId) ?? 0;
  if (!intervalElapsed(now, last, state.screenshotIntervalMs)) return;
  const tab = await browser.tabs.get(tabId).catch(() => null);
  if (!tab?.url || tab.windowId === undefined) return;
  const key = await captureTile(tabId, tab.windowId);
  if (!key) return;
  lastScreenshotAt.set(tabId, now);
  await queue.enqueue(pageScreenshotEvent(tab.url, "periodic", [key]));
  await (await ensureClient()).notifyEnqueued();
}

/** On-demand full-page capture: scroll the tab in viewport-height steps
 * (driven by the content script, which alone has DOM access), capturing a
 * tile at each stop, then ship all tiles as one page_screenshot event for
 * the backend to stitch. */
async function captureFullPage(tabId: number): Promise<void> {
  const tab = await browser.tabs.get(tabId).catch(() => null);
  if (!tab?.url || tab.windowId === undefined) return;
  const first = await browser.tabs
    .sendMessage(tabId, { type: "bb:scroll-step", index: 0 })
    .catch(() => null);
  if (!first) return;
  const { scrollHeight, viewportHeight } = first as { scrollHeight: number; viewportHeight: number };
  const steps = scrollStepCount(scrollHeight, viewportHeight);
  const keys: string[] = [];
  for (let i = 0; i < steps; i++) {
    if (i > 0) {
      const ok = await browser.tabs
        .sendMessage(tabId, { type: "bb:scroll-step", index: i })
        .catch(() => null);
      if (!ok) break;
    }
    const key = await captureTile(tabId, tab.windowId);
    if (key) keys.push(key);
  }
  await browser.tabs.sendMessage(tabId, { type: "bb:scroll-step", index: -1 }).catch(() => undefined);
  if (keys.length === 0) return;
  await queue.enqueue(pageScreenshotEvent(tab.url, "on_demand_fullpage", keys));
  await (await ensureClient()).notifyEnqueued();
}
```

Wire the two new message types into the existing `browser.runtime.onMessage.addListener` — change:

```ts
  if (msg?.type === "bb:click") {
    logged(handleClickMessage(message as ClickMessage));
    return false; // fire-and-forget
  }
  if (msg?.type === "bb:session") {
```

to:

```ts
  if (msg?.type === "bb:click") {
    logged(handleClickMessage(message as ClickMessage));
    return false; // fire-and-forget
  }
  if (msg?.type === "bb:screenshot-tick") {
    logged(handleScreenshotTick(sender.tab?.id));
    return false;
  }
  if (msg?.type === "bb:capture-fullpage") {
    logged(
      sessionManager.current().then((s) => {
        if (s?.status !== "recording" || tickingTabId === null) return;
        return captureFullPage(tickingTabId);
      }),
    );
    return false;
  }
  if (msg?.type === "bb:session") {
```

Wire ticking into the tab/window focus listeners — change:

```ts
browser.tabs.onActivated.addListener((info) => {
  void browser.windows
    .get(info.windowId)
    .then((win) => {
      // tabs.onActivated fires for background windows too; TabTracker's
      // precondition is "focused window's active tab only".
      if (win.focused) {
        void tracker.recordActivation(info.tabId, info.windowId).then(scheduleSnapshot);
      } else {
        scheduleSnapshot();
      }
    })
    .catch(() => scheduleSnapshot()); // window can close mid-switch
});
```

to:

```ts
browser.tabs.onActivated.addListener((info) => {
  void browser.windows
    .get(info.windowId)
    .then((win) => {
      // tabs.onActivated fires for background windows too; TabTracker's
      // precondition is "focused window's active tab only".
      if (win.focused) {
        void tracker.recordActivation(info.tabId, info.windowId).then(scheduleSnapshot);
        logged(syncTickingState());
      } else {
        scheduleSnapshot();
      }
    })
    .catch(() => scheduleSnapshot()); // window can close mid-switch
});
```

And change:

```ts
browser.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === browser.windows.WINDOW_ID_NONE) {
    logged(tracker.recordFocusLost().then(scheduleSnapshot));
  } else {
    logged(
      browser.tabs.query({ active: true, windowId }).then(([tab]) => {
        if (tab?.id !== undefined) {
          return tracker.recordActivation(tab.id, windowId).then(scheduleSnapshot);
        }
      }),
    );
  }
});
```

to:

```ts
browser.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === browser.windows.WINDOW_ID_NONE) {
    logged(tracker.recordFocusLost().then(scheduleSnapshot));
    setTickingTab(null); // browser itself lost focus — nothing is "the active tab"
  } else {
    logged(
      browser.tabs.query({ active: true, windowId }).then(([tab]) => {
        if (tab?.id !== undefined) {
          return tracker.recordActivation(tab.id, windowId).then(scheduleSnapshot);
        }
      }),
    );
    logged(syncTickingState());
  }
});
```

Finally, fold `syncTickingState` into the wake-reconcile at the bottom of the file — change:

```ts
// Reconcile capture registration + badge with persisted session state on
// wake: a browser restart keeps the session recording (user never said stop).
logged(serializeLifecycle(syncCaptureState));
```

to:

```ts
// Reconcile capture registration + badge + screenshot ticking with
// persisted session state on wake: a browser restart keeps the session
// recording (user never said stop).
logged(serializeLifecycle(() => syncCaptureState().then(syncTickingState)));
```

- [ ] **Step 2: Run** — `npm --prefix extension run check` (same test count as after Task 13, clean — no new tests for this file; tsc/eslint verify the wiring compiles) and `npm --prefix extension run build`.

- [ ] **Step 3: Commit**

```bash
git add extension/src/background.ts
git commit -m "Wire active-tab-gated screenshot scheduling and on-demand full-page capture"
```

---

## Task 15: Extension — content script tick loop + scroll-step listener

**Files:**
- Modify: `extension/src/content.ts`

Content script is a thin IIFE with no unit tests (verified by build + smoke, matching M3a/Slice A/B). Its pure logic already has coverage from Task 12.

- [ ] **Step 1: Edit `extension/src/content.ts`** — insert this block right before the IIFE's closing `})();`, after the existing `nav?.addEventListener("navigatesuccess", ...)` block:

```ts
  // --- Screenshots (Slice C) ------------------------------------------
  // Tick loop: a fixed-cadence heartbeat while this tab is the one
  // background has designated "active." This script has no idea whether
  // screenshots are enabled or what interval is configured — background
  // decides everything (same split as the bb:page/bb:click messages
  // above). Content scripts stay alive for the life of the document,
  // unlike the ephemeral background worker, which is exactly why the
  // cadence lives here rather than a background timer.
  const SCREENSHOT_TICK_MS = 2000;
  let screenshotTimer: ReturnType<typeof setTimeout> | undefined;

  const screenshotTick = (): void => {
    void runtime?.sendMessage({ type: "bb:screenshot-tick" }).catch(() => undefined);
    screenshotTimer = setTimeout(screenshotTick, SCREENSHOT_TICK_MS);
  };

  // Full-page on-demand capture driver: background asks this script to
  // scroll to each viewport-height step and reports back the page's
  // dimensions so background knows how many steps remain; background alone
  // calls captureVisibleTab (content scripts can't).
  runtime?.onMessage.addListener((message, _sender, sendResponse) => {
    const msg = message as { type?: string; action?: "start" | "stop"; index?: number };
    if (msg.type === "bb:screenshot-tick-control") {
      clearTimeout(screenshotTimer);
      if (msg.action === "start") screenshotTimer = setTimeout(screenshotTick, SCREENSHOT_TICK_MS);
      return false;
    }
    if (msg.type === "bb:scroll-step") {
      if (msg.index === -1) {
        window.scrollTo(0, 0); // restore scroll position once the tile sequence is done
        sendResponse({});
        return true;
      }
      window.scrollTo(0, (msg.index ?? 0) * window.innerHeight);
      setTimeout(() => {
        // Let reflow/lazy-loaded images settle before background captures
        // this tile.
        sendResponse({
          scrollHeight: document.documentElement.scrollHeight,
          viewportHeight: window.innerHeight,
        });
      }, 300);
      return true; // async reply
    }
    return false;
  });
```

- [ ] **Step 2: Run** — `npm --prefix extension run check` (clean, no new tests for this file) and `npm --prefix extension run build`; confirm `dist/firefox/content.js` exists and is an IIFE, not an `import`.

- [ ] **Step 3: Commit**

```bash
git add extension/src/content.ts
git commit -m "Add screenshot tick loop and full-page scroll-step listener to content script"
```

---

## Task 16: Extension — popup UI (toggle, interval, capture-now)

**Files:**
- Modify: `extension/src/popup/popup.html`
- Modify: `extension/src/popup/popup.ts`

Popup UI; no unit tests for this file today (verified by build + smoke, same as the rest of the popup).

- [ ] **Step 1: Edit `extension/src/popup/popup.html`** — replace the body's `<style>` block's closing rule and markup. Change:

```html
      #connection { color: #555; margin-top: 0.5rem; font-size: 12px; }
      #settings { display: inline-block; margin-top: 0.5rem; font-size: 12px; color: #2668c9; cursor: pointer; text-decoration: none; }
      .hidden { display: none; }
    </style>
  </head>
  <body>
    <h1>BrowserBuddy</h1>
    <div id="idle">
      <input id="name" type="text" placeholder="Session name (what are you researching?)" />
      <button id="start">Start session</button>
    </div>
    <div id="active" class="hidden">
      <div id="session"></div>
      <button id="pause">Pause</button>
      <button id="resume" class="hidden">Resume</button>
      <button id="stop">Stop</button>
    </div>
    <div id="connection">…</div>
    <a id="settings" href="#">⚙ Settings</a>
    <script type="module" src="./popup.ts"></script>
  </body>
</html>
```

to:

```html
      #connection { color: #555; margin-top: 0.5rem; font-size: 12px; }
      #settings { display: inline-block; margin-top: 0.5rem; font-size: 12px; color: #2668c9; cursor: pointer; text-decoration: none; }
      #capture-settings { margin: 0.5rem 0; font-size: 12px; }
      #capture-settings input[type="number"] { width: 3.5em; display: inline; padding: 0.1rem; }
      .hidden { display: none; }
    </style>
  </head>
  <body>
    <h1>BrowserBuddy</h1>
    <div id="idle">
      <input id="name" type="text" placeholder="Session name (what are you researching?)" />
      <button id="start">Start session</button>
    </div>
    <div id="active" class="hidden">
      <div id="session"></div>
      <button id="pause">Pause</button>
      <button id="resume" class="hidden">Resume</button>
      <button id="stop">Stop</button>
      <button id="capture-now">📷 Capture now</button>
    </div>
    <div id="capture-settings">
      <label><input id="screenshots-enabled" type="checkbox" /> Capture screenshots</label>
      every <input id="screenshot-interval" type="number" min="1" value="30" /> sec
    </div>
    <div id="connection">…</div>
    <a id="settings" href="#">⚙ Settings</a>
    <script type="module" src="./popup.ts"></script>
  </body>
</html>
```

- [ ] **Step 2: Edit `extension/src/popup/popup.ts`** — add the new element lookups after the existing ones. Change:

```ts
const pauseBtn = el<HTMLButtonElement>("pause");
const resumeBtn = el<HTMLButtonElement>("resume");
const connectionEl = el<HTMLDivElement>("connection");
```

to:

```ts
const pauseBtn = el<HTMLButtonElement>("pause");
const resumeBtn = el<HTMLButtonElement>("resume");
const connectionEl = el<HTMLDivElement>("connection");
const screenshotsEnabledInput = el<HTMLInputElement>("screenshots-enabled");
const screenshotIntervalInput = el<HTMLInputElement>("screenshot-interval");
const captureNowBtn = el<HTMLButtonElement>("capture-now");

function captureOptions(): { screenshotsEnabled: boolean; screenshotIntervalMs: number } {
  const seconds = Math.max(1, Number(screenshotIntervalInput.value) || 30);
  return {
    screenshotsEnabled: screenshotsEnabledInput.checked,
    screenshotIntervalMs: seconds * 1000,
  };
}
```

Change the `command` function to always include capture options:

```ts
async function command(action: string, name?: string): Promise<void> {
  try {
    const reply = (await browser.runtime.sendMessage({
      type: "bb:session",
      action,
      name,
    })) as SessionReply;
    render(reply);
  } catch {
    connectionEl.textContent = "Backend: background unavailable";
  }
}
```

to:

```ts
async function command(action: string, name?: string): Promise<void> {
  try {
    const reply = (await browser.runtime.sendMessage({
      type: "bb:session",
      action,
      name,
      ...captureOptions(),
    })) as SessionReply;
    render(reply);
  } catch {
    connectionEl.textContent = "Backend: background unavailable";
  }
}
```

Add live-update and capture-now wiring at the end of the file, after the existing `settings` click listener:

```ts
// Toggling/changing screenshot settings while a session may already be
// recording applies immediately — the "start" fields double as the live
// controls (a no-op if idle, since update-capture requires an active
// session; see sessions.ts).
screenshotsEnabledInput.addEventListener("change", () => void command("update-capture"));
screenshotIntervalInput.addEventListener("change", () => void command("update-capture"));

captureNowBtn.addEventListener("click", () => {
  void browser.runtime.sendMessage({ type: "bb:capture-fullpage" });
});
```

- [ ] **Step 3: Run** — `npm --prefix extension run check` (clean) and `npm --prefix extension run build`.

- [ ] **Step 4: Commit**

```bash
git add extension/src/popup/popup.html extension/src/popup/popup.ts
git commit -m "Add screenshot toggle, interval, and capture-now controls to the popup"
```

---

## Task 17: Gates, automated e2e, docs, smoke instructions

**Files:** `README.md` (modify); otherwise verification only.

- [ ] **Step 1: Full gates, both projects**

```bash
cd /Users/david/Projects/BrowserBuddy/extension && npm run check && npm run build && npx web-ext lint --source-dir dist/firefox
cd /Users/david/Projects/BrowserBuddy/backend && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected: extension clean (prior ~80-test baseline + Tasks 10-13's additions); backend clean (prior baseline + Tasks 1-9's additions, now including Pillow). `web-ext lint`: the pre-existing `data_collection_permissions` + bundled-Readability `UNSAFE_VAR_ASSIGNMENT` warnings are known/acceptable — report any new ones.

- [ ] **Step 2: Automated protocol e2e** (same pattern as prior slices' Task 13/12): scratch Node script against a temp-data-dir `buddy serve` on an unused port —

1. auth over WS, `session_start`, `page_capture` for `https://a.com` — expect acks.
2. `POST /api/blobs` with the shared secret header and a small real JPEG's bytes — expect `200 { key }`.
3. `page_screenshot { url: "https://a.com", kind: "periodic", blob_keys: [key], occurred_at }` over WS — expect ack.
4. `session_end` — expect ack.
5. `page_screenshot` again (same url/key) — expect an **error frame** (no active session).
6. `POST /api/blobs` with a wrong secret — expect `401`.

Verify via `BROWSERBUDDY_DATA_DIR=… uv run buddy screenshots "<session name>"` that the capture shows up with `[periodic]` and the correct blob key. Kill server, clean up. Report verbatim output.

- [ ] **Step 3: Update `README.md`** — add one paragraph after the existing dwell-in-`buddy search` note:

```markdown

During a session, the popup's "Capture screenshots" toggle takes a
dwell-gated viewport screenshot on a configurable interval (default 30s,
adjustable live while recording), and the "📷 Capture now" button takes an
on-demand full-page capture via scroll-and-stitch. `buddy screenshots
<session>` lists what was captured; visually near-duplicate frames (a mostly
static page) are deduped server-side and never stored.
```

Commit:

```bash
cd /Users/david/Projects/BrowserBuddy
git add README.md
git commit -m "Document screenshot capture in the README"
```

- [ ] **Step 4: Hand off the interactive smoke to the user** (not automatable): via `./scripts/smoke.sh --fresh --build`, pair, start a session with "Capture screenshots" checked and a short interval (e.g. 5 sec). Then:
  - **Periodic viewport capture:** stay on one page past a couple of intervals; switch to a second tab and stay there past another interval. Stop. Run `uv run buddy screenshots "<name>"` and confirm captures exist for both pages, tagged `[periodic]` — and that switching tabs didn't produce a capture of the tab you left.
  - **Dedup:** confirm a static page you dwelt on for many intervals produced far fewer stored screenshots than intervals elapsed (near-duplicates rejected).
  - **On-demand full-page:** on a page taller than one screen, click "📷 Capture now." Confirm (via `uv run buddy screenshots "<name>" --url <that url>`) a `[on_demand_fullpage]` row appears, and that the blob (`uv run python -c "from browserbuddy... "` or just trust the stitched-dimensions backend test) is taller than a single viewport.
  - **Mid-session toggle:** start a session with screenshots off; toggle it on partway through and confirm captures start appearing after the toggle, without needing to stop/restart the session.
  - **Regression:** confirm dwell (`dwell=Ns` in `buddy search`), link tracking (`buddy links`), and SPA recapture from Slices A/B still work.

---

## Self-review notes

- **Spec coverage:** REST/WS split for binary vs. metadata (Tasks 3, 7, 8) — spec §A/§2. Generic content-in/key-out `/api/blobs`, no session/tab context (Task 7) — spec §A "will the blobs endpoint know which tab" exchange. Active-tab-gated content-script scheduling, not alarms/global-interval (Tasks 12, 14, 15) — spec §3 scheduling rationale. Screenshots-only toggle, mutable mid-session, always tied to an active session (Tasks 13, 14, 16) — spec's scope decisions. Periodic viewport-only / on-demand full-page distinction (Tasks 14, 15) — spec scope decisions. Backend-side dedup via tunable difference-hash threshold (Tasks 4, 5, 6, 8) — spec §4. Stitching + hashing off the event loop, alongside finally moving the pre-existing HTML `gzip.compress` off it too (Task 8) — spec §4 "closing the roadmap's long-carried item." `BlobStore` skip-compression for images (Task 1) — spec §4. `buddy screenshots` CLI as this slice's own query consumer, page-level filtering via the existing FK join (Tasks 6, 9) — spec §1/§4. Best-effort upload, no retry buffer (Tasks 11, 14) — spec's "backend assumed reachable" scope decision. Out-of-scope items (video recording, general toggle framework, domain-level indexing, periodic full-page, backend settings UI, OCR/embedding, blob GC) are absent by design — spec "Out of scope for Slice C."
- **Type consistency:** `PageScreenshotEvent` fields (`url: str, occurred_at: int (ge=0), kind: Literal[...], blob_keys: list[str] (min_length=1)`) match across `models.py`, `app.py`'s dispatch, `storage.add_page_screenshot`'s keyword args, `types.ts`'s `page_screenshot` arm, and `capture.ts`'s `pageScreenshotEvent`. `kind` literals (`"periodic" | "on_demand_viewport" | "on_demand_fullpage"`) are identical strings in the SQL `CHECK` constraint (Task 2), the Pydantic model (Task 3), the TS type (Task 10), and every call site that constructs one (Task 14's `handleScreenshotTick`/`captureFullPage`). `BlobStore.put(data, *, compress: bool = True)` / `path_for(key, *, compressed: bool = True)` signatures (Task 1) match their four call sites (`app.py`'s HTML branch, REST endpoint, and the two screenshot-branch `put`/`get` calls in Task 8). `CaptureOptions` (Task 13: `{screenshotsEnabled: boolean, screenshotIntervalMs: number}`) matches the shape read/written in `background.ts`'s `"start"`/`"update-capture"` cases (Task 14) and the popup's `captureOptions()` (Task 16).
- **Behavior preserved:** `page_screenshot` dispatch gates on an active session and raises `NoActiveSessionError` (error frame, no ack) exactly like every other Tier-2 event — checked *before* any blob read, so a no-session capture never touches the blob store. Additive protocol under v1 — no `PROTOCOL_VERSION` bump. Idempotency for `page_screenshot` comes from the existing `envelope.seq > last_seq` cursor check, same as every other event type — not from any new dedup logic (dedup is a perceptual-similarity decision, orthogonal to protocol-level replay protection). Existing `BlobStore` callers (HTML, always `compress=True` by default) are behaviorally unchanged. Existing `sessions.test.ts` assertions not touched by this plan (state-survives-recreation, pause/resume, stop-commits, stop-preserves-state-on-throw, start-replaces-active) are unaffected since `CaptureOptions` only adds fields, never removes or renames existing ones.
- **Known simplifications (deliberate, per spec's scope decisions):** no durable retry-buffer for failed REST uploads — a capture is lost if the upload fails, full stop (not queued for later, unlike every WS-delivered event). Full-page capture is on-demand only; periodic stays viewport-only. No general per-capture-type toggle framework — screenshots got their own bespoke toggle rather than a reusable mechanism, by explicit decision (a future slice's problem). Domain/site-level screenshot queries aren't supported (page-level only, via the `page_visit_id` FK) — shares its root cause with the pre-existing lack of URL canonicalization anywhere in this schema, not solved here. `captureVisibleTab`'s ~2/sec rate limit is not explicitly throttled in the full-page tile loop beyond the natural pacing of the upload round-trip between captures — acceptable at the scale of a handful of tiles per on-demand capture, revisit only if smoke testing surfaces throttling errors on unusually tall pages.
