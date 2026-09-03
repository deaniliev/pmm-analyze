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

# Игнориране на предупрежденията за самоподписан SSL сертификат (InsecureRequestWarning)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 1. ДЕФОЛТНИ СТОЙНОСТИ
# ==========================================
PMM_CONTAINER_NAME = "pmm-server"
PMM_URL = "https://localhost:8443"
PMM_USER = "admin"
PMM_PASS = "your_pmm_password"

CLICKHOUSE_USER = "default"
CLICKHOUSE_PASS = "clickhouse"

USE_AI = False
AI_API_KEY = "your_api_key_here"

# Празно означава "генерирай име на файл според избрания период"
OUTPUT_DATA_FILE = ""

# Времеви прозорец: START/END са конкретни дати, LAST е относителен период (напр. 3d, 12h, 90m)
START_TIME = ""
END_TIME = ""
LAST_PERIOD = "3d"
STEP = "300s"  # 5 минути (300 секунди)

# ==========================================
# 2. ЗАРЕЖДАНЕ НА КОНФИГУРАЦИЯ ОТ ФАЙЛОВЕ
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class ConfigError(Exception):
    """Липсващ или нечетим конфигурационен файл."""


def default_config_candidates():
    """Местата, където се търси .env, когато не е зададен --config."""
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
                f"⚠️ Зададеният конфигурационен файл не е намерен: {explicit_path}\n"
                f"   Търсено в: {path}"
            )
        return path

    candidates = default_config_candidates()
    for path in candidates:
        if os.path.isfile(path):
            return path

    tried = "\n".join(f"   - {path}" for path in candidates)
    raise ConfigError(
        "❌ Не е намерен .env файл. Проверени места:\n"
        f"{tried}\n"
        "   Създайте .env по образец на .env.example или посочете друг файл с --config."
    )


def load_env_file(path):
    try:
        from dotenv import load_dotenv
    except ImportError:
        # Резервен парсер, когато python-dotenv не е инсталиран
        try:
            values = {}
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        values[key.strip()] = val.strip().strip("'\"")
        except OSError as e:
            raise ConfigError(f"❌ Конфигурационният файл не може да бъде прочетен: {e}")

        # setdefault: реалната среда има приоритет над файла, както при python-dotenv
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

        print("ℹ️  Конфигурацията е допълнена от env.py", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Грешка при зареждане на env.py: {e}", file=sys.stderr)


def load_config(explicit_path):
    """Зарежда конфигурацията и връща пътя на използвания файл."""
    if not explicit_path:
        print(
            "ℹ️  Не е зададен --config, търси се .env по подразбиране.",
            file=sys.stderr,
        )

    path = resolve_config_file(explicit_path)
    load_env_file(path)
    apply_env_config()

    env_py_path = os.path.join(SCRIPT_DIR, "env.py")
    if os.path.exists(env_py_path):
        if explicit_path:
            # Иначе env.py би подменил изрично избраната конфигурация
            print(
                f"ℹ️  env.py е пропуснат заради --config {explicit_path}",
                file=sys.stderr,
            )
        else:
            apply_env_py(env_py_path)

    return path


def parse_config_arg(argv):
    """Изважда --config преди основния парсер, защото от него зависят стойностите по подразбиране."""
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config")
    known_args, _ = pre_parser.parse_known_args(argv)
    return known_args.config

# ==========================================
# 3. НАСТРОЙКИ НА ВРЕМЕВИ ПРОЗОРЕЦ И ЗАЯВКИ
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
        f"Невалидна дата за {option_name}: '{value}'. "
        "Позволени формати: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM', 'YYYY-MM-DD HH:MM:SS', "
        "UNIX timestamp или 'now'."
    )


def parse_duration(value, option_name):
    value = str(value).strip().lower()
    match = re.fullmatch(r"(\d+)\s*([mhdw])", value)
    if not match:
        raise ValueError(
            f"Невалиден период за {option_name}: '{value}'. "
            "Позволени формати: 90m, 12h, 3d, 2w."
        )

    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise ValueError(f"Периодът за {option_name} трябва да е по-голям от нула.")

    return timedelta(**{DURATION_UNITS[unit]: amount})


def resolve_time_window(start_value, end_value, last_value):
    """CLI/env стойностите се превръщат в конкретни начало и край на прозореца."""
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
            f"Началната дата ({start:%Y-%m-%d %H:%M}) трябва да е преди крайната "
            f"({end:%Y-%m-%d %H:%M})."
        )

    return start, end


def parse_step_seconds(value):
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", str(value).strip().lower())
    if not match:
        raise ValueError(
            f"Невалидна стъпка за --step: '{value}'. Позволени формати: 30s, 300s, 5m, 1h."
        )

    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    seconds = amount * multiplier
    if seconds <= 0:
        raise ValueError("Стъпката за --step трябва да е по-голяма от нула.")

    return seconds


def format_period(start, end):
    hours = (end - start).total_seconds() / 3600
    length = f"{hours / 24:.1f} дни" if hours >= 48 else f"{hours:.1f} часа"
    return f"{start:%Y-%m-%d %H:%M} - {end:%Y-%m-%d %H:%M} ({length})"


HELP_EPILOG = """
ИЗБОР НА ВРЕМЕВИ ПЕРИОД
  Периодът се определя от комбинацията на --start, --end и --last:

    --start и --end        точният период между двете дати
    само --start           от началната дата до текущия момент
    само --end             период с дължина --last, завършващ на тази дата
    без --start и --end    последните --last (по подразбиране 3d) до сега

  Дати за --start и --end се приемат в следните формати:

    2026-08-10             00:00 ч. на тази дата
    '2026-08-10 14:30'     дата и час (кавичките са нужни заради интервала)
    2026-08-10T14:30:00    ISO формат, работи и без кавички
    1755000000             UNIX timestamp
    now                    текущият момент

  Продължителност за --last: 90m, 12h, 3d, 2w (минути, часове, дни, седмици).

ПРИМЕРИ
  python3 analyze.py
      последните 3 дни (поведението по подразбиране)

  python3 analyze.py --last 12h
      последните 12 часа

  python3 analyze.py --start '2026-08-10' --end '2026-08-12 18:00'
      точен период, например докато е продължавал инцидент

  python3 analyze.py --start '2026-08-10 09:00'
      от този момент до сега

  python3 analyze.py --end '2026-08-12 18:00' --last 6h
      6 часа преди даден момент, за да се види какво е довело до него

  python3 analyze.py --last 2w --step 20m
      дълъг период с по-груба стъпка (виж бележката по-долу)

КОНФИГУРАЦИЯ
  Без --config се използва .env от текущата директория, а ако липсва там - .env
  до самия скрипт. Ако не бъде намерен нито един, скриптът спира с грешка.

  С --config се посочва конкретен файл, което е удобно при няколко PMM
  инсталации на един хост:

    python3 analyze.py --config ./.env-customer1
    python3 analyze.py --config /root/.env-customer2 --last 12h

  Файлът има формата на .env.example. Когато е зададен --config, евентуален
  env.py до скрипта се пропуска, за да не подмени избраната конфигурация.

ЗАБЕЛЕЖКИ
  Prometheus връща максимум 11000 точки на заявка, затова дълъг период изисква
  по-голяма стъпка. Скриптът проверява това предварително и предлага стойност
  за --step, вместо да оставя заявките да се провалят.

  Стойностите по подразбиране може да се зададат и в конфигурационния файл чрез
  START_TIME, END_TIME, LAST_PERIOD, STEP и OUTPUT_DATA_FILE. Аргументите от
  командния ред имат приоритет.
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Извлича PMM телеметрия за избран период, филтрира аномалиите и подготвя "
            "данните за AI анализ."
        ),
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="ФАЙЛ",
        help="Конфигурационен файл (по подразбиране: .env в текущата директория или до скрипта).",
    )
    parser.add_argument(
        "--start",
        metavar="ДАТА",
        default=START_TIME,
        help="Начало на периода. Виж форматите по-долу.",
    )
    parser.add_argument(
        "--end",
        metavar="ДАТА",
        default=END_TIME,
        help="Край на периода (по подразбиране: текущият момент).",
    )
    parser.add_argument(
        "--last",
        metavar="ПЕРИОД",
        default=LAST_PERIOD,
        help=f"Период назад от края, когато не е зададено --start (по подразбиране: {LAST_PERIOD}).",
    )
    parser.add_argument(
        "--step",
        metavar="СТЪПКА",
        default=STEP,
        help=f"Стъпка на извадката за Prometheus (по подразбиране: {STEP}).",
    )
    parser.add_argument(
        "--output",
        metavar="ФАЙЛ",
        default=OUTPUT_DATA_FILE,
        help="Файл за суровите данни (по подразбиране: име, генерирано от периода).",
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
    
    # Мрежова пропускателна способност (Network Throughput - MB/s)
    "net_rx_mb_s": 'sum(rate(node_network_receive_bytes_total{device!="lo"}[5m])) / 1024 / 1024',
    "net_tx_mb_s": 'sum(rate(node_network_transmit_bytes_total{device!="lo"}[5m])) / 1024 / 1024',
    
    # Пакети в секунда (Network Packets Per Second - PPS)
    "net_rx_pps": 'sum(rate(node_network_receive_packets_total{device!="lo"}[5m]))',
    "net_tx_pps": 'sum(rate(node_network_transmit_packets_total{device!="lo"}[5m]))',

    "mysql_slow_queries_rate": 'rate(mysql_global_status_slow_queries[5m])',
    "mysql_active_threads": 'mysql_global_status_threads_running',
    "mysql_connected_threads": 'mysql_global_status_threads_connected',
    "mysql_queries_rate": 'rate(mysql_global_status_queries[5m])'
}

# ==========================================
# 4. ИЗВЛИЧАНЕ НА МЕТРИКИ ОТ PROMETHEUS API
# ==========================================
def fetch_prometheus_metrics(start_time, end_time, step):
    print(f"⏳ Извличане на Prometheus метрики през PMM API (стъпка {step})...")
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
                print(f"⚠️ HTTP {r.status_code} при {metric_name}.", file=sys.stderr)
                if r.status_code in (401, 403):
                    print("   👉 Грешка с автентикацията! Проверете PMM_USER и PMM_PASS.", file=sys.stderr)
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
            print(f"⚠️ Грешка при метрика {metric_name}: {e}", file=sys.stderr)
            
    return time_series_data

# ==========================================
# 5. ИЗВЛИЧАНЕ НА SQL ЗАЯВКИ ЧРЕЗ DOCKER EXEC
# ==========================================
def fetch_clickhouse_queries_via_docker(start_time, end_time):
    print("⏳ Извличане на бавни SQL заявки от ClickHouse през `docker exec`...")
    
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
        print(f"✅ Успешно извлечени {len(queries)} бавни SQL заявки.")
        return queries
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode('utf-8').strip() if e.stderr else str(e)
        print(f"❌ Грешка при извличане на ClickHouse заявки през Docker: {err_msg}", file=sys.stderr)
    except Exception as e:
        print(f"❌ Грешка при парсване на ClickHouse JSON: {e}", file=sys.stderr)
        
    return []


def _nonempty_unique(values):
    seen = []
    for value in values or []:
        if value and value not in seen:
            seen.append(value)
    return seen


def attach_query_databases(queries):
    """Добавя към всяка заявка базата/схемата, в която е наблюдавана.

    В PMM ClickHouse `schema` е MySQL базата (и PostgreSQL схемата), а
    `database` е PostgreSQL базата/каталогът. Един fingerprint може да се
    среща в повече от една база, затова се пази списък.
    """
    for query in queries:
        schemas = _nonempty_unique(query.pop("schemas", None))
        pg_databases = _nonempty_unique(query.pop("databases", None))
        service_names = _nonempty_unique(query.get("service_names"))

        query["service_names"] = service_names
        # MySQL: schema държи името на базата; PostgreSQL: database е каталогът.
        query["databases"] = pg_databases or schemas
        if schemas and pg_databases:
            query["schemas"] = schemas
        query["database"] = query["databases"][0] if query["databases"] else ""
    return queries

# ==========================================
# 6. ФИЛТРИРАНЕ НА АНОМАЛИИ И СМАЛЯВАНЕ НА ДАННИТЕ
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
# 7. AI ROOT CAUSE АНАЛИЗ
# ==========================================
def analyze_with_ai(system_prompt, full_user_prompt_with_json):
    print("\n🧠 Изпращане на оптимизирания пакет към AI за Root Cause анализ...", file=sys.stderr)
    
    try:
        import openai
    except ImportError:
        print("❌ Не е инсталирана `openai` библиотеката (`pip install openai`).", file=sys.stderr)
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
        print(f"\n❌ Грешка при връзка с AI API: {e}", file=sys.stderr)
        return None

# ==========================================
# ОСНОВНО ИЗПЪЛНЕНИЕ
# ==========================================
if __name__ == "__main__":
    # --config се обработва преди parse_args(), защото конфигурацията определя
    # стойностите по подразбиране на останалите аргументи.
    help_requested = any(arg in ("-h", "--help") for arg in sys.argv[1:])
    try:
        config_file = load_config(parse_config_arg(sys.argv[1:]))
        print(f"ℹ️  Конфигурация: {config_file}", file=sys.stderr)
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
        print("   👉 Виж `python3 analyze.py --help` за избор на времеви период.", file=sys.stderr)
        sys.exit(2)

    # Prometheus отказва query_range с повече от 11000 точки на заявка
    points = (end_time - start_time).total_seconds() / step_seconds
    if points > 11000:
        suggested = math.ceil((end_time - start_time).total_seconds() / 11000 / 60) * 60
        print(
            f"⚠️ Периодът дава {int(points)} точки при стъпка {args.step}, а Prometheus позволява "
            f"максимум 11000. Използвайте --step {suggested}s или по-къс период.",
            file=sys.stderr,
        )
        sys.exit(2)

    period_label = format_period(start_time, end_time)
    output_file = args.output or (
        f"pmm_telemetry_{start_time:%Y%m%d-%H%M}_{end_time:%Y%m%d-%H%M}.json"
    )
    print(f"🗓️  Анализиран период: {period_label}")

    # 1. Събиране на данни
    metrics_history = fetch_prometheus_metrics(start_time, end_time, args.step)
    top_queries = fetch_clickhouse_queries_via_docker(start_time, end_time)
    
    if not metrics_history:
        print("❌ Не бяха намерени метрики от Prometheus. Проверете PMM_URL и паролата.", file=sys.stderr)
        sys.exit(1)
        
    # 2. Пълен payload за запазване във файл
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
        print(f"💾 Пълните сурови данни са запазени във файл: {output_file}")
    except Exception as e:
        print(f"⚠️ Грешка при запис във файл {output_file}: {e}", file=sys.stderr)

    # 3. Филтриране за AI
    summary_stats, anomaly_timeline = process_telemetry_for_ai(metrics_history)
    
    tokens_saved_pct = round((1 - (len(anomaly_timeline) / max(1, len(metrics_history)))) * 100, 1)
    print(f"✅ Успешно филтрирани данни: от {len(metrics_history)} са оставени {len(anomaly_timeline)} критични точки ({tokens_saved_pct}% спестени токени).")

    ai_payload = {
        "analyzed_period": period_label,
        "period_summary": summary_stats,
        "anomalies_and_spikes_timeline": anomaly_timeline,
        "top_problematic_sql_queries": top_queries
    }

    # 4. Подготовка на промпта
    system_prompt = f"""Ти си главен Database Reliability Engineer (DBRE) и Linux Performance Expert.
Анализирай предоставените PMM телеметрични данни за периода {period_label}.
Забележка: Данните съдържат Общо статистическо резюме за целия период + хронологични отрязъци САМО за регистрираните пикове и аномалии (включително мрежов трафик MB/s и пакети/сек PPS), както и топ бавните SQL заявки от ClickHouse.

Направи подробен Root Cause Analysis:
1. ИДЕНТИФИКАЦИЯ НА МОДЕЛИ И ПИКОВЕ: Кога са основните пикове в CPU, Load, Swap, Disk I/O, Network Throughput/PPS или Slow Queries в аномалната хронология?
2. ХРОНОЛОГИЧНА КОРЕЛАЦИЯ: Кой ресурс започва да деградира ПЪРВИ и как това влияе на останалите (напр. пик в Network Packets/MBs -> претоварване на MySQL нишки -> висока консумация на CPU/RAM)?
3. КОРЕЛАЦИЯ СЪС SQL ЗАЯВКИ: Кои от предоставените SQL заявки съвпадат с тези пикове и вероятно причиняват висока консумация на ресурси (напр. липса на индекси, сканиране на много редове `total_rows_examined` или прехвърляне на големи обем данни по мрежата). За всяка заявка посочи базата данни (`database` / `databases`) и инстанцията (`service_names`).
4. ПЪРВОПРИЧИНА (Root Cause Hypothesis): Опиши пълната верига на проблема (напр. 'Network flood / Голяма SELECT заявка -> Disk Read saturation -> Network TX saturation -> Swap thrashing -> Locking на MySQL нишки').
5. ПРЕПОРЪКИ ЗА РЕШЕНИЕ: Дай конкретни стъпки за:
   - Оптимизация на SQL заявките (индекси, преписване).
   - Системни, Мрежови и MySQL настройки (innodb_buffer_pool_size, swappiness, max_connections, txqueuelen и др.)."""

    # Декларираме текстовата част за изход на конзолата
    user_prompt_display = (
        f"Период: {period_label}\n"
        f"Размер на изпращаните данни: {len(anomaly_timeline)} точки (от общо {len(metrics_history)})."
    )

    # Декларираме пълния user_prompt, който съдържа и JSON payload-а за подаване към API-то
    full_user_prompt_with_json = f"""Моля, анализирай предоставените телеметрични данни от Percona Monitoring and Management (PMM) за периода {period_label} и направи Root Cause Analysis.

Ето структурираните данни за аномалии, обща статистика за периода и топ проблематични SQL заявки:

```json
{json.dumps(ai_payload, indent=2, ensure_ascii=False)}
```"""

    print("\n" + "="*80)
    print("                      ПРОМПТ ЗА AI АНАЛИЗ (ОПТИМИЗИРАН)                         ")
    print("="*80 + "\n")
    print(f"--- SYSTEM PROMPT ---\n{system_prompt}\n")
    print(f"--- USER PROMPT ---\n{user_prompt_display}")
    print("\n" + "="*80 + "\n")

    # 5. Изпращане към AI
    if USE_AI:
        report = analyze_with_ai(system_prompt, full_user_prompt_with_json)
        if report:
            print("\n" + "="*50)
            print("         AI ROOT CAUSE ANALYSIS REPORT         ")
            print("="*50 + "\n")
            print(report)
    else:
        print("ℹ️  Флагът USE_AI = False. Пропуснато е автоматичното изпращане към API.", file=sys.stderr)
