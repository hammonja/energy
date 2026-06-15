# Energy Project Context

This project is a small local web app for tracking Octopus Energy electricity prices and actual smart-meter consumption. It is currently implemented as a single Python file, `octopus.py`, which serves an HTML/JavaScript dashboard and runs a background collector thread.

The app is intended to run on a Raspberry Pi or similar always-on machine. The user links an Octopus account by entering an account number and API key in the web UI. The app then discovers the active electricity tariff, logs price data, discovers comparable tariffs for the same product, and plots everything in the browser.

## Main Files

- `octopus.py`: Python HTTP server, background collector, Octopus API integration, embedded HTML/CSS/JavaScript frontend.
- `energy.db`: SQLite database storing the linked Octopus account ID and API key in `account_config`.
- `energy_seed.sql`: Ignored deployment seed script from an earlier migration. It contains the API key if regenerated from the live DB, so keep it private.
- `energy_schema_reset.sql`: Ignored schema reset script. It drops and recreates empty SQLite tables using the current lean schema.
- `octopus_config.json`: Legacy config file. The app can migrate from it if `energy.db` is empty, but current config reads/writes use SQLite.
- `octopus_prices.json`: Stores collected price samples.
- `.gitignore`: Open in the IDE during this work, but not central to the app behaviour.

## Current Features

- Links an Octopus account using account number and API key.
- Uses the Octopus account API to find the active import electricity tariff.
- Records the linked account tariff price over time.
- Uses the Octopus REST product endpoint to discover other electricity tariffs for the same product.
- Logs comparison tariff prices as well as the linked account tariff.
- Plots tariff rates as line charts in `p/kWh`.
- Highlights the linked account tariff in blue.
- Shows comparison tariffs in weak pastel colours.
- Fetches actual half-hourly smart-meter consumption from:
  `/electricity-meter-points/{MPAN}/meters/{METER_SERIAL}/consumption/`
- Plots actual consumption as bar charts in `kWh` on a second right-hand axis.
- Displays MPAN-derived meter serial information in the dashboard header.
- Shows clear status text for price samples and usage chunks.

## Important Behaviour

Account config is stored in `energy.db`, next to `octopus.py`.

The config table is:

- `account_config(account_id TEXT NOT NULL, api_key TEXT NOT NULL, mpan TEXT, meter_serial TEXT)`

The app keeps this as a single-row table and maps `account_id` to the existing UI field called `account_number`. Because this installation has a single MPAN and meter serial, `mpan` and `meter_serial` are stored once here rather than repeated on every consumption row.

Price data remains in `octopus_prices.json`:

- `samples`: tariff price samples.

Actual consumption is stored in `energy.db`.

The consumption table is:

- `consumption_readings(interval_start TEXT, interval_end TEXT, consumption_kwh REAL, collected_at TEXT)`

The primary key is `(interval_start, interval_end)`, so repeated collector runs upsert delayed or refreshed Octopus usage data without duplicating rows.

Consumption is not live. Octopus usually publishes smart-meter consumption after a delay. The app requests a rolling 31-day window and stores readings by MPAN, meter serial, interval start, and interval end.

The consumption endpoint is paginated. The app follows Octopus `next` links and also requests a large `page_size`, so it should pull the full available 31-day window rather than only the first page.

## Timezone Fixes

The Pi clock was correct, but the app originally recorded site timestamps in UTC using `Z`, which looked one hour out during British Summer Time.

Current behaviour:

- Octopus API comparisons still use UTC internally, because Octopus timestamps are absolute.
- The app records its own local collection/update timestamps with the local timezone offset, for example `+01:00`.
- JSON reading uses `utf-8-sig` so BOM-marked JSON files do not break the app.
- Existing old `Z` timestamps may remain in stored historical data, but new app-generated timestamps use local offset time.

## Graph Behaviour

The dashboard uses Plotly.

- Price traces are grouped by `tariff_code` and `rate_name`.
- Linked account trace stays blue (`#0078a8`) and thicker.
- Comparison tariff traces use soft pastel colours and lower opacity.
- Actual consumption is plotted as semi-transparent bars on `yaxis2` in `kWh`.
- Tariff rates remain on the left axis in `p/kWh`.
- The lower Plotly range slider was removed because the user did not like the second mini graph under the main graph.
- The default range is `All`.
- Range buttons still exist for 30 min, 6 hours, day, week, month, and all.

## Octopus API Notes

The active tariff is found from:

- `/accounts/{ACCOUNT_NUMBER}/`

The app scans account properties and electricity meter points, ignores export MPANs, and chooses the currently active import agreement.

The active product/tariff is used to build price endpoints:

- `/products/{PRODUCT_CODE}/electricity-tariffs/{TARIFF_CODE}/{RATE_ENDPOINT}/`

The app handles:

- `standard-unit-rates`
- `day-unit-rates`
- `night-unit-rates`

Product tariff discovery originally treated `_A`, `_B`, etc. as tariff codes. That was wrong: those are region buckets. The parser now recursively walks the product payload and only accepts real electricity tariff codes beginning with `E-1R-` or `E-2R-`.

Consumption endpoint:

- `/electricity-meter-points/{MPAN}/meters/{METER_SERIAL}/consumption/`

The app URL-escapes MPAN and meter serial path components.

The app discovers all meter serials returned for the MPAN and tries each serial until one returns consumption rows. It records `last_consumption_query` with MPAN, serials tried, query window, and row counts.

## Known User Account Context

Observed during development:

- Account number/account ID: stored in `energy.db`.
- MPAN seen in app data: `2000005457590`.
- Meter serial seen in UI/status: `Z14N131482`.
- Product: Intelligent Octopus Go.
- Product code: `INTELLI-VAR-24-10-29`.
- Active tariff seen: `E-1R-INTELLI-VAR-24-10-29-H`.

Do not expose or print the API key.

## Data Cleansing Already Done

The user asked to cleanse data before 14:00 local time on 15 June 2026. `octopus_prices.json` was rewritten to remove samples before that cutoff.

A PowerShell rewrite briefly introduced a UTF-8 BOM, causing:

`Unexpected UTF-8 BOM (decode using utf-8-sig)`

This was fixed by:

- Changing JSON reads to `encoding="utf-8-sig"`.
- Rewriting `octopus_prices.json` and the legacy `octopus_config.json` without the BOM.

## Current Verification

After the latest changes, `python -m py_compile octopus.py` passed.

Network access from the coding workspace was restricted, so Octopus API calls were mostly verified through the user running the app and reporting status messages. The app did successfully find consumption for at least 17 May, proving the endpoint and meter pair can work. Pagination was then added because only the first page was being read.

## Useful Future Tasks

Potential next improvements:

- Add a manual meter serial override field if Octopus account data ever returns the wrong serial.
- Add a separate consumption summary tile, e.g. today kWh, yesterday kWh, last 7 days kWh.
- Add cost calculation by matching consumption chunks to the active tariff rate for that interval.
- Add pruning/retention settings for `octopus_prices.json`.
- Split embedded HTML out of `octopus.py` if the frontend grows.
- Add a small `/api/debug/consumption` endpoint to expose the latest query metadata directly.
