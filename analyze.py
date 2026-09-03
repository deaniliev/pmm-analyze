#!/usr/bin/env python3
import argparse
import subprocess
import requests
import json
import sys
import os
import re
import math
import importlib.util
from datetime import datetime, timedelta

# Ignore warnings about a self-signed SSL certificate (InsecureRequestWarning)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 1. DEFAULT VALUES
# ==========================================
PMM_CONTAINER_NAME = "pmm-server"
PMM_URL = "https://localhost:8443"
PMM_USER = "admin"
PMM_PASS = "your_pmm_password"

CLICKHOUSE_USER = "default"
CLICKHOUSE_PASS = "clickhouse"

USE_AI = False
AI_API_KEY = "your_api_key_here"

# Empty means "generate a file name from the selected period"
OUTPUT_DATA_FILE = ""

# Time window: START/END are specific dates, LAST is a relative period (e.g. 3d, 12h, 90m)
START_TIME = ""
END_TIME = ""
LAST_PERIOD = "3d"
STEP = "300s"  # 5 minutes (300 seconds)

# ==========================================
# 2. LOADING CONFIGURATION FROM FILES
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class ConfigError(Exception):
    """Missing or unreadable configuration file."""


def default_config_candidates():
    """Locations searched for .env when --config is not set."""
    candidates = [os.path.abspath(".env")]
    script_env = os.path.join(SCRIPT_DIR, ".env")
    if script_env not in candidates:
        candidates.append(script_env)
    return candidates


def resolve_config_file(explicit_path):
    if explicit_path:
        path = os.path.abspath(os.path.expanduser(explicit_path))
        if not os.path.isfile(path):
            raise ConfigError(
                f"⚠️ The specified configuration file was not found: {explicit_path}\n"
                f"   Looked in: {path}"
            )
        return path

    candidates = default_config_candidates()
    for path in candidates:
        if os.path.isfile(path):
            return path

    tried = "\n".join(f"   - {path}" for path in candidates)
    raise ConfigError(
        "❌ No .env file found. Checked locations:\n"
        f"{tried}\n"
        "   Create a .env from .env.example or pass another file with --config."
    )


def load_env_file(path):
    try:
        from dotenv import load_dotenv
    except ImportError:
        # Fallback parser when python-dotenv is not installed
        try:
            values = {}
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        values[key.strip()] = val.strip().strip("'\"")
        except OSError as e:
            raise ConfigError(f"❌ The configuration file cannot be read: {e}")

        # setdefault: the real environment takes priority over the file, as with python-dotenv
        for key, val in values.items():
            os.environ.setdefault(key, val)
    else:
        load_dotenv(path)


def apply_env_config():
    global PMM_CONTAINER_NAME, PMM_URL, PMM_USER, PMM_PASS
    global CLICKHOUSE_USER, CLICKHOUSE_PASS
    global USE_AI, AI_API_KEY, OUTPUT_DATA_FILE
    global START_TIME, END_TIME, LAST_PERIOD, STEP

    PMM_CONTAINER_NAME = os.getenv("PMM_CONTAINER_NAME", PMM_CONTAINER_NAME)
    PMM_URL = os.getenv("PMM_URL", PMM_URL)
    PMM_USER = os.getenv("PMM_USER", PMM_USER)
    PMM_PASS = os.getenv("PMM_PASS", PMM_PASS)

    CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", CLICKHOUSE_USER)
    CLICKHOUSE_PASS = os.getenv("CLICKHOUSE_PASS", CLICKHOUSE_PASS)

    USE_AI = os.getenv("USE_AI", str(USE_AI)).lower() in ("true", "1", "yes")
    AI_API_KEY = os.getenv("AI_API_KEY", AI_API_KEY)

    OUTPUT_DATA_FILE = os.getenv("OUTPUT_DATA_FILE", OUTPUT_DATA_FILE)

    START_TIME = os.getenv("START_TIME", START_TIME)
    END_TIME = os.getenv("END_TIME", END_TIME)
    LAST_PERIOD = os.getenv("LAST_PERIOD", LAST_PERIOD)
    STEP = os.getenv("STEP", STEP)


def apply_env_py(env_py_path):
    global PMM_CONTAINER_NAME, PMM_URL, PMM_USER, PMM_PASS
    global CLICKHOUSE_USER, CLICKHOUSE_PASS
    global USE_AI, AI_API_KEY, OUTPUT_DATA_FILE
    global START_TIME, END_TIME, LAST_PERIOD, STEP

    try:
        spec = importlib.util.spec_from_file_location("custom_env", env_py_path)
        custom_env = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(custom_env)

        PMM_CONTAINER_NAME = getattr(custom_env, "PMM_CONTAINER_NAME", PMM_CONTAINER_NAME)
        PMM_URL = getattr(custom_env, "PMM_URL", PMM_URL)
        PMM_USER = getattr(custom_env, "PMM_USER", PMM_USER)
        PMM_PASS = getattr(custom_env, "PMM_PASS", PMM_PASS)

        CLICKHOUSE_USER = getattr(custom_env, "CLICKHOUSE_USER", CLICKHOUSE_USER)
        CLICKHOUSE_PASS = getattr(custom_env, "CLICKHOUSE_PASS", CLICKHOUSE_PASS)

        USE_AI = getattr(custom_env, "USE_AI", USE_AI)
        AI_API_KEY = getattr(custom_env, "AI_API_KEY", AI_API_KEY)

        OUTPUT_DATA_FILE = getattr(custom_env, "OUTPUT_DATA_FILE", OUTPUT_DATA_FILE)

        START_TIME = getattr(custom_env, "START_TIME", START_TIME)
        END_TIME = getattr(custom_env, "END_TIME", END_TIME)
        LAST_PERIOD = getattr(custom_env, "LAST_PERIOD", LAST_PERIOD)
        STEP = getattr(custom_env, "STEP", STEP)

        print("ℹ️  Configuration was supplemented from env.py", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Error loading env.py: {e}", file=sys.stderr)


def load_config(explicit_path):
    """Load configuration and return the path of the file used."""
    if not explicit_path:
        print(
            "ℹ️  --config was not set, looking for .env by default.",
            file=sys.stderr,
        )

    path = resolve_config_file(explicit_path)
    load_env_file(path)
    apply_env_config()

    env_py_path = os.path.join(SCRIPT_DIR, "env.py")
    if os.path.exists(env_py_path):
        if explicit_path:
            # Otherwise env.py would override the explicitly selected configuration
            print(
                f"ℹ️  env.py skipped because of --config {explicit_path}",
                file=sys.stderr,
            )
        else:
            apply_env_py(env_py_path)

    return path


def parse_config_arg(argv):
    """Extract --config before the main parser, because default values depend on it."""
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config")
    known_args, _ = pre_parser.parse_known_args(argv)
    return known_args.config

# ==========================================
# 3. TIME WINDOW AND QUERY SETTINGS
# ==========================================
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)

DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_datetime(value, option_name):
    value = str(value).strip()

    if value.lower() == "now":
        return datetime.now()

    if re.fullmatch(r"\d{9,}", value):
        return datetime.fromtimestamp(int(value))

    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    raise ValueError(
        f"Invalid date for {option_name}: '{value}'. "
        "Allowed formats: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', 'YYYY-MM-DD HH:MM:SS', "
        "UNIX timestamp or 'now'."
    )


def parse_duration(value, option_name):
    value = str(value).strip().lower()
    match = re.fullmatch(r"(\d+)\s*([mhdw])", value)
    if not match:
        raise ValueError(
            f"Invalid period for {option_name}: '{value}'. "
            "Allowed formats: 90m, 12h, 3d, 2w."
        )

    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise ValueError(f"The period for {option_name} must be greater than zero.")

    return timedelta(**{DURATION_UNITS[unit]: amount})


def resolve_time_window(start_value, end_value, last_value):
    """Turn CLI/env values into a concrete start and end of the window."""
    start = parse_datetime(start_value, "--start") if start_value else None
    end = parse_datetime(end_value, "--end") if end_value else None

    if start and end:
        pass
    elif start:
        end = datetime.now()
    else:
        end = end or datetime.now()
        start = end - parse_duration(last_value, "--last")

    if start >= end:
        raise ValueError(
            f"The start date ({start:%Y-%m-%d %H:%M}) must be before the end date "
            f"({end:%Y-%m-%d %H:%M})."
        )

    return start, end


def parse_step_seconds(value):
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", str(value).strip().lower())
    if not match:
        raise ValueError(
            f"Invalid step for --step: '{value}'. Allowed formats: 30s, 300s, 5m, 1h."
        )

    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    seconds = amount * multiplier
    if seconds <= 0:
        raise ValueError("The --step interval must be greater than zero.")

    return seconds


def format_period(start, end):
    hours = (end - start).total_seconds() / 3600
    length = f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.1f} hours"
    return f"{start:%Y-%m-%d %H:%M} - {end:%Y-%m-%d %H:%M} ({length})"


HELP_EPILOG = """
CHOOSING A TIME PERIOD
  The period is determined by the combination of --start, --end and --last:

    --start and --end      the exact period between the two dates
    --start only           from the start date until now
    --end only             a period of length --last ending at that date
    neither --start nor --end
                           the last --last (default 3d) until now

  Dates for --start and --end accept the following formats:

    2026-08-10             00:00 on that date
    '2026-08-10 14:30'     date and time (quotes are required because of the space)
    2026-08-10T14:30:00    ISO format, works without quotes
    1755000000             UNIX timestamp
    now                    the current moment

  Duration for --last: 90m, 12h, 3d, 2w (minutes, hours, days, weeks).

EXAMPLES
  python3 analyze.py
      last 3 days (the default)

  python3 analyze.py --last 12h
      last 12 hours

  python3 analyze.py --start '2026-08-10' --end '2026-08-12 18:00'
      exact period, for example while an incident lasted

  python3 analyze.py --start '2026-08-10 09:00'
      from that moment until now

  python3 analyze.py --end '2026-08-12 18:00' --last 6h
      6 hours before a given moment, to see what led up to it

  python3 analyze.py --last 2w --step 20m
      a long period with a coarser step (see the note below)

CONFIGURATION
  Without --config, .env from the current directory is used, and if it is missing
  there, .env next to the script. If neither is found, the script exits with an error.

  --config points at a specific file, which is handy when one host runs several PMM
  installations:

    python3 analyze.py --config ./.env-customer1
    python3 analyze.py --config /root/.env-customer2 --last 12h

  The file has the same format as .env.example. When --config is set, an env.py
  sitting next to the script is skipped so it cannot override the selected
  configuration.

NOTES
  Prometheus returns at most 11000 points per query, so a long period needs a
  larger step. The script checks this up front and suggests a --step value
  instead of letting the queries fail.

  Defaults can also be set in the configuration file via START_TIME, END_TIME,
  LAST_PERIOD, STEP and OUTPUT_DATA_FILE. Command-line arguments take priority.
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Fetch PMM telemetry for a chosen period, filter anomalies and prepare "
            "the data for AI analysis."
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="Configuration file (default: .env in the current directory or next to the script).",
    )
    parser.add_argument(
        "--start",
        metavar="DATE",
        default=START_TIME,
        help="Start of the period. See the formats below.",
    )
    parser.add_argument(
        "--end",
        metavar="DATE",
        default=END_TIME,
        help="End of the period (default: the current moment).",
    )
    parser.add_argument(
        "--last",
        metavar="PERIOD",
        default=LAST_PERIOD,
        help=f"Period back from the end when --start is not set (default: {LAST_PERIOD}).",
    )
    parser.add_argument(
        "--step",
        metavar="STEP",
        default=STEP,
        help=f"Prometheus sample step (default: {STEP}).",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=OUTPUT_DATA_FILE,
        help="File for the raw data (default: a name generated from the period).",
    )
    return parser.parse_args()


PROMETHEUS_QUERIES = {
    "cpu_usage_pct": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "load_1m": 'node_load1',
    "ram_free_gb": 'node_memory_MemAvailable_bytes / 1024 / 1024 / 1024',
    "swap_used_mb": '(node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes) / 1024 / 1024',
    "disk_read_mb_s": 'sum(rate(node_disk_read_bytes_total[5m])) / 1024 / 1024',
    "disk_write_mb_s": 'sum(rate(node_disk_written_bytes_total[5m])) / 1024 / 1024',
    "disk_free_gb": 'node_filesystem_free_bytes{mountpoint="/"}/ 1024 / 1024 / 1024',
    
    # Network throughput (MB/s)
    "net_rx_mb_s": 'sum(rate(node_network_receive_bytes_total{device!="lo"}[5m])) / 1024 / 1024',
    "net_tx_mb_s": 'sum(rate(node_network_transmit_bytes_total{device!="lo"}[5m])) / 1024 / 1024',
    
    # Network packets per second (PPS)
    "net_rx_pps": 'sum(rate(node_network_receive_packets_total{device!="lo"}[5m]))',
    "net_tx_pps": 'sum(rate(node_network_transmit_packets_total{device!="lo"}[5m]))',

    "mysql_slow_queries_rate": 'rate(mysql_global_status_slow_queries[5m])',
    "mysql_active_threads": 'mysql_global_status_threads_running',
    "mysql_connected_threads": 'mysql_global_status_threads_connected',
    "mysql_queries_rate": 'rate(mysql_global_status_queries[5m])'
}

# ==========================================
# 4. FETCHING METRICS FROM THE PROMETHEUS API
# ==========================================
def fetch_prometheus_metrics(start_time, end_time, step):
    print(f"⏳ Fetching Prometheus metrics via the PMM API (step {step})...")
    time_series_data = {}
    
    session = requests.Session()
    session.auth = (PMM_USER, PMM_PASS)
    session.verify = False

    for metric_name, query in PROMETHEUS_QUERIES.items():
        url = f"{PMM_URL.rstrip('/')}/prometheus/api/v1/query_range"
        params = {
            'query': query,
            'start': int(start_time.timestamp()),
            'end': int(end_time.timestamp()),
            'step': step
        }
        try:
            r = session.get(url, params=params, timeout=30)
            
            if r.status_code != 200:
                print(f"⚠️ HTTP {r.status_code} for {metric_name}.", file=sys.stderr)
                if r.status_code in (401, 403):
                    print("   👉 Authentication error! Check PMM_USER and PMM_PASS.", file=sys.stderr)
                    break
                continue

            res = r.json()
            if res.get('status') == 'success' and res.get('data', {}).get('result'):
                metrics_values = res['data']['result'][0].get('values', [])
                for ts, val in metrics_values:
                    time_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
                    if time_str not in time_series_data:
                        time_series_data[time_str] = {}
                    
                    try:
                        float_val = float(val)
                        if not math.isnan(float_val) and not math.isinf(float_val):
                            time_series_data[time_str][metric_name] = round(float_val, 2)
                        else:
                            time_series_data[time_str][metric_name] = None
                    except ValueError:
                        time_series_data[time_str][metric_name] = None
        except Exception as e:
            print(f"⚠️ Error fetching metric {metric_name}: {e}", file=sys.stderr)
            
    return time_series_data

# ==========================================
# 5. FETCHING SQL QUERIES VIA DOCKER EXEC
# ==========================================
def fetch_clickhouse_queries_via_docker(start_time, end_time):
    print("⏳ Fetching slow SQL queries from ClickHouse via `docker exec`...")
    
    clickhouse_sql = f"""
    SELECT 
        fingerprint AS query_signature,
        any(example) AS sample_sql,
        groupUniqArray(schema) AS schemas,
        groupUniqArray(database) AS databases,
        groupUniqArray(service_name) AS service_names,
        sum(m_query_time_cnt) AS total_executions,
        round(sum(m_query_time_sum) / sum(m_query_time_cnt), 4) AS avg_latency_sec,
        round(max(m_query_time_max), 2) AS max_latency_sec,
        round(sum(m_rows_examined_sum), 0) AS total_rows_examined
    FROM pmm.metrics
    WHERE period_start >= '{start_time.strftime('%Y-%m-%d %H:%M:%S')}'
      AND period_start <= '{end_time.strftime('%Y-%m-%d %H:%M:%S')}'
      AND example != ''
    GROUP BY fingerprint
    HAVING total_executions > 0
    ORDER BY avg_latency_sec DESC
    LIMIT 10
    FORMAT JSON
    """
    
    cmd = [
        "docker", "exec", "-i", PMM_CONTAINER_NAME,
        "sh", "-c",
        f"clickhouse-client --user='{CLICKHOUSE_USER}' --password='{CLICKHOUSE_PASS}' --database=pmm --query \"{clickhouse_sql}\""
    ]
    
    try:
        result = subprocess.run(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            check=True
        )
        stdout_text = result.stdout.decode('utf-8')
        data = json.loads(stdout_text)
        queries = attach_query_databases(data.get('data', []))
        print(f"✅ Successfully fetched {len(queries)} slow SQL queries.")
        return queries
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode('utf-8').strip() if e.stderr else str(e)
        print(f"❌ Error fetching ClickHouse queries via Docker: {err_msg}", file=sys.stderr)
    except Exception as e:
        print(f"❌ Error parsing ClickHouse JSON: {e}", file=sys.stderr)
        
    return []


def _nonempty_unique(values):
    seen = []
    for value in values or []:
        if value and value not in seen:
            seen.append(value)
    return seen


def attach_query_databases(queries):
    """Attach the database/schema each query was observed in.

    In PMM ClickHouse, `schema` is the MySQL database (and the PostgreSQL schema),
    while `database` is the PostgreSQL database/catalog. One fingerprint can
    appear in more than one database, so a list is kept.
    """
    for query in queries:
        schemas = _nonempty_unique(query.pop("schemas", None))
        pg_databases = _nonempty_unique(query.pop("databases", None))
        service_names = _nonempty_unique(query.get("service_names"))

        query["service_names"] = service_names
        # MySQL: schema holds the database name; PostgreSQL: database is the catalog.
        query["databases"] = pg_databases or schemas
        if schemas and pg_databases:
            query["schemas"] = schemas
        query["database"] = query["databases"][0] if query["databases"] else ""
    return queries

# ==========================================
# 6. FILTERING ANOMALIES AND REDUCING THE DATA
# ==========================================
def process_telemetry_for_ai(metrics_history):
    timeline = [{"t": ts, **metrics} for ts, metrics in sorted(metrics_history.items())]
    if not timeline:
        return {}, []

    summary_stats = {}
    metric_keys = list(PROMETHEUS_QUERIES.keys())

    for key in metric_keys:
        vals = [pt[key] for pt in timeline if pt.get(key) is not None]
        if vals:
            vals_sorted = sorted(vals)
            p95_idx = int(len(vals_sorted) * 0.95)
            mean_val = sum(vals) / len(vals)
            variance = sum((x - mean_val) ** 2 for x in vals) / len(vals)
            std_dev = math.sqrt(variance)

            summary_stats[key] = {
                "min": round(vals_sorted[0], 2),
                "avg": round(mean_val, 2),
                "max": round(vals_sorted[-1], 2),
                "p95": round(vals_sorted[p95_idx], 2),
                "std_dev": round(std_dev, 2)
            }

    anomaly_indices = set()
    for idx, pt in enumerate(timeline):
        is_anomaly = False

        cpu = pt.get("cpu_usage_pct", 0) or 0
        load = pt.get("load_1m", 0) or 0
        slow_q = pt.get("mysql_slow_queries_rate", 0) or 0
        swap = pt.get("swap_used_mb", 0) or 0
        ram_free = pt.get("ram_free_gb", 999) or 999
        
        net_rx_mb = pt.get("net_rx_mb_s", 0) or 0
        net_tx_mb = pt.get("net_tx_mb_s", 0) or 0
        net_rx_pps = pt.get("net_rx_pps", 0) or 0
        net_tx_pps = pt.get("net_tx_pps", 0) or 0

        if cpu > 80.0: is_anomaly = True
        if slow_q > 0.1: is_anomaly = True
        if swap > 500.0: is_anomaly = True
        if ram_free < 1.0: is_anomaly = True
        
        if net_rx_mb > 50.0 or net_tx_mb > 50.0: is_anomaly = True
        if net_rx_pps > 10000 or net_tx_pps > 10000: is_anomaly = True

        if summary_stats.get("load_1m"):
            avg_load = summary_stats["load_1m"]["avg"]
            sd_load = summary_stats["load_1m"]["std_dev"]
            if load > (avg_load + 2 * sd_load) and load > 2.0:
                is_anomaly = True

        if summary_stats.get("net_rx_mb_s"):
            avg_rx = summary_stats["net_rx_mb_s"]["avg"]
            sd_rx = summary_stats["net_rx_mb_s"]["std_dev"]
            if net_rx_mb > (avg_rx + 2.5 * sd_rx) and net_rx_mb > 10.0:
                is_anomaly = True

        if summary_stats.get("net_tx_mb_s"):
            avg_tx = summary_stats["net_tx_mb_s"]["avg"]
            sd_tx = summary_stats["net_tx_mb_s"]["std_dev"]
            if net_tx_mb > (avg_tx + 2.5 * sd_tx) and net_tx_mb > 10.0:
                is_anomaly = True

        if is_anomaly:
            for window_idx in range(max(0, idx - 3), min(len(timeline), idx + 4)):
                anomaly_indices.add(window_idx)

    filtered_timeline = [timeline[i] for i in sorted(anomaly_indices)]
    
    return summary_stats, filtered_timeline

# ==========================================
# 7. AI ROOT CAUSE ANALYSIS
# ==========================================
def analyze_with_ai(system_prompt, full_user_prompt_with_json):
    print("\n🧠 Sending the optimized payload to AI for Root Cause analysis...", file=sys.stderr)
    
    try:
        import openai
    except ImportError:
        print("❌ The `openai` library is not installed (`pip install openai`).", file=sys.stderr)
        return None

    try:
        client = openai.OpenAI(api_key=AI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_user_prompt_with_json}
            ],
            temperature=0.2
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"\n❌ Error connecting to the AI API: {e}", file=sys.stderr)
        return None

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    # --config is processed before parse_args() because configuration determines
    # the default values of the remaining arguments.
    help_requested = any(arg in ("-h", "--help") for arg in sys.argv[1:])
    try:
        config_file = load_config(parse_config_arg(sys.argv[1:]))
        print(f"ℹ️  Configuration: {config_file}", file=sys.stderr)
    except ConfigError as e:
        if not help_requested:
            print(e, file=sys.stderr)
            sys.exit(2)

    args = parse_args()

    try:
        start_time, end_time = resolve_time_window(args.start, args.end, args.last)
        step_seconds = parse_step_seconds(args.step)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        print("   👉 See `python3 analyze.py --help` for choosing a time period.", file=sys.stderr)
        sys.exit(2)

    # Prometheus rejects query_range with more than 11000 points per query
    points = (end_time - start_time).total_seconds() / step_seconds
    if points > 11000:
        suggested = math.ceil((end_time - start_time).total_seconds() / 11000 / 60) * 60
        print(
            f"⚠️ The period produces {int(points)} points at step {args.step}, and Prometheus allows "
            f"a maximum of 11000. Use --step {suggested}s or a shorter period.",
            file=sys.stderr,
        )
        sys.exit(2)

    period_label = format_period(start_time, end_time)
    output_file = args.output or (
        f"pmm_telemetry_{start_time:%Y%m%d-%H%M}_{end_time:%Y%m%d-%H%M}.json"
    )
    print(f"🗓️  Analyzed period: {period_label}")

    # 1. Collect data
    metrics_history = fetch_prometheus_metrics(start_time, end_time, args.step)
    top_queries = fetch_clickhouse_queries_via_docker(start_time, end_time)
    
    if not metrics_history:
        print("❌ No Prometheus metrics were found. Check PMM_URL and the password.", file=sys.stderr)
        sys.exit(1)
        
    # 2. Full payload to save to a file
    full_payload = {
        "period_start": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "period_end": end_time.strftime("%Y-%m-%d %H:%M:%S"),
        "granularity": args.step,
        "total_time_points": len(metrics_history),
        "system_metrics_timeline": [
            {"t": ts, **metrics} for ts, metrics in sorted(metrics_history.items())
        ],
        "top_problematic_sql_queries": top_queries
    }
    
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(full_payload, f, indent=2, ensure_ascii=False)
        print(f"💾 Full raw data saved to file: {output_file}")
    except Exception as e:
        print(f"⚠️ Error writing to file {output_file}: {e}", file=sys.stderr)

    # 3. Filter for AI
    summary_stats, anomaly_timeline = process_telemetry_for_ai(metrics_history)
    
    tokens_saved_pct = round((1 - (len(anomaly_timeline) / max(1, len(metrics_history)))) * 100, 1)
    print(f"✅ Successfully filtered data: {len(anomaly_timeline)} critical points kept out of {len(metrics_history)} ({tokens_saved_pct}% tokens saved).")

    ai_payload = {
        "analyzed_period": period_label,
        "period_summary": summary_stats,
        "anomalies_and_spikes_timeline": anomaly_timeline,
        "top_problematic_sql_queries": top_queries
    }

    # 4. Prepare the prompt
    system_prompt = f"""You are a principal Database Reliability Engineer (DBRE) and Linux Performance Expert.
Analyze the provided PMM telemetry data for the period {period_label}.
Note: The data contains an overall statistical summary for the whole period plus chronological slices ONLY for the recorded peaks and anomalies (including network traffic MB/s and packets/sec PPS), as well as the top slow SQL queries from ClickHouse.

Produce a detailed Root Cause Analysis:
1. IDENTIFY PATTERNS AND PEAKS: When are the main peaks in CPU, Load, Swap, Disk I/O, Network Throughput/PPS or Slow Queries in the anomaly timeline?
2. CHRONOLOGICAL CORRELATION: Which resource starts degrading FIRST and how does that affect the others (e.g. a spike in Network Packets/MBs -> overload of MySQL threads -> high CPU/RAM consumption)?
3. CORRELATION WITH SQL QUERIES: Which of the provided SQL queries coincide with these peaks and are likely causing high resource consumption (e.g. missing indexes, scanning many rows `total_rows_examined`, or transferring large volumes of data over the network). For each query, name the database (`database` / `databases`) and the instance (`service_names`).
4. ROOT CAUSE HYPOTHESIS: Describe the full problem chain (e.g. 'Network flood / Large SELECT query -> Disk Read saturation -> Network TX saturation -> Swap thrashing -> Locking of MySQL threads').
5. REMEDIATION RECOMMENDATIONS: Give concrete steps for:
   - SQL query optimization (indexes, rewriting).
   - System, network and MySQL settings (innodb_buffer_pool_size, swappiness, max_connections, txqueuelen, etc.)."""

    # Text shown on the console
    user_prompt_display = (
        f"Period: {period_label}\n"
        f"Size of data being sent: {len(anomaly_timeline)} points (out of {len(metrics_history)} total)."
    )

    # Full user prompt, including the JSON payload for the API
    full_user_prompt_with_json = f"""Please analyze the provided Percona Monitoring and Management (PMM) telemetry data for the period {period_label} and produce a Root Cause Analysis.

Here is the structured data for anomalies, overall statistics for the period, and the top problematic SQL queries:

```json
{json.dumps(ai_payload, indent=2, ensure_ascii=False)}
```"""

    print("\n" + "="*80)
    print("                      PROMPT FOR AI ANALYSIS (OPTIMIZED)                        ")
    print("="*80 + "\n")
    print(f"--- SYSTEM PROMPT ---\n{system_prompt}\n")
    print(f"--- USER PROMPT ---\n{user_prompt_display}")
    print("\n" + "="*80 + "\n")

    # 5. Send to AI
    if USE_AI:
        report = analyze_with_ai(system_prompt, full_user_prompt_with_json)
        if report:
            print("\n" + "="*50)
            print("         AI ROOT CAUSE ANALYSIS REPORT         ")
            print("="*50 + "\n")
            print(report)
    else:
        print("ℹ️  USE_AI = False. Automatic API submission skipped.", file=sys.stderr)
