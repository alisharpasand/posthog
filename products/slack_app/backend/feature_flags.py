"""Feature flag helpers for the Slack app.

Lives at the product root (not under `services/`) because flag checks are
cross-cutting — any service, handler, or temporal activity may need a gate
and shouldn't have to reach into another service to get one.

Each helper is a thin, fail-closed wrapper around `posthoganalytics`. A
transient PostHog API outage must never silently enable a feature for
everyone — exceptions return `False`.
"""

from __future__ import annotations

import logging

import posthoganalytics

from posthog.models.integration import Integration
from posthog.utils import get_instance_region

logger = logging.getLogger(__name__)


SLACK_APP_HOME_FLAG = "slack-app-home"


def is_slack_app_home_enabled(integration: Integration, *, region: str | None = None) -> bool:
    """Return True when the `slack-app-home` flag is on for this workspace.

    The flag controls the App Home tab surface and the AI-settings resolver
    that feeds Slack-triggered task runs. Keyed on Slack workspace id +
    PostHog org so the same flag rule can target either dimension.

    `region` overrides the auto-detected instance region — useful in dev
    environments where `get_instance_region()` returns `None`. The default
    fallback is `"dev"` (not `"unknown"`), so a flag rule targeting
    `region == "dev"` opts local environments in without leaking to prod.
    """
    return _flag_enabled(SLACK_APP_HOME_FLAG, integration=integration, region=region)


def _flag_enabled(flag: str, *, integration: Integration, region: str | None) -> bool:
    try:
        return bool(
            posthoganalytics.feature_enabled(
                flag,
                f"slack_workspace:{integration.integration_id}",
                groups={"organization": str(integration.team.organization_id)},
                person_properties={"region": region or get_instance_region() or "dev"},
                only_evaluate_locally=False,
                send_feature_flag_events=False,
            )
        )
    except Exception:
        logger.exception(
            "slack_app_feature_flag_check_failed",
            extra={"flag": flag, "integration_id": integration.id},
        )
        return False
