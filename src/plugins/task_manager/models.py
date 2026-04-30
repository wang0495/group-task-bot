"""Database models and session management."""
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

# ----------------------------------------------------------------------
# Engine / Session singleton
# ----------------------------------------------------------------------
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine(db_url: str | None = None):
    global _engine
    if _engine is None:
        if db_url is None:
            # Default: data/task_manager.db relative to this file's parent
            from pathlib import Path
            data_dir = Path(__file__).parent.parent.parent / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_url = f"sqlite+aiosqlite:///{data_dir / 'task_manager.db'}"
        _engine = create_async_engine(db_url, echo=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(_get_engine(), expire_on_commit=False)
    return _session_factory


def get_session() -> AsyncSession:
    factory = get_session_factory()
    return factory()


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------
class TaskStatus(str, Enum):
    PENDING = "pending"    # 待认领
    CLAIMED = "claimed"     # 已认领
    DONE = "done"           # 已完成


# ----------------------------------------------------------------------
# Base
# ----------------------------------------------------------------------
from sqlalchemy.orm import declarative_base  # noqa: E402
Base = declarative_base()

# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    openid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    openid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    leader_openid: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"), index=True)
    publisher_openid: Mapped[str] = mapped_column(String(64), index=True)
    assignee_openid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.PENDING.value)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Contribution(Base):
    __tablename__ = "contributions"
    __table_args__ = (UniqueConstraint("user_id", "group_id", name="uq_user_group"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"), index=True)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    early_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_assigned_count: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[int] = mapped_column(Integer, default=0)


async def init_db() -> None:
    """Create all tables."""
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
