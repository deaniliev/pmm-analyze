# Analyze PMM metrics with AI

## Get metrics by script

This script scrapes the metrics locally and normalizes data to reduce cost of the ai analysis.

How it works:

1. Create .env file as per the .env.example
2. run the scripts by running
   python3 analyze.py

3. The script will print the AI prompt including the system instructions

Take the json data and ask your favorite AI provider to analyze the data

## Choosing the time frame

By default the script analyzes the last 3 days. Use `--start` / `--end` to pick an explicit
window, or `--last` to change the relative one:

```bash
python3 analyze.py --last 12h                                  # last 12 hours
python3 analyze.py --start '2026-08-10' --end '2026-08-12 18:00'
python3 analyze.py --start '2026-08-10 09:00'                  # from that moment until now
python3 analyze.py --end '2026-08-12 18:00' --last 6h          # 6 hours before that moment
```

Dates accept `YYYY-MM-DD`, `YYYY-MM-DD HH:MM[:SS]`, a UNIX timestamp or `now`.
Relative periods accept minutes, hours, days and weeks: `90m`, `12h`, `3d`, `2w`.

Two related options:

- `--step` sets the Prometheus sampling resolution (default `300s`). Prometheus allows at most
  11000 data points per query, so long windows need a bigger step — the script tells you which
  one to use if the window is too large.
- `--output` sets the raw data file. Without it the file name is derived from the period,
  e.g. `pmm_telemetry_20260810-0000_20260812-1800.json`.

All four can also be set in `.env` as `START_TIME`, `END_TIME`, `LAST_PERIOD`, `STEP` and
`OUTPUT_DATA_FILE`; command line arguments take precedence.

