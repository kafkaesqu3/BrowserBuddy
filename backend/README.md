# BrowserBuddy backend

The backend captures browser sessions in SQLite and exposes lexical/hybrid retrieval,
recall Q&A, queued Markdown digests, and read-only MCP tools. All commands run from this folder.

## Start and configure recall

```sh
uv sync
export BROWSERBUDDY_LLM_BASE_URL=http://localhost:11434/v1
export BROWSERBUDDY_LLM_MODEL=your-tool-capable-model
# Set BROWSERBUDDY_LLM_API_KEY if the selected endpoint requires it.
uv run buddy serve
```

Use a second terminal with the same BROWSERBUDDY_DATA_DIR when overridden:

```sh
uv run buddy ask "What did I read about quantum computing last week?"
uv run buddy ask "quantum computing" --session 1 --json
uv run buddy ask "quantum computing" --since 2026-11-01T00:00:00Z --until 2026-11-07T00:00:00Z
uv run buddy digest 1
uv run buddy jobs
uv run buddy jobs retry 3
```

The backend has no default cloud endpoint. Retrieved text is sent to the configured LLM
when recall or digest generation runs. Configuring a remote endpoint therefore sends that
evidence off the machine. The endpoint must support tool calling and structured answers;
OpenAI-compatible HTTP alone does not guarantee support. Do not put API keys in command URLs.
Set BROWSERBUDDY_TIMEZONE to an IANA timezone; UTC is the default. Last week means the
previous Monday-to-Monday calendar week. Explicit ranges are inclusive start/exclusive end.

## Optional local semantic search

```sh
uv sync --extra semantic
export BROWSERBUDDY_SEMANTIC_ENABLED=true
uv run buddy serve
# In a second terminal:
uv run buddy index prepare
```

Preparation explicitly downloads sentence-transformers/all-mpnet-base-v2 and records the
resolved revision and tokenizer/model capacity under the data directory. Normal startup
uses cached files only. BROWSERBUDDY_EMBEDDING_MODEL and BROWSERBUDDY_EMBEDDING_REVISION
override the selection. Preparing a different revision creates a new index generation.
Inference runs on CPU. The semantic extra is large: distribution-specific Torch wheels
may include accelerator libraries even though BrowserBuddy uses CPU inference.

Reconciliation discovers existing and newly captured pages/history every 60 seconds.
Partial documents are not searchable as vectors. Missing models/extensions, query timeout,
or indexing backlog preserve lexical search, with capability warnings in tool/API results.
The old `buddy search` command remains lexical; hybrid retrieval is available through the
memory API, recall tools, and product MCP.

## Digests and durable jobs

`buddy digest ID` returns the current artifact or queues a snapshot and reports its job ID.
Run it again after the job finishes. `--regenerate` schedules replacement while retaining
previous output. Session IDs are numeric, and active-session requests do not end recording.
Digest files live under `digests/<session-id>/` inside the configured data directory.

Automatic digests require BROWSERBUDDY_AUTO_DIGESTS=true plus LLM configuration. Enabling
this includes all historical ended sessions and may make many paid calls to a remote model.
Implicit closure when starting a new session is covered. Defaults perform no automatic LLM
calls. To change backend settings, restart the server with the new environment.

Jobs survive restarts. Transient failures retry with backoff; terminal failures remain visible
in `buddy jobs` and can be explicitly retried. Capture is independent of model availability.
Old index generations and orphan/previous digest artifacts are retained; garbage collection
is not implemented. Dwell remains approximate, and long evidence is truncated with counts.

## Product MCP

Run `uv run buddy mcp` as a stdio server in your MCP client configuration, with this folder
as cwd and the same data-directory/host/port settings as `buddy serve`. The backend must be
running. The adapter reads its shared secret from the local data directory; stdout contains
only MCP protocol messages. No additional network listener is created.

Tools: search_memory, search_history, get_open_tabs, list_sessions, get_session_digest,
get_page, get_screenshots. All are read-only; reading a digest never generates one.
Screenshot results contain metadata and availability, not image bytes or OCR text.
`scripts/browser-mcp` is a separate developer browser-automation server.

## Verification and limits

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run --extra semantic python scripts/benchmark_memory.py
```

Tests use real SQLite/FTS/vector functions and fake external model/network boundaries.
The benchmark uses deterministic 768-dimensional vectors and excludes model inference.
See the [M4 smoke report](../docs/superpowers/smoke/2026-11-06-m4-smoke.md) for actual results
and remaining real-model/platform validation. No dashboard, sidebar, OCR, image analysis,
or HTTP-hosted MCP is provided. Citation checks verify provenance, not factual entailment.
