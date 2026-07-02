"""Enrich subpackage — DB prep, eligibility, reporting, rate limits."""

# Single source of truth for the always-skip types (already-enriched rows the
# eligibility query and the type-allowlist logic must both exclude). Lives here
# in the package leaf so both m3_enrich (the facade) and enrich.eligibility can
# import it without a cycle — do NOT redefine it in either place.
ALWAYS_SKIP_TYPES = ("observation",)  # already enriched; idempotency

