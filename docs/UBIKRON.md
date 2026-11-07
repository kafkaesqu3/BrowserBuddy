# Ubikron — Research Notes & Comparison with BrowserBuddy

**Date:** 2026-08-12
**Purpose:** Competitive analysis and design input

---

## What Is Ubikron?

Ubikron is a **browser-based OSINT investigation workbench** delivered as a
Chrome/Chromium extension backed by a cloud or self-hosted server. It is built
by **Roelof Temmingh** — co-founder of SensePost and creator of Maltego —
through his company **Vortimo Pty Ltd**. Ubikron is his third attempt at the
same core idea over nine years: Vortimo → Skylight/OSINT-Tool → Ubikron. The
first two are deprecated.

The elevator pitch: **your browser becomes a structured intelligence collection
and analysis platform**. Instead of leaving OSINT research scattered across
browser tabs and separate tools, Ubikron auto-archives everything you browse
into a structured case file with entity extraction, one-click enrichments to
200+ OSINT services, a visual relationship graph, and AI-assisted analysis.

**Primary sources:** ubikron.com, servers.ubikron.com, Chrome Web Store (ID:
`edfkaefjonbohokoldemepfolefiplgd`), github.com/ubikron.

---

## Architecture

### Client-Server Split

Ubikron is deliberately split into a browser client and a database server — a
direct fix for the failure mode of its predecessor (Vortimo OSINT-Tool), which
stored everything locally and was "too local to share" — you couldn't hand off
a case between analysts.

```
Chrome Extension (client)
  ├── Capture engine        — auto-archives every page visited
  ├── Entity extractor      — PCRE regex over captured text
  ├── Enrichment router     — fires browser requests to 200+ external services
  └── Graph/UI              — visual entity graph, case management

Server (database + AI)
  ├── Free tier             — Ubikron's cloud servers
  └── Self-hosted (paid)    — Docker stack, your infrastructure, multi-user
```

**Enrichment mechanics matter for OPSEC:** enrichments fire as real browser
requests directly from the analyst's browser. Each pivot to Shodan, Intel X,
Dehashed, etc. reveals the investigator's IP to that service. This is fine for
most corporate use cases; it is a genuine risk for high-OPSEC investigations
(journalism, hostile-nation actors, undercover work).

### Browser Support

Chrome/Chromium only. No Firefox, no Safari. The extension is Manifest V3.

### Deployment Tiers

| Tier | Storage | Users | Cost |
|---|---|---|---|
| Free | Ubikron's servers | Single | Free |
| Self-hosted | Your Docker host | Multi | Paid (no public price) |
| Enterprise | Your infrastructure | Multi | Paid (unlisted) |

Self-hosted includes full-text search, AI-assisted analysis, multi-user access,
and complete data sovereignty. Pro adds the entity graph, enrichment APIs, and
the MCP server.

---

## Feature Set

### Capture (Collection)

- **Always-on passive capture:** every page visited is archived automatically
  — full text, screenshots, and MHTML — unless the domain is excluded or
  capture is paused.
- **MHTML archiving:** preserves page layout, images, and dynamic content in a
  single file. Critical for social media pages, news articles, and anything
  that changes or disappears after capture.
- **Infinite-scroll capture:** captures the full content of paginated feeds
  without manual scroll tricks.
- **PDF save:** on-demand PDF export of any captured page.
- **Screenshot capture:** included in the standard archive.
- **Detailed browsing history:** ambient history sync, shown as a searchable
  timeline (paid add-on for extended history).

**What this enables implicitly:**

- *Takedown-resistant evidence archiving:* auto-capture means evidence exists
  before an account is suspended or a page is deleted. An analyst monitoring a
  fraud campaign doesn't need to manually save anything — it's already in the
  case file.
- *Passive adversary monitoring:* simply browsing threat actor forums or social
  media as part of routine work silently builds a searchable evidence archive.
  No session management required.
- *Timeline reconstruction:* continuous timestamps + captures let you
  reconstruct exactly what you looked at and when — valuable for writing up
  methodology or handing a case to another analyst.

### Entity Extraction

Entity extraction runs over every captured page via PCRE regular expressions.
Default entity types extracted out of the box:

- IPv4 / IPv6 / CIDR
- Domain / URL / website
- Email address
- Phone number
- GPS coordinates
- Cryptographic hash (MD5, SHA variants)
- Cryptocurrency address
- Social media alias / hashtag
- Flight number / ISBN (in some documentation)

Users can add **unlimited custom PCRE extraction rules** (with a noted
performance caveat: too many active regexes slow extraction). The community
list lives at `github.com/ubikron/Advanced-Enrichments`.

**What this enables implicitly:**

- *Cross-case entity correlation:* because all entities go into the same
  database regardless of which case they came from, a search for an IP or hash
  finds every case and every page it ever appeared in. Ubikron becomes a
  persistent OSINT memory across all investigations over time, not just a
  per-case tool.
- *Implicit threat hunting:* an analyst who routinely browses threat feeds,
  security dashboards, and incident reports is passively building a searchable
  entity library of every IOC they've ever seen, without deliberate effort.
- *Full-text + OCR search as entity pivot:* searching "192.168.1.1" across
  all captures finds that IP even if it appeared inside a screenshot, not just
  in page text.

### Enrichment (200+ OSINT Integrations)

The enrichment model: select an entity (or right-click any string on a captured
page), choose a service, and Ubikron opens the lookup in your browser, routed
to that service with the entity pre-populated. Named integrations include:

Shodan, Intel X, Epieos, OSINT Industries, Dehashed, WhatIsMyName,
DomainTools, ScamSearch, Internet Archive (Wayback Machine), and 190+ more.

Selection criteria for built-in enrichments: the service must be usable for
free, without registration, and runnable via a URL parameter. The full curated
list is community-extensible via the GitHub repo.

Pro adds **direct API integration** for services that support it, replacing the
browser-tab approach with programmatic queries.

**What this enables implicitly:**

- *Manual pivot trees:* entity extracted → enriched via Shodan → new IPs/ports
  from Shodan results → right-click → enriched again. Ubikron doesn't automate
  the chain but makes each step frictionless, enabling deep manual pivot trees
  with low overhead.
- *Enrichment composition:* combine enrichments from multiple services on the
  same entity. An email address gets queried against Epieos (account recovery),
  Dehashed (breach databases), and WhatIsMyName (username search) in three
  clicks. Results feed back into the capture database as new pages.

### Analysis

**Entity relationship graph (Pro):** A Maltego-style interactive visual graph
that maps extracted entities and their relationships across all captures in a
case. This is the tool's marquee differentiator — it surfaces structural
connections that are invisible in a flat search result list.

**AI assistant:**
- Built-in chat with a preloaded **OSINT-specific prompt library**
- AI-generated insights and a "Find++" feature (not independently detailed)
- **AI image identification** and **reverse image search** across multiple
  engines — more capable than generic AI assistants for investigation work
- **AI file analysis** — drop a file into the case and interrogate it
- Full-text + OCR search across all captured text and screenshots

**MCP server (Pro, beta):** Exposes the investigation database and entity graph
to AI agents via the Model Context Protocol. An AI agent connected to the MCP
server can query the case, pivot across entities, and surface findings without
the analyst driving each step manually.

**What this enables implicitly:**

- *Autonomous agent investigation:* with the MCP server, an AI agent could be
  handed a case, autonomously pivot through entities, trigger enrichment
  lookups, and summarize findings. This is speculative but architecturally
  enabled by the combination of entity graph + MCP.
- *Evidence chain of custody:* MHTML archives with timestamps are forensically
  defensible — this is what the page looked like at this exact moment. Relevant
  for legal proceedings, journalism, and corporate investigations.

### Case Management

- **Projects:** all captures and entities organized into named investigation
  projects
- **Tags:** user-defined tags to classify and correlate material across a case
- **Investigation plans:** built-in editor for planning notes and task lists
- **Clippings:** tag and annotate selected portions of captured pages
- **Multi-user (self-hosted):** team members share cases, divide investigation
  work, and see each other's entity extractions

### Capture Control

- **One-click pause:** stop all capture instantly
- **Domain exclusions:** protect-listed domains are never captured (personal
  accounts, banking, etc.)
- No equivalent to BrowserBuddy's explicit opt-in session model — Ubikron's
  default is opt-out

---

## Data Handling & Privacy

| Aspect | Free Tier | Self-Hosted |
|---|---|---|
| Storage location | Ubikron's servers | Your infrastructure |
| Data sovereignty | None | Full |
| Multi-user | No | Yes |
| Encryption at rest | Not disclosed | Not disclosed |
| Data residency | Undisclosed | Your choice |

**Permissions:** The extension requires broad tab access. chrome-stats flags
it: *"requires a lot of sensitive permissions — exercise caution before
installing."* Vendor states it is not a tracking or advertising tool.

**Historical note:** The predecessor (Vortimo OSINT-Tool) was strictly
local-only — "all data stored locally and does not leave your computer."
Ubikron deliberately traded that guarantee for a client-server architecture to
enable collaboration and case hand-off. Self-hosting is the way to recover
data sovereignty, but it requires a paid tier.

**OPSEC gap:** Because enrichments fire as real browser requests, each pivot
reveals the analyst's IP to the enrichment service. For most corporate/civilian
cases this is irrelevant; for investigative journalism or high-value
counterintelligence targets it is a real exposure.

---

## Open Source Status

The product is **proprietary**. The GitHub org (`github.com/ubikron`) exists
but hosts documentation and community-extensible lists, not product source:

- `ubikron/Advanced-Enrichments` — the full enrichment + extraction list
- `ubikron/Awesome-AI-OSINT`, `awesome-osint-chrome-extensions`, `OSINT-Books`,
  `OSINT-Conferences`, `OSINT-Companies`, `Timestamping-the-web`

No source code is published. Data formats are undisclosed — there is no
documented export to STIX, MISP, GraphML, or other standard formats, which
creates vendor lock-in risk for enterprise deployments.

---

## Pricing

Freemium. No public pricing for paid tiers (Pro beta closed; enterprise by
contact).

- **Free:** core capture, entity extraction, enrichments, MHTML saving, basic
  history, AI assistant (limited)
- **Paid add-ons:** extended history, full AI features
- **Pro:** entity graph, enrichment APIs, MCP server — *beta closed, not yet
  publicly launched as of 2026-08*
- **Self-hosted / enterprise:** Docker stack, multi-user — paid, price unlisted

---

## Target Users

- Professional OSINT investigators and intelligence analysts
- Cyber threat intelligence (CTI) researchers
- Corporate due diligence and digital investigation teams
- Investigative journalists
- Academic researchers requiring structured evidence capture

Not aimed at: general consumers, developers wanting browser automation, or
users wanting an AI coding assistant. The AI in Ubikron is investigation-scoped,
not general-purpose.

---

## BrowserBuddy vs. Ubikron — Feature Comparison

### Positioning

| | BrowserBuddy | Ubikron |
|---|---|---|
| **Core job** | Personal research recall — "what did I read about X?" | Structured OSINT investigation — "who is this entity and what connects them?" |
| **Mental model** | Research diary with AI memory | Digital case file with entity graph |
| **Primary user** | Developer, researcher, knowledge worker | OSINT analyst, CTI investigator, journalist |
| **Workflow trigger** | You explicitly start a named session | Always-on; you opt out per domain |

### Architecture

| | BrowserBuddy | Ubikron |
|---|---|---|
| **Browser** | Firefox-first (Chrome later, cheap) | Chrome/Chromium only |
| **Backend location** | 100% local — `127.0.0.1`, never leaves machine | Cloud by default; self-hosted Docker (paid) |
| **Privacy default** | Strongest possible — local-only always | Cloud-hosted by default |
| **Extension weight** | Thin — all intelligence in the Python backend | Heavier — extraction + enrichment run in browser |
| **Open source** | Yes | No |
| **Stack** | TypeScript + Python/FastAPI/Pydantic AI + SQLite | Proprietary (Docker, internals undisclosed) |
| **Data formats** | Open (SQLite + files on disk) | Undisclosed (possible vendor lock-in) |

### Capture Model

| | BrowserBuddy | Ubikron |
|---|---|---|
| **Capture trigger** | Explicit opt-in (user starts a named session) | Always-on opt-out (paused per domain) |
| **Consent story** | Session scope = cleanest consent model; visible recording indicator; no ambient capture of personal browsing | Broad capture by default; must actively exclude sensitive domains |
| **Session concept** | First-class: named, pausable, linkable, digestible with AI | No session concept — cases are the organizing unit, capture is continuous |
| **Content captured** | Readability.js text + raw HTML + dwell-gated screenshots + full link graph + nav edges | Full text + MHTML + screenshots (all pages automatically) |
| **MHTML archiving** | No | Yes — core artifact; best for volatile/social pages |
| **Infinite scroll** | Not featured | Yes |
| **Dwell time** | Yes — tab-active-time signal per page | Not featured |
| **Navigation graph** | Yes — how you moved between pages, including search-engine provenance | Not featured (entities go into the relationship graph, not navigation paths) |
| **Link graph** | Full outbound + clicked links per page with nav edges | Entities extracted from pages but not navigation topology |
| **History backfill** | Yes — reads `places.sqlite` directly (free) | Detailed history — paid add-on |

### Data Model & Search

| | BrowserBuddy | Ubikron |
|---|---|---|
| **Primary unit** | Page visit within a named session | Page capture within a case/project |
| **Graph type** | Navigation graph — how pages connect via your clicks and searches | Entity relationship graph — how OSINT entities (IPs, people, domains) connect |
| **Entity extraction** | Not featured | Core feature — 15+ default entity types via PCRE, unlimited custom |
| **Semantic search** | Yes — embeddings via local sentence-transformers | Not featured |
| **Full-text search** | FTS5 + vector hybrid with session citations | Full-text + OCR across all captures |
| **Cross-case query** | Search spans captured sessions; optional session/time filters | All entities in one DB — implicit cross-case correlation |
| **Storage** | SQLite + content-addressed blobs (open, portable) | Proprietary (undisclosed) |

### AI Capabilities

| | BrowserBuddy | Ubikron |
|---|---|---|
| **AI model** | Model-agnostic — any OpenAI-compatible endpoint (Ollama, OpenRouter, LiteLLM) | Bundled assistant (model undisclosed) |
| **Recall Q&A** | Yes — the core feature; answers "what did I read about X?" with citations | No |
| **Post-session digest** | Queued snapshots; automatic generation is opt-in | No equivalent |
| **OSINT prompt library** | No | Yes — preloaded OSINT-specific prompts |
| **Image identification** | Not implemented; screenshots are stored and listed as metadata | AI image ID + multi-engine reverse image search |
| **File analysis** | Not featured | Yes |
| **AI insights** | Recall Q&A + digest generation | Investigation insights + "Find++" (not fully documented) |
| **MCP server** | Yes — typed tools: search_memory, get_open_tabs, list_sessions, get_page, get_screenshots | Yes (Pro, beta) — investigation graph + entities |
| **Agent autonomy** | MCP exposes memory; agents can query passively | MCP + entity graph enables richer autonomous pivoting |

### OSINT-Specific Features

| | BrowserBuddy | Ubikron |
|---|---|---|
| **OSINT enrichments** | None | 200+ (Shodan, Intel X, Dehashed, Epieos, DomainTools, …) |
| **Enrichment model** | N/A | One-click browser request to external service |
| **API enrichments** | N/A | Yes (Pro) |
| **Entity extraction** | None | 15+ default types + unlimited custom PCRE |
| **Relationship graph** | None | Maltego-style entity graph (Pro) |
| **OPSEC** | Excellent — no outbound calls except configured LLM | Risk — enrichments expose analyst's IP to third parties |
| **Evidence preservation** | Raw HTML + screenshots, content-addressed | MHTML + screenshots; forensically defensible timestamps |
| **Case management** | Named sessions; no project-management feature | Cases + projects + tags + task lists |
| **Collaboration** | Single user (local) | Multi-user (self-hosted) |

### Privacy & Deployment

| | BrowserBuddy | Ubikron |
|---|---|---|
| **Data residency** | Local only — always | Cloud (free), your server (paid) |
| **Private window handling** | Never captured | Not stated |
| **Sensitive-content firewall** | Roadmap (regex rejection of keys/tokens at ingest) | Not disclosed |
| **Extension permissions** | Standard (tabs, history, storage, webNavigation) | Broad (all page access); flagged "sensitive" by chrome-stats |
| **Open formats** | Yes | No (undisclosed, potential lock-in) |
| **Licensing** | Open source | Proprietary |

### Pricing & Maturity

| | BrowserBuddy | Ubikron |
|---|---|---|
| **License** | Open source | Proprietary |
| **Cost** | Free (self-hosted) | Freemium; paid tiers for AI/history/graph/self-host |
| **Self-host** | Always — the only mode | Paid tier only |
| **Install base** | Early / in development | Early / niche; low Chrome Web Store review count |
| **Maturity** | Active development | Active; predecessor tools deprecated |
| **Firefox** | Yes (primary) | No |

---

## Where Each Tool Is Clearly Stronger

### BrowserBuddy wins on:

- **Privacy** — local-only by default, forever. No cloud tier, no data leaving
  the machine. The strongest possible default for a tool that captures
  everything you read.
- **Consent model** — explicit opt-in sessions with visible recording indicator
  solves a problem that has sunk other tools (Recall's launch disaster, Memex's
  retreat from index-everything). Users know exactly what is being captured.
- **Firefox** — meaningful for privacy-conscious users and OSINT analysts who
  use Firefox-specific privacy tooling.
- **Recall Q&A** — answering "what did I read about X last week?" is
  BrowserBuddy's core job. Ubikron has no equivalent.
- **Research session narrative** — a named session produces a coherent artifact:
  what you looked at, in what order, via what navigation path, with a generated
  digest. This is a unique data model with no Ubikron equivalent.
- **Navigation graph** — tracking how you *moved* through the web during a
  research session, with search-engine provenance, is a distinct capability
  from Ubikron's entity relationship graph.
- **Semantic search** — local embeddings enable fuzzy recall across sessions
  without exact keyword matches.
- **Model-agnostic AI** — use any OpenAI-compatible endpoint: Ollama (fully
  offline), OpenRouter, your organization's LLM endpoint. Ubikron's AI model
  is bundled and undisclosed.
- **Open source + open formats** — data is always portable; no vendor lock-in;
  extraction and embedding processing can be improved and
  re-run over historical data.

### Ubikron wins on:

- **OSINT enrichments** — 200+ one-click pivots to external services is a
  massive capability BrowserBuddy has no answer for. For investigative work
  this is the most important single feature.
- **Entity extraction** — automatically pulling structured entities (IPs, emails,
  hashes, crypto addresses, GPS coords) from every captured page is a
  fundamentally different data model from BrowserBuddy's text-based approach.
- **Entity relationship graph** — Maltego-style visual link analysis is the
  tool's signature capability. There is nothing like it in BrowserBuddy.
- **MHTML archiving** — better evidence fidelity for volatile pages (social
  media, pages that change after capture). BrowserBuddy stores raw HTML but
  MHTML is a stronger archival format for visual fidelity.
- **Infinite scroll capture** — automatically captures full paginated feeds
  without analyst intervention.
- **Always-on passive capture** — for analysts whose browsing *is* their
  intelligence work, not needing to manage session start/stop is a real UX
  advantage.
- **Multi-user collaboration** — team investigations sharing a case database are
  not possible in BrowserBuddy.
- **AI image analysis** — AI-driven image identification and multi-engine reverse
  image search goes beyond BrowserBuddy's screenshot-storage support.
- **Evidence chain of custody** — MHTML + timestamps are forensically defensible
  in a way that serves legal proceedings and journalism.

---

## Potential Cross-Pollination Ideas

Features or patterns in Ubikron worth considering for BrowserBuddy, evaluated
against BrowserBuddy's design principles:

### Worth evaluating:

**MHTML archiving alongside raw HTML.** BrowserBuddy already stores raw HTML
content-addressed. Adding optional MHTML generation would improve archival
fidelity for pages with heavy CSS/images — particularly useful when re-rendering
sessions or surfacing page previews in the dashboard. Low architectural cost
since content is already stored.

**Infinite-scroll capture.** A research session on a long news article or Reddit
thread currently captures what was in the DOM at extraction time. Dwell-gated
re-extraction (already planned for SPA handling) partially covers this, but an
explicit scroll-to-bottom trigger during active sessions would improve
completeness. Consistent with the existing dwell/recapture architecture.

**Cross-session entity search.** BrowserBuddy's FTS5 + vector search already
spans all sessions. Adding a lightweight extraction pass (at minimum: domains,
IPs, email addresses via simple regex) would enable "find all sessions where I
saw this IP" — a natural extension of the existing search capability that
doesn't require the full Ubikron entity model.

### Probably not right for BrowserBuddy:

**Always-on passive capture.** This would undermine BrowserBuddy's explicit
session model, which is its strongest consent story and a core design choice.
The prior-art analysis at design time identified this as a solved problem by
other tools (screenpipe, Ubikron) and an *unsolved* one: Memex retreated from
index-everything; Ubikron's free tier stores data on someone else's servers.
BrowserBuddy's session-scoped capture remains the right architectural choice.

**OSINT enrichments.** Adding 200 external-service enrichments would turn
BrowserBuddy into a different product. The target user (knowledge worker,
researcher, developer) does not need to pivot an IP to Shodan. This is Ubikron's
domain; BrowserBuddy should stay out of it.

**Multi-user collaboration.** The architecture is explicitly local and
single-user. This is a feature, not a limitation.

---

## Summary Verdict

Ubikron and BrowserBuddy are built on the same substrate — browser extension
plus backend, capturing what you browse, making it searchable and AI-queryable
— but they solve different problems for different users and have taken opposite
positions on the most important design axes.

| Axis | BrowserBuddy | Ubikron |
|---|---|---|
| **Capture intent** | What did I learn? | What did I find? |
| **Data model** | Temporal narrative (sessions, navigation paths) | Entity graph (who/what/how connected) |
| **Privacy default** | Local-only, always | Cloud-hosted, opt-in self-hosting |
| **AI role** | Recall assistant | Investigation analyst |
| **Consent story** | Explicit sessions, visible indicator | Always-on, domain exclusions |
| **User** | Knowledge worker / developer | OSINT investigator / CTI analyst |
| **Openness** | Open source, open formats | Proprietary, undisclosed formats |

They are **not direct competitors**. An OSINT analyst might use both: Ubikron
for entity-driven active investigations, BrowserBuddy for ambient research
recall across their general information diet. The MCP server is the one place
they genuinely converge — both expose their data to AI agents — and that
convergence will deepen as autonomous agent use matures.

The clearest differentiation BrowserBuddy should lean into: **local-first
privacy + Firefox + research session narrative + model-agnostic recall Q&A**.
None of those are Ubikron strengths, and together they form a coherent position
that no other tool in the space occupies.
