from typing import cast

from posthog.schema import (
    DataWarehouseSourceCategory,
    ExternalDataSourceType as SchemaExternalDataSourceType,
    SourceConfig,
)

from posthog.temporal.data_imports.sources.common.base import FieldType, SimpleSource
from posthog.temporal.data_imports.sources.common.registry import SourceRegistry
from posthog.temporal.data_imports.sources.generated_configs import LemonSqueezySourceConfig

from products.data_warehouse.backend.types import ExternalDataSourceType


@SourceRegistry.register
class LemonSqueezySource(SimpleSource[LemonSqueezySourceConfig]):
    @property
    def source_type(self) -> ExternalDataSourceType:
        return ExternalDataSourceType.LEMONSQUEEZY

    @property
    def get_source_config(self) -> SourceConfig:
        return SourceConfig(
            name=SchemaExternalDataSourceType.LEMON_SQUEEZY,
            category=DataWarehouseSourceCategory.PAYMENTS___BILLING,
            label="Lemon Squeezy",
            iconPath="/static/services/lemon_squeezy.png",
            fields=cast(list[FieldType], []),
            unreleasedSource=True,
        )
