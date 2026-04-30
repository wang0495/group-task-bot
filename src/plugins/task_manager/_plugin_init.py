"""Group task management plugin — NoneBot2."""
from datetime import datetime, timedelta, timezone

from nonebot import on_command, on_message, get_driver, get_bot
from nonebot.adapters.qq import GroupAtMessageCreateEvent
from nonebot.params import CommandArg
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from .models import get_session, Task, User, Group, Contribution, TaskStatus


def _get_scheduler():
    """Lazy-load scheduler after nonebot.init() has run."""
    from nonebot_plugin_apscheduler import scheduler
    return scheduler

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def TZ(): return timezone(timedelta(hours=8))

def now_tz(): return datetime.now(TZ())

def _aware(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware (UTC+8). SQLite loses tzinfo."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ())
    return dt

# Track users in multi-step publish flow
# user_id -> {"title", "desc", "deadline", "group_openid", "publisher_openid"}
_PUBLISH_DATA: dict[str, dict] = {}

async def get_or_create_user(session: AsyncSession, openid: str, name: str = "") -> User:
    user = (await session.scalars(select(User).where(User.openid == openid))).first()
    if not user:
        user = User(openid=openid, name=name)
        session.add(user)
        await session.flush()
    elif name and not user.name:
        user.name = name
    return user

async def get_or_create_group(session: AsyncSession, openid: str, name: str = "") -> Group:
    group = (await session.scalars(select(Group).where(Group.openid == openid))).first()
    if not group:
        group = Group(openid=openid, name=name)
        session.add(group)
        await session.flush()
    elif name and not group.name:
        group.name = name
    return group

async def ensure_member(session: AsyncSession, event: GroupAtMessageCreateEvent) -> tuple[User, Group]:
    """Ensure user and group exist. Works with GroupAtMessageCreateEvent (QQ adapter 1.x)."""
    user_id = str(event.get_user_id())
    group_id = str(event.group_openid)
    # Try to get nickname from author; fallback to user_id
    nickname = getattr(event.author, "id", user_id) or user_id
    user = await get_or_create_user(session, user_id, nickname)
    # group_openid doesn't carry group name; use openid as name fallback
    group = await get_or_create_group(session, group_id, group_id)
    return user, group

async def is_group_admin(session: AsyncSession, event: GroupAtMessageCreateEvent) -> bool:
    """Check if sender is group admin/owner. Also checks bot superusers."""
    driver = get_driver()
    superusers = set(driver.config.superusers or [])
    if str(event.get_user_id()) in superusers:
        return True
    user_id = str(event.get_user_id())
    group = await get_or_create_group(session, str(event.group_openid))
    return group.leader_openid == user_id

# ----------------------------------------------------------------------
# /发布任务
# ----------------------------------------------------------------------
@on_command("发布任务", aliases={"publish", "newtask"}).handle()
async def publish_task(event: GroupAtMessageCreateEvent, arg: str = CommandArg()):
    arg_str = str(arg).strip()
    user_id = str(event.get_user_id())

    # Reset if user starts a new publish while in flow
    _PUBLISH_DATA.pop(user_id, None)

    title = arg_str
    if not title:
        await publish_task.finish("⚠️ 格式：/发布任务 <标题>\n示例：/发布任务 完成项目文档")

    async with get_session() as session:
        if not await is_group_admin(session, event):
            await publish_task.finish("⛔ 只有组长或管理员可以发布任务")

        # Check if description and deadline are embedded: /发布任务 标题 --desc 描述 --deadline 2026-05-01 18:00
        parts = title.split(" --desc ")
        title_only = parts[0].strip()
        description = ""
        deadline = None

        if len(parts) > 1:
            rest = parts[1]
            if " --deadline " in rest:
                desc_part, dl_part = rest.split(" --deadline ", 1)
                description = desc_part.strip()
                try:
                    deadline = datetime.strptime(dl_part.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ())
                except ValueError:
                    pass
            else:
                description = rest.strip()

        # Store initial data
        _PUBLISH_DATA[user_id] = {
            "title": title_only,
            "desc": description,
            "deadline": deadline,
            "group_openid": str(event.group_openid),
            "publisher_openid": str(event.get_user_id()),
        }

        # If no inline description, enter step-by-step flow
        if len(parts) == 1:
            await publish_task.send(
                f"📋 标题：{title_only}\n"
                f"📝 请输入任务描述（或输入\"跳过\"）："
            )
            return

        # If no inline deadline, ask for it
        if deadline is None:
            await publish_task.send(
                f"📋 标题：{title_only}\n"
                f"📝 描述：{description or '无'}\n"
                f"⏰ 请输入截止时间（格式 2026-05-01 18:00，或输入\"无\"）："
            )
            return

        # All data collected — create task
        data = _PUBLISH_DATA.pop(user_id, None)
        task = await _create_task(session, data)
        await publish_task.finish(_fmt_task_published(task))


async def _create_task(session: AsyncSession, data: dict) -> Task:
    group = await get_or_create_group(session, data["group_openid"])
    user = await get_or_create_user(session, data["publisher_openid"])
    task = Task(
        group_id=group.id,
        publisher_openid=user.openid,
        title=data["title"],
        description=data.get("desc", ""),
        deadline=data.get("deadline"),
        status=TaskStatus.PENDING.value,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _fmt_task_published(task: Task) -> str:
    deadline_str = task.deadline.strftime("%Y-%m-%d %H:%M") if task.deadline else "无"
    auto_assign = task.created_at + timedelta(hours=48)
    return (
        f"✅ 任务已发布！\n"
        f"ID: #{task.id}\n"
        f"标题：{task.title}\n"
        f"描述：{task.description or '无'}\n"
        f"截止：{deadline_str}\n"
        f"状态：🔵 待认领\n"
        f"自动分配：{auto_assign.strftime('%Y-%m-%d %H:%M')}（48小时后）"
    )


# ----------------------------------------------------------------------
# Multi-step input handler (description and deadline text input)
# ----------------------------------------------------------------------
@on_message().handle()
async def handle_publish_input(event: GroupAtMessageCreateEvent):
    user_id = str(event.get_user_id())
    data = _PUBLISH_DATA.get(user_id)
    if not data:
        return  # not in a publish flow

    text = event.get_plaintext().strip()

    if not data.get("desc") and "desc" not in data:
        # First input: description
        data["desc"] = text if text.lower() != "跳过" else ""
        await handle_publish_input.send(
            f"📋 标题：{data['title']}\n"
            f"📝 描述：{data['desc'] or '无'}\n"
            f"⏰ 请输入截止时间（格式 2026-05-01 18:00，或输入\"无\"）："
        )
        return

    # Second input: deadline
    deadline = None
    if text != "无":
        try:
            deadline = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ())
        except ValueError:
            data["deadline"] = None
            data.pop("desc", None)
            _PUBLISH_DATA.pop(user_id, None)
            await handle_publish_input.finish("⚠️ 时间格式错误，任务已保存（无截止时间）")

    data["deadline"] = deadline
    _PUBLISH_DATA.pop(user_id, None)

    async with get_session() as session:
        task = await _create_task(session, data)
        await handle_publish_input.finish(_fmt_task_published(task))

# ----------------------------------------------------------------------
# /任务列表
# ----------------------------------------------------------------------
@on_command("任务列表", aliases={"tasks"}).handle()
async def list_tasks(event: GroupAtMessageCreateEvent, arg: str = CommandArg()):
    async with get_session() as session:
        _, group = await ensure_member(session, event)
        status_filter = str(arg).strip().lower() if arg else None

        query = select(Task).where(Task.group_id == group.id)
        if status_filter and status_filter not in ("全部", ""):
            query = query.where(Task.status == status_filter)
        query = query.order_by(Task.created_at.desc())

        tasks = (await session.scalars(query)).all()

        if not tasks:
            await list_tasks.finish("📭 暂无任务")

        lines = ["📋 任务列表：\n"]
        for t in tasks:
            emoji = {"pending": "🔵", "claimed": "🟡", "done": "✅"}.get(t.status, "⚪")
            deadline_str = t.deadline.strftime("%m-%d %H:%M") if t.deadline else "无"
            assignee = f"→{t.assignee_openid}" if t.assignee_openid else "→待认领"
            lines.append(f"{emoji} #{t.id} {t.title} | {deadline_str} | {assignee}")
        await list_tasks.finish("\n".join(lines))

# ----------------------------------------------------------------------
# /认领任务
# ----------------------------------------------------------------------
@on_command("认领任务", aliases={"claim"}).handle()
async def claim_task(event: GroupAtMessageCreateEvent, arg: str = CommandArg()):
    task_id_str = str(arg).strip()
    if not task_id_str.isdigit():
        await claim_task.finish("⚠️ 格式：/认领任务 <ID>\n示例：/认领任务 42")

    async with get_session() as session:
        user, group = await ensure_member(session, event)
        task = await session.get(Task, int(task_id_str))

        if not task or task.group_id != group.id:
            await claim_task.finish("⚠️ 任务不存在")
        if task.status != TaskStatus.PENDING.value:
            await claim_task.finish(f"⚠️ 任务状态为 {task.status}，无法认领")
        if task.publisher_openid == user.openid:
            await claim_task.finish("⚠️ 不能认领自己发布的任务")

        task.status = TaskStatus.CLAIMED.value
        task.assignee_openid = user.openid
        task.claimed_at = now_tz()
        await session.commit()

        await claim_task.finish(f"✅ 已认领「{task.title}」，请尽快完成！")

# ----------------------------------------------------------------------
# /完成任务
# ----------------------------------------------------------------------
@on_command("完成任务", aliases={"done"}).handle()
async def complete_task_cmd(event: GroupAtMessageCreateEvent, arg: str = CommandArg()):
    task_id_str = str(arg).strip()
    if not task_id_str.isdigit():
        await complete_task_cmd.finish("⚠️ 格式：/完成任务 <ID>\n示例：/完成任务 42")

    async with get_session() as session:
        user, group = await ensure_member(session, event)
        task = await session.get(Task, int(task_id_str))

        if not task or task.group_id != group.id:
            await complete_task_cmd.finish("⚠️ 任务不存在")
        if task.status != TaskStatus.CLAIMED.value:
            await complete_task_cmd.finish(f"⚠️ 任务状态为 {task.status}，需要先认领")
        if task.assignee_openid != user.openid:
            await complete_task_cmd.finish("⛔ 只有认领人可以标记完成")

        early = bool(task.deadline and now_tz() < _aware(task.deadline))

        task.status = TaskStatus.DONE.value
        task.done_at = now_tz()

        contrib = (await session.scalars(
            select(Contribution).where(
                and_(Contribution.user_id == user.id, Contribution.group_id == group.id)
            )
        )).first()
        if not contrib:
            contrib = Contribution(user_id=user.id, group_id=group.id)
            session.add(contrib)
            await session.flush()

        contrib.completed_count += 1
        if early:
            contrib.early_count += 1
        contrib.score = contrib.completed_count * 10 + contrib.early_count * 5

        await session.commit()
        pts = 10 + (5 if early else 0)
        note = "提前完成" if early else "按时完成"
        await complete_task_cmd.finish(f"✅ 「{task.title}」已完成！贡献 +{pts} 分（{note}）")

# ----------------------------------------------------------------------
# /放弃任务
# ----------------------------------------------------------------------
@on_command("放弃任务", aliases={"drop"}).handle()
async def drop_task(event: GroupAtMessageCreateEvent, arg: str = CommandArg()):
    task_id_str = str(arg).strip()
    if not task_id_str.isdigit():
        await drop_task.finish("⚠️ 格式：/放弃任务 <ID>\n示例：/放弃任务 42")

    async with get_session() as session:
        user, group = await ensure_member(session, event)
        task = await session.get(Task, int(task_id_str))

        if not task or task.group_id != group.id:
            await drop_task.finish("⚠️ 任务不存在")
        if task.status != TaskStatus.CLAIMED.value:
            await drop_task.finish(f"⚠️ 任务状态为 {task.status}，无法放弃")
        if task.assignee_openid != user.openid:
            await drop_task.finish("⛔ 只有认领人可以放弃")

        task.status = TaskStatus.PENDING.value
        task.assignee_openid = None
        task.claimed_at = None
        await session.commit()

        await drop_task.finish(f"↩️ 「{task.title}」已退回待认领池")

# ----------------------------------------------------------------------
# /我的任务
# ----------------------------------------------------------------------
@on_command("我的任务", aliases={"mytasks"}).handle()
async def my_tasks(event: GroupAtMessageCreateEvent):
    async with get_session() as session:
        user, group = await ensure_member(session, event)
        tasks = (await session.scalars(
            select(Task).where(
                and_(Task.group_id == group.id, Task.assignee_openid == user.openid)
            ).order_by(Task.deadline)
        )).all()

        if not tasks:
            await my_tasks.finish("📭 你当前没有认领的任务")
        lines = ["📋 你的任务：\n"]
        for t in tasks:
            emoji = {"claimed": "🟡", "done": "✅"}.get(t.status, "🔵")
            deadline_str = t.deadline.strftime("%m-%d %H:%M") if t.deadline else "无"
            lines.append(f"{emoji} #{t.id} {t.title} | {deadline_str}")
        await my_tasks.finish("\n".join(lines))

# ----------------------------------------------------------------------
# /任务详情
# ----------------------------------------------------------------------
@on_command("任务详情", aliases={"task"}).handle()
async def task_detail(event: GroupAtMessageCreateEvent, arg: str = CommandArg()):
    task_id_str = str(arg).strip()
    if not task_id_str.isdigit():
        await task_detail.finish("⚠️ 格式：/任务详情 <ID>\n示例：/任务详情 42")

    async with get_session() as session:
        _, group = await ensure_member(session, event)
        task = await session.get(Task, int(task_id_str))

        if not task or task.group_id != group.id:
            await task_detail.finish("⚠️ 任务不存在")

        deadline_str = task.deadline.strftime("%Y-%m-%d %H:%M") if task.deadline else "无"
        created_str = task.created_at.strftime("%Y-%m-%d %H:%M") if task.created_at else "无"
        status_map = {"pending": "🔵 待认领", "claimed": "🟡 进行中", "done": "✅ 已完成"}
        extra = ""
        if task.status == TaskStatus.DONE.value and task.done_at and task.deadline:
            extra = "\n✨ 提前完成！" if task.done_at < task.deadline else "\n⏰ 按时完成"

        msg = (
            f"📋 任务详情\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"#{task.id} {task.title}\n"
            f"描述：{task.description or '无'}\n"
            f"状态：{status_map.get(task.status, task.status)}\n"
            f"发布者：{task.publisher_openid}\n"
            f"负责人：{task.assignee_openid or '无'}\n"
            f"截止：{deadline_str}\n"
            f"创建：{created_str}{extra}\n"
            f"━━━━━━━━━━━━━━━━"
        )
        await task_detail.finish(msg)

# ----------------------------------------------------------------------
# /删除任务
# ----------------------------------------------------------------------
@on_command("删除任务", aliases={"delete"}).handle()
async def delete_task(event: GroupAtMessageCreateEvent, arg: str = CommandArg()):
    task_id_str = str(arg).strip()
    if not task_id_str.isdigit():
        await delete_task.finish("⚠️ 格式：/删除任务 <ID>\n示例：/删除任务 42")

    async with get_session() as session:
        user, group = await ensure_member(session, event)
        task = await session.get(Task, int(task_id_str))

        if not task or task.group_id != group.id:
            await delete_task.finish("⚠️ 任务不存在")
        is_admin = await is_group_admin(session, event)
        if not is_admin and task.publisher_openid != user.openid:
            await delete_task.finish("⛔ 只有发布者或管理员可以删除")

        await session.delete(task)
        await session.commit()
        await delete_task.finish(f"🗑️ 「{task.title}」已删除")

# ----------------------------------------------------------------------
# /贡献排名
# ----------------------------------------------------------------------
@on_command("贡献排名", aliases={"rank"}).handle()
async def contribution_ranking(event: GroupAtMessageCreateEvent, arg: str = CommandArg()):
    limit_str = str(arg).strip()
    limit = int(limit_str) if limit_str.isdigit() and 1 <= int(limit_str) <= 50 else 10

    async with get_session() as session:
        _, group = await ensure_member(session, event)

        results = (await session.execute(
            select(Contribution, User.name)
            .join(User, Contribution.user_id == User.id)
            .where(Contribution.group_id == group.id)
            .order_by(Contribution.score.desc())
            .limit(limit)
        )).all()

        if not results:
            await contribution_ranking.finish("📭 暂无贡献记录")

        lines = ["🏆 贡献排名：\n"]
        for i, (c, name) in enumerate(results, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"  {i}.")
            early_note = f" (⏰{c.early_count})" if c.early_count else ""
            lines.append(
                f"{medal} {name or '未知'}：{c.completed_count}完成{early_note} → {c.score} 分"
            )
        await contribution_ranking.finish("\n".join(lines))

# ----------------------------------------------------------------------
# /我的贡献
# ----------------------------------------------------------------------
@on_command("我的贡献", aliases={"myscore"}).handle()
async def my_contribution(event: GroupAtMessageCreateEvent):
    async with get_session() as session:
        user, group = await ensure_member(session, event)

        contrib = (await session.scalars(
            select(Contribution).where(
                and_(Contribution.user_id == user.id, Contribution.group_id == group.id)
            )
        )).first()

        if not contrib:
            contrib = Contribution(user_id=user.id, group_id=group.id)
            session.add(contrib)
            await session.flush()

        rank = (await session.scalar(
            select(func.count(Contribution.id)).where(
                and_(Contribution.group_id == group.id, Contribution.score > contrib.score)
            )
        )) or 0
        rank += 1

        formula = f"{contrib.completed_count}×10 + {contrib.early_count}×5 = {contrib.score} 分"
        msg = (
            f"📊 {user.name or event.get_user_id()} 的贡献\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ 完成任务：{contrib.completed_count} 个\n"
            f"⏰ 提前完成：{contrib.early_count} 个\n"
            f"🎯 贡献分数：{formula}\n"
            f"🤖 被自动分配：{contrib.auto_assigned_count} 次\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏆 小组排名：第 {rank} 名\n"
            f"公式：分数 = 完成×10 + 提前×5"
        )
        await my_contribution.finish(msg)

# ----------------------------------------------------------------------
# /帮助
# ----------------------------------------------------------------------
@on_command("帮助", aliases={"help"}).handle()
async def show_help():
    await show_help.finish(
        "📖 <b>小组任务 Bot 命令</b>\n\n"
        "<b>任务管理</b>\n"
        "/发布任务 <标题> — 发布任务（组长）\n"
        "/任务列表 [状态] — 查看任务\n"
        "/任务详情 <id> — 任务详情\n"
        "/删除任务 <id> — 删除任务（发布者/组长）\n\n"
        "<b>认领流程</b>\n"
        "/认领任务 <id> — 认领待认领任务\n"
        "/完成任务 <id> — 标记完成\n"
        "/放弃任务 <id> — 退回待认领池\n"
        "/我的任务 — 我的任务\n\n"
        "<b>贡献系统</b>\n"
        "/贡献排名 [N] — 贡献前 N 名\n"
        "/我的贡献 — 我的贡献分\n\n"
        "📌 任务发布48h后无人认领自动分配\n"
        "📌 贡献分 = 完成×10 + 提前×5",
        reply_message=True,
    )

# ----------------------------------------------------------------------
# Scheduler: 48h auto-assign (every 30 min)
# ----------------------------------------------------------------------
@_get_scheduler().scheduled_job("cron", minute="*/30", id="auto_assign_check")
async def auto_assign_check():
    """Unclaimed tasks after 48h → auto-assign to random member."""
    try:
        bot = get_bot()
    except Exception:
        return

    async with get_session() as session:
        cutoff = datetime.utcnow() - timedelta(hours=48)
        overdue = (await session.scalars(
            select(Task).where(
                and_(
                    Task.status == TaskStatus.PENDING.value,
                    _aware(Task.created_at) < cutoff.replace(tzinfo=TZ()),
                )
            )
        )).all()

        for task in overdue:
            group = await session.get(Group, task.group_id)
            if not group:
                continue

            # Get all users who have tasks in this group (via publisher or assignee)
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
            eligible = [u for u in eligible if u.openid != task.publisher_openid]

            if not eligible:
                try:
                    await bot.send_group_message(
                        group_id=int(group.openid),
                        message=f"⚠️ 「{task.title}」(#{task.id}) 无人认领，请组长手动处理",
                    )
                except Exception:
                    pass
                continue

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
            task.claimed_at = now_tz()

            contrib = (await session.scalars(
                select(Contribution).where(
                    and_(Contribution.user_id == chosen.id, Contribution.group_id == task.group_id)
                )
            )).first()
            if contrib:
                contrib.auto_assigned_count += 1

            await session.commit()

            try:
                await bot.send_group_message(
                    group_id=int(group.openid),
                    message=f"🤖 「{task.title}」(#{task.id}) 超过48h无人认领，系统已分配给 @{chosen.name or chosen.openid}，请尽快完成！",
                )
            except Exception:
                pass

# ----------------------------------------------------------------------
# Scheduler: daily reminder at 9am
# ----------------------------------------------------------------------
@_get_scheduler().scheduled_job("cron", hour=9, minute=0, id="daily_reminder")
async def daily_reminder():
    """Remind about tasks approaching 48h auto-assign deadline."""
    try:
        bot = get_bot()
    except Exception:
        return

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
                        _aware(Task.created_at) < upper.replace(tzinfo=TZ()),
                        _aware(Task.created_at) > lower.replace(tzinfo=TZ()),
                    )
                )
            )).all()

            if not approaching:
                continue

            task_list = "\n".join(f"• {t.title} (#{t.id})" for t in approaching)
            try:
                await bot.send_group_message(
                    group_id=int(group.openid),
                    message=f"⏰ 提醒：以下任务即将触发自动分配：\n{task_list}",
                )
            except Exception:
                pass
