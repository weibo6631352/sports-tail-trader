"""系统性能监控（in-memory，无持久化）。

收集：
- boot_phase_timings: 各模块/阶段启动耗时
- http_endpoint_stats: FastAPI middleware 自动统计（call_count / latency p50/p90/p99 / errors）
- ws_traffic: WS in/out 消息数 + bytes（由各 client 主动 record）
- db_query_latencies: DB 查询延迟分位
- process_metrics: CPU / RSS memory / thread count / file descriptors
- asyncio_metrics: pending tasks / running / exceptions
- runtime_uptime: 系统运行时长
"""
from __future__ import annotations

import time
import asyncio
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any


class _EndpointStats:
    """per HTTP endpoint 性能 — 区分程序处理时间 + 响应体大小 + 流量。

    - handler_ms_*: 程序内部处理时间（FastAPI middleware 看到的，不含网络传输）
    - response_size_*: 返回字节数（衡量"网络传输因子" — 大 response 即使 handler 快，
      客户端仍会等待传输）
    - bytes_per_second: 派生流量（total_bytes / uptime）
    """
    __slots__ = (
        "call_count", "error_count",
        "total_handler_ms", "handler_buffer",
        "total_response_bytes", "response_size_buffer",
        "first_seen", "last_called",
    )

    def __init__(self) -> None:
        self.call_count = 0
        self.error_count = 0
        self.total_handler_ms = 0.0
        self.handler_buffer: deque[float] = deque(maxlen=500)
        self.total_response_bytes = 0
        self.response_size_buffer: deque[int] = deque(maxlen=500)
        self.first_seen = time.time()
        self.last_called = 0.0

    def record(self, handler_ms: float, response_bytes: int, error: bool) -> None:
        self.call_count += 1
        if error:
            self.error_count += 1
        self.total_handler_ms += handler_ms
        self.handler_buffer.append(handler_ms)
        self.total_response_bytes += response_bytes
        self.response_size_buffer.append(response_bytes)
        self.last_called = time.time()

    def percentiles(self) -> dict[str, Any]:
        buf = sorted(self.handler_buffer)
        sz = sorted(self.response_size_buffer)
        result: dict[str, Any] = {}
        if buf:
            n = len(buf)
            result["handler_p50_ms"] = round(buf[int(n * 0.5)], 2)
            result["handler_p90_ms"] = round(buf[int(n * 0.9)], 2)
            result["handler_p99_ms"] = round(buf[min(int(n * 0.99), n - 1)], 2)
            result["handler_min_ms"] = round(buf[0], 2)
            result["handler_max_ms"] = round(buf[-1], 2)
        if sz:
            n = len(sz)
            result["response_size_p50_b"] = sz[int(n * 0.5)]
            result["response_size_p99_b"] = sz[min(int(n * 0.99), n - 1)]
            result["response_size_max_kb"] = round(sz[-1] / 1024, 2)
        return result


class SystemPerfMonitor:
    """单例 in-memory 性能监控（无持久化，重启清零）。"""

    _instance: "SystemPerfMonitor | None" = None
    _instance_lock = RLock()

    def __init__(self) -> None:
        self.boot_phase_timings: dict[str, float] = {}  # phase → elapsed_seconds
        self._boot_phase_starts: dict[str, float] = {}
        self.endpoint_stats: dict[str, _EndpointStats] = defaultdict(_EndpointStats)
        self.ws_traffic: dict[str, dict[str, int]] = defaultdict(
            lambda: {"in_msg_count": 0, "in_bytes": 0, "out_msg_count": 0, "out_bytes": 0, "errors": 0}
        )
        self.db_query_latencies: deque[float] = deque(maxlen=500)
        self.db_query_count = 0
        self.db_error_count = 0
        # 程序级 worker 频率监控（per worker name）：
        # tick_intervals: 实际每次 tick 间隔时序（最近 100 次），便于算实际频率 vs 期望
        # expected_interval_s: 设定值
        self.worker_ticks: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "last_tick": 0.0, "expected_interval_s": None,
                     "intervals": deque(maxlen=100), "errors": 0}
        )
        # 事件链路 latency 跟踪：event_type → 处理耗时分位
        # 入: "orderbook_snapshot_updated", "fill_recorded" 等
        # 关心 publish→consume 的端到端延迟
        self.event_latencies: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=200))
        self.event_counts: dict[str, int] = defaultdict(int)
        # decision 端到端延迟：candidate evaluate → order submit → fill
        self.decision_pipeline: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=200))
        # 内存增长追踪：每 60s 采 RSS 一次，最多 60 点（1h 数据）
        self.memory_samples: deque[tuple[float, float]] = deque(maxlen=60)  # (ts, rss_mb)
        # 内存高频时序(每 30s,保留 120 点 = 1h):用于内存曲线 endpoint
        self.memory_timeseries: deque[tuple[float, float]] = deque(maxlen=120)
        # event loop scheduler lag: 后台 task 定期 await asyncio.sleep(0.1),
        # 实际超过预期就反映 loop 被卡.deque ms 值,最近 200 点.
        self.eventloop_lags_ms: deque[float] = deque(maxlen=200)
        # workflow hook 耗时分位:hook_name → deque[ms]
        self.hook_latencies: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=200))
        # active 并发 HTTP request 数(middleware enter/exit 计数器)
        self.active_http_requests: int = 0
        self.peak_active_http_requests: int = 0
        # per-token WS message rate: token_id → count
        self.per_token_ws_counts: dict[str, int] = defaultdict(int)
        # per-route write throughput (audit/decision/outbox/market 等)
        # route_name → count
        self.persistence_route_writes: dict[str, int] = defaultdict(int)
        # block detection: 单次 callback > 50ms 记录 (timestamp, duration_ms, task_name)
        self.slow_callback_events: deque[dict[str, Any]] = deque(maxlen=100)
        # per-table SQL counter: table_name → {"select": N, "insert": N, "update": N, "delete": N}
        self.per_table_sql: dict[str, dict[str, int]] = defaultdict(
            lambda: {"select": 0, "insert": 0, "update": 0, "delete": 0}
        )
        # 协程异常计数
        self.coroutine_exceptions: dict[str, int] = defaultdict(int)
        # PnL 时序（每 30s 一次，最多 120 点）
        self.pnl_samples: deque[tuple[float, float]] = deque(maxlen=120)
        # WS queue 实时状态：channel → (qsize, maxsize, peak, full_count)
        self.ws_queues: dict[str, dict[str, int]] = defaultdict(lambda: {"qsize": 0, "maxsize": 0, "peak": 0, "full_count": 0})
        # API 调用计数：per client per outcome
        # client_api_stats: client_name → {"success": N, "errors": N, "by_error_type": {type: N}}
        self.client_api_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"success": 0, "errors": 0, "by_error_type": defaultdict(int), "last_error": None}
        )
        # 管道 CPU 占用追踪 (cpu_track decorator 上报):
        # pipeline_name → deque[(timestamp, cpu_ms, wall_ms)]
        # 滚动窗口 600 个采样, 按时间过滤算最近 N 秒.
        # 用 time.process_time() 与 perf_counter 的差 → 真实 CPU 占用 vs wall 总时长.
        self.pipeline_cpu_samples: dict[str, deque[tuple[float, float, float]]] = defaultdict(
            lambda: deque(maxlen=600)
        )
        # pipeline 内部 step 耗时追踪 (step_track ctx 上报):
        # (pipeline, step) → deque[(timestamp, cpu_ms, wall_ms)]
        # 按 pipeline 分组聚合后给 /runtime/pipeline-health.
        self.pipeline_step_samples: dict[tuple[str, str], deque[tuple[float, float, float]]] = defaultdict(
            lambda: deque(maxlen=600)
        )
        self.started_at = time.time()
        self._lock = RLock()

    @classmethod
    def get(cls) -> "SystemPerfMonitor":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = SystemPerfMonitor()
            return cls._instance

    # Boot 阶段
    def start_phase(self, name: str) -> None:
        with self._lock:
            self._boot_phase_starts[name] = time.time()

    def end_phase(self, name: str) -> float:
        with self._lock:
            start = self._boot_phase_starts.pop(name, None)
            if start is None:
                return 0.0
            elapsed = time.time() - start
            self.boot_phase_timings[name] = elapsed
            return elapsed

    # HTTP — 区分 handler 处理时间 vs 响应大小（流量）
    def record_http(self, endpoint: str, handler_ms: float, response_bytes: int = 0, error: bool = False) -> None:
        with self._lock:
            self.endpoint_stats[endpoint].record(handler_ms, response_bytes, error)

    # WS
    def record_ws_in(self, channel: str, msg_size_bytes: int = 0) -> None:
        with self._lock:
            t = self.ws_traffic[channel]
            t["in_msg_count"] += 1
            t["in_bytes"] += int(msg_size_bytes)

    def record_ws_out(self, channel: str, msg_size_bytes: int = 0) -> None:
        with self._lock:
            t = self.ws_traffic[channel]
            t["out_msg_count"] += 1
            t["out_bytes"] += int(msg_size_bytes)

    def record_ws_error(self, channel: str) -> None:
        with self._lock:
            self.ws_traffic[channel]["errors"] += 1

    # Worker 频率
    def worker_tick(self, name: str, expected_interval_s: float | None = None) -> None:
        """每次 worker 循环 / fetch 完成时调用，记录实际间隔。"""
        with self._lock:
            now = time.time()
            stat = self.worker_ticks[name]
            if stat["last_tick"] > 0:
                interval = now - stat["last_tick"]
                stat["intervals"].append(interval)
            stat["last_tick"] = now
            stat["count"] += 1
            if expected_interval_s is not None:
                stat["expected_interval_s"] = expected_interval_s

    def worker_error(self, name: str) -> None:
        with self._lock:
            self.worker_ticks[name]["errors"] += 1

    # 事件链路
    def report_ws_queue(self, channel: str, qsize: int, maxsize: int) -> None:
        """ws_loops 每次 offer 时调用，实时更新 queue 状态。"""
        with self._lock:
            q = self.ws_queues[channel]
            q["qsize"] = qsize
            q["maxsize"] = maxsize
            if qsize > q["peak"]:
                q["peak"] = qsize

    def record_api_call(
        self, client_name: str, success: bool,
        error_type: str | None = None, latency_ms: float | None = None,
    ) -> None:
        """各 client (goalserve / polymarket / clob / data / gamma) HTTP 调用统计.

        latency_ms 为每次 HTTP 请求端到端耗时(DNS+TCP+TLS+request+response),
        O(1) 写入 deque,不阻塞 P0.
        """
        with self._lock:
            st = self.client_api_stats[client_name]
            if success:
                st["success"] += 1
            else:
                st["errors"] += 1
                if error_type:
                    st["by_error_type"][error_type] += 1
                    st["last_error"] = error_type
            if latency_ms is not None:
                if "latencies" not in st:
                    st["latencies"] = deque(maxlen=200)
                st["latencies"].append(float(latency_ms))

    def sample_memory(self, rss_mb: float) -> None:
        """后台 60s 采样内存（用于增长率检测）。"""
        with self._lock:
            now = time.time()
            self.memory_samples.append((now, rss_mb))
            self.memory_timeseries.append((now, rss_mb))

    # === 新增性能指标 ===

    def record_eventloop_lag(self, lag_ms: float) -> None:
        """后台 task await sleep(0.1) 实际耗时,反映 event loop 是否被阻塞."""
        with self._lock:
            self.eventloop_lags_ms.append(lag_ms)

    def record_pipeline_cpu(self, pipeline: str, *, cpu_ms: float, wall_ms: float) -> None:
        """``cpu_track`` decorator 上报: 按管道维度累计 CPU + wall 时间.

        cpu_ms 由 ``time.process_time()`` 差算出 (跨 await 不计), 反映该管道
        真实占用 CPU 时间片. wall_ms 是端到端墙上时间 (含 await IO 等待).
        cpu/wall 比值 = "CPU 效率": 接近 1 表示纯 CPU 任务, 接近 0 表示 IO 密集.
        """
        with self._lock:
            self.pipeline_cpu_samples[pipeline].append((time.time(), float(cpu_ms), float(wall_ms)))

    def record_pipeline_step(self, pipeline: str, step: str, *, cpu_ms: float, wall_ms: float) -> None:
        """``step_track`` ctx 管理器上报: pipeline 内部细分 step 耗时.

        与 ``record_pipeline_cpu`` 并行使用——pipeline 是函数整体, step 是内部段.
        ``/runtime/pipeline-health`` 按 pipeline 分组展示各 step 的耗时占比.
        """
        with self._lock:
            self.pipeline_step_samples[(pipeline, step)].append(
                (time.time(), float(cpu_ms), float(wall_ms))
            )

    def record_hook_latency(self, hook_name: str, latency_ms: float) -> None:
        """记录 hook 调用耗时——当前只有 ``quant_decide``，后续接入更多 hook
        （如 ``match_live_state``）也走同一份监控。"""
        with self._lock:
            self.hook_latencies[hook_name].append(latency_ms)

    def http_request_enter(self) -> int:
        """middleware 入口,返回当前 active 数."""
        with self._lock:
            self.active_http_requests += 1
            if self.active_http_requests > self.peak_active_http_requests:
                self.peak_active_http_requests = self.active_http_requests
            return self.active_http_requests

    def http_request_exit(self) -> None:
        with self._lock:
            self.active_http_requests = max(0, self.active_http_requests - 1)

    def record_per_token_ws(self, token_id: str) -> None:
        """每条 ws message 按 token_id 计数."""
        with self._lock:
            self.per_token_ws_counts[token_id] += 1

    def record_persistence_route_write(self, route: str, count: int = 1) -> None:
        """persistence worker 写入按 route(audit/decision/outbox/market/...)计数."""
        with self._lock:
            self.persistence_route_writes[route] += count

    def record_slow_callback(self, duration_ms: float, task_name: str | None = None) -> None:
        """单次 callback 耗时超过阈值(50ms)就记录,反映 main loop 被卡的瞬间."""
        with self._lock:
            self.slow_callback_events.append({
                "ts": time.time(), "duration_ms": round(duration_ms, 1),
                "task_name": task_name or "?",
            })

    def sample_pnl(self, pnl_usdc: float) -> None:
        """后台 30s 采样 PnL（用于 drift rate）。"""
        with self._lock:
            self.pnl_samples.append((time.time(), pnl_usdc))

    def record_coroutine_exception(self, task_name: str) -> None:
        with self._lock:
            self.coroutine_exceptions[task_name] += 1

    def record_event_latency(self, event_type: str, latency_ms: float) -> None:
        """事件 publish→consume 延迟。"""
        with self._lock:
            self.event_latencies[event_type].append(latency_ms)
            self.event_counts[event_type] += 1

    # 决策链路
    def record_decision_step(self, step: str, latency_ms: float) -> None:
        """决策链路某步骤耗时。step 如: 'evaluate', 'risk', 'sign', 'submit'。"""
        with self._lock:
            self.decision_pipeline[step].append(latency_ms)

    # DB
    def record_db_query(self, latency_ms: float, error: bool = False, statement: str | None = None) -> None:
        with self._lock:
            self.db_query_count += 1
            if error:
                self.db_error_count += 1
            self.db_query_latencies.append(latency_ms)
            if statement and latency_ms > 0:
                # 归一化 statement: 取 SELECT/INSERT 等首词 + FROM 后的表名作 key
                key = _normalize_sql_key(statement)
                if not hasattr(self, "db_slow_queries"):
                    from collections import defaultdict, deque
                    self.db_slow_queries = defaultdict(lambda: deque(maxlen=200))
                self.db_slow_queries[key].append(latency_ms)
                # per-table 计数(verb + table 从 key 拆解)
                parts = key.split(" ", 1)
                if len(parts) == 2:
                    verb_text, table_text = parts[0].lower(), parts[1]
                    if verb_text in ("select", "insert", "update", "delete"):
                        self.per_table_sql[table_text][verb_text] += 1

    def record_endpoint_step(self, endpoint: str, step: str, latency_ms: float) -> None:
        """endpoint 内部分步骤耗时。step 如 'db', 'serialize'。"""
        with self._lock:
            if not hasattr(self, "endpoint_step_latencies"):
                from collections import defaultdict, deque
                self.endpoint_step_latencies = defaultdict(lambda: deque(maxlen=500))
            self.endpoint_step_latencies[f"{endpoint}:{step}"].append(latency_ms)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            uptime_s = now - self.started_at
            # 进程级 + 系统级指标 — 优先 Python 内置（resource/os/threading 无外部依赖），
            # psutil 可选用于更深指标（CPU per core / net io / connections）
            process_metrics: dict[str, Any] = {}
            system_metrics: dict[str, Any] = {}
            # === 内置 ===
            try:
                import resource  # Unix only
                ru = resource.getrusage(resource.RUSAGE_SELF)
                # macOS: ru_maxrss 单位 bytes；Linux: KB
                import platform
                rss_bytes = ru.ru_maxrss if platform.system() == "Darwin" else ru.ru_maxrss * 1024
                process_metrics["memory_rss_mb"] = round(rss_bytes / 1024 / 1024, 1)
                process_metrics["cpu_time_user_s"] = round(ru.ru_utime, 2)
                process_metrics["cpu_time_system_s"] = round(ru.ru_stime, 2)
                process_metrics["page_faults_minor"] = ru.ru_minflt
                process_metrics["page_faults_major"] = ru.ru_majflt
                process_metrics["block_reads"] = ru.ru_inblock
                process_metrics["block_writes"] = ru.ru_oublock
                process_metrics["voluntary_ctx_switches"] = ru.ru_nvcsw
                process_metrics["involuntary_ctx_switches"] = ru.ru_nivcsw
            except Exception as e: process_metrics["resource_err"] = str(e)[:50]
            try:
                import threading
                process_metrics["threads"] = threading.active_count()
                process_metrics["thread_names"] = [t.name for t in threading.enumerate()][:20]
            except Exception: pass
            try:
                process_metrics["pid"] = os.getpid()
            except Exception: pass
            # File descriptors（Linux: /proc/self/fd, macOS: 用 SC_OPEN_MAX 间接）
            try:
                if os.path.isdir("/proc/self/fd"):
                    process_metrics["open_fds"] = len(os.listdir("/proc/self/fd"))
                else:
                    # macOS: 没有 /proc，没有简单内置方法。跳过。
                    pass
            except Exception: pass
            # === 系统级（内置）===
            try:
                cpu_logical = os.cpu_count()
                system_metrics["cpu_count_logical"] = cpu_logical
                if hasattr(os, "sched_getaffinity"):
                    system_metrics["cpu_count_affinity"] = len(os.sched_getaffinity(0))
            except Exception: pass
            try:
                la1, la5, la15 = os.getloadavg()
                system_metrics["load_avg_1min"] = round(la1, 2)
                system_metrics["load_avg_5min"] = round(la5, 2)
                system_metrics["load_avg_15min"] = round(la15, 2)
                # 转换为 CPU 占用百分比
                if cpu_logical and cpu_logical > 0:
                    system_metrics["load_pct_1min"] = round(la1 / cpu_logical * 100, 1)
                    system_metrics["load_pct_5min"] = round(la5 / cpu_logical * 100, 1)
            except Exception: pass
            # statvfs (磁盘)
            try:
                stat = os.statvfs("/")
                disk_total_gb = stat.f_blocks * stat.f_frsize / 1024 ** 3
                disk_free_gb = stat.f_bavail * stat.f_frsize / 1024 ** 3
                system_metrics["disk_total_gb"] = round(disk_total_gb, 2)
                system_metrics["disk_free_gb"] = round(disk_free_gb, 2)
                system_metrics["disk_used_pct"] = round((disk_total_gb - disk_free_gb) / disk_total_gb * 100, 1) if disk_total_gb > 0 else 0
            except Exception: pass
            # === psutil 补充（深度指标：CPU per core / net io / connections / VMS）===
            psutil = None  # type: ignore[assignment]
            try:
                import psutil  # type: ignore  # noqa: F811
            except BaseException as e:
                # psutil 装在哪个 venv 经常错位，import 失败时把异常类型暴露出来便于诊断
                process_metrics["psutil_import_err"] = f"{type(e).__name__}: {str(e)[:120]}"
            if psutil is not None:
                try:
                    proc = psutil.Process(os.getpid())
                    process_metrics["cpu_percent"] = proc.cpu_percent(interval=None)
                except Exception as e: process_metrics["cpu_percent_err"] = str(e)[:50]
                try:
                    proc = psutil.Process(os.getpid())
                    mi = proc.memory_info()
                    process_metrics["memory_rss_mb"] = round(mi.rss / 1024 / 1024, 1)
                    process_metrics["memory_vms_mb"] = round(mi.vms / 1024 / 1024, 1)
                    process_metrics["memory_percent"] = round(proc.memory_percent(), 2)
                except Exception as e: process_metrics["memory_err"] = str(e)[:50]
                try:
                    process_metrics["threads"] = psutil.Process(os.getpid()).num_threads()
                except Exception: pass
                try:
                    process_metrics["open_files"] = len(psutil.Process(os.getpid()).open_files())
                except Exception: pass
                try:
                    cs = psutil.Process(os.getpid()).num_ctx_switches()
                    process_metrics["ctx_switches_voluntary"] = cs.voluntary
                    process_metrics["ctx_switches_involuntary"] = cs.involuntary
                except Exception: pass
                try:
                    io = psutil.Process(os.getpid()).io_counters()
                    process_metrics["io_read_count"] = io.read_count
                    process_metrics["io_write_count"] = io.write_count
                    process_metrics["io_read_mb"] = round(io.read_bytes / 1024 / 1024, 2)
                    process_metrics["io_write_mb"] = round(io.write_bytes / 1024 / 1024, 2)
                except Exception: pass
                try:
                    process_metrics["connections_inet"] = len(psutil.Process(os.getpid()).net_connections(kind="inet"))
                except Exception: pass
                # 系统级
                try:
                    vm = psutil.virtual_memory()
                    system_metrics["memory_total_gb"] = round(vm.total / 1024 ** 3, 2)
                    system_metrics["memory_available_gb"] = round(vm.available / 1024 ** 3, 2)
                    system_metrics["memory_used_pct"] = vm.percent
                except Exception as e: system_metrics["memory_err"] = str(e)[:50]
                try:
                    disk = psutil.disk_usage('/')
                    system_metrics["disk_total_gb"] = round(disk.total / 1024 ** 3, 2)
                    system_metrics["disk_free_gb"] = round(disk.free / 1024 ** 3, 2)
                    system_metrics["disk_used_pct"] = disk.percent
                except Exception: pass
                try:
                    system_metrics["cpu_count_logical"] = psutil.cpu_count(logical=True)
                    system_metrics["cpu_count_physical"] = psutil.cpu_count(logical=False)
                    system_metrics["cpu_percent_total"] = psutil.cpu_percent(interval=None)
                    system_metrics["cpu_per_core_pct"] = psutil.cpu_percent(interval=None, percpu=True)
                except Exception as e: system_metrics["cpu_err"] = str(e)[:50]
                try:
                    load1, load5, load15 = os.getloadavg()
                    system_metrics["load_avg_1min"] = load1
                    system_metrics["load_avg_5min"] = load5
                    system_metrics["load_avg_15min"] = load15
                except Exception: pass
                try:
                    net = psutil.net_io_counters()
                    system_metrics["net_bytes_sent_mb"] = round(net.bytes_sent / 1024 / 1024, 2)
                    system_metrics["net_bytes_recv_mb"] = round(net.bytes_recv / 1024 / 1024, 2)
                    system_metrics["net_packets_sent"] = net.packets_sent
                    system_metrics["net_packets_recv"] = net.packets_recv
                    system_metrics["net_errin"] = net.errin
                    system_metrics["net_errout"] = net.errout
                except Exception: pass
                try:
                    disk_io = psutil.disk_io_counters()
                    if disk_io:
                        system_metrics["disk_read_mb"] = round(disk_io.read_bytes / 1024 / 1024, 2)
                        system_metrics["disk_write_mb"] = round(disk_io.write_bytes / 1024 / 1024, 2)
                        system_metrics["disk_read_count"] = disk_io.read_count
                        system_metrics["disk_write_count"] = disk_io.write_count
                except Exception: pass
            # asyncio task 统计
            asyncio_metrics: dict[str, Any] = {}
            try:
                loop = asyncio.get_running_loop()
                all_tasks = asyncio.all_tasks(loop)
                asyncio_metrics = {
                    "task_count": len(all_tasks),
                    "running_task_names": [
                        t.get_name() for t in all_tasks if not t.done()
                    ][:30],
                }
            except RuntimeError:
                asyncio_metrics = {"no_running_loop": True}
            # 各 endpoint 派生指标
            endpoint_summary: list[dict[str, Any]] = []
            for ep, stats in self.endpoint_stats.items():
                pct = stats.percentiles()
                avg_handler_ms = round(stats.total_handler_ms / stats.call_count, 2) if stats.call_count else 0
                avg_resp_bytes = round(stats.total_response_bytes / stats.call_count) if stats.call_count else 0
                endpoint_summary.append({
                    "endpoint": ep,
                    "call_count": stats.call_count,
                    "error_count": stats.error_count,
                    "error_rate_pct": round(stats.error_count / stats.call_count * 100, 2) if stats.call_count else 0,
                    # 程序处理时间（middleware 测的，是真实 server-side 处理，不含客户端网络）
                    "handler_avg_ms": avg_handler_ms,
                    **pct,
                    # 响应体大小（真实数据，给客户端自己估传输用）
                    "avg_response_size_b": avg_resp_bytes,
                    "avg_response_size_kb": round(avg_resp_bytes / 1024, 2),
                    "total_response_mb": round(stats.total_response_bytes / 1024 / 1024, 2),
                    "last_called_s_ago": round(now - stats.last_called, 1) if stats.last_called else None,
                    "calls_per_minute": round(stats.call_count / max(uptime_s / 60, 0.01), 1),
                    "throughput_kbps": round(stats.total_response_bytes / 1024 / max(uptime_s, 0.01), 2),
                })
            endpoint_summary.sort(key=lambda x: -x["call_count"])
            # WS 流量
            ws_traffic_summary = []
            for ch, t in self.ws_traffic.items():
                rate = t["in_msg_count"] / max(uptime_s, 0.01)
                bytes_per_s = t["in_bytes"] / max(uptime_s, 0.01)
                ws_traffic_summary.append({
                    "channel": ch,
                    "in_msg_total": t["in_msg_count"],
                    "in_msg_rate_per_s": round(rate, 2),
                    "in_bandwidth_kbps": round(bytes_per_s / 1024 * 8, 2),
                    "in_bytes_total_kb": round(t["in_bytes"] / 1024, 1),
                    "out_msg_total": t["out_msg_count"],
                    "out_bytes_total_kb": round(t["out_bytes"] / 1024, 1),
                    "errors": t["errors"],
                })
            ws_traffic_summary.sort(key=lambda x: -x["in_msg_total"])
            # DB
            db_buf = sorted(self.db_query_latencies)
            db_pct = {}
            if db_buf:
                n = len(db_buf)
                db_pct = {
                    "p50": round(db_buf[int(n * 0.5)], 2),
                    "p90": round(db_buf[int(n * 0.9)], 2),
                    "p99": round(db_buf[min(int(n * 0.99), n - 1)], 2),
                    "min": round(db_buf[0], 2),
                    "max": round(db_buf[-1], 2),
                }
            # Worker 频率：实际 vs 期望
            worker_perf = []
            for name, st in self.worker_ticks.items():
                intervals = list(st["intervals"])
                if intervals:
                    avg = sum(intervals) / len(intervals)
                    min_iv = min(intervals)
                    max_iv = max(intervals)
                    expected = st["expected_interval_s"]
                    drift_pct = ((avg - expected) / expected * 100) if expected else None
                    worker_perf.append({
                        "worker": name,
                        "tick_count": st["count"],
                        "errors": st["errors"],
                        "actual_avg_interval_s": round(avg, 3),
                        "actual_min_s": round(min_iv, 3),
                        "actual_max_s": round(max_iv, 3),
                        "actual_rate_hz": round(1.0 / avg, 3) if avg > 0 else None,
                        "expected_interval_s": expected,
                        "drift_pct": round(drift_pct, 1) if drift_pct is not None else None,
                        "health": ("ok" if drift_pct is None or abs(drift_pct) < 20
                                   else ("slow" if drift_pct > 0 else "fast")),
                    })
            worker_perf.sort(key=lambda x: -x["tick_count"])
            # 事件链路 latency 分位
            event_perf = []
            for et, lats in self.event_latencies.items():
                if not lats: continue
                buf = sorted(lats)
                n = len(buf)
                event_perf.append({
                    "event_type": et,
                    "count": self.event_counts[et],
                    "p50_ms": round(buf[int(n*0.5)], 2),
                    "p90_ms": round(buf[int(n*0.9)], 2),
                    "p99_ms": round(buf[min(int(n*0.99), n-1)], 2),
                    "max_ms": round(buf[-1], 2),
                })
            # decision pipeline 分位
            decision_perf = []
            for step, lats in self.decision_pipeline.items():
                if not lats: continue
                buf = sorted(lats)
                n = len(buf)
                decision_perf.append({
                    "step": step,
                    "samples": n,
                    "p50_ms": round(buf[int(n*0.5)], 2),
                    "p90_ms": round(buf[int(n*0.9)], 2),
                    "p99_ms": round(buf[min(int(n*0.99), n-1)], 2),
                    "max_ms": round(buf[-1], 2),
                })
            # 内存增长率（最近 N 个采样 first→last delta）
            memory_growth: dict[str, Any] = {}
            if len(self.memory_samples) >= 2:
                first_ts, first_rss = self.memory_samples[0]
                last_ts, last_rss = self.memory_samples[-1]
                duration_s = last_ts - first_ts
                if duration_s > 0:
                    rate_mb_per_h = (last_rss - first_rss) / duration_s * 3600
                    memory_growth = {
                        "samples_count": len(self.memory_samples),
                        "duration_minutes": round(duration_s / 60, 1),
                        "rss_first_mb": round(first_rss, 1),
                        "rss_last_mb": round(last_rss, 1),
                        "delta_mb": round(last_rss - first_rss, 1),
                        "growth_rate_mb_per_h": round(rate_mb_per_h, 2),
                        "leak_suspicion": rate_mb_per_h > 50,  # >50MB/h 可疑泄漏
                    }
            # GC 统计
            gc_stats: dict[str, Any] = {}
            try:
                import gc
                gc_stats = {
                    "collections": gc.get_count(),
                    "gen0_thresholds": gc.get_threshold(),
                }
                # gc.get_stats() 给每代统计含 collections / collected / uncollectable
                stats_list = gc.get_stats()
                if stats_list:
                    gc_stats["per_generation"] = [
                        {
                            "generation": i,
                            "collections": s.get("collections", 0),
                            "collected": s.get("collected", 0),
                            "uncollectable": s.get("uncollectable", 0),
                        }
                        for i, s in enumerate(stats_list)
                    ]
            except Exception as e:
                gc_stats = {"error": str(e)[:50]}
            # PnL drift rate（最近 N 个采样 first→last delta）
            pnl_drift: dict[str, Any] = {}
            if len(self.pnl_samples) >= 2:
                first_ts, first_pnl = self.pnl_samples[0]
                last_ts, last_pnl = self.pnl_samples[-1]
                duration_s = last_ts - first_ts
                if duration_s > 0:
                    rate_per_min = (last_pnl - first_pnl) / duration_s * 60
                    pnl_drift = {
                        "samples_count": len(self.pnl_samples),
                        "duration_minutes": round(duration_s / 60, 1),
                        "first_pnl": round(first_pnl, 4),
                        "last_pnl": round(last_pnl, 4),
                        "drift_per_min_usdc": round(rate_per_min, 4),
                        "direction": "gain" if rate_per_min > 0.05 else ("loss" if rate_per_min < -0.05 else "stable"),
                    }
            return {
                "uptime_seconds": round(uptime_s, 1),
                "uptime_human": _format_duration(uptime_s),
                "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
                "boot_phase_timings": {k: round(v, 3) for k, v in self.boot_phase_timings.items()},
                "worker_frequencies": worker_perf,
                "event_latencies": event_perf,
                "decision_pipeline_latencies": decision_perf,
                "memory_growth": memory_growth,
                "pnl_drift": pnl_drift,
                "gc_stats": gc_stats,
                # WS 内部队列（market_ws / user_ws 4096 缓冲）
                "ws_queues": [
                    {
                        "channel": ch,
                        "qsize": q["qsize"],
                        "maxsize": q["maxsize"],
                        "util_pct": round(q["qsize"] / q["maxsize"] * 100, 2) if q["maxsize"] else 0,
                        "peak_qsize": q["peak"],
                        "peak_util_pct": round(q["peak"] / q["maxsize"] * 100, 2) if q["maxsize"] else 0,
                    }
                    for ch, q in self.ws_queues.items()
                ],
                # API 调用异常计数（per client）
                "client_api_stats": [
                    _client_api_row(cn, st)
                    for cn, st in self.client_api_stats.items()
                ],
                "coroutine_exceptions": dict(self.coroutine_exceptions),
                "coroutine_exception_total": sum(self.coroutine_exceptions.values()),
                "process_metrics": process_metrics,
                "system_metrics": system_metrics,
                "asyncio_metrics": asyncio_metrics,
                "http_endpoints": {
                    "total_calls": sum(s.call_count for s in self.endpoint_stats.values()),
                    "total_errors": sum(s.error_count for s in self.endpoint_stats.values()),
                    "unique_endpoints": len(self.endpoint_stats),
                    "top_endpoints": endpoint_summary[:30],
                },
                "ws_traffic": ws_traffic_summary,
                "db_queries": {
                    "total": self.db_query_count,
                    "errors": self.db_error_count,
                    "buffered_samples": len(self.db_query_latencies),
                    **db_pct,
                    "top_slow_by_p99": _top_slow_sql(getattr(self, "db_slow_queries", {}), limit=15),
                },
                "endpoint_steps": _endpoint_steps_summary(
                    getattr(self, "endpoint_step_latencies", {}), limit=30
                ),
                "eventloop_lag": _eventloop_lag_summary(self.eventloop_lags_ms),
                "hook_latencies": _hooks_summary(self.hook_latencies),
                "http_concurrent": {
                    "active": self.active_http_requests,
                    "peak": self.peak_active_http_requests,
                },
                "persistence_route_writes": dict(self.persistence_route_writes),
                "per_table_sql": {tbl: dict(verbs) for tbl, verbs in self.per_table_sql.items()},
                "top_per_token_ws_rate": _top_per_token_ws(self.per_token_ws_counts, uptime_s, limit=20),
                "slow_callback_count": len(self.slow_callback_events),
                "slow_callback_recent": list(self.slow_callback_events)[-10:],
            }


def _client_api_row(client_name, st):
    """per-client API stats 含 latency 分位."""
    total = st["success"] + st["errors"]
    row = {
        "client": client_name,
        "success": st["success"],
        "errors": st["errors"],
        "error_rate_pct": round(st["errors"] / total * 100, 2) if total else 0,
        "by_error_type": dict(st["by_error_type"]),
        "last_error": st["last_error"],
    }
    lats = st.get("latencies")
    if lats:
        lst = list(lats)
        pct = _percentiles(lst)
        row["latency_p50_ms"] = pct["p50"]
        row["latency_p90_ms"] = pct["p90"]
        row["latency_p99_ms"] = pct["p99"]
        row["latency_max_ms"] = round(max(lst), 1)
        row["latency_avg_ms"] = round(sum(lst) / len(lst), 1)
        row["latency_samples"] = len(lst)
    return row


def _eventloop_lag_summary(lags):
    """event loop scheduler lag 分位.

    后台 task await asyncio.sleep(0.1),实际耗时 - 100ms = lag.
    p99 > 50ms 说明 loop 经常被卡 50ms+(用户感知卡顿).
    """
    if not lags:
        return {"samples": 0}
    lst = list(lags)
    pct = _percentiles(lst)
    return {
        "samples": len(lst),
        "p50_ms": pct["p50"],
        "p90_ms": pct["p90"],
        "p99_ms": pct["p99"],
        "max_ms": round(max(lst), 1),
        "min_ms": round(min(lst), 1),
    }


def _hooks_summary(hook_map):
    out = []
    for name, lats in hook_map.items():
        if not lats: continue
        lst = list(lats)
        pct = _percentiles(lst)
        out.append({
            "hook": name, "count": len(lst),
            "p50_ms": pct["p50"], "p99_ms": pct["p99"],
            "max_ms": round(max(lst), 1),
        })
    out.sort(key=lambda r: r["p99_ms"] or 0, reverse=True)
    return out[:30]


def _top_per_token_ws(counts, uptime_s, limit=20):
    if not counts or uptime_s <= 0:
        return []
    rows = [{"token_id": tid[:24] + "...", "msg_count": c,
             "rate_per_s": round(c / uptime_s, 2)}
            for tid, c in counts.items()]
    rows.sort(key=lambda r: r["msg_count"], reverse=True)
    return rows[:limit]


def _percentiles(values, ps=(50, 90, 99)):
    if not values:
        return {f"p{p}": None for p in ps}
    sorted_v = sorted(values)
    out = {}
    for p in ps:
        idx = max(0, int(len(sorted_v) * p / 100) - 1)
        out[f"p{p}"] = round(sorted_v[idx], 2)
    return out


def _top_slow_sql(slow_map, limit=15):
    rows = []
    for key, latencies in slow_map.items():
        if not latencies:
            continue
        lst = list(latencies)
        pct = _percentiles(lst)
        rows.append({
            "sql_key": key,
            "count": len(lst),
            "p50_ms": pct["p50"],
            "p99_ms": pct["p99"],
            "max_ms": round(max(lst), 2),
            "total_ms": round(sum(lst), 1),
        })
    rows.sort(key=lambda r: r["p99_ms"] or 0, reverse=True)
    return rows[:limit]


def _endpoint_steps_summary(step_map, limit=30):
    rows = []
    for key, latencies in step_map.items():
        if not latencies:
            continue
        lst = list(latencies)
        pct = _percentiles(lst)
        rows.append({
            "step": key,
            "count": len(lst),
            "p50_ms": pct["p50"],
            "p99_ms": pct["p99"],
            "max_ms": round(max(lst), 2),
        })
    rows.sort(key=lambda r: r["p99_ms"] or 0, reverse=True)
    return rows[:limit]


def pipeline_cpu_summary(samples_map: dict, *, window_seconds: float = 60.0) -> list[dict]:
    """聚合 pipeline_cpu_samples 成可观测 rows.

    Args:
        samples_map: ``SystemPerfMonitor.pipeline_cpu_samples`` 引用.
        window_seconds: 滚动窗口 (默认 60s) - 只统计该时间窗内的样本.

    Returns:
        list of dict, 按 share_of_total_cpu_pct 倒序:
        - name: pipeline 标识
        - calls: 窗口内调用次数
        - cpu_total_ms / cpu_avg_ms / cpu_p50_ms / cpu_p99_ms / cpu_max_ms
        - wall_total_ms / wall_avg_ms
        - cpu_efficiency: cpu_total / wall_total, [0,1], 接近 1=纯 CPU, 接近 0=IO
        - share_of_total_cpu_pct: 该 pipeline 占所有 pipeline CPU 总量的百分比
        - calls_per_second: 调用频率
    """
    now = time.time()
    cutoff = now - window_seconds
    aggregates: list[dict] = []
    grand_total_cpu_ms = 0.0
    for name, samples in samples_map.items():
        # 过滤窗口内样本 (samples 是 deque[(ts, cpu_ms, wall_ms)])
        recent = [(c, w) for ts, c, w in samples if ts >= cutoff]
        if not recent:
            continue
        cpu_list = [c for c, _ in recent]
        wall_list = [w for _, w in recent]
        cpu_total = sum(cpu_list)
        wall_total = sum(wall_list)
        grand_total_cpu_ms += cpu_total
        pcts = _percentiles(cpu_list, ps=(50, 90, 99))
        aggregates.append({
            "name": name,
            "calls": len(recent),
            "cpu_total_ms": round(cpu_total, 3),
            "cpu_avg_ms": round(cpu_total / len(recent), 3),
            "cpu_p50_ms": pcts.get("p50"),
            "cpu_p90_ms": pcts.get("p90"),
            "cpu_p99_ms": pcts.get("p99"),
            "cpu_max_ms": round(max(cpu_list), 3),
            "wall_total_ms": round(wall_total, 3),
            "wall_avg_ms": round(wall_total / len(recent), 3),
            "cpu_efficiency": round(cpu_total / wall_total, 4) if wall_total > 0 else None,
            "calls_per_second": round(len(recent) / window_seconds, 2),
        })
    # 算 share_of_total_cpu_pct (跨所有 pipeline)
    for row in aggregates:
        row["share_of_total_cpu_pct"] = (
            round(row["cpu_total_ms"] / grand_total_cpu_ms * 100, 2)
            if grand_total_cpu_ms > 0 else 0.0
        )
    aggregates.sort(key=lambda r: r["share_of_total_cpu_pct"], reverse=True)
    return aggregates


def pipeline_step_summary(
    step_samples_map: dict,
    *,
    window_seconds: float = 60.0,
) -> dict[str, list[dict]]:
    """按 pipeline 分组聚合 step 耗时.

    Returns:
        dict[pipeline_name, list of step rows]:
        每个 step 含 calls / cpu_avg_ms / cpu_p50_ms / cpu_p99_ms / wall_avg_ms /
        share_of_pipeline_pct (该 step cpu 占 pipeline 所有 step 的百分比).
    """
    now = time.time()
    cutoff = now - window_seconds
    # 先按 pipeline 分组累积
    by_pipeline: dict[str, list[tuple[str, list[float], list[float]]]] = defaultdict(list)
    for (pipeline, step), samples in step_samples_map.items():
        recent = [(c, w) for ts, c, w in samples if ts >= cutoff]
        if not recent:
            continue
        cpu_list = [c for c, _ in recent]
        wall_list = [w for _, w in recent]
        by_pipeline[pipeline].append((step, cpu_list, wall_list))

    result: dict[str, list[dict]] = {}
    for pipeline, steps in by_pipeline.items():
        pipeline_cpu_total = sum(sum(cpu_list) for _, cpu_list, _ in steps)
        rows = []
        for step, cpu_list, wall_list in steps:
            cpu_total = sum(cpu_list)
            wall_total = sum(wall_list)
            pcts = _percentiles(cpu_list, ps=(50, 90, 99))
            rows.append({
                "name": step,
                "calls": len(cpu_list),
                "cpu_total_ms": round(cpu_total, 3),
                "cpu_avg_ms": round(cpu_total / len(cpu_list), 3),
                "cpu_p50_ms": pcts.get("p50"),
                "cpu_p90_ms": pcts.get("p90"),
                "cpu_p99_ms": pcts.get("p99"),
                "wall_avg_ms": round(wall_total / len(wall_list), 3),
                "share_of_pipeline_pct": (
                    round(cpu_total / pipeline_cpu_total * 100, 2)
                    if pipeline_cpu_total > 0 else 0.0
                ),
            })
        rows.sort(key=lambda r: r["share_of_pipeline_pct"], reverse=True)
        result[pipeline] = rows
    return result


def _normalize_sql_key(statement: str) -> str:
    """把 SQL 归一成稳定 key: 取动作 + FROM/INTO/UPDATE 后第一个表名。

    避免 bind 参数变化产生几千个不同 key；max 100 char 防长 statement 撑爆。
    """
    s = " ".join(statement.split())[:400].upper()
    # 取首动词
    verb = s.split(" ", 1)[0] if s else "?"
    # 找表名
    table = "?"
    for kw in (" FROM ", " INTO ", " UPDATE ", " JOIN "):
        idx = s.find(kw)
        if idx >= 0:
            tail = s[idx + len(kw):].split()
            if tail:
                table = tail[0].strip('"').strip("'").lower()
                break
    return f"{verb} {table}"[:80]


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m}m"
