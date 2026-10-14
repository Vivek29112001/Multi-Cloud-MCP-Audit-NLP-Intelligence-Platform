"""Validated, versionable AWS live evidence normalization.

The immutable Auto Scan is never edited. This module only translates known AWS
MCP response shapes into a separate, attributed enrichment layer.
Unrecognized AWS API responses are retained in aws_live_evidence without
claiming to have updated resource configuration fields.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

S3_FIELDS = {
    'check_encryption': 'encryption',
    'check_public_access': 'public_access',
    'check_versioning': 'versioning',
    'check_logging': 'logging',
    'check_sensitive_indicators': 'sensitive_data_indicators',
}


def bucket_name(value: str | None) -> str | None:
    value = str(value or '').strip()
    if value.startswith('arn:aws:s3:::'):
        value = value.split(':::', 1)[1].split('/', 1)[0]
    # Do not accept an arbitrary object key as a bucket name.
    if '/' in value or not 3 <= len(value) <= 63:
        return None
    import re
    return value.lower() if re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*[a-zA-Z0-9]', value) else None


def s3_audit_enrichments(intent: dict, evidence: dict, *, fetched_at: str) -> dict[str, dict]:
    """Only recognized S3 bucket audit records enter the resource inventory."""
    if (intent.get('service') or '').lower() != 's3' or intent.get('operation') != 'S3SecurityAudit':
        return {}
    if not isinstance(evidence, dict) or evidence.get('audit_type') != 'S3_SECURITY_AUDIT':
        return {}
    requirements = evidence.get('requirements') or {}
    if not isinstance(requirements, dict) or not isinstance(evidence.get('buckets'), list):
        return {}
    updates: dict[str, dict] = {}
    for item in evidence['buckets']:
        if not isinstance(item, dict):
            continue
        name = bucket_name(item.get('name'))
        if not name:
            continue
        fields = {}
        for control, field in S3_FIELDS.items():
            if requirements.get(control) is True and item.get(field) is not None:
                fields[field] = copy.deepcopy(item[field])
        if not fields:
            continue
        # Unverified controls remain useful as partial evidence, but must not
        # create a newly "verified" resource when no AWS read succeeded.
        verified = any(
            (k == 'versioning' or k == 'logging') or
            (k == 'encryption' and isinstance(v, dict) and v.get('evidence_status') == 'AVAILABLE') or
            (k == 'public_access' and isinstance(v, dict) and v.get('posture') not in (None, 'UNKNOWN'))
            for k, v in fields.items()
        )
        if not verified and not item.get('region'):
            continue
        updates[f'arn:arn:aws:s3:::{name}'] = {
            'service': 's3', 'resource_id': name, 'arn': f'arn:aws:s3:::{name}',
            'region': item.get('region'), 'fields': fields,
            'field_verified_at': {k: fetched_at for k in fields},
            'source': 'AWS_MCP_LIVE', 'fetched_at': fetched_at,
        }
    return updates


def apply_aws_enrichments(context: dict, enrichments: dict) -> dict:
    """Overlay evidence without overwriting user fields or changing scan resource counts.

    If a valid AWS bucket is absent from the original discovery inventory, add
    it as LIVE_AWS_MCP_VERIFIED, unless that service was manually excluded.
    """
    from app.services.aws_inventory_service import resource_key
    out = copy.deepcopy(context)
    resources = out.setdefault('resources', {})
    rows = copy.deepcopy(resources.get('resources') or [])
    indexes = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        try:
            indexes[resource_key(row)] = i
        except ValueError:
            continue
    excluded_services = {
        name.lower() for name, action in
        ((out.get('_excluded_services') or {}).items()) if action == 'EXCLUDE'
    }
    for key, entry in (enrichments or {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get('fields'), dict):
            continue
        if str(entry.get('service') or '').lower() in excluded_services:
            continue
        if key in indexes:
            row = rows[indexes[key]]
        else:
            row = {
                'service': entry['service'], 'resource_id': entry['resource_id'],
                'arn': entry['arn'], 'region': entry.get('region'),
                'data_source': 'LIVE_AWS_MCP_VERIFIED',
            }
            indexes[key] = len(rows)
            rows.append(row)
        existing = row.get('aws_evidence') or {}
        updated_fields = copy.deepcopy(existing.get('fields') or {})
        field_verified = copy.deepcopy(existing.get('field_verified_at') or {})
        for field, value in entry['fields'].items():
            updated_fields[field] = copy.deepcopy(value)
            field_verified[field] = (entry.get('field_verified_at') or {}).get(field, entry['fetched_at'])
        row['aws_evidence'] = {
            'source': 'AWS_MCP_LIVE', 'fields': updated_fields,
            'field_verified_at': field_verified,
        }
    resources['resources'] = rows
    out.pop('_excluded_services', None)
    # Derive accurate counts once and only once from the final rows.
    from collections import defaultdict
    groups = defaultdict(lambda: {'count': 0, 'regions': set()})
    for item in rows:
        name = str(item.get('service') or '').lower()
        if not name:
            continue
        groups[name]['count'] += 1
        if item.get('region'):
            groups[name]['regions'].add(item['region'])
    previous_services = {
        old['service']: old for old in (resources.get('detected_services') or [])
        if isinstance(old, dict) and old.get('service')
    }
    for name in previous_services:
        if name not in groups:
            groups[name]  # Manually added service without physical resources
    resources['total_resources'] = len(rows)
    resources['used_regions'] = sorted({r.get('region') for r in rows if isinstance(r, dict) and r.get('region')})
    resources['detected_services'] = [
        {
            **previous_services.get(name, {}), 'service': name,
            'resource_count': state['count'], 'regions': sorted(state['regions']),
            'data_source': previous_services.get(name, {}).get('data_source')
                           or ('LIVE_AWS_MCP_VERIFIED' if state['count'] else 'USER_ADDED'),
        }
        for name, state in sorted(groups.items())
    ]
    summary = out.setdefault('summary', {})
    summary['discovered_resources'] = len(rows)
    summary['detected_services'] = len(groups)
    summary['used_regions'] = len(resources['used_regions'])
    return out


def select_s3_cached_evidence(records: list[dict], requested: dict) -> dict | None:
    """Reuse earlier S3 posture evidence for a subset of checks on the same bucket.

    For account-wide questions require an account-wide cached audit and matching
    requested control coverage. Targeted questions may reuse account-wide reads.
    """
    params = requested.get('params_json') or '{}'
    import json
    try:
        wanted = json.loads(params) if isinstance(params, str) else params
    except (ValueError, TypeError):
        return None
    if not isinstance(wanted, dict):
        return None
    target = bucket_name(wanted.get('bucket_name')) if wanted.get('bucket_name') else None
    if wanted.get('bucket_name') and target is None:
        return None
    checks = [key for key in S3_FIELDS if wanted.get(key) is True]
    if not checks:
        return None
    by_bucket: dict[str, dict] = {}
    coverage: dict[str, set] = {}
    # Caller orders newest first; first usable value for each control wins.
    for record in records:
        if record.get('audit_type') != 'S3_SECURITY_AUDIT':
            continue
        requirements = record.get('requirements') or {}
        if not isinstance(requirements, dict):
            continue
        record_target = bucket_name(requirements.get('bucket_name')) if requirements.get('bucket_name') else None
        if not target and record_target:
            continue  # Single-bucket evidence cannot establish account-wide coverage.
        for item in record.get('buckets') or []:
            if not isinstance(item, dict):
                continue
            name = bucket_name(item.get('name'))
            if not name or (target and name != target):
                continue
            merged = by_bucket.setdefault(name, copy.deepcopy(item))
            covered = coverage.setdefault(name, set())
            for control in checks:
                field = S3_FIELDS[control]
                if control in covered:
                    continue
                if requirements.get(control) is True and item.get(field) is not None:
                    merged[field] = copy.deepcopy(item[field])
                    covered.add(control)
    if not by_bucket or (target and target not in by_bucket):
        return None
    if any(not set(checks).issubset(coverage.get(name, set())) for name in by_bucket):
        return None
    if not target and not any(not (r.get('requirements') or {}).get('bucket_name') for r in records if r.get('audit_type') == 'S3_SECURITY_AUDIT'):
        return None
    buckets = [by_bucket[name] for name in sorted(by_bucket)]
    # Recompute scope-specific findings; historical unrelated controls must
    # not turn an encryption-only follow-up into a false-positive audit.
    reason_map = {
        'check_public_access': {'PUBLIC_ACCESS_DETECTED'},
        'check_versioning': {'VERSIONING_NOT_ENABLED'},
        'check_logging': {'SERVER_ACCESS_LOGGING_DISABLED'},
        'check_encryption': {'DEFAULT_ENCRYPTION_NOT_KMS',
                             'DEFAULT_ENCRYPTION_NOT_APPROVED_KMS',
                             'ENCRYPTION_EVIDENCE_UNAVAILABLE'},
    }
    allowed = set().union(*(v for k, v in reason_map.items() if wanted.get(k))) if checks else set()
    for item in buckets:
        item['affected_reasons'] = [r for r in (item.get('affected_reasons') or []) if r in allowed]
        item['affected'] = bool(item['affected_reasons'])
    return {
        'audit_type': 'S3_SECURITY_AUDIT', 'scope': 'BUCKET' if target else 'ACCOUNT',
        'requirements': wanted, 'bucket_count': len(buckets),
        'affected_bucket_count': sum(bool(b.get('affected')) for b in buckets),
        'buckets': buckets, 'warnings': [],
        'evidence_reused': True,
    }
