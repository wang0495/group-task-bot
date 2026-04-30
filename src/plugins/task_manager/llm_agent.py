"""LLM Agent — 让 Bot 像个人."""
import json
import os
import re
from typing import Any

import httpx

from .models import get_session
from .llm_tools import (
    publish_task, batch_publish_tasks, claim_task, complete_task, drop_task, delete_task,
    list_tasks, task_detail, my_tasks, my_contribution, ranking,
    build_context,
)


LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = "https://api.siliconflow.cn/v1"
LLM_MODEL = "stepfun-ai/Step-3.5-Flash"

# ── System Prompt：人格定义 ──────────────────────────────────────────────
SYSTEM_PROMPT = """你是「小助」，一个小组里的任务管理助手，就在群里像个靠谱的同事。

说话风格：
- 口语化，像微信聊天。可以用"搞定"、"收到"、"加把劲"、"啥"、"咋"
- 简短，2-4句话，别写小作文
- 最多1-2个emoji，不要满屏emoji
- 偶尔幽默一下，但别尬，别刻意卖萌

你能做的事：
- 发布、认领、完成、放弃、删除任务
- 查看任务列表、任务详情、个人贡献、小组排名
- 主动给建议

重要规则：
- 操作类（发布/认领/完成等）：如果有足够信息就直接执行，不要反复确认。只有真的缺少关键信息（比如标题）才问
- 不能让组员认领自己发布的任务
- 回复要有上下文感：知道谁是组长、谁手头忙、谁刚完成任务
- 你能看到的"当前状态"里包含了任务看板信息，善用它们

如果用户只是随便聊聊（比如问好、闲聊），也友好回应，不需要调用任何工具。

【多轮任务流】：如果用户在说"主题是农场"、"截止一周后"这类补充信息，说明他们在回答你之前的问题，请把这些信息整合到正在进行的任务中继续执行，不要重复问同样的问题。"""

# ── 工具定义（Step-3.5-Flash function calling）─────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "publish_task",
            "description": "发布一个新任务到小组。如果用户说自己是组长或者问能不能发任务，就用这个确认后执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题"},
                    "description": {"type": "string", "description": "任务详细描述"},
                    "deadline": {"type": "string", "description": "截止日期，可以是自然语言如'下周三'、'5月1号'、'一周后'"},
                    "assign_to": {"type": "string", "description": "指定给谁认领（填姓名，可选）"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_publish_tasks",
            "description": "批量发布多个任务。用于拆分子任务：把一个大任务拆成多个小任务一次发布。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "子任务标题"},
                                "description": {"type": "string", "description": "子任务描述"},
                                "deadline": {"type": "string", "description": "截止日期"},
                            },
                            "required": ["title"],
                        },
                        "description": "子任务列表",
                    }
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_task",
            "description": "认领一个待认领的任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "标记任务为已完成。如果用户说'完成了'、'搞定了'、'搞掂了'，先确认是哪个任务再调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drop_task",
            "description": "放弃已认领的任务，退回待认领池",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "删除任务（仅发布者或组长可用）",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "查看小组所有任务列表",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_detail",
            "description": "查看某个任务的详细信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "my_tasks",
            "description": "查看当前用户认领的所有任务",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "my_contribution",
            "description": "查看当前用户的贡献分数、排名和详细数据",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ranking",
            "description": "查看小组贡献排名",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


TOOL_MAP = {
    "publish_task": publish_task,
    "batch_publish_tasks": batch_publish_tasks,
    "claim_task": claim_task,
    "complete_task": complete_task,
    "drop_task": drop_task,
    "delete_task": delete_task,
    "list_tasks": list_tasks,
    "task_detail": task_detail,
    "my_tasks": my_tasks,
    "my_contribution": my_contribution,
    "ranking": ranking,
}


# ── 自然语言参数提取器 ────────────────────────────────────────────────

def _extract_deadline(text: str) -> str | None:
    """从用户回复中提取截止日期。"""
    # 截止xxx，xxx -> 截止后的部分到第一个标点/连接词
    m = re.search(r"截止\s*[:：]?\s*([^，,\s]{2,15})", text)
    if m:
        raw = m.group(1).strip()
        for suffix in ("子任务", "然后", "并且"):
            if raw.endswith(suffix):
                raw = raw[:-len(suffix)].strip()
        return raw
    # 相对日期：一周后/一星期后
    if "一周" in text or "一星期" in text:
        from datetime import datetime, timedelta
        target = datetime.now() + timedelta(days=7)
        return target.strftime("%m月%d日")
    return None


def _extract_title(text: str) -> str | None:
    """从回复中提取任务标题。"""
    # "标题是/就/用 XXX" 或 "标题就XXX吧"（吧在标点之前）
    for pat in (r"标题(?:就|是|用)\s*([^，,\s]{2,30})", r"标题就([^，,\s]{2,30}?)吧"):
        m = re.search(pat, text)
        if m:
            title = m.group(1).strip().rstrip("吧").strip()
            if title and len(title) >= 2:
                return title
    # "发个任务XXX"
    m = re.search(r"发个任务\s*(.{2,30})", text)
    if m:
        t = m.group(1).strip().rstrip("吧")
        if t and len(t) >= 2:
            return t
    # 兜底：去掉截止日期部分，剩下的当标题
    cleaned = text.strip()
    # 截止 + 日期词（下周五/5月1号/明天/三天后等）+ 可选标点
    _deadline_re = r"截止\s*(?:\d{1,2}月\d{1,2}[号日]?|[下本上这]?[一二三四五六日天末周]+|明天|后天|大后天|\d+天后)\s*[，,]?\s*"
    for pattern in (r"[，,].*截止.*", r"\s+截止.*", _deadline_re, r"[，,].*deadline.*", r"[，,].*到期.*"):
        cleaned = re.sub(pattern, "", cleaned).strip()
    if cleaned and 2 <= len(cleaned) <= 50:
        # 排除明显的非标题
        skip = ("取消", "算了", "不要", "帮助", "列表", "排名", "查看", "确认", "好的", "对", "是")
        if cleaned not in skip and not any(cleaned.startswith(w) for w in skip):
            return cleaned
    return None


# ── Tool Executor ──────────────────────────────────────────────────────
async def _execute_tool(tool_name: str, arguments: dict, user_openid: str, user_name: str, group_openid: str, session) -> str:
    """执行一个工具，返回 JSON 字符串."""
    func = TOOL_MAP.get(tool_name)
    if not func:
        return json.dumps({"ok": False, "error": f"未知工具: {tool_name}", "type": tool_name}, ensure_ascii=False)

    try:
        # 只传需要的参数
        sig_params: dict[str, Any] = {"session": session}

        if tool_name == "publish_task":
            sig_params.update({
                "user_openid": user_openid,
                "user_name": user_name,
                "group_openid": group_openid,
                "title": arguments.get("title") or "",
                "description": arguments.get("description") or "",
                "deadline": arguments.get("deadline"),
                "assign_to": arguments.get("assign_to"),
            })
        elif tool_name == "batch_publish_tasks":
            sig_params.update({
                "user_openid": user_openid,
                "user_name": user_name,
                "group_openid": group_openid,
                "tasks": arguments.get("tasks") or [],
            })
        elif tool_name == "claim_task":
            sig_params.update({
                "user_openid": user_openid, "user_name": user_name,
                "group_openid": group_openid, "task_id": arguments.get("task_id"),
            })
        elif tool_name == "complete_task":
            sig_params.update({
                "user_openid": user_openid, "user_name": user_name,
                "group_openid": group_openid, "task_id": arguments.get("task_id"),
            })
        elif tool_name == "drop_task":
            sig_params.update({
                "user_openid": user_openid,
                "group_openid": group_openid, "task_id": arguments.get("task_id"),
            })
        elif tool_name == "delete_task":
            sig_params.update({
                "user_openid": user_openid, "user_name": user_name,
                "group_openid": group_openid, "task_id": arguments.get("task_id"),
            })
        elif tool_name == "task_detail":
            sig_params.update({
                "task_id": arguments.get("task_id"),
                "group_openid": group_openid,
            })
        elif tool_name == "my_contribution":
            sig_params.update({
                "user_openid": user_openid, "user_name": user_name, "group_openid": group_openid,
            })
        elif tool_name == "my_tasks":
            sig_params.update({"user_openid": user_openid, "group_openid": group_openid})
        elif tool_name == "list_tasks":
            sig_params["group_openid"] = group_openid
        elif tool_name == "ranking":
            sig_params["group_openid"] = group_openid
            if arguments.get("limit"):
                sig_params["limit"] = arguments.get("limit")
        else:
            sig_params.update(arguments)

        result = await func(**sig_params)
        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"ok": False, "error": str(e), "type": tool_name}, ensure_ascii=False)


# ── LLM Caller ─────────────────────────────────────────────────────────
async def _call_llm(messages: list[dict], tools: list | None = None) -> dict:
    """调用 Step-3.5-Flash API."""
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        async with httpx.AsyncClient(
            base_url=LLM_BASE_URL,
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        ) as client:
            response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            # 检查 API 返回的错误
            if "error" in data:
                return {"content": f"[API错误: {data['error'].get('message', data['error'])}]"}
            if "choices" not in data:
                return {"content": f"[API返回异常: {str(data)[:100]}]"}
            msg = data["choices"][0]["message"]
            # Step-3.5-Flash 是推理模型，content 可能为空，reasoning_content 有内容
            if not msg.get("content") and msg.get("reasoning_content"):
                # 从推理内容中提取最终回复（取最后几行作为回复）
                reasoning = msg["reasoning_content"]
                # 尝试取最后一段作为回复
                paragraphs = [p.strip() for p in reasoning.split("\n") if p.strip()]
                if paragraphs:
                    msg["content"] = paragraphs[-1]
            return msg
    except httpx.HTTPStatusError as e:
        return {"content": f"[API错误 {e.response.status_code}]"}
    except Exception as e:
        return {"content": f"[网络错误: {type(e).__name__} {str(e)[:80]}]"}


# ── Agent ──────────────────────────────────────────────────────────────
class TaskAgent:
    """LLM Agent，生成有人味的回复."""

    def __init__(self):
        # user_openid -> 对话历史
        self._history: dict[str, list[dict]] = {}
        # user_openid -> {"action": str, "params": dict, "msg_history": list}
        # 跨轮次追踪 pending 操作
        self._pending: dict[str, dict] = {}

    def _pending_summary(self, user_openid: str) -> str:
        """生成 pending 状态的文本描述."""
        p = self._pending.get(user_openid)
        if not p:
            return ""
        action = p.get("action", "")
        params = p.get("params", {})
        lines = [f"\n【进行中的操作：{action}】"]
        if params:
            lines.append(f"已收集的信息：{params}")
        lines.append("用户正在回答你的问题，请继续执行这个操作。")
        return "\n".join(lines)

    def _get_history(self, user_openid: str) -> list[dict]:
        """返回最近 10 轮对话."""
        return self._history.get(user_openid, [])[-20:]

    def _add_history(self, user_openid: str, user_msg: str, bot_reply: str):
        """保存对话历史（正常回复才保存）."""
        if user_openid not in self._history:
            self._history[user_openid] = []
        self._history[user_openid].append({"role": "user", "content": user_msg})
        self._history[user_openid].append({"role": "assistant", "content": bot_reply})
        if len(self._history[user_openid]) > 40:
            self._history[user_openid] = self._history[user_openid][-40:]

    def _clear_pending(self, user_openid: str):
        self._pending.pop(user_openid, None)

    def _set_pending(self, user_openid: str, action: str, params: dict):
        self._pending[user_openid] = {"action": action, "params": params}

    def _merge_pending(self, user_openid: str, new_params: dict) -> bool:
        """把新参数合并到 pending，返回 True 表示参数已齐全可以执行."""
        if user_openid not in self._pending:
            return False
        self._pending[user_openid]["params"].update(new_params)
        return True

    def _pending_params_ok(self, user_openid: str) -> bool:
        """检查 pending 参数是否齐全."""
        p = self._pending.get(user_openid)
        if not p:
            return False
        params = p.get("params", {})
        title = params.get("title")
        # publish_task 需要 title
        return bool(title and title.strip())

    async def _handle_pending_response(
        self,
        user_message: str,
        user_openid: str,
        user_name: str,
        group_openid: str,
        session,
        messages: list,
        raw_reply: str,
    ) -> str:
        """处理用户对 pending 操作的回复，尝试提取参数并执行."""
        p = self._pending[user_openid]
        action = p.get("action", "")
        params = dict(p.get("params", {}))

        # 1. 正则提取参数
        new_params: dict[str, Any] = {}
        if action == "publish_task":
            title = _extract_title(user_message)
            deadline = _extract_deadline(user_message)
            if title:
                new_params["title"] = title
            if deadline:
                new_params["deadline"] = deadline

        # 2. 合并参数
        params.update(new_params)

        # 3. 如果仍然没有 title，把用户消息当标题（短消息、非命令）
        if action == "publish_task" and not params.get("title"):
            msg = user_message.strip()
            skip_words = ("取消", "算了", "不要", "帮助", "列表", "排名", "查看")
            if 2 <= len(msg) <= 30 and not any(w in msg for w in skip_words):
                params["title"] = msg

        # 4. 参数齐全 → 直接执行工具，不使用 LLM 的初始回复
        if action == "publish_task" and params.get("title"):
            result = await _execute_tool(
                action, params, user_openid, user_name, group_openid, session
            )
            try:
                r = json.loads(result)
                if r.get("ok"):
                    self._clear_pending(user_openid)
                    tool_msg = json.dumps({**r, "type": "publish"}, ensure_ascii=False)
                    # 让 LLM 根据工具结果生成回复（不使用之前的 raw_reply）
                    messages.append({"role": "user", "content": user_message})
                    messages.append({"role": "tool", "tool_call_id": "pending_exec", "content": tool_msg})
                    final = await _call_llm(messages)
                    reply = (final.get("content") or "").strip()
                    return reply or self._fallback_reply([{"content": tool_msg}])
                else:
                    self._pending[user_openid]["params"].update(new_params)
                    return f"出问题了：{r.get('error', '未知错误')}"
            except Exception as e:
                self._pending[user_openid]["params"].update(new_params)
                return f"执行出错：{e}"

        # 5. 参数仍然不够，更新 pending 并让 LLM 继续问
        self._pending[user_openid]["params"].update(new_params)
        messages.append({"role": "assistant", "content": raw_reply})
        messages.append({"role": "user", "content": user_message})
        retry_resp = await _call_llm(messages, tools=TOOLS)
        retry_reply = (retry_resp.get("content") or "").strip()
        return retry_reply

    async def chat(
        self,
        user_message: str,
        user_openid: str,
        group_openid: str,
        user_name: str,
    ) -> str:
        """主入口：用户消息 → Agent → 自然语言回复."""
        pending_hint = self._pending_summary(user_openid)

        async with get_session() as session:
            context = await build_context(session, group_openid, user_openid, user_name)

            messages: list[dict] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": f"当前状态:\n{context}{pending_hint}"},
                *self._get_history(user_openid),
                {"role": "user", "content": user_message},
            ]

            # 第一轮：LLM 决定是否调用工具
            resp = await _call_llm(messages, tools=TOOLS)

            reply = ""

            if resp.get("tool_calls"):
                # LLM 请求调用工具
                tool_results: list[dict] = []

                for call in resp["tool_calls"]:
                    name = call["function"]["name"]
                    args_raw = call["function"]["arguments"]
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except json.JSONDecodeError:
                        args = {}

                    # 尝试合并 pending 参数
                    if self._pending_params_ok(user_openid):
                        merged = {**self._pending[user_openid]["params"], **args}
                        args = merged

                    tool_result = await _execute_tool(
                        name, args, user_openid, user_name, group_openid, session
                    )

                    try:
                        tr_data = json.loads(tool_result)
                        if tr_data.get("ok"):
                            self._clear_pending(user_openid)
                        # 失败时不清除 pending，用户可补充信息后重试
                    except Exception:
                        pass

                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": tool_result,
                    })

                # 第二轮：把工具结果给 LLM 生成回复
                messages.append({"role": "assistant", "content": resp.get("content") or "", "tool_calls": resp["tool_calls"]})
                for tr in tool_results:
                    messages.append(tr)

                # 如果 tool 执行失败，说明参数不齐全，设置 pending 让用户继续补充
                all_ok = True
                for tr in tool_results:
                    try:
                        if not json.loads(tr["content"]).get("ok", False):
                            all_ok = False
                            break
                    except Exception:
                        all_ok = False
                        break
                if not all_ok:
                    for call in resp["tool_calls"]:
                        name = call["function"]["name"]
                        try:
                            args = json.loads(call["function"]["arguments"]) if isinstance(call["function"]["arguments"], str) else call["function"]["arguments"]
                        except Exception:
                            args = {}
                        # 设置 pending，让下一轮可以继续补充参数
                        if name == "publish_task" and user_openid not in self._pending:
                            self._set_pending(user_openid, name, args)

                final = await _call_llm(messages)
                reply = (final.get("content") or "").strip()

                if not reply:
                    reply = self._fallback_reply(tool_results)

            elif resp.get("content"):
                # LLM 直接回复（闲聊、确认问题等）
                raw = resp["content"].strip()

                # 如果之前有 pending，用户在补充信息 → 尝试执行
                if user_openid in self._pending:
                    reply = await self._handle_pending_response(
                        user_message, user_openid, user_name, group_openid, session, messages, raw
                    )
                else:
                    reply = raw

                    # 如果 LLM 问了确认问题，设置 pending
                    if _looks_like_asking_for_info(reply):
                        history_text = " ".join(
                            m["content"] for m in self._get_history(user_openid)
                        )
                        if "发布" in history_text or "发任务" in user_message:
                            self._set_pending(user_openid, "publish_task", {})
            else:
                reply = ""

            # 最终兜底
            if not reply:
                if user_openid in self._pending:
                    # 有 pending 操作 → 让 LLM 再试一次
                    p = self._pending[user_openid]
                    action = p.get("action", "publish_task")
                    params = p.get("params", {})
                    missing = "title" if action == "publish_task" and not params.get("title") else ""
                    if missing:
                        reply = f"还需要一个任务标题哦，你想发布什么内容？"
                    else:
                        reply = "收到，继续处理中～"
                else:
                    # 真正的闲聊兜底
                    reply = f"收到「{user_message}」，有什么需要帮忙的吗？"

        # 写对话历史
        if reply and not reply.startswith("["):
            self._add_history(user_openid, user_message, reply)
        elif user_openid in self._pending:
            # pending 状态下的空回复：只记录用户消息
            if user_openid not in self._history:
                self._history[user_openid] = []
            self._history[user_openid].append({"role": "user", "content": user_message})

        return reply

    def _fallback_reply(self, tool_results: list[dict]) -> str:
        """从工具结果生成兜底回复."""
        for tr in tool_results:
            try:
                r = json.loads(tr["content"])
                if not r.get("ok", True):
                    return f"出问题了：{r.get('error', '未知错误')}"
                t = r.get("type", "")
                if t == "publish":
                    return f"搞定！「{r.get('title')}」已经挂上去了，截止{r.get('deadline', '没设')}。"
                if t == "batch_publish":
                    tasks = r.get("tasks", [])
                    if not tasks:
                        return "没有创建任何任务"
                    lines = [f"搞定！已发布{len(tasks)}个子任务："]
                    for item in tasks:
                        lines.append(f"  · #{item['task_id']}「{item['title']}」截止{item['deadline']}")
                    return "\n".join(lines)
                if t == "claim":
                    return f"好的，「{r.get('title')}」交给你了～"
                if t == "complete":
                    note = "提前搞定！" if r.get("early") else "完成！"
                    return f"效率！{note} +{r.get('score')}分"
                if t == "drop":
                    return f"好，「{r.get('title')}」退回待认领了。"
                if t == "delete":
                    return f"「{r.get('title')}」已删除。"
                if t == "list":
                    tasks = r.get("tasks", [])
                    if not tasks:
                        return "目前没有任务，挺好的～"
                    lines = [f"当前{len(tasks)}个任务："]
                    for t_item in tasks:
                        lines.append(f"· {t_item['title']} [{t_item['status_label']}] → {t_item['assignee']}")
                    return "\n".join(lines)
                if t == "my_tasks":
                    tasks = r.get("tasks", [])
                    if not tasks:
                        return "你手头没有进行中的任务，挺轻松的嘛～"
                    lines = ["你当前的任务："]
                    for t_item in tasks:
                        lines.append(f"· 「{t_item['title']}」[{t_item['status_label']}] 截止{t_item['deadline']}")
                    return "\n".join(lines)
                if t == "contribution":
                    return (f"{r.get('name')}：{r.get('completed')}个完成，"
                            f"{r.get('early')}个提前，{r.get('score')}分，排名第{r.get('rank')}")
                if t == "ranking":
                    rankings = r.get("rankings", [])
                    if not rankings:
                        return "还没有贡献记录，大家加油～"
                    lines = ["🏆 贡献排名："]
                    for item in rankings:
                        lines.append(f"{item['medal']} {item['name']}: {item['score']}分")
                    return "\n".join(lines)
                return "搞定啦～"
            except Exception:
                pass
        return "处理完了，还有什么要帮忙的？"


def _looks_like_asking_for_info(text: str) -> bool:
    """判断回复是否是在向用户询问信息."""
    if not text:
        return False
    text = text.strip()
    keywords = ["需要", "补充", "要不要", "确认", "是吗", "可以吗", "你有", "具体是", "哪", "什么", "哪个"]
    return any(kw in text for kw in keywords)


# 全局单例
agent = TaskAgent()
