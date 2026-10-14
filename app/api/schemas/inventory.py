from typing import Literal
from pydantic import BaseModel, Field, model_validator


class ManualResource(BaseModel):
    service: str = Field(min_length=2, max_length=100, pattern=r'^[a-z0-9-]+$')
    resource_id: str | None = Field(default=None, max_length=512)
    arn: str | None = Field(default=None, max_length=1024)
    region: str | None = Field(default=None, max_length=64)
    resource_type: str | None = Field(default=None, max_length=256)
    properties: list[dict] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def requires_identity(self):
        if not self.resource_id and not self.arn:
            raise ValueError('A resource_id or ARN is required.')
        return self


class InventoryEditRequest(BaseModel):
    action: Literal['ADD_SERVICE', 'EXCLUDE_SERVICE', 'RESTORE_SERVICE']
    service: str | None = Field(default=None, max_length=100)
    resource: ManualResource | None = None
    resource_key: str | None = Field(default=None, max_length=1300)

    @model_validator(mode='after')
    def services_only(self):
        # The public inventory editing API permits only services to be added,
        # removed (excluded), or restored. Resources and Regions remain read-only.
        if self.resource is not None or self.resource_key is not None:
            raise ValueError('Only service Add/Remove/Restore actions are allowed.')
        if not self.service or not self.service.strip():
            raise ValueError('Service namespace is required.')
        return self
