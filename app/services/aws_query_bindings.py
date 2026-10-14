from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.aws_inventory_service import AWSInventoryService


def live_evidence_bindings(db: AsyncSession, *, workspace_id: int,
                           connection_id: int, user_id: int):
    """Workspace-scoped lookup/write callbacks shared by /aws/query and /chats/query."""
    inventory = AWSInventoryService()

    async def lookup(intent: dict) -> dict | None:
        return await inventory.get_cached(db, workspace_id=workspace_id,
                                          connection_id=connection_id, intent=intent)

    async def persist(intent: dict, evidence: dict) -> int:
        try:
            revision = await inventory.persist_live(
                db, workspace_id=workspace_id, connection_id=connection_id,
                user_id=user_id, intent=intent, evidence=evidence,
            )
            await db.commit()
            return revision.revision
        except Exception:
            await db.rollback()
            raise

    return lookup, persist
