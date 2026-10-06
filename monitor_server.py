#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 客户端出口真实性与思考等级监控器 (AI Model & Reasoning Effort Monitor)
支持: Antigravity IDE, Codex, Claude Code, Cursor 等
功能:
- 实时捕获请求的模型与思考等级 (reasoning_effort: xhigh/high/low/thinking budget)
- 捕获服务端流式首包 (创建回显) 与尾包/Usage (最终回显)
- 自动检测服务商/中转是否“降智” (偷调思考等级或偷换模型)
- 提供可视化 Web 仪表盘 (与实测截图一致) + 终端 Rich 实时表格
"""

import sys
import os
import json
import time
import queue
import threading
import sqlite3
import re
import subprocess
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests

# Windows 终端控制台环境安全初始化：强制切换 UTF-8 代码页并启用虚拟终端 (VT100)
# 彻底防止双击 exe 时因 Windows 默认 GBK 编码无法输出特殊符号导致 UnicodeEncodeError 闪退
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
        STD_OUTPUT_HANDLE = -11
        h_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(h_out, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass

    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    console = Console(safe_box=True)
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    console = None

def free_port(port):
    """自动释放被旧监控器进程占用的端口，防止 WinError 10048 闪退"""
    if sys.platform != "win32":
        return
    try:
        my_pid = os.getpid()
        res = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, errors="ignore")
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                if parts[1].endswith(f":{port}"):
                    target_pid = int(parts[4])
                    if target_pid != my_pid and target_pid != 0:
                        subprocess.run(["taskkill", "/F", "/PID", str(target_pid)], capture_output=True)
        time.sleep(0.5)
    except Exception:
        pass

# 路径常量 (cc-switch 与各客户端配置文件)
CC_SWITCH_SETTINGS = os.path.expanduser(r"~\.cc-switch\settings.json")
CC_SWITCH_DB = os.path.expanduser(r"~\.cc-switch\cc-switch.db")
CLAUDE_DESKTOP_PROFILE = os.path.expanduser(r"~\AppData\Local\Claude-3p\configLibrary\00000000-0000-4000-8000-000000157210.json")

# 缓存当前供应商信息 (TTL 2秒，防止高频 sqlite 查询)
_PROVIDER_CACHE = {}
_PROVIDER_CACHE_TIME = {}

def get_ccswitch_provider_info(app_key="ClaudeDesktop", force_refresh=False):
    """
    实时/动态读取 cc-switch 中指定客户端当前的活动供应商配置
    app_key 可选: 'ClaudeDesktop', 'Claude', 'Codex'
    返回: (provider_name, base_url, auth_token)
    """
    now = time.time()
    if not force_refresh and app_key in _PROVIDER_CACHE and (now - _PROVIDER_CACHE_TIME.get(app_key, 0) < 2):
        return _PROVIDER_CACHE[app_key]

    settings_key = f"currentProvider{app_key}"
    provider_id = None
    if os.path.exists(CC_SWITCH_SETTINGS):
        try:
            with open(CC_SWITCH_SETTINGS, "r", encoding="utf-8") as f:
                st = json.load(f)
                provider_id = st.get(settings_key)
        except Exception:
            pass

    app_type_map = {
        "ClaudeDesktop": "claude-desktop",
        "Claude": "claude",
        "Codex": "codex",
    }
    target_app_type = app_type_map.get(app_key, "claude-desktop")

    p_name, base_url, token = None, None, None
    if os.path.exists(CC_SWITCH_DB):
        try:
            conn = sqlite3.connect(f"file:{CC_SWITCH_DB}?mode=ro", uri=True)
            cur = conn.cursor()
            if provider_id:
                cur.execute("SELECT name, settings_config FROM providers WHERE id = ?", (provider_id,))
                row = cur.fetchone()
            else:
                cur.execute("SELECT name, settings_config FROM providers WHERE app_type = ? AND is_current = 1", (target_app_type,))
                row = cur.fetchone()
            conn.close()

            if row:
                p_name, cfg_str = row
                cfg = json.loads(cfg_str)
                env = cfg.get("env", {})
                base_url = env.get("ANTHROPIC_BASE_URL") or cfg.get("baseUrl")
                token = env.get("ANTHROPIC_AUTH_TOKEN") or cfg.get("apiKey")
                if not base_url and "config" in cfg:
                    m = re.search(r'base_url\s*=\s*["\']([^"\']+)["\']', cfg["config"])
                    if m:
                        base_url = m.group(1)
                    auth = cfg.get("auth", {})
                    token = auth.get("OPENAI_API_KEY")
        except Exception:
            pass

    result = (p_name or "默认供应商", (base_url or "").rstrip("/"), token or "")
    _PROVIDER_CACHE[app_key] = result
    _PROVIDER_CACHE_TIME[app_key] = now
    return result

def build_target_url(upstream_base, req_path):
    """构建安全转发 URL，自动避免 /v1/v1 路径重复问题"""
    base = upstream_base.rstrip("/")
    path = req_path
    if base.endswith("/v1") and path.startswith("/v1/"):
        base = base[:-3]
    return f"{base}{path}"

# 默认上游配置 (自动适配已运行的 cc-switch: 127.0.0.1:15721)
DEFAULT_UPSTREAMS = {
    "openai": os.environ.get("UPSTREAM_OPENAI", "http://127.0.0.1:15721"),
    "anthropic": os.environ.get("UPSTREAM_ANTHROPIC", "http://127.0.0.1:15721"),
    "gemini": os.environ.get("UPSTREAM_GEMINI", "https://generativelanguage.googleapis.com"),
}

LOGS_LOCK = threading.Lock()
LOGS = []
LOG_ID_COUNTER = 0
SSE_SUBSCRIBERS = []


BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
WEB_UI_PATH = os.path.join(BASE_DIR, "web_ui.html")

def add_log_entry(entry):
    global LOG_ID_COUNTER
    with LOGS_LOCK:
        LOG_ID_COUNTER += 1
        entry["id"] = LOG_ID_COUNTER
        LOGS.insert(0, entry)
        if len(LOGS) > 500:
            LOGS.pop()

    # Notify SSE web subscribers
    for q in list(SSE_SUBSCRIBERS):
        try:
            q.put_nowait(entry)
        except Exception:
            pass

    # Print to console
    print_rich_table(entry)

def print_rich_table(latest_entry=None):
    if not HAS_RICH:
        alert = f" [! {latest_entry.get('alert_reason')}]" if latest_entry.get("is_downgraded") else " [OK]"
        print(f"[{latest_entry['time']}] {latest_entry['tool']} | 模型:{latest_entry['model']} | "
              f"请求:{latest_entry['req_effort']} | 创建回显:{latest_entry['create_effort']} | "
              f"最终回显:{latest_entry['final_effort']}{alert}")
        return

    table = Table(title="[监控] AI 出口思考等级监控 (AI Reasoning Monitor)", show_header=True, header_style="bold cyan")
    table.add_column("时间", width=10, style="dim")
    table.add_column("来源工具", width=13, style="bold")
    table.add_column("模型", width=17, style="magenta")
    table.add_column("请求", width=8, justify="center")
    table.add_column("首包回显", width=9, justify="center")
    table.add_column("最终回显", width=9, justify="center")
    table.add_column("思考Tokens", width=11, justify="right")
    table.add_column("状态判定", width=14)

    # Show last 10 entries in terminal
    with LOGS_LOCK:
        display_items = LOGS[:8]

    for item in reversed(display_items):
        is_alert = item.get("is_downgraded")
        status_text = Text("[!] " + item.get("alert_reason", "降级"), style="bold red") if is_alert else Text("[OK] 匹配", style="green")

        def color_effort(lvl):
            if not lvl or lvl == "-": return Text("-", style="dim")
            lvl_str = str(lvl)
            if "xhigh" in lvl_str.lower() or "max" in lvl_str.lower(): return Text(lvl_str, style="bold orange1")
            if "high" in lvl_str.lower(): return Text(lvl_str, style="bold yellow")
            if "medium" in lvl_str.lower(): return Text(lvl_str, style="yellow3")
            if "low" in lvl_str.lower(): return Text(lvl_str, style="cyan")
            return Text(lvl_str, style="white")

        table.add_row(
            item["time"],
            item["tool"],
            item["model"],
            color_effort(item["req_effort"]),
            color_effort(item["create_effort"]),
            color_effort(item["final_effort"]),
            f"{item['reasoning_tokens']} tok" if item.get('reasoning_tokens') else "-",
            status_text
        )

    console.clear()
    console.print(table)

    stats_rows = compute_group_stats(limit=6)
    if stats_rows:
        st = Table(title="📊 档位思考消耗统计 (已过滤空回 · 仅统计不作判定)", show_header=True, header_style="bold cyan", title_justify="left")
        st.add_column("模型", width=24, style="magenta")
        st.add_column("生效档位", width=10, justify="center")
        st.add_column("样本", width=6, justify="right")
        st.add_column("平均", width=13, justify="right")
        st.add_column("中位数", width=13, justify="right")
        st.add_column("范围", width=19, justify="right")
        for r in stats_rows:
            st.add_row(
                r["model"],
                r["level"],
                str(r["count"]),
                f"{r['avg']:,.0f} {r['unit']}",
                f"{r['median']:,.0f} {r['unit']}",
                f"{r['min']:,.0f} ~ {r['max']:,.0f}",
            )
        console.print(st)

    console.print("[dim]Web 监控面板: [link=http://127.0.0.1:5050]http://127.0.0.1:5050[/link] | 按 Ctrl+C 停止[/dim]\n")


def detect_tool_name(headers, path, body):
    ua = headers.get("User-Agent", "").lower()
    if "desktop" in ua or "claude.exe" in ua or "claude-3p" in ua:
        return "Claude Desktop"
    if "claude-code" in ua:
        return "Claude Code"
    if "antigravity" in ua or "gemini" in ua or "google" in ua:
        return "Antigravity IDE"
    if "codex" in ua or "copilot" in ua or "openai" in ua or "/responses" in path:
        return "Codex"
    if "cursor" in ua:
        return "Cursor"
    if "/messages" in path:
        # 检测请求特征或模型（例如 Claude Desktop 客户端特征）
        if isinstance(body, dict):
            m = str(body.get("model", "")).lower()
            sys_prompt = str(body.get("system", ""))
            if "fable" in m or "session titles" in sys_prompt:
                return "Claude Desktop"
        if "code" in ua:
            return "Claude Code"
        return "Claude Desktop"
    return "AI-Client"
def normalize_effort_score(effort_val):
    """
    量化评分对齐 5 档思考等级（完全基于服务端真实返回参数，不基于 Token 数量猜测）：
    5档: xhigh, max, extreme, ultra, "5"
    4档: high, deep, "4"
    3档: medium, med, balanced, "3"
    2档: low, "2"
    1档: minimal, min, very-low, lowest, "1"
    0档: none, off, disabled, "0", "-"
    """
    if effort_val is None:
        return 0
    s = str(effort_val).lower().strip()
    if not s or s in ["-", "none", "off", "disabled", "false", "0"]:
        return 0
    if s in ["5", "xhigh", "max", "extreme", "extra-high", "extra_high", "ultra"]:
        return 5
    if s in ["4", "high", "deep"]:
        return 4
    if s in ["3", "medium", "med", "balanced"]:
        return 3
    if s in ["2", "low"]:
        return 2
    if s in ["1", "minimal", "min", "very-low", "lowest"]:
        return 1

    if "xhigh" in s or "max" in s or "extreme" in s or "ultra" in s:
        return 5
    if "high" in s or "deep" in s:
        return 4
    if "medium" in s or "med" in s or "balanced" in s:
        return 3
    if "low" in s:
        return 2
    if "min" in s:
        return 1

    return 0

LEVEL_LABELS = {5: "XHigh", 4: "High", 3: "Medium", 2: "Low", 1: "Minimal"}


def effective_level_label(entry):
    """生效档位：优先取服务端回显 (final -> create)，无回显时退回请求档位；无法识别时保留原始字符串。"""
    for key in ("final_effort", "create_effort", "req_effort"):
        raw = str(entry.get(key) or "-")
        score = normalize_effort_score(raw)
        if score > 0:
            return LEVEL_LABELS[score]
    raw = str(entry.get("req_effort") or "-")
    return raw if raw != "-" else "未标注"


def compute_group_stats(limit=None):
    """按 (模型, 生效档位) 统计思考消耗；过滤空回。Token 数仅作统计参考，不用于等级判定。"""
    groups = {}
    with LOGS_LOCK:
        items = list(LOGS)
    for it in items:
        toks = it.get("reasoning_tokens") or 0
        chars = it.get("thinking_chars") or 0
        if toks <= 0 and chars <= 0:
            continue
        key = (it.get("model") or "-", effective_level_label(it))
        g = groups.setdefault(key, {"toks": [], "chars": []})
        if toks > 0:
            g["toks"].append(toks)
        else:
            g["chars"].append(chars)

    rows = []
    for (model, level), g in groups.items():
        vals = sorted(g["toks"] or g["chars"])
        unit = "tok" if g["toks"] else "字"
        n = len(vals)
        median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        rows.append({
            "model": model, "level": level, "unit": unit, "count": n,
            "avg": sum(vals) / n, "median": median, "min": vals[0], "max": vals[-1],
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows[:limit] if limit else rows


def parse_request_payload(body_bytes, path):
    data = {}
    if not body_bytes:
        return "-", "-", {}
    try:
        data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
    except Exception:
        return "-", "-", {}

    # Extract model
    model = data.get("model") or data.get("modelName") or "-"

    # Extract request thinking / reasoning effort
    req_effort = "-"
    if "reasoning_effort" in data:
        req_effort = str(data["reasoning_effort"])
    elif "reasoning" in data:
        r_conf = data["reasoning"]
        if isinstance(r_conf, dict):
            req_effort = str(r_conf.get("effort") or r_conf.get("level") or "enabled")
        else:
            req_effort = str(r_conf)
    elif "thinking" in data:
        thinking_conf = data["thinking"]
        if isinstance(thinking_conf, dict):
            if thinking_conf.get("type") == "enabled":
                budget = thinking_conf.get("budget_tokens")
                if budget:
                    if budget >= 16000: req_effort = f"xhigh({budget})"
                    elif budget >= 8000: req_effort = f"high({budget})"
                    elif budget >= 2000: req_effort = f"medium({budget})"
                    else: req_effort = f"low({budget})"
                else:
                    req_effort = "enabled"
            else:
                req_effort = "disabled"
    elif "generationConfig" in data and "thinkingConfig" in data["generationConfig"]:
        tcfg = data["generationConfig"]["thinkingConfig"]
        budget = tcfg.get("thinkingBudget")
        req_effort = f"budget({budget})" if budget else "gemini-think"

    return model, req_effort, data


class MonitorProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default noisy access logs
        return

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ["/", "/index.html", "/dashboard"]:
            self.serve_web_ui()
            return
        elif path == "/api/logs":
            self.serve_logs_json()
            return
        elif path == "/api/events":
            self.serve_sse_events()
            return
        elif path == "/api/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(DEFAULT_UPSTREAMS).encode())
            return
        elif path == "/api/inject_sample_data":
            samples = [
                {"time": "17:36:09", "tool": "Codex", "model": "gpt-6-astra", "req_effort": "xhigh", "create_effort": "low", "final_effort": "low", "reasoning_tokens": 128, "path": "/v1/chat/completions", "duration_ms": 1420, "is_downgraded": True, "alert_reason": "思考等级降级 (xhigh -> low)"},
                {"time": "17:35:57", "tool": "Codex", "model": "gpt-6-astra", "req_effort": "xhigh", "create_effort": "low", "final_effort": "low", "reasoning_tokens": 128, "path": "/v1/chat/completions", "duration_ms": 1380, "is_downgraded": True, "alert_reason": "思考等级降级 (xhigh -> low)"},
                {"time": "17:35:38", "tool": "Claude Code", "model": "claude-3-7-sonnet", "req_effort": "xhigh", "create_effort": "low", "final_effort": "low", "reasoning_tokens": 160, "path": "/v1/messages", "duration_ms": 1510, "is_downgraded": True, "alert_reason": "思考等级降级 (xhigh -> low)"},
                {"time": "17:35:20", "tool": "Antigravity IDE", "model": "gpt-6-astra", "req_effort": "xhigh", "create_effort": "low", "final_effort": "low", "reasoning_tokens": 112, "path": "/v1/chat/completions", "duration_ms": 1650, "is_downgraded": True, "alert_reason": "思考等级降级 (xhigh -> low)"},
                {"time": "17:34:05", "tool": "Antigravity IDE", "model": "gemini-2.5-pro", "req_effort": "high", "create_effort": "high", "final_effort": "high", "reasoning_tokens": 4096, "path": "/v1beta/models/...", "duration_ms": 2890, "is_downgraded": False, "alert_reason": None}
            ]
            for s in reversed(samples):
                add_log_entry(s)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"injected"}')
            return

        # Fallback to reverse proxy forwarding for GET
        self.forward_request("GET")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/clear":
            with LOGS_LOCK:
                LOGS.clear()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            print_rich_table()
            return
        elif path == "/api/config":
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            try:
                new_cfg = json.loads(body_bytes.decode("utf-8"))
                for k, v in new_cfg.items():
                    if k in DEFAULT_UPSTREAMS and v:
                        DEFAULT_UPSTREAMS[k] = v.rstrip("/")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "updated", "config": DEFAULT_UPSTREAMS}).encode())
                print(f"[配置更新] 当前上游目标: {DEFAULT_UPSTREAMS}")
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        elif path == "/api/internal_log":
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            try:
                item = json.loads(body_bytes.decode("utf-8"))
                add_log_entry(item)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"logged"}')
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        elif path == "/api/inject_sample_data":
            # Inject sample rows matching user screenshot for testing
            samples = [
                {"time": "17:36:09", "tool": "Codex", "model": "gpt-6-astra", "req_effort": "xhigh", "create_effort": "low", "final_effort": "low", "reasoning_tokens": 128, "path": "/v1/chat/completions", "duration_ms": 1420, "is_downgraded": True, "alert_reason": "思考等级降级 (xhigh -> low)"},
                {"time": "17:35:57", "tool": "Codex", "model": "gpt-6-astra", "req_effort": "xhigh", "create_effort": "low", "final_effort": "low", "reasoning_tokens": 128, "path": "/v1/chat/completions", "duration_ms": 1380, "is_downgraded": True, "alert_reason": "思考等级降级 (xhigh -> low)"},
                {"time": "17:35:38", "tool": "Claude Code", "model": "claude-3-7-sonnet", "req_effort": "xhigh", "create_effort": "low", "final_effort": "low", "reasoning_tokens": 160, "path": "/v1/messages", "duration_ms": 1510, "is_downgraded": True, "alert_reason": "思考等级降级 (xhigh -> low)"},
                {"time": "17:35:20", "tool": "Antigravity IDE", "model": "gpt-6-astra", "req_effort": "xhigh", "create_effort": "low", "final_effort": "low", "reasoning_tokens": 112, "path": "/v1/chat/completions", "duration_ms": 1650, "is_downgraded": True, "alert_reason": "思考等级降级 (xhigh -> low)"},
                {"time": "17:34:05", "tool": "Antigravity IDE", "model": "gemini-2.5-pro", "req_effort": "high", "create_effort": "high", "final_effort": "high", "reasoning_tokens": 4096, "path": "/v1beta/models/...", "duration_ms": 2890, "is_downgraded": False, "alert_reason": None}
            ]
            for s in reversed(samples):
                add_log_entry(s)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"injected"}')
            return

        self.forward_request("POST")

    def serve_web_ui(self):
        if not os.path.exists(WEB_UI_PATH):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"web_ui.html not found")
            return

        with open(WEB_UI_PATH, "rb") as f:
            content = f.read()

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(content)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def serve_logs_json(self):
        with LOGS_LOCK:
            data = json.dumps(LOGS).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def serve_sse_events(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = queue.Queue()
            SSE_SUBSCRIBERS.append(q)

            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
            while True:
                try:
                    entry = q.get(timeout=5)
                    payload = f"data: {json.dumps(entry)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                except queue.Empty:
                    # Keep-alive heartbeat ping
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, Exception):
            pass
        finally:
            if 'q' in locals() and q in SSE_SUBSCRIBERS:
                SSE_SUBSCRIBERS.remove(q)

    def determine_upstream(self, path, tool_name="AI-Client"):
        # 1. Custom header override
        custom_upstream = self.headers.get("X-Target-Upstream")
        if custom_upstream:
            return custom_upstream.rstrip("/")

        # 2. Tool-based / Path-based dynamic resolution
        if tool_name == "Claude Desktop" or ("/messages" in path and "codex" not in tool_name.lower()):
            # 自动读取 cc-switch 中 Claude Desktop 当前激活的真实供应商
            p_name, base_url, _ = get_ccswitch_provider_info("ClaudeDesktop")
            if base_url:
                return base_url
            return DEFAULT_UPSTREAMS.get("anthropic", "https://anyrouter.top")
        elif ":generateContent" in path or ":streamGenerateContent" in path:
            return DEFAULT_UPSTREAMS["gemini"]
        else:
            return DEFAULT_UPSTREAMS["openai"]

    def forward_request(self, method):
        start_time = time.time()
        req_time_str = datetime.now().strftime("%H:%M:%S")

        # Read client body
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""

        # Extract request info
        model_req, req_effort, req_json = parse_request_payload(body_bytes, self.path)
        tool_name = detect_tool_name(self.headers, self.path, req_json)

        # Prepare upstream request with dynamic resolution
        upstream_base = self.determine_upstream(self.path, tool_name)
        target_url = build_target_url(upstream_base, self.path)

        HOP_BY_HOP_HEADERS = {
            "host", "content-length", "connection", "keep-alive",
            "proxy-authenticate", "proxy-authorization", "te", "trailers",
            "transfer-encoding", "upgrade"
        }
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
        parsed_target = urlparse(target_url)
        headers["Host"] = parsed_target.netloc

        # 对于 Claude Desktop，如果请求未带鉴权头，自动补全 cc-switch 当前供应商的 API Key
        if tool_name == "Claude Desktop" and not headers.get("x-api-key") and not headers.get("authorization"):
            _, _, p_token = get_ccswitch_provider_info("ClaudeDesktop")
            if p_token:
                headers["x-api-key"] = p_token

        create_effort = "-"
        final_effort = "-"
        reasoning_tokens = 0
        thinking_chars = 0
        create_echo_raw = {}
        final_echo_raw = {}
        first_chunk_inspected = False
        resp_model = model_req

        try:
            resp = None
            for attempt in range(2):
                try:
                    resp = requests.request(
                        method=method,
                        url=target_url,
                        headers=headers,
                        data=body_bytes,
                        stream=True,
                        timeout=180
                    )
                    break
                except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError):
                    if attempt == 0:
                        time.sleep(0.3)
                        continue
                    raise

            # Check upstream headers for reasoning or model metadata
            for h_key, h_val in resp.headers.items():
                k_lower = h_key.lower()
                if "reasoning-effort" in k_lower or "reasoning_effort" in k_lower:
                    create_effort = h_val
                if "openai-model" in k_lower or "model" in k_lower:
                    resp_model = h_val

            is_sse = "text/event-stream" in resp.headers.get("Content-Type", "")

            if is_sse:
                # Forward response headers for streaming SSE
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() not in ["content-length", "transfer-encoding", "content-encoding", "connection"]:
                        self.send_header(k, v)
                self.end_headers()

                sse_buffer = ""
                for chunk in resp.iter_content(chunk_size=512):
                    if not chunk:
                        continue
                    # Immediately forward to client without stalling
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        break

                    # Inspect SSE chunk for reasoning effort & tokens
                    try:
                        text = chunk.decode("utf-8", errors="ignore")
                        sse_buffer += text
                        lines = sse_buffer.split("\n")
                        sse_buffer = lines.pop() # Keep unfinished line

                        for line in lines:
                            line = line.strip()
                            if line.startswith("data:") and not line.startswith("data: [DONE]"):
                                data_str = line[5:].strip()
                                try:
                                    chunk_json = json.loads(data_str)
                                except Exception:
                                    continue

                                # Support responses wire API structure
                                if "response" in chunk_json and isinstance(chunk_json["response"], dict):
                                    resp_obj = chunk_json["response"]
                                    if "model" in resp_obj:
                                        resp_model = resp_obj["model"]
                                    if "reasoning" in resp_obj and isinstance(resp_obj["reasoning"], dict):
                                        create_effort = str(resp_obj["reasoning"].get("effort") or "enabled")
                                    if "usage" in resp_obj:
                                        chunk_json["usage"] = resp_obj["usage"]

                                # 1. First data chunk (创建回显)
                                if not first_chunk_inspected:
                                    first_chunk_inspected = True
                                    create_echo_raw = chunk_json
                                    if "model" in chunk_json:
                                        resp_model = chunk_json["model"]
                                    if "reasoning_effort" in chunk_json:
                                        create_effort = str(chunk_json["reasoning_effort"])
                                    elif "choices" in chunk_json and len(chunk_json["choices"]) > 0:
                                        delta = chunk_json["choices"][0].get("delta", {})
                                        if "reasoning_effort" in delta:
                                            create_effort = str(delta["reasoning_effort"])
                                        elif "reasoning_content" in delta:
                                            create_effort = req_effort if req_effort != "-" else "active"

                                    # Anthropic message_start
                                    if chunk_json.get("type") == "message_start":
                                        msg = chunk_json.get("message", {})
                                        if "model" in msg: resp_model = msg["model"]
                                        if msg.get("thinking"):
                                            create_effort = "thinking-on"

                                # Anthropic 流式思考内容累计：thinking_delta 字符数（统计实际思考量）
                                if chunk_json.get("type") == "content_block_delta":
                                    delta_obj = chunk_json.get("delta", {})
                                    if isinstance(delta_obj, dict) and delta_obj.get("type") == "thinking_delta":
                                        thinking_chars += len(delta_obj.get("thinking") or "")

                                # 2. Last / Usage chunk (最终回显)
                                if "usage" in chunk_json:
                                    final_echo_raw = chunk_json
                                    usage = chunk_json["usage"]
                                    if "completion_tokens_details" in usage:
                                        dtls = usage["completion_tokens_details"]
                                        rtoks = dtls.get("reasoning_tokens", 0)
                                        if rtoks: reasoning_tokens = rtoks
                                    elif "output_tokens_details" in usage:
                                        dtls = usage["output_tokens_details"]
                                        rtoks = dtls.get("reasoning_tokens", 0)
                                        if rtoks: reasoning_tokens = rtoks
                                    elif "thinking_tokens" in usage:
                                        reasoning_tokens = usage["thinking_tokens"]
                                    
                                    if "reasoning_effort" in chunk_json:
                                        final_effort = str(chunk_json["reasoning_effort"])

                                # Gemini 流式: usageMetadata.thoughtsTokenCount
                                if "usageMetadata" in chunk_json:
                                    final_echo_raw = chunk_json
                                    g_toks = chunk_json["usageMetadata"].get("thoughtsTokenCount") or 0
                                    if g_toks:
                                        reasoning_tokens = g_toks

                                if "model" in chunk_json:
                                    resp_model = chunk_json["model"]

                    except Exception:
                        pass
            else:
                # Non-streaming response: read full body first to provide accurate Content-Length
                full_body = resp.content
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() not in ["content-length", "transfer-encoding", "content-encoding"]:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(full_body)))
                self.end_headers()

                try:
                    self.wfile.write(full_body)
                    self.wfile.flush()
                except Exception:
                    pass

                try:
                    resp_json = json.loads(full_body.decode("utf-8", errors="ignore"))
                    create_echo_raw = resp_json
                    final_echo_raw = resp_json
                    if "model" in resp_json: resp_model = resp_json["model"]
                    if "reasoning_effort" in resp_json:
                        create_effort = str(resp_json["reasoning_effort"])
                        final_effort = str(resp_json["reasoning_effort"])
                    if "usage" in resp_json:
                        u = resp_json["usage"]
                        dtls = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
                        rtoks = dtls.get("reasoning_tokens", 0) or u.get("thinking_tokens", 0) or 0
                        if rtoks:
                            reasoning_tokens = rtoks
                    if "usageMetadata" in resp_json:
                        g_toks = resp_json["usageMetadata"].get("thoughtsTokenCount") or 0
                        if g_toks:
                            reasoning_tokens = g_toks
                    if isinstance(resp_json.get("content"), list):
                        for blk in resp_json["content"]:
                            if isinstance(blk, dict) and blk.get("type") == "thinking":
                                thinking_chars += len(blk.get("thinking") or "")
                except Exception:
                    pass

        except Exception as e:
            self.send_response(502)
            self.end_headers()
            err_msg = json.dumps({"error": f"Upstream proxy error: {str(e)}"}).encode("utf-8")
            self.wfile.write(err_msg)
            return

        # 实事求是：服务端未返回思考等级字段时，忠实保留为 "-" (空)，绝不根据 Token 数量臆测 minimal
        duration_ms = int((time.time() - start_time) * 1000)
        is_downgraded = False
        alert_reasons = []

        # 降级判定：仅比对服务端回显的思考等级与请求等级（思考 Token 数是动态的，不作判定依据）
        req_score = normalize_effort_score(req_effort)
        create_score = normalize_effort_score(create_effort) if create_effort != "-" else req_score
        final_score = normalize_effort_score(final_effort) if final_effort != "-" else create_score

        if req_score > 0:
            # 只比对「服务端回显等级」与「请求等级」；思考 Token 数是动态的，仅作统计，不参与判定
            is_create_off = str(create_effort).lower().strip() in ["none", "off", "disabled", "false", "0"]
            is_final_off = str(final_effort).lower().strip() in ["none", "off", "disabled", "false", "0"]

            # 1. 首包明确回显了更低等级或关闭思考
            if create_effort != "-" and (is_create_off or (0 < create_score < req_score)):
                is_downgraded = True
                alert_reasons.append(f"首包回显降级 ({req_effort} -> {create_effort})")
            # 2. 尾包明确回显了更低等级或关闭思考
            elif final_effort != "-" and (is_final_off or (0 < final_score < req_score)):
                is_downgraded = True
                alert_reasons.append(f"最终回显降级 ({req_effort} -> {final_effort})")

        # 2. Check model substitution (e.g. requested gpt-6-astra or sonnet-3-7, got 4o-mini)
        if model_req != "-" and resp_model != "-" and model_req.lower() not in resp_model.lower():
            # If completely different family
            if ("o1" in model_req or "o3" in model_req or "astra" in model_req or "opus" in model_req) and ("mini" in resp_model or "3.5" in resp_model or "flash" in resp_model):
                is_downgraded = True
                alert_reasons.append(f"模型被替换 ({model_req} -> {resp_model})")

        log_item = {
            "time": req_time_str,
            "tool": tool_name,
            "model": model_req if model_req != "-" else resp_model,
            "resp_model": resp_model,
            "req_effort": req_effort,
            "create_effort": create_effort,
            "final_effort": final_effort,
            "reasoning_tokens": reasoning_tokens,
            "thinking_chars": thinking_chars,
            "path": self.path,
            "upstream": upstream_base,
            "duration_ms": duration_ms,
            "is_downgraded": is_downgraded,
            "alert_reason": ", ".join(alert_reasons) if alert_reasons else None,
            "client_thinking_raw": req_json.get("reasoning_effort") or req_json.get("thinking") or req_json.get("generationConfig", {}).get("thinkingConfig") or req_json,
            "create_echo_raw": create_echo_raw,
            "final_echo_raw": final_echo_raw,
        }

        add_log_entry(log_item)


def start_config_auto_sync():
    """
    全自动配置同步守护线程 (Codex & Claude Desktop)
    1. Codex 守护：
       检测 ~/.codex/config.toml，若检测到 cc-switch 重置为 127.0.0.1:15721/v1，
       自动替换为 127.0.0.1:5050/v1，实现无感拦截。
    2. Claude Desktop 守护：
       检测 Claude Desktop 配置 profile (AppData/Local/Claude-3p/configLibrary/00000000-0000-4000-8000-000000157210.json)
       当 cc-switch 切换供应商并向该文件写入真实远端地址时，
       自动记录最新真实上游，并将 inferenceGatewayBaseUrl 维持重写为 http://127.0.0.1:5050，
       保证 Claude Desktop 始终直连 5050 监控器，同时上游动态跟随后台切换，零手动操作！
    """
    def _watcher():
        codex_toml = os.path.expanduser(r"~\.codex\config.toml")
        claude_profile = CLAUDE_DESKTOP_PROFILE

        while True:
            # 1. 自动同步 Codex
            try:
                if os.path.exists(codex_toml):
                    with open(codex_toml, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    if "127.0.0.1:15721/v1" in content:
                        new_content = content.replace("127.0.0.1:15721/v1", "127.0.0.1:5050/v1")
                        with open(codex_toml, "w", encoding="utf-8") as f:
                            f.write(new_content)
                        msg = "[*] 自动守护生效：检测到 cc-switch 切换了 Codex 配置，已自动重定向至 5050 监控器！"
                        if HAS_RICH and console:
                            console.print(f"[bold cyan]{msg}[/bold cyan]")
                        else:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            except Exception:
                pass

            # 2. 自动同步 Claude Desktop
            try:
                if os.path.exists(claude_profile):
                    with open(claude_profile, "r", encoding="utf-8", errors="ignore") as f:
                        p_data = json.load(f)
                    cur_gw = (p_data.get("inferenceGatewayBaseUrl") or "").strip()
                    if cur_gw and cur_gw.rstrip("/") != "http://127.0.0.1:5050":
                        new_real_upstream = cur_gw.rstrip("/")
                        p_data["inferenceGatewayBaseUrl"] = "http://127.0.0.1:5050"
                        with open(claude_profile, "w", encoding="utf-8") as f:
                            json.dump(p_data, f, indent=2, ensure_ascii=False)

                        # 强制刷新缓存，获取当前供应商名称
                        prov_name, _, _ = get_ccswitch_provider_info("ClaudeDesktop", force_refresh=True)
                        msg = f"[*] 自动守护生效：检测到 cc-switch 切换了 Claude Desktop 供应商 -> [{prov_name}] ({new_real_upstream})，已自动对接 5050 监控器！"
                        if HAS_RICH and console:
                            console.print(f"[bold cyan]{msg}[/bold cyan]")
                        else:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            except Exception:
                pass

            time.sleep(1)

    t = threading.Thread(target=_watcher, daemon=True)
    t.start()

class ThreadedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

def run_server(port=5050):
    start_config_auto_sync()

    try:
        server = ThreadedHTTPServer(("0.0.0.0", port), MonitorProxyHandler)
    except OSError as e:
        if getattr(e, "winerror", None) == 10048 or "10048" in str(e):
            msg = f"[!] 检测到端口 {port} 已被占用，正在自动释放旧进程并重试..."
            if HAS_RICH and console:
                console.print(f"[yellow]{msg}[/yellow]")
            else:
                print(msg)
            free_port(port)
            server = ThreadedHTTPServer(("0.0.0.0", port), MonitorProxyHandler)
        else:
            raise

    cd_name, cd_url, _ = get_ccswitch_provider_info("ClaudeDesktop")
    codex_name, codex_url, _ = get_ccswitch_provider_info("Codex")

    if HAS_RICH:
        console.print(f"[bold green][OK] AI 模型与思考等级监控服务已启动！[/bold green]")
        console.print(f"[cyan]- Web 仪表盘:[/cyan] [bold underline]http://127.0.0.1:{port}[/bold underline]")
        console.print(f"[cyan]- 代理监听端口:[/cyan] [bold]127.0.0.1:{port}[/bold]")
        console.print(f"[cyan]- 全自动守护状态:[/cyan] [bold green]Codex (已接管)[/bold green] | [bold green]Claude Desktop (已接管)[/bold green]")
        console.print(f"[dim]- 动态上游解析: Codex -> 127.0.0.1:15721 [{codex_name}], Claude Desktop -> {cd_url or '默认'} [{cd_name}][/dim]\n")
    else:
        print(f"[OK] Server started on http://127.0.0.1:{port}")
        print(f"Auto-sync active: Codex & Claude Desktop -> 127.0.0.1:{port}")

    print_rich_table()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        server.server_close()


if __name__ == "__main__":
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
        run_server(port)
    except Exception as e:
        import traceback
        print("\n" + "=" * 60)
        print("服务启动发生未捕获异常:")
        traceback.print_exc()
        print("=" * 60)
        try:
            input("\n按回车键退出程序 (Press Enter to exit)...")
        except Exception:
            pass

