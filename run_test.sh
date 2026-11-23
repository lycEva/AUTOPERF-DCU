#!/bin/bash
# 一键压测：可选改服务配置 -> 可选预热 -> 正式测量，边压边直接采集容器资源并出图。
#
# 用法:
#   bash run_test.sh <jmx路径> <结果目录> <容器名> <线程数> <持续秒数> \
#     [--service 服务名] [--port N] [--ports-config 路径] \
#     [--warmup N] [--monitor-containers a,b] \
#     [--monitor-interval 0.3] [--monitor-timeout 2] \
#     [--monitor-policy tolerant|strict] \
#     [--monitor-max-consecutive-failures 5] [--monitor-max-failure-percent 1] \
#     [--server-config 路径 [--workers N] [--server-threads N] \
#      [--target-path 容器内绝对路径] [--config-wait 90]]
#
# 流程：改 server_config.json（可选，改的是运行目录副本）-> 重启容器并等就绪
#       -> 生成独立 JMX 副本（--warmup 时做测量窗口手术）-> 直接监控预检
#       -> JMeter 施压（预热期不采样、请求不进 JTL/聚合/HTML 报告）
#       -> 正式窗口采样 -> 停止监控并生成四张 PNG/SVG 曲线。
set -euo pipefail
BASE_DIR=$(cd "$(dirname "$0")" && pwd)
# 固定用系统 Python：conda 环境的 libffi 与系统 libgobject 版本符号冲突，
# 会让 plot_metrics 加载 librsvg/Cairo 失败、整轮被判失败（退出码 2）。
PYTHON_BIN=${PYTHON_BIN:-/usr/bin/python3}
if [ ! -x "$PYTHON_BIN" ]; then
  echo "❌ 找不到可执行的 Python: $PYTHON_BIN（可用 PYTHON_BIN 环境变量指定）" >&2
  exit 1
fi
if [ "$#" -ge 1 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
  if [ -x "$BASE_DIR/bin/dcu-bench" ]; then
    exec bash "$BASE_DIR/bin/dcu-bench" help
  fi
  echo "用法: bash run_test.sh <jmx路径> <结果路径> <容器名> <线程数> <持续秒数> [--service 服务名] [--port N] [--warmup N] [--monitor-containers a,b] [--monitor-interval 0.3] [--monitor-timeout 2] [--server-config 路径 --workers N --server-threads N]" >&2
  exit 0
fi
if [ "$#" -lt 5 ]; then
  echo "用法: bash run_test.sh <jmx路径> <结果路径> <容器名> <线程数> <持续秒数> [--service 服务名] [--port N] [--warmup N] [--monitor-containers a,b] [--monitor-interval 0.3] [--monitor-timeout 2] [--server-config 路径 --workers N --server-threads N]" >&2
  echo "提示: 也可以在任何目录用 dcu-bench <jmx> <结果目录> <容器> <线程数> <秒数> --service <服务名>" >&2
  exit 1
fi
JMX_FILE=$1
RESULT_DIR=$2
CONTAINER=$3
THREADS=$4
DURATION=$5
shift 5

JMETER=${JMETER:-jmeter}
MONITOR_MODE=${MONITOR_MODE:-direct}
MONITOR_INTERVAL=${MONITOR_INTERVAL:-0.3}
MONITOR_TIMEOUT=${MONITOR_TIMEOUT:-2}
MONITOR_CONTAINERS=${MONITOR_CONTAINERS:-}
MONITOR_POLICY=${MONITOR_POLICY:-tolerant}
MONITOR_MAX_CONSECUTIVE_FAILURES=${MONITOR_MAX_CONSECUTIVE_FAILURES:-5}
MONITOR_MAX_FAILURE_PERCENT=${MONITOR_MAX_FAILURE_PERCENT:-1}
WARMUP=${WARMUP:-0}
PORT=${PORT:-}
SERVICE=${SERVICE:-}
PORTS_CONFIG=${PORTS_CONFIG:-}
SERVER_CONFIG=${SERVER_CONFIG:-}
WORKERS=${WORKERS:-}
SERVER_THREADS=${SERVER_THREADS:-}
TARGET_PATH=${TARGET_PATH:-}
CONFIG_WAIT=${CONFIG_WAIT:-90}

while [ "$#" -gt 0 ]; do
  if [ "$#" -lt 2 ]; then echo "❌ 选项缺少参数: $1" >&2; exit 1; fi
  case "$1" in
    --monitor-mode) MONITOR_MODE=$2 ;;
    --monitor-interval) MONITOR_INTERVAL=$2 ;;
    --monitor-timeout) MONITOR_TIMEOUT=$2 ;;
    --monitor-containers) MONITOR_CONTAINERS=$2 ;;
    --monitor-policy) MONITOR_POLICY=$2 ;;
    --monitor-max-consecutive-failures) MONITOR_MAX_CONSECUTIVE_FAILURES=$2 ;;
    --monitor-max-failure-percent) MONITOR_MAX_FAILURE_PERCENT=$2 ;;
    --warmup) WARMUP=$2 ;;
    --port) PORT=$2 ;;
    --service) SERVICE=$2 ;;
    --ports-config) PORTS_CONFIG=$2 ;;
    --server-config) SERVER_CONFIG=$2 ;;
    --workers) WORKERS=$2 ;;
    --server-threads) SERVER_THREADS=$2 ;;
    --target-path) TARGET_PATH=$2 ;;
    --config-wait) CONFIG_WAIT=$2 ;;
    *) echo "❌ 未知选项: $1" >&2; exit 1 ;;
  esac
  shift 2
done

# 默认只监控压测目标容器；A 调用 B 的场景用 --monitor-containers a,b 一起看。
[ -n "$MONITOR_CONTAINERS" ] || MONITOR_CONTAINERS=$CONTAINER
[ -n "$PORTS_CONFIG" ] || PORTS_CONFIG="$BASE_DIR/ports.json"

if [[ "$MONITOR_MODE" != direct ]]; then
  echo "❌ 已移除 PerfMon 监控模式（--monitor-mode ${MONITOR_MODE}）；现在只有 direct 直接采集，请删除 JMX 中的 PerfMon 组件" >&2
  exit 1
fi
if [[ ! "$WARMUP" =~ ^(0|[1-9][0-9]*)$ ]]; then
  echo "❌ 预热秒数必须为非负整数（不含前导零），当前为: ${WARMUP}" >&2
  exit 1
fi
if [[ "$MONITOR_POLICY" != tolerant && "$MONITOR_POLICY" != strict ]]; then
  echo "❌ monitor-policy 必须为 tolerant 或 strict" >&2
  exit 1
fi
if [[ ! "$MONITOR_MAX_CONSECUTIVE_FAILURES" =~ ^[1-9][0-9]*$ ]]; then
  echo "❌ monitor-max-consecutive-failures 必须为正整数" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; v=float(sys.argv[1]); sys.exit(0 if 0 <= v <= 100 else 1)' \
  "$MONITOR_MAX_FAILURE_PERCENT" 2>/dev/null; then
  echo "❌ monitor-max-failure-percent 必须是 0~100 之间的数" >&2
  exit 1
fi
if [ -n "$PORT" ]; then
  if [[ ! "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "❌ 端口必须是 1-65535 之间的整数，当前为: ${PORT}" >&2
    exit 1
  fi
fi
if [ -z "$SERVICE" ]; then
  echo "❌ --service 必传（服务名，见 ports.json）" >&2
  exit 1
fi
if [[ ! "$SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  echo "❌ 服务名格式错误: ${SERVICE}" >&2
  exit 1
fi
"$PYTHON_BIN" -c 'import math,sys; values=[float(x) for x in sys.argv[1:]]; sys.exit(0 if all(math.isfinite(x) and x>0 for x in values) else 1)' \
  "$MONITOR_INTERVAL" "$MONITOR_TIMEOUT" || { echo "❌ 采样间隔和超时必须为有限正数（秒）" >&2; exit 1; }
if [[ ! "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  echo "❌ 容器名格式错误" >&2; exit 1
fi
for name in ${MONITOR_CONTAINERS//,/ }; do
  if [[ ! "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
    echo "❌ 监控容器名格式错误: $name" >&2; exit 1
  fi
done
if [[ ! "$THREADS" =~ ^[1-9][0-9]*$ || ! "$DURATION" =~ ^[1-9][0-9]*$ ]]; then
  echo "❌ 线程数和持续秒数必须为正整数" >&2; exit 1
fi
if [ ! -f "$JMX_FILE" ]; then echo "❌ JMX文件不存在: $JMX_FILE" >&2; exit 1; fi
if ! command -v "$JMETER" >/dev/null 2>&1; then echo "❌ 找不到JMeter: $JMETER" >&2; exit 1; fi

# --- 可选：压测前修改服务 server_config.json（workers/threads）并重启容器 ---
if [ -n "$SERVER_CONFIG" ]; then
  if [ ! -f "$SERVER_CONFIG" ]; then
    echo "❌ server_config.json 不存在: $SERVER_CONFIG" >&2; exit 1
  fi
  if [[ ! "$WORKERS" =~ ^[0-9]+$ || ! "$SERVER_THREADS" =~ ^[0-9]+$ ]]; then
    echo "❌ 使用 --server-config 时必须同时给 --workers 和 --server-threads（非负整数）" >&2; exit 1
  fi
  if [ -n "$TARGET_PATH" ] && [[ "$TARGET_PATH" != /* ]]; then
    echo "❌ --target-path 必须是容器内绝对路径: $TARGET_PATH" >&2; exit 1
  fi
elif [ -n "$WORKERS" ] || [ -n "$SERVER_THREADS" ]; then
  echo "❌ --workers / --server-threads 需要和 --server-config 一起使用" >&2; exit 1
fi
if [[ ! "$CONFIG_WAIT" =~ ^[0-9]+$ ]]; then
  echo "❌ --config-wait 必须是非负整数秒" >&2; exit 1
fi

# 相对路径统一按调用方当前目录解析：从任意目录（或经 dcu-bench 软链）调用时，
# ./results 落在你敲命令的地方，而不是工具安装目录。
if [[ "$RESULT_DIR" != /* ]]; then RESULT_DIR="$PWD/$RESULT_DIR"; fi
mkdir -p "$RESULT_DIR"
RESULT_DIR=$(cd "$RESULT_DIR" && pwd)
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RUN_DIR=$(mktemp -d "$RESULT_DIR/run_${CONTAINER}_${TIMESTAMP}_XXXXXX")
chmod 755 "$RUN_DIR"
RESULT_PATH="$RUN_DIR/result.jtl"
JMETER_LOG="$RUN_DIR/jmeter.log"
REPORT_DIR="$RUN_DIR/report"
TOTAL_DURATION=$(( WARMUP + DURATION ))

printf '📄 原始模板: %s\n📦 压测容器: %s\n📈 监控容器: %s\n🏷 服务名: %s\n🔌 端口: %s\n📁 本次运行: %s\n👥 线程数: %s\n⏱️ 正式测量: %s秒\n🔥 预热: %s秒（总施压 %s秒）\n📊 监控策略: %s\n' \
  "$JMX_FILE" "$CONTAINER" "$MONITOR_CONTAINERS" "$SERVICE" "${PORT:-（按 ports.json）}" "$RUN_DIR" "$THREADS" "$DURATION" "$WARMUP" "$TOTAL_DURATION" "$MONITOR_POLICY"

# 端口：--port 优先，否则查 ports.json；仅用于重启后等待服务就绪。
port_for_wait() {
  "$PYTHON_BIN" - "$PORTS_CONFIG" "$SERVICE" "$PORT" <<'PY'
import json, sys
path, service, port = sys.argv[1], sys.argv[2], sys.argv[3]
if port:
    print(int(port)); raise SystemExit(0)
try:
    with open(path, encoding='utf-8') as handle:
        data = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(0)
if isinstance(data.get('services'), dict):
    data = data['services']
value = data.get(service)
if isinstance(value, int) and 1 <= value <= 65535:
    print(value)
PY
}
tcp_open() {
  "$PYTHON_BIN" - "$1" <<'PY'
import socket, sys
sock = socket.socket(); sock.settimeout(1)
try:
    sock.connect(('127.0.0.1', int(sys.argv[1])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}
wait_container_ready() {
  local wait_s=$1 port=$2 deadline status
  if [ "$wait_s" -eq 0 ]; then
    echo "ℹ️ --config-wait 0：跳过容器就绪探测" >&2
    return 0
  fi
  deadline=$((SECONDS + wait_s))
  echo "⏳ 等待容器 $CONTAINER 就绪（最多 ${wait_s}s，端口探测 ${port:-跳过}）..."
  while :; do
    status=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)
    if [ "$status" = "true" ] && { [ -z "$port" ] || tcp_open "$port"; }; then
      echo "✅ 容器已就绪"
      return 0
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "❌ 等待容器就绪超时（${wait_s}s）：running=${status}，端口 ${port:-未配置} 未就绪" >&2
      echo "   服务不在本机 127.0.0.1:${port:-?} 时可加 --config-wait 0 跳过探测" >&2
      return 1
    fi
    sleep 1
  done
}

if [ -n "$SERVER_CONFIG" ]; then
  CONFIG_COPY="$RUN_DIR/server_config.json"
  cp "$SERVER_CONFIG" "$CONFIG_COPY"
  echo "🛠 更新服务配置: workers=$WORKERS, threads=$SERVER_THREADS（副本 $CONFIG_COPY）"
  CONFIG_ARGS=("$CONFIG_COPY" "$CONTAINER" "$SERVICE" "$WORKERS" "$SERVER_THREADS")
  [ -n "$TARGET_PATH" ] && CONFIG_ARGS+=(--target-path "$TARGET_PATH")
  bash "$BASE_DIR/update_server_config.sh" "${CONFIG_ARGS[@]}"
  wait_container_ready "$CONFIG_WAIT" "$(port_for_wait)"
fi

PREPARE_ARGS=(--template "$JMX_FILE" --run-dir "$RUN_DIR" --container "$CONTAINER"
  --threads "$THREADS" --duration "$DURATION" --warmup "$WARMUP" --monitor-mode direct)
if [ -n "$PORT" ]; then PREPARE_ARGS+=(--port "$PORT"); fi
PREPARE_ARGS+=(--service "$SERVICE" --ports-config "$PORTS_CONFIG")
"$PYTHON_BIN" "$BASE_DIR/prepare_run.py" "${PREPARE_ARGS[@]}"

JMETER_ARGS=("$JMETER" -n -t "$RUN_DIR/plan.jmx" -l "$RESULT_PATH" -j "$JMETER_LOG"
  -e -o "$REPORT_DIR" -Jthreads="$THREADS" -Jduration="$TOTAL_DURATION" -Jwarmup="$WARMUP")
# exec 保留调用方 PID：TERM/INT 直达负责收尾的进程。
exec "$PYTHON_BIN" "$BASE_DIR/summary_monitor/run_direct.py" \
  --container "$MONITOR_CONTAINERS" --run-dir "$RUN_DIR" \
  --interval "$MONITOR_INTERVAL" --timeout "$MONITOR_TIMEOUT" --warmup "$WARMUP" \
  --policy "$MONITOR_POLICY" \
  --max-consecutive-failures "$MONITOR_MAX_CONSECUTIVE_FAILURES" \
  --max-failure-percent "$MONITOR_MAX_FAILURE_PERCENT" \
  -- "${JMETER_ARGS[@]}"
