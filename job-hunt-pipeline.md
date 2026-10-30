# Job Hunt Pipeline

Oct 7, 2026 

A Python program collects job postings every night, cuts the ones the candidate can't take using plain code and a local language model, scores the rest with a fixed formula, and serves the sorted list as a local web page. Each job is processed once, and nothing in it is company-specific.

**Pipeline at a glance**

1. Collect, nightly: new postings from 37 sources and every company board found
2. Dedupe: one job, many sightings
3. Code rules: clear cases the candidate can't take go to the removed pile
4. Extract with the local model: facts only, once per job
5. Fact rules: the remaining cases the candidate can't take go to the removed pile
6. Score by formula: every list is sorted by it
7. List page: every kept job, best first, new ones on top
8. Apply: a packet drafted for each job marked apply

Each job goes down this path once, the night it first appears. Code rules cut the clear cases before the model reads anything; fact rules cut the rest once the model has read the posting. Every removed job waits in the removed pile with its rule and quote.

## Components and tech

Python 3.12, one package named jobhunt, one SQLite file and one command-line entry point. Python fits because its scraping libraries are mature and the candidate already knows it.

| Module | Job | Libraries |
| --- | --- | --- |
| cli | Commands: init, nightly, collect, discover, process, rescore, reextract, packet, serve | standard library |
| config | Reads config.toml (settings, weights, schedule) and profile.toml (the candidate's skills, pay floor, commutable cities) | tomllib |
| db | Schema, migrations, queries. SQLite in WAL mode (write-ahead logging), so the list page can read while the nightly run writes | sqlite3 |
| fetch | The only door to the network: per-site rate limits, retries, backoff, raw-page cache | httpx |
| collectors | One module per collector type, all with the same interface | httpx, selectolax, feedparser |
| browser | The browser-only sites | Playwright with headless Chromium |
| discover | Finds company boards in the Common Crawl and Wayback Machine indexes | httpx |
| rules | Code removal rules and the queue score | GeoNames city and country lists |
| dedupe | Merges sightings into jobs | datasketch (MinHash) |
| llm | Talks to the local model and validates its output | httpx, pydantic |
| score | Formula scoring | standard library |
| web | The list page: a JSON API and the Vue app it serves | FastAPI with Pydantic models, uvicorn; Vue 3, TypeScript, Vite, Vue Router, openapi-fetch, Vitest |
| packets | Application packets | llm |

**Files on disk**

- data/jobhunt.db: the database
- data/raw/SOURCE/HASH.html.gz: saved raw responses
- data/geonames/: city and country lists
- profile/resumes/: resume versions in Markdown
- packets/JOB_ID.md: generated application packets
- config.toml, profile.toml, and sources.csv, built from the Source list at the end of this doc

## Data model

One SQLite file. A **sighting** is one place a posting was seen; a **job** is the real position behind one or more sightings.

| Table | Columns | Holds |
| --- | --- | --- |
| sources | id (slug), name, collector, config (JSON), enabled, min_delay_s, daily_cap, backoff_until, fail_streak, cursor, last_run_at, last_status | One row per source from sources.csv |
| companies | id, ats, slug, extra (JSON: Workday host, tenant, site), found_via, tier, last_checked_at, last_job_count, dead_streak | One row per company board on a hiring system |
| sightings | id, source_id, external_id, url (canonical, unique), title, company_raw, location_raw, posted_at, description, desc_hash, source_fields (JSON), first_seen_at, last_seen_at, job_id | One row per place a posting was seen |
| jobs | id, company_norm, title_norm, city_norm, primary_sighting_id, stage, removal_rule, removal_quote, prescore, facts (JSON), prompt_version, score, score_parts (JSON), reason_line, badges, status, status_at, follow_up_on, first_seen_at, japanese_override | One row per real position |
| dupe_pairs | job_a, job_b, title_sim, desc_sim, verdict, decided_by | Borderline duplicates waiting for the model |
| company_aliases | alias_norm, company_norm | Hand-kept company name aliases |
| fetch_log | source_id, url, status, bytes, ms, at, error | Every request, for politeness and health |
| runs | id, started_at, ended_at, counts (JSON) | One row per nightly run (fetching only) |
| packets | job_id, resume_file, letter, answers (JSON), gaps (JSON), made_at | One per job marked apply |
| settings | key, value | Last list visit, the crawl each hiring system was last read from |

**Job stage** moves one way: new → code_removed, or queued → extracted → removed, kept or japanese. japanese is a job that passes every rule but requires Japanese: the backup list for when the English jobs run out. A button at the end of each card's buttons moves a job to the other list by hand ("Japanese" on a main-list job, "Not Japanese" on a Japanese one) (japanese_override), which wins over the model's answer and survives a rescore. A failed extraction is extract_failed and stays visible.

**Job status** is the candidate's: none, apply, applied, interviewing, rejected, ghosted, not_interested.

**Indexes:** sightings(url) unique; sightings(source_id, external_id); jobs(stage, prescore) for the model queue; jobs(score) for the list; companies(ats, slug) unique.

## The nightly run

The operating system's scheduler (cron on Mac or Linux, Task Scheduler on Windows) starts "jobhunt nightly" at 01:00. The nightly run only fetches; the model worker is a separate process that is always on. The two share the database and never call each other: fetching queues jobs, and the worker takes them.

**Fetching, 01:00 to 05:00.** At 05:00 it stops; each source saves its cursor and resumes there the next night.

1. Pick the sources and boards that are due (board tiers decide which boards).
2. Collect new postings only (see Collectors).
3. Run the code rules on each new sighting. Survivors without a description get one detail fetch.
4. Dedupe levels 1 and 2: attach to an existing job or create a new one.
5. New jobs that pass the code rules enter the model queue with their prescore.

**At 05:00:** formula scoring, the run summary and the backup. Then the run ends.

**Model worker, always on.** "jobhunt process" is started at logon by its own scheduled task, hidden, and never stops by itself. It always takes the highest-priority work first:

1. Packets for jobs the candidate marked apply
2. Borderline duplicate pairs
3. Extraction of queued jobs, highest prescore first

Each extraction applies the removal rules and the formula to that job, so the list page shows it as soon as it is done. With no work left, the worker looks again every minute. Only one model worker runs at a time (an OS lock on data/model.lock); a second one leaves the queue to the first and exits. When the model server fails, the worker waits a minute and tries again.

**Pause, Stop, Play** on the Health page. Pause lets the worker finish the task it has, then it takes no more (it looks every 5 seconds whether it may go on). Stop ends the worker's process at once; the job it had stays queued. Stop also leaves it paused, so a worker started again, at the next logon say, waits too. Play unpauses it, and starts a worker when none is running.

**The first night is bigger.** Everything open is new, so the first pull and the extraction backlog take a while; the worker keeps going until the queue is empty.

**By hand, any time:** "jobhunt process --minutes 60" runs a worker for an hour when the always-on one is not running; "jobhunt collect --source NAME" runs one source; "jobhunt packet JOB_ID" drafts one packet now; "jobhunt rescore" re-applies the formula in seconds, with no model calls.

## Fetching and politeness

Every request goes through the fetch module, so these rules hold everywhere.

| Rule | Value |
| --- | --- |
| Requests at once per site | 1 |
| Gap between requests, HTML sites | 4 s, plus 0–2 s random |
| Gap between requests, hiring-system APIs | 1 s, plus 0–0.5 s random |
| Sites fetched in parallel | up to 6, never two on the same site |
| Timeout | 30 s |
| Stated limits | Remotive: 4 calls a day; startup.jobs: 20 a minute |
| The same page twice in one night | never: the raw cache answers |
| Feeds | conditional requests (ETag and Last-Modified headers), so an unchanged feed costs one tiny reply |
| Logins | never |

**When a site pushes back:**

1. A 429 ("too many requests") or a 503: wait for the Retry-After header, or 60 s, and retry at most twice.
   A 502 or 504 (a gateway that timed out) is retried the same way; still failing, it is an ordinary error (6), not a block.
2. Still failing, a 403 ("forbidden"), or a challenge page (detected by markers such as "captcha" or "Just a moment" in the body): stop that source for the night. Set backoff_until to the next night, double its gap (up to 30 s), and add 1 to fail_streak.
3. A fail_streak of 3 disables the source and flags it on the health view until the candidate turns it back on.
4. A clean night resets fail_streak, and the gap halves back toward its default.
5. No rotating addresses or other ways around a block. A blocked source waits.
6. Any other error (a bad status, a timeout, a page that will not parse): log it and stop that site for the rest of the run, with no backoff. On a hiring system the site is the one company board: the run goes on to the next board, and only pushback (step 2) stops the whole system, except on Workday, where each board is its own site and a block stops only that board. A posting that is simply gone (404) is not an error.

## Collectors

Every collector has the same two calls and never touches the database. A runner does the bookkeeping around them.

- **list_postings(source, cursor)** returns summaries (external id, link, title, company, location, posted date, and the description when the list carries it), the ids it saw, and the next cursor.
- **fetch_detail(summary)** returns the full description. The runner calls it only for new postings that passed the code rules and arrived without a description.

The runner compares the returned ids with the database: new ids become sightings and known ids get last_seen_at.

**No closed postings.** A job missing from a list is not taken as gone: most sources show only their newest jobs, so absence says nothing. Jobs are never marked closed; a dead posting is found by opening it, and dropped.

| Collector | Request | Paging | Detail fetch |
| --- | --- | --- | --- |
| Greenhouse | GET boards-api.greenhouse.io/v1/boards/SLUG/jobs | none: whole board | GET …/jobs/ID for new jobs |
| Lever | GET api.lever.co/v0/postings/SLUG?mode=json | none | none: the list is complete |
| Ashby | GET api.ashbyhq.com/posting-api/job-board/SLUG?includeCompensation=true | none | none |
| Workable | GET apply.workable.com/api/v1/widget/accounts/SLUG?details=true | none | none |
| SmartRecruiters | GET api.smartrecruiters.com/v1/companies/ID/postings?limit=100&offset=N | offset | GET …/postings/POSTING_ID |
| Workday | POST HOST/wday/cxs/TENANT/SITE/jobs with limit 20, offset N, empty search text | offset | GET HOST/wday/cxs/TENANT/SITE plus the job's path |
| HRMOS, Talentio | GET the public job list page | page links | GET the job page |
| RSS feeds | GET the feed | none | when the feed's text is under 500 characters, unless the source is feed-only (startup.jobs, whose job pages have a checkbox challenge) |
| JSON APIs (Himalayas, Jobicy, Remotive, RemoteOK, Working Nomads, Amazon, Tech in Asia) | GET with the API's Japan, Asia or worldwide filter where it has one; Tech in Asia POSTs an Algolia search | the API's own paging, newest first | rarely |
| Hacker News | hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring, then /items/ID for the month's thread | none | none: each comment is a posting |
| Japan boards (TokyoDev, Japan Dev, Daijob, GaijinPot) | GET list pages, newest first, IT categories | page numbers | GET the job page |
| Browser sites (CareerCross, Wellfound) | Playwright loads list pages | until no new ids | the job page |

**Stop rule for newest-first lists:** stop at the first page where every id is already known.

## Company discovery and board tiers

"jobhunt discover" fills the companies table once a month, when a new Common Crawl crawl appears. Common Crawl visits the hiring systems rarely (its 2026-39 crawl held 105 jobs.lever.co pages, 28 boards), so the Wayback Machine's index is read too (below).

1. GET index.commoncrawl.org/collinfo.json and take the newest crawl's index address.
2. Ask that index for every captured link under each hiring-system pattern: jobs.lever.co/\*, boards.greenhouse.io/\*, job-boards.greenhouse.io/\*, jobs.ashbyhq.com/\*, apply.workable.com/\*, and myworkdayjobs.com as a whole domain. Ask for the page count first (showNumPages=true), then fetch page by page, one request every 2 s.
3. Take the first path segment of each link as the board slug (the host, tenant and site for Workday). Drop duplicates and insert boards not already known.
4. Check each new board with one call: a 404 makes it dead, an empty list makes it cold, any jobs make it active. A board whose check fails some other way (a 500, a page that will not parse) stays new and is asked again next time; the checks go on to the next board. Pushback (429, 503, 403) ends the checks on that hiring system for the run, except on Workday, where each board is its own site.
5. The company lists in sources.csv add boards the same way, from the hiring-system links on their pages.

**Wayback Machine.** Its URL index (web.archive.org/cdx/search/cdx) keeps every capture since the 2010s: about 419 index pages for jobs.lever.co alone. Discovery asks it only for board front pages (jobs.lever.co/SLUG, status 200, a server-side filter), one page every 30 s with a 5-minute timeout, since their side scans each page for the filter. A busy answer (502, 503, or 504: a search that timed out on their side) or a lost request (a dropped connection, a timeout) doubles the wait before asking again, up to 30 minutes; from then on it asks once an hour for as long as the index stays busy, and a success starts the wait over at 30 s. Any other failure stops the run. Each page's boards are saved as they come, with the next page number (settings: wayback_page:lever), so a run that stops resumes there; when the last page is in, wayback_read:lever marks it read and later runs skip it ("--force" reads it again). Only Lever is asked for so far. Its boards have found_via = "wayback".

A hiring system counts as read for that crawl (settings: crawl_read:lever and so on) only when all its searches finished, so one that failed is asked again on the next run. "jobhunt discover --ats lever" reads one hiring system and checks only its new boards, without the company lists.

| Tier | Rule | Checked |
| --- | --- | --- |
| Active | has open jobs | nightly |
| Cold | exists but has no jobs | weekly |
| Dead | answered 404 | monthly; dropped after 3 dead months in a row |

A board moves between tiers after each check. At 1 request a second per hiring system, 10,000 active boards take about 3 hours, and the systems run side by side.

## Code rules before the model

These run on every new sighting, before any detail fetch when the list already has the field. They remove only clear cases; anything unclear goes on to the model.

**Location.** The location text is matched against GeoNames cities15000 (every city over 15,000 people), its country list, and a short region list.

- Keep: any place in Japan; Asia or APAC (Asia-Pacific); Worldwide, Anywhere or Global; a bare "Remote".
- Remove, rule "location": only places outside Japan with no anywhere-marker, such as "Remote - US", "London, UK" or "EMEA" (Europe, the Middle East and Africa).
- Empty or unrecognized: keep.

**Title.** Remove, rule "not software", only when the title has a word from a non-software list (sales, account executive, recruiter, marketing, legal, finance, customer success) and none from the software list (engineer, developer, programmer, SRE, DevOps, full stack, frontend, backend, QA, data engineer). SRE is site reliability engineer; QA is quality assurance.

Language never removes a job: jobs that require Japanese are the backup list (see Removal after extraction).

**Employment type.** Remove, rule "contract", when the source's own structured field says contract, freelance or temporary (for example Lever's commitment field), or the title starts with "Contract" or "Freelance".

**Blacklisted employer.** Remove, rule "blacklisted", when the employer (compared as dedupe compares companies: lower case, legal suffixes gone) is on the blacklist. A job card's Blacklist button adds its employer and hides that employer's stored jobs at once; the Blacklist page lists the employers with how many jobs each hides, and Remove brings the jobs back where they were: a job the model has read is judged again from its facts, one it never read goes to the model queue.

**Queue order.** A code score decides what the model reads first:

prescore = 3 × location hint + 2 × title hint + 1 × (listed pay at or above the floor) + 1 × (posted within 7 days)

- location hint: 1 for Japan, Osaka, Kansai, APAC, worldwide or anywhere; 0.5 for a bare "Remote"; 0 otherwise
- title hint: 1 for React, Java, JavaScript, frontend, full stack or backend; 0.5 for engineer or developer; 0 otherwise
- a posting written in Japanese (over 30% of the description's letters in the hiragana, katakana and kanji ranges) loses 10, more than any English posting can score, so the model reads it only when no English job is waiting

## Model stage

The model only reads postings and fills in facts. It never scores: scoring is a formula in code, so a weight change re-sorts everything in seconds with no model calls.

**Runtime.** Any local server that speaks the OpenAI-style chat API; Ollama, llama.cpp's server and LM Studio all do. config.toml holds its address and model name. Temperature 0, one job per call.

**Input trimming.**

1. Convert the description from HTML to plain text.
2. Split it into sections at its headings. A bullet point is never a heading.
3. Drop only filler sections: about the company, culture and values, equal-opportunity, diversity and privacy statements. Keep everything else, since headings vary too much to list the useful ones. If trimming would keep less than 40% of the posting, send the whole posting: a heading was misread.
4. Cap the result at 6,000 characters. Title, company, location text and the source's structured fields go in front, untouched.

**Number check.** After extraction, code looks for each pay, years-required and office-days figure in the text the model read ("120k", "120,000", "800万" and "three" all count). A figure that appears nowhere is a guess and is cleared to null. A guessed salary would otherwise put a job at the top of the list.

**Output.** One JSON object, validated against this schema with pydantic. The schema itself is sent along when the server supports constrained output.

| Field | Values |
| --- | --- |
| open_to_japan_resident | yes, no, unclear |
| japanese | required, plus, not_mentioned |
| employment | payroll, employer_of_record, contract, freelance, dispatch, unclear |
| work_mode | remote, hybrid, onsite |
| office_city, office_days_per_week | text and number, or null |
| overlap | time zone, start hour, end hour; or null |
| seniority | junior, mid, senior, staff_plus, unclear |
| years_required | number or null |
| stack, primary_language | list of technologies; one language or null |
| pay_min, pay_max, pay_currency, pay_period | numbers; currency code; year, month or hour; or null |
| overtime | none_mentioned, some, heavy |
| on_call | yes, no, not_mentioned |
| is_software_job | yes, no |
| evidence | a short quote behind each of open_to_japan_resident, japanese and employment |

**Validation.** Invalid JSON or a schema failure gets one retry, with the error message added to the prompt. A second failure marks the job extract_failed. It stays in the list with a "check" badge and shows on the health view.

**Server failures.** A server error or lost connection is retried after 30 s, then 2 minutes, then 5 minutes; a crashed model engine restarts on the next request. When all four attempts fail, the worker waits a minute and starts again. A request the server refuses (a 4xx, such as a prompt longer than its context) skips that one task instead, and so does a reply the server cuts off because the model looped ("prediction aborted, token repeat limit reached"): that gets one more try straight away, then the job is marked extract_failed and the queue goes on.

**Versioning.** Each job stores the prompt version behind its facts. A new prompt never re-runs old jobs by itself; "jobhunt reextract --since DATE" does it on request.

**Throughput** is logged per call. The health view shows seconds per job and how many hours of model time the current queue will take.

## Removal after extraction

Once a job has facts, these rules run in code. Each removal stores the rule's name and the model's quote in removal_rule and removal_quote.

| Rule | Removes when |
| --- | --- |
| Not open to Japan | open_to_japan_resident is no |
| Not payroll | employment is contract or freelance |
| Can't commute | work_mode is onsite or hybrid, office_city is not on the commutable list in profile.toml, and it is not a Tokyo hybrid role |
| Not software | is_software_job is no |

A job no rule removes is kept, unless japanese is required: then its stage is japanese, scored the same way and shown in the Japanese view. The language is checked last, so that view holds only jobs that are targets except for the language.

- "unclear" never removes. The job stays and gets a "check" badge.
- Dispatch to a client site is still a payroll job, so it stays; it is shown with a "dispatch" badge.
- Removed jobs keep all their data. The Removed view shows the rule and the quote, and a restore button puts the job back.
- A restored job is never removed again by the same rule.

## Deduping

Deduping runs in code as each sighting arrives, so the model never reads the same position twice.

**Level 1: same link.** The canonical link has a lowercase host, https, no fragment, no trailing slash, and no tracking parameters (utm_ anything, ref, source, gh_src, lever-source, lever-origin). A link already in sightings only updates last_seen_at.

**Level 2: same key.** key = company_norm + title_norm + city_norm. An exact match attaches the sighting to that job.

- company_norm: Unicode NFKC normalization (which turns full-width letters into ordinary ones), lowercase, punctuation removed, legal suffixes removed (inc, llc, ltd, co, corp, gmbh, kk, 株式会社, 合同会社, (株)), then the alias table.
- title_norm: NFKC, lowercase, bracketed text and place names removed, "sr" to "senior", "jr" to "junior", spaces collapsed.
- city_norm: the matched gazetteer city, or "remote".

**Level 3: fuzzy, same company only.** Runs after the detail fetch, against jobs with the same company_norm seen within the last 30 days.

- Title similarity: shared title words divided by all title words (Jaccard similarity).
- Description similarity: Jaccard similarity over 5-word shingles (every run of five consecutive words).
- Title ≥ 0.6 and description ≥ 0.8: merge.
- Description between 0.5 and 0.8: a borderline pair. The model gets both trimmed postings and answers "same" or "different".
- Below that: different jobs.

**Agent posts that hide the company** (no company, or a name on the known-agency list) are never merged. MinHash, a fast estimate of description similarity, finds the company's jobs at 0.8 or above, and the post is linked to that job as "probably the same".

**Primary link.** A job shows the link from its best sighting: the employer's hiring system first, then a Japan board, then a global board, then an agent.

**Reposts.** Both lookups match only jobs seen within the last 30 days. A posting matching a job not seen for longer starts a new job, without the old one's status.

## Scoring

Each kept job gets a score from 0 to 100 from a fixed formula over its facts and profile.toml. Weights and breakpoints live in config.toml, and "jobhunt rescore" re-applies them in seconds.

score = 100 × (0.40 × pay + 0.20 × location + 0.20 × stack + 0.10 × seniority + 0.10 × balance)

| Part | Scores 1.0 | In between | Scores low | Unknown |
| --- | --- | --- | --- | --- |
| Pay (yearly, US dollars) | $120k and up | 0.7 at the $100k floor; straight lines between the points | 0 at $40k and below | 0.4 |
| Location | remote and open to Japan, or in a commutable Kansai city | 0.5 Tokyo hybrid, 2 office days or fewer | 0.3 Tokyo hybrid, 3 or more | 0.6 |
| Stack | primary language at level 3 in profile.toml | 0.8 at level 2, 0.5 at level 1 | 0.3 not in the profile | 0.6 |
| Seniority | years asked at or under the candidate's years | minus 0.2 per extra year asked | 0.2 for staff level and above | 0.7 |
| Balance | overlap hours inside 08:00–20:00 Japan time | 0.5 if overlap reaches 20:00–23:00 | 0.1 if any overlap falls 23:00–07:00 | 0.6 |

- **Pay conversion:** monthly × 12, hourly × 2,000, then a currency table in config.toml that the candidate updates by hand. A range uses its midpoint.
- **Balance adjustments:** heavy overtime multiplies balance by 0.5, on-call duty by 0.8.
- **Skill levels** in profile.toml: 3 is strong, 2 is working, 1 is "could pick it up". The candidate sets them.
- **Reason line,** written by code from the parts: work mode and place · pay in the posting's currency and in dollars · primary language and the candidate's level · years asked against the candidate's · overlap hours in Japan time.

**Badges**

- **unicorn:** open to Japan, Japanese not required, payroll or employer of record, remote or Kansai-based, pay at or above $100k, primary language at level 1 or higher. A label only; the score still sets the order.
- **check:** a removal-relevant fact is unclear, or extraction failed.
- **dispatch:** a payroll job placed at a client's site.

There is no model ranking on top of the score. A pairwise comparator (the model picking the better of two postings) was tried and dropped: a small local model's picks were closer to guesses than judgments, and it reordered the list on a handful of answers.

## The list page

"jobhunt serve" runs a local web page at localhost:8765: a Vue 3 + TypeScript app in web/, built with Vite (`npm run build`), over a JSON API under /api. The API is FastAPI routes with Pydantic response models, documented at /api/docs. The app's TypeScript types are generated from that API's OpenAPI schema (`npm run types`), so the two cannot drift apart. The API reads the database directly, so the page is always current. The jobs table adds jobs live: every 10 seconds the page asks the API for jobs listed since its last answer and slots them in by score, highlighted briefly. Filters, searches and pages live in the URL, so a view survives a reload and can be linked to. The browser also remembers the jobs table's last filters, so a bare /jobs (the Jobs tab) opens where the candidate left off.
| View | Shows |
| --- | --- |
| Jobs | every job in one table, best first (at most 500 at a time), narrowed by filters; see The jobs table |
| Health | the model worker's state with Pause, Stop and Play; per-source status (see Health and errors); and the last backup with a "Back up now" button |
| Activity | what each running command and worker is doing right now, queue progress, the latest web requests and model calls mixed in time order, and the end of the log; refreshes every 5 seconds |
| Skills | every skill the kept jobs name, most common first, with your level beside each; Japanese jobs are left out unless switched on; changing a level (or adding a skill) saves it to profile.toml with its comments kept and rescores the jobs; the model worker reads the skills again before each job |
| Data | every table, read-only: newest rows first, 50 a page, with a search across all columns |
| Job (#id) | one job in full: state, removal rule and quote, score parts, every extracted fact and its evidence, every place it was seen, model calls, duplicate results, and the description |
| Packet (#id) | the application packet drafted for a job marked apply |

**The jobs table** works like a ServiceNow list: one table of every job, and filters instead of separate lists. Each filter is a set of on/off options, any combination, and a job shows when an option that is on matches it in every filter. The filters live in jobhunt/web/filters.py, each option with its own SQL condition; the API's query parameters and the page's filter bar are both built from that list, so a new filter is one entry there. The filters sit in a rail left of the list, which stays in view and scrolls on its own: rating as dots, every other filter as a section that opens to a checklist and, closed, says what is on ("No status", "Any", "3 of 7"); the browser remembers which sections are open. Above the list, a results bar holds the count, the sort, and "Copy top 3 links" (the first three postings' links, one per line). On a narrow window the rail sits above the list.

| Filter | Options (on by default in bold) |
| --- | --- |
| Rating | **green**, **yellow**, **red**, **not rated**, shown as dots like the card's rating dots |
| Japanese | Japanese, **not Japanese**: the model's answer, or the candidate's move by hand |
| Status | **no status**, to apply, applied, interviewing, rejected, ghosted, drop |
| Removed | **kept**, then one option per removal rule |
| Source | **every source that has given us a job**; a job shows while any source it was seen on is on |

A plain /jobs is the open list, best first; "Recently changed" (?sort=changed) puts the jobs the candidate changed by hand last at the top instead (status, rating, list move, restore, blacklist), so one marked by mistake is easy to find. A skill chip, or a skill in the Skills table, narrows the table to the jobs asking for that skill (?skill=go). Removed jobs show their rule and quote, and a Restore button.

Workers report what they are doing in an activity table as they go, with their process id. A note whose process has exited (killed, or out of memory) is left off the Activity page; a live one left untouched for 30 minutes is flagged, since it may be stuck.

**Each job row:** score, badges, title, company, place and work mode, pay (original currency and dollars), primary language, reason line, primary link, other sightings, and its buttons as one segmented control. Each button has its own rule for when it shows (web/src/cardButtons.ts): to apply and drop before applying; applied before it; interviewing after applying or being ghosted; rejected after applying, interviewing or being ghosted; ghosted after applying or interviewing; then Japanese or Not Japanese for the list the job is not on, Restore on a removed job (not a blacklisted one: those come back from the Blacklist page), and Blacklist on any job that names its employer. The current status shows filled in; clicking it again clears it.

**Rating:** three dots in each tile's top-right corner (green, yellow, red) mark how much the candidate likes a job, and clicking the chosen color again clears it. A rating is separate from status, and a rated job gets a matching colored edge, so a long list is easy to scan. The rating saves at once, without reloading the page.

**Buttons** set the job's status and time at once, without reloading the page. "Apply" also queues a packet. After any button, the table asks again, so a job its filters no longer let through leaves it. An undo bar at the bottom then names the change for 10 seconds (longer while the pointer or focus is on it) and takes it back: an earlier status returns with its own date and follow-up, a rating or list move goes back, and a blacklisting comes off the list. Restore has no undo, since the job is already on its way to the model.

**Follow-up:** marking a job applied sets a follow-up date, 10 days later by default (set in config.toml). It shows on the job's card.

## Application packets

Each job marked apply gets a packet in the next model window, or right away with "jobhunt packet JOB_ID". Every application is submitted by hand.

| Part | Made by | How |
| --- | --- | --- |
| Resume version | code | Picked from profile/resumes by primary language: the frontend file for React and JavaScript, the backend file for Java, the general file otherwise |
| Cover letter | model | From the trimmed posting, the job's facts and the chosen resume; 250 words or fewer |
| Screening answers | model | Only for questions printed in the posting itself |
| Gap list | code | Stack items missing from profile.toml, plus years asked minus the candidate's years |

**Rules for the model's text:**

- English only.
- Never name the candidate's previous project or the agency it served; call it a large-scale federal modernization program.
- Use only facts from the resume and profile.toml. Nothing invented.

**Output:** a packets row plus packets/JOB_ID.md, linked from the job's row on the list page.

## Backups

The database and packets are backed up to a separate private repo, jobFinder-data, kept in a folder next to this one ([backup] in config.toml). "jobhunt backup" writes the database as a text SQL dump, so git stores only each night's changed lines, copies packets/, commits and pushes. The nightly run calls it at the end; the Health view has a "Back up now" button and shows the last backup's time and result. The page cache and GeoNames lists are not backed up, since both download again. "jobhunt restore" rebuilds jobhunt.db from the dump and keeps any existing database as jobhunt.db.bak.

## Health and errors

Every request lands in fetch_log. Every run writes one runs row with its counts: fetched, new sightings, new jobs, removed by code, extracted, removed after extraction, failed.

The Health view shows each source's last run, status, requests, new jobs, errors, backoff, and its 7-night average of new jobs. It flags:

- **Blocked:** fail_streak above 0, or disabled after 3.
- **Parser broken:** requests succeeded but parsed postings dropped to 0, on a source that averages 5 or more new jobs a night. This also catches a site that changed its layout.
- **Extraction failing:** more than 10% of the last 200 extraction calls failed.
- **Model behind:** the queue holds more than 24 hours of measured throughput.

Every collector runs inside its own error boundary: one broken site is logged and skipped, and never stops the run.

## Build order and testing

Each step ends with something that runs.

1. **Skeleton:** package, config files, schema, the fetch module with all its politeness rules, and the CLI with init and collect.
2. **Thin slice, end to end:** Himalayas (JSON API), TokyoDev and Japan Dev (HTML), and the Greenhouse collector on 20 boards taken from the Japan Dev company list. They run through code rules, dedupe levels 1 and 2, extraction, removal, scoring and the list page. From here on the list is real.
3. **Every feed, JSON API and hiring-system collector,** plus company discovery and board tiers.
4. **Plain-page collectors,** reusing the jpjobs package (Python, version 0.2.0) where it already covers a board.
5. **Dedupe level 3 and packets.**
6. **The browser sites,** last: the most work for the least new volume.

**Testing**

- Each collector has saved sample responses in tests/fixtures and a parser test against them.
- "jobhunt collect --source NAME --dry-run" prints parsed postings without writing anything.
- The code rules have a table test: location strings, titles and texts, each with its expected verdict.
- A set of 50 postings checks extraction accuracy whenever the prompt or the model changes. Claude Code labels them during the build: "jobhunt evalextract --export 50" takes 50 postings from the first collection, Claude Code writes the correct facts for each by hand into tests/extraction_set, and the candidate reads the labels over. "jobhunt evalextract" runs extraction on all 50 and reports the share right per field.

## Open items

- [ ] Fill in profile.toml: skills with levels 1 to 3, years of professional experience, and the commutable city list
- [ ] Write the resume versions in profile/resumes: frontend, backend, general
- [ ] Pick the local model server and model; the first night measures seconds per job

## Source list

Every source the collectors cover. sources.csv is built from this table; company boards found through discovery go into the companies table, not here.

| Source | Collector | Link | How to collect |
| --- | --- | --- | --- |
| TokyoDev | html | https://www.tokyodev.com/jobs | Server-rendered HTML, filter pages like /jobs/fully-remote. No RSS |
| Japan Dev | html | https://japan-dev.com/jobs | Server-rendered HTML. No RSS or API |
| Daijob | html | https://www.daijob.com/en/ | Plain HTTP pages (jpjobs package already scrapes it) |
| CareerCross | browser | https://www.careercross.com/en | Public pages with anti-bot |
| GaijinPot Jobs | html | https://jobs.gaijinpot.com | Public pages. robots.txt blocks crawlers; jpjobs scrapes it over HTTP |
| Wellfound (Japan) | browser | https://wellfound.com/location/japan | JavaScript-heavy pages |
| startup.jobs | rss | https://startup.jobs/locations/japan | RSS without key: startup.jobs/feeds/jobs?role=engineering. Feed-only: job pages have a checkbox challenge. (The API needs a free key, 20 req/min) |
| Jobgether (Japan) | html | https://jobgether.com/remote-jobs/japan/it-development | Public pages |
| JAC Recruitment | html | https://www.jac-recruitment.jp/search/it/ | IT category list; mostly Japanese postings that name the employer |
| en world Japan | manual | https://www.enworld.com/ | Job search is a Salesforce site whose cards have no links; its jobs come from Aura calls |
| Robert Walters Japan | html | https://www.robertwalters.co.jp/en/jobs.html?specialization=information-technology | Information technology list, pages from p=0 |
| Hays Japan | browser | https://www.hays.co.jp/en/job-search?specialismId=Technology | Technology search (a JavaScript app); JobPosting JSON-LD on job pages |
| Michael Page Japan | html | https://www.michaelpage.co.jp/en/jobs/it | IT and Technology & Telecoms lists, pages from page=0 |
| Robert Half Japan | html | https://www.roberthalf.com/jp/en/jobs?lobid=RHT | IT and technology list; JobPosting JSON-LD inside the page's mainEntity |
| RGF Professional Recruitment | html | https://www.rgf-professional.jp/en/jobs/ | Technology and Internal IT occupations; mostly Japanese postings |
| Himalayas | json_api | https://himalayas.app/jobs/countries/japan | API: himalayas.app/jobs/api/search?country=JP&employment_type=Full%20Time; RSS himalayas.app/rss |
| Jobicy | json_api | https://jobicy.com | API: jobicy.com/api/v2/remote-jobs?geo=japan&industry=dev&count=50; RSS jobicy.com/jobs/feed |
| Real Work From Anywhere | rss | https://www.realworkfromanywhere.com | RSS: /rss.xml, /remote-fullstack-jobs/rss.xml, /remote-backend-jobs/rss.xml, /remote-frontend-jobs/rss.xml |
| RemoteOK | json_api | https://remoteok.com | JSON API remoteok.com/api; RSS remoteok.com/rss (link-back required) |
| We Work Remotely | rss | https://weworkremotely.com | RSS: /categories/remote-full-stack-programming-jobs.rss, -back-end-, -front-end-, remote-programming-jobs.rss |
| Remotive | json_api | https://remotive.com | API: remotive.com/api/remote-jobs?category=software-dev (max 4 calls a day; 24-hour delay) |
| Working Nomads | json_api | https://www.workingnomads.com | JSON feed: workingnomads.com/api/exposed_jobs/ |
| RemoteYeah | rss | https://remoteyeah.com | RSS by role: /remote-full-stack-engineer-jobs.xml, -backend-, -software-; all at /rss.xml |
| Remote First Jobs | rss | https://remotefirstjobs.com | RSS: remotefirstjobs.com/rss |
| Hacker News Who Is Hiring | hn | https://news.ycombinator.com/submitted?id=whoishiring | Algolia API: hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring then /api/v1/items/{id} |
| Remote Rocketship | html | https://www.remoterocketship.com/country/japan/jobs/software-engineer | Public pages; email alerts |
| Remote.com Jobs | html | https://remote.com/jobs/all | Public pages |
| HRMOS | hrmos | https://hrmos.co/pages/COMPANY/jobs | Public list page per company |
| Greenhouse | greenhouse | https://boards-api.greenhouse.io/v1/boards/COMPANY/jobs | Public JSON API, no key. List without content, then /jobs/ID for new jobs |
| Lever | lever | https://api.lever.co/v0/postings/COMPANY?mode=json | Public JSON API, no key |
| Ashby | ashby | https://api.ashbyhq.com/posting-api/job-board/BOARD?includeCompensation=true | Public JSON API, no key |
| Workable | workable | https://apply.workable.com/api/v1/widget/accounts/SLUG?details=true | Public JSON API, no key |
| SmartRecruiters | smartrecruiters | https://api.smartrecruiters.com/v1/companies/ID/postings?country=jp | Public JSON API, no key |
| Workday | workday | https://TENANT.wdN.myworkdayjobs.com/wday/cxs/TENANT/SITE/jobs | POST with JSON body {limit, offset, searchText} |
| Talentio | talentio | https://open.talentio.com | Public pages |
| Money Forward | hrmos | https://hrmos.co/pages/moneyforward/jobs | HRMOS |
| Moneytree | hrmos | https://hrmos.co/pages/2235668992508076032/jobs | HRMOS |
| Cybozu | html | https://cybozu.co.jp/recruit/entry/ | Static job pages |
| HACARUS | html | https://hacarus.com/careers/ | Own portal (hacarus.recruitment.jp) |
| PayPay | greenhouse | https://boards-api.greenhouse.io/v1/boards/paypay/jobs | Greenhouse API |
| Rakuten | workday | https://rakuten.wd1.myworkdayjobs.com/RakutenInc | Workday JSON at /wday/cxs/rakuten/RakutenInc/jobs |
| Woven by Toyota | lever | https://api.lever.co/v0/postings/woven-by-toyota | Lever API |
| SmartNews | workable | https://apply.workable.com/smartnews | Workable API |
| Preferred Networks | talentio | https://www.preferred.jp/en/careers/ | Talentio |
| Amazon Japan | json_api | https://www.amazon.jobs/en/search.json?loc_query=Japan | Public JSON search |
| Treasure Data | html | https://www.treasuredata.com/company/careers/ | Own page |
| HENNGE | html | https://recruit.hennge.com/en/ | Own pages |
| Paidy | greenhouse | https://job-boards.greenhouse.io/paidyinc | Greenhouse (API returned 0 jobs when checked) |
| TokyoDev company list | company_list | https://www.tokyodev.com/companies | Public pages |
| Japan Dev company list | company_list | https://japan-dev.com/companies | Public pages |
| Remote In Tech | company_list | https://github.com/remoteintech/remote-jobs | Clone the repo |
| Established Remote | company_list | https://github.com/yanirs/established-remote | README table |
| Common Crawl URL index | discover | https://index.commoncrawl.org | CDX API with JSON output; prefix queries such as url=jobs.lever.co/\* against the newest crawl (list at index.commoncrawl.org/collinfo.json); re-run monthly when a new crawl lands |
| Wayback Machine URL index | discover | https://web.archive.org/cdx/search/cdx | CDX API; board front pages only (jobs.lever.co/SLUG), read once and resumed by page |
| Coral Capital portfolio | company_list | https://coralcap.co/portfolio/?lang=en | Public pages |
