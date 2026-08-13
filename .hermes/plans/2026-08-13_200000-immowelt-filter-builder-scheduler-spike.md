# Immowelt Filter Builder and Scalable Scheduler Feasibility Plan

> **For Hermes:** This is a feasibility investigation. Do not change the production monitoring path until the spike verdict is recorded and approved.

**Goal:** Prove whether PotsdamBot can create correct Immowelt searches from Telegram parameters and monitor up to 100 filters without opening many tabs simultaneously or overloading Immowelt.

**Architecture:** Separate the feature into (1) a deterministic filter model and URL builder, (2) browser-based validation against the real Immowelt page, and (3) a persisted due-task queue. The Chrome extension remains the page executor because direct HTTP currently returns 403. Unique canonical searches are collected once and their listings are distributed to all matching owners.

**Tech Stack:** Python 3.11/aiohttp/SQLite in `C:\opt\check-Wohnung`; Chrome Manifest V3 extension; Python Telegram bot in Docker; JavaScript test harness or Node syntax checks; unittest/pytest.

---

## Current evidence

- The receiver already persists `user_id`, `title`, `search_url`, initialization state, last check time, and per-filter seen listings.
- The extension currently calls `/api/housing/tasks`, then iterates Immowelt tasks sequentially with one temporary tab at a time. It does not open 100 tabs simultaneously.
- All active filters are nevertheless started as one batch every 15-minute slot; one slow/erroring task can prevent later tasks from running.
- A temporary tab waits up to 60 seconds for page load and 45 seconds for result cards, so worst-case throughput of one worker is far below 100 filters per 15 minutes.
- Direct HTTP requests to both tested Immowelt search URLs returned HTTP 403, so browser execution is required unless a supported public API is discovered.
- Current known URL parameters include `distributionTypes`, `estateTypes`, `locations`, `numberOfRoomsMin`, and `spaceMin`. Price and location encoding still need real-page validation.

## Feasibility questions, ordered by risk

| # | Spike | Given / When / Then | Risk |
|---|---|---|---|
| 001 | Real URL round-trip | Given Telegram parameters, when a URL is generated and opened in the working Chrome profile, then the page reports the same city/type/rooms/area/price and loads cards | High |
| 002 | Queue throughput | Given 10/50/100 unique searches and measured page timings, when tasks are scheduled with bounded concurrency, then every due task finishes inside its service window without simultaneous tab explosion | High |
| 003 | Canonical deduplication | Given multiple owners with equivalent searches, when tasks are built, then one page collection fans out to every owner while preserving per-filter baselines and delivery history | Medium |
| 004 | Telegram flow | Given an allowed user, when they answer the wizard and confirm the preview, then a structured pending filter is saved and activated only after browser validation | Medium |

## Spike 001: Prove URL generation against the real page

**Files:**
- Create: `C:\opt\check-Wohnung\spikes\001-immowelt-url-roundtrip\README.md`
- Create: `C:\opt\check-Wohnung\spikes\001-immowelt-url-roundtrip\cases.json`
- Create: `C:\opt\check-Wohnung\spikes\001-immowelt-url-roundtrip\probe.js`
- Do not modify production `background.js`.

**Cases:**
1. Potsdam, rent, apartment+house, rooms >= 2, area >= 65.
2. Potsdam, rent, apartment, rooms >= 3, area >= 70, price <= 1500.
3. A second supported city or postal code to test location encoding.
4. Invalid/unknown location to observe the page's failure mode.

**Procedure:**
1. Define a structured parameter schema and deterministic URL builder in the disposable probe.
2. Open one case at a time in the existing Chrome profile, never in parallel.
3. Extract `location.href`, heading, result count, visible filter labels/controls, blocked status, and load duration.
4. Compare requested versus applied values and record exact mismatches.
5. Reload each generated canonical URL once to prove persistence.
6. Close every temporary tab in `finally`.

**Acceptance:** All supported fields round-trip correctly in at least three cases. If location or price cannot be proven from the page, verdict is PARTIAL and that field must not be exposed in the production wizard yet.

## Spike 002: Measure and design the queue

**Files:**
- Create: `C:\opt\check-Wohnung\spikes\002-immowelt-queue-throughput\README.md`
- Create: `C:\opt\check-Wohnung\spikes\002-immowelt-queue-throughput\simulate.py`
- Read-only timing input from spike 001 and current extension storage/logs.

**Procedure:**
1. Measure p50, p95, and failure timeout for at least 10 real sequential page checks spread over time; do not generate rapid traffic merely for benchmarking.
2. Simulate 10, 50, and 100 unique searches for target intervals of 15, 30, and 60 minutes.
3. Compare worker limits 1 and 2. Do not recommend more than 2 browser tabs without a separate block-rate experiment.
4. Model retry backoff (5, 15, 30, 60 minutes), random jitter, and a per-host minimum start gap.
5. Calculate queue lag and required concurrency from measured timings.

**Capacity formula:**

`required_workers = ceil(unique_searches * p95_seconds / target_window_seconds)`

Preliminary examples for 100 unique searches per 15 minutes:
- 8 seconds/search: ~1 worker;
- 15 seconds/search: ~2 workers;
- 30 seconds/search: ~4 workers;
- 45 seconds/search: ~5 workers.

These are arithmetic bounds, not proof that Immowelt tolerates the traffic.

**Acceptance:** Produce a recommended service interval and worker count whose p95 queue lag fits the window. If 100 unique searches cannot safely fit 15 minutes, explicitly degrade the promise (for example, 30–60 minutes under load) rather than increase tabs aggressively.

## Spike 003: Canonical search and fan-out design

**Proposed production schema (do not implement during spike):**

- `housing_searches`: canonical query JSON, canonical URL, query hash, validation status/error/time, active, next_run_at, lease_until, last_started_at, last_finished_at, failure_count.
- `housing_filter_subscriptions`: owner user ID, title, search ID, active, initialized, created/updated time.
- Existing `housing_seen_items` remains subscription/filter scoped so each new owner receives a silent baseline independently.

**Rules:**
1. Normalize ordering, case, decimal values, object types, and location identifiers before hashing.
2. One `housing_searches` row per unique normalized query.
3. One browser collection creates one listing snapshot.
4. Receiver evaluates that snapshot for every active subscription and preserves independent deduplication/baseline.
5. Ownership checks remain mandatory for user management.

**Acceptance:** Unit-level prototype proves two users with the same query produce one due browser task but two independent subscription outcomes.

## Spike 004: Telegram creation flow design

**Proposed flow:**

1. `➕ Додати Immowelt`
2. Choose `🧭 Налаштувати через бота` or `🔗 Вставити готове посилання`.
3. Collect title, city/postal code, distribution type, estate types, minimum rooms, minimum area, maximum price.
4. Show a complete preview.
5. On confirmation save as `pending_validation`.
6. Browser validator checks the real page.
7. Only a validated search becomes active; failure returns a precise message and leaves no active task.
8. First successful collection creates a silent baseline.

**Acceptance:** No unvalidated generated URL is added to active monitoring.

## Recommended production scheduler if spikes validate

- Alarm wakes every minute, but does not run all tasks.
- Receiver returns only tasks with `next_run_at <= now`, ordered by lateness, with a small limit.
- Task is atomically leased before opening Chrome so overlapping alarms cannot duplicate it.
- Extension keeps `MAX_CONCURRENCY=1` initially; possibly 2 only after measured evidence.
- A random 5–20 second jitter and a minimum host gap spread requests.
- Success sets `next_run_at = completed_at + interval`.
- Block/403/challenge triggers exponential backoff and a single rate-limited administrator alert.
- One failed task never aborts the loop; each task has its own `try/catch`.
- Queue metrics: due count, oldest lag, running count, p50/p95 duration, recent block rate.
- Capacity protection: when queue lag exceeds the target, extend intervals or reject new unique searches with a clear administrator warning; never spawn unbounded tabs.

## Verification before implementation approval

- Real Chrome screenshots or extracted page state prove filter round-trip.
- No secrets/cookies are recorded.
- No production DB or extension is mutated by spikes.
- No more than one experimental Immowelt tab is open at once.
- Verdicts `VALIDATED`, `PARTIAL`, or `INVALIDATED` are written for all four spikes.
- Final architecture recommendation includes measured capacity for 10/50/100 unique searches and distinguishes users from unique searches.

## Production implementation sequence after approval

1. TDD: structured filter model and canonical URL builder.
2. TDD: DB migration for canonical searches/subscriptions and backwards migration of existing URLs.
3. TDD: due-task leasing API and queue metrics.
4. Extension: bounded worker loop, per-task error isolation, backoff, and jitter.
5. TDD: Telegram wizard and pending validation state.
6. Browser validation endpoint and activation workflow.
7. Silent baseline and fan-out integration tests.
8. Staged rollout: existing 2 filters, then 10 synthetic schedules without extra traffic, then limited real users.
9. Only after stable observation, remove legacy URL-only creation as default; keep it as an advanced fallback.

## Risks and decisions

- Immowelt can change undocumented URL parameters or page selectors at any time.
- Browser automation may be rate-limited; 100 users do not necessarily mean 100 unique searches, so canonical deduplication is essential.
- A strict 15-minute service promise may be unsafe at 100 unique searches on one residential Chrome profile.
- Multiple Chrome workers increase capacity but also block risk and resource use.
- The current stage-5 observation should continue unchanged; feasibility work must not contaminate its evidence.
