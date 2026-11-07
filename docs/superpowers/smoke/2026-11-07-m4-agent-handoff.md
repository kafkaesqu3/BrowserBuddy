# M4 smoke testing handoff

Prepared 2026-11-07 for the next agent. Your task is to close the remaining M4 acceptance
gaps, reproduce and fix failures within M4 scope, and record evidence. Do not rebuild M4
from the original plan or mark the milestone complete from deterministic tests alone.

## Start here

- Implementation branch: `feat/m4-recall`.
- Implementation HEAD before this documentation handoff: `15230a0`.
- Current implementation worktree: `/home/agent/.codex/worktrees/997e/BrowserBuddy`.
- Original checkout: `/c/Users/david/Projects/CAT_BROWSEREXTENSIONS/BrowserBuddy`, on `main`
  at `1026e7e`, with many pre-existing modified files. Do not reset, clean, switch, or copy
  implementation over that checkout. Use the feature worktree.
- The branch is local and has not been merged or pushed. Do not assume a remote clone has it.
  For a different machine, transfer the branch using a Git bundle or another authorized
  repository transfer, and check out `feat/m4-recall` in a separate directory.

Run `git status --short`, `git branch --show-current`, and `git log -3 --oneline` in the
implementation worktree before editing. Preserve changes made after this handoff.

Read these in order:

1. [Backend setup and supported behavior](../../../backend/README.md).
2. [Existing smoke evidence and open gaps](2026-11-06-m4-smoke.md).
3. [M4 design](../specs/2026-11-06-m4-recall-design.md).
4. [Implementation plan and execution status](../plans/2026-11-06-m4-recall.md).

Use `browserbuddy-recall-acceptance` if available. Its requirements are: distinguish synthetic
tests from real-model/native-platform acceptance; record corpus, dimensions, machine,
versions, latency and memory; keep blocked checks open with their causes. The original
checkout contains this skill under `.agents/skills/`. For fixes, also read the applicable
embedding-generation or digest-publication skill there, and use the project's debugging,
regression-test and verification workflow. No new architecture is needed for this task.

## Product and implementation overview

BrowserBuddy records browsing locally. M4 adds evidence retrieval, optional local semantic
indexing, cited answers, read-only MCP, and Markdown session digests. The FastAPI backend
owns models, SQLite writes, reconciliation and the job runner. New CLI/MCP commands use
authenticated HTTP and require `buddy serve`; old offline commands remain available.

| Modules under `backend/src/browserbuddy/` | Responsibility |
|---|---|
| `memory_models.py`, `memory.py`, `memory_api.py` | Typed evidence, lexical retrieval, authenticated reads |
| `jobs.py`, `worker.py`, `ownership.py` | Persistent jobs, 60-second reconciliation, exclusive backend ownership |
| `embeddings.py`, `indexing.py`, `search.py` | Explicit model setup, cached CPU inference, atomic indexing, hybrid search |
| `client.py`, `cli_memory.py`, `operations_api.py` | Shared HTTP adapter, CLI, operational routes |
| `recall.py`, `mcp.py` | Bounded model tools, citation validation, stdio MCP |
| `digests.py`, `digest_generation.py`, `link_scoring.py` | Frozen snapshots, checkpoints, publication, outbound-link ranking |

Invariants to preserve: no automatic model download; semantic search and automatic digests
default off; no default remote LLM; capture remains available during enrichment failures;
one inference slot; five-second query-embedding timeout; source/config/claim checks prevent
obsolete publication. Digest filenames include claim tokens and a database pointer selects
the visible artifact. Previous output survives replacement failure. Screenshot tools expose
metadata, not image bytes or OCR. Citation checks establish provenance, not entailment.

## Existing evidence and what it does not prove

At `15230a0`, 200 backend tests passed; Ruff lint/format and the documented complexity and
function-length checks passed. The independent review's seven Important findings were
fixed. See the smoke report for regression details; do not reopen those tasks without a
new failure.

The synthetic benchmark uses 10,000 documents, 100,000 chunks and 768-dimensional vectors.
It measured 1.11–1.16 seconds per retrieval and about 280 MiB peak process RSS on Linux.
It excludes real model loading/inference and does not establish total response latency.

`tests/test_mcp_stdio.py` starts a backend and a separate stdio adapter, lists seven tools,
and calls `list_sessions`. It does not call all seven tools. Broader MCP evidence also
includes an in-process `get_page` call. Exercise all seven in the native smoke below.

The previous model download was denied by environment policy for `huggingface.co:443`.
Do not bypass that restriction or repeatedly retry the same denial. A permitted download
environment or an available valid cache is required. Native Windows and live LLM calls
have not been tested. Live-provider smoke is optional; the first two acceptance lanes are open.

## Isolated setup

Use synthetic browsing content and a fresh data directory. Do not point this run at the
user's normal browsing database. Run commands from the feature worktree's `backend/`.
Use the same data-directory, host and port settings in every server/client terminal.

POSIX shell:

```sh
env -u VIRTUAL_ENV uv sync --frozen --extra semantic
export BROWSERBUDDY_DATA_DIR="$(mktemp -d /tmp/browserbuddy-m4-smoke.XXXXXX)"
export BROWSERBUDDY_HOST=127.0.0.1
export BROWSERBUDDY_PORT=8876
export BROWSERBUDDY_SEMANTIC_ENABLED=true
export BROWSERBUDDY_AUTO_DIGESTS=false
unset BROWSERBUDDY_LLM_BASE_URL BROWSERBUDDY_LLM_MODEL BROWSERBUDDY_LLM_API_KEY
```

Record the generated path and export that exact path in the second terminal. Pick another
unused port if 8876 is occupied. The semantic extra is large; ordinary Torch wheels can
include accelerator libraries even though inference is CPU-only.

Native Windows PowerShell, in a separate native checkout of the feature branch:

```powershell
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
uv sync --frozen --extra semantic
$env:BROWSERBUDDY_DATA_DIR = Join-Path $env:TEMP ("browserbuddy-m4-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $env:BROWSERBUDDY_DATA_DIR | Out-Null
$env:BROWSERBUDDY_HOST = "127.0.0.1"
$env:BROWSERBUDDY_PORT = "8876"
$env:BROWSERBUDDY_SEMANTIC_ENABLED = "true"
$env:BROWSERBUDDY_AUTO_DIGESTS = "false"
Remove-Item Env:BROWSERBUDDY_LLM_BASE_URL,Env:BROWSERBUDDY_LLM_MODEL,Env:BROWSERBUDDY_LLM_API_KEY -ErrorAction SilentlyContinue
.venv\Scripts\python.exe -c "import os,sys; print(os.name,sys.platform,sys.executable)"
```

Native acceptance requires `nt win32` and a Windows Python executable. WSL is Linux evidence.
Recreate the virtual environment on Windows; do not reuse the Linux `.venv`.

For an unchanged Linux tree/environment, use the existing test evidence and run targeted
checks as needed. On a new platform, or after fixes affecting shared paths, run:

```sh
uv run --frozen --extra semantic ruff check .
uv run --frozen --extra semantic ruff format --check .
uv run --frozen --extra semantic pytest -q
```

Do not silently update dependencies to make setup succeed. Record installation failures
and investigate before changing the lockfile. No Python type checker is configured.

## Lane A Real embedding model

1. Create a synthetic fixture in the empty database before starting the server. Use
   `get_settings()`, `db.connect(settings.db_path)` and `Storage(conn)`; connection setup
   applies migrations. Use `start_session(client_id, name, started_at)`, then
   `add_page_visit(session_id, url=..., title=..., text=..., visited_at=...,
   html_key=None, container=None)`, then `end_session(client_id, ended_at)`. Close the DB.
   Alternatively feed equivalent protocol-v1 WebSocket events as in
   `tests/test_m4_integration.py`. Record the returned session/page IDs and exact text.
2. Include three distinct pages: astronomy (stars/planets), cooking (bread/yeast), and
   gardening (soil/seedlings). Put a second session outside the chosen time range in the
   fixture for filter checks. Use fixed UTC millisecond timestamps and unique URLs.
3. Start `uv run --frozen --extra semantic buddy serve`. In the second terminal run
   `uv run --frozen --extra semantic buddy index prepare`. Expected: `ready=true` and a
   config ID. If blocked, save the error cause without secrets; continue independent lanes.
4. Record `embedding.json`: model, requested/resolved revision, dimensions, capacity and
   config ID. The default is `sentence-transformers/all-mpnet-base-v2`. Verify the resolved
   revision identifies the cached snapshot, including on Windows; a mutable `main` label
   or a filesystem path masquerading as a revision is not sufficient evidence.
5. Allow the 60-second reconciliation loop to discover the fixture. Use `buddy jobs` and
   authenticated `GET /api/capabilities` to observe progress. Set a bounded wait, such as
   five minutes for this tiny corpus; report failed/stalled work instead of polling forever.
   Expect completed jobs and the fixture's documents indexed for the prepared generation.
6. Search via `MemoryClient.search_memory(SearchRequest(...))`, which reads the local secret,
   or `POST /api/memory/search` with `X-BB-Secret` read from the smoke data directory's
   `secret` file. Do not print the secret. `buddy search` is lexical and cannot prove hybrid
   retrieval. Query paraphrases such as "baking a loaf" and inspect `mode`, warnings,
   returned source IDs and excerpts. Expect hybrid mode after indexing and sensible related
   evidence; record actual ranking rather than imposing an undocumented exact order.
7. Repeat with `session_id`, `since`, and `until`; ranges are inclusive start/exclusive end.
   Verify excluded pages never appear, and every body citation's offsets reproduce its
   returned text from the stored page. Metadata chunks may have no body offsets.
8. With the actual tokenizer, exercise long text, Unicode, and a long title. Inspect body
   chunks and final encoder inputs: title prefix at most 32 tokens, overlap 64 tokens,
   body target at most 512 tokens and clamped to actual model capacity. Verify coverage of
   the stored text and no silently truncated encoder inputs. Use `LocalEmbedder.document`
   in a separate stopped-server diagnostic process if needed; retain reproducing input.
9. Restart with the same cache and outbound model access disabled (for example set
   `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` before process startup). Do not call
   prepare again. Retrieval must work from cache. Record first-query cold-start behavior
   separately; a five-second timeout should return FTS with a warning, not fail capture.
10. Stop the server, temporarily rename only this fixture's model-cache directory, and
    restart. Verify FTS remains usable with semantic-unavailable reporting and capture still
    works. Restore the cache after stopping the server. Do not remove user model caches.

Record cold model-load time, query inference/retrieval time and loaded-process memory
separately from the synthetic benchmark. If rerunning `scripts/benchmark_memory.py`, record
machine, dependency versions, corpus/chunk counts, dimensions, all timings and RSS units.
The existing two-second gate applies to retrieval mechanics, not total model latency.

## Lane B Native Windows behavior

Run Lane A on native Windows as well as these checks. Linux success does not close this lane.

1. Start backend A on the fixture data directory. Start backend B on the same directory
   but a different free port. B must fail with backend-ownership rejection, not merely a
   port-binding error; A must remain healthy. Stop B if it unexpectedly starts.
2. Stop A normally and start another backend on the same directory; it must acquire the
   lock. Repeat by terminating only the recorded PID of the smoke backend to simulate a
   crash. Restart must acquire ownership without manually deleting `backend.lock`.
3. Validate sqlite-vec native extension loading using a real hybrid query, not import alone.
4. Run `uv run --frozen --extra semantic pytest -q tests/test_mcp_stdio.py`. Then extend the
   smoke harness, using its `FastMCP Client` / `StdioTransport` pattern, to call all seven:
   `search_memory`, `search_history`, `get_open_tabs`, `list_sessions`, `get_session_digest`,
   `get_page`, `get_screenshots`. Use fixture IDs; empty metadata/history is a valid typed
   result. Read tool schemas for argument names. Verify stdout carries protocol only and
   reading a missing digest does not enqueue generation. No new listening port is expected.
5. Stop the backend and invoke a backend-dependent CLI command; expect actionable failure,
   no secret leakage and no second writer starting implicitly.

## Lane C Optional live recall and digest

Use an available configured local endpoint, or a remote endpoint the user has authorized.
Do not invent credentials or sign up for a provider. Remote use sends retrieved text off
the machine; use only the synthetic fixture. Skip with a reason if no suitable endpoint
is available. Automatic digests should stay disabled except for a deliberate small-fixture
test, because enabling them discovers every ended session in the selected data directory.

Set `BROWSERBUDDY_LLM_BASE_URL`, `BROWSERBUDDY_LLM_MODEL`, and the key only if required;
restart the backend. OpenAI-compatible transport alone does not prove tool/structured-output
support. Run `buddy ask "What did I read about baking?" --session ID --json`, then a bounded
ISO-timestamp query and a question unsupported by the fixture. Check citation provenance,
scope, sensible answer content and no unsupported assertion presented as captured evidence.

Run `buddy digest ID`, observe its job, and repeat until ready. Check the Markdown artifact,
page references, omitted-evidence disclosure and suggested-question labeling. Run
`buddy digest ID --regenerate`; verify prior output remains available while pending and
the replacement becomes visible only after publication. Preserve reproduction details for
provider failures, omitting keys, headers and unrelated local data.

## Evidence and completion requirements

Update [the smoke report](2026-11-06-m4-smoke.md) with dated new results, retaining the prior
Linux/synthetic baseline. Use one row per check: environment, exact commit, command or
action, expected result, observed result, PASS/FAIL/BLOCKED/NOT RUN, and evidence location.
Store temporary scripts/logs under ignored `.smoke/`; commit reusable test improvements
only when they add meaningful coverage. Never commit model weights, the browsing DB or keys.

For failures, capture a minimal reproducer and root cause before changing code. Add a
regression test, observe it fail, fix within M4 scope, then run affected tests plus lint and
format checks. Run the full backend suite when shared behavior changed. Do not expand this
task into dashboards, OCR, retention/GC, new providers or architecture changes.

Update the [plan](../plans/2026-11-06-m4-recall.md) and [roadmap](../ROADMAP.md) to match actual
acceptance evidence. Close real-model and native-Windows gaps only after exercising them;
an unavailable environment stays BLOCKED. Optional live-provider smoke may remain skipped
with a clear reason. Any newly discovered material failure must remain visible.

Finish with results by lane, fixes/commit IDs, remaining blockers, and exact next actions.
Leave the branch and worktree available for review; do not merge or push without instruction.
Stop only the test processes you started. Preserve useful failure evidence and report where
the isolated data/model cache was left; do not delete unrelated files.
