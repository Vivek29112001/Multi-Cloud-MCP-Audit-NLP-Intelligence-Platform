"""Phase 3 offline validation: service-agnostic APIs and resource attribution."""
import ast
import asyncio
import copy
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from app.ai.intent_models import AWSQueryIntent
from app.services.aws_generic_evidence import generic_api_enrichments
from app.services.aws_inventory_service import make_effective


def scan():
    return {
        'scan_id': 'test-scan', 'status': 'COMPLETED', 'summary': {},
        'resources': {'resources': [
            {'service': 'ec2', 'resource_id': 'i-example', 'region': 'us-west-2'},
            {'service': 'ec2', 'resource_id': 'i-other', 'region': 'us-west-2'},
            {'service': 'rds', 'resource_id': 'db-example', 'region': 'eu-west-1'},
            {'service': 'x-custom', 'arn': 'arn:aws:x-custom:eu-west-1:123:thing/test',
             'resource_id': 'thing/test', 'region': 'eu-west-1'},
        ]}
    }


def result(service, operation, region, pages, *, success=True):
    return {'service': service, 'operation': operation,
            'results': [{'region': region, 'success': success, 'data': pages}]}


def intent(service, operation, *, rid=None, params=None):
    return {'service': service, 'operation': operation, 'resource_id': rid,
            'params_json': json.dumps(params or {})}


def test_dynamic_instances_does_not_cross_contaminate_resources():
    data = result('ec2', 'DescribeInstances', 'us-west-2', [{
        'Reservations': [{'Instances': [
            {'InstanceId': 'i-example', 'State': {'Name': 'running'}},
            {'InstanceId': 'i-other', 'State': {'Name': 'stopped'}},
        ]}]
    }])
    enriched = generic_api_enrichments(
        intent('ec2', 'DescribeInstances'), data, inventory=scan(), fetched_at='2026-10-13T10:00:00Z')
    assert len(enriched) == 2
    effective = make_effective(scan(), {'services': {}, 'resources': {}, 'aws_enrichments': enriched})
    one = next(x for x in effective['resources']['resources'] if x.get('resource_id') == 'i-example')
    two = next(x for x in effective['resources']['resources'] if x.get('resource_id') == 'i-other')
    assert one['aws_evidence']['fields']['DescribeInstances'][0]['State']['Name'] == 'running'
    assert two['aws_evidence']['fields']['DescribeInstances'][0]['State']['Name'] == 'stopped'
    assert effective['resources']['total_resources'] == 4


def test_identity_free_targeted_response_maps_to_exact_known_resource():
    data = result('rds', 'DescribeDBParameters', 'eu-west-1', [{'Parameters': [{'ParameterName': 'abc'}]}])
    scoped = generic_api_enrichments(intent('rds', 'DescribeDBParameters', rid='db-example',
        params={'DBInstanceIdentifier': 'db-example'}), data, inventory=scan(), fetched_at='time')
    assert len(scoped) == 1
    assert next(iter(scoped.values()))['fields']['DescribeDBParameters']
    unscoped = generic_api_enrichments(intent('rds', 'DescribeDBParameters'), data,
                                       inventory=scan(), fetched_at='time')
    assert unscoped == {}


def test_dynamic_service_unknown_to_code_maps_using_arn():
    arn = 'arn:aws:x-custom:eu-west-1:123:thing/test'
    data = result('x-custom', 'GetThingConfiguration', 'eu-west-1', [{'Arn': arn, 'Encrypted': True}])
    updated = generic_api_enrichments(intent('x-custom', 'GetThingConfiguration'), data,
                                      inventory=scan(), fetched_at='time')
    assert len(updated) == 1
    assert next(iter(updated.values()))['fields']['GetThingConfiguration'][0]['Encrypted'] is True


def test_failed_region_and_other_region_never_attach():
    data = result('rds', 'DescribeDBInstances', 'us-east-1', [{'DBInstanceIdentifier': 'db-example'}])
    assert generic_api_enrichments(intent('rds','DescribeDBInstances'), data,
                                   inventory=scan(), fetched_at='time') == {}
    data['results'][0]['region'] = 'eu-west-1'
    data['results'][0]['success'] = False
    assert generic_api_enrichments(intent('rds','DescribeDBInstances'), data,
                                   inventory=scan(), fetched_at='time') == {}


def test_manual_service_exclusion_takes_precedence_over_mcp():
    data = result('ec2', 'DescribeInstances', 'us-west-2', [{'InstanceId': 'i-example', 'State': 'running'}])
    enrichments = generic_api_enrichments(intent('ec2','DescribeInstances'), data,
                                           inventory=scan(), fetched_at='time')
    effective = make_effective(scan(), {'resources': {}, 'services': {'ec2':'EXCLUDE'},
                                         'aws_enrichments': enrichments})
    assert all(r.get('service') != 'ec2' for r in effective['resources']['resources'])


def test_multi_operation_steps_parsing_and_bound():
    steps = [dict(service='rds', operation='DescribeDBInstances',
                  params_json='{"DBInstanceIdentifier":"db-example"}', resource_id='db-example')]
    plan = AWSQueryIntent(service='rds', operation='DescribeDBInstances', steps_json=json.dumps(steps))
    assert plan.get_steps()[0]['service'] == 'rds'
    assert plan.get_steps()[0]['params_json'] == '{"DBInstanceIdentifier":"db-example"}'
    assert AWSQueryIntent(service='rds', operation='SnapshotLookup').get_steps() == []
    try:
        AWSQueryIntent(service='rds', operation='DescribeDBInstances',
                       steps_json=json.dumps(steps * 9)).get_steps()
        assert False, 'Expected bounded operation plan'
    except ValueError:
        pass


def test_generic_enrichment_does_not_modify_original_scan():
    base = scan(); original = copy.deepcopy(base)
    data = result('rds','DescribeDBInstances','eu-west-1',[{'DBInstanceIdentifier':'db-example'}])
    updates = generic_api_enrichments(intent('rds','DescribeDBInstances'), data,
                                      inventory=base, fetched_at='time')
    _ = make_effective(base, {'resources': {}, 'services': {}, 'aws_enrichments': updates})
    assert base == original


def test_no_static_service_registry_in_generic_normalizer():
    source = (Path(__file__).resolve().parents[1] / 'app/services/aws_generic_evidence.py').read_text()
    assert 's3' not in source.lower()
    assert 'rds' not in source.lower()
    assert 'ec2' not in source.lower()


def test_snapshot_answer_receives_verified_generic_resource_fields():
    from app.ai.snapshot_evidence import build_snapshot_evidence
    payload = result('rds', 'DescribeDBInstances', 'eu-west-1', [
        {'DBInstanceIdentifier':'db-example', 'StorageEncrypted': True}])
    updates = generic_api_enrichments(intent('rds','DescribeDBInstances'), payload,
                                      inventory=scan(), fetched_at='2026-10-13T10:00:00Z')
    effective = make_effective(scan(), {'resources': {}, 'services': {}, 'aws_enrichments': updates})
    evidence = build_snapshot_evidence(scan_result=effective,
        selected_context={'service':'rds','resource_id':'db-example'},
        intent={'service':'rds','operation':'SnapshotLookup'})
    row = evidence['resources']['matching_resources'][0]
    assert row['aws_evidence']['fields']['DescribeDBInstances'][0]['StorageEncrypted'] is True
    assert row['aws_evidence']['field_verified_at']['DescribeDBInstances'] == '2026-10-13T10:00:00Z'
