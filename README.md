# AUTOPERF-DCU

DCU 自动化压测与容器资源监控

[run_test.sh](run_test.sh) 一条命令完成：**改服务配置（可选）→ 预热 → JMeter 压测 → 直接采集容器资源 → 输出 PNG 曲线**。每次运行独立目录，原始 JMX 与共享配置永不被改写。

环境：Linux、Docker、`rocm-smi`、JMeter、系统 Python `/usr/bin/python3`。
无需 matplotlib/Pillow（PNG 由系统 librsvg/Cairo 渲染）。

装成命令后可在**任意目录**调用，见 [命令行工具 dcu-bench](#命令行工具-dcu-bench任意目录执行)。

> **Python 必须用 `/usr/bin/python3`。** 本机 conda 的 `libffi.so.7` 指向 libffi 8.x，缺 `LIBFFI_BASE_7.0` 符号，加载系统 `libgobject` 会失败，导致绘图失败、整轮退出码 2。`run_test.sh`（以及封装它的 `dcu-bench`）已固定 `PYTHON_BIN=/usr/bin/python3`；`plot_metrics.py`/`collect_metrics.py` 入口遇到加载失败会自动用系统 Python 重新执行自己。

## 命令行工具 dcu-bench（任意目录执行）

[bin/dcu-bench](bin/dcu-bench) 把整套流程封装成一个命令：装一次，之后在任何目录直接敲 `dcu-bench`，不用再关心脚本放在哪。

```bash
# 安装（软链到 /usr/local/bin，非 root 可加 --prefix ~/.local/bin）
bash install.sh

# 可选：同时装 bash 补全（选项名、子命令、容器名参数都能 Tab）
bash install.sh --with-completion

dcu-bench doctor     # 体检 Python / Docker / rocm-smi / JMeter / ports.json
dcu-bench version
bash install.sh --uninstall   # 只删本项目装的那个软链
```

### 路径规则

`dcu-bench` 用 `readlink -f` 找回工具目录，但**绝不切换工作目录**：

- JMX、结果目录、`--server-config`、`--ports-config` 的相对路径都按**你敲命令时所在的目录**解析；
- `ports.json`、绘图脚本等工具资源固定从工具目录取，与你当前在哪无关；
- 全程 `exec` 转发，退出码与 `Ctrl-C`/`kill` 信号直达真正的压测进程，中间层不吞信号。

```bash
# 在 /tmp 下也能正常工作：结果落在 /tmp/results/run_<容器>_<时间>_<随机>/
cd /tmp
dcu-bench /root/lyc/yc/DCU-TOOL/fs/frdetect/yc.jmx ./results container_a 3 900 --service fs
```

### 子命令

| 子命令 | 作用 |
| --- | --- |
| `dcu-bench run ...` | 完整流程（改配置 → 预热 → 压测 → 采集 → 出图），参数与 `run_test.sh` 完全一致；省略子命令时默认就是 `run` |
| `dcu-bench config ...` | 只改 `server_config.json` 并重启容器（= `update_server_config.sh`） |
| `dcu-bench collect ...` | 只做资源采集（= `summary_monitor/collect_metrics.py`） |
| `dcu-bench plot ...` | 用已有 `metrics.csv` 重绘（= `summary_monitor/plot_metrics.py`） |
| `dcu-bench ports [--json]` | 列出 `ports.json` 的服务名与端口，`--ports-config` 可换配置 |
| `dcu-bench doctor` | 体检运行环境，缺依赖时给出具体项 |
| `dcu-bench version` / `help` | 版本 / 完整帮助 |

`run_test.sh` 保留不变，仍可 `bash run_test.sh ...` 直接调用（相对结果目录现在同样按调用方目录解析），两种入口行为一致。

## 快速开始

装一次就能在任意目录用 `dcu-bench`（没装时把 `dcu-bench` 换成 `bash run_test.sh`，参数完全一致）：

```bash
# 装一次：软链到 /usr/local/bin（非 root 加 --prefix ~/.local/bin）
bash install.sh

dcu-bench <jmx路径> <结果目录> <容器名> <线程数> <正式秒数> --service <服务名> [选项]

# 最小用法
dcu-bench ./fs/frdetect/yc.jmx ./results container_a 3 900 --service fs

# 改配置 + 预热120秒 + 正式600秒 + 出图
dcu-bench ./fs/frdetect/yc.jmx ./results container_a 3 600 --service fs \
  --server-config ./server_config.json --workers 2 --server-threads 8 --warmup 120
```

### 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--service` | **必传** | 服务名，查 `ports.json` 得到 JMX 端口；查不到直接报错，无默认回落 |
| `--port` | 空 | 直接指定端口，优先级最高（只改运行副本） |
| `--ports-config` | `ports.json` | 换一份端口配置 |
| `--warmup` | `0` | 预热秒数，非负整数；第五个参数始终是**正式测量**时长，总施压 = 预热 + 正式 |
| `--monitor-containers` | 压测容器 | 一起采集的容器，逗号分隔 |
| `--monitor-interval` | `0.3` | 采样起点间隔（秒） |
| `--monitor-timeout` | `2` | 每轮 Docker 与 rocm-smi 命令共用的超时预算（秒） |
| `--monitor-policy` | `tolerant` | `strict` 时任意失败样本即判失败 |
| `--monitor-max-consecutive-failures` | `5` | 连续失败达到即停止监控并停压测 |
| `--monitor-max-failure-percent` | `1` | 整轮失败采样比例上限（0～100，等于允许） |
| `--server-config` | 空 | 压测前改 workers/threads 并推进容器（改运行目录副本，源文件不动） |
| `--workers` / `--server-threads` | 空 | 配合 `--server-config`，必须同时给 |
| `--target-path` | `/hexapp/ai-<服务名>-serving/conf/server_config.json` | 容器内配置路径 |
| `--config-wait` | `90` | 重启后等就绪的秒数（容器 running + 本机端口可连）；`0` 跳过探测 |

也可用环境变量 `MONITOR_INTERVAL`、`MONITOR_TIMEOUT`、`MONITOR_POLICY`、`MONITOR_MAX_CONSECUTIVE_FAILURES`、`MONITOR_MAX_FAILURE_PERCENT`、`WARMUP`、`JMETER`，命令行优先。

### 服务端口

[ports.json](ports.json) 是扁平映射，容器名与端口选择无关：

```json
{ "fs": 58096, "bsocr": 58028, "tableocr": 56010, "pygeneral": 58080, "solarpanel": 58066 }
```

只改**启用**的 `HTTPSamplerProxy` 的 `HTTPSampler.port`，禁用采样器保持原值。

## 预热：先施压、不记录

预热期监控只做一次预检（写 `ready.json`，不落 CSV、不周期采样），请求不进 JTL/聚合/HTML 报告；正式窗口由 JMeter 内部首次采样时建立，写入 `measurement_window.json`。

| 阶段 | 业务请求 | 统计/报告 | 资源监控 |
| --- | --- | --- | --- |
| 预检 | 不施压 | 无 | 一次健康检查 |
| 预热 N 秒 | 持续施压 | 不记录 | 不采样 |
| 正式 M 秒 | 同一轮 JMeter | 只纳入窗口内发起的请求 | 记录 CSV、曲线与峰值 |

### 预热参数

| 写法 | 默认 | 说明 |
| --- | --- | --- |
| `--warmup <秒>` | `0` | 预热秒数，非负整数（`0` 或 `[1-9][0-9]*`，前导零会被拒绝） |
| 第五个位置参数 | 必传 | 始终是**正式测量**时长，不含预热 |
| `WARMUP=<秒>` | `0` | 环境变量形式，作用同 `--warmup`，命令行优先 |

```bash
# 预热 120 秒 + 正式测量 600 秒：总施压 720 秒，JTL/报告只算后 600 秒
dcu-bench ./fs/frdetect/yc.jmx ./results container_a 3 600 --service fs --warmup 120
```

- `--warmup 0`（默认）不预热：不改写 JMX 结构、不注入任何脚本，任意多个 ThreadGroup 都能跑。
- `--warmup >0` 才做窗口手术：ThreadGroup 改成持续循环、调度时长 = 预热 + 正式，每个采样器挂一个按**请求开始时间**过滤的后置处理器，所以跨过边界的慢请求算预热、不算正式。
- 窗口文件 `measurement_window.json` 在 JMeter 首次采样时写入（`load_started_epoch_s`、`measurement_start_epoch_s`、`measurement_end_epoch_s`、`warmup_s`、`duration_s`），监控等到它出现才开始落 CSV。
- 配合 `--server-config` 时，改配置、重启、等就绪都在预热之前完成，预热从 JMeter 首次采样算起。

预热对 JMX 结构有要求（不满足会在施压前明确报错，不会静默跑错）：恰好一个启用的普通 ThreadGroup + 启用采样器；不支持事务、Include、Module、自定义控制器、setup/teardown/插件线程组、串行计划。相对路径（CSVDataSet/Include）会按模板目录转成绝对路径。

## 多容器

```bash
# 场景1：两个无关服务同时压测 —— 各跑一次，各自结果目录
dcu-bench ./fs/frdetect/yc.jmx ./results container_a 3 900 --service fs &
dcu-bench ./fs/frocr/yc.jmx ./results container_b 5 900 --service fs &

# 场景2：A 调用 B —— 压测 A，A/B 画进同一批图
dcu-bench ./fs/frdetect/yc.jmx ./results svc-a 10 900 --service fs --monitor-containers svc-a,svc-b
```

未安装 CLI 时，把上面每条 `dcu-bench` 换成 `bash run_test.sh`，位置参数与选项一字不差。

第三个位置参数是压测目标容器；`--monitor-containers` 只决定采谁。每轮 = 每容器一条 `docker top` + 一条共享的 `rocm-smi`，同一时刻取数。

## 指标口径

| CSV 列 | 单位 | 口径 |
| --- | --- | --- |
| `cpu_pct` | % | 容器主进程树读 `/proc/<pid>/stat` 的增量，100% ≈ 一个逻辑核 |
| `vram_mib` | MiB | 占卡进程的显存合计 |
| `vram_pct` | % | 占卡进程的 `VRAM USED(%)` 合计（不是计算利用率） |
| `memory_gib` | GiB | 主进程树的 RSS 合计（非 cgroup，共享页可能重复计入） |

首次采样或 PID 更换时 CPU 需建基线，字段留空不伪造 0；确实无 GPU 进程时记 `no_gpu_processes` 与 0 值。

## 失败处理与判定

- 每轮成功/失败立即 flush；超时杀进程组，字段留空记 `timeout`，不写成 0；部分读取失败记 `partial`。
- 启动 JMeter 前先预检一次，失败则不施压，仍保留 CSV/日志。
- `tolerant`（默认）：失败比例 ≤ 上限即通过，曲线在失败点断开；连续失败达阈值立即停监控并停压测。
- 状态文件 `monitor_status.json`：`healthy`、`accepted`、`verdict`(ok/warning/failed)、`failure_reasons`，预热模式另有 `measurement_completed`、`preflight_recorded`，`duration_s` 为正式窗口时长。**判成功看 `accepted`，不要只看 `healthy`。**

## 每次运行目录

```text
results/run_<容器名>_<时间>_<随机串>/
├── plan.jmx                 # 独立运行副本（端口已改）
├── result.jtl / jmeter.log / report/index.html
├── metrics.csv              # 四指标 + 状态 + 采样耗时
├── ready.json               # 监控预检
├── monitor.log / monitor_status.json
├── measurement_window.json  # 仅 --warmup>0
├── server_config.json       # 仅 --server-config（本次实际推进容器的副本）
└── plots/
    ├── cpu.png / vram.png / vram_utilization.png / memory.png   # 2000×680
    └── summary.json         # 峰值、峰值时间、有效/缺失点数
```

默认只输出 PNG，不落任何 `.svg`（需要时给绘图命令加 `--keep-svg`）。

## 改服务配置（已并入压测流程）

`--server-config` 把 [update_server_config.sh](update_server_config.sh) 串进来：复制配置到运行目录 → 改副本的 `workers`/`threads` → `docker cp` → `docker restart` → 等就绪 → 才开始压测。源配置永远不改写。也可单独使用：

```bash
bash update_server_config.sh ./server_config.json <容器名> <服务名> 4 8 \
  [--target-path /容器内/绝对路径/server_config.json]

# 或走 CLI，参数相同
dcu-bench config ./server_config.json <容器名> <服务名> 4 8 \
  [--target-path /容器内/绝对路径/server_config.json]
```

## 单独采集或重绘

```bash
/usr/bin/python3 summary_monitor/collect_metrics.py --container a --container b \
  --output-dir ./standalone --interval 0.3 --timeout 2 --duration 60
# 不传 --duration 可一直采集，INT/TERM 停止后自动绘图

/usr/bin/python3 summary_monitor/plot_metrics.py ./standalone/metrics.csv --output-dir ./standalone/plots

# 用 CLI 写更短，且不需要先 cd 到工具目录
dcu-bench collect --container a --container b --output-dir ./standalone --duration 60
dcu-bench plot ./standalone/metrics.csv --output-dir ./standalone/plots
```

历史失败轮次无法补画真实曲线。

## 回归测试

```bash
# 模拟 JMeter/Docker/rocm-smi，不向真实接口施压；必须用系统 Python
/usr/bin/python3 -B -m unittest discover -s tests -p 'test_*.py' -v

# 只跑 CLI 契约（跨目录调用、ports、安装/卸载、模拟完整 run）
/usr/bin/python3 -B -m unittest discover -s tests -p 'test_cli.py' -v

# 附加本机 JMeter 的本地虚拟采样器验证（不请求业务接口）
XNCS_TEST_REAL_JMETER=1 /usr/bin/python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```
