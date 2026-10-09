# Analyze PMM metrics with AI

## Get metrics

This script scrapes metrics over the PMM HTTP API and normalizes the data to reduce the cost of AI analysis. It detects PMM 2 vs PMM 3 and uses the matching Query Analytics endpoints, so Docker access to the PMM container is not required.

**This script scrapes system, network, MySQL/InnoDB and process-level metrics:**

- **System**: CPU usage/load, CPU iowait % and steal % (VM noisy-neighbor signal), RAM, swap.
- **Disk**: read/write throughput, %util, read/write latency (ms), free space (root and the lowest free % across all mounts).
- **Network**: throughput (MB/s), packets/sec, plus rx/tx errors, drops, and TCP retransmit rate.
- **MySQL/InnoDB**: slow queries, thread/connection counts and connection-used %, aborted connects, thread-cache misses, temp-tables-to-disk %, table lock waits, deadlocks, InnoDB buffer pool hit ratio, row lock waits/avg wait time, log writes, dirty page %.
- **Processes**: process-exporter process count, RSS and swap, plus top process groups by memory/count.
- **Slow queries**: DIGESTED slow queries from Query Analytics (QAN).

How it works:

1. Create .env file as per the .env.example
   
   (or several, e.g. `.env-customer1`, and select one with `--config`)
   
2. run the scripts by running
   
   python3 analyze.py --config .env

3. The script will print the AI prompt including the system instructions

Take the json data and ask your favorite AI provider to analyze the data

## Selecting a config file

Without `--config` the script reads `.env` from the current directory, falling back to `.env`
next to the script. If neither exists it stops with an error instead of querying a default
`localhost` PMM.

Use `--config` to point at a specific file, which is handy when one host runs several PMM
installations:

```bash
python3 analyze.py --config ./.env-customer1
python3 analyze.py --config /root/.env-customer2 --last 12h
```

A missing `--config` file is reported and the script exits. Each file has the same format as
`.env.example`. When `--config` is given, an `env.py` sitting next to the script is skipped so
it cannot silently override the config you selected.

## Choosing the time frame

Run `python3 analyze.py --help` for the full guide on selecting a period, including all
accepted date formats and worked examples.

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


## Selecting a server

When PMM monitors several servers, analyze one of them with `--server` (alias `--node`).
The value is the PMM node name, as shown in PMM's inventory and dashboards:

```bash
python3 analyze.py --list-servers                        # node names with data in the period
python3 analyze.py --server mysql.example.com --last 12h
python3 analyze.py --server mysql                        # short name, if it is unambiguous
```

With `--server`, everything is limited to that node: CPU, RAM, disk and network metrics,
MySQL/InnoDB metrics, process-exporter groups, and the QAN slow queries and their examples.
The node name is also written to the JSON (`node_name`), the AI prompt, and the default
output file name, e.g. `pmm_telemetry_mysql.example.com_20260810-0000_20260812-1800.json`.

The name is matched exactly, then case-insensitively, then by the short host name (the part
before the first dot) in either direction. An unknown or ambiguous name stops the script and
lists the available nodes.

Without `--server` and with several nodes in PMM, the script prints a warning: the metrics of
all servers are then mixed (for example `sum(...)` adds up all nodes) and are not meaningful
for a root cause analysis.

A default can be set in `.env` as `NODE_NAME`; `--server` takes precedence.

How it works: PMM stores metrics in VictoriaMetrics, so the script passes
`extra_label=node_name=<node>` with every Prometheus query, which applies the filter to every
series in the expression, and sends `labels: [{key: node_name, value: [<node>]}]` to the QAN API
(the same filter the QAN web UI uses). The QAN labels endpoint, which fills `service_names` of a
query, has no such filter, so those names are limited to the node's services as reported by
VictoriaMetrics.
Before collecting, it checks that the backend honours the filter, and stops instead of
returning mixed data if it does not.
