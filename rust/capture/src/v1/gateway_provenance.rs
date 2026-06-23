// AI-gateway provenance verification.
//
// The gateway proxies a customer's LLM calls, bills them against a prepaid
// wallet, and posts an `$ai_generation` event per call through capture with the
// team token. Those events must be excluded from the team's billable AIO usage
// (otherwise the wallet spend is double-billed). The exclusion can't trust the
// client-settable `$ai_gateway*` properties, so the gateway HMAC-signs each
// event and capture verifies the signature here, at the edge, before the
// llm_events quota limiter runs.
//
// A valid, fresh signature stamps a trusted `$ai_gateway_verified` and the event
// is exempted from the limiter; anything else has its whole `$ai_gateway*`
// namespace stripped so a forged marker can't survive.

use axum::http::HeaderMap;
use chrono::{DateTime, Utc};
use hmac::{Hmac, Mac};
use serde_json::value::RawValue;
use serde_json::{Map, Value};
use sha2::Sha256;

use super::constants::{
    POSTHOG_AI_GATEWAY_REQUEST_ID, POSTHOG_AI_GATEWAY_SIGNATURE, POSTHOG_AI_GATEWAY_SIGNED_AT,
};

type HmacSha256 = Hmac<Sha256>;

const GATEWAY_PREFIX: &str = "$ai_gateway";
const VERIFIED_PROPERTY: &str = "$ai_gateway_verified";

/// How far `signed_at` may sit from capture's receive time. Capture stamps the
/// event, so there's no ingestion lag and wall-clock now is the capture time;
/// the window only absorbs gateway/capture clock skew.
pub const FRESHNESS_WINDOW_SECS: i64 = 5 * 60;

/// The provenance signature carried on the request headers.
#[derive(Debug, Clone)]
pub struct GatewaySignature {
    pub signature: String,
    pub signed_at: String,
    pub request_id: String,
}

/// Reads the provenance headers. Returns `None` unless both the signature and
/// signed_at are present; `request_id` defaults to empty to match a signature
/// computed over an empty nonce.
pub fn parse_signature(headers: &HeaderMap) -> Option<GatewaySignature> {
    Some(GatewaySignature {
        signature: header_str(headers, POSTHOG_AI_GATEWAY_SIGNATURE)?,
        signed_at: header_str(headers, POSTHOG_AI_GATEWAY_SIGNED_AT)?,
        request_id: header_str(headers, POSTHOG_AI_GATEWAY_REQUEST_ID).unwrap_or_default(),
    })
}

fn header_str(headers: &HeaderMap, name: &str) -> Option<String> {
    headers.get(name)?.to_str().ok().map(str::to_owned)
}

/// Verifies the HMAC against the gateway's canonical tuple and checks freshness.
pub fn verify(
    secret: &[u8],
    token: &str,
    distinct_id: &str,
    sig: &GatewaySignature,
    now: DateTime<Utc>,
) -> bool {
    let message = canonical(&[token, distinct_id, &sig.request_id, &sig.signed_at]);
    verify_hmac(secret, &message, &sig.signature) && is_fresh(&sig.signed_at, now)
}

// Length-prefixed encoding of the four fields: each is its big-endian u32 byte
// length followed by its bytes. distinct_id is customer-controlled, so a plain
// delimiter (it can contain any byte) wouldn't be injective; length-prefixing
// means no field's content can shift another's boundary. Must match the gateway
// signer byte-for-byte (ai-gateway internal/emitter/signer.go).
fn canonical(fields: &[&str]) -> Vec<u8> {
    let mut buf = Vec::with_capacity(fields.iter().map(|f| f.len() + 4).sum());
    for f in fields {
        buf.extend_from_slice(&(f.len() as u32).to_be_bytes());
        buf.extend_from_slice(f.as_bytes());
    }
    buf
}

// HMAC-SHA256 over the canonical message, signature as lowercase hex. Compared
// in constant time via `verify_slice`.
fn verify_hmac(secret: &[u8], message: &[u8], signature_hex: &str) -> bool {
    let Ok(expected) = hex::decode(signature_hex) else {
        return false;
    };
    let Ok(mut mac) = HmacSha256::new_from_slice(secret) else {
        return false;
    };
    mac.update(message);
    mac.verify_slice(&expected).is_ok()
}

fn is_fresh(signed_at: &str, now: DateTime<Utc>) -> bool {
    match DateTime::parse_from_rfc3339(signed_at) {
        Ok(t) => (now - t.with_timezone(&Utc)).num_seconds().abs() <= FRESHNESS_WINDOW_SECS,
        Err(_) => false,
    }
}

/// Stamps the trusted billing marker, overwriting any client-supplied value. The
/// other `$ai_gateway*` props are now trusted (they rode a valid signature), so
/// they survive.
pub fn stamp_verified(props: &mut Map<String, Value>) {
    props.insert(VERIFIED_PROPERTY.to_string(), Value::Bool(true));
}

/// Removes the whole `$ai_gateway*` namespace, including any forged
/// `$ai_gateway_verified`, so an unverified event can't reach billing as trusted.
pub fn strip_gateway(props: &mut Map<String, Value>) {
    props.retain(|k, _| !k.starts_with(GATEWAY_PREFIX));
}

/// Cheap pre-check on the raw bytes: does the property object plausibly contain a
/// `$ai_gateway*` key? Lets the hot path skip the parse for the common case (an
/// SDK `$ai_*` event that never went through the gateway).
///
/// JSON `\u` escapes can encode `$ai_gateway` as bytes that don't contain it
/// literally (e.g. `"$ai_gateway_verified"`), which serde decodes back to
/// the real key on parse — so any `\u` escape also forces the full parse rather
/// than risk an obfuscated forged marker slipping through unstripped. Both checks
/// can false-positive, which only costs a wasted parse.
pub fn has_gateway_props(properties: &RawValue) -> bool {
    let raw = properties.get();
    raw.contains("\"$ai_gateway") || raw.contains("\\u")
}

/// Strips the `$ai_gateway*` namespace from raw properties. Returns `None` when
/// nothing changed (or the properties don't parse), so the caller keeps the
/// original bytes and avoids a needless reserialize.
pub fn strip_gateway_raw(properties: &RawValue) -> Option<Box<RawValue>> {
    let mut map: Map<String, Value> = serde_json::from_str(properties.get()).ok()?;
    let before = map.len();
    strip_gateway(&mut map);
    if map.len() == before {
        return None;
    }
    reserialize(map)
}

/// Stamps the trusted marker onto raw properties. Returns `None` if the
/// properties don't parse (a genuine gateway event always emits valid JSON, so
/// the caller treats this as a non-stamp and the event stays counted).
pub fn stamp_verified_raw(properties: &RawValue) -> Option<Box<RawValue>> {
    let mut map: Map<String, Value> = serde_json::from_str(properties.get()).ok()?;
    stamp_verified(&mut map);
    reserialize(map)
}

fn reserialize(map: Map<String, Value>) -> Option<Box<RawValue>> {
    RawValue::from_string(serde_json::to_string(&Value::Object(map)).ok()?).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &[u8] = b"test-signing-secret";
    const TOKEN: &str = "phc_test";
    const DISTINCT_ID: &str = "user-7";

    fn sign(token: &str, distinct_id: &str, request_id: &str, signed_at: &str) -> String {
        let mut mac = HmacSha256::new_from_slice(SECRET).unwrap();
        mac.update(&canonical(&[token, distinct_id, request_id, signed_at]));
        hex::encode(mac.finalize().into_bytes())
    }

    fn now() -> DateTime<Utc> {
        "2026-05-28T10:00:00Z".parse().unwrap()
    }

    fn valid_sig(request_id: &str) -> GatewaySignature {
        let signed_at = "2026-05-28T10:00:00Z".to_string();
        GatewaySignature {
            signature: sign(TOKEN, DISTINCT_ID, request_id, &signed_at),
            signed_at,
            request_id: request_id.to_string(),
        }
    }

    #[test]
    fn verifies_a_valid_fresh_signature() {
        assert!(verify(
            SECRET,
            TOKEN,
            DISTINCT_ID,
            &valid_sig("req-1"),
            now()
        ));
    }

    #[test]
    fn verifies_with_an_empty_request_id() {
        assert!(verify(SECRET, TOKEN, DISTINCT_ID, &valid_sig(""), now()));
    }

    #[test]
    fn rejects_a_tampered_distinct_id() {
        assert!(!verify(SECRET, TOKEN, "user-8", &valid_sig("req-1"), now()));
    }

    #[test]
    fn rejects_a_tampered_token() {
        assert!(!verify(
            SECRET,
            "phc_other",
            DISTINCT_ID,
            &valid_sig("req-1"),
            now()
        ));
    }

    #[test]
    fn rejects_a_garbage_signature() {
        let mut sig = valid_sig("req-1");
        sig.signature = "deadbeef".to_string();
        assert!(!verify(SECRET, TOKEN, DISTINCT_ID, &sig, now()));
    }

    #[test]
    fn rejects_a_non_hex_signature() {
        let mut sig = valid_sig("req-1");
        sig.signature = "not-hex".to_string();
        assert!(!verify(SECRET, TOKEN, DISTINCT_ID, &sig, now()));
    }

    #[test]
    fn rejects_a_wrong_secret() {
        let signed_at = "2026-05-28T10:00:00Z".to_string();
        let mut mac = HmacSha256::new_from_slice(b"other-secret").unwrap();
        mac.update(&canonical(&[TOKEN, DISTINCT_ID, "req-1", &signed_at]));
        let sig = GatewaySignature {
            signature: hex::encode(mac.finalize().into_bytes()),
            signed_at,
            request_id: "req-1".to_string(),
        };
        assert!(!verify(SECRET, TOKEN, DISTINCT_ID, &sig, now()));
    }

    #[test]
    fn distinct_id_cannot_shift_field_boundaries() {
        // A signature for (distinct_id="user", request_id="req") must NOT verify
        // when the boundary-shifting candidate (distinct_id="user\nreq",
        // request_id="") is presented. A delimiter join would collide here;
        // length-prefixing keeps the two encodings distinct.
        let signed_at = "2026-05-28T10:00:00Z".to_string();
        let signature = sign(TOKEN, "user", "req", &signed_at);
        let forged = GatewaySignature {
            signature,
            signed_at,
            request_id: String::new(),
        };
        assert!(!verify(SECRET, TOKEN, "user\nreq", &forged, now()));
    }

    #[test]
    fn rejects_a_stale_signature() {
        let stale = (now() - chrono::Duration::seconds(FRESHNESS_WINDOW_SECS + 1)).to_rfc3339();
        let sig = GatewaySignature {
            signature: sign(TOKEN, DISTINCT_ID, "req-1", &stale),
            signed_at: stale,
            request_id: "req-1".to_string(),
        };
        assert!(!verify(SECRET, TOKEN, DISTINCT_ID, &sig, now()));
    }

    #[test]
    fn accepts_a_signature_at_the_window_edge() {
        let edge = (now() - chrono::Duration::seconds(FRESHNESS_WINDOW_SECS)).to_rfc3339();
        let sig = GatewaySignature {
            signature: sign(TOKEN, DISTINCT_ID, "req-1", &edge),
            signed_at: edge,
            request_id: "req-1".to_string(),
        };
        assert!(verify(SECRET, TOKEN, DISTINCT_ID, &sig, now()));
    }

    #[test]
    fn rejects_an_unparseable_signed_at() {
        let sig = GatewaySignature {
            signature: sign(TOKEN, DISTINCT_ID, "req-1", "not-a-date"),
            signed_at: "not-a-date".to_string(),
            request_id: "req-1".to_string(),
        };
        assert!(!verify(SECRET, TOKEN, DISTINCT_ID, &sig, now()));
    }

    #[test]
    fn parse_requires_signature_and_signed_at() {
        let mut headers = HeaderMap::new();
        assert!(parse_signature(&headers).is_none());
        headers.insert(POSTHOG_AI_GATEWAY_SIGNATURE, "abc".parse().unwrap());
        assert!(parse_signature(&headers).is_none());
        headers.insert(
            POSTHOG_AI_GATEWAY_SIGNED_AT,
            "2026-05-28T10:00:00Z".parse().unwrap(),
        );
        let sig = parse_signature(&headers).unwrap();
        assert_eq!(sig.signature, "abc");
        assert_eq!(sig.request_id, "");
    }

    #[test]
    fn stamp_verified_overwrites_a_client_value_and_keeps_other_props() {
        let mut props: Map<String, Value> = serde_json::from_str(
            r#"{"$ai_gateway": true, "$ai_gateway_verified": false, "$ai_model": "claude"}"#,
        )
        .unwrap();
        stamp_verified(&mut props);
        assert_eq!(props["$ai_gateway_verified"], Value::Bool(true));
        assert_eq!(props["$ai_gateway"], Value::Bool(true));
        assert_eq!(props["$ai_model"], Value::String("claude".to_string()));
    }

    #[test]
    fn strip_gateway_removes_the_whole_namespace_including_a_forged_marker() {
        let mut props: Map<String, Value> = serde_json::from_str(
            r#"{"$ai_gateway": true, "$ai_gateway_verified": true, "$ai_model": "claude"}"#,
        )
        .unwrap();
        strip_gateway(&mut props);
        assert!(!props.contains_key("$ai_gateway"));
        assert!(!props.contains_key("$ai_gateway_verified"));
        assert_eq!(props["$ai_model"], Value::String("claude".to_string()));
    }

    fn raw(s: &str) -> Box<RawValue> {
        RawValue::from_string(s.to_string()).unwrap()
    }

    #[test]
    fn has_gateway_props_detects_a_key_but_not_a_bare_value() {
        assert!(has_gateway_props(&raw(r#"{"$ai_gateway_verified": true}"#)));
        assert!(!has_gateway_props(&raw(r#"{"$ai_model": "claude"}"#)));
        assert!(!has_gateway_props(&raw(
            r#"{"note": "mentions ai_gateway"}"#
        )));
    }

    #[test]
    fn has_gateway_props_catches_a_unicode_escaped_key() {
        // "\\u0024" decodes to "$", so this key parses to "$ai_gateway_verified"
        // even though the raw bytes lack the literal prefix. The \\u fallback must
        // catch it so the strip path isn't skipped on a forged escaped marker.
        assert!(has_gateway_props(&raw(
            r#"{"\u0024ai_gateway_verified": true}"#
        )));
    }

    #[test]
    fn strip_gateway_raw_returns_none_when_nothing_to_strip() {
        assert!(strip_gateway_raw(&raw(r#"{"$ai_model": "claude"}"#)).is_none());
        let stripped =
            strip_gateway_raw(&raw(r#"{"$ai_gateway": true, "$ai_model": "x"}"#)).unwrap();
        assert!(!stripped.get().contains("$ai_gateway"));
        assert!(stripped.get().contains("$ai_model"));
    }

    #[test]
    fn strip_gateway_raw_strips_a_unicode_escaped_key() {
        // The escaped key decodes to "$ai_gateway_verified" on parse, so strip
        // (which matches decoded keys) removes it like any other gateway prop.
        let stripped = strip_gateway_raw(&raw(
            r#"{"\u0024ai_gateway_verified": true, "$ai_model": "x"}"#,
        ))
        .unwrap();
        assert!(!stripped.get().contains("ai_gateway"));
        assert!(stripped.get().contains("$ai_model"));
    }

    #[test]
    fn stamp_verified_raw_adds_the_marker() {
        let stamped = stamp_verified_raw(&raw(r#"{"$ai_gateway": true}"#)).unwrap();
        let map: Map<String, Value> = serde_json::from_str(stamped.get()).unwrap();
        assert_eq!(map["$ai_gateway_verified"], Value::Bool(true));
        assert_eq!(map["$ai_gateway"], Value::Bool(true));
    }

    #[test]
    fn accepts_the_cross_language_known_answer() {
        // The exact hex the Go signer produces for this input (ai-gateway
        // internal/emitter/signer_test.go TestSigner_KnownAnswer). If either
        // side's length-prefixed canonical form drifts, this fails and real
        // gateway signatures stop verifying.
        let sig = GatewaySignature {
            signature: "a3de0bf6819919c28f6bf6e8b61f01d53d4f313bd57a67e4a31d3f4fc1161351"
                .to_string(),
            signed_at: "2026-05-28T10:00:00Z".to_string(),
            request_id: "req-123".to_string(),
        };
        let now: DateTime<Utc> = "2026-05-28T10:00:00Z".parse().unwrap();
        assert!(verify(
            b"test-signing-secret",
            "phc_test",
            "user-7",
            &sig,
            now
        ));
    }
}
