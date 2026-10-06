# 🛡️ AI Reasoning Monitor

专为 **Claude Code** 与 **Codex** 打造、配合 **cc-switch** 使用的轻量代理工具。通过分析服务端返回值，实时判断**推理强度（思考等级）是否被中转暗中削减**。

> ⚠️ **说明**：
> 1. 本工具**不具备检测模型真伪的能力**。降级判定仅比对服务端回显的思考等级与请求等级。
> 2. 推理模型的思考量是动态的：同一档位下，简单问题可能只思考少量 Token。因此本工具**不依据 Token 数量反推等级**，思考 Token 仅作消耗统计参考。
> 3. 需配合本地 **cc-switch** 使用（专为 Claude Code 与 Codex 设计）。

---

## 📸 运行效果预览

![AI Reasoning Monitor 监控控制台](assets/dashboard.png)

---

## 🧐 它做什么？

在使用第三方中转时，常常遇到客户端请求了高思考等级，但中转实际返回缩水的问题。本工具通过本地代理（端口 5050）透明转发并核对：

1. **提取请求等级**：捕获客户端发出的思考强度参数（如 `reasoning_effort: xhigh/high` 或 thinking budget）。
2. **抓取返回回显**：从服务端流式首包（Chunk 0）和尾包/Usage 中提取实际生效的思考参数、`reasoning_tokens`（Anthropic 累计 `thinking_delta` 字符数）。
3. **档位思考消耗统计**：按「模型 × 生效档位」统计实际思考 Token 的平均值、中位数与范围（自动过滤空回），仅作消耗参考，不代表真实等级。
4. **判定降级告警**：比对两者，若服务端返回的推理强度低于请求等级，在 Web 控制台和终端立即红字告警。
5. **协同守护**：当 cc-switch 切换服务时，自动维护 Codex 端口映射，无需手动反复修改配置。

---

## 🚀 怎么用？

### 前提条件
电脑上已运行 **cc-switch**（默认监听端口 `15721`）。

### 1. 启动监控服务
双击运行根目录下的：
```bat
start_monitor.bat
```
> 自动拉起代理并打开浏览器监控面板：`http://127.0.0.1:5050`。  
> *(命令行方式：`pip install -r requirements.txt && python monitor_server.py 5050`)*

### 2. 客户端接入

- **一键绑定环境变量（最简单）**  
  双击运行 **`bind_env.bat`**，自动将 `ANTHROPIC_BASE_URL` 和 `OPENAI_BASE_URL` 指向监控器。  
  *(随时双击 **`unbind_env.bat`** 即可恢复默认直连)*

- **手动接入**  
  - **Claude Code**：设置环境变量 `ANTHROPIC_BASE_URL=http://127.0.0.1:5050`
  - **Codex**：Base URL 设为 `http://127.0.0.1:5050/v1`（工具内建守护线程，自动与 cc-switch 同步）

---

## 📦 打包单文件 Exe（免 Python 环境）

双击运行 **`build_exe.bat`**，将在 `dist\ModelMonitor.exe` 生成独立运行程序，随处双击即用。

---

## 📄 许可证

[MIT](LICENSE)
