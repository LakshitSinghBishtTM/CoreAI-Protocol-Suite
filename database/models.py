"""
CoreAI Protocol Suite - Models
SQLAlchemy ORM models.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String, Text, Float, Integer, Boolean,
    DateTime, JSON, ForeignKey, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ #
# request_logs — every completion request
# ------------------------------------------------------------------ #

class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="balanced")

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Truncated prompt hash for cache correlation — not the full prompt
    prompt_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    agent_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_request_logs_provider", "provider"),
        Index("ix_request_logs_created_at", "created_at"),
        Index("ix_request_logs_agent_id", "agent_id"),
    )

    def __repr__(self):
        return (
            f"<RequestLog {self.id[:8]} provider={self.provider} "
            f"model={self.model} cost=${self.cost_usd:.6f}>"
        )


# ------------------------------------------------------------------ #
# usage_daily — aggregated daily cost/usage per provider
# ------------------------------------------------------------------ #

class UsageDaily(Base):
    __tablename__ = "usage_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)        # YYYY-MM-DD
    provider: Mapped[str] = mapped_column(String(32), nullable=False)

    total_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    avg_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    p95_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_usage_daily_date_provider", "date", "provider", unique=True),
    )

    def __repr__(self):
        return (
            f"<UsageDaily {self.date} {self.provider} "
            f"requests={self.total_requests} cost=${self.total_cost_usd:.4f}>"
        )


# ------------------------------------------------------------------ #
# agents — registered agent registry
# ------------------------------------------------------------------ #

class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="agent")

    def __repr__(self):
        return f"<Agent {self.id} tasks={self.total_tasks}>"


# ------------------------------------------------------------------ #
# tasks — persistent agent task records
# ------------------------------------------------------------------ #

class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agents.id"), nullable=False
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending | running | completed | failed | cancelled

    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    agent: Mapped["Agent"] = relationship("Agent", back_populates="tasks")

    __table_args__ = (
        Index("ix_tasks_agent_id", "agent_id"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_created_at", "created_at"),
    )

    def duration_seconds(self) -> Optional[float]:
        if not self.started_at:
            return None
        end = self.completed_at or datetime.utcnow()
        return (end - self.started_at).total_seconds()

    def __repr__(self):
        return f"<Task {self.id[:8]} agent={self.agent_id} status={self.status}>"


# ------------------------------------------------------------------ #
# api_keys — if you expose CoreAI as a service with auth
# ------------------------------------------------------------------ #

class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requests_made: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_api_keys_key_hash", "key_hash"),
    )

    def __repr__(self):
        return f"<APIKey {self.id[:8]} owner={self.owner} active={self.active}>"
