import { Counter } from 'prom-client'

import { Team } from '../../types'
import { parseJSON } from '../../utils/json-parse'

// A destination that fetches one of PostHog's own ingestion endpoints, authenticating
// as its own project, re-enters the event pipeline. If that re-ingested event then
// re-triggers the same destination, the chain forms an event-forwarding loop that
// doubles traffic on every hop.
//
// - 'disabled': no-op.
// - 'warn': detect the shape and emit a metric, but never block (observe-only).
// - 'enforce': bound the chain using the same hop counter that already protects the
//   `postHogCapture` helper - allow the first SELF_LOOP_MAX_DEPTH hops (stamping an
//   incrementing counter onto each captured event), then break the chain.
export type SelfLoopGuardMode = 'disabled' | 'warn' | 'enforce'

// The event property carrying how many times the current chain has already re-entered
// the pipeline. Shared with `postHogCapture` so a chain mixing both paths is bounded by
// a single counter.
export const EXECUTION_COUNT_PROPERTY = '$hog_function_execution_count'

// Max self-capture hops before the chain is broken. Matches the `postHogCapture` limit.
export const SELF_LOOP_MAX_DEPTH = 10

// Only these paths re-enter the event pipeline. Observability (`/i/v1/logs`) and the
// REST API (`/api/...`, `/decide`) do NOT re-trigger event processing, so a fetch to
// them - even with the project's own token - cannot form an event-forwarding loop.
const INGEST_PATHS = new Set(['/capture', '/batch', '/e', '/track', '/i/v0/e'])

// Top-level body fields that carry the credential a capture request authenticates with.
const API_KEY_FIELDS = ['api_key', 'token', 'api_token'] as const

export const selfLoopGuardCounter = new Counter({
    name: 'cdp_self_loop_guard_total',
    help: 'Count of fetches the self-loop guard acted on, by mode and action',
    labelNames: ['mode', 'action'],
})

export const isPostHogIngestUrl = (urlString: string): boolean => {
    try {
        const url = new URL(urlString)
        const host = url.hostname.toLowerCase()
        const isIngestHost = host === 'posthog.com' || host.endsWith('.posthog.com')
        if (!isIngestHost) {
            return false
        }
        const path = url.pathname.replace(/\/+$/, '') || '/'
        return INGEST_PATHS.has(path)
    } catch {
        return false
    }
}

// The credential a capture request authenticates with - either a top-level body field
// or an `api_key`/`token` query parameter. Deliberately does NOT look at `$lib_token`
// (SDK metadata auto-attached to event properties) or `Authorization` headers, both of
// which carry a token without expressing intent to ingest as that project.
export const extractRequestApiKey = (body: string | null | undefined, urlString: string): string | null => {
    if (body) {
        try {
            const parsed = parseJSON(body)
            if (parsed && typeof parsed === 'object') {
                const obj = parsed as Record<string, unknown>
                for (const field of API_KEY_FIELDS) {
                    if (typeof obj[field] === 'string') {
                        return obj[field] as string
                    }
                }
            }
        } catch {
            // Not JSON - fall through to the query string.
        }
    }
    try {
        const params = new URL(urlString).searchParams
        return params.get('api_key') ?? params.get('token')
    } catch {
        return null
    }
}

const ownsToken = (team: Pick<Team, 'api_token' | 'secret_api_token'>, token: string): boolean => {
    return token === team.api_token || (team.secret_api_token !== null && token === team.secret_api_token)
}

// True when a fetch targets a PostHog ingestion endpoint authenticating with the
// invocation's own project token - i.e. the shape that can form an event-forwarding
// loop. Returns false for cross-project replication (a different project's token),
// non-ingest endpoints, and requests carrying no project credential.
export const isSelfReferentialIngestFetch = (input: {
    url: string
    body: string | null | undefined
    team: Pick<Team, 'api_token' | 'secret_api_token'>
}): boolean => {
    if (!isPostHogIngestUrl(input.url)) {
        return false
    }
    const requestToken = extractRequestApiKey(input.body, input.url)
    return requestToken !== null && ownsToken(input.team, requestToken)
}

// Stamp the incremented hop counter onto the outgoing capture body so the re-ingested
// event carries it forward. Handles both single-event and batch shapes. Returns the
// body unchanged if it can't be parsed as a capture payload.
export const injectExecutionCount = (body: string | null | undefined, count: number): string | null | undefined => {
    if (!body) {
        return body
    }
    let parsed: unknown
    try {
        parsed = parseJSON(body)
    } catch {
        return body
    }
    if (!parsed || typeof parsed !== 'object') {
        return body
    }
    const stamp = (event: Record<string, unknown>): void => {
        const properties = (event.properties && typeof event.properties === 'object' ? event.properties : {}) as Record<
            string,
            unknown
        >
        properties[EXECUTION_COUNT_PROPERTY] = count
        event.properties = properties
    }
    const obj = parsed as Record<string, unknown>
    if (Array.isArray(obj.batch)) {
        for (const entry of obj.batch) {
            if (entry && typeof entry === 'object') {
                stamp(entry as Record<string, unknown>)
            }
        }
    } else {
        stamp(obj)
    }
    return JSON.stringify(parsed)
}
