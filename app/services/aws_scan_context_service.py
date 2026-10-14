from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.repositories.aws_scan_repository import AWSScanRepository
from app.services.aws_inventory_service import AWSInventoryService, scan_to_context


class AWSScanContextService:
    def __init__(self):
        self._scans = AWSScanRepository()
        self._inventory = AWSInventoryService()

    async def load_scan_context(
        self,
        db: AsyncSession,
        *,
        user: User,
        workspace_id: int,
        scan_id: str,
    ) -> dict:
        scan = await self._scans.find_by_scan_id(
            db,
            scan_id=scan_id,
            workspace_id=workspace_id,
        )
        if scan is None:
            raise HTTPException(status_code=404, detail="AWS scan not found in this workspace.")
        if scan.status != "COMPLETED":
            raise HTTPException(status_code=409, detail="AWS scan is not completed.")
        return {
            "workspace_id": scan.workspace_id,
            "scan_id": scan.scan_id,
            "status": scan.status,
            "is_active": bool(scan.is_active),
            "activated_at": scan.activated_at,
            "started_at": scan.started_at,
            "completed_at": scan.completed_at,
            "account": scan.account_json,
            "summary": scan.summary_json,
            "regions": scan.regions_json,
            "zones": scan.zones_json,
            "resources": scan.resources_json,
            "billing": scan.billing_json,
            "warnings": scan.warnings_json,
            "timings": scan.timings_json,
            "aws_connection_id": scan.aws_connection_id,
        }

    async def load_latest_context(
        self, db: AsyncSession, *, workspace_id: int, connection_id: int,
    ) -> dict:
        latest = await self._inventory.ensure_latest(
            db, workspace_id=workspace_id, connection_id=connection_id,
        )
        # The revision JSON is already the effective view with user edits.
        result = dict(latest.context_json)
        result['excluded_services'] = sorted(
            service for service, action in ((latest.overrides_json or {}).get('services') or {}).items()
            if action == 'EXCLUDE'
        )
        result['inventory_revision'] = latest.revision
        result['inventory_revision_id'] = latest.id
        result['last_inventory_update_at'] = latest.changed_at.isoformat()
        result['last_inventory_update_source'] = latest.change_type
        return result
