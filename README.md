# FF Live Dashboard

Automated live-tracking dashboard for the Fundamental First equity strategy.

## What this repo does, automatically

1. **Every trading-day evening** (7:00 PM IST) — refreshes OHLC prices, scans
   for fresh signals, rebuilds the dashboard, and publishes it.
2. **Every Sunday** — checks Screener.in for any newly-published quarterly
   results and merges them in (harmless no-op most weeks; only does
   something during results season).

## Where to see the live dashboard

Once GitHub Pages is turned on for this repo (Settings -> Pages -> source:
`main` branch, `/docs` folder), the live dashboard is always available at:

`https://<your-github-username>.github.io/ff-live-dashboard/`

No need to open this repo, Colab, or run anything manually — just visit
that link any time and refresh the page.

## Checking if an automated run worked

Click the **Actions** tab at the top of this repo. Each run shows a
green tick (succeeded) or red cross (failed) — click into any run to see
the full log, exactly like the Colab output you're used to.

## Running something manually

Actions tab -> pick the workflow ("Daily Dashboard Update" or "Weekly
Fundamentals Check") -> **Run workflow** button (top right) -> Run workflow.

## Files in this repo

| File | Purpose |
|---|---|
| `ohlc_data/` | Daily price history, one CSV per stock |
| `fund_flags.csv` | Quarterly fundamentals + rolling-high flags |
| `ident.csv` | Stock -> market-cap segment mapping |
| `FF_Yahoo_OHLC_Updater.py` | Refreshes OHLC from Yahoo Finance |
| `FF_Screener_Fundamentals_Scraper.py` | Scrapes latest fundamentals from Screener.in |
| `FF_fundamentals_ingest.py` | Merges new fundamentals into fund_flags.csv |
| `FF_Fresh_Signal_Scanner.py` | Finds new qualifying signals |
| `FF_build_dashboard.py` | Builds the dashboard HTML |
| `FF_dashboard_template.html` | Dashboard template (do not edit directly) |
| `.github/workflows/` | The automation schedules |
