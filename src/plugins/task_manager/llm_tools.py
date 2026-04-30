"""Tool execution layer — Agent 调用的数据库操作.

每个函数接收 session + 参数，返回 dict，供 LLM 读取结果后生成自然语言回复。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, and_

from .models import Task, User, Group, Contribution, TaskStatus


TZ = timezone(timedelta(hours=8))


def _aware(dt: datetime) -> datetime | None:
    """Ensure datetime is timezone-aware (UTC+8). SQLite returns naive."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt


def _now() -> datetime:
    return datetime.now(TZ)


def _parse_deadline(raw: str | None) -> datetime | None:
    """Parse deadline from natural language. Returns aware datetime or None."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%m月%d日"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            pass
    # 自然语言：下周五 → 找到下周五的日期
    import re
    m = re.search(r"下周([日一二三四五六])", raw)
    if m:
        day_map = {"日": 6, "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5}
        target_weekday = day_map.get(m.group(1))
        if target_weekday is not None:
            today = datetime.now(TZ)
            days_ahead = (target_weekday - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target = today + timedelta(days=days_ahead)
            return target.replace(hour=23, minute=59)
    # 三天后 / 3天 → now + N days
    m = re.search(r"(\d+)\s*天", raw)
    if m:
        days = int(m.group(1))
        return (datetime.now(TZ) + timedelta(days=days)).replace(hour=23, minute=59)
    return None


# ── 工具实现 ──────────────────────────────────────────────────────────────

async def publish_task(
    session,
    user_openid: str,
    user_name: str,
    group_openid: str,
    title: str,
    description: str = "",
    deadline: str | None = None,
    assign_to: str | None = None,
) -> dict:
    """发布任务。返回 dict 供 LLM 生成回复。"""
    try:
        # 确保 group 存在
        group = (await session.scalars(
            select(Group).where(Group.openid == group_openid)
        )).first()
        if not group:
            group = Group(openid=group_openid, name=group_openid)
            session.add(group)
            await session.flush()

        dl = _parse_deadline(deadline)

        task = Task(
            group_id=group.id,
            publisher_openid=user_openid,
            title=title,
            description=description,
            deadline=dl,
            status=TaskStatus.PENDING.value,
        )
        session.add(task)
        await session.flush()

        # 如果指定了认领人，尝试匹配
        assignee_name = None
        if assign_to:
            assignee_user = (await session.scalars(
                select(User).where(User.name.like(f"%{assign_to}%"))
            )).first()
            if assignee_user:
                assignee_name = assignee_user.name

        await session.commit()
        await session.refresh(task)

        dl_str = dl.strftime("%m月%d日 %H:%M") if dl else "未设"
        auto_assign_time = (task.created_at + timedelta(hours=48)).strftime("%m月%d日 %H:%M")

        return {
            "ok": True,
            "task_id": task.id,
            "title": task.title,
            "deadline": dl_str,
            "auto_assign_time": auto_assign_time,
            "assignee": assignee_name,
            "type": "publish",
        }
    except Exception as e:
        await session.rollback()
        return {"ok": False, "error": f"发布失败：{e}", "type": "publish"}


async def batch_publish_tasks(
    session,
    user_openid: str,
    user_name: str,
    group_openid: str,
    tasks: list[dict],
) -> dict:
    """批量发布任务（用于拆分子任务）。tasks: [{"title": str, "description": str, "deadline": str}, ...]"""
    try:
        group = (await session.scalars(
            select(Group).where(Group.openid == group_openid)
        )).first()
        if not group:
            group = Group(openid=group_openid, name=group_openid)
            session.add(group)
            await session.flush()

        created = []
        for t in tasks:
            title = t.get("title", "").strip()
            if not title:
                continue
            dl = _parse_deadline(t.get("deadline"))
            task = Task(
                group_id=group.id,
                publisher_openid=user_openid,
                title=title,
                description=t.get("description", ""),
                deadline=dl,
                status=TaskStatus.PENDING.value,
            )
            session.add(task)
            await session.flush()
            dl_str = dl.strftime("%m月%d日") if dl else "未设"
            created.append({"task_id": task.id, "title": title, "deadline": dl_str})

        await session.commit()
        return {"ok": True, "tasks": created, "count": len(created), "type": "batch_publish"}
    except Exception as e:
        await session.rollback()
        return {"ok": False, "error": f"批量发布失败：{e}", "type": "batch_publish"}


async def claim_task(
    session,
    user_openid: str,
    user_name: str,
    group_openid: str,
    task_id: int,
) -> dict:
    """认领任务。"""
    try:
        # 确保 user 存在
        user = (await session.scalars(
            select(User).where(User.openid == user_openid)
        )).first()
        if not user:
            user = User(openid=user_openid, name=user_name)
            session.add(user)
            await session.flush()

        group = (await session.scalars(
            select(Group).where(Group.openid == group_openid)
        )).first()
        if not group:
            group = Group(openid=group_openid, name=group_openid)
            session.add(group)
            await session.flush()

        task = await session.get(Task, task_id)

        if not task or task.group_id != group.id:
            return {"ok": False, "error": "任务不存在", "type": "claim"}
        if task.status != TaskStatus.PENDING.value:
            return {"ok": False, "error": f"任务状态是 {task.status}，无法认领", "type": "claim"}
        if task.publisher_openid == user_openid:
            return {"ok": False, "error": "不能认领自己发布的任务", "type": "claim"}

        task.status = TaskStatus.CLAIMED.value
        task.assignee_openid = user_openid
        task.claimed_at = _now()
        await session.commit()

        return {
            "ok": True,
            "task_id": task.id,
            "title": task.title,
            "user_name": user.name or user_openid,
            "type": "claim",
        }
    except Exception as e:
        await session.rollback()
        return {"ok": False, "error": f"认领失败：{e}", "type": "claim"}


async def complete_task(
    session,
    user_openid: str,
    user_name: str,
    group_openid: str,
    task_id: int,
) -> dict:
    """完成任务，更新贡献。"""
    try:
        user = (await session.scalars(
            select(User).where(User.openid == user_openid)
        )).first()
        if not user:
            user = User(openid=user_openid, name=user_name)
            session.add(user)
            await session.flush()

        group = (await session.scalars(
            select(Group).where(Group.openid == group_openid)
        )).first()
        if not group:
            group = Group(openid=group_openid, name=group_openid)
            session.add(group)
            await session.flush()

        task = await session.get(Task, task_id)

        if not task or task.group_id != group.id:
            return {"ok": False, "error": "任务不存在", "type": "complete"}
        if task.status == TaskStatus.PENDING.value:
            return {"ok": False, "error": "任务还没认领，先认领再完成", "type": "complete"}
        if task.status == TaskStatus.DONE.value:
            return {"ok": False, "error": "任务已经完成了", "type": "complete"}
        if task.assignee_openid != user_openid:
            return {"ok": False, "error": "只有认领人可以标记完成", "type": "complete"}

        early = bool(task.deadline and _now() < _aware(task.deadline))

        task.status = TaskStatus.DONE.value
        task.done_at = _now()

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
        return {
            "ok": True,
            "task_id": task.id,
            "title": task.title,
            "early": early,
            "score": pts,
            "type": "complete",
        }
    except Exception as e:
        await session.rollback()
        return {"ok": False, "error": f"完成失败：{e}", "type": "complete"}


async def drop_task(
    session,
    user_openid: str,
    group_openid: str,
    task_id: int,
) -> dict:
    """放弃任务。"""
    try:
        group = (await session.scalars(
            select(Group).where(Group.openid == group_openid)
        )).first()
        if not group:
            group = Group(openid=group_openid, name=group_openid)
            session.add(group)
            await session.flush()

        task = await session.get(Task, task_id)

        if not task or task.group_id != group.id:
            return {"ok": False, "error": "任务不存在", "type": "drop"}
        if task.status != TaskStatus.CLAIMED.value:
            return {"ok": False, "error": f"任务状态是 {task.status}，无法放弃", "type": "drop"}
        if task.assignee_openid != user_openid:
            return {"ok": False, "error": "只有认领人可以放弃", "type": "drop"}

        task.status = TaskStatus.PENDING.value
        task.assignee_openid = None
        task.claimed_at = None
        await session.commit()

        return {
            "ok": True,
            "task_id": task.id,
            "title": task.title,
            "type": "drop",
        }
    except Exception as e:
        await session.rollback()
        return {"ok": False, "error": f"放弃失败：{e}", "type": "drop"}


async def delete_task(
    session,
    user_openid: str,
    user_name: str,
    group_openid: str,
    task_id: int,
    is_admin: bool = False,
) -> dict:
    """删除任务。"""
    try:
        group = (await session.scalars(
            select(Group).where(Group.openid == group_openid)
        )).first()
        if not group:
            group = Group(openid=group_openid, name=group_openid)
            session.add(group)
            await session.flush()

        task = await session.get(Task, task_id)

        if not task or task.group_id != group.id:
            return {"ok": False, "error": "任务不存在", "type": "delete"}
        if not is_admin and task.publisher_openid != user_openid:
            return {"ok": False, "error": "只有发布者或组长可以删除", "type": "delete"}

        await session.delete(task)
        await session.commit()

        return {
            "ok": True,
            "task_id": task.id,
            "title": task.title,
            "type": "delete",
        }
    except Exception as e:
        await session.rollback()
        return {"ok": False, "error": f"删除失败：{e}", "type": "delete"}


async def list_tasks(
    session,
    group_openid: str,
) -> dict:
    """查看所有任务。"""
    group = (await session.scalars(
        select(Group).where(Group.openid == group_openid)
    )).first()
    if not group:
        group = Group(openid=group_openid, name=group_openid)
        session.add(group)
        await session.flush()

    tasks = (await session.scalars(
        select(Task).where(Task.group_id == group.id).order_by(Task.created_at.desc())
    )).all()

    if not tasks:
        return {"ok": True, "tasks": [], "type": "list"}

    result = []
    for t in tasks:
        status_label = {"pending": "待认领", "claimed": "进行中", "done": "已完成"}.get(t.status, t.status)
        dl = t.deadline.strftime("%m月%d日") if t.deadline else "未设"
        assignee_name = "待认领"

        if t.assignee_openid:
            assignee_user = (await session.scalars(
                select(User).where(User.openid == t.assignee_openid)
            )).first()
            assignee_name = assignee_user.name if assignee_user and assignee_user.name else t.assignee_openid

        result.append({
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "status_label": status_label,
            "deadline": dl,
            "assignee": assignee_name,
            "publisher": t.publisher_openid,
        })

    return {"ok": True, "tasks": result, "type": "list"}


async def task_detail(
    session,
    task_id: int,
    group_openid: str,
) -> dict:
    """查看任务详情。"""
    group = (await session.scalars(
        select(Group).where(Group.openid == group_openid)
    )).first()
    if not group:
        group = Group(openid=group_openid, name=group_openid)
        session.add(group)
        await session.flush()

    task = await session.get(Task, task_id)

    if not task or task.group_id != group.id:
        return {"ok": False, "error": "任务不存在", "type": "detail"}

    publisher_user = (await session.scalars(
        select(User).where(User.openid == task.publisher_openid)
    )).first()
    assignee_user = None
    if task.assignee_openid:
        assignee_user = (await session.scalars(
            select(User).where(User.openid == task.assignee_openid)
        )).first()

    status_label = {"pending": "待认领", "claimed": "进行中", "done": "已完成"}.get(task.status, task.status)
    dl = task.deadline.strftime("%Y-%m-%d %H:%M") if task.deadline else "未设"
    created = task.created_at.strftime("%Y-%m-%d %H:%M") if task.created_at else "无"

    extra = ""
    if task.status == TaskStatus.DONE.value and task.done_at and task.deadline:
        extra = "提前完成 ✨" if _aware(task.done_at) < _aware(task.deadline) else "按时完成"

    return {
        "ok": True,
        "id": task.id,
        "title": task.title,
        "description": task.description or "无",
        "status": task.status,
        "status_label": status_label,
        "publisher": publisher_user.name if publisher_user else task.publisher_openid,
        "assignee": assignee_user.name if assignee_user else "无",
        "deadline": dl,
        "created": created,
        "extra": extra,
        "type": "detail",
    }


async def my_tasks(
    session,
    user_openid: str,
    group_openid: str,
) -> dict:
    """查看当前用户的任务。"""
    group = (await session.scalars(
        select(Group).where(Group.openid == group_openid)
    )).first()
    if not group:
        group = Group(openid=group_openid, name=group_openid)
        session.add(group)
        await session.flush()

    tasks = (await session.scalars(
        select(Task).where(
            and_(Task.group_id == group.id, Task.assignee_openid == user_openid)
        ).order_by(Task.deadline)
    )).all()

    if not tasks:
        return {"ok": True, "tasks": [], "type": "my_tasks"}

    result = []
    for t in tasks:
        status_label = {"claimed": "进行中", "done": "已完成"}.get(t.status, "待认领")
        dl = t.deadline.strftime("%m月%d日") if t.deadline else "无"
        result.append({
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "status_label": status_label,
            "deadline": dl,
        })

    return {"ok": True, "tasks": result, "type": "my_tasks"}


async def my_contribution(
    session,
    user_openid: str,
    user_name: str,
    group_openid: str,
) -> dict:
    """查看个人贡献。"""
    user = (await session.scalars(
        select(User).where(User.openid == user_openid)
    )).first()
    if not user:
        user = User(openid=user_openid, name=user_name)
        session.add(user)
        await session.flush()

    group = (await session.scalars(
        select(Group).where(Group.openid == group_openid)
    )).first()
    if not group:
        group = Group(openid=group_openid, name=group_openid)
        session.add(group)
        await session.flush()

    contrib = (await session.scalars(
        select(Contribution).where(
            and_(Contribution.user_id == user.id, Contribution.group_id == group.id)
        )
    )).first()

    if not contrib:
        contrib = Contribution(user_id=user.id, group_id=group.id)
        session.add(contrib)
        await session.flush()

    rank = ((await session.scalar(
        select(func.count(Contribution.id)).where(
            and_(Contribution.group_id == group.id, Contribution.score > contrib.score)
        )
    )) or 0) + 1

    total = (await session.scalar(
        select(func.count(Contribution.id)).where(Contribution.group_id == group.id)
    )) or 0

    return {
        "ok": True,
        "name": user.name or user_name or user_openid,
        "completed": contrib.completed_count,
        "early": contrib.early_count,
        "score": contrib.score,
        "auto_assigned": contrib.auto_assigned_count,
        "rank": rank,
        "total": total,
        "type": "contribution",
    }


async def ranking(
    session,
    group_openid: str,
    limit: int = 10,
) -> dict:
    """查看贡献排名。"""
    group = (await session.scalars(
        select(Group).where(Group.openid == group_openid)
    )).first()
    if not group:
        group = Group(openid=group_openid, name=group_openid)
        session.add(group)
        await session.flush()

    results = (await session.execute(
        select(Contribution, User.name)
        .join(User, Contribution.user_id == User.id)
        .where(Contribution.group_id == group.id)
        .order_by(Contribution.score.desc())
        .limit(limit)
    )).all()

    if not results:
        return {"ok": True, "rankings": [], "type": "ranking"}

    result = []
    for i, (c, name) in enumerate(results, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f" {i}.")
        result.append({
            "rank": i,
            "medal": medal,
            "name": name or "未知",
            "completed": c.completed_count,
            "early": c.early_count,
            "score": c.score,
        })

    return {"ok": True, "rankings": result, "type": "ranking"}


async def build_context(
    session,
    group_openid: str,
    user_openid: str,
    user_name: str,
) -> str:
    """构建上下文摘要，供 LLM 读取."""
    group = (await session.scalars(
        select(Group).where(Group.openid == group_openid)
    )).first()
    if not group:
        group = Group(openid=group_openid, name=group_openid)
        session.add(group)
        await session.flush()

    lines = []

    # 小组成员及工作量
    members_in_group = (await session.execute(
        select(User, func.count(Task.id).label("task_count"))
        .join(Task, Task.assignee_openid == User.openid, isouter=True)
        .where(Task.group_id == group.id, Task.status == TaskStatus.CLAIMED.value)
        .group_by(User.id)
    )).all()

    if members_in_group:
        lines.append("小组成员当前工作量：")
        for m, count in members_in_group:
            role = "(组长)" if m.openid == group.leader_openid else ""
            lines.append(f"  - {m.name or m.openid} {role}：{count}个进行中任务")
    else:
        lines.append("小组成员：暂无记录")

    # 当前用户任务
    my_active = (await session.scalars(
        select(Task).where(
            and_(
                Task.group_id == group.id,
                Task.assignee_openid == user_openid,
                Task.status == TaskStatus.CLAIMED.value,
            )
        )
    )).all()
    if my_active:
        lines.append("\n你的进行中任务：")
        for t in my_active:
            dl = t.deadline.strftime("%m月%d日") if t.deadline else "无"
            lines.append(f"  - #{t.id}「{t.title}」截止{dl}")
    else:
        lines.append("\n你没有进行中的任务")

    # 所有未完成任务
    pending = (await session.scalars(
        select(Task).where(
            and_(Task.group_id == group.id, Task.status == TaskStatus.PENDING.value)
        ).order_by(Task.created_at.desc())
    )).all()
    if pending:
        lines.append(f"\n待认领任务（{len(pending)}个）：")
        for t in pending[:5]:
            age_h = (datetime.utcnow() - t.created_at.replace(tzinfo=None)).total_seconds() / 3600
            age_str = f"（挂了{int(age_h)}小时）" if age_h > 24 else ""
            lines.append(f"  - #{t.id}「{t.title}」{age_str}")
        if len(pending) > 5:
            lines.append(f"  ...还有{len(pending)-5}个")
    else:
        lines.append("\n目前没有待认领任务")

    # 最近完成的任务
    recent = (await session.scalars(
        select(Task).where(
            and_(Task.group_id == group.id, Task.status == TaskStatus.DONE.value)
        ).order_by(Task.done_at.desc())
    )).all()
    if recent:
        lines.append("\n最近完成的任务：")
        for t in recent[:3]:
            who = t.assignee_openid or "?"
            who_name = (await session.scalars(
                select(User).where(User.openid == who)
            )).first()
            lines.append(f"  - 「{t.title}」by {who_name.name if who_name else who}")

    lines.append(f"\n你是{'组长' if user_openid == group.leader_openid else '组员'}")

    return "\n".join(lines)
