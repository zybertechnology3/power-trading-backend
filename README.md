# Power Trading Backend

FastAPI backend for SAPP data ingestion and time series access.

## Features

- SAPP MTP document scraping with Selenium and Firefox
- SAPP constrained and unconstrained area result extraction and MongoDB upserts
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

## Render / Deployment Note

The backend can be deployed on Render using the included Dockerfile.

The scraping endpoints run Selenium, Firefox, and geckodriver in headless mode on the server. That means the deployed API can be used both to scrape and to serve already-imported MongoDB data, provided the Render service has enough memory and time for the request.

Recommended workflow:

1. Deploy the Docker image to Render as a web service.
2. Set the MongoDB and SAPP credentials as Render environment variables.
3. Call the scrape endpoints remotely when you want the server to import fresh data.
4. Use the read endpoints for charting and API access.

If you enable the backend scheduler, keep the app on a single worker/process and set:

```text
SCRAPE_SCHEDULER_ENABLED=true
SCRAPE_SCHEDULER_TIMEZONE=CAT
SCRAPE_SCHEDULER_POLL_SECONDS=30
SCRAPE_SCHEDULES_JSON=[...]
```

The scheduler runs inside the FastAPI process, so it should not be paired with multiple Uvicorn workers.
`CAT` is normalized to Zambia time (`Africa/Lusaka`).

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
GET  /sapp/scheduled-scrapes
GET  /sapp/scheduled-scrapes/jobs
POST /sapp/scheduled-scrapes/trigger
GET  /sapp/scheduled-scrapes/runs
GET  /sapp/scheduled-scrapes/runs/{run_id}
POST /sapp/scrape
POST /sapp/scrape-range
GET  /sapp/public-holidays
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
GET  /outage-requests/generating-units
GET  /outage-requests
GET  /outage-requests/{id}
POST /outage-requests
PUT  /outage-requests/{id}
DELETE /outage-requests/{id}
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
GET  /resource-forecasting/hydrology-forecasting
PUT  /resource-forecasting/hydrology-forecasting
POST /resource-forecasting/hydrology-forecasting/calculate
GET  /resource-forecasting/solar-forecasting
GET  /resource-forecasting/solar-forecasting/plants
POST /resource-forecasting/solar-forecasting/records
GET  /resource-forecasting/solar-forecasting/records
GET  /resource-forecasting/solar-forecasting/records/{record_id}
PATCH /resource-forecasting/solar-forecasting/records/{record_id}
DELETE /resource-forecasting/solar-forecasting/records/{record_id}
GET  /resource-forecasting/solar-forecasting/aggregate
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
GET  /metering/sites
GET  /metering/meters
PATCH /metering/meters/{meter_id}
POST /metering/meter-capture/readings
GET  /metering/meter-capture/readings
GET  /metering/meter-capture/readings/{record_id}
PATCH /metering/meter-capture/readings/{record_id}
DELETE /metering/meter-capture/readings/{record_id}
```

Contract records require `expiration_date`. If `duration` is omitted, the API
derives it as a date range from `effective_date` to `expiration_date`. Date
fields can carry reminder preferences for future alert processing via
`expiration_reminder` or `custom_fields[].reminder`.

## Contract Operations

Power outage requests:

```text
GET /outage-requests/generating-units
GET /outage-requests?unit=MPS%20UNIT%204&reason=PM&from=2026-06-01T00:00:00Z&to=2026-06-30T23:59:59Z
GET /outage-requests/{id}

POST /outage-requests
{
  "unit_code": "MPS UNIT 4",
  "reason": "PM",
  "start_at": "2026-06-10T08:00:00Z",
  "restore_at": "2026-06-10T14:30:00Z",
  "expected_mw_reduction": 24.5,
  "description": "Preventive maintenance outage"
}

PUT /outage-requests/{id}
DELETE /outage-requests/{id}
```

Each saved outage record is treated as approved for scheduling purposes.
`unit_name` is denormalized from the generating-unit lookup, and `duration_hrs`
is derived from `restore_at - start_at`. Requests validate `restore_at >
start_at` and reject overlapping outage windows for the same `unit_code`.

## SAPP Examples

Zimbabwe public holidays for SAPP market calendar logic:

```text
GET /sapp/public-holidays?year=2026
GET /sapp/public-holidays?year=2026&public_only=false
```

The endpoint fetches Zimbabwe (`ZW`) holidays from the configured public
holidays provider and normalizes the response to snake_case fields:
`date`, `local_name`, `name`, `country_code`, `fixed`, `global_holiday`,
`counties`, `launch_year`, and `types`. The provider base URL defaults to
`https://date.nager.at/api/v3` and can be overridden with
`PUBLIC_HOLIDAYS_API_BASE_URL`.

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

The constrained scrape job also imports the matching DAM unconstrained results
document for the same delivery date:

```text
POST /sapp/scrape?job_name=constrained_area_results&delivery_date=2026-06-26
POST /sapp/scrape-range?job_name=constrained_area_results&start_date=2026-06-24&end_date=2026-06-26
```

Those calls now run both `constrained_area_results` and
`unconstrained_area_results`. Unconstrained rows are stored in
`sapp_unconstrained_area_results` with hourly `total_purchase_volume_mw`,
`total_sales_volume_mw`, `price_usd_per_mwh`, and `price_zar_per_mwh`.

`GET /sapp/constrained-area-results` keeps constrained rows in `records` and
adds `unconstrained_records` plus `unconstrained_total`. The day endpoint now
returns both arrays:

```text
GET /sapp/constrained-area-results/2026-06-26
{
  "delivery_date": "2026-06-26",
  "constrained_records": [],
  "unconstrained_records": []
}
```

The list endpoint also accepts date-range filters:

```text
GET /sapp/constrained-area-results?start_date=2026-06-20&end_date=2026-06-26
GET /sapp/constrained-area-results?start_date=2026-06-20&end_date=2026-06-26&frequency=1d
```

The standalone `area_results_test_scraper.py` script now persists:

- hourly constrained rows in `sapp_constrained_area_results`
- hourly unconstrained rows in `sapp_unconstrained_area_results`

Each hourly row now carries explicit search-window metadata such as
`search_delivery_date`, `window_start_date`, `window_end_date`,
`window_offset_days`, `product`, and `category`.

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

Hydrology forecasting:

```text
GET /resource-forecasting/hydrology-forecasting
GET /resource-forecasting/hydrology-forecasting?base_date=2026-05-21
```

Returns a 24-month forecast for the current year and next year for both MPS and
LPS. Months before the current month use the latest monitoring record in that
month as the month-end level. The current month and future months start from the
latest monitoring level before the current month. Each projected month is then
calculated as previous month level minus that month's equivalent water volume
from the yearly budget. If no saved budget exists for a year, the default yearly
budget is used. If a saved hydrology rainfall forecast exists for the selected
base year, `GET /resource-forecasting/hydrology-forecasting` applies it and
returns `saved_forecast_id` plus `saved_forecast_updated_at`.

Use rainfall allocations to preview the red rainfall-adjusted line/bar without
saving:

```text
POST /resource-forecasting/hydrology-forecasting/calculate
{
  "base_date": "2026-05-21",
  "rainfall": {
    "mps": {
      "total_volume_mm3": 30,
      "monthly_allocations_mm3": {
        "2026-06": 10,
        "2026-12": 20
      }
    },
    "lps": {
      "total_volume_mm3": 12,
      "monthly_allocations_mm3": {
        "2026-11": 12
      }
    }
  }
}
```

Rainfall volumes are in `Mm3`, matching the yearly budget equivalent water-volume
unit. Allocations may be less than the total while the user is editing; the
response includes `rainfall_remaining_volume_mm3`. If monthly allocations exceed
the total, the API no longer rejects the request; it returns a negative remaining
value and `rainfall_overallocated_volume_mm3` for the frontend to display.

Rainfall allocations are converted from `Mm3` to reservoir level using the same
dam lookup curve as dam calculations. For each projected month, the red line is
calculated from `projected_volume_m3 + rainfall_volume_m3`, converted back to
feet as `rainfall_adjusted_level_ft`, and clamped to the dam calculation min/max
level if it exceeds the configured range. The response also includes the derived
`rainfall_level_adjustment_ft`.

Save the hydrologist's forecast with:

```text
PUT /resource-forecasting/hydrology-forecasting
{
  "base_date": "2026-05-21",
  "rainfall": {
    "mps": {
      "total_volume_mm3": 30,
      "monthly_allocations_mm3": {
        "2026-06": 10,
        "2026-12": 20
      }
    }
  }
}
```

The saved forecast is keyed by the base year and is reused by later GET requests
until it is overwritten by another PUT for the same base year.

Each reservoir response includes monthly `projected_level_ft` for the green
forecast and `rainfall_adjusted_level_ft` for the red rainfall-adjusted forecast.
MPS uses the Mulungushi dam computation model and LPS uses the Mita Hills model
to convert between water volume and feet.

Solar forecasting:

```text
GET /resource-forecasting/solar-forecasting/plants
GET /resource-forecasting/solar-forecasting?plant=lps_solar&base_date=2026-05-21
```

Returns a 24-month solar irradiation series for the current year and next year.
Previous months and the current month use stored daily irradiation records as
actuals. Future months use predicted irradiation from historical same-month
averages, falling back to a seasonal default curve if no historical data exists.
Irradiation values are in `W/m2`. The response includes a `weather_condition`
hint (`sunny`, `partly_cloudy`, `cloudy`, or `overcast`) that the frontend can
map to its own weather graphic.

Daily irradiation records:

```text
POST /resource-forecasting/solar-forecasting/records
{
  "plant": "lps_solar",
  "record_date": "2026-05-21",
  "irradiation_w_m2": 742.5,
  "weather_condition": "sunny",
  "notes": "Morning station reading"
}

GET /resource-forecasting/solar-forecasting/records?plant=lps_solar&start_date=2026-05-01&end_date=2026-05-31&limit=100
PATCH /resource-forecasting/solar-forecasting/records/{record_id}
DELETE /resource-forecasting/solar-forecasting/records/{record_id}
GET /resource-forecasting/solar-forecasting/aggregate?plant=lps_solar&group_by=month&start_date=2026-01-01&end_date=2026-12-31
```

Fresh databases are seeded with deterministic demo daily irradiation readings for
`lps_solar` from January 1 of the previous year through the current date.

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

## Metering

Meter Capture supports 30-minute interval readings for MPS and LPS. Each site has
four meters, and each meter can be set to `manual` or `automatic` entry mode.
Automatic capture is only a mode flag for now; automatic ingestion can be added
later without changing the table contract.

```text
GET /metering/sites
GET /metering/meters?site=mps
PATCH /metering/meters/mps_meter_1
{
  "entry_mode": "manual"
}
```

Meter capture rows:

```text
GET /metering/meter-capture/readings?site=mps&limit=100
GET /metering/meter-capture/readings?site=lps&start_time=2026-05-21T00:00:00Z&end_time=2026-05-21T23:30:00Z

POST /metering/meter-capture/readings
{
  "site": "mps",
  "interval_start": "2026-05-21T10:30:00Z",
  "source": "manual",
  "readings": {
    "mps_meter_1": 24.125,
    "mps_meter_2": 25.8,
    "mps_meter_3": 28.4,
    "mps_meter_4": 30.05
  },
  "notes": "Manual meter capture"
}

PATCH /metering/meter-capture/readings/{record_id}
DELETE /metering/meter-capture/readings/{record_id}
```

List responses include `meters` for table columns and `records` for interval
rows. `interval_start` must be aligned to `:00` or `:30`. Fresh databases are
seeded with deterministic half-hourly dummy readings for both MPS and LPS across
the previous seven days.
