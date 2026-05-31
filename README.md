# Real Estate Analyzer

Real Estate Analyzer is a comprehensive toolkit designed for scraping, analyzing, and evaluating real estate data from Zillow. It simplifies the process of gathering property details, estimating market values, and making investment decisions based on a variety of metrics.

## Features

- Scraping property details and Zestimate history from Zillow.
- Calculating real estate investment metrics.
- Leveraging Chrome profiles for more efficient scraping.

## Getting Started

## Browser automation hygiene

- Prefer an isolated Chrome user-data directory for scraping/debugging (do not point automation at your personal Chrome profile).
- Keep Chrome + WebDriver tooling updated.
- Avoid programmatically “humanizing” inputs (mouse/keyboard simulation) for protected flows; when a site challenges, the runner is designed to pause and let a human resolve it in the visible browser instead.

## Conservative scraper runner

Use the bounded runner for local debugging and controlled collection. It defaults to
dry-run mode, one ZIP, one page, and conservative delays. It does not write scraped
results unless `--save` is explicitly provided.

```bash
cd analyzer_package
./venv/bin/python -m re_analyzer.scrapers.conservative_scraper_runner \
  --provider zillow \
  --zip-code 32404 \
  --max-pages 1 \
  --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

The runner normalizes provider-native records into a source-neutral
`CanonicalListing` shape with:

- `source_name`
- `source_property_id`
- `canonical_property_id`
- normalized address, city, state, and ZIP
- price, rent estimate, home type, and URL when available

## Scraper to backend handoff

Generated scraper data is intentionally not part of git. By default it is written
under `re_analyzer/Data/`. On a private scraper/VPS host, set
`RE_ANALYZER_DATA_PATH` to a local persistent directory such as
`/opt/parcel/scraper_data` so the backend container can ingest the same files
through its `/app/scraper_data` mount. Treat canonical JSON, raw listing JSON,
property-detail payloads, screenshots, HTML, downloaded images, generated
visualizations, ad hoc analysis CSVs, and old Parquet exports as runtime/operator
artifacts.

When `--save` is set, each provider/ZIP scrape writes three operator-readable
files under `re_analyzer/Data/Fetched/{provider}/{zip_code}/`:

- `listings_TIMESTAMP.json`: provider-native listing payloads.
- `canonical_listings_TIMESTAMP.json`: the normalized listing rows the backend can ingest.
- `injection_manifest_latest.json`: the exact backend handoff for that provider/ZIP.

Run normalization after a scrape to prune old timestamps, rebuild the canonical
Parquet handoff, and write the global manifest:

```bash
cd analyzer_package
export RE_ANALYZER_DATA_PATH=/opt/parcel/scraper_data
./venv/bin/python -m re_analyzer.scrapers.normalize_data
```

The global manifest is
`$RE_ANALYZER_DATA_PATH/Fetched/injection_manifest.json` when the override is
set, otherwise `re_analyzer/Data/Fetched/injection_manifest.json`. The backend
`POST /api/admin/ingest` job prefers that manifest, then falls back to scanning
latest `canonical_listings_*.json` files if the manifest is missing.
The manifest intentionally excludes browser diagnostics, screenshots, HTML
pages, downloaded image binaries, and Chrome profile/cache files from the
database injection path.

### Zillow Florida refresh

The Zillow ZIP runner can discover the available page count from the first
response for each ZIP and continue through every discovered page, with a hard
per-ZIP page cap as a guardrail. Keep the default debug mode small, then promote
to the state-wide command only after the one-ZIP smoke test looks healthy.

Refresh the Zillow query-state cache first. This cache is the ZIP iterator used
by the conservative runner:

```bash
cd analyzer_package
./venv/bin/python -m re_analyzer.scrapers.zillow_search_query_state_scraper
```

Smoke-test dynamic page discovery without writing results:

```bash
cd analyzer_package
./venv/bin/python -m re_analyzer.scrapers.conservative_scraper_runner \
  --provider zillow \
  --zip-code 32404 \
  --all-pages \
  --max-discovered-pages-per-zip 2 \
  --manual-challenge-wait-seconds 45 \
  --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

Run a conservative Florida refresh. `--max-zip-codes 0` means every cached ZIP;
`--respect-cooldown` skips ZIPs whose metadata was refreshed recently; and
`--max-discovered-pages-per-zip 20` mirrors the old Zillow search scraper's page
guardrail.

```bash
cd analyzer_package
./venv/bin/python -m re_analyzer.scrapers.conservative_scraper_runner \
  --provider zillow \
  --all-pages \
  --max-discovered-pages-per-zip 20 \
  --max-zip-codes 0 \
  --respect-cooldown \
  --cooldown-hours 36 \
  --save \
  --min-delay-seconds 3 \
  --max-delay-seconds 6 \
  --zip-delay-seconds 20 \
  --manual-challenge-wait-seconds 45 \
  --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

If a long run stops mid-state, resume deterministically after the last completed
ZIP:

```bash
--start-after-zip 32404
```

This gives Zillow and Redfin data a common reconciliation layer. Redfin ZIP runs
resolve the provider's region token through Redfin's own location lookup endpoint,
then fetch bounded GIS pages from the active browser session.

```bash
cd analyzer_package
./venv/bin/python -m re_analyzer.scrapers.conservative_scraper_runner \
  --provider redfin \
  --zip-code 32934 \
  --max-pages 1 \
  --manual-challenge-wait-seconds 45 \
  --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

Realtor.com ZIP runs use the same headful browser session, then fetch bounded
Remix `.data` pages and decode the compact single-fetch payload into property
rows.

```bash
cd analyzer_package
./venv/bin/python -m re_analyzer.scrapers.conservative_scraper_runner \
  --provider realtor \
  --zip-code 32934 \
  --max-pages 1 \
  --manual-challenge-wait-seconds 45 \
  --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

To compare all providers in one bounded local run, use the side-by-side runner.
It launches one dry-run subprocess per provider, prefixes each output stream, and
uses separate temporary Chrome profiles by default to avoid profile-lock fights
between concurrent browser processes.

```bash
cd analyzer_package
./venv/bin/python -m re_analyzer.scrapers.side_by_side_scraper_runner \
  --zip-code 32404 \
  --max-pages 1 \
  --manual-challenge-wait-seconds 45 \
  --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

When the main app is running in Docker, start the host-side scraper controller so
the admin dashboard can launch the same side-by-side session while still using
your Mac Chrome:

```bash
cd analyzer_package
./venv/bin/python -m re_analyzer.scrapers.local_scraper_control_server \
  --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

Then open `/admin` and use the "Local Scraper Sessions" panel. The controller
binds to `127.0.0.1:5071` by default and is intended for local debugging only.
Set `LOCAL_SCRAPER_CONTROL_TOKEN` on both the backend and controller before
starting or cancelling scraper/probe runs; read-only health endpoints stay
available without a token for local diagnostics. When Docker-backed backend
proxying needs to reach it through `host.docker.internal`, also pass
`--host 0.0.0.0`.

Set `--startup-stagger-seconds 0` for a true simultaneous launch. A tiny stagger
such as `--startup-stagger-seconds 0.5` is still much faster than serial startup
and is usually easier to watch/debug.

Side-by-side mode tiles the provider browsers into equal columns by default, so
Zillow, Redfin, and Realtor can be viewed and manually verified at the same time.
It also defaults to `--no-stop-on-challenge`, which means a visible challenge is
logged and snapshotted, but the provider can still attempt backend continuation
requests from the loaded browser session.

Useful tiling knobs:

```bash
--tile-screen-width 1728 \
--tile-screen-height 1050 \
--tile-screen-x 0 \
--tile-screen-y 0 \
--tile-gap 8
```

For faster iteration, side-by-side mode passes the window geometry directly to
Chrome at launch and skips the slower post-launch resize call. If Chrome ignores
the launch geometry on your machine, add `--enforce-window-rect`. Side-by-side
mode also enables undetected-chromedriver's multi-process startup path by default,
which avoids the slow full serialization while protecting the shared driver
patcher from concurrent launch races. Use `--driver-startup-lock always` for a
slower maximum-caution first run, or `--no-driver-user-multi-procs` only when you
explicitly want the legacy serialized startup behavior.

The runner also records diagnostics when a page looks blocked, challenged, or
unexpectedly empty. By default it writes metadata, HTML, and a screenshot under:

```text
re_analyzer/Data/ScraperDiagnostics
```

Use `--no-debug-snapshots` to disable file snapshots. The runner stops on detected
captcha / robot checks by default instead of trying to automate around them.
