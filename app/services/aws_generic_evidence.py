"""Schema-agnostic, conservative AWS MCP evidence-to-resource mapping.

No resource type / service inventory / response-field allowlist is embedded here.
An AWS API response is attributed to an existing discovered resource only if
its identity is proven by the request parameters or by the response itself.
Unknown or unscoped data still belongs in append-only AWSLiveEvidence, never in
an invented inventory resource.
"""
from __future__ import annotations

import copy
from typing import Any



def _scalars(value: Any, depth: int = 0):
    if depth > 10:
        return
    if isinstance(value, dict):
        for v in value.values():
            yield from _scalars(v, depth + 1)
    elif isinstance(value, list):
        for v in value[:10000]:
            yield from _scalars(v, depth + 1)
    elif isinstance(value, str):
        yield value.lower()


def _matching_containers(value: Any, identifiers: set[str], depth: int = 0) -> list[dict]:
    """Return minimal matching dictionaries; don't attach whole multi-resource pages."""
    if depth > 14:
        return []
    if isinstance(value, list):
        result = []
        for v in value[:10000]:
            result.extend(_matching_containers(v, identifiers, depth + 1))
        return result
    if not isinstance(value, dict):
        return []
    nested = []
    for item in value.values():
        if isinstance(item, (dict, list)):
            nested.extend(_matching_containers(item, identifiers, depth + 1))
    if nested:
        return nested
    # Check scalar values directly owned by the same object. An unrelated
    # sibling resource cannot establish identity for this dictionary.
    immediate = {v.lower() for v in value.values() if isinstance(v, str)}
    if identifiers & immediate:
        return [value]
    return []


def generic_api_enrichments(
    intent: dict, evidence: dict, *, inventory: dict, fetched_at: str,
) -> dict[str, dict]:
    """Map an arbitrary successful read-only API response to known resources.

    The stored field is the actual AWS operation name (e.g. DescribeDBInstances),
    not a fabricated generic security score or static service classification.
    """
    service = str(intent.get('service') or '').strip().lower()
    operation = str(intent.get('operation') or '').strip()
    if not service or not operation or not isinstance(evidence, dict):
        return {}
    if service != str(evidence.get('service') or '').lower() or operation != evidence.get('operation'):
        return {}
    results = evidence.get('results')
    if not isinstance(results, list):
        return {}
    import json
    try:
        params = intent.get('params_json') or '{}'
        params = json.loads(params) if isinstance(params, str) else params
        if not isinstance(params, dict):
            return {}
    except (ValueError, TypeError):
        return {}
    explicit = str(intent.get('resource_id') or '').strip().lower()
    inventory_rows = (inventory.get('resources') or {}).get('resources') or []
    updates = {}
    for row in inventory_rows:
        if not isinstance(row, dict) or str(row.get('service') or '').lower() != service:
            continue
        identifiers = {str(row.get(k) or '').lower() for k in ('resource_id', 'arn')}
        identifiers.discard('')
        if not identifiers or (explicit and explicit not in identifiers):
            continue
        # Only a request that explicitly selects this one resource may use
        # identity-free output (Get* often omits the input identifier).
        request_scoped = bool(identifiers.intersection(set(_scalars(params))))
        for regional in results:
            if not isinstance(regional, dict) or regional.get('success') is not True:
                continue
            region = regional.get('region')
            known_region = row.get('region')
            if known_region and region and known_region not in (region, 'global'):
                continue
            payload = regional.get('data')
            # A provider-returned error dictionary is not positive evidence,
            # even if the outer MCP wrapper reports a successful tool call.
            if isinstance(payload, dict) and any(k in payload for k in ('Error', 'error', 'errors')):
                continue
            matched = _matching_containers(payload, identifiers)
            # Identity-free output can only be attributed if the request
            # uniquely specified an existing resource and returned a result.
            if not matched and not request_scoped:
                continue
            selected = matched if matched else payload
            try:
                from app.services.aws_inventory_service import resource_key
                key = resource_key(row)
            except ValueError:
                continue
            updates[key] = {
                'service': service,
                'resource_id': row.get('resource_id'),
                'arn': row.get('arn'),
                'region': known_region or region,
                'fields': {operation: copy.deepcopy(selected)},
                'field_verified_at': {operation: fetched_at},
                'source': 'AWS_MCP_LIVE',
                'fetched_at': fetched_at,
            }
    return updates
