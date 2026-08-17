#!/usr/bin/env python3
import subprocess
import requests
import json
import sys
import os
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

OUTPUT_DATA_FILE = "pmm_telemetry_last_3days.json"

# ==========================================
# 2. ЗАРЕЖДАНЕ НА КОНФИГУРАЦИЯ ОТ ФАЙЛОВЕ
# ==========================================
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_file = ".env"
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip("'\"")

PMM_CONTAINER_NAME = os.getenv("PMM_CONTAINER_NAME", PMM_CONTAINER_NAME)
PMM_URL = os.getenv("PMM_URL", PMM_URL)
PMM_USER = os.getenv("PMM_USER", PMM_USER)
PMM_PASS = os.getenv("PMM_PASS", PMM_PASS)

CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", CLICKHOUSE_USER)
CLICKHOUSE_PASS = os.getenv("CLICKHOUSE_PASS", CLICKHOUSE_PASS)

USE_AI = os.getenv("USE_AI", str(USE_AI)).lower() in ("true", "1", "yes")
AI_API_KEY = os.getenv("AI_API_KEY", AI_API_KEY)

OUTPUT_DATA_FILE = os.getenv("OUTPUT_DATA_FILE", OUTPUT_DATA_FILE)

env_py_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "env.py")
if os.path.exists(env_py_path):
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
        
        print("ℹ️  Конфигурацията е заредена от env.py", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Грешка при зареждане на env.py: {e}", file=sys.stderr)

# ==========================================
# 3. НАСТРОЙКИ НА ВРЕМЕВИ ПРОЗОРЕЦ И ЗАЯВКИ
# ==========================================
end_time = datetime.now()
start_time = end_time - timedelta(days=3)
STEP = "300s"  # 5 минути (300 секунди)

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
def fetch_prometheus_metrics():
    print("⏳ Извличане на Prometheus метрики през PMM API (5-мин интервали)...")
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
            'step': STEP
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
def fetch_clickhouse_queries_via_docker():
    print("⏳ Извличане на бавни SQL заявки от ClickHouse през `docker exec`...")
    
    clickhouse_sql = f"""
    SELECT 
        fingerprint AS query_signature,
        any(example) AS sample_sql,
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
        queries = data.get('data', [])
        print(f"✅ Успешно извлечени {len(queries)} бавни SQL заявки.")
        return queries
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode('utf-8').strip() if e.stderr else str(e)
        print(f"❌ Грешка при извличане на ClickHouse заявки през Docker: {err_msg}", file=sys.stderr)
    except Exception as e:
        print(f"❌ Грешка при парсване на ClickHouse JSON: {e}", file=sys.stderr)
        
    return []

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
    # 1. Събиране на данни
    metrics_history = fetch_prometheus_metrics()
    top_queries = fetch_clickhouse_queries_via_docker()
    
    if not metrics_history:
        print("❌ Не бяха намерени метрики от Prometheus. Проверете PMM_URL и паролата.", file=sys.stderr)
        sys.exit(1)
        
    # 2. Пълен payload за запазване във файл
    full_payload = {
        "granularity": "5 minutes",
        "total_time_points": len(metrics_history),
        "system_metrics_timeline": [
            {"t": ts, **metrics} for ts, metrics in sorted(metrics_history.items())
        ],
        "top_problematic_sql_queries": top_queries
    }
    
    try:
        with open(OUTPUT_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(full_payload, f, indent=2, ensure_ascii=False)
        print(f"💾 Пълните сурови данни са запазени във файл: {OUTPUT_DATA_FILE}")
    except Exception as e:
        print(f"⚠️ Грешка при запис във файл {OUTPUT_DATA_FILE}: {e}", file=sys.stderr)

    # 3. Филтриране за AI
    summary_stats, anomaly_timeline = process_telemetry_for_ai(metrics_history)
    
    tokens_saved_pct = round((1 - (len(anomaly_timeline) / max(1, len(metrics_history)))) * 100, 1)
    print(f"✅ Успешно филтрирани данни: от {len(metrics_history)} са оставени {len(anomaly_timeline)} критични точки ({tokens_saved_pct}% спестени токени).")

    ai_payload = {
        "period_summary_3days": summary_stats,
        "anomalies_and_spikes_timeline": anomaly_timeline,
        "top_problematic_sql_queries": top_queries
    }

    # 4. Подготовка на промпта
    system_prompt = """Ти си главен Database Reliability Engineer (DBRE) и Linux Performance Expert.
Анализирай предоставените PMM телеметрични данни за последните 3 дни.
Забележка: Данните съдържат Общо статистическо резюме за целия период + хронологични отрязъци САМО за регистрираните пикове и аномалии (включително мрежов трафик MB/s и пакети/сек PPS), както и топ бавните SQL заявки от ClickHouse.

Направи подробен Root Cause Analysis:
1. ИДЕНТИФИКАЦИЯ НА МОДЕЛИ И ПИКОВЕ: Кога са основните пикове в CPU, Load, Swap, Disk I/O, Network Throughput/PPS или Slow Queries в аномалната хронология?
2. ХРОНОЛОГИЧНА КОРЕЛАЦИЯ: Кой ресурс започва да деградира ПЪРВИ и как това влияе на останалите (напр. пик в Network Packets/MBs -> претоварване на MySQL нишки -> висока консумация на CPU/RAM)?
3. КОРЕЛАЦИЯ СЪС SQL ЗАЯВКИ: Кои от предоставените SQL заявки съвпадат с тези пикове и вероятно причиняват висока консумация на ресурси (напр. липса на индекси, сканиране на много редове `total_rows_examined` или прехвърляне на големи обем данни по мрежата).
4. ПЪРВОПРИЧИНА (Root Cause Hypothesis): Опиши пълната верига на проблема (напр. 'Network flood / Голяма SELECT заявка -> Disk Read saturation -> Network TX saturation -> Swap thrashing -> Locking на MySQL нишки').
5. ПРЕПОРЪКИ ЗА РЕШЕНИЕ: Дай конкретни стъпки за:
   - Оптимизация на SQL заявките (индекси, преписване).
   - Системни, Мрежови и MySQL настройки (innodb_buffer_pool_size, swappiness, max_connections, txqueuelen и др.)."""

    # Декларираме текстовата част за изход на конзолата
    user_prompt_display = f"Размер на изпращаните данни: {len(anomaly_timeline)} точки (от общо {len(metrics_history)})."

    # Декларираме пълния user_prompt, който съдържа и JSON payload-а за подаване към API-то
    full_user_prompt_with_json = f"""Моля, анализирай предоставените телеметрични данни от Percona Monitoring and Management (PMM) за последните 3 дни и направи Root Cause Analysis.

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
