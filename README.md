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
```

## SAPP Examples

Scrape one delivery date locally:

```text
POST /sapp/scrape?job_name=constrained_area_results&delivery_date=2026-05-16
```

Scrape a delivery date range locally:

```text
POST /sapp/scrape-range?job_name=constrained_area_results&start_date=2026-05-10&end_date=2026-05-15
```

Read constrained area results:

```text
GET /sapp/constrained-area-results?start_time=2026-05-10T00:00:00&end_time=2026-05-15T23:59:59&frequency=1h
```

Supported frequencies:

```text
1h, 4h, 1d, 1w, 1mo, 1y
```
