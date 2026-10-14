"""Offline Phase 2 evidence tests (no AWS/Groq credentials, no network)."""
import ast
import copy
from datetime import datetime, timezone

from app.services.aws_inventory_service import make_effective, sanitize_evidence
from app.services.aws_evidence_enrichment import (
    s3_audit_enrichments, select_s3_cached_evidence, bucket_name,
)
# Load the dataclass and script-builder without importing external MCP runtime.
from pathlib import Path
from dataclasses import dataclass
import json

def _s3_executor_types():
    path = Path(__file__).resolve().parents[1] / 'app/providers/aws/audits/s3_security.py'
    source = path.read_text()
    tree = ast.parse(source)
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef)
             and n.name in ('S3SecurityAuditRequest', 'S3SecurityAuditExecutor')]
    # Drop execute method because it requires an external AWS MCP client type.
    for n in nodes:
        if n.name == 'S3SecurityAuditExecutor':
            n.body = [m for m in n.body if (isinstance(m, ast.Assign) or getattr(m, 'name', None) in ('_build_script', '_find_result'))]
    ns = {'__name__': __name__, 'dataclass': dataclass, 'json': json, 'Any': object}
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), ns)
    return ns['S3SecurityAuditRequest'], ns['S3SecurityAuditExecutor']

S3SecurityAuditRequest, S3SecurityAuditExecutor = _s3_executor_types()


def base():
    return {
        'scan_id': 'scan-1', 'status': 'COMPLETED',
        'resources': {'resources': [
            {'service': 's3', 'arn': 'arn:aws:s3:::erp-prod', 'resource_id': 'erp-prod', 'region': 'ap-south-1'},
            {'service': 'ec2', 'resource_id': 'i-7', 'region': 'ap-south-1'},
        ]}, 'summary': {},
    }


def audit(bucket='erp-prod', *, versioning=True, target=None):
    return {
        'audit_type': 'S3_SECURITY_AUDIT',
        'requirements': {'bucket_name': target, 'check_encryption': True, 'check_public_access': True,
                         'check_versioning': versioning, 'check_logging': True},
        'bucket_count': 1, 'buckets': [{
            'name': bucket, 'region': 'ap-south-1',
            'encryption': {'evidence_status': 'AVAILABLE', 'algorithm': 'AES256', 'posture': 'SSE_S3'},
            'public_access': {'posture': 'BLOCKED', 'public_access_block': {'BlockPublicAcls': True}},
            'versioning': {'status': 'Enabled'} if versioning else None,
            'logging': {'enabled': True}, 'affected': False, 'affected_reasons': [], 'warnings': [],
        }],
    }


def intent(target=None):
    import json
    return {'service': 's3', 'operation': 'S3SecurityAudit',
            'params_json': json.dumps({'bucket_name': target, 'check_encryption': True,
                                       'check_public_access': True, 'check_versioning': True,
                                       'check_logging': True})}


def test_live_s3_resource_fields_survive_manual_service_edit():
    payload = audit()
    enrichments = s3_audit_enrichments(intent(), payload, fetched_at='2026-10-13T09:00:00+00:00')
    assert len(enrichments) == 1
    overrides = {'services': {'ecs': 'ADD'}, 'resources': {}, 'aws_enrichments': enrichments}
    context = make_effective(base(), overrides)
    target = next(x for x in context['resources']['resources'] if x.get('resource_id') == 'erp-prod')
    assert target['aws_evidence']['fields']['encryption']['algorithm'] == 'AES256'
    assert target['aws_evidence']['fields']['versioning']['status'] == 'Enabled'
    assert context['summary']['detected_services'] == 3
    assert context['resources']['total_resources'] == 2
    assert any(r['service'] == 'ecs' for r in context['resources']['detected_services'])
    assert 'aws_evidence' not in base()['resources']['resources'][0]


def test_manually_excluded_service_must_not_reappear():
    overrides = {'services': {'s3': 'EXCLUDE'}, 'resources': {}, 'aws_enrichments':
                 s3_audit_enrichments(intent(), audit(), fetched_at='2026-10-13T09:00:00Z')}
    # Engine normally omits excluded updates before persisting, but even a
    # previous stored S3 enrichment cannot resurrect a user-excluded service.
    result = make_effective(base(), overrides)
    assert all(row['service'] != 's3' for row in result['resources']['resources'])


def test_generic_unrecognized_aws_api_does_not_mutate_resource_inventory():
    assert s3_audit_enrichments({'service': 'ec2', 'operation': 'DescribeInstances'},
                                {'regions': [{'result': 'ok'}]}, fetched_at='now') == {}


def test_cached_general_audit_answers_followup_for_one_bucket():
    evidence = select_s3_cached_evidence([audit()], intent('erp-prod'))
    assert evidence is not None
    assert evidence['bucket_count'] == 1
    assert evidence['evidence_reused'] is True
    assert evidence['buckets'][0]['encryption']['algorithm'] == 'AES256'


def test_partial_audit_does_not_claim_full_security_coverage():
    incomplete = audit(versioning=False)
    assert select_s3_cached_evidence([incomplete], intent('erp-prod')) is None


def test_bucket_specific_read_does_not_enumerate_all_buckets():
    req = S3SecurityAuditRequest(check_public_access=True, bucket_name='erp-prod')
    script = S3SecurityAuditExecutor._build_script(req)
    assert 'if config.get("bucket_name"):' in script
    assert 'unique_buckets = {config["bucket_name"]: {"Name": config["bucket_name"]}}' in script
    compile(script, 'mcp_script', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_redaction_applies_to_live_aws_evidence_before_save():
    raw = {'audit_type': 'S3_SECURITY_AUDIT', 'SecretAccessKey': 'DONT_STORE',
           'buckets': [{'name': 'erp-prod', 'encryption': {'algorithm': 'AES256'}}]}
    assert sanitize_evidence(raw)['SecretAccessKey'] == '[REDACTED]'


def test_s3_bucket_name_extraction_and_invalid_object_key():
    assert bucket_name('arn:aws:s3:::erp-prod') == 'erp-prod'
    assert bucket_name('erp-prod/object') is None
    assert bucket_name('') is None
