"""
Web聊天界面 — 小组任务 Bot
支持多用户切换 + 自然语言输入 + LLM 语义理解
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv  # noqa: E402
load_dotenv()  # 加载 .env 中的环境变量

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402

import nonebot  # noqa: E402
from nonebot.adapters.qq import Adapter as QQAdapter  # noqa: E402

# Init NoneBot before importing models (models triggers plugin load via __init__.py)
nonebot.init()
nonebot.get_driver().register_adapter(QQAdapter)

from sqlalchemy import select, func, and_  # noqa: E402
sys.path.insert(0, str(Path(__file__).parent / "src" / "plugins"))
from task_manager.models import get_session, init_db, Task, User, Group, Contribution, TaskStatus  # noqa: E402
from task_manager.llm_agent import agent  # noqa: E402

# ----------------------------------------------------------------------
# 多用户模拟
# ----------------------------------------------------------------------
USERS = {
    "组长":   {"openid": "111111111", "name": "王组长",  "is_leader": True},
    "组员A":  {"openid": "222222222", "name": "张小明",  "is_leader": False},
    "组员B":  {"openid": "333333333", "name": "李华",    "is_leader": False},
    "组员C":  {"openid": "444444444", "name": "陈小芳",  "is_leader": False},
}
DEFAULT_USER = "组长"

# ----------------------------------------------------------------------
# LLM 调用统一在 llm_agent.py（stepfun-ai/Step-3.5-Flash，function calling）
# ----------------------------------------------------------------------
LLM_API_KEY = os.getenv("LLM_API_KEY", "")


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
BJ_TZ = timezone(timedelta(hours=8))


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BJ_TZ)
    return dt


def _now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def _extract_task_id(text: str) -> int | None:
    """从文本中提取任务ID数字."""
    m = re.search(r"#?(\d+)", text)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------
# LLM Agent 入口
# ----------------------------------------------------------------------
async def _process_message(text: str, user_openid: str, sender_name: str) -> str:
    """处理用户消息：直接走 LLM Agent."""
    text = text.strip()
    reply = await agent.chat(text, user_openid, "888888888", sender_name)
    return reply or f"收到：「{text}」"


# ----------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------
app = FastAPI(title="小组任务 Bot")

HTML = """
<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>小组任务 Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#1a1a2e;color:#eee;height:100vh;display:flex;flex-direction:column}
header{background:#16213e;padding:10px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #0f3460}
header .title{font-size:18px;font-weight:bold}
header .subtitle{font-size:12px;color:#888}
#chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:75%;padding:10px 14px;border-radius:12px;font-size:15px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.msg.user{align-self:flex-end;background:#0f3460}
.msg.bot{align-self:flex-start;background:#16213e;border:1px solid #0f3460}
.msg .sender{font-size:11px;color:#888;margin-bottom:2px}
.msg.user .sender{text-align:right}
.cmd-hint{font-size:12px;color:#555;padding:4px 16px 8px;background:#1a1a2e}
.cmd-hint span{color:#e94560}
#input-row{background:#16213e;padding:12px 16px;display:flex;gap:8px;align-items:center}
#user-select{background:#0f3460;border:1px solid #1a4a8a;color:#eee;padding:8px 10px;border-radius:6px;font-size:13px}
#input{flex:1;background:#0f3460;border:1px solid #1a4a8a;color:#eee;padding:10px 14px;border-radius:8px;font-size:15px;outline:none}
#input:focus{border-color:#e94560}
#send{background:#e94560;color:white;border:none;padding:10px 20px;border-radius:8px;cursor:pointer;font-size:15px;font-weight:bold}
#send:hover{background:#ff6b8a}
</style>
</head>
<body>
<header>
  <div>
    <div class="title">🤖 小组任务 Bot</div>
    <div class="subtitle">多用户模拟 · 自然语言</div>
  </div>
  <select id="user-select">
    <option value="组长">👤 组长（王组长）</option>
    <option value="组员A">👤 组员A（张小明）</option>
    <option value="组员B">👤 组员B（李华）</option>
    <option value="组员C">👤 组员C（陈小芳）</option>
  </select>
</header>
<div id="chat"></div>
<div class="cmd-hint">直接说话就好，Bot 听得懂～</div>
<div id="input-row">
  <input id="input" placeholder="输入消息..." autocomplete="off" autofocus />
  <button id="send" onclick="sendMsg()">发送</button>
</div>
<script>
const chat=document.getElementById("chat"),inp=document.getElementById("input"),sel=document.getElementById("user-select");
let lastMsgCount = 0;
let sseWorking = false;

function renderMsgs(msgs) {
  const tb = document.getElementById("thinking-bubble");
  if (tb) tb.remove();
  if (msgs.length !== lastMsgCount) {
    lastMsgCount = msgs.length;
    chat.innerHTML = msgs.map(m =>
      `<div class="msg ${m.is_bot?'bot':'user'}"><div class="sender">${m.sender}</div><pre>${m.text}</pre></div>`
    ).join("");
    chat.scrollTop = chat.scrollHeight;
  }
}

// ---- SSE 连接 ----
let evtSource = null;
function connectSSE() {
  if (evtSource) { try { evtSource.close(); } catch(e){} }
  evtSource = new EventSource("/web_chat/stream");
  evtSource.addEventListener("messages", e => {
    sseWorking = true;
    renderMsgs(JSON.parse(e.data));
  });
  evtSource.addEventListener("stream", e => {
    const d = JSON.parse(e.data);
    const tb = document.getElementById("thinking-bubble");
    if (!tb) return;
    if (d.event.kind === "chunk") {
      tb.textContent += d.event.text;
      chat.scrollTop = chat.scrollHeight;
    }
  });
  evtSource.onerror = () => {
    sseWorking = false;
    evtSource.close();
    // 3 秒后重连
    setTimeout(connectSSE, 3000);
  };
}
connectSSE();

// ---- 保底轮询（每 3 秒拉一次，SSE 失效时兜底）----
setInterval(async () => {
  try {
    const r = await fetch("/web_chat/messages");
    const msgs = await r.json();
    renderMsgs(msgs);
  } catch(e) {}
}, 3000);

// ---- 发送消息 ----
async function sendMsg(){
  const t = inp.value.trim(); if(!t) return;
  inp.value = "";
  const div = document.createElement("div");
  div.className = "msg bot";
  div.id = "thinking-bubble";
  div.innerHTML = `<div class="sender">🤖 Bot</div><pre>🤖 正在思考...</pre>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  fetch("/web_chat/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:t,user:sel.value})});
}
inp.addEventListener("keydown",e=>{if(e.key==="Enter")sendMsg()});
</script>
</body></html>
"""

# ----------------------------------------------------------------------
# Message store
# ----------------------------------------------------------------------
class MsgStore:
    def __init__(self):
        self._msgs: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        # 每个进行中请求的流式事件队列 {request_id: Queue}
        self._pending_queues: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    def append(self, text: str, sender: str, is_bot: bool = False):
        self._msgs.append({
            "time": datetime.now(BJ_TZ).strftime("%H:%M:%S"),
            "sender": sender,
            "text": text,
            "is_bot": is_bot,
        })

    async def broadcast(self):
        """Notify all SSE subscribers of new messages."""
        payload = json.dumps(self._msgs)
        for q in self._subscribers:
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    async def broadcast_event(self, request_id: str, event: dict):
        """向指定请求的流式队列推送事件（thinking/chunk/done）。"""
        for q in self._subscribers:
            try:
                q.put_nowait(json.dumps({
                    "type": "stream_event",
                    "request_id": request_id,
                    "event": event,
                }))
            except Exception:
                pass

    def add_pending(self, request_id: str):
        """注册一个进行中的请求，返回其队列。"""
        q: asyncio.Queue = asyncio.Queue()
        self._pending_queues[request_id] = q
        return q

    def remove_pending(self, request_id: str):
        self._pending_queues.pop(request_id, None)

    async def stream_pending(self, request_id: str) -> asyncio.Queue:
        """等待指定请求的流式队列可用（兼容未注册的情况）。"""
        while request_id not in self._pending_queues:
            await asyncio.sleep(0.05)
        return self._pending_queues[request_id]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    def get_all(self):
        return list(self._msgs)

    def clear(self):
        self._msgs.clear()


msg_store = MsgStore()

# ----------------------------------------------------------------------
# FastAPI routes
# ----------------------------------------------------------------------
@app.post("/web_chat/send")
async def web_send(payload: dict):
    text = payload.get("text", "").strip()
    user_key = payload.get("user", DEFAULT_USER)
    if not text:
        raise HTTPException(400, "empty message")

    user_info = USERS.get(user_key, USERS[DEFAULT_USER])
    sender_name = user_info["name"]

    # 显示用户说的话 + SSE 通知
    msg_store.append(text, f"【{sender_name}】", is_bot=False)
    await msg_store.broadcast()

    # 生成请求 ID（用于流式 chunk 匹配）
    import uuid
    request_id = str(uuid.uuid4())[:8]

    # 在后台处理 LLM，不阻塞 HTTP 响应
    async def _bg(req_id: str):
        try:
            try:
                reply = await _process_message(text, user_info["openid"], sender_name)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                reply = f"⚠️ 处理出错：{type(exc).__name__}: {exc}"
            # 分段推送（模拟打字）
            for i in range(0, len(reply), 20):
                await msg_store.broadcast_event(req_id, {"kind": "chunk", "text": reply[i:i+20]})
                await asyncio.sleep(0.05)
            # 写入消息 + 全量广播
            msg_store.append(reply, "🤖 Bot", is_bot=True)
            await msg_store.broadcast()
        except Exception as e:
            import traceback
            traceback.print_exc()
            await msg_store.broadcast_event(req_id, {"kind": "error", "text": str(e)})

    asyncio.create_task(_bg(request_id))
    return {"ok": True, "request_id": request_id}


@app.get("/web_chat/stream")
async def web_stream(http_request: Request):
    """SSE endpoint — 实时推送消息更新 + 流式响应."""
    queue = msg_store.subscribe()
    try:
        async def event_generator():
            # 发送初始消息
            yield {"event": "messages", "data": json.dumps(msg_store.get_all())}
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                    # 判断是 messages 广播还是流式事件
                    try:
                        parsed = json.loads(payload)
                    except Exception:
                        continue

                    if isinstance(parsed, list):
                        # 全量消息广播
                        yield {"event": "messages", "data": payload}
                    elif parsed.get("type") == "stream_event":
                        # 流式事件统一通过 "stream" 事件发送，request_id 在数据里
                        yield {"event": "stream", "data": payload}
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": ""}
    finally:
        msg_store.unsubscribe(queue)

    async def sse_wrapper():
        async for event in event_generator():
            if event["event"] == "heartbeat":
                yield "event: heartbeat\ndata: \n\n"
            else:
                yield f"event: {event['event']}\ndata: {event['data']}\n\n"
            await asyncio.sleep(0.01)

    return StreamingResponse(
        sse_wrapper(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/web_chat/messages")
async def web_messages():
    return msg_store.get_all()


@app.get("/")
async def index():
    return HTMLResponse(content=HTML)


# ----------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------
def run_bot():
    try:
        asyncio.run(nonebot.run())
    except (NotImplementedError, ValueError, ExceptionGroup):
        # Windows 下信号处理会报错，不影响 Web 功能
        pass


def push_notification(text: str):
    """Push a scheduler notification to the web chat."""
    msg_store.append(text, "🤖 系统通知", is_bot=True)


if __name__ == "__main__":
    asyncio.run(init_db())
    msg_store.clear()
    msg_store.append("🤖 小组任务 Bot 已连接！\n切换右上角身份模拟多人协作，支持自然语言输入。\n例如：「发任务 写周报」「我完成了任务1」「认领任务2」", "System", is_bot=True)

    # Start scheduler background tasks
    from threading import Thread
    bot_thread = Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Auto-assign: every 30 minutes
    async def run_auto_assign():
        while True:
            await asyncio.sleep(1800)  # 30 minutes
            try:
                async with get_session() as session:
                    cutoff = datetime.utcnow() - timedelta(hours=48)
                    overdue = (await session.scalars(
                        select(Task).where(
                            and_(
                                Task.status == TaskStatus.PENDING.value,
                                _aware(Task.created_at) < cutoff.replace(tzinfo=BJ_TZ),
                            )
                        )
                    )).all()
                    for task in overdue:
                        group = await session.get(Group, task.group_id)
                        if not group:
                            continue
                        # Get eligible users from this group
                        user_ids = set()
                        tasks_in_group = (await session.scalars(
                            select(Task.publisher_openid, Task.assignee_openid).where(Task.group_id == task.group_id)
                        )).all()
                        for publisher, assignee in tasks_in_group:
                            if publisher:
                                user_ids.add(publisher)
                            if assignee:
                                user_ids.add(assignee)
                        eligible = []
                        for uid in user_ids:
                            if uid == task.publisher_openid:
                                continue
                            user = (await session.scalars(select(User).where(User.openid == uid))).first()
                            if user:
                                eligible.append(user)
                        if not eligible:
                            push_notification(f"⚠️ 「{task.title}」(#{task.id}) 无人认领，请组长手动处理")
                            continue
                        # Assign to user with fewest active tasks
                        async def active_count(uid: str) -> int:
                            return (await session.scalar(
                                select(func.count(Task.id)).where(
                                    and_(Task.assignee_openid == uid, Task.status == TaskStatus.CLAIMED.value)
                                )
                            )) or 0
                        scored = [(u, await active_count(u.openid)) for u in eligible]
                        scored.sort(key=lambda x: x[1])
                        chosen = scored[0][0]
                        task.status = TaskStatus.CLAIMED.value
                        task.assignee_openid = chosen.openid
                        task.claimed_at = _now_bj()
                        contrib = (await session.scalars(
                            select(Contribution).where(
                                and_(Contribution.user_id == chosen.id, Contribution.group_id == task.group_id)
                            )
                        )).first()
                        if contrib:
                            contrib.auto_assigned_count += 1
                        await session.commit()
                        push_notification(f"🤖 「{task.title}」(#{task.id}) 超48h无人认领，系统已分配给 @{chosen.name or chosen.openid}，请尽快完成！")
            except Exception as exc:
                print(f"[AutoAssign] error: {exc}")

    # Daily reminder: every day at 9am
    async def run_daily_reminder():
        while True:
            now = datetime.now(BJ_TZ)
            # Calculate seconds until next 9am
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            try:
                async with get_session() as session:
                    groups = (await session.scalars(select(Group))).all()
                    for group in groups:
                        upper = datetime.utcnow() - timedelta(hours=40)
                        lower = datetime.utcnow() - timedelta(hours=48)
                        approaching = (await session.scalars(
                            select(Task).where(
                                and_(
                                    Task.group_id == group.id,
                                    Task.status == TaskStatus.PENDING.value,
                                    _aware(Task.created_at) < upper.replace(tzinfo=BJ_TZ),
                                    _aware(Task.created_at) > lower.replace(tzinfo=BJ_TZ),
                                )
                            )
                        )).all()
                        if approaching:
                            task_list = "\n".join(f"• {t.title} (#{t.id})" for t in approaching)
                            push_notification(f"⏰ 提醒：以下任务即将触发自动分配：\n{task_list}")
            except Exception as exc:
                print(f"[DailyReminder] error: {exc}")

    # Start background tasks (use threading since asyncio loop is blocked by uvicorn)
    t1 = Thread(target=lambda: asyncio.run(run_auto_assign()), daemon=True)
    t2 = Thread(target=lambda: asyncio.run(run_daily_reminder()), daemon=True)
    t1.start()
    t2.start()

    print("\n" + "=" * 50)
    print("  🤖 小组任务 Bot 已启动")
    print("  🌐 打开浏览器访问: http://localhost:8765")
    print("  按 Ctrl+C 停止")
    print("=" * 50 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning",
                 timeout_keep_alive=0)  # 防止 SSE 连接被 uvicorn 超时断开
