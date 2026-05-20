# Power Trading Backend

FastAPI backend for SAPP data ingestion and time series access.

## Features

- SAPP MTP document scraping with Selenium and Firefox
- SAPP constrained area result extraction and MongoDB upserts
- Date range scraping for multiple SAPP documents in one run
- Frequency-based aggregation for constrained area time series data
- Power system telemetry endpoints
- Health and database health checks

## Run Locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Docker

```bash
docker build -t power-trading-backend .
docker run --rm --name power-trading-backend --env-file .env -p 8000:8000 power-trading-backend
```

## Scraping Deployment Note

Run SAPP scraping locally, not on the deployed server.

The scraping endpoints start Selenium, Firefox, and geckodriver. That browser stack needs more memory than the current hosted server can reliably provide, so server-side scraping may fail with memory limit restarts or browser startup errors. The deployed server should be used to serve already-imported data from MongoDB.

Recommended workflow:

1. Run scraping locally against the same MongoDB database.
2. Let the scraper upsert the extracted records into MongoDB.
3. Use the deployed API for reading SAPP constrained area results.

## Environment Variables

```text
MONGODB_URL=
DATABASE_NAME=
API_TITLE=
API_VERSION=
DEBUG=
SAPP_USERNAME=
SAPP_PASSWORD=
```

## Main Endpoints

```text
GET  /health
GET  /health/db
GET  /sapp/scrape-jobs
POST /sapp/scrape
POST /sapp/scrape-range
GET  /sapp/constrained-area-results
GET  /sapp/constrained-area-results/{delivery_date}
POST /sapp/bids
GET  /sapp/time-of-use-periods
GET  /sapp/bid-trading-period
GET  /sapp/submitted-bids/summary
GET  /sapp/submitted-bids/{bid_id}/comparison
GET  /sapp/bids/history
GET  /sapp/bids/{bid_id}
PATCH /sapp/bids/{bid_id}
POST /sapp/bids/{bid_id}/submit
POST /sapp/bid-templates
GET  /sapp/bid-templates
GET  /sapp/bid-templates/{template_id}
PATCH /sapp/bid-templates/{template_id}
DELETE /sapp/bid-templates/{template_id}
POST /contracts
GET  /contracts
GET  /contracts/{contract_id}
PATCH /contracts/{contract_id}
DELETE /contracts/{contract_id}
POST /contracts/{contract_id}/files
DELETE /contracts/{contract_id}/files/{file_id}
GET  /resource-forecasting/reservoirs
POST /resource-forecasting/level-monitoring/fields
GET  /resource-forecasting/level-monitoring/fields
PATCH /resource-forecasting/level-monitoring/fields/{field_id}
POST /resource-forecasting/level-monitoring/records
GET  /resource-forecasting/level-monitoring/records
GET  /resource-forecasting/level-monitoring/records/{record_id}
PATCH /resource-forecasting/level-monitoring/records/{record_id}
DELETE /resource-forecasting/level-monitoring/records/{record_id}
GET  /resource-forecasting/level-monitoring/aggregate
GET  /resource-forecasting/dam-calculation/configs
POST /resource-forecasting/dam-calculation/calculate
GET  /energy-scheduling/yearly-budget/defaults
POST /energy-scheduling/yearly-budget/calculate
GET  /energy-scheduling/yearly-budgets/budgetable
PUT  /energy-scheduling/yearly-budgets/budgetable/{year}
POST /energy-scheduling/yearly-budgets
GET  /energy-scheduling/yearly-budgets
GET  /energy-scheduling/yearly-budgets/{budget_id}
PATCH /energy-scheduling/yearly-budgets/{budget_id}
DELETE /energy-scheduling/yearly-budgets/{budget_id}
POST /energy-scheduling/equivalent-water-volume
```

Contract records require `expiration_date`. If `duration` is omitted, the API
derives it as a date range from `effective_date` to `expiration_date`. Date
fields can carry reminder preferences for future alert processing via
`expiration_reminder` or `custom_fields[].reminder`.

## SAPP Examples

Scrape one delivery date locally:

```text
POST /sapp/scrape?job_name=constrained_area_results&delivery_date=2026-05-16
POST /sapp/scrape?job_name=constrained_area_results&delivery_date=2026-05-16&page_start=12
```

Scrape a delivery date range locally:

```text
POST /sapp/scrape-range?job_name=constrained_area_results&start_date=2026-05-10&end_date=2026-05-15
POST /sapp/scrape-range?job_name=constrained_area_results&start_date=2026-05-10&end_date=2026-05-15&page_start=12
```

Read constrained area results:

```text
GET /sapp/constrained-area-results?start_time=2026-05-10T00:00:00&end_time=2026-05-15T23:59:59&frequency=1h
```

Supported frequencies:

```text
1h, 4h, 1d, 1w, 1mo, 1y
```

## Bid Construction

Supported bid markets are `dam`, `fpm_w`, and `fpm_m`.

- `dam` bids use `delivery_date` and hourly quantity cells with `hour`.
- `fpm_w` bids use `week_start_date`, which must be a Monday, and product quantity cells with `product`.
- `fpm_m` bids use `month_start_date`, which must be the first day of the calendar month, and product quantity cells with `product`.

FPM products are `off_peak`, `peak`, and `standard`.

Time-of-use period mappings are available from:

```text
GET /sapp/time-of-use-periods?delivery_date=2026-05-18
GET /sapp/time-of-use-periods?start_date=2026-05-18&end_date=2026-05-24
GET /sapp/bid-trading-period?market=fpm_w&reference_date=2026-05-18
GET /sapp/bid-trading-period?market=fpm_m&reference_date=2026-05-18
GET /sapp/bid-trading-period?market=fpm_w&reference_date=2026-05-18&mode=period
GET /sapp/bid-trading-period?market=fpm_m&reference_date=2026-05-18&mode=period
```

For FPM-W, `reference_date` is the bid placement date. Placement before Friday maps
to the next Monday-Sunday trading week; Friday or later maps to the following
Monday-Sunday week.

For FPM-M, `reference_date` is the bid placement date. Placement on or before
the monthly deadline maps to the next calendar month. If the last Wednesday
before the next month is 5 days or fewer before the 1st, the deadline moves to
the previous Wednesday.

Use `mode=period` when reconstructing a historical or future bid directly for a
chosen trading week or month. In that mode the `reference_date` is any date
inside the desired trading period, and placement deadlines are not used to choose
the period.

Submitted bid/result comparison endpoints:

```text
GET /sapp/submitted-bids/summary?market=dam&start_date=2026-05-01&end_date=2026-05-31
GET /sapp/submitted-bids/{bid_id}/comparison
GET /sapp/submitted-bids/{bid_id}/comparison?delivery_date=2026-05-18
```

## Resource Forecasting

Level monitoring currently supports `mps` and `lps` reservoirs.

Create a persistent extra field:

```text
POST /resource-forecasting/level-monitoring/fields
{
  "reservoir": "mps",
  "label": "Rainfall",
  "field_type": "number",
  "unit": "mm"
}
```

Create a daily level record:

```text
POST /resource-forecasting/level-monitoring/records
{
  "reservoir": "mps",
  "record_date": "2026-05-19",
  "daily_inflow": 120.5,
  "unaccounted_inflow": 3.2,
  "reservoir_level_value": 415.8,
  "reservoir_level_unit": "ft",
  "custom_fields": {
    "rainfall": 8.4
  }
}
```

Read records and configured extra fields:

```text
GET /resource-forecasting/level-monitoring/records?reservoir=mps&start_date=2026-05-01&end_date=2026-05-31&limit=100
```

Aggregate records:

```text
GET /resource-forecasting/level-monitoring/aggregate?reservoir=mps&group_by=week
GET /resource-forecasting/level-monitoring/aggregate?reservoir=lps&group_by=month&start_date=2026-01-01&end_date=2026-12-31
```

Dam calculation tool:

```text
GET /resource-forecasting/dam-calculation/configs
GET /resource-forecasting/dam-calculation/configs?include_lookup=true
```

Returns available dams and default input values. Supported dam codes are
`mita_hills` and `mulungushi`. Use `include_lookup=true` to return the full
lookup tables and calculation constants for client-side calculations.

```text
POST /resource-forecasting/dam-calculation/calculate
{
  "dam": "mita_hills",
  "current_level_ft": 216.95,
  "evaporation_rate": 0.042,
  "production_rate_mw": 23.24
}
```

The calculation returns the selected lookup range, calculated dam volume, useful
dam volume, percentage fill, equivalent energy, and projected generation days and
months. The lookup tables are stored in `app/core/dam_calculations.py` so both
the config endpoint and backend calculation run in memory without database reads.

## Energy Scheduling

Yearly Power Sources budget defaults:

```text
GET /energy-scheduling/yearly-budget/defaults?year=2026
```

Calculate without saving:

```text
POST /energy-scheduling/yearly-budget/calculate
```

Current and next year budget page:

```text
GET /energy-scheduling/yearly-budgets/budgetable
GET /energy-scheduling/yearly-budgets/budgetable?base_year=2026
PUT /energy-scheduling/yearly-budgets/budgetable/2026
PUT /energy-scheduling/yearly-budgets/budgetable/2027?base_year=2026
```

The budgetable endpoint returns exactly two records: current year and next year.
If a saved budget exists for a year, the latest saved budget is returned;
otherwise the response contains default calculated values with `is_saved=false`.
The PUT endpoint upserts the latest budget for one of those two years.

Save and manage yearly budgets:

```text
POST /energy-scheduling/yearly-budgets
GET /energy-scheduling/yearly-budgets?year=2026
GET /energy-scheduling/yearly-budgets/{budget_id}
PATCH /energy-scheduling/yearly-budgets/{budget_id}
DELETE /energy-scheduling/yearly-budgets/{budget_id}
```

Equivalent water-volume backpass:

```text
POST /energy-scheduling/equivalent-water-volume
{
  "dam": "mulungushi",
  "generation_mw": 24.074074074,
  "hours": 720
}
```

The Power Sources calculation mirrors the workbook table from rows 7-19 and
returns all computed rows in one response. MPS water-volume conversion uses the
Mulungushi factor, and LPS uses the Mita Hills factor.
