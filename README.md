# 小组任务 Bot

LLM 驱动的智能小组任务管理助手，支持自然语言交互、任务分配、贡献积分。

## 功能特点

- **自然语言交互** - 不用记命令，像跟同事聊天一样发任务
- **智能任务管理** - 发布、认领、完成、放弃、删除任务
- **贡献积分系统** - 自动计算贡献分，排名激励
- **多轮对话** - 能记住上下文，连续对话不丢失
- **子任务拆分** - 大任务自动拆成小任务发布
- **自动分配** - 48小时无人认领自动分配
- **每日提醒** - 临近截止任务自动提醒

## 效果演示

发布任务：
> 用户：帮我发个任务整理数学笔记，下周五截止
> Bot：搞定！任务已发布~

认领完成任务：
> 用户：我来认领任务3
> Bot：好的，「整理数学笔记」交给你了~

查看贡献排名：
> 用户：看看谁贡献最多
> Bot：🏆 贡献排名：
> - 张小明: 45分
> - 王组长: 30分

## 快速开始

### 环境要求

- Python 3.12+
- 硅基流动 API Key（免费）

### 安装

```bash
pip install -r requirements.txt
```

### 配置

创建 `.env` 文件：

```env
# NoneBot2
DRIVER=~httpx+~websockets

# QQ Bot（可选，需要 QQ 开放平台账号）
QQ_IS_SANDBOX=false
QQ_BOTS='[{"id": "YOUR_APP_ID", "token": "YOUR_TOKEN", "secret": "YOUR_SECRET"}]'
SUPERUSERS=[""]

# 数据库
TASK_MANAGER_DB_PATH=data/task_manager.db

# LLM API（硅基流动）
LLM_API_KEY=your_siliconflow_api_key
```

### 启动 Web 版（推荐）

```bash
python web_chat.py
```

浏览器打开 http://localhost:8765

支持多用户切换，模拟组长和组员的不同权限。

### 启动 QQ Bot

```bash
python bot.py
```

群里 @机器人 发消息即可。

## 项目结构

```
小组分配/
├── bot.py              # QQ Bot 入口
├── web_chat.py         # Web 聊天界面入口
├── .env                # 环境配置（需创建）
├── src/
│   └── plugins/
│       └── task_manager/
│           ├── models.py     # 数据模型
│           ├── llm_agent.py   # LLM Agent 核心
│           └── llm_tools.py  # 工具执行层
├── data/               # SQLite 数据库
└── requirements.txt
```

## 技术栈

- **LLM**: 硅基流动 Step-3.5-Flash（function calling）
- **Web**: FastAPI + SSE 流式响应
- **Bot**: NoneBot2 + QQ 适配器
- **数据库**: SQLAlchemy + aiosqlite

## AI 能力

基于 Function Calling 的 LLM Agent 架构：

```
用户消息 → LLM 理解意图 → 调用工具（发布/认领/完成等）→ 执行数据库操作 → LLM 生成自然语言回复
```

Agent 具备：
- 上下文感知：知道当前任务状态、成员工作量
- 多轮对话：记住之前的操作和回复
- 主动建议：任务拆分建议、超时提醒
- 人格化回复：像同事一样说话，不生硬

## License

MIT
