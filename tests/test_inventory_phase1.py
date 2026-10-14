"""Offline regression tests: no real AWS credentials or network calls."""
from sqlalchemy import create_engine, inspect
from app.db.base import Base
import app.db.models.workspace  # required FK table
import app.db.models  # register the full metadata
from app.services.aws_inventory_service import (
    make_effective, resource_key, sanitize_evidence, intent_key,
)


def baseline():
    return {
        'account': {'account_id': '111122223333'},
        'summary': {'detected_services': 2, 'discovered_resources': 2,
                    'primary_paid_services': 1},
        'resources': {'resources': [
            {'service': 'ec2', 'region': 'ap-south-1', 'resource_id': 'i-1'},
            {'service': 's3', 'region': 'ap-south-1', 'resource_id': 'bucket-x'},
        ], 'total_resources': 2},
    }


def test_dynamic_service_and_resource_inventory_has_no_classification():
    result = make_effective(baseline(), {'resources': {}, 'services': {}})
    assert result['resources']['total_resources'] == 2
    assert {s['service'] for s in result['resources']['detected_services']} == {'ec2', 's3'}
    assert 'primary_paid_services' not in result['summary']
    assert all('category' not in row for row in result['resources']['detected_services'])


def test_user_edit_and_exclusion_persist_across_new_auto_scan():
    overrides = {'services': {'ecs': 'ADD'}, 'resources': {
        'resource:ec2:ap-south-1:i-1': {'action': 'EXCLUDE'},
        'resource:rds:us-east-1:db-9': {'action': 'UPSERT', 'value':
            {'service': 'rds', 'region': 'us-east-1', 'resource_id': 'db-9'}},
    }}
    result = make_effective(baseline(), overrides)
    assert result['resources']['total_resources'] == 2
    services = {r['service']: r for r in result['resources']['detected_services']}
    assert set(services) == {'rds', 's3', 'ecs'}
    assert services['ecs']['resource_count'] == 0
    assert any(r['data_source'] == 'USER_ADDED' for r in result['resources']['resources'])
    refreshed = baseline()
    refreshed['resources']['resources'].append({'service': 'lambda', 'resource_id': 'func'})
    result2 = make_effective(refreshed, overrides)
    assert {r['service'] for r in result2['resources']['resources']} == {'s3', 'rds', 'lambda'}


def test_sensitive_aws_fields_redacted_before_persistence():
    payload = {'SecretAccessKey': 'SENSITIVE', 'nested': {
        'SessionToken': 'SECRET', 'state': 'running'},
        'Password': 'OTHERSECRET', 'Data': [1, 2]}
    cleaned = sanitize_evidence(payload)
    assert cleaned['SecretAccessKey'] == '[REDACTED]'
    assert cleaned['nested']['SessionToken'] == '[REDACTED]'
    assert cleaned['Password'] == '[REDACTED]'
    assert cleaned['nested']['state'] == 'running'


def test_live_query_key_is_stable_to_json_param_order():
    a = {'service': 'ec2', 'operation': 'DescribeInstances', 'region': 'ap-south-1',
         'params_json': '{"Filters":[],"MaxResults":10}'}
    b = {**a, 'params_json': '{"MaxResults":10,"Filters":[]}'}
    assert intent_key(a) == intent_key(b)
    assert intent_key(a) != intent_key({**a, 'region': 'eu-west-1'})


def test_new_sqlalchemy_schema_contains_no_stored_credential_field():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    columns = {c['name'] for c in inspect(engine).get_columns('aws_connections')}
    assert 'credential_ciphertext' not in columns
    assert 'role_arn' in columns
    assert 'aws_inventory_revisions' in inspect(engine).get_table_names()
    assert 'aws_live_evidence' in inspect(engine).get_table_names()
    engine.dispose()


def test_alembic_upgrade_destroys_legacy_encrypted_keys():
    from pathlib import Path
    from runpy import run_path
    from sqlalchemy import text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        old_tables = [t for t in Base.metadata.sorted_tables
                      if t.name not in ('aws_inventory_revisions', 'aws_live_evidence')]
        Base.metadata.create_all(conn, tables=old_tables)
        conn.execute(text('ALTER TABLE aws_connections ADD COLUMN credential_ciphertext TEXT'))
        conn.execute(text("INSERT INTO aws_connections (id,workspace_id,user_id,provider,account_id,auth_type,default_region,auth_status,credential_ciphertext,created_at) "
                          "VALUES (5,3,7,'AWS','111122223333','PERSISTED_KEYS','us-east-1','VERIFIED','FAKE_ENCRYPTED_SECRET','2026-10-12')"))
        upgrade = run_path(str(Path('alembic/versions/e20261012_inventory_revision_no_keys.py')))['upgrade']
        migration_context = MigrationContext.configure(conn)
        with Operations.context(migration_context):
            upgrade()
        assert 'credential_ciphertext' not in {c['name'] for c in inspect(conn).get_columns('aws_connections')}
        row = conn.execute(text('SELECT auth_type, auth_status FROM aws_connections WHERE id=5')).fetchone()
        assert row == ('TRANSIENT_KEYS', 'REAUTH_REQUIRED')
        assert 'aws_inventory_revisions' in inspect(conn).get_table_names()
        assert 'aws_live_evidence' in inspect(conn).get_table_names()
    engine.dispose()
