from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.aws_inventory_revision import AWSInventoryRevision, AWSLiveEvidence
from app.db.models.aws_scan import AWSScan
from app.repositories.aws_scan_repository import AWSScanRepository
from app.services.aws_evidence_enrichment import apply_aws_enrichments
from app.services.aws_generic_evidence import generic_api_enrichments


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resource_key(resource: dict) -> str:
    arn = str(resource.get('arn') or '').strip()
    if arn:
        return 'arn:' + arn.lower()
    service = str(resource.get('service') or '').lower().strip()
    region = str(resource.get('region') or '').lower().strip()
    rid = str(resource.get('resource_id') or '').lower().strip()
    if not service or not rid:
        raise ValueError('A resource needs an ARN or both service and resource_id.')
    return f'resource:{service}:{region}:{rid}'


def scan_to_context(scan: AWSScan) -> dict:
    return {
        'workspace_id': scan.workspace_id, 'aws_connection_id': scan.aws_connection_id,
        'scan_id': scan.scan_id, 'status': scan.status,
        'started_at': scan.started_at.isoformat() if scan.started_at else None,
        'completed_at': scan.completed_at.isoformat() if scan.completed_at else None,
        'account': copy.deepcopy(scan.account_json or {}),
        'summary': copy.deepcopy(scan.summary_json or {}),
        'regions': copy.deepcopy(scan.regions_json or {}),
        'zones': copy.deepcopy(scan.zones_json or {}),
        'resources': copy.deepcopy(scan.resources_json or {}),
        'billing': copy.deepcopy(scan.billing_json or {}),
        'warnings': copy.deepcopy(scan.warnings_json or []),
        'timings': copy.deepcopy(scan.timings_json or {}),
    }


def make_effective(base: dict, overrides: dict) -> dict:
    result = copy.deepcopy(base)
    resources = result.setdefault('resources', {})
    initial = [dict(r, data_source='AWS_DISCOVERY') for r in resources.get('resources', []) if isinstance(r, dict)]
    by_key = {}
    for index, row in enumerate(initial):
        if row.get('arn') or (row.get('service') and row.get('resource_id')):
            key = resource_key(row)
        else:
            digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
            key = f'unidentified:{index}:{digest}'
        by_key[key] = row

    for key, entry in (overrides.get('resources') or {}).items():
        if entry['action'] == 'EXCLUDE':
            by_key.pop(key, None)
        elif entry['action'] == 'UPSERT':
            original = by_key.get(key, {})
            submitted = copy.deepcopy(entry['value'])
            by_key[key] = {**original, **submitted, 'data_source': 'USER_UPDATED' if original else 'USER_ADDED'}

    # Recompute summaries from the effective resource set instead of reusing stale scan counts.
    rows = list(by_key.values())
    excluded_services = {k for k,v in (overrides.get('services') or {}).items() if v == 'EXCLUDE'}
    rows = [r for r in rows if str(r.get('service') or '').lower() not in excluded_services]
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {'resource_count': 0, 'regions': set()})
    for row in rows:
        name = str(row.get('service') or '').lower().strip()
        if not name:
            continue
        groups[name]['resource_count'] += 1
        if row.get('region'):
            groups[name]['regions'].add(row['region'])
    sources = defaultdict(set)
    for row in rows:
        if row.get('service'):
            sources[str(row['service']).lower()].add(row.get('data_source', 'AWS_DISCOVERY'))
    for name, action in (overrides.get('services') or {}).items():
        if action == 'ADD' and name not in excluded_services:
            _ = groups[name]
    resources['resources'] = sorted(rows, key=lambda r: (str(r.get('service')), str(r.get('region')), str(r.get('resource_id'))))
    resources['total_resources'] = len(rows)
    resources['used_regions'] = sorted({r['region'] for r in rows if r.get('region')})
    resources['detected_services'] = [
        {'service': name, 'resource_count': val['resource_count'], 'regions': sorted(val['regions']),
         'data_source': ('USER_ADDED' if val['resource_count'] == 0 else
                         next(iter(sources[name])) if len(sources[name]) == 1 else 'MIXED')}
        for name, val in sorted(groups.items())
    ]
    summary = result.setdefault('summary', {})
    summary['discovered_resources'] = len(rows)
    summary['detected_services'] = len(groups)
    summary['used_regions'] = len(resources['used_regions'])
    # These legacy billing/classification counters must not leak into summaries.
    for obsolete in ('primary_paid_services', 'zero_cost_services', 'supporting_services', 'paid_regions'):
        summary.pop(obsolete, None)
    # Resource-level AWS configuration enrichments are independent of user edits.
    # Apply them last, so no manual metadata or source snapshot is overwritten.
    result['_excluded_services'] = overrides.get('services') or {}
    return apply_aws_enrichments(result, overrides.get('aws_enrichments') or {})


# Avoid persisting AWS-returned secrets and credentials as generic MCP evidence.
_SENSITIVE = re.compile(r'(secret|password|passwd|private.?key|access.?key|session.?token|authorization|credential|auth.?token|^body$|^content$)', re.I)


def sanitize_evidence(value: Any, depth: int = 0) -> Any:
    if depth > 15:
        return '[MAX_DEPTH]'
    if isinstance(value, dict):
        return {str(k): ('[REDACTED]' if _SENSITIVE.search(str(k)) else sanitize_evidence(v, depth + 1)) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_evidence(v, depth + 1) for v in value]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value if not isinstance(value, str) or len(value) <= 15000 else '[OVERSIZED_FIELD]'
    return str(value)


def intent_key(intent: dict) -> str:
    raw = intent.get('params_json') or '{}'
    try:
        params = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        params = raw
    canonical = {'service': str(intent.get('service') or '').lower(),
                 'operation': intent.get('operation'), 'region': intent.get('region'),
                 'resource_id': intent.get('resource_id'), 'params': params}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()


class AWSInventoryService:
    def __init__(self):
        self._scans = AWSScanRepository()

    async def latest(self, db: AsyncSession, *, workspace_id: int, connection_id: int) -> AWSInventoryRevision | None:
        result = await db.execute(select(AWSInventoryRevision).where(
            AWSInventoryRevision.workspace_id == workspace_id,
            AWSInventoryRevision.aws_connection_id == connection_id,
        ).order_by(AWSInventoryRevision.revision.desc()).limit(1))
        return result.scalar_one_or_none()

    async def ensure_latest(self, db: AsyncSession, *, workspace_id: int, connection_id: int) -> AWSInventoryRevision:
        latest = await self.latest(db, workspace_id=workspace_id, connection_id=connection_id)
        if latest:
            return latest
        result = await db.execute(select(AWSScan).where(
            AWSScan.workspace_id == workspace_id, AWSScan.aws_connection_id == connection_id,
            AWSScan.status == 'COMPLETED',
        ).order_by(AWSScan.started_at.desc(), AWSScan.id.desc()).limit(1))
        scan = result.scalar_one_or_none()
        if scan is None:
            raise HTTPException(404, 'No completed AWS discovery scan exists for this connection.')
        seeded = await self.register_scan(db, scan=scan, user_id=None)
        await db.commit()
        return seeded

    async def register_scan(self, db: AsyncSession, *, scan: AWSScan, user_id: int | None) -> AWSInventoryRevision:
        latest = await self.latest(db, workspace_id=scan.workspace_id, connection_id=scan.aws_connection_id)
        if latest and latest.base_scan_id == scan.scan_id:
            return latest
        overrides = copy.deepcopy(latest.overrides_json) if latest else {'resources': {}, 'services': {}}
        # A new Auto Scan starts a fresh evidence coverage window, while the
        # user's service additions/exclusions remain unchanged.
        overrides.pop('aws_enrichments', None)
        effective = make_effective(scan_to_context(scan), overrides)
        row = AWSInventoryRevision(
            workspace_id=scan.workspace_id, aws_connection_id=scan.aws_connection_id,
            base_scan_id=scan.scan_id, revision=(latest.revision + 1 if latest else 1),
            change_type='AUTO_SCAN', changed_by_user_id=user_id, changed_at=utc_now(),
            context_json=effective, overrides_json=overrides,
            change_json={'source': 'AWS_SCAN', 'scan_id': scan.scan_id, 'reconciled_overrides': len(overrides.get('resources', {}))},
        )
        db.add(row)
        await db.flush()
        return row

    async def edit(self, db: AsyncSession, *, workspace_id: int, connection_id: int,
                   user_id: int, action: str, service: str | None = None,
                   resource: dict | None = None, key: str | None = None) -> AWSInventoryRevision:
        latest = await self.ensure_latest(db, workspace_id=workspace_id, connection_id=connection_id)
        overrides = copy.deepcopy(latest.overrides_json or {'resources': {}, 'services': {}})
        overrides.setdefault('resources', {})
        overrides.setdefault('services', {})
        base = copy.deepcopy(latest.context_json)
        # Rebuild from the immutable source scan: user overrides are not applied twice.
        scan = await self._scans.find_by_scan_id(db, scan_id=latest.base_scan_id, workspace_id=workspace_id)
        if scan is None:
            raise HTTPException(409, 'Base AWS scan no longer exists.')
        resource = sanitize_evidence(resource) if resource else resource
        before: Any = None
        after: Any = None
        if action in ('ADD_SERVICE', 'EXCLUDE_SERVICE', 'RESTORE_SERVICE'):
            name = str(service or '').lower().strip()
            if not name or not re.fullmatch(r'[a-z0-9-]{2,100}', name):
                raise HTTPException(422, 'A valid AWS service namespace is required.')
            before = overrides['services'].get(name)
            if action == 'RESTORE_SERVICE':
                overrides['services'].pop(name, None)
            else:
                overrides['services'][name] = 'ADD' if action == 'ADD_SERVICE' else 'EXCLUDE'
            after = overrides['services'].get(name)
        elif action in ('UPSERT_RESOURCE', 'EXCLUDE_RESOURCE', 'RESTORE_RESOURCE'):
            if action == 'UPSERT_RESOURCE':
                if not resource:
                    raise HTTPException(422, 'Resource data is required.')
                try:
                    key = resource_key(resource)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
            if not key:
                raise HTTPException(422, 'Resource key is required.')
            before = overrides['resources'].get(key)
            if action == 'RESTORE_RESOURCE':
                overrides['resources'].pop(key, None)
            else:
                overrides['resources'][key] = ({'action': 'EXCLUDE'} if action == 'EXCLUDE_RESOURCE'
                                               else {'action': 'UPSERT', 'value': resource})
            after = overrides['resources'].get(key)
        else:
            raise HTTPException(422, 'Unsupported inventory action.')
        effective = make_effective(scan_to_context(scan), overrides)
        row = AWSInventoryRevision(
            workspace_id=workspace_id, aws_connection_id=connection_id,
            base_scan_id=latest.base_scan_id, revision=latest.revision + 1,
            change_type='USER_UPDATE', changed_by_user_id=user_id, changed_at=utc_now(),
            context_json=effective, overrides_json=overrides,
            change_json={'action': action, 'target': key or service, 'before': before, 'after': after},
        )
        db.add(row)
        await db.flush()
        return row

    async def get_cached(self, db: AsyncSession, *, workspace_id: int, connection_id: int,
                         intent: dict) -> dict | None:
        # A fresh AWS scan starts a new evidence window. Manual service edits
        # do not change AWS configuration and should not discard validated reads.
        latest = await self.latest(db, workspace_id=workspace_id, connection_id=connection_id)
        excluded = ((latest.overrides_json or {}).get('services') or {}) if latest else {}
        if excluded.get(str(intent.get('service') or '').lower()) == 'EXCLUDE':
            return None
        boundary_result = await db.execute(select(AWSInventoryRevision).where(
            AWSInventoryRevision.workspace_id == workspace_id,
            AWSInventoryRevision.aws_connection_id == connection_id,
            AWSInventoryRevision.change_type == 'AUTO_SCAN',
        ).order_by(AWSInventoryRevision.revision.desc()).limit(1))
        boundary = boundary_result.scalar_one_or_none()
        predicates = [
            AWSLiveEvidence.workspace_id == workspace_id,
            AWSLiveEvidence.aws_connection_id == connection_id,
            AWSLiveEvidence.fetched_at >= utc_now() - timedelta(minutes=15),
        ]
        if boundary:
            predicates.append(AWSLiveEvidence.inventory_revision_id >= boundary.id)
        predicates.append(AWSLiveEvidence.query_key == intent_key(intent))
        result = await db.execute(select(AWSLiveEvidence).where(*predicates)
            .order_by(AWSLiveEvidence.fetched_at.desc(), AWSLiveEvidence.id.desc()).limit(100))
        records = result.scalars().all()
        # Incomplete regional coverage must not become a trusted cache hit.
        for row in records:
            data = row.evidence_json or {}
            if not data.get('failed_regions') and not data.get('warnings'):
                return copy.deepcopy(data)
        return None

    async def persist_live(self, db: AsyncSession, *, workspace_id: int, connection_id: int,
                           user_id: int, intent: dict, evidence: dict) -> AWSInventoryRevision:
        latest = await self.ensure_latest(db, workspace_id=workspace_id, connection_id=connection_id)
        cleaned = sanitize_evidence(evidence)
        fetched_at = utc_now()
        # Generic for all discovered AWS services.  Legacy S3 enrichment rows
        # remain readable via apply_aws_enrichments but are not special-cased.
        fresh_updates = generic_api_enrichments(
            intent, cleaned, inventory=latest.context_json,
            fetched_at=fetched_at.isoformat(),
        )
        overrides = copy.deepcopy(latest.overrides_json or {})
        enrichments = overrides.setdefault('aws_enrichments', {})
        excluded = {name for name, state in (overrides.get('services') or {}).items()
                    if state == 'EXCLUDE'}
        for key, update in fresh_updates.items():
            if update['service'] in excluded:
                continue
            previous = enrichments.get(key) or {}
            previous_fields = copy.deepcopy(previous.get('fields') or {})
            previous_fields.update(update['fields'])
            verified_times = copy.deepcopy(previous.get('field_verified_at') or {})
            verified_times.update(update.get('field_verified_at') or {})
            enrichments[key] = {**update, 'fields': previous_fields,
                                'field_verified_at': verified_times}
        # Always reconstruct from immutable discovery + overrides to prevent
        # duplicate/orphan resources and to retain user service exclusions.
        scan = await self._scans.find_by_scan_id(
            db, scan_id=latest.base_scan_id, workspace_id=workspace_id)
        if scan is None:
            raise HTTPException(409, 'Base AWS scan no longer exists.')
        effective = make_effective(scan_to_context(scan), overrides)
        effective['last_live_update'] = {
            'operation': intent.get('operation'), 'service': intent.get('service'),
            'query_key': intent_key(intent), 'fetched_at': fetched_at.isoformat(),
        }
        row = AWSInventoryRevision(
            workspace_id=workspace_id, aws_connection_id=connection_id,
            base_scan_id=latest.base_scan_id, revision=latest.revision + 1,
            change_type='LIVE_MCP_ENRICHMENT', changed_by_user_id=user_id,
            changed_at=fetched_at, context_json=effective, overrides_json=overrides,
            change_json={'operation': intent.get('operation'), 'service': intent.get('service'),
                         'region': intent.get('region'), 'query_key': intent_key(intent),
                         'enriched_resources': sorted(fresh_updates),
                         'source': 'AWS_MCP_LIVE'},
        )
        db.add(row)
        await db.flush()
        db.add(AWSLiveEvidence(
            workspace_id=workspace_id, aws_connection_id=connection_id,
            inventory_revision_id=row.id, query_key=intent_key(intent),
            intent_json=sanitize_evidence(intent), evidence_json=cleaned, fetched_at=fetched_at,
        ))
        await db.flush()
        return row
