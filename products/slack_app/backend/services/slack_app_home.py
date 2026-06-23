"""App Home tab + edit modal renderers for the PostHog Slack app.

The Home tab is the user-facing control panel for the integration. For this
first iteration it carries one card — the AI settings picker that feeds
Slack-triggered task runs — but the layout leaves room for additional cards
(notifications, account linking, activity feed) as they come online. Each card
follows the same pattern: a one-line "effective" summary, an admin-aware edit
control, and an optional explainer of where the effective value came from.

All Block Kit payloads (views, modals) are built as plain dicts here so they
can be unit-tested without any Slack client. The event/interactivity handlers
in `products/slack_app/backend/api.py` are the ones that actually call
`views.publish` / `views.open` / `views.update`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from django.http import HttpResponse, JsonResponse

from posthog.models.integration import Integration, SlackIntegration

from products.slack_app.backend.services.ai_settings import AISettings

if TYPE_CHECKING:
    from products.slack_app.backend.models import SlackSettings

# Block / action / callback identifiers. Centralised so the interactivity
# handler in api.py and the renderers here cannot drift apart.
HOME_CALLBACK_ID = "slack_app_home"

ACTION_EDIT_PERSONAL = "slack_app_home:edit_personal"
ACTION_EDIT_WORKSPACE = "slack_app_home:edit_workspace"
ACTION_RESET_PERSONAL = "slack_app_home:reset_personal"

EDIT_MODAL_PERSONAL_CALLBACK_ID = "slack_app_ai_prefs:personal"
EDIT_MODAL_WORKSPACE_CALLBACK_ID = "slack_app_ai_prefs:workspace"

MODAL_ACTION_RUNTIME_ADAPTER = "ai_prefs:runtime_adapter"
MODAL_ACTION_MODEL = "ai_prefs:model"
MODAL_ACTION_REASONING_EFFORT = "ai_prefs:reasoning_effort"

MODAL_BLOCK_RUNTIME_ADAPTER = "block_runtime_adapter"
MODAL_BLOCK_MODEL = "block_model"
MODAL_BLOCK_REASONING_EFFORT = "block_reasoning_effort"

EditScope = Literal["personal", "workspace"]

# Display labels for the picker UI. These are Slack-app concerns — the tasks
# product owns the structural truth (which runtimes/models/efforts exist) but
# how they're spelled in this surface stays here. If another surface (web
# settings page, Linear app, etc.) needs labels, it owns its own copy rather
# than reaching across products for UI strings.
RUNTIME_ADAPTER_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude (Anthropic)",
    "codex": "Codex (OpenAI)",
}

MODEL_DISPLAY_NAMES: dict[str, str] = {
    "claude-opus-4-5": "Claude Opus 4.5",
    "claude-opus-4-6": "Claude Opus 4.6",
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-fable-5": "Claude Fable 5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "gpt-5": "GPT-5",
    "gpt-5.5": "GPT-5.5",
}

REASONING_EFFORT_DISPLAY_NAMES: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
}


@dataclass(frozen=True)
class PickerEffort:
    """A single reasoning effort choice surfaced in the AI settings modal."""

    value: str
    label: str


@dataclass(frozen=True)
class PickerModel:
    """A single model choice grouped under its runtime adapter."""

    value: str
    label: str
    supported_efforts: tuple[PickerEffort, ...]


@dataclass(frozen=True)
class PickerAdapter:
    """A single runtime adapter exposed to the picker, with its models."""

    value: str
    label: str
    models: tuple[PickerModel, ...]


def get_picker_choices() -> tuple[PickerAdapter, ...]:
    """Build the picker tree from the tasks facade's structural data.

    The tasks product owns the source of truth — which runtime adapters exist,
    which model identifiers each runs, and which reasoning efforts each model
    supports. This function adds the Slack-app UI labels and packages the
    result into picker-shaped dataclasses for the renderer.

    Display labels fall back to the raw identifier when a new model or adapter
    ships in the tasks runtime without a matching label here, so unknown
    values surface rather than silently disappearing.
    """
    from products.tasks.backend.facade.run_config import (
        RuntimeAdapter,
        get_models_for_runtime_adapter,
        get_supported_reasoning_efforts,
    )

    adapters: list[PickerAdapter] = []
    for adapter in RuntimeAdapter:
        models: list[PickerModel] = []
        for model in get_models_for_runtime_adapter(adapter):
            efforts = tuple(
                PickerEffort(value=e.value, label=REASONING_EFFORT_DISPLAY_NAMES.get(e.value) or e.value)
                for e in get_supported_reasoning_efforts(adapter, model)
            )
            models.append(
                PickerModel(
                    value=model,
                    label=MODEL_DISPLAY_NAMES.get(model) or model,
                    supported_efforts=efforts,
                )
            )
        adapters.append(
            PickerAdapter(
                value=adapter.value,
                label=RUNTIME_ADAPTER_DISPLAY_NAMES.get(adapter.value) or adapter.value,
                models=tuple(models),
            )
        )
    return tuple(adapters)


def _runtime_adapter_label(value: str | None) -> str:
    if not value:
        return "—"
    return RUNTIME_ADAPTER_DISPLAY_NAMES.get(value, value)


def _reasoning_effort_label(value: str | None) -> str:
    if not value:
        return "—"
    return REASONING_EFFORT_DISPLAY_NAMES.get(value, value)


def _model_label_lookup(model: str | None) -> str:
    if not model:
        return "—"
    return MODEL_DISPLAY_NAMES.get(model, model)


def _models_for(runtime_adapter: str) -> tuple[tuple[str, str], ...]:
    """Return `(value, label)` pairs for the modal's model dropdown."""
    for adapter in get_picker_choices():
        if adapter.value == runtime_adapter:
            return tuple((m.value, m.label) for m in adapter.models)
    return ()


def _runtime_adapter_options() -> tuple[tuple[str, str], ...]:
    """Return `(value, label)` pairs for the modal's runtime dropdown."""
    return tuple((a.value, a.label) for a in get_picker_choices())


@dataclass(frozen=True)
class PreferenceSource:
    """Which row contributed the effective `(runtime_adapter, model)` pair.

    Used to render the "Source: …" line on the active-model card so the
    precedence (personal → workspace → unset) is visible at a glance.
    """

    label: str
    is_personal: bool
    is_workspace: bool
    is_unset: bool

    @classmethod
    def personal(cls) -> PreferenceSource:
        return cls(label="Your personal override", is_personal=True, is_workspace=False, is_unset=False)

    @classmethod
    def workspace(cls) -> PreferenceSource:
        return cls(label="Workspace default", is_personal=False, is_workspace=True, is_unset=False)

    @classmethod
    def unset(cls) -> PreferenceSource:
        return cls(label="System default", is_personal=False, is_workspace=False, is_unset=True)


def resolve_source(
    user_row: SlackSettings | None,
    workspace_row: SlackSettings | None,
) -> PreferenceSource:
    """Return where the effective pair came from.

    Mirrors the same atomic-pair rule the resolver uses: a row only "sources"
    the pair when both halves are set on it.
    """
    if user_row and user_row.runtime_adapter and user_row.model:
        return PreferenceSource.personal()
    if workspace_row and workspace_row.runtime_adapter and workspace_row.model:
        return PreferenceSource.workspace()
    return PreferenceSource.unset()


def render_home_view(
    *,
    effective: AISettings,
    user_row: SlackSettings | None,
    workspace_row: SlackSettings | None,
    is_admin: bool,
) -> dict:
    """Render the Block Kit payload for `views.publish` on the App Home tab."""

    source = resolve_source(user_row, workspace_row)
    blocks: list[dict] = []

    blocks.extend(_header_blocks())
    blocks.append({"type": "divider"})
    blocks.extend(_active_model_blocks(effective, source))
    blocks.append({"type": "divider"})
    blocks.extend(_personal_section_blocks(user_row))
    blocks.append({"type": "divider"})
    blocks.extend(_workspace_section_blocks(workspace_row, is_admin=is_admin))
    blocks.append({"type": "divider"})
    blocks.extend(_footer_blocks())

    return {"type": "home", "callback_id": HOME_CALLBACK_ID, "blocks": blocks}


def _header_blocks() -> list[dict]:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "PostHog · AI settings", "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Pick the model that runs when you @PostHog from Slack. Set a personal override for yourself, or a workspace default for everyone.",
                }
            ],
        },
    ]


def _active_model_blocks(effective: AISettings, source: PreferenceSource) -> list[dict]:
    if effective.is_empty:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Active model*\n_Using PostHog's default model._",
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Source: {source.label}"}],
            },
        ]

    runtime_label = _runtime_adapter_label(effective.runtime_adapter)
    model_label = _model_label_lookup(effective.model)
    effort_part = (
        f" · Reasoning: *{_reasoning_effort_label(effective.reasoning_effort)}*" if effective.reasoning_effort else ""
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Active model*\n*{model_label}* · {runtime_label}{effort_part}",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Source: {source.label}"}],
        },
    ]


def _personal_section_blocks(user_row: SlackSettings | None) -> list[dict]:
    """Personal override card. Always editable by the user themselves."""

    has_override = bool(user_row and user_row.runtime_adapter and user_row.model)
    summary = _row_summary(user_row) if has_override else "_No personal override — inheriting the workspace default._"

    actions: list[dict] = [
        {
            "type": "button",
            "action_id": ACTION_EDIT_PERSONAL,
            "text": {"type": "plain_text", "text": "Edit my settings", "emoji": True},
        }
    ]
    if has_override:
        actions.append(
            {
                "type": "button",
                "action_id": ACTION_RESET_PERSONAL,
                "style": "danger",
                "text": {"type": "plain_text", "text": "Reset to workspace default", "emoji": True},
                "confirm": {
                    "title": {"type": "plain_text", "text": "Clear your override?"},
                    "text": {
                        "type": "mrkdwn",
                        "text": "You'll inherit the workspace default until you set new personal preferences.",
                    },
                    "confirm": {"type": "plain_text", "text": "Reset"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            }
        )

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Personal settings*\n{summary}"},
        },
        {"type": "actions", "elements": actions},
    ]


def _workspace_section_blocks(
    workspace_row: SlackSettings | None,
    *,
    is_admin: bool,
) -> list[dict]:
    """Workspace default card. Read-only for non-admins; admins see Edit."""

    has_default = bool(workspace_row and workspace_row.runtime_adapter and workspace_row.model)
    summary = (
        _row_summary(workspace_row)
        if has_default
        else "_No workspace default set — falling back to PostHog's system default._"
    )
    admin_note = "" if is_admin else " _Editable by Slack workspace admins only._"

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Workspace default*{admin_note}\n{summary}"},
        }
    ]
    if is_admin:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": ACTION_EDIT_WORKSPACE,
                        "text": {"type": "plain_text", "text": "Edit workspace default", "emoji": True},
                    }
                ],
            }
        )
    return blocks


def _footer_blocks() -> list[dict]:
    return []


def _row_summary(row: SlackSettings | None) -> str:
    if not row or not row.runtime_adapter or not row.model:
        return "_(none)_"
    parts = [
        f"*Model:* {_model_label_lookup(row.model)}",
        f"*Runtime:* {_runtime_adapter_label(row.runtime_adapter)}",
    ]
    if row.reasoning_effort:
        parts.append(f"*Reasoning:* {_reasoning_effort_label(row.reasoning_effort)}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Edit modal
# ---------------------------------------------------------------------------


def render_edit_modal(
    *,
    scope: EditScope,
    current: AISettings,
    supported_efforts: list[str] | None = None,
) -> dict:
    """Build the Block Kit modal payload for personal or workspace editing.

    `supported_efforts` lets the caller pre-compute which efforts are valid for
    the currently selected model (using
    `products.tasks.backend.temporal.process_task.utils.get_supported_reasoning_efforts`).
    When `None`, the effort block is omitted entirely; the modal re-renders via
    `block_actions` on runtime_adapter / model change to fill it in.
    """

    callback_id = EDIT_MODAL_PERSONAL_CALLBACK_ID if scope == "personal" else EDIT_MODAL_WORKSPACE_CALLBACK_ID
    title = "AI settings (personal)" if scope == "personal" else "AI settings (workspace)"

    runtime_pairs = _runtime_adapter_options()
    runtime_options = [
        {
            "text": {"type": "plain_text", "text": label, "emoji": True},
            "value": value,
        }
        for value, label in runtime_pairs
    ]
    runtime_element: dict[str, Any] = {
        "type": "static_select",
        "action_id": MODAL_ACTION_RUNTIME_ADAPTER,
        "placeholder": {"type": "plain_text", "text": "Pick a runtime"},
        "options": runtime_options,
    }
    if current.runtime_adapter and any(v == current.runtime_adapter for v, _ in runtime_pairs):
        runtime_element["initial_option"] = next(o for o in runtime_options if o["value"] == current.runtime_adapter)
    runtime_block: dict[str, Any] = {
        "type": "input",
        "block_id": MODAL_BLOCK_RUNTIME_ADAPTER,
        "label": {"type": "plain_text", "text": "Runtime"},
        "dispatch_action": True,
        "element": runtime_element,
    }

    model_block: dict[str, Any] | None = None
    if current.runtime_adapter:
        model_options = [
            {
                "text": {"type": "plain_text", "text": label, "emoji": True},
                "value": value,
            }
            for value, label in _models_for(current.runtime_adapter)
        ]
        if model_options:
            model_element: dict[str, Any] = {
                "type": "static_select",
                "action_id": MODAL_ACTION_MODEL,
                "placeholder": {"type": "plain_text", "text": "Pick a model"},
                "options": model_options,
            }
            if current.model and any(o["value"] == current.model for o in model_options):
                model_element["initial_option"] = next(o for o in model_options if o["value"] == current.model)
            model_block = {
                "type": "input",
                "block_id": MODAL_BLOCK_MODEL,
                "label": {"type": "plain_text", "text": "Model"},
                "dispatch_action": True,
                "element": model_element,
            }

    effort_block: dict[str, Any] | None = None
    if supported_efforts:
        effort_options = [
            {
                "text": {"type": "plain_text", "text": _reasoning_effort_label(v), "emoji": True},
                "value": v,
            }
            for v in supported_efforts
        ]
        effort_element: dict[str, Any] = {
            "type": "static_select",
            "action_id": MODAL_ACTION_REASONING_EFFORT,
            "placeholder": {"type": "plain_text", "text": "Pick an effort (optional)"},
            "options": effort_options,
        }
        if current.reasoning_effort and current.reasoning_effort in supported_efforts:
            effort_element["initial_option"] = next(o for o in effort_options if o["value"] == current.reasoning_effort)
        effort_block = {
            "type": "input",
            "block_id": MODAL_BLOCK_REASONING_EFFORT,
            "label": {"type": "plain_text", "text": "Reasoning effort"},
            "optional": True,
            "element": effort_element,
        }

    blocks = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Pick the runtime and model that should handle PostHog Slack requests for you."
                        if scope == "personal"
                        else "Set the default runtime and model for everyone in this Slack workspace."
                    ),
                }
            ],
        },
        runtime_block,
    ]
    if model_block:
        blocks.append(model_block)
    if effort_block:
        blocks.append(effort_block)

    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title, "emoji": True},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def parse_modal_submission(view: dict) -> tuple[str | None, str | None, str | None]:
    """Pull `(runtime_adapter, model, reasoning_effort)` out of a Slack view_submission payload.

    Returns `(None, None, None)` for any block the user didn't fill in. The
    caller validates the triple via `validate_ai_settings`.
    """

    state = view.get("state", {}).get("values", {})

    runtime_adapter = _selected_value(state, MODAL_BLOCK_RUNTIME_ADAPTER, MODAL_ACTION_RUNTIME_ADAPTER)
    model = _selected_value(state, MODAL_BLOCK_MODEL, MODAL_ACTION_MODEL)
    reasoning_effort = _selected_value(state, MODAL_BLOCK_REASONING_EFFORT, MODAL_ACTION_REASONING_EFFORT)
    return runtime_adapter, model, reasoning_effort


def _selected_value(state: dict, block_id: str, action_id: str) -> str | None:
    block = state.get(block_id, {})
    action = block.get(action_id, {})
    selected = action.get("selected_option")
    if isinstance(selected, dict):
        return selected.get("value")
    return None


# ---------------------------------------------------------------------------
# Event + interactivity handlers
# ---------------------------------------------------------------------------
#
# Public entry points are re-exported from `api.py` under matching `_handle_*`
# names so the dispatchers there can call them with minimal extra wiring.
#
# Concurrency model: each Slack interactivity request is short-lived (<3s SLA),
# so all writes use plain Django ORM calls inside the request thread. The
# resolver is read at task-creation time inside the Temporal workflow, not
# here.

import logging as _logging  # noqa: E402

_logger = _logging.getLogger(__name__)

# Re-exported so api.py's dispatch table can reference these by alias.
EDIT_PERSONAL = ACTION_EDIT_PERSONAL
EDIT_WORKSPACE = ACTION_EDIT_WORKSPACE
RESET_PERSONAL = ACTION_RESET_PERSONAL
MODAL_RUNTIME_ADAPTER = MODAL_ACTION_RUNTIME_ADAPTER
MODAL_MODEL = MODAL_ACTION_MODEL


def handle_app_home_opened(event: dict, slack_team_id: str) -> None:
    """Publish the Home tab for the user who just opened it.

    No-op when the slack-app-home flag is off — that way installs without the
    manifest changes still get a benign empty Home tab from Slack's default.
    """

    from products.slack_app.backend.services.ai_settings import resolve_ai_settings

    slack_user_id = event.get("user")
    if not slack_user_id:
        return

    integration = _get_slack_integration(slack_team_id)
    if integration is None:
        return

    # The flag check inside the resolver makes this a no-op for installs that
    # haven't opted in. Loading the rows up front anyway keeps the publish
    # path uniform (the empty-state Home view is still rendered).
    effective = resolve_ai_settings(integration, slack_user_id)
    user_row, workspace_row = _load_rows(integration, slack_user_id)

    slack = SlackIntegration(integration)
    is_admin = _is_admin(slack, integration, slack_user_id)

    view = render_home_view(
        effective=effective,
        user_row=user_row,
        workspace_row=workspace_row,
        is_admin=is_admin,
    )
    try:
        slack.client.views_publish(user_id=slack_user_id, view=view)
    except Exception:
        _logger.exception(
            "slack_app_home_publish_failed",
            extra={"slack_user_id": slack_user_id, "slack_team_id": slack_team_id},
        )


def handle_ai_settings_block_action(payload: dict, action: dict) -> HttpResponse:
    """Dispatch a `block_actions` payload originating from the Home tab or modal."""

    action_id = action.get("action_id")
    slack_team_id = (payload.get("team") or {}).get("id", "")
    slack_user_id = (payload.get("user") or {}).get("id", "")
    trigger_id = payload.get("trigger_id")

    integration = _get_slack_integration(slack_team_id)
    if integration is None:
        return HttpResponse(status=200)

    if action_id == ACTION_EDIT_PERSONAL and trigger_id:
        _open_edit_modal(integration, slack_user_id, scope="personal", trigger_id=trigger_id)
        return HttpResponse(status=200)

    if action_id == ACTION_EDIT_WORKSPACE and trigger_id:
        slack = SlackIntegration(integration)
        if not _is_admin(slack, integration, slack_user_id):
            _post_ephemeral_admin_only(slack, payload)
            return HttpResponse(status=200)
        _open_edit_modal(integration, slack_user_id, scope="workspace", trigger_id=trigger_id)
        return HttpResponse(status=200)

    if action_id == ACTION_RESET_PERSONAL:
        _clear_personal_override(integration, slack_user_id)
        _republish_home(integration, slack_user_id)
        return HttpResponse(status=200)

    if action_id in (MODAL_ACTION_RUNTIME_ADAPTER, MODAL_ACTION_MODEL):
        # Modal re-render: a runtime / model change updates which downstream
        # blocks (model list, effort options) are valid. Push an updated view.
        return _update_modal_after_input_change(payload)

    return HttpResponse(status=200)


def handle_app_home_view_submission(payload: dict) -> HttpResponse | JsonResponse:
    """Handle the Save click on the personal or workspace edit modal."""
    from django.core.exceptions import ValidationError

    from products.slack_app.backend.services.ai_settings import validate_ai_settings

    view = payload.get("view", {})
    callback_id = view.get("callback_id")
    if callback_id not in (EDIT_MODAL_PERSONAL_CALLBACK_ID, EDIT_MODAL_WORKSPACE_CALLBACK_ID):
        return HttpResponse(status=200)

    slack_team_id = (payload.get("team") or {}).get("id", "")
    slack_user_id = (payload.get("user") or {}).get("id", "")

    integration = _get_slack_integration(slack_team_id)
    if integration is None:
        return _modal_error_response("This Slack workspace is no longer connected to PostHog.")

    runtime_adapter, model, reasoning_effort = parse_modal_submission(view)

    try:
        validate_ai_settings(runtime_adapter, model, reasoning_effort)
    except ValidationError as exc:
        return _modal_error_response(_first_validation_message(exc))

    if callback_id == EDIT_MODAL_PERSONAL_CALLBACK_ID:
        _write_row(
            integration,
            slack_user_id=slack_user_id,
            runtime_adapter=runtime_adapter,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    else:
        slack = SlackIntegration(integration)
        if not _is_admin(slack, integration, slack_user_id):
            return _modal_error_response("Only Slack workspace admins can change the workspace default.")
        _write_row(
            integration,
            slack_user_id=None,
            runtime_adapter=runtime_adapter,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    _republish_home(integration, slack_user_id)
    return JsonResponse({"response_action": "clear"})


# ---------------------------------------------------------------------------
# Handler internals
# ---------------------------------------------------------------------------


def _get_slack_integration(slack_team_id: str) -> Integration | None:

    if not slack_team_id:
        return None
    return (
        Integration.objects.select_related("team", "team__organization")
        .filter(kind="slack", integration_id=slack_team_id)
        .first()
    )


def _load_rows(integration: Integration, slack_user_id: str) -> tuple[SlackSettings | None, SlackSettings | None]:
    from products.slack_app.backend.models import SlackSettings

    user_row = SlackSettings.objects.filter(
        slack_workspace_id=integration.integration_id,
        slack_user_id=slack_user_id,
    ).first()
    workspace_row = SlackSettings.objects.filter(
        slack_workspace_id=integration.integration_id,
        slack_user_id__isnull=True,
    ).first()
    return user_row, workspace_row


def _row_to_settings(row: SlackSettings | None) -> AISettings:
    if row is None:
        return AISettings()
    return AISettings(
        runtime_adapter=row.runtime_adapter,
        model=row.model,
        reasoning_effort=row.reasoning_effort,
    )


def _is_admin(slack: SlackIntegration, integration: Integration, slack_user_id: str) -> bool:
    from products.slack_app.backend.services.slack_user_info import is_slack_workspace_admin

    try:
        return is_slack_workspace_admin(slack, integration, slack_user_id)
    except Exception:
        _logger.exception(
            "slack_app_home_is_admin_check_failed",
            extra={"slack_user_id": slack_user_id, "integration_id": integration.id},
        )
        return False


def _open_edit_modal(integration: Integration, slack_user_id: str, *, scope: EditScope, trigger_id: str) -> None:

    user_row, workspace_row = _load_rows(integration, slack_user_id)
    current = _row_to_settings(user_row if scope == "personal" else workspace_row)
    supported = _supported_efforts(current.runtime_adapter, current.model)
    view = render_edit_modal(scope=scope, current=current, supported_efforts=supported)
    slack = SlackIntegration(integration)
    try:
        slack.client.views_open(trigger_id=trigger_id, view=view)
    except Exception:
        _logger.exception(
            "slack_app_home_open_modal_failed",
            extra={"slack_user_id": slack_user_id, "scope": scope},
        )


def _update_modal_after_input_change(payload: dict) -> HttpResponse:
    """Re-render the modal in response to a runtime_adapter or model change.

    Reads the in-flight state from `payload["view"]`, derives the new supported
    efforts (changes when the model changes), and pushes the updated view via
    `views.update`. Nothing is persisted here — the user still has to Save to
    commit.
    """

    view = payload.get("view", {})
    callback_id = view.get("callback_id")
    if callback_id not in (EDIT_MODAL_PERSONAL_CALLBACK_ID, EDIT_MODAL_WORKSPACE_CALLBACK_ID):
        return HttpResponse(status=200)

    runtime_adapter, model, reasoning_effort = parse_modal_submission(view)
    current = AISettings(runtime_adapter=runtime_adapter, model=model, reasoning_effort=reasoning_effort)
    supported = _supported_efforts(runtime_adapter, model)

    scope: EditScope = "personal" if callback_id == EDIT_MODAL_PERSONAL_CALLBACK_ID else "workspace"
    updated_view = render_edit_modal(scope=scope, current=current, supported_efforts=supported)

    slack_team_id = (payload.get("team") or {}).get("id", "")
    integration = _get_slack_integration(slack_team_id)
    if integration is None:
        return HttpResponse(status=200)

    slack = SlackIntegration(integration)
    try:
        slack.client.views_update(view_id=view.get("id"), hash=view.get("hash"), view=updated_view)
    except Exception:
        _logger.exception("slack_app_home_modal_update_failed")
    return HttpResponse(status=200)


def _supported_efforts(runtime_adapter: str | None, model: str | None) -> list[str] | None:
    if not runtime_adapter or not model:
        return None
    from products.tasks.backend.facade.run_config import get_supported_reasoning_efforts

    return [e.value for e in get_supported_reasoning_efforts(runtime_adapter, model)] or None


def _write_row(
    integration: Integration,
    *,
    slack_user_id: str | None,
    runtime_adapter: str | None,
    model: str | None,
    reasoning_effort: str | None,
) -> None:
    """Upsert a SlackSettings row with the given AI settings.

    `default_integration` is required by the existing schema; we point it at
    this integration so a fresh AI-settings-only write still produces a
    coherent row (it doubles as the routing default if no other row exists).
    Existing rows have their AI fields updated in-place.
    """
    from products.slack_app.backend.models import SlackSettings

    SlackSettings.objects.update_or_create(
        slack_workspace_id=integration.integration_id,
        slack_user_id=slack_user_id,
        defaults={
            "default_integration": integration,
            "runtime_adapter": runtime_adapter,
            "model": model,
            "reasoning_effort": reasoning_effort,
        },
    )


def _clear_personal_override(integration: Integration, slack_user_id: str) -> None:
    """Clear just the AI fields on the user's row. Leaves routing alone."""
    from products.slack_app.backend.models import SlackSettings

    SlackSettings.objects.filter(
        slack_workspace_id=integration.integration_id,
        slack_user_id=slack_user_id,
    ).update(
        runtime_adapter=None,
        model=None,
        reasoning_effort=None,
    )


def _republish_home(integration: Integration, slack_user_id: str) -> None:

    from products.slack_app.backend.services.ai_settings import resolve_ai_settings

    user_row, workspace_row = _load_rows(integration, slack_user_id)
    effective = resolve_ai_settings(integration, slack_user_id)
    slack = SlackIntegration(integration)
    is_admin = _is_admin(slack, integration, slack_user_id)
    view = render_home_view(
        effective=effective,
        user_row=user_row,
        workspace_row=workspace_row,
        is_admin=is_admin,
    )
    try:
        slack.client.views_publish(user_id=slack_user_id, view=view)
    except Exception:
        _logger.exception("slack_app_home_republish_failed")


def _modal_error_response(message: str) -> JsonResponse:
    """Slack-format response: keep the modal open and surface an error.

    Slack expects `response_action=errors` with a `block_id`-keyed errors map.
    We attach the error to the runtime block so it's visible without scrolling.
    """

    return JsonResponse(
        {
            "response_action": "errors",
            "errors": {MODAL_BLOCK_RUNTIME_ADAPTER: message[:200]},
        }
    )


def _first_validation_message(exc: Exception) -> str:
    if getattr(exc, "messages", None):
        return exc.messages[0]
    return "Settings could not be saved."


def _post_ephemeral_admin_only(slack: SlackIntegration, payload: dict) -> None:
    """Tell a non-admin that workspace edits are gated.

    The Home tab Edit button is already rendered admin-only, so reaching this
    path means the user came in via a stale view or a hand-crafted payload.
    """
    channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
    if not channel:
        return
    try:
        slack.client.chat_postEphemeral(
            channel=channel,
            user=(payload.get("user") or {}).get("id", ""),
            text="Only Slack workspace admins can change the PostHog workspace default.",
        )
    except Exception:
        _logger.warning("slack_app_home_admin_only_notice_failed")
