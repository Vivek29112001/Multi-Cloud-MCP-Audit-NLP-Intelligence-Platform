"""Isolated dynamic router tests; no Groq, MCP, AWS or DB connectivity."""
import ast
import asyncio
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import AsyncMock

from app.providers.aws.query.read_only_validator import AWSReadOnlyOperationValidator, UnsafeAWSOperationError
from app.services.aws_inventory_service import sanitize_evidence


def load_dynamic_method():
    source = Path(__file__).resolve().parents[1] / 'app/ai/query_router.py'
    module = ast.parse(source.read_text())
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == 'AWSQueryRouter')
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_execute_dynamic_plan')
    isolated = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                                ast.ClassDef(name='TestRouter', bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[])
    ns = {'Any': Any, 'json': json, 'logger': logging.getLogger('test'),
          'sanitize_evidence': sanitize_evidence, 'AWSMCPClient': FakeMCP,
          'UnsafeAWSOperationError': UnsafeAWSOperationError,
          'needs_fresh_data': lambda q: 'right now' in q.lower()}
    exec(compile(ast.fix_missing_locations(isolated), str(source), 'exec'), ns)
    return ns['TestRouter']


class FakeMCP:
    enters = 0
    async def __aenter__(self):
        self.__class__.enters += 1
        return self
    async def __aexit__(self, *args):
        pass
    def __init__(self, credentials):
        pass


class FakeResult:
    successful_regions = ['eu-west-1']
    failed_regions = []
    warnings = []
    def model_dump(self, mode):
        return {'service': 'rds', 'operation': 'DescribeDBInstances',
                'results': [{'region': 'eu-west-1', 'success': True,
                             'data': [{'DBInstanceIdentifier': 'db-test', 'StorageEncrypted': True}]}]}


class FakeExecutor:
    def __init__(self):
        self.calls = []
    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResult()


class FakeAnswer:
    def generate(self, **kwargs):
        return f"Result with {len(kwargs['aws_result']['results'])} evidence steps."


def setup():
    router = load_dynamic_method()()
    router._validator = AWSReadOnlyOperationValidator()
    router._region_resolver = type('Resolver', (), {'resolve': lambda self, **kwargs: ['eu-west-1']})()
    router._executor = FakeExecutor()
    router._answer_generator = FakeAnswer()
    router._ms = lambda _: 5
    router._error = lambda start, timings, status, intent, answer, **kwargs: {
        'status': status, 'answer': answer, **kwargs}
    return router


def steps():
    return [{'service': 'rds', 'operation': 'DescribeDBInstances',
             'params_json': '{"DBInstanceIdentifier":"db-test"}', 'resource_id': 'db-test'},
            {'service': 'rds', 'operation': 'DescribeDBClusters',
             'params_json': '{"DBClusterIdentifier":"cluster-test"}', 'resource_id': None}]


def arguments():
    return {'started': perf_counter(), 'timings': {}, 'question': 'Describe selected database security',
            'steps': steps(), 'scan_result': {'scan_id': 'scan-1', 'resources': {
                'detected_services': [{'service': 'rds', 'regions': ['eu-west-1']}],
                'resources': [{'service':'rds','resource_id':'db-test','region':'eu-west-1'}]}},
            'credential_loader': AsyncMock(return_value=type('Cred', (), {'default_region':'eu-west-1'})()),
            'selected_context': {'service':'rds', 'resource_id':'db-test', 'region':'eu-west-1'},
            'intent_dict': {'service':'rds','operation':'DescribeDBInstances'},
            'live_evidence_lookup': AsyncMock(return_value=None),
            'live_evidence_persist': AsyncMock(return_value=11)}


def test_multi_live_reads_and_persists_every_step():
    router = setup(); opts = arguments()
    opts['steps'][1]['resource_id'] = 'db-test'
    result = asyncio.run(router._execute_dynamic_plan(**opts))
    assert result['status'] == 'SUCCESS'
    assert result['inventory_revision'] == 11
    assert opts['live_evidence_persist'].await_count == 2
    assert len(router._executor.calls) == 2
    assert result['data']['coverage'] == 'COMPLETE'


def test_all_cached_never_connects_to_mcp():
    router = setup(); opts=arguments()
    opts['steps'][1]['resource_id'] = 'db-test'
    opts['live_evidence_lookup'] = AsyncMock(return_value={'sample': 'saved'})
    opts['credential_loader'] = AsyncMock(side_effect=AssertionError('must not authenticate'))
    result=asyncio.run(router._execute_dynamic_plan(**opts))
    assert result['status']=='SUCCESS'
    assert result['source']=='PERSISTED_LIVE_EVIDENCE'
    assert len(router._executor.calls)==0
    opts['credential_loader'].assert_not_awaited()


def test_selected_context_blocks_different_service():
    router = setup(); opts=arguments()
    opts['steps'][0]['service']='ec2'
    result=asyncio.run(router._execute_dynamic_plan(**opts))
    assert result['status']=='SELECTED_SCOPE_MISMATCH'
    opts['credential_loader'].assert_not_awaited()


def test_write_operation_blocked_before_authentication():
    router = setup(); opts=arguments()
    opts['steps'][0]['operation']='DeleteDBInstance'
    result=asyncio.run(router._execute_dynamic_plan(**opts))
    assert result['status']=='OPERATION_BLOCKED'
    opts['credential_loader'].assert_not_awaited()


def test_authorization_required_when_data_not_cached():
    router = setup(); opts=arguments()
    opts['steps'][1]['resource_id']='db-test'
    opts['credential_loader']=None
    result=asyncio.run(router._execute_dynamic_plan(**opts))
    assert result['status']=='AWS_AUTHORIZATION_REQUIRED'


def test_current_question_bypasses_saved_evidence():
    router = setup(); opts=arguments(); opts['steps'][1]['resource_id']='db-test'
    opts['question']='Is it encrypted right now?'
    opts['live_evidence_lookup']=AsyncMock(return_value={'sample':'cached'})
    result=asyncio.run(router._execute_dynamic_plan(**opts))
    assert result['source']=='LIVE_AWS_MCP_PERSISTED'
    assert opts['live_evidence_lookup'].await_count==0
