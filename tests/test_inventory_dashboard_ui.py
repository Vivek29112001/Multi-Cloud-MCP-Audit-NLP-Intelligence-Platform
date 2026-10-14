"""UI view-model tests that do not require a Streamlit runtime or AWS credentials."""
from __future__ import annotations

import ast
from collections import Counter
from html import escape
from pathlib import Path
import re

import pytest
from pydantic import ValidationError
from app.api.schemas.inventory import InventoryEditRequest

UI_FILE = Path(__file__).resolve().parents[1] / 'ui' / 'app.py'


def view_helpers():
    tree = ast.parse(UI_FILE.read_text(encoding='utf-8'))
    names = {'_excluded_services_from_history', '_service_rows', '_region_rows'}
    node_list = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(node_list) == len(names), 'Dashboard functions missing'
    mod = ast.Module(body=node_list, type_ignores=[])
    namespace = {'Counter': Counter, 're': re, 'escape': escape}
    exec(compile(ast.fix_missing_locations(mod), str(UI_FILE), 'exec'), namespace)
    return namespace


def test_dynamic_service_list_without_predefined_catalog():
    f = view_helpers()['_service_rows']
    context = {'resources': {'detected_services': [
        {'service': 'rds', 'resource_count': 2, 'data_source': 'AWS_DISCOVERY', 'regions': ['ap-south-1']},
        {'service': 'future-aws-service', 'resource_count': 0, 'data_source': 'USER_ADDED', 'regions': []},
    ]}}
    rows = f(context, [])
    assert {r['service'] for r in rows} == {'rds', 'future-aws-service'}
    assert next(r for r in rows if r['service'] == 'future-aws-service')['status'] == 'Added by user'


def test_removed_service_visible_with_restore_action():
    f = view_helpers()['_service_rows']
    revisions = [{'revision': 9, 'change': {'action': 'EXCLUDE_SERVICE', 'target': 'rds', 'before': None}},
                 {'revision': 8, 'change': {'action': 'EXCLUDE_SERVICE', 'target': 'ecs', 'before': 'ADD'}},
                 {'revision': 7, 'change': {'action': 'ADD_SERVICE', 'target': 'ecs', 'before': None}}]
    rows = f({'resources': {'detected_services': []}}, revisions)
    rds = next(r for r in rows if r['service'] == 'rds')
    ecs = next(r for r in rows if r['service'] == 'ecs')
    assert rds['status'] == 'Removed by user'
    assert rds['restore_action'] == 'RESTORE_SERVICE'
    assert ecs['restore_action'] == 'ADD_SERVICE'


def test_recent_restore_hides_excluded_row():
    f = view_helpers()['_service_rows']
    revisions = [{'revision': 5, 'change': {'action': 'RESTORE_SERVICE', 'target': 's3'}},
                 {'revision': 4, 'change': {'action': 'EXCLUDE_SERVICE', 'target': 's3'}}]
    assert f({'resources': {'detected_services': []}}, revisions) == []


def test_regions_used_unused_disabled_in_single_read_only_list():
    f = view_helpers()['_region_rows']
    context = {
        'regions': {'regions': [
            {'region_name': 'ap-south-1', 'enabled': True},
            {'region_name': 'us-east-1', 'enabled': True},
            {'region_name': 'ap-east-1', 'enabled': False},
        ]},
        'resources': {'resources': [
            {'service': 'ec2', 'region': 'ap-south-1'},
            {'service': 'rds', 'region': 'ap-south-1'},
            {'service': 'iam', 'region': 'global'},
        ]},
    }
    result = {r['region']: r for r in f(context)}
    assert len(result) == 3
    assert result['ap-south-1']['status'] == 'Used'
    assert result['ap-south-1']['resource_count'] == 2
    assert result['us-east-1']['status'] == 'Unused / not detected'
    assert result['ap-east-1']['status'] == 'Not enabled'
    assert 'global' not in result


def test_api_allows_service_edits_only():
    for action in ('ADD_SERVICE', 'EXCLUDE_SERVICE', 'RESTORE_SERVICE'):
        assert InventoryEditRequest(action=action, service='ecs').action == action
    for action in ('UPSERT_RESOURCE', 'EXCLUDE_RESOURCE', 'RESTORE_RESOURCE'):
        with pytest.raises(ValidationError):
            InventoryEditRequest(action=action, service='ecs')
    with pytest.raises(ValidationError):
        InventoryEditRequest(action='ADD_SERVICE', service='ecs', resource_key='some-key')


def test_ui_does_not_invoke_resource_edit_controls_or_show_billing_by_default():
    tree = ast.parse(UI_FILE.read_text(encoding='utf-8'))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'render_discovery_page')
    calls = [node.func.id for node in ast.walk(fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert 'render_inventory_manager' not in calls
    assert 'render_read_only_resources' in calls
    assert 'render_billing_on_click' in calls
    code = ast.get_source_segment(UI_FILE.read_text(encoding='utf-8'), fn)
    # Billing is rendered solely inside the selected accordion body.
    assert 'if is_open:' in code
    assert 'elif section == "billing":' in code
    assert 'render_billing_on_click(scan_data, int(connection_id))' in code
    assert 'services_col, regions_col = st.columns' not in code
    fn_billing = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'render_billing_on_click')
    billing_code = ast.get_source_segment(UI_FILE.read_text(encoding='utf-8'), fn_billing)
    assert 'render_billing_results(context.get("billing"))' in billing_code


def test_exclusive_accordion_toggle_logic():
    tree = ast.parse(UI_FILE.read_text(encoding='utf-8'))
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_next_inventory_section')
    ns = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(UI_FILE), 'exec'), ns)
    update = ns['_next_inventory_section']
    assert update(None, 'services') == 'services'
    assert update('services', 'regions') == 'regions'
    assert update('regions', 'resources') == 'resources'
    assert update('resources', 'resources') is None
    assert update('history', 'billing') == 'billing'


def test_service_and_resource_chat_use_persisted_context():
    source = UI_FILE.read_text(encoding='utf-8')
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    service_code = ast.get_source_segment(source, funcs['render_services_list'])
    resource_code = ast.get_source_segment(source, funcs['render_read_only_resources'])
    assert '"context_type": "SERVICE"' in service_code
    assert '"context_type": "RESOURCE"' in resource_code
    assert '_open_context_chat(' in service_code
    assert '_open_context_chat(' in resource_code
    assert '"resource_id": item.get("resource_id")' in resource_code
    assert '"arn": item.get("arn")' in resource_code
    assert '"region": item.get("region")' in resource_code
    assert 'post_inventory_edit' not in resource_code


def test_accordion_sections_stacked_in_expected_order():
    source = UI_FILE.read_text(encoding='utf-8')
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'render_discovery_page')
    code = ast.get_source_segment(source, fn)
    names = ['("services",', '("regions",', '("resources",', '("history",', '("billing",']
    positions = [code.index(name) for name in names]
    assert positions == sorted(positions)
    assert 'if is_open:' in code
    assert 'with st.container(border=True):' in code
