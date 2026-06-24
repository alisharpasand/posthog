# RistoReipas overlay: stock PostHog image + the presigned-PUT symbol-set patch
# (3 interpreted Python files). Avoids rebuilding the monorepo.
ARG BASE=ghcr.io/posthog/posthog:latest
FROM ${BASE}
COPY posthog/storage/object_storage.py /code/posthog/storage/object_storage.py
COPY posthog/settings/object_storage.py /code/posthog/settings/object_storage.py
COPY products/error_tracking/backend/logic/symbol_sets.py /code/products/error_tracking/backend/logic/symbol_sets.py
