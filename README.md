# jobFinder

A job-search pipeline that collects postings every night, removes the ones a candidate can't take using code
rules and a local language model, scores the rest against a candidate profile, and serves a ranked list as a
local web app.

It is tuned for software roles. `profile.toml` sets the candidate's skills, experience, pay floor and the
cities they can commute to; the domain itself (what counts as a software job, the technology stack, the job
sites) and the candidate's location (Japan) are built into the code.

## How it works

1. **Collect** (nightly): new postings from job boards, recruiter sites and the public APIs of company hiring
   systems. All fetching goes through one module that enforces per-site rate limits, retries and backoff.
2. **Dedupe:** the same job seen on several sites becomes one job with several sightings.
3. **Code rules:** clear mismatches, such as a location outside the candidate's reach, are removed before the
   model reads anything.
4. **Extract:** a local model (gpt-oss:20b in Ollama) reads each remaining posting once and records facts:
   who can apply from where, Japanese requirement, employment type, stack, seniority and pay.
5. **Fact rules:** removes what the facts rule out. Every removal keeps its rule and the quote it was based on,
   and can be undone.
6. **Score:** a fixed formula over pay, location, stack, seniority and working hours sorts every list.
7. **List page:** filters, ratings and statuses for every kept job, and an application packet (resume version,
   cover letter, screening answers, skill gaps) for each job marked to apply.

New company boards are found in the Common Crawl and Wayback Machine URL indexes and checked once before the
nightly run fetches them. Each night 37 sources are collected through six collector types (HTML pages, RSS
feeds, a headless browser, JSON APIs, Hacker News and Lever): 28 job boards, 6 recruiter sites, 2 company career
sites, and Lever's public API, which covers over 1,600 live company boards found so far. Collectors for
Greenhouse, Ashby, Workday, Workable and SmartRecruiters are also built and off by default in `sources.csv`.

The full design is in [job-hunt-pipeline.md](job-hunt-pipeline.md).

## Stack

- **Pipeline:** Python 3.12+, SQLite in WAL mode, httpx, selectolax, feedparser, Playwright (Chromium),
  datasketch (MinHash), Pydantic
- **Model:** any server with the OpenAI-style chat API; Ollama with gpt-oss:20b by default
- **Web:** FastAPI and uvicorn; Vue 3, TypeScript, Vite, Vue Router, openapi-fetch, with the TypeScript types
  generated from the API's OpenAPI schema
- **Quality:** pytest, Vitest, ruff; PR Verify on GitHub Actions

## Setup

Requirements: Windows, Python 3.12 or newer, Node 22.12 or newer on an even-numbered release (22, 24, 26),
and [Ollama](https://ollama.com). The two Task Scheduler tasks are the Windows-specific part; the code has POSIX
branches where it touches the OS, and the test suite runs on Linux in CI.

```powershell
git clone https://github.com/chight1/jobFinder.git
cd jobFinder
python -m venv .venv
.venv\Scripts\pip install -e ".[dev,browser]"
.venv\Scripts\playwright install chromium

npm ci --prefix web
npm run build --prefix web

ollama pull gpt-oss:20b
ollama create gpt-oss-jobhunt -f models/gpt-oss-jobhunt.Modelfile  # the same model with an 8k context
```

Create `profile.toml` next to `config.toml`. It holds the candidate's facts and is gitignored:

```toml
years_experience = 4
pay_floor_usd = 65000
commutable_cities = ["Nagoya"]  # as GeoNames spells them

[skills]  # 3: strong, 2: working, 1: could pick it up
haskell = 3
elixir = 2
cobol = 1

[packets]
forbidden_names = []  # names that must never appear in an application packet
```

Then create the folders and the database, load `sources.csv` and download the GeoNames place lists:

```powershell
.venv\Scripts\jobhunt init
```

Settings, the schedule and the scoring weights are in `config.toml`.

## Running

| Command | What it does |
| --- | --- |
| `jobhunt serve` | the web app at http://localhost:8765 |
| `jobhunt nightly` | fetch every due source until 05:00, then score and back up |
| `jobhunt process` | the model worker: works the queue, then checks for more every minute |
| `jobhunt discover` | find new company boards in Common Crawl, the Wayback Machine and company lists |
| `jobhunt collect` | run the collectors now |
| `jobhunt rescore` | re-apply the rules and the formula, with no model calls |
| `jobhunt packet JOB_ID` | draft one application packet now |

`jobhunt --help` lists the rest.

Two Task Scheduler tasks keep it running:

- **jobhunt nightly**, daily at 01:00, runs `jobhunt nightly`. It wakes the computer, and a missed run starts
  as soon as it can.
- **jobhunt worker**, at logon, runs `jobhunt process` with no console window (`conhost --headless`), with no
  time limit, and is restarted a minute after it stops.

<details>
<summary>Register the tasks (PowerShell, from the repo folder)</summary>

```powershell
$repo = (Get-Location).Path
$jobhunt = "$repo\.venv\Scripts\jobhunt.exe"

Register-ScheduledTask -TaskName "jobhunt nightly" `
  -Action (New-ScheduledTaskAction -Execute $jobhunt -Argument "nightly" -WorkingDirectory $repo) `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 01:00) `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew)

Register-ScheduledTask -TaskName "jobhunt worker" `
  -Action (New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless `"$jobhunt`" process" `
    -WorkingDirectory $repo) `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew)
```

</details>

Keep the 01:00 in step with `[schedule] start` in `config.toml`, which records when the nightly run begins.

The Health page pauses, stops and restarts the worker.

## Tests

```powershell
.venv\Scripts\pytest
.venv\Scripts\ruff check .
npm test --prefix web
```

Every pull request runs the PR Verify workflow ([.github/workflows/pr-verify.yml](.github/workflows/pr-verify.yml)).
