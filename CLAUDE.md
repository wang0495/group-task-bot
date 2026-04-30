# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

小组任务管理 Bot — 支持 QQ 群内交互和 Web 聊天界面的任务分配与贡献积分系统。LLM（Step-3.5-Flash）驱动 Agent，提供有人味的自然语言交互体验。

## Tech Stack

- Python 3.12, NoneBot2 (QQ Bot 框架), FastAPI (Web 界面)
- SQLAlchemy + aiosqlite (异步 SQLite)
- 硅基流动 Step-3.5-Flash (LLM Agent，function calling)

## Commands

```bash
# 启动 Web 聊天界面（含 QQ Bot 后台）
python web_chat.py

# 仅启动 QQ Bot（需配置 .env 中的 QQ_BOTS）
python bot.py

# 代码检查
ruff check .
mypy .

# 数据库位置
# data/task_manager.db (SQLite)
```

## Architecture

两套入口，共享数据模型和业务逻辑：

**`bot.py`** → NoneBot2 入口，加载 `src/plugins/task_manager/` 插件，通过 QQ 群 @机器人交互。插件入口是 `_plugin_init.py`（延迟加载，避免 apscheduler 时序问题）。

**`web_chat.py`** → FastAPI 应用，内置 HTML 聊天界面，支持多用户切换模拟。同时启动 NoneBot2 后台线程。

**`src/plugins/task_manager/models.py`** → 数据模型：User、Group、Task（pending/claimed/done）、Contribution。通过 `get_session()` 获取异步会话。

**`src/plugins/task_manager/llm_agent.py`** → LLM Agent 核心。System Prompt 定义人格、TOOLS 定义 function calling、TaskAgent 类管理对话历史和 LLM 调用。

**`src/plugins/task_manager/llm_tools.py`** → 工具执行层。每个 async 函数对应一个 tool（publish_task、claim_task、complete_task 等），返回 dict 供 LLM 读取后生成自然语言回复。

**NLP 流程**（双通道）：
- 简单命令 → 正则快查 `nlp_to_cmd()` → `_handle_cmd()`（0延迟，模板回复）
- 自然语言 → LLM Agent → tool 执行 → LLM 生成有人味回复

## Environment Variables (.env)

- `QQ_BOTS` — QQ 开放平台凭据（JSON 数组）
- `SUPERUSERS` — Bot 管理员 openid 列表
- `TASK_MANAGER_DB_PATH` — 数据库路径（默认 `data/task_manager.db`）
- `TASK_MANAGER_AUTO_ASSIGN_HOURS` — 无人认领自动分配时限（默认 48h）
- `TASK_MANAGER_DAILY_REMINDER_HOUR` — 每日提醒时间（默认 9 点）
- `LLM_API_KEY` — 硅基流动 API 密钥

## Known Issues

- Web 前端 800ms 轮询全量消息，无增量更新
- QQ Bot 端尚未接入 Agent（Agent 仅在 Web 版生效）
- 定时任务（auto_assign、daily_reminder）在 QQ Bot 模式下可能因 Windows 信号处理报错（不影响 Web 版）
