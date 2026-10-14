from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class AWSInventoryRevision(Base):
    """Append-only effective inventory. The original AWSScan rows remain immutable."""

    __tablename__ = "aws_inventory_revisions"
    __table_args__ = (
        UniqueConstraint("aws_connection_id", "revision", name="uq_aws_inventory_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    aws_connection_id: Mapped[int] = mapped_column(ForeignKey("aws_connections.id", ondelete="CASCADE"), index=True)
    base_scan_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    change_type: Mapped[str] = mapped_column(String(40))
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    context_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    overrides_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    change_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class AWSLiveEvidence(Base):
    """Append-only cached MCP evidence, scoped to one AWS connection and query."""

    __tablename__ = "aws_live_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    aws_connection_id: Mapped[int] = mapped_column(ForeignKey("aws_connections.id", ondelete="CASCADE"), index=True)
    inventory_revision_id: Mapped[int] = mapped_column(ForeignKey("aws_inventory_revisions.id", ondelete="CASCADE"), index=True)
    query_key: Mapped[str] = mapped_column(String(64), index=True)
    intent_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
