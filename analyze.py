#!/usr/bin/env python3
import argparse
import requests
import json
import sys
import os
import re
import math
import importlib.util
from datetime import datetime, timedelta, timezone

# Ignore warnings about a self-signed SSL certificate (InsecureRequestWarning)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 1. DEFAULT VALUES
# ==========================================
PMM_URL = "https://localhost:8443"
PMM_USER = "admin"
PMM_PASS = "your_pmm_password"

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
    global PMM_URL, PMM_USER, PMM_PASS
    global USE_AI, AI_API_KEY, OUTPUT_DATA_FILE
    global START_TIME, END_TIME, LAST_PERIOD, STEP

    PMM_URL = os.getenv("PMM_URL", PMM_URL)
    PMM_USER = os.getenv("PMM_USER", PMM_USER)
    PMM_PASS = os.getenv("PMM_PASS", PMM_PASS)

    USE_AI = os.getenv("USE_AI", str(USE_AI)).lower() in ("true", "1", "yes")
    AI_API_KEY = os.getenv("AI_API_KEY", AI_API_KEY)

    OUTPUT_DATA_FILE = os.getenv("OUTPUT_DATA_FILE", OUTPUT_DATA_FILE)

    START_TIME = os.getenv("START_TIME", START_TIME)
    END_TIME = os.getenv("END_TIME", END_TIME)
    LAST_PERIOD = os.getenv("LAST_PERIOD", LAST_PERIOD)
    STEP = os.getenv("STEP", STEP)


def apply_env_py(env_py_path):
    global PMM_URL, PMM_USER, PMM_PASS
    global USE_AI, AI_API_KEY, OUTPUT_DATA_FILE
    global START_TIME, END_TIME, LAST_PERIOD, STEP

    try:
        spec = importlib.util.spec_from_file_location("custom_env", env_py_path)
        custom_env = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(custom_env)

        PMM_URL = getattr(custom_env, "PMM_URL", PMM_URL)
        PMM_USER = getattr(custom_env, "PMM_USER", PMM_USER)
        PMM_PASS = getattr(custom_env, "PMM_PASS", PMM_PASS)

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

    # Lowest free space % across all real (non-tmpfs/overlay) mounts, e.g. a separate MySQL data volume
    "disk_free_min_pct": (
        'min(node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} '
        '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay"} * 100)'
    ),

    # CPU time spent waiting on I/O and stolen by the hypervisor (noisy-neighbor signal on VMs)
    "cpu_iowait_pct": 'avg(rate(node_cpu_seconds_total{mode="iowait"}[5m])) * 100',
    "cpu_steal_pct": 'avg(rate(node_cpu_seconds_total{mode="steal"}[5m])) * 100',

    # Average time a disk spends busy servicing requests, and per-request latency
    "disk_io_util_pct": 'avg(rate(node_disk_io_time_seconds_total[5m])) * 100',
    "disk_read_latency_ms": (
        'sum(rate(node_disk_read_time_seconds_total[5m])) '
        '/ sum(rate(node_disk_reads_completed_total[5m])) * 1000'
    ),
    "disk_write_latency_ms": (
        'sum(rate(node_disk_write_time_seconds_total[5m])) '
        '/ sum(rate(node_disk_writes_completed_total[5m])) * 1000'
    ),

    # Network throughput (MB/s)
    "net_rx_mb_s": 'sum(rate(node_network_receive_bytes_total{device!="lo"}[5m])) / 1024 / 1024',
    "net_tx_mb_s": 'sum(rate(node_network_transmit_bytes_total{device!="lo"}[5m])) / 1024 / 1024',

    # Network packets per second (PPS)
    "net_rx_pps": 'sum(rate(node_network_receive_packets_total{device!="lo"}[5m]))',
    "net_tx_pps": 'sum(rate(node_network_transmit_packets_total{device!="lo"}[5m]))',

    # Network errors/drops and TCP retransmits (real network trouble, not just "busy")
    "net_rx_errors_rate": 'sum(rate(node_network_receive_errs_total{device!="lo"}[5m]))',
    "net_tx_errors_rate": 'sum(rate(node_network_transmit_errs_total{device!="lo"}[5m]))',
    "net_rx_drop_rate": 'sum(rate(node_network_receive_drop_total{device!="lo"}[5m]))',
    "net_tx_drop_rate": 'sum(rate(node_network_transmit_drop_total{device!="lo"}[5m]))',
    "tcp_retrans_rate": 'rate(node_netstat_Tcp_RetransSegs[5m])',

    "mysql_slow_queries_rate": 'rate(mysql_global_status_slow_queries[5m])',
    "mysql_active_threads": 'mysql_global_status_threads_running',
    "mysql_connected_threads": 'mysql_global_status_threads_connected',
    "mysql_queries_rate": 'rate(mysql_global_status_queries[5m])',

    # Connection pressure and thread-cache efficiency
    # `ignoring(job)`: status metrics are scraped at PMM's high-resolution interval, variables
    # (like max_connections) at low-resolution, so they land under different `job` labels and
    # would otherwise fail to vector-match.
    "mysql_connections_used_pct": (
        'mysql_global_status_max_used_connections '
        '/ ignoring(job) mysql_global_variables_max_connections * 100'
    ),
    "mysql_aborted_connects_rate": 'rate(mysql_global_status_aborted_connects[5m])',
    "mysql_threads_created_rate": 'rate(mysql_global_status_threads_created[5m])',

    # Queries spilling to disk-based temp tables usually mean missing indexes / small sort/join buffers
    "mysql_tmp_disk_tables_pct": (
        'rate(mysql_global_status_created_tmp_disk_tables[5m]) '
        '/ rate(mysql_global_status_created_tmp_tables[5m]) * 100'
    ),
    "mysql_deadlocks_rate": 'rate(mysql_global_status_innodb_deadlocks[5m])',
    "mysql_table_locks_waited_rate": 'rate(mysql_global_status_table_locks_waited[5m])',

    # InnoDB internals: buffer pool efficiency, row locking, redo log and dirty-page pressure
    "innodb_buffer_pool_hit_ratio": (
        '(1 - (rate(mysql_global_status_innodb_buffer_pool_reads[5m]) '
        '/ rate(mysql_global_status_innodb_buffer_pool_read_requests[5m]))) * 100'
    ),
    "innodb_row_lock_waits_rate": 'rate(mysql_global_status_innodb_row_lock_waits[5m])',
    "innodb_row_lock_time_avg_ms": 'mysql_global_status_innodb_row_lock_time_avg',
    "innodb_log_writes_rate": 'rate(mysql_global_status_innodb_log_writes[5m])',
    "innodb_buffer_pool_dirty_pages_pct": (
        'mysql_global_status_innodb_buffer_pool_bytes_dirty '
        '/ ignoring(job) mysql_global_variables_innodb_buffer_pool_size * 100'
    ),

    # process-exporter (https://github.com/ncabatoff/process-exporter), scraped as a PMM external service
    "process_count": 'sum(namedprocess_namegroup_num_procs)',
    "process_rss_gb": 'sum(namedprocess_namegroup_memory_bytes{memtype="resident"}) / 1024 / 1024 / 1024',
    "process_swap_mb": 'sum(namedprocess_namegroup_memory_bytes{memtype="swapped"}) / 1024 / 1024',
}

# ==========================================
# 4. PMM HTTP SESSION AND VERSION DETECTION
# ==========================================
PMM_VERSION_ENDPOINTS = (
    "/v1/server/version",  # PMM 3
    "/v1/version",         # PMM 2 (and some PMM 3 installs)
)

QAN_API = {
    2: {
        "report": "/v0/qan/GetReport",
        "example": "/v0/qan/ObjectDetails/GetQueryExample",
        "labels": "/v0/qan/ObjectDetails/GetLabels",
    },
    3: {
        "report": "/v1/qan/metrics:getReport",
        "example": "/v1/qan/query:getExample",
        "labels": "/v1/qan:getLabels",
    },
}

QAN_QUERY_LIMIT = 10
PROCESS_GROUP_LIMIT = 10

PMM_REQUEST_TIMEOUT = 30


class PmmApiError(Exception):
    """PMM HTTP API is unreachable, unauthorized, or returned an unexpected version."""


def pmm_session():
    session = requests.Session()
    session.auth = (PMM_USER, PMM_PASS)
    session.verify = False
    session.headers["Accept"] = "application/json"
    return session


def pmm_url(path):
    return f"{PMM_URL.rstrip('/')}{path}"


def to_rfc3339_utc(dt):
    """QAN timestamps are RFC3339. Naive values are treated as local time."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_string(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_pmm_version_string(payload):
    if not isinstance(payload, dict):
        return ""
    version = _first_string(payload.get("version"))
    if version:
        return version
    server = payload.get("server")
    if isinstance(server, dict):
        return _first_string(server.get("version"))
    return ""


def parse_pmm_major(version_string):
    match = re.match(r"v?(\d+)", version_string.strip())
    if not match:
        return None
    major = int(match.group(1))
    if major in QAN_API:
        return major
    return None


def detect_pmm_version(session):
    """Return (major, version_string) for PMM 2 or 3."""
    print("⏳ Detecting PMM version...")
    last_error = None
    auth_error = None

    for path in PMM_VERSION_ENDPOINTS:
        try:
            response = session.get(pmm_url(path), timeout=PMM_REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last_error = f"{path}: {e}"
            continue

        if response.status_code in (401, 403):
            auth_error = (
                f"Authentication error from {path} (HTTP {response.status_code}). "
                "Check PMM_USER and PMM_PASS."
            )
            last_error = f"{path}: HTTP {response.status_code}"
            continue

        if response.status_code != 200:
            last_error = f"{path}: HTTP {response.status_code}"
            continue

        try:
            payload = response.json()
        except ValueError:
            last_error = f"{path}: response is not JSON"
            continue

        version_string = extract_pmm_version_string(payload)
        major = parse_pmm_major(version_string)
        if major is None:
            last_error = f"{path}: unrecognized version {version_string!r}"
            continue

        print(f"✅ PMM {major} ({version_string})")
        return major, version_string

    if auth_error:
        raise PmmApiError(auth_error)

    detail = f" Last error: {last_error}" if last_error else ""
    raise PmmApiError(
        "Could not detect PMM 2 or PMM 3 from the version API "
        f"({', '.join(PMM_VERSION_ENDPOINTS)}).{detail}"
    )


# ==========================================
# 5. FETCHING METRICS FROM THE PROMETHEUS API
# ==========================================
def fetch_prometheus_metrics(session, start_time, end_time, step):
    print(f"⏳ Fetching Prometheus metrics via the PMM API (step {step})...")
    time_series_data = {}

    for metric_name, query in PROMETHEUS_QUERIES.items():
        url = pmm_url("/prometheus/api/v1/query_range")
        params = {
            "query": query,
            "start": int(start_time.timestamp()),
            "end": int(end_time.timestamp()),
            "step": step,
        }
        try:
            r = session.get(url, params=params, timeout=PMM_REQUEST_TIMEOUT)

            if r.status_code != 200:
                print(f"⚠️ HTTP {r.status_code} for {metric_name}.", file=sys.stderr)
                if r.status_code in (401, 403):
                    print("   👉 Authentication error! Check PMM_USER and PMM_PASS.", file=sys.stderr)
                    break
                continue

            res = r.json()
            if res.get("status") == "success" and res.get("data", {}).get("result"):
                metrics_values = res["data"]["result"][0].get("values", [])
                for ts, val in metrics_values:
                    time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
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


def _parse_prom_float(value):
    try:
        float_val = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(float_val) or math.isinf(float_val):
        return None
    return float_val


def prometheus_instant_query(session, query, when):
    """Run a Prometheus instant query at `when`. Returns a list of {metric, value}."""
    url = pmm_url("/prometheus/api/v1/query")
    params = {
        "query": query,
        "time": int(when.timestamp()),
    }
    try:
        response = session.get(url, params=params, timeout=PMM_REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"⚠️ Process-exporter query failed: {e}", file=sys.stderr)
        return []

    if response.status_code in (401, 403):
        print("⚠️ Authentication error while fetching process-exporter metrics.", file=sys.stderr)
        return []
    if response.status_code != 200:
        print(
            f"⚠️ HTTP {response.status_code} for process-exporter query.",
            file=sys.stderr,
        )
        return []

    try:
        payload = response.json()
    except ValueError as e:
        print(f"⚠️ Invalid JSON for process-exporter query: {e}", file=sys.stderr)
        return []

    if payload.get("status") != "success":
        return []

    series = []
    for item in payload.get("data", {}).get("result") or []:
        metric = item.get("metric") or {}
        raw = (item.get("value") or [None, None])[1]
        value = _parse_prom_float(raw)
        if value is None:
            continue
        series.append({"metric": metric, "value": value})
    return series


def _process_group_identity(metric):
    groupname = _first_string(metric.get("groupname"), metric.get("group"))
    node_name = _first_string(metric.get("node_name"), metric.get("nodeName"))
    service_name = _first_string(metric.get("service_name"), metric.get("serviceName"))
    return groupname, node_name, service_name


def fetch_top_process_groups(session, start_time, end_time):
    """Top process-exporter groups by peak RSS and process count over the window."""
    print("⏳ Fetching process-exporter groups via the PMM API...")
    duration_s = max(int((end_time - start_time).total_seconds()), 60)
    duration = f"{duration_s}s"
    limit = PROCESS_GROUP_LIMIT

    count_query = (
        f"topk({limit}, max_over_time(namedprocess_namegroup_num_procs[{duration}]))"
    )
    rss_query = (
        f"topk({limit}, max_over_time("
        f'namedprocess_namegroup_memory_bytes{{memtype="resident"}}[{duration}]))'
    )

    groups = {}
    for series in prometheus_instant_query(session, count_query, end_time):
        groupname, node_name, service_name = _process_group_identity(series["metric"])
        if not groupname:
            continue
        entry = groups.setdefault(
            (groupname, node_name, service_name),
            {"groupname": groupname, "node_name": node_name, "service_name": service_name},
        )
        entry["process_count"] = round(series["value"], 2)

    for series in prometheus_instant_query(session, rss_query, end_time):
        groupname, node_name, service_name = _process_group_identity(series["metric"])
        if not groupname:
            continue
        entry = groups.setdefault(
            (groupname, node_name, service_name),
            {"groupname": groupname, "node_name": node_name, "service_name": service_name},
        )
        entry["rss_gb"] = round(series["value"] / 1024 / 1024 / 1024, 2)

    ranked = []
    for entry in groups.values():
        item = {"groupname": entry["groupname"]}
        if entry.get("node_name"):
            item["node_name"] = entry["node_name"]
        if entry.get("service_name"):
            item["service_name"] = entry["service_name"]
        if "process_count" in entry:
            item["process_count"] = entry["process_count"]
        if "rss_gb" in entry:
            item["rss_gb"] = entry["rss_gb"]
        ranked.append(item)

    ranked.sort(
        key=lambda item: (item.get("rss_gb") or 0, item.get("process_count") or 0),
        reverse=True,
    )
    ranked = ranked[:limit]

    if ranked:
        print(f"✅ Successfully fetched {len(ranked)} process-exporter groups.")
    else:
        print(
            "⚠️ No process-exporter metrics found (namedprocess_namegroup_*). "
            "Confirm the external service is being scraped by PMM.",
            file=sys.stderr,
        )
    return ranked


# ==========================================
# 6. FETCHING SQL QUERIES VIA THE QAN HTTP API
# ==========================================
def _qan_post(session, path, payload):
    response = session.post(
        pmm_url(path),
        json=payload,
        timeout=PMM_REQUEST_TIMEOUT,
        headers={"Content-Type": "application/json"},
    )
    if response.status_code in (401, 403):
        raise PmmApiError(
            f"Authentication error from {path} (HTTP {response.status_code}). "
            "Check PMM_USER and PMM_PASS."
        )
    if response.status_code != 200:
        snippet = (response.text or "").strip().replace("\n", " ")[:300]
        raise PmmApiError(f"{path} returned HTTP {response.status_code}: {snippet}")
    try:
        return response.json()
    except ValueError as e:
        raise PmmApiError(f"{path} returned invalid JSON: {e}") from e


def _metric_stats(row, name):
    metrics = row.get("metrics") or {}
    cell = metrics.get(name) or {}
    if not isinstance(cell, dict):
        return {}
    stats = cell.get("stats")
    return stats if isinstance(stats, dict) else cell


def _metric_number(stats, *keys):
    for key in keys:
        value = stats.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _round_or_none(value, digits):
    if value is None:
        return None
    return round(value, digits)


def _is_totals_row(row):
    dimension = _first_string(row.get("dimension"), row.get("Dimension"))
    fingerprint = _first_string(row.get("fingerprint"), row.get("Fingerprint"))
    if not dimension:
        return True
    if fingerprint.upper() == "TOTAL":
        return True
    if dimension.upper() == "TOTAL":
        return True
    return False


def _label_values(labels_payload, *names):
    if not isinstance(labels_payload, dict):
        return []
    wanted = {name.lower() for name in names}
    collected = []
    for key, raw in labels_payload.items():
        if str(key).lower() not in wanted:
            continue
        values = []
        if isinstance(raw, dict):
            values = raw.get("values") or raw.get("value") or []
        elif isinstance(raw, list):
            values = raw
        elif isinstance(raw, str):
            values = [raw]
        collected.extend(values)
    return _nonempty_unique(collected)


def _example_text(example):
    if not isinstance(example, dict):
        return ""
    return _first_string(
        example.get("example"),
        example.get("Example"),
        example.get("explain_fingerprint"),
        example.get("explainFingerprint"),
    )


def fetch_qan_example(session, path, period, queryid):
    payload = {
        **period,
        "group_by": "queryid",
        "filter_by": queryid,
        "limit": 1,
    }
    data = _qan_post(session, path, payload)
    examples = data.get("query_examples") or data.get("queryExamples") or []
    if not examples:
        return {}, ""
    example = examples[0] if isinstance(examples[0], dict) else {}
    return example, _example_text(example)


def fetch_qan_labels(session, path, period, queryid):
    payload = {
        **period,
        "group_by": "queryid",
        "filter_by": queryid,
    }
    data = _qan_post(session, path, payload)
    return data.get("labels") or {}


def qan_row_to_query(session, paths, period, row):
    queryid = _first_string(row.get("dimension"), row.get("Dimension"))
    fingerprint = _first_string(row.get("fingerprint"), row.get("Fingerprint"))
    database = _first_string(row.get("database"), row.get("Database"))

    query_time = _metric_stats(row, "query_time")
    rows_examined = _metric_stats(row, "rows_examined")
    num_queries_stats = _metric_stats(row, "num_queries")

    total_executions = _metric_number(num_queries_stats, "sum", "cnt")
    if total_executions is None:
        for key in ("num_queries", "numQueries"):
            try:
                if row.get(key) is not None:
                    total_executions = float(row[key])
                    break
            except (TypeError, ValueError):
                continue

    example = {}
    sample_sql = ""
    labels = {}
    try:
        example, sample_sql = fetch_qan_example(session, paths["example"], period, queryid)
    except PmmApiError as e:
        print(f"⚠️ Could not fetch a query example for {queryid}: {e}", file=sys.stderr)
    try:
        labels = fetch_qan_labels(session, paths["labels"], period, queryid)
    except PmmApiError as e:
        print(f"⚠️ Could not fetch QAN labels for {queryid}: {e}", file=sys.stderr)

    schemas = _nonempty_unique(
        [
            example.get("schema"),
            example.get("Schema"),
        ]
        + _label_values(labels, "schema")
    )
    databases = _nonempty_unique(
        [
            database,
            example.get("database"),
            example.get("Database"),
        ]
        + _label_values(labels, "database")
        + schemas
    )
    service_names = _nonempty_unique(
        [
            example.get("service_name"),
            example.get("serviceName"),
        ]
        + _label_values(labels, "service_name", "serviceName")
    )

    query = {
        "queryid": queryid,
        "query_signature": fingerprint,
        "sample_sql": sample_sql or fingerprint,
        "service_names": service_names,
        "databases": databases,
        "total_executions": int(total_executions) if total_executions is not None else 0,
        "avg_latency_sec": _round_or_none(_metric_number(query_time, "avg"), 4),
        "max_latency_sec": _round_or_none(_metric_number(query_time, "max"), 2),
        "total_rows_examined": _round_or_none(_metric_number(rows_examined, "sum"), 0),
        "database": databases[0] if databases else "",
    }
    if schemas and set(schemas) != set(databases):
        query["schemas"] = schemas
    return query


def fetch_qan_queries(session, pmm_major, start_time, end_time):
    paths = QAN_API[pmm_major]
    period = {
        "period_start_from": to_rfc3339_utc(start_time),
        "period_start_to": to_rfc3339_utc(end_time),
    }
    print(f"⏳ Fetching slow SQL queries via the PMM {pmm_major} QAN API...")

    report_payload = {
        **period,
        "group_by": "queryid",
        "order_by": "-query_time",
        "main_metric": "query_time",
        "limit": QAN_QUERY_LIMIT,
        "offset": 0,
        "columns": ["query_time", "rows_examined", "num_queries"],
    }

    try:
        report = _qan_post(session, paths["report"], report_payload)
    except PmmApiError as e:
        print(f"❌ Error fetching QAN report: {e}", file=sys.stderr)
        return []

    rows = report.get("rows") or report.get("Rows") or []
    queries = []
    for row in rows:
        if not isinstance(row, dict) or _is_totals_row(row):
            continue
        try:
            queries.append(qan_row_to_query(session, paths, period, row))
        except Exception as e:
            queryid = _first_string(row.get("dimension"), row.get("Dimension")) or "?"
            print(f"⚠️ Skipping QAN row {queryid}: {e}", file=sys.stderr)
        if len(queries) >= QAN_QUERY_LIMIT:
            break

    print(f"✅ Successfully fetched {len(queries)} slow SQL queries.")
    return queries


def _nonempty_unique(values):
    seen = []
    for value in values or []:
        if value and value not in seen:
            seen.append(value)
    return seen

# ==========================================
# 7. FILTERING ANOMALIES AND REDUCING THE DATA
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
        process_count = pt.get("process_count", 0) or 0
        process_rss = pt.get("process_rss_gb", 0) or 0

        cpu_iowait = pt.get("cpu_iowait_pct", 0) or 0
        cpu_steal = pt.get("cpu_steal_pct", 0) or 0
        disk_read_latency = pt.get("disk_read_latency_ms", 0) or 0
        disk_write_latency = pt.get("disk_write_latency_ms", 0) or 0
        disk_free_min_pct = pt.get("disk_free_min_pct", 100) or 100

        net_rx_errors = pt.get("net_rx_errors_rate", 0) or 0
        net_tx_errors = pt.get("net_tx_errors_rate", 0) or 0
        net_rx_drop = pt.get("net_rx_drop_rate", 0) or 0
        net_tx_drop = pt.get("net_tx_drop_rate", 0) or 0

        mysql_conn_used_pct = pt.get("mysql_connections_used_pct", 0) or 0
        mysql_aborted_connects = pt.get("mysql_aborted_connects_rate", 0) or 0
        mysql_tmp_disk_pct = pt.get("mysql_tmp_disk_tables_pct", 0) or 0
        mysql_deadlocks = pt.get("mysql_deadlocks_rate", 0) or 0
        innodb_hit_ratio = pt.get("innodb_buffer_pool_hit_ratio", 100) or 100

        if cpu > 80.0: is_anomaly = True
        if slow_q > 0.1: is_anomaly = True
        if swap > 500.0: is_anomaly = True
        if ram_free < 1.0: is_anomaly = True

        if net_rx_mb > 50.0 or net_tx_mb > 50.0: is_anomaly = True
        if net_rx_pps > 10000 or net_tx_pps > 10000: is_anomaly = True

        if cpu_iowait > 20.0: is_anomaly = True
        if cpu_steal > 10.0: is_anomaly = True
        if disk_read_latency > 20.0 or disk_write_latency > 20.0: is_anomaly = True
        if disk_free_min_pct < 10.0: is_anomaly = True

        if net_rx_errors > 0 or net_tx_errors > 0: is_anomaly = True
        if net_rx_drop > 0 or net_tx_drop > 0: is_anomaly = True

        if mysql_conn_used_pct > 80.0: is_anomaly = True
        if mysql_aborted_connects > 0.1: is_anomaly = True
        if mysql_tmp_disk_pct > 25.0: is_anomaly = True
        if mysql_deadlocks > 0: is_anomaly = True
        if innodb_hit_ratio < 95.0: is_anomaly = True

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

        if summary_stats.get("process_count"):
            avg_procs = summary_stats["process_count"]["avg"]
            sd_procs = summary_stats["process_count"]["std_dev"]
            if process_count > (avg_procs + 2 * sd_procs) and process_count > 20:
                is_anomaly = True

        if summary_stats.get("process_rss_gb"):
            avg_rss = summary_stats["process_rss_gb"]["avg"]
            sd_rss = summary_stats["process_rss_gb"]["std_dev"]
            if process_rss > (avg_rss + 2 * sd_rss) and process_rss > 1.0:
                is_anomaly = True

        tcp_retrans = pt.get("tcp_retrans_rate", 0) or 0
        if summary_stats.get("tcp_retrans_rate"):
            avg_retrans = summary_stats["tcp_retrans_rate"]["avg"]
            sd_retrans = summary_stats["tcp_retrans_rate"]["std_dev"]
            if tcp_retrans > (avg_retrans + 2.5 * sd_retrans) and tcp_retrans > 1.0:
                is_anomaly = True

        row_lock_waits = pt.get("innodb_row_lock_waits_rate", 0) or 0
        if summary_stats.get("innodb_row_lock_waits_rate"):
            avg_lock = summary_stats["innodb_row_lock_waits_rate"]["avg"]
            sd_lock = summary_stats["innodb_row_lock_waits_rate"]["std_dev"]
            if row_lock_waits > (avg_lock + 2 * sd_lock) and row_lock_waits > 0.1:
                is_anomaly = True

        if is_anomaly:
            for window_idx in range(max(0, idx - 3), min(len(timeline), idx + 4)):
                anomaly_indices.add(window_idx)

    filtered_timeline = [timeline[i] for i in sorted(anomaly_indices)]
    
    return summary_stats, filtered_timeline

# ==========================================
# 8. AI ROOT CAUSE ANALYSIS
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

    session = pmm_session()
    try:
        pmm_major, _pmm_version = detect_pmm_version(session)
    except PmmApiError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    # 1. Collect data
    metrics_history = fetch_prometheus_metrics(session, start_time, end_time, args.step)
    top_queries = fetch_qan_queries(session, pmm_major, start_time, end_time)
    top_process_groups = fetch_top_process_groups(session, start_time, end_time)

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
        "top_problematic_sql_queries": top_queries,
        "top_process_groups": top_process_groups,
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
        "top_problematic_sql_queries": top_queries,
        "top_process_groups": top_process_groups,
    }

    # 4. Prepare the prompt
    system_prompt = f"""You are a principal Database Reliability Engineer (DBRE) and Linux Performance Expert.
Analyze the provided PMM telemetry data from the given date and time range.
Note: The data contains an overall statistical summary for the whole period plus chronological slices ONLY for the recorded peaks and anomalies, the top process groups by memory/count, and the top slow SQL queries from Query Analytics (QAN). Metrics cover:
  - System: CPU usage/load, CPU iowait % and steal % (VM noisy-neighbor signal), RAM/swap.
  - Disk: read/write throughput, %util (`disk_io_util_pct`), read/write latency in ms, and lowest free-space % across all mounts (`disk_free_min_pct`).
  - Network: throughput MB/s, packets/sec, plus rx/tx errors, drops, and TCP retransmit rate (real network faults, not just load).
  - MySQL/InnoDB: slow queries, thread/connection counts, connection-used %, aborted connects, thread-cache misses (`mysql_threads_created_rate`), temp-tables-to-disk % (`mysql_tmp_disk_tables_pct`), table lock waits, deadlocks, InnoDB buffer pool hit ratio, row lock waits/avg wait time, log writes, and dirty page %.
  - process-exporter: process count and RSS/swap.

Produce a detailed Root Cause Analysis:
1. IDENTIFY PATTERNS AND PEAKS: When are the main peaks/dips across CPU, Load, iowait/steal, Swap, Disk I/O throughput+latency, Network throughput/PPS/errors, InnoDB (lock waits, buffer pool hit ratio, deadlocks), connection pressure, or Slow Queries in the anomaly timeline?
2. CHRONOLOGICAL CORRELATION: Which resource starts degrading FIRST and how does that affect the others (e.g. a spike in Network Packets/MBs -> overload of MySQL threads -> connections saturate -> row lock waits pile up -> high CPU/RAM; or disk latency rises -> buffer pool hit ratio drops -> slow queries -> lock waits)?
3. CORRELATION WITH SQL QUERIES AND PROCESSES: Which of the provided SQL queries or process groups (`top_process_groups`) coincide with these peaks and are likely causing high resource consumption (e.g. missing indexes, scanning many rows `total_rows_examined`, temp tables spilling to disk, a process group with high `rss_gb` or `process_count`)? For each query, name the database (`database` / `databases`) and the instance (`service_names`). For each process group, name `groupname` and `node_name`.
4. ROOT CAUSE HYPOTHESIS: Describe the full problem chain (e.g. 'Network flood / Large SELECT query -> Disk read latency spike -> InnoDB buffer pool hit ratio drops -> row lock waits -> connection pool exhaustion -> Swap thrashing').
5. REMEDIATION RECOMMENDATIONS: Give concrete steps for:
   - SQL query optimization (indexes, rewriting, avoiding disk temp tables).
   - System, network and MySQL settings (innodb_buffer_pool_size, innodb_log_file_size, swappiness, max_connections, thread_cache_size, txqueuelen, etc.)."""

    # Text shown on the console
    user_prompt_display = (
        f"Period: {period_label}\n"
        f"Size of data being sent: {len(anomaly_timeline)} points (out of {len(metrics_history)} total)."
    )

    # Full user prompt, including the JSON payload for the API
    full_user_prompt_with_json = f"""Please analyze the provided Percona Monitoring and Management (PMM) telemetry data for the period {period_label} and produce a Root Cause Analysis.

Here is the structured data for anomalies, overall statistics for the period, top process groups, and the top problematic SQL queries:

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
