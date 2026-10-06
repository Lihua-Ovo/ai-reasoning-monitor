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
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests

try:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    console = None

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
        alert = f" [⚠️ {latest_entry.get('alert_reason')}]" if latest_entry.get("is_downgraded") else " [OK]"
        print(f"[{latest_entry['time']}] {latest_entry['tool']} | 模型:{latest_entry['model']} | "
              f"请求:{latest_entry['req_effort']} | 创建回显:{latest_entry['create_effort']} | "
              f"最终回显:{latest_entry['final_effort']}{alert}")
        return

    table = Table(title="🔍 AI 出口思考等级监控 (AI Reasoning Monitor)", show_header=True, header_style="bold cyan")
    table.add_column("时间", width=10, style="dim")
    table.add_column("来源工具", width=13, style="bold")
    table.add_column("模型", width=17, style="magenta")
    table.add_column("请求", width=8, justify="center")
    table.add_column("首包回显", width=9, justify="center")
    table.add_column("最终回显", width=9, justify="center")
    table.add_column("思考Tokens", width=11, justify="right")
    table.add_column("预测等级", width=9, justify="center")
    table.add_column("状态判定", width=14)

    # Show last 10 entries in terminal
    with LOGS_LOCK:
        display_items = LOGS[:8]

    for item in reversed(display_items):
        is_alert = item.get("is_downgraded")
        status_text = Text("⚠️ " + item.get("alert_reason", "降级"), style="bold red") if is_alert else Text("✓ 匹配", style="green")

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
            color_effort(item.get("predicted_effort", "-")),
            status_text
        )

    console.clear()
    console.print(table)
    console.print("[dim]Web 监控面板: [link=http://127.0.0.1:5050]http://127.0.0.1:5050[/link] | 按 Ctrl+C 停止[/dim]\n")


def detect_tool_name(headers, path, body):
    ua = headers.get("User-Agent", "").lower()
    if "claude" in ua or "anthropic" in ua or "/messages" in path:
        return "Claude Code"
    if "antigravity" in ua or "gemini" in ua or "google" in ua:
        return "Antigravity IDE"
    if "codex" in ua or "copilot" in ua or "openai" in ua:
        return "Codex"
    if "cursor" in ua:
        return "Cursor"
def normalize_effort_score(effort_val, tokens=0):
    """
    量化评分对齐 5 档思考等级（完全基于服务端真实返回参数，不基于 Token 数量脑补）：
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

def estimate_predicted_effort(tokens):
    """
    基于 Artificial Analysis Intelligence Index v4.3 权威测试基准根据 Token 消耗估算推理档位：
    - Low:    约 938 tok  (范围: 0 < tokens < 2000)
    - Medium: 约 3k tok   (范围: 2000 <= tokens < 4000)
    - High:   约 5k tok   (范围: 4000 <= tokens < 7000)
    - XHigh:  约 9k tok   (范围: 7000 <= tokens < 13000)
    - Max:    约 17k+ tok (范围: tokens >= 13000)
    """
    if not tokens or tokens <= 0:
        return "-"
    if tokens >= 13000:
        return "Max"
    elif tokens >= 7000:
        return "XHigh"
    elif tokens >= 4000:
        return "High"
    elif tokens >= 2000:
        return "Medium"
    else:
        return "Low"


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

    def determine_upstream(self, path):
        # 1. Custom header override
        custom_upstream = self.headers.get("X-Target-Upstream")
        if custom_upstream:
            return custom_upstream.rstrip("/")

        # 2. Path-based routing
        if "/messages" in path:
            return DEFAULT_UPSTREAMS["anthropic"]
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

        # Prepare upstream request
        upstream_base = self.determine_upstream(self.path)
        target_url = f"{upstream_base}{self.path}"

        headers = {k: v for k, v in self.headers.items() if k.lower() not in ["host", "content-length"]}
        parsed_target = urlparse(target_url)
        headers["Host"] = parsed_target.netloc

        create_effort = "-"
        final_effort = "-"
        reasoning_tokens = 0
        create_echo_raw = {}
        final_echo_raw = {}
        first_chunk_inspected = False
        resp_model = model_req

        try:
            resp = requests.request(
                method=method,
                url=target_url,
                headers=headers,
                data=body_bytes,
                stream=True,
                timeout=180
            )

            # Check upstream headers for reasoning or model metadata
            for h_key, h_val in resp.headers.items():
                k_lower = h_key.lower()
                if "reasoning-effort" in k_lower or "reasoning_effort" in k_lower:
                    create_effort = h_val
                if "openai-model" in k_lower or "model" in k_lower:
                    resp_model = h_val

            # Forward response headers to client
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in ["content-length", "transfer-encoding", "content-encoding"]:
                    self.send_header(k, v)
            self.end_headers()

            # Stream response body to client while inspecting chunks
            is_sse = "text/event-stream" in resp.headers.get("Content-Type", "")

            if is_sse:
                sse_buffer = ""
                for chunk in resp.iter_content(chunk_size=512):
                    if not chunk:
                        continue
                    # Immediately forward to client without stalling
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
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

                                if "model" in chunk_json:
                                    resp_model = chunk_json["model"]

                    except Exception:
                        pass
            else:
                # Non-streaming response
                full_body = b""
                for chunk in resp.iter_content(chunk_size=4096):
                    if not chunk: continue
                    full_body += chunk
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except Exception:
                        break

                try:
                    resp_json = json.loads(full_body.decode("utf-8", errors="ignore"))
                    create_echo_raw = resp_json
                    final_echo_raw = resp_json
                    if "model" in resp_json: resp_model = resp_json["model"]
                    if "reasoning_effort" in resp_json:
                        create_effort = str(resp_json["reasoning_effort"])
                        final_effort = str(resp_json["reasoning_effort"])
                    if "usage" in resp_json and "completion_tokens_details" in resp_json["usage"]:
                        reasoning_tokens = resp_json["usage"]["completion_tokens_details"].get("reasoning_tokens", 0)
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

        # 降级判定：仅在服务端明确返回了更低的思考等级，或请求思考但 0 token 思考失效时判定
        req_score = normalize_effort_score(req_effort)
        create_score = normalize_effort_score(create_effort) if create_effort != "-" else req_score
        final_score = normalize_effort_score(final_effort) if final_effort != "-" else create_score

        if req_score > 0:
            # 1. 首包明确回显了更低等级
            if create_effort != "-" and create_score < req_score:
                is_downgraded = True
                alert_reasons.append(f"首包回显降级 ({req_effort} -> {create_effort})")
            # 2. 尾包明确回显了更低等级
            elif final_effort != "-" and final_score < req_score:
                is_downgraded = True
                alert_reasons.append(f"最终回显降级 ({req_effort} -> {final_effort})")
            # 3. 请求了思考，但最终未产生任何思考 Token 且无回显确认（思考被剥夺）
            elif reasoning_tokens == 0 and create_effort in ["-", "none", "off", "0"] and final_effort in ["-", "none", "off", "0"]:
                is_downgraded = True
                alert_reasons.append(f"未产生思考Token ({req_effort} 思考失效)")

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
            "predicted_effort": estimate_predicted_effort(reasoning_tokens),
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


def start_codex_config_auto_sync():
    def _watcher():
        codex_toml = os.path.expanduser(r"~\.codex\config.toml")
        while True:
            try:
                if os.path.exists(codex_toml):
                    with open(codex_toml, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    if "127.0.0.1:15721/v1" in content:
                        new_content = content.replace("127.0.0.1:15721/v1", "127.0.0.1:5050/v1")
                        with open(codex_toml, "w", encoding="utf-8") as f:
                            f.write(new_content)
                        if HAS_RICH and console:
                            console.print(f"[bold cyan]⚡ 自动守护生效：[/bold cyan]检测到 CCSwitch 切换了 Codex 配置，已自动重定向至 5050 监控器！")
                        else:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] 自动守护：已自动将 Codex 端口重定向至 5050！")
            except Exception:
                pass
            time.sleep(1)

    t = threading.Thread(target=_watcher, daemon=True)
    t.start()

class ThreadedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

def run_server(port=5050):
    start_codex_config_auto_sync()
    server = ThreadedHTTPServer(("0.0.0.0", port), MonitorProxyHandler)
    if HAS_RICH:
        console.print(f"[bold green]✓ AI 模型与思考等级监控服务已启动！[/bold green]")
        console.print(f"[cyan]• Web 仪表盘:[/cyan] [bold underline]http://127.0.0.1:{port}[/bold underline]")
        console.print(f"[cyan]• 代理监听端口:[/cyan] [bold]127.0.0.1:{port}[/bold]")
        console.print(f"[dim]• 转发上游: OpenAI({DEFAULT_UPSTREAMS['openai']}), Claude({DEFAULT_UPSTREAMS['anthropic']})[/dim]\n")
    else:
        print(f"Server started on http://127.0.0.1:{port}")

    print_rich_table()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        server.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
    run_server(port)
