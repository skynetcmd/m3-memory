# 🏁 Project M3-v3: Milestone 3 Technical Walkthrough (Sovereign Cloud Failover & PII Redaction)

We have successfully implemented and validated **Milestone 3** of Project M3-v3. This milestone provides resilient cloud enclave failover, robust PII redaction gating at the boundary, OS keyring-based credential resolution, and circuit breakers.

---

## 🛠️ 1. Tier 4 Cloud Enclave Configurations
We introduced environment-driven variables inside [config.py](file:///bin/memory/config.py) and [embed.py](file:///bin/memory/embed.py):
*   **Fallback Enable Flag:** `M3_ALLOW_CLOUD_FALLBACK` (defaults to `False` to maintain standard local-first sovereign default).
*   **Enclave URL:** `M3_CLOUD_ENCLAVE_URL` (points to the secure, private cloud-enclave endpoint).
*   **Auth Token Keyring Key:** `M3_CLOUD_AUTH_TOKEN_KEYRING` (used to query the host OS keyring for credentials).
*   **Minimization Level:** `M3_CLOUD_MINIMIZATION_LEVEL` (controls level of PII redaction, defaulting to `standard`).

---

## 🔒 2. PII Redaction Gate Integration
To comply with multi-tenancy privacy models and the local-first philosophy, text inputs are passed through the standard redaction engine BEFORE being transmitted to any remote Tier 4 endpoint:
*   **The Guard:** Connected `bin/chatlog_redaction.py`'s `scrub` function inside both `_embed` and `_embed_many` pipelines.
*   **Data Scopes:** Any email, auth header, generic bearer token, JWT token, AWS/GitHub key, or custom pattern is scrubbed and substituted with `[REDACTED:<group>]`.
*   **Safety Boundary:** The raw, un-redacted payload is never transmitted outside of the secure local machine boundary, ensuring absolute compliance with **GDPR Article 17/20** compliance.

---

## 🔑 3. Keyring Credentials Resolution
*   **Keyring Query:** If `M3_CLOUD_AUTH_TOKEN_KEYRING` is defined (e.g., `service:username`), the pipeline utilizes `auth_utils.py`'s `safe_keyring_get_password` to query the OS vault with single-concurrency thread locks and a 2s timeout.
*   **Env Fallback:** If keyring resolution is offline or fails, the pipeline safely falls back to standard `M3_CLOUD_AUTH_TOKEN` environment values.

---

## 🚦 4. Cloud Enclave Circuit Breaker
We added the fourth circuit breaker, `_CLOUD_BREAKER`, matching the structure of other tiers:
*   **Failure Threshold:** Trips open after `M3_EMBED_BREAKER_CLOUD_THRESHOLD` (default: 3) consecutive failures, skipping the cloud enclave entirely for `M3_EMBED_BREAKER_CLOUD_RESET_SECS` (default: 60s) seconds.
*   **Status Exposure:** Surfaced the breaker status smoothly inside `get_embed_breaker_state` and `reset_embed_breakers`.

---

## 🔄 5. Local Ollama & Cache Fallback
*   If the cloud enclave fails, times out, or experiences high load errors (triggering the breaker), the pipeline logs a warning and routes payloads back to local fallbacks (or returns `None`).
*   The cache checks are performed in SQLite first to prevent any redundant requests in the first place, ensuring maximum efficiency.

---

## 🚀 6. Test Suite & Consistency Validation
All Tier 4 logic has been validated through a new test suite:
*   **Test File:** [tests/test_tier4_cloud.py](file:///tests/test_tier4_cloud.py)
*   **Test Cases:**
    1.  `test_tier4_fallback_disabled_by_default`: Ensures fallback remains strictly opt-in.
    2.  `test_tier4_fallback_triggered_with_redaction`: Verifies the redaction gate successfully scrubs PII and passes the keyring token before HTTP post.
    3.  `test_tier4_circuit_breaker`: Verifies the enclave circuit breaker trips open after threshold failures.
*   **Test Execution Result:** **100% Passed (3 passed in 0.26s)**.

---

## 📜 7. Consistency with `DESIGN_PHILOSOPHIES.md`
All newly added files and code modifications are fully aligned with the consolidate design principles:
*   **Modularity (§2):** Keyring lookups and PII scrubbing are imported dynamically to prevent load-time dependency cycles.
*   **Robustness (§3):** Unexpected errors in local LLM selections are caught and safely delegated to cloud fallback or logged as warnings instead of crashing the pipeline.
*   **Hardening & Security (§6):** Strict HTTP timeouts, Keyring thread bounds, and PII redaction are integrated directly into the transport boundaries.
*   **Privacy & Tenancy (§7):** No original sensitive user tokens or credentials ever leave local disk; only redacted text passes through cloud boundaries.
