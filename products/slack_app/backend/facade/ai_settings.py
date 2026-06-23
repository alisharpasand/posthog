"""Facade re-exports for the Slack-app AI settings subsystem.

Cross-product callers (e.g. Temporal activities under `posthog/temporal/`)
should import from here rather than reaching into `services/ai_settings.py`
directly. The facade is the supported entry point — the internals can move
around without breaking the call site as long as this surface stays stable.

Mirrors the layering already in place for the tasks product
(`products/tasks/backend/facade/`).
"""

from products.slack_app.backend.feature_flags import SLACK_APP_HOME_FLAG
from products.slack_app.backend.services.ai_settings import AISettings, resolve_ai_settings, validate_ai_settings

__all__ = [
    "SLACK_APP_HOME_FLAG",
    "AISettings",
    "resolve_ai_settings",
    "validate_ai_settings",
]
