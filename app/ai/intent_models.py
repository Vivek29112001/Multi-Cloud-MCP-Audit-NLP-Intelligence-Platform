from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


class AWSQueryIntent(BaseModel):
    """
    Structured plan produced by Groq for one read-only AWS query.

    params_json is intentionally kept as a JSON string because strict LLM
    structured-output schemas are more reliable when the arbitrary AWS API
    parameters are encoded as a string rather than as an open-ended object.
    """

    service: str
    operation: str
    region: str | None = None
    params_json: str = "{}"
    resource_id: str | None = None
    steps_json: str = "[]"
    requires_live_data: bool = True

    def get_params(self) -> dict[str, Any]:
        """Decode params_json into a dictionary for the generic AWS executor."""
        try:
            value = json.loads(self.params_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Groq returned invalid params_json."
            ) from exc

        if not isinstance(value, dict):
            raise ValueError(
                "params_json must represent a JSON object."
            )

        return value

    @property
    def params(self) -> dict[str, Any]:
        """Compatibility property for existing router/executor code."""
        return self.get_params()

    def get_steps(self) -> list[dict[str, Any]]:
        """Validate a dynamic, bounded set of read-only API query steps."""
        try:
            steps = json.loads(self.steps_json or '[]')
        except (ValueError, TypeError) as exc:
            raise ValueError('Groq returned invalid steps_json.') from exc
        if not isinstance(steps, list) or len(steps) > 8:
            raise ValueError('Live plan must contain at most eight operations.')
        parsed = []
        for step in steps:
            if not isinstance(step, dict) or not all(
                isinstance(step.get(key), str) and step[key].strip()
                for key in ('service', 'operation')
            ):
                raise ValueError('Invalid AWS operation step.')
            params = step.get('params_json', '{}')
            try:
                obj = json.loads(params) if isinstance(params, str) else params
            except (TypeError, ValueError) as exc:
                raise ValueError('Invalid AWS operation parameters.') from exc
            if not isinstance(obj, dict):
                raise ValueError('AWS operation parameters must be an object.')
            parsed.append({
                'service': step['service'].strip().lower(),
                'operation': step['operation'].strip(),
                'region': step.get('region'),
                'resource_id': step.get('resource_id'),
                'params_json': json.dumps(obj, sort_keys=True, separators=(',', ':')),
                'requires_live_data': True,
            })
        return parsed
