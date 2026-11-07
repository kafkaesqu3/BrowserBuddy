# M4 — Recall, hybrid search, MCP, and digests

Status: approved by user continuation; implemented on `feat/m4-recall`, acceptance closeout open.
Validation: [smoke report](../smoke/2026-11-06-m4-smoke.md), updated 2026-11-07.
Date: 2026-11-06. Baseline inspected: `1026e7e` (remote fetch unavailable).
Parent: [original design](2026-07-14-browserbuddy-design.md).
Plan: [implementation tasks](../plans/2026-11-06-m4-recall.md).

## Intent and scope

Turn captured browsing into cited recall answers and regenerable Markdown session digests.
Recording must continue when enrichment or model endpoints fail. Preserve ingest protocol v1
and existing CLI behavior. M3a enables text recall; M3b links/dwell/screenshots enrich it.

Included: typed retrieval, durable jobs, local page/history embeddings, hybrid search,
recall agent, CLI, stdio MCP, and session digests. Excluded: OCR, image understanding,
binary screenshot delivery to models, dashboard, sidebar, SSE, HTTP MCP hosting, video,
encryption, retention/GC, region classification, and general URL canonicalization.
Recall searches across sessions; separate cross-session synthesis artifacts remain deferred.

## Approaches and proposed decisions

The backend-owned approach was accepted for implementation by user continuation.

1. **Backend-owned service and worker (recommended):** `buddy serve` owns model resources
   and jobs. New CLI/MCP operations call authenticated local HTTP. New commands require the
   backend; existing offline commands remain available. One process owns enrichment.
2. Independent CLI/MCP workers allow offline operation but duplicate models and complicate
   job ownership and writes. Defer this architecture.
3. A broker/vector service isolates work but adds deployment machinery beyond this project.

Use source reconciliation instead of rewriting all ingest transactions to enqueue jobs.
A committed capture is durable intent: a later scan discovers it even after a crash.

## Lifecycle and durable work

FastAPI lifespan owns one reconciliation loop and one job runner. Blocking inference runs
outside the event loop in a dedicated executor with one inference slot. Query embedding
shares that slot, with a 5-second timeout including slot wait; timeout returns FTS results.
Never hold a SQLite transaction across inference, HTTP, or file I/O.

Each new synchronous DB operation owns a connection in its executing thread, with WAL,
foreign keys, and 5-second busy timeout. Never share the existing ingest Storage connection
with worker threads. Run migrations at startup before workers/HTTP traffic. Enforce one
backend owner per data directory with an OS-held lock, not merely a lock-file existence test.
Stop claiming at shutdown. A completed CPU operation cannot publish after losing its claim.
Restart resets running jobs to pending under exclusive ownership.

Append migrations after v6, with these invariants (column types follow existing conventions):

| Table | Required fields/invariants |
|---|---|
| enrichment_jobs | id, kind (embed_page/embed_history/digest), source_id, source_version, config_id, origin (automatic/explicit), state (pending/running/done/failed), attempt, next_attempt_at, claim_token, error_code, timestamps; unique kind/source/version/config |
| embedding_configs | id = canonical config hash; model and immutable revision, tokenizer revision, dimensions, effective chunk size, overlap, normalization, format version |
| memory_chunks | id, source_kind/id/version, config_id, ordinal, kind (body/metadata), text, body character offsets, float32 vector BLOB; unique source/version/config/kind/ordinal |
| indexed_documents | source_kind/id/version, config_id, chunk_count, completed_at; unique source/version/config |
| digests | id, session_id FK, source_version, config_id, state, markdown_path, immutable evidence_manifest JSON, summary_checkpoint JSON, generated_at, error_code; unique session/version/config |

Validate polymorphic page/history references in services. No source-deletion API is added.
Store vectors in ordinary tables and use sqlite-vec scalar distance for exact filtered search
initially. This supports changing dimensions without dimension-specific table migrations.
Only vectors matching the active config/dimensions are searchable. Measure performance.

Reconcile on startup and every 60 seconds. Keyset batches of 200 sweep pages, history, and
eligible ended sessions; yield between batches and finish a sweep before starting another.
Old data is included. Insert absent jobs idempotently; never reset failed jobs every sweep.
Disabled capabilities create no jobs or attempts; later configuration discovers old data.
Source version hashes title/URL/text; history has title/URL only. Version changes create jobs.

Publish all chunks, completion marker, and job success in one transaction after rechecking
source/config/claim identity. Partial chunks never become searchable. New configurations
reindex without mixing generations. FTS covers records awaiting embeddings; old vectors
remain until a later cleanup feature.

Retry transient HTTP/timeouts/DB contention at 5, 30, 120, 600, 3600 seconds; fail after the
sixth failed execution. Auth/config/invalid-vector failures fail immediately. Errors expose
codes and sanitized messages, never secrets, page bodies, or raw provider dumps.
`buddy jobs` exposes state and `buddy jobs retry ID` requeues failed work explicitly.

## Retrieval and evidence contracts

Define Pydantic models in `memory_models.py`, shared across HTTP, agent, and MCP.
All stored timestamps are UTC milliseconds; time filters are [since, until).
Reject since >= until. Apply filters inside lexical/vector queries before candidate limits.

- SearchRequest: query trimmed, max 2000 chars; optional since/until/session_id; limit 20,
  valid 1..100. History rejects session_id. Empty query returns empty results.
- SearchHit: source_kind/id, session_id/name when applicable, URL/title, captured_at,
  excerpt, citation, optional chunk_id, score, collapsed_count.
- Citation: `page:<id>` or `history:<id>`, optional chunk_id and character span. Resolve
  stored evidence rather than fetching current web content.
- SearchResponse: hits, truncated, mode (fts/hybrid), capabilities, warnings.
- PageEvidence: id/session, URL/title, visited_at, text slice, offset/next_offset,
  truncated, approximate dwell_ms, html_available.
- OpenTab: current stored tab metadata, explicitly snapshot data rather than guaranteed live.
- SessionPage/ScreenshotPage/JobPage: items, next_cursor, truncated; ascending numeric ID,
  after_id default 0, limit default 20 and maximum 100.
- ScreenshotInfo: id, page_visit_id, captured_at, kind, blob_key, available. Missing files
  mark individual items unavailable instead of failing the result.
- Capabilities: semantic state (disabled/unavailable/indexing/ready), indexed/eligible
  counts for the active generation, LLM configured, OCR false, screenshots metadata-only.

Seven shared read-only tool signatures:

```
search_memory(request: SearchRequest) -> SearchResponse
search_history(request: SearchRequest) -> SearchResponse
get_open_tabs() -> list[OpenTab]
list_sessions(*, after_id: int = 0, limit: int = 20) -> SessionPage
get_session_digest(session_id: int) -> DigestResult
get_page(page_visit_id: int, *, offset: int = 0, max_chars: int = 12000) -> PageEvidence
get_screenshots(page_visit_id: int, *, after_id: int = 0, limit: int = 20) -> ScreenshotPage
```

get_page accepts offset >=0, max_chars 1..24000; offset beyond text returns an empty slice.
Unknown IDs are not-found errors. Digest reads never trigger generation.

Select up to 200 source documents per lexical/vector branch. Aggregate best chunk per
document before limiting. Fuse with RRF sum(1/(60 + one-based rank)); tie by timestamp
descending then ID ascending. Collapse exact URL/content-hash duplicates within a session
after fusion, retaining the best hit and count. Distinct content remains separate. History
visits remain distinct. Fetch limit+1 after collapse; disclose candidate-cap truncation too.
FTS retains existing quoted-token OR/BM25 behavior.

Timezone configuration defaults to UTC. Provide the agent current time/timezone; last week
means the previous Monday-to-Monday calendar week in that zone. Explicit CLI dates require
timezone-bearing ISO timestamps. Return the interpreted interval. Natural-language parsing
is evaluated with fixed fixtures, not claimed universally deterministic. Explicit filters
are enforced by service/tool context and cannot be widened by model arguments.

## Embeddings and dependencies

Proposed local model: sentence-transformers/all-mpnet-base-v2. Select and record an immutable
revision at implementation. Explicit `buddy index prepare` downloads/caches weights;
normal startup never downloads them. Disable remote custom code. No remote embedding API.
Preparation caches the selected revision; enable indexing separately with semantic_enabled=true.

Target body size 512 tokens, overlap 64, title prefix at most 32 tokens. Cap body size to
the actual tokenizer/model capacity after prefix and special tokens; never silently truncate.
Reject a capacity <=64. Store effective size/config and original character offsets using
tokenizer offset mappings. Metadata uses title/URL; omit unavailable author/description.
History gets a metadata vector only. Batch 16, normalize float32, reject wrong dimensions,
NaN/infinity/zero vectors. Query and document encoding use the same config generation.
Unavailable native extension/model or query timeout falls back to FTS with clear warnings.

Add sentence-transformers/sqlite-vec as optional semantic dependencies, Pydantic AI's
OpenAI-compatible support for recall, FastMCP for stdio, and httpx as a runtime dependency.
Justify a locking dependency only if portable stdlib locking cannot meet the requirement.
Check current stable releases and lock with uv when implementing; no dependencies are
installed and no unverified version pins are introduced by this design.

## Agent, HTTP, CLI, and MCP

All new HTTP routes require existing X-BB-Secret auth and default to loopback. New clients
read the existing local secret; never send it in URLs. No default cloud LLM destination.

| Route | Contract |
|---|---|
| POST /api/memory/search | SearchRequest -> SearchResponse |
| POST /api/history/search | SearchRequest -> SearchResponse |
| GET /api/tabs | list[OpenTab] |
| GET /api/sessions | SessionPage |
| GET /api/pages/{id} | PageEvidence, offset/max_chars |
| GET /api/pages/{id}/screenshots | ScreenshotPage |
| GET /api/sessions/{id}/digest | DigestResult |
| POST /api/sessions/{id}/digest | queue snapshot/regeneration, DigestResult, 202 |
| POST /api/ask | AskRequest -> RecallAnswer |
| GET /api/capabilities | Capabilities |
| GET /api/jobs | JobPage |
| POST /api/jobs/{id}/retry | JobInfo; 409 unless failed |
| POST /api/index/prepare | explicit setup, SetupResult; actionable offline failure |

JobInfo contains id/kind/source/state/attempt/next_attempt_at/error_code. SetupResult contains
config_id, ready boolean, error_code. AskRequest contains question and optional time/session
filters. RecallAnswer contains text, validated citations, interpreted interval, warnings,
status (answered/insufficient_evidence). Configure LLM base_url/model/key explicitly.
Require tool calling; unsupported compatible endpoints produce an actionable capability error.

One Pydantic AI agent uses the seven read-only tools. Treat captured content as untrusted
evidence. No shell/browsing/filesystem/write tools. Disable external telemetry by default.
Maintain a ledger of retrieved evidence; reject unknown citations and mismatched excerpts.
Allow one repair, then fail explicitly. No evidence returns insufficient_evidence.
Provenance validation cannot guarantee factual truth or entailment.

Per run: 8 model requests including repair, 16 tool calls, 48000 evidence characters,
90-second wall limit, 2000 output tokens where supported plus a local 12000-character cap.
Disclose truncation. Retrieved text may leave the machine for the configured LLM; document
this during setup. No model calls until configured and explicitly invoked or auto-enabled.

CLI additions: `buddy ask QUESTION [--since ... --until ... --session ID --json]`,
`buddy digest SESSION_ID [--regenerate]`, `buddy jobs [--json]`, `buddy jobs retry ID`,
`buddy index prepare`, `buddy mcp`. New session selectors use numeric IDs, not the newest
20-session list. Digest reads current output; missing/stale/--regenerate enqueues and prints
status without waiting. Exit 0 accepted/success, 2 invalid input, 1 service/config failure.

FastMCP stdio proxies the seven tools through a shared typed HTTP client. stdout is protocol
only; logs go to stderr. Local-user access to the secret is the trust boundary. No additional
listening socket or mutation tool. Descriptions reflect capabilities at startup; tool results
disclose runtime degradation so stale descriptions do not conceal it.

## Digest lifecycle

Automatic digests require auto_digests=true and a configured LLM; default false. Enabling
auto generation includes historical ended sessions, with cost/data consequences documented.
Explicit requests support active-session snapshots without ending them. Empty sessions emit
a deterministic local No pages captured artifact without an LLM.

Jobs record automatic/explicit origin. An explicit request upgrades an existing automatic
job to explicit. Check enablement/configuration before claiming and before each model call:
disabling auto_digests prevents further automatic calls, leaving such jobs pending without
consuming attempts. An already in-flight HTTP request may finish; its result may checkpoint,
but no next call starts. Explicit requests remain eligible with auto_digests disabled.

Build an immutable manifest in a DB read snapshot: page IDs, complete stored text/title/URL,
content hashes, links/clicks, navigation, dwell, ended_at, capture cutoff. Copy every field
consumed by generation into the manifest; IDs/hashes alone cannot preserve old evidence.
This duplicates text for each digest snapshot, an explicit storage tradeoff until retention
is designed. Do not copy raw HTML or images. Capture cutoff is the maximum included page
visited_at, or session.started_at for an empty session. Observation time is metadata only
and excluded from the version hash. Hash the manifest as source_version; model/endpoint/prompt
identity (never credentials) forms config_id. Store mutable summary checkpoints separately.
Reconciliation catches explicit and implicit closure; replay does not multiply work.
Do not invent paused backend states. Later data produces a stale flag and a new version.

Do not wait for vectors or OCR. Summarize pages chronologically with bounded evidence and
citations. Persist per-page summaries; process at most 100 pages per runner turn, then
continue without consuming a retry. Final synthesis obeys context limits and names omitted
page/character counts. Reads use manifest versions; no changing evidence midway through a job.

Publish via temp file and atomic rename to
digests/<session_id>/<source_version>-<config_id>-<claim_token>.md, then commit ready state.
The database pointer selects the visible artifact; claim-specific paths prevent an obsolete
publisher from overwriting a newer artifact. A restart
between rename and DB publication verifies/reuses or safely regenerates the artifact.
DigestResult: id, state (missing/pending/failed/ready/stale), source_version, stale,
previous_output (boolean, default false), job_id,
error_code, markdown capped at 24000 characters, truncated, markdown_path relative to data
directory. CLI may display the full local artifact; MCP reports truncation without arbitrary
file access. Prefer current source/config output, otherwise latest artifact marked stale.

For --regenerate with the same source/config, atomically reset the existing done/failed job
to pending with explicit origin and clear its summary checkpoint. Retain the previous ready
artifact until its replacement is atomically published; reads return pending plus previous
Markdown marked as previous output. Repeated requests while pending/running return that job
without another reset. Normal reads/reconciliation never reset done work. Resetting a job
invalidates its prior claim token. Publication replaces the visible database pointer only
after the new output has passed validation; failure leaves the previous artifact available.
Old and orphan files remain until a separately designed retention/GC feature.

Sections: pages read/approximate dwell, cited findings, notable unfollowed links, open
questions. Label suggested questions as suggestions. Shared pure score_outbound_links
excludes clicked hrefs and URLs visited anywhere in the session, deduplicates exact href,
and scores before capping to 50 session-wide:

`2*in_main + off_site + novel_before_session + descriptive_anchor - frequency_penalty`.

Descriptive = >=3 words excluding generic click here/read more labels. Frequency penalty
is min(3, distinct source pages - 1). Off-site compares parsed hosts; novelty checks earlier
page/history visits. Ties sort by href. Exclude/count invalid or non-http(s) links. Report
eligible/omitted counts. Use in_main, without depending on future region labels.

## Acceptance and sources

Test recovery after capture commit, partial batches, restart, duplicate delivery, model
outage, temporal filters, citation resolution, distinct recaptures, explicit-filter confinement,
implicit session closure, active/empty digests, and publication crashes. Verify WS ack while
inference is blocked. Fake model/network boundaries; use TestModel/FunctionModel for agents.
Cached real-embedding smoke is separate from deterministic tests; paid endpoint smoke optional.
Measure exact vector retrieval on 10000 documents/100000 chunks, record machine/latency/RAM.
If retrieval exceeds 2 seconds, optimize or explicitly revise this gate before closing M4.

Primary documentation checked 2026-11-06:
- [Pydantic AI providers](https://github.com/pydantic/pydantic-ai/blob/main/docs/models/compatible-apis.md)
- [FastMCP transports](https://gofastmcp.com/docs/deployment/running-server)
- [sqlite-vec Python](https://alexgarcia.xyz/sqlite-vec/python.html)
- [vector functions](https://github.com/asg017/sqlite-vec/blob/main/site/api-reference.md)
- [Sentence Transformers](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html)
- [proposed model card](https://huggingface.co/sentence-transformers/all-mpnet-base-v2)

Native web search and primary-page retrieval were used; Context7/Brave/Exa were unavailable.
This is targeted API verification, not an exhaustive model comparison. Check stable versions
and native-extension platform support again during implementation.

Approval before implementation: backend-required new CLI/MCP, proposed local embedding model,
and explicit opt-in automatic digests. Remaining routine module internals belong to the builder.
