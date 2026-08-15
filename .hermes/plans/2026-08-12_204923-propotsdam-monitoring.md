# ProPotsdam Monitoring Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add ProPotsdam/easy­square apartment monitoring to PotsdamBot with one shared collector and per-user Telegram filters managed from the private admin UI.

**Architecture:** Do not scrape ProPotsdam once per user. A single host-side browser worker logs in to the easysquare portal, navigates to `Immobiliensuche → mehr anzeigen → Immobilien`, extracts all current listings into a local database, then the bot matches each listing against user-specific filters and sends only new matching items. Credentials stay outside git in environment/secret files.

**Tech Stack:** Python, python-telegram-bot 13.15, SQLite, Docker Compose, requests, host browser automation through Playwright/Selenium or an existing local receiver pattern (`CHECK_WOHNUNG_BASE_URL`).

---

## Current context

- Bot repository: `C:\Media\Downloads\Docker\PotsdamBot\Tgbot_Artem\telegram-search-bot`.
- Current branch has existing uncommitted work around `housing_monitor.py`, `tests/test_housing_monitor.py`, `anonymous_posts.py`, `robot.py`, and `docker-compose.yml`; do not overwrite it blindly.
- Existing housing monitoring UI currently targets Immowelt URLs through `CHECK_WOHNUNG_BASE_URL=http://host.docker.internal:18765`.
- ProPotsdam page likely has no stable public listing URL and requires authenticated portal navigation.
- Screenshot shows list page data useful for parsing:
  - top navigation: `Immobilien`, tabs `Liste`, `Karte`, search field, `Filter` button;
  - count: `7 EINTRÄGE`;
  - card fields: title, address, `Stadtteil`, `Zimmer`, `Wohnfläche`, `Gesamtmi...` price, `Verfügbar...` date;
  - example districts: `Babelsberg`, `Waldstadt 2`.

## Security decisions

- Do **not** commit the supplied portal password into source code, tests, logs, or plan output beyond local secret references.
- Store credentials in a local ignored file or Windows environment variables, for example:
  - `PROPOTSDAM_USERNAME`
  - `PROPOTSDAM_PASSWORD`
- Redact credentials in every log line.
- Browser session storage/cookies must live in an ignored local path, not in git.

---

## Phase 0: Confirm finished current work and establish baseline

**Objective:** Treat the existing housing/admin changes as finished, verify them, and start ProPotsdam work from a clean known baseline.

**Steps:**
1. Run `git status --short --branch`.
2. Inspect existing changes with `git diff -- docker-compose.yml robot.py user_handlers/housing_monitor.py tests/test_housing_monitor.py user_handlers/anonymous_posts.py user_jobs/commands_set.py` to confirm they are the completed previous housing work.
3. Run the current housing tests before changes:
   ```bash
   python -m unittest tests.test_housing_monitor -v
   ```
4. If tests pass, commit the finished previous housing/admin changes before starting ProPotsdam so the new work is isolated.
5. If tests fail, fix or explicitly record the baseline failure before starting ProPotsdam; do not mix unknown failures with the new feature.

**Verification:** Previous completed changes are either committed or their exact baseline status is documented before any ProPotsdam code is written.

---

## Phase 1: Decide collector boundary

**Objective:** Keep browser automation out of the Telegram polling process.

**Recommended design:** Create a host-side ProPotsdam collector service similar to the current housing receiver pattern.

**Files likely to create:**
- `tools/propotsdam_collector.py` or external host script under `C:\Scripts\propotsdam-monitor\collector.py`.
- `user_jobs/propotsdam_store.py` for shared parsing/storage helpers if collector runs inside bot repository.
- `tests/test_propotsdam_store.py`.

**Collector responsibilities:**
1. Open/reuse one persistent browser profile.
2. Navigate to portal.
3. If not logged in, click `Anmelden` and submit credentials.
4. Navigate: `Immobiliensuche` → `mehr anzeigen` → `Immobilien`.
5. Wait until the list count/cards are visible.
6. Extract all listing cards.
7. Save normalized listings to SQLite.
8. Expose `GET /api/propotsdam/listings` or write directly to the bot database.

**Why not bot-in-container browser:** The bot container currently has no browser dependencies. Host browser automation is easier to debug, preserves login session, and avoids bloating the Telegram bot container.

---

## Phase 2: Explore ProPotsdam portal safely

**Objective:** Find stable selectors/API calls without guessing.

**Steps:**
1. Use a dedicated persistent browser profile directory, for example:
   `C:\Users\Admin\AppData\Local\PotsdamBot\propotsdam-browser`.
2. Manually or with automation log in once.
3. Open developer tools/network and check whether listings are loaded from a JSON endpoint.
4. If a JSON endpoint exists:
   - capture request URL, method, headers required, and response schema;
   - prefer API polling with saved session cookies.
5. If no usable JSON endpoint exists:
   - extract data from DOM cards.
6. Identify exact selectors for:
   - login button/form;
   - `Immobiliensuche` menu;
   - `mehr anzeigen` button;
   - `Immobilien` menu item;
   - listing card container;
   - title, address, district, rooms, area, price, availability, detail route/link, images.

**Verification:** A read-only script prints the current listing count and normalized cards from a live session.

---

## Phase 3: Define data model

**Objective:** Store one shared ProPotsdam snapshot for all users.

**Tables:**

```sql
CREATE TABLE IF NOT EXISTS propotsdam_listings (
    listing_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    address TEXT,
    district TEXT,
    rooms REAL,
    area_m2 REAL,
    total_rent_eur REAL,
    available_from TEXT,
    detail_url TEXT,
    image_url TEXT,
    raw_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS propotsdam_filters (
    filter_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    districts TEXT,
    min_rooms REAL,
    max_rooms REAL,
    min_area_m2 REAL,
    max_area_m2 REAL,
    max_total_rent_eur REAL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS propotsdam_deliveries (
    filter_id INTEGER NOT NULL,
    listing_key TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (filter_id, listing_key)
);
```

**Listing key strategy:** Prefer stable detail identifier from portal route/API. If none exists, hash normalized `title|address|rooms|area|rent|available_from`.

---

## Phase 4: Build parser with tests first

**Objective:** Make extraction robust before connecting Telegram.

**Files:**
- Create: `user_jobs/propotsdam_parser.py`
- Create: `tests/test_propotsdam_parser.py`

**Tests:**
1. Parse German decimal comma: `963,79 EUR` → `963.79`.
2. Parse area: `64 m²` → `64.0`.
3. Parse rooms: `2` / `1,5` → `2.0` / `1.5`.
4. Normalize availability: `ab sofort`, `ab 01.11.2026`.
5. Extract fields from saved HTML fixture based on screenshot structure.

**Verification command:**
```bash
python -m unittest tests.test_propotsdam_parser -v
```

---

## Phase 5: Implement collector

**Objective:** Collect once for everyone on a schedule.

**Preferred behavior:**
- interval: 10–30 minutes initially, configurable;
- single run lock to avoid overlapping browser sessions;
- if login expires, relogin automatically;
- if extraction fails, do not delete previous active listings until a successful full scan;
- log: count collected, count active, count new, redacted account name only.

**Possible service modes:**
1. **HTTP receiver mode:** host process exposes `/api/propotsdam/scan`, `/api/propotsdam/listings`, `/health`.
2. **Bot job mode:** bot calls host receiver every N minutes via `host.docker.internal`.
3. **Classic scheduled task:** Windows Task Scheduler runs collector, bot only reads DB/API.

**Recommended:** HTTP receiver mode, consistent with current `CHECK_WOHNUNG_BASE_URL` pattern.

---

## Phase 6: Add filter matching logic

**Objective:** Match shared listings to per-user criteria.

**Files:**
- Create: `user_jobs/propotsdam_matching.py`
- Create: `tests/test_propotsdam_matching.py`

**Filter fields from admin survey:**
- Telegram user ID;
- display name/title;
- districts/areas in Potsdam, comma-separated, allow empty = all;
- minimum rooms;
- maximum rooms;
- minimum area;
- maximum area;
- maximum total rent;
- optional availability rule later if needed.

**Matching rules:**
- District matching is case-insensitive exact normalized name first.
- Empty numeric bound means no restriction.
- Rent uses total rent (`Gesamtmiete`) from card.
- A listing is sent once per filter using `propotsdam_deliveries`.

**Verification command:**
```bash
python -m unittest tests.test_propotsdam_matching -v
```

---

## Phase 7: Extend Telegram admin UI

**Objective:** Add ProPotsdam monitoring to the existing housing admin area without breaking Immowelt monitoring.

**Files likely to modify:**
- `user_handlers/housing_monitor.py`
- `tests/test_housing_monitor.py`
- possibly `user_jobs/commands_set.py`

**UI structure:**
- Main private menu:
  - `🏠 Моніторинг житла`
- Admin menu:
  - `➕ Додати Immowelt користувача` (existing flow renamed)
  - `🏢 Додати ProPotsdam користувача`
  - `📋 Фільтри житла`
- ProPotsdam add flow questions:
  1. Telegram ID користувача
  2. Назва/імʼя фільтра
  3. Райони Потсдама через кому або `всі`
  4. Мінімум кімнат або `-`
  5. Максимум кімнат або `-`
  6. Мінімальна площа або `-`
  7. Максимальна площа або `-`
  8. Максимальна ціна або `-`
  9. Confirmation summary → save

**Validation:**
- Numeric values accept both `1.5` and `1,5`.
- District text is trimmed and deduplicated.
- Admin can cancel at any step.

---

## Phase 8: Notification formatting

**Objective:** Send clear apartment cards to matching users.

**Message format:**

```text
🏢 Нова квартира ProPotsdam

Назва: renovierte Altbauwohnung
Адреса: Großbeerenstr. 19, 14482 Potsdam
Район: Babelsberg
Кімнати: 2
Площа: 64 м²
Оренда: 963,79 EUR
Доступна: ab sofort
ID/ключ: <stable listing id or generated listing_key>
Посилання/портал: <detail URL if stable, otherwise portal URL>

Відкрити: [portal/detail if stable, otherwise portal main page]
```

**Mandatory notification rule:** Every notification must include **all apartment data extracted for that new listing**, not only the fields used by the user's filter. If the portal exposes extra fields (floor, object number, heating/cold rent, deposit, description, tags, images, detail route), include them too. The filter only decides whether to notify; it must not truncate the notification payload.

**Important:** If direct detail URL is unstable, still include all extracted listing data and send portal entry URL plus instruction line: `Після входу: Immobiliensuche → mehr anzeigen → Immobilien`.

---

## Phase 9: Scheduling and deployment

**Objective:** Run one scan for all users and notify from bot.

**Steps:**
1. Add env variables to `.env.example` without real values.
2. Add local `.env` values manually, never commit secrets.
3. Add job in `robot.py`, for example every 900 or 1800 seconds, that:
   - calls collector scan/list endpoint;
   - reads active filters;
   - sends unsent matches;
   - records deliveries.
4. Rebuild and restart bot:
   ```bash
   docker compose build tgbot
   docker compose up -d --force-recreate tgbot
   ```

**Verification:**
- `docker ps` shows `tgbot` running.
- `docker logs --since 5m tgbot` contains `robot start...` and no traceback.
- Manual forced scan creates/updates ProPotsdam listings.
- Test filter for Artem receives at most one message per matching listing.

---

## Phase 10: Acceptance tests

**Done means:**
1. Collector can login/reuse session and reach the screenshot page automatically.
2. Collector extracts all visible current listings, including district, rooms, area, rent, availability.
3. Shared database updates once per scan, independent of number of users.
4. Admin can add ProPotsdam filters through a Telegram survey.
5. Users receive only listings matching their filters, but each sent message contains all extracted data for that apartment.
6. Duplicate messages are prevented per user/filter/listing.
7. Credentials are not in git, logs, or Telegram messages.
8. Bot survives restart and continues monitoring.

---

## Risks and open questions

- ProPotsdam/easysquare may block automated login or require changing selectors after UI updates.
- Session cookies may expire unpredictably; relogin must be tested.
- There may be no stable detail URL. In that case send main portal URL and exact navigation path.
- Need to decide scan interval: 10, 15, or 30 minutes.
- Need to confirm whether notification language should be Ukrainian like current bot UI or Russian for Artem/admin.
- Need to check whether ProPotsdam terms permit automated monitoring; keep scan frequency conservative.

---

## Suggested implementation commits

1. `test: add propotsdam parser fixtures`
2. `feat: parse propotsdam listing cards`
3. `feat: store shared propotsdam listings and filters`
4. `feat: collect propotsdam listings via browser worker`
5. `feat: match propotsdam listings to user filters`
6. `feat: add propotsdam admin survey`
7. `feat: send propotsdam listing notifications`
8. `chore: document propotsdam secrets and deployment`
