from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Any, Awaitable, Callable

from app.ai.answer_generator import GroqAnswerGenerator
from app.services.aws_inventory_service import sanitize_evidence
from app.ai.groq_client import GroqIntentClient
from app.ai.snapshot_answer import try_generate_snapshot_answer
from app.ai.snapshot_evidence import build_snapshot_evidence
from app.providers.aws.credentials import AWSCredentials
from app.providers.aws.mcp_client import AWSMCPClient
from app.providers.aws.query.multi_region_executor import AWSMultiRegionQueryExecutor
from app.providers.aws.query.read_only_validator import (
    AWSReadOnlyOperationValidator,
    UnsafeAWSOperationError,
)
from app.providers.aws.query.region_resolver import AWSQueryRegionResolver


logger = logging.getLogger(__name__)

CredentialLoader = Callable[[], Awaitable[AWSCredentials]]
LiveEvidenceLookup = Callable[[dict[str, Any]], Awaitable[dict | None]]
LiveEvidencePersist = Callable[[dict[str, Any], dict], Awaitable[int]]


def needs_fresh_data(question: str) -> bool:
    """Explicit freshness overrides cached evidence; never infer freshness from a default."""
    import re
    return bool(re.search(r'\b(right now|currently|at the moment|as of now|live|real.time|refresh|latest state|now)\b', question, re.I))



class AWSQueryRouter:
    """
    Routes natural-language AWS audit questions to either:
      1. saved immutable scan evidence,
      2. a deterministic service-specific audit executor, or
      3. one generic live read-only AWS operation.

    Service-specific audit executors are used for questions that inherently
    require dependent multi-step evidence collection (for example, an S3
    encryption + object-name/metadata audit). This avoids forcing a complex
    audit into the generic one-operation intent schema.
    """

    USER_UNDERSTANDING_ERROR = (
        "Unable to understand the question. Please try again or rephrase your request."
    )
    USER_EXECUTION_ERROR = (
        "I couldn't complete that AWS request right now. Please try again."
    )
    USER_FORMATTING_ERROR = (
        "I couldn't complete that request right now. Please try again."
    )

    def __init__(self) -> None:
        self._intent_client = GroqIntentClient()
        self._validator = AWSReadOnlyOperationValidator()
        self._region_resolver = AWSQueryRegionResolver()
        self._executor = AWSMultiRegionQueryExecutor()
        self._answer_generator = GroqAnswerGenerator()

    async def execute(
        self,
        *,
        question: str,
        scan_result: dict[str, Any],
        credential_loader: CredentialLoader | None = None,
        conversation_context: str | None = None,
        selected_context: dict[str, Any] | None = None,
        live_evidence_lookup: LiveEvidenceLookup | None = None,
        live_evidence_persist: LiveEvidencePersist | None = None,
    ) -> dict[str, Any]:
        started = perf_counter()
        timings: dict[str, int] = {}
        selected_context = dict(selected_context or {})
        excluded = set(scan_result.get('excluded_services') or [])
        selected_service = str(selected_context.get('service') or '').lower().strip()
        if selected_service and selected_service in excluded:
            return self._error(started, timings, 'SERVICE_EXCLUDED_BY_USER', {},
                               f'{selected_service} was removed from the effective inventory by a user. '
                               'Restore the service in AWS Inventory before requesting its AWS evidence.')

        # Generic Phase 3 path: no service-specific special case is routed here.
        # Service/resource identifiers are taken from the latest effective inventory.

        # ------------------------------------------------------------
        # Generic intent planning.
        # Internal provider/schema exceptions are logged, never shown to
        # the end user as raw 400/json_validate_failed payloads.
        # ------------------------------------------------------------
        t = perf_counter()
        try:
            resource_inventory = scan_result.get('resources') or {}
            selected_id = str(selected_context.get('arn') or selected_context.get('resource_id') or '').strip().lower()
            selected_rows = [
                item for item in (resource_inventory.get('resources') or [])
                if isinstance(item, dict) and selected_id and selected_id in {
                    str(item.get('arn') or '').lower(), str(item.get('resource_id') or '').lower()
                }
                and (not selected_service or str(item.get('service') or '').lower() == selected_service)
            ]
            planner_context = {
                **selected_context,
                '_selected_saved_aws_evidence': [
                    {
                        'resource_id': r.get('resource_id'), 'arn': r.get('arn'),
                        'aws_evidence': r.get('aws_evidence'),
                    }
                    for r in selected_rows[:1] if r.get('aws_evidence')
                ],
                '_latest_effective_inventory': {
                    'revision': scan_result.get('inventory_revision'),
                    'discovered_resources': resource_inventory.get('total_resources'),
                    'services': [x.get('service') for x in
                                 (resource_inventory.get('detected_services') or [])[:100]
                                 if isinstance(x, dict)],
                    'billing_available': (scan_result.get('billing') or {}).get('available'),
                    'last_live_update': scan_result.get('last_live_update'),
                },
            }
            intent = self._intent_client.parse_aws_question(
                question,
                conversation_context=conversation_context,
                selected_context=planner_context,
            )
        except Exception:
            logger.exception("AWS intent planning failed")
            return self._error(
                started,
                timings,
                "INTENT_PLANNING_FAILED",
                {},
                self.USER_UNDERSTANDING_ERROR,
            )

        timings["intent_ms"] = self._ms(t)
        intent_dict = intent.model_dump(mode="json")
        planned_service = str(intent.service or '').lower().strip()
        if selected_service and planned_service and planned_service != selected_service and str(intent.operation).lower() != 'outofscope':
            return self._error(started, timings, 'SELECTED_SCOPE_MISMATCH', intent_dict,
                               'This chat is scoped to the selected AWS service. Open the other service to inspect it.')
        # A resource selection is an actual resource scope, not just a chat title.
        selected_id = str(selected_context.get('resource_id') or selected_context.get('arn') or '').lower().strip()
        if selected_id and intent.resource_id and str(intent.resource_id).lower() != selected_id:
            rows = (scan_result.get('resources') or {}).get('resources') or []
            record = next((r for r in rows if isinstance(r, dict) and selected_id in {
                str(r.get('arn') or '').lower(), str(r.get('resource_id') or '').lower()}), None)
            allowed_ids = {selected_id}
            if record:
                allowed_ids.update(str(record.get(k) or '').lower() for k in ('arn','resource_id'))
            if str(intent.resource_id).lower() not in allowed_ids:
                return self._error(started, timings, 'SELECTED_SCOPE_MISMATCH', intent_dict,
                                   'The planned AWS resource differs from the selected resource.')
        if selected_id and not intent.resource_id and intent.requires_live_data:
            intent_dict['resource_id'] = selected_context.get('resource_id') or selected_context.get('arn')
        if str(intent.service or '').lower().strip() in excluded:
            return self._error(started, timings, 'SERVICE_EXCLUDED_BY_USER', intent_dict,
                               'This service was removed from the effective inventory by a user. '
                               'Restore it in AWS Inventory before requesting its AWS evidence.')

        if str(intent.operation or "").strip().lower() == "outofscope":
            return self._error(
                started,
                timings,
                "OUT_OF_SCOPE",
                intent_dict,
                (
                    "This chat is scoped to the selected AWS account, saved "
                    "discovery snapshots, billing, configuration, and read-only "
                    "cloud-audit questions."
                ),
            )

        # Groq may request multiple read-only AWS APIs for a question.  Their
        # names, services, Regions and parameters are data, not a static registry.
        try:
            steps = intent.get_steps()
        except ValueError:
            logger.warning('Groq returned an invalid multi-operation plan')
            return self._error(started, timings, 'INVALID_LIVE_PLAN', intent_dict,
                               'The AWS query plan could not be validated.')
        if steps and not intent.requires_live_data:
            return self._error(started, timings, 'INVALID_LIVE_PLAN', intent_dict,
                               'Saved-snapshot plans cannot execute AWS API steps.')
        if steps:
            return await self._execute_dynamic_plan(
                started=started, timings=timings, question=question, steps=steps,
                scan_result=scan_result, credential_loader=credential_loader,
                selected_context=selected_context, intent_dict=intent_dict,
                live_evidence_lookup=live_evidence_lookup,
                live_evidence_persist=live_evidence_persist,
            )

        # ------------------------------------------------------------
        # Saved snapshot path.
        # ------------------------------------------------------------
        if not intent.requires_live_data:
            evidence = build_snapshot_evidence(
                scan_result=scan_result,
                selected_context=selected_context,
                intent=intent_dict,
            )
            answer = try_generate_snapshot_answer(
                question=question,
                snapshot_evidence=evidence,
            )

            t = perf_counter()
            try:
                if answer is None:
                    answer = self._answer_generator.generate_snapshot(
                        question=question,
                        snapshot_evidence=evidence,
                        conversation_context=conversation_context,
                    )
            except Exception:
                logger.exception("Snapshot answer generation failed")
                return self._error(
                    started,
                    timings,
                    "ANSWER_GENERATION_FAILED",
                    intent_dict,
                    self.USER_FORMATTING_ERROR,
                    data=evidence,
                )

            timings["answer_ms"] = self._ms(t)
            timings["total_ms"] = self._ms(started)

            return {
                "status": "SUCCESS",
                "source": "PERSISTED_DISCOVERY_SNAPSHOT",
                "scan_id": scan_result.get("scan_id"),
                "intent": intent_dict,
                "selected_context": selected_context,
                "answer": answer,
                "data": evidence,
                "warnings": scan_result.get("warnings") or [],
                "timings": timings,
                "response_time_ms": timings["total_ms"],
                "response_time_seconds": round(timings["total_ms"] / 1000, 2),
            }

        # Reuse a recent, persisted result for the same planned live query.
        # Current/right-now questions always bypass the cache.
        if live_evidence_lookup is not None and not needs_fresh_data(question):
            try:
                cached = await live_evidence_lookup(intent_dict)
            except Exception:
                logger.exception('Unable to read cached AWS evidence')
                cached = None
            if cached is not None:
                try:
                    answer = self._answer_generator.generate(
                        question=question, intent=intent_dict, aws_result=cached,
                    )
                except Exception:
                    logger.exception('Cached AWS answer generation failed')
                    return self._error(started, timings, 'ANSWER_GENERATION_FAILED', intent_dict, self.USER_FORMATTING_ERROR)
                timings['total_ms'] = self._ms(started)
                return {
                    'status': 'SUCCESS', 'source': 'PERSISTED_LIVE_EVIDENCE',
                    'scan_id': scan_result.get('scan_id'), 'intent': intent_dict,
                    'selected_context': selected_context, 'answer': answer,
                    'data': cached, 'warnings': [], 'timings': timings,
                    'response_time_ms': timings['total_ms'],
                    'response_time_seconds': round(timings['total_ms'] / 1000, 2),
                }

        # ------------------------------------------------------------
        # Generic single-operation live path.
        # ------------------------------------------------------------
        if credential_loader is None:
            return self._error(
                started,
                timings,
                "AWS_AUTHORIZATION_REQUIRED",
                intent_dict,
                (
                    "AWS authorization is required to complete this request. "
                    "Please reconnect or re-authorize the account."
                ),
            )

        try:
            t = perf_counter()
            credentials = await credential_loader()
            timings["automatic_auth_ms"] = self._ms(t)
        except Exception:
            logger.exception("Automatic AWS authentication failed")
            return self._error(
                started,
                timings,
                "AWS_AUTHORIZATION_REQUIRED",
                intent_dict,
                (
                    "AWS authorization is required to complete this request. "
                    "Please reconnect or re-authorize the account."
                ),
            )

        service = (intent.service or "").strip().lower()
        operation = (intent.operation or "").strip()

        if not service or not operation:
            return self._error(
                started,
                timings,
                "INVALID_LIVE_PLAN",
                intent_dict,
                self.USER_UNDERSTANDING_ERROR,
            )

        try:
            self._validator.validate(service=service, operation=operation)
        except UnsafeAWSOperationError:
            logger.warning(
                "Blocked non-read-only AWS operation: service=%s operation=%s",
                service,
                operation,
            )
            return self._error(
                started,
                timings,
                "OPERATION_BLOCKED",
                intent_dict,
                "That AWS operation is not permitted in read-only audit mode.",
            )

        detected = (scan_result.get("resources") or {}).get("detected_services", [])
        entry = next(
            (
                item
                for item in detected
                if str(item.get("service", "")).strip().lower() == service
            ),
            None,
        )

        regions = self._region_resolver.resolve(
            service=service,
            requested_region=intent.region or selected_context.get('region'),
            service_entry=entry,
            fallback_region=credentials.default_region,
        )

        if not regions:
            return self._error(
                started,
                timings,
                "NO_QUERY_REGION",
                intent_dict,
                self.USER_EXECUTION_ERROR,
            )

        t = perf_counter()
        try:
            async with AWSMCPClient(credentials) as mcp:
                execution = await self._executor.execute(
                    credentials=credentials,
                    service=service,
                    operation=operation,
                    regions=regions,
                    params=intent.get_params(),
                    mcp=mcp,
                )
        except Exception:
            logger.exception(
                "AWS MCP generic query failed: service=%s operation=%s",
                service,
                operation,
            )
            return self._error(
                started,
                timings,
                "AWS_MCP_EXECUTION_FAILED",
                intent_dict,
                self.USER_EXECUTION_ERROR,
            )

        timings["aws_mcp_ms"] = self._ms(t)
        data = sanitize_evidence(execution.model_dump(mode="json"))

        if not execution.successful_regions:
            logger.warning(
                "AWS query failed in all target Regions: service=%s operation=%s warnings=%s",
                service,
                operation,
                execution.warnings,
            )
            return self._error(
                started,
                timings,
                "AWS_QUERY_FAILED",
                intent_dict,
                self.USER_EXECUTION_ERROR,
                data=data,
                warnings=execution.warnings,
            )

        # Commit validated evidence BEFORE answering, so successful answers
        # never claim a database update that did not actually persist.
        if live_evidence_persist is not None:
            try:
                revision = await live_evidence_persist(intent_dict, data)
            except Exception:
                logger.exception('AWS live evidence could not be persisted')
                return self._error(started, timings, 'EVIDENCE_PERSISTENCE_FAILED', intent_dict,
                                   'AWS data was retrieved, but could not be saved. No updated answer was committed.')
        else:
            revision = None

        t = perf_counter()
        try:
            answer = self._answer_generator.generate(
                question=question,
                intent=intent_dict,
                aws_result=data,
            )
        except Exception:
            logger.exception("Live AWS answer generation failed")
            return self._error(
                started,
                timings,
                "ANSWER_GENERATION_FAILED",
                intent_dict,
                self.USER_FORMATTING_ERROR,
                data=data,
                warnings=execution.warnings,
            )

        timings["answer_ms"] = self._ms(t)
        timings["total_ms"] = self._ms(started)

        return {
            "status": "SUCCESS",
            "source": "LIVE_AWS_MCP_PERSISTED" if revision is not None else "LIVE_AWS_MCP_AUTO_AUTH",
            "inventory_revision": revision,
            "scan_id": scan_result.get("scan_id"),
            "intent": intent_dict,
            "selected_context": selected_context,
            "answer": answer,
            "data": data,
            "warnings": execution.warnings,
            "timings": timings,
            "response_time_ms": timings["total_ms"],
            "response_time_seconds": round(timings["total_ms"] / 1000, 2),
        }

    async def _execute_dynamic_plan(
        self, *, started: float, timings: dict[str, int], question: str,
        steps: list[dict[str, Any]], scan_result: dict[str, Any],
        credential_loader: CredentialLoader | None,
        selected_context: dict[str, Any], intent_dict: dict[str, Any],
        live_evidence_lookup: LiveEvidenceLookup | None,
        live_evidence_persist: LiveEvidencePersist | None,
    ) -> dict[str, Any]:
        """One generic pipeline for any set of validated AWS read operations."""
        selected_service = str(selected_context.get('service') or '').strip().lower()
        selected_id = str(selected_context.get('arn') or selected_context.get('resource_id') or '').strip().lower()
        excluded = set(scan_result.get('excluded_services') or [])
        # Validate the entire plan *before* performing any live AWS call.
        for step in steps:
            if step['service'] in excluded:
                return self._error(started, timings, 'SERVICE_EXCLUDED_BY_USER', intent_dict,
                                   'This service has been excluded from the effective inventory.')
            if selected_service and step['service'] != selected_service:
                return self._error(started, timings, 'SELECTED_SCOPE_MISMATCH', intent_dict,
                                   'Select the requested service explicitly to change chat scope.')
            if selected_id and step.get('resource_id') and str(step['resource_id']).lower() != selected_id:
                # Resource IDs and ARNs may differ: accept either if it matches
                # the known selected resource in saved inventory.
                rows = (scan_result.get('resources') or {}).get('resources') or []
                known = next((r for r in rows if isinstance(r, dict) and selected_id in {
                    str(r.get('arn') or '').lower(), str(r.get('resource_id') or '').lower()}), None)
                identities = {selected_id}
                if known:
                    identities.update(str(known.get(k) or '').lower() for k in ('arn', 'resource_id'))
                if str(step['resource_id']).lower() not in identities:
                    return self._error(started, timings, 'SELECTED_SCOPE_MISMATCH', intent_dict,
                                       'The planned AWS resource differs from the selected resource.')
            if selected_id and not step.get('resource_id'):
                step['resource_id'] = selected_context.get('resource_id') or selected_context.get('arn')
            try:
                self._validator.validate(service=step['service'], operation=step['operation'])
            except UnsafeAWSOperationError:
                return self._error(started, timings, 'OPERATION_BLOCKED', intent_dict,
                                   'One of the requested AWS operations is not permitted in read-only mode.')

        responses: list[dict[str, Any]] = []
        misses: list[dict[str, Any]] = []
        for step in steps:
            cached = None
            if live_evidence_lookup and not needs_fresh_data(question):
                try:
                    cached = await live_evidence_lookup(step)
                except Exception:
                    logger.exception('Generic cached evidence lookup failed')
            if cached is not None and not cached.get('failed_regions'):
                responses.append({'intent': step, 'source': 'PERSISTED_LIVE_EVIDENCE', 'evidence': cached})
            else:
                misses.append(step)

        revision = None
        warnings = []
        if misses:
            if credential_loader is None or live_evidence_persist is None:
                return self._error(started, timings, 'AWS_AUTHORIZATION_REQUIRED' if credential_loader is None
                                   else 'EVIDENCE_PERSISTENCE_UNAVAILABLE', intent_dict,
                                   'AWS authorization and evidence persistence are required for missing data.')
            try:
                credentials = await credential_loader()
            except Exception:
                logger.exception('AWS authorization failed')
                return self._error(started, timings, 'AWS_AUTHORIZATION_REQUIRED', intent_dict,
                                   'AWS authorization is required. Reconnect or configure an IAM role.')
            try:
                async with AWSMCPClient(credentials) as mcp:
                    for step in misses:
                        try:
                            detected = (scan_result.get('resources') or {}).get('detected_services') or []
                            entry = next((r for r in detected if r.get('service') == step['service']), None)
                            regions = self._region_resolver.resolve(
                                service=step['service'], requested_region=step.get('region') or selected_context.get('region'),
                                service_entry=entry, fallback_region=credentials.default_region,
                            )
                            execution = await self._executor.execute(
                                credentials=credentials, service=step['service'],
                                operation=step['operation'], regions=regions,
                                params=json.loads(step['params_json']), mcp=mcp,
                            )
                            data = sanitize_evidence(execution.model_dump(mode='json'))
                            if not execution.successful_regions:
                                warnings.append({'service': step['service'], 'operation': step['operation'],
                                                 'status': 'AWS_QUERY_FAILED', 'warnings': execution.warnings})
                                continue
                            # Each successful read is committed before any final answer.
                            revision = await live_evidence_persist(step, data)
                            responses.append({'intent': step, 'source': 'LIVE_AWS_MCP_PERSISTED', 'evidence': data})
                            if execution.failed_regions or execution.warnings:
                                warnings.append({'service': step['service'], 'operation': step['operation'],
                                                 'status': 'PARTIAL_OR_TRUNCATED_COVERAGE', 'warnings': execution.warnings})
                        except Exception:
                            logger.exception('Dynamic AWS MCP step failed')
                            warnings.append({'service': step['service'], 'operation': step['operation'],
                                             'status': 'FAILED_OR_NOT_PERSISTED'})
            except Exception:
                logger.exception('AWS MCP connection failed for dynamic operation plan')
                if not responses:
                    return self._error(started, timings, 'AWS_MCP_EXECUTION_FAILED', intent_dict,
                                       self.USER_EXECUTION_ERROR)
                warnings.append({'status': 'MCP_CONNECTION_FAILED'})

        if not responses:
            return self._error(started, timings, 'AWS_QUERY_FAILED', intent_dict,
                               'No requested AWS evidence could be verified and persisted.', warnings=warnings)
        # A failed step must never be silently interpreted as a passing control.
        combined = {'plan': [
            {'service': x['service'], 'operation': x['operation'],
             'region': x.get('region'), 'resource_id': x.get('resource_id')}
            for x in steps
        ], 'results': responses, 'warnings': warnings,
            'coverage': 'PARTIAL' if warnings or len(responses) != len(steps) else 'COMPLETE'}
        try:
            answer = self._answer_generator.generate(
                question=question, intent=intent_dict, aws_result=combined,
            )
        except Exception:
            logger.exception('Dynamic evidence answer generation failed')
            return self._error(started, timings, 'ANSWER_GENERATION_FAILED', intent_dict,
                               self.USER_FORMATTING_ERROR, data=combined, warnings=warnings)
        if combined['coverage'] == 'PARTIAL':
            answer += '\n\nSome requested AWS checks could not be verified; this is not a complete security assessment.'
        timings['total_ms'] = self._ms(started)
        return {
            'status': 'SUCCESS', 'source': 'LIVE_AWS_MCP_PERSISTED' if revision is not None else 'PERSISTED_LIVE_EVIDENCE',
            'inventory_revision': revision, 'scan_id': scan_result.get('scan_id'),
            'intent': intent_dict, 'selected_context': selected_context,
            'answer': answer, 'data': combined, 'warnings': warnings,
            'timings': timings, 'response_time_ms': timings['total_ms'],
            'response_time_seconds': round(timings['total_ms'] / 1000, 2),
        }

    @staticmethod
    def _ms(started: float) -> int:
        return round((perf_counter() - started) * 1000)

    @classmethod
    def _error(
        cls,
        started: float,
        timings: dict[str, int],
        status: str,
        intent: dict[str, Any],
        answer: str,
        data: Any = None,
        warnings: list[Any] | None = None,
    ) -> dict[str, Any]:
        timings["total_ms"] = cls._ms(started)
        out: dict[str, Any] = {
            "status": status,
            "intent": intent,
            "answer": answer,
            "warnings": warnings or [],
            "timings": timings,
            "response_time_ms": timings["total_ms"],
            "response_time_seconds": round(timings["total_ms"] / 1000, 2),
        }
        if data is not None:
            out["data"] = data
        return out
