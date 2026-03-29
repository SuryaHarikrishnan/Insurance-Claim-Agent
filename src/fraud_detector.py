"""
fraud_detector.py - Validation, consistency checks, and fraud heuristics.

Returns a structured report:
  missing_fields   : required fields absent from extracted data
  inconsistencies  : logical / data errors that may block processing
  flags            : suspicious patterns that warrant human review
"""

from datetime import datetime, timedelta
from typing import Optional
from src.utils import get_logger, extract_all_dates, extract_dollar_amounts, parse_date

logger = get_logger()

# ── Configuration ──────────────────────────────────────────────────────────────
REQUIRED_FIELDS          = ["claimant_name", "policy_number", "claim_amount", "incident_date"]
HIGH_AMOUNT_THRESHOLD    = 10_000    # Flag claims above this value
EXTREME_AMOUNT_THRESHOLD = 500_000   # Likely data error or extreme fraud
ROUND_AMOUNT_FLOOR       = 500       # Only flag round numbers above this
OLD_INCIDENT_DAYS        = 730       # Flag incidents older than this many days (2 years)
SAME_DAY_THRESHOLD_HOURS = 24        # Flag same-day filing (incident == today)


# ── Helper ─────────────────────────────────────────────────────────────────────

def _parse_iso(date_str: str) -> Optional[datetime]:
    """Parse an ISO date string (YYYY-MM-DD) or fall back to utils.parse_date."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return parse_date(date_str)


# ── Individual Checks ──────────────────────────────────────────────────────────

def check_missing_fields(data: dict) -> list[str]:
    """Return list of required fields with None / empty values."""
    return [
        f for f in REQUIRED_FIELDS
        if data.get(f) is None or (isinstance(data.get(f), str) and not data[f].strip())
    ]


def check_claim_amount(amount: Optional[float]) -> list[str]:
    """Validate claim amount logic."""
    if amount is None:
        return []
    issues = []
    if amount <= 0:
        issues.append(f"Claim amount is non-positive: {amount}")
    if amount > EXTREME_AMOUNT_THRESHOLD:
        issues.append(f"Claim amount is abnormally high: ${amount:,.2f}")
    return issues


def check_incident_date(date_str: Optional[str]) -> list[str]:
    """Validate that incident date is parseable. Future dates are caught as fraud flags."""
    if not date_str:
        return []
    dt = _parse_iso(date_str)
    if dt is None:
        return [f"Could not parse incident date: '{date_str}'"]
    # Future dates are returned as empty here — handled as a fraud FLAG below
    return []


def check_policy_number_format(policy_number: Optional[str]) -> list[str]:
    """Sanity-check the policy number value."""
    if not policy_number:
        return []
    issues = []
    if len(policy_number) < 4:
        issues.append(f"Policy number suspiciously short: '{policy_number}'")
    if policy_number.upper() in {"N/A", "NA", "NONE", "UNKNOWN", "TBD", "000000"}:
        issues.append(f"Policy number is a placeholder: '{policy_number}'")
    return issues


def check_multiple_amounts(raw_text: str, extracted_amount: Optional[float]) -> list[str]:
    """
    Flag documents containing wildly conflicting dollar amounts
    (>10x spread), which may indicate tampering or a composite document.
    """
    if not raw_text or extracted_amount is None:
        return []
    significant = [a for a in extract_dollar_amounts(raw_text) if a >= 100]
    if len(significant) < 2:
        return []
    max_amt, min_amt = max(significant), min(significant)
    if max_amt > 0 and (max_amt / max(min_amt, 1)) > 10:
        return [
            f"Conflicting dollar amounts in document "
            f"(range: ${min_amt:,.2f}–${max_amt:,.2f}, extracted: ${extracted_amount:,.2f})"
        ]
    return []


def check_multiple_dates(raw_text: str) -> list[str]:
    """Flag documents with an unusually high number of distinct dates."""
    if not raw_text:
        return []
    unique_dates = set(extract_all_dates(raw_text))
    if len(unique_dates) > 5:
        return [
            f"Unusually many distinct dates found ({len(unique_dates)}); "
            "may indicate a composite or altered document."
        ]
    return []


def check_old_incident(date_str: Optional[str]) -> list[str]:
    """Flag incident dates older than OLD_INCIDENT_DAYS (late filing)."""
    if not date_str:
        return []
    dt = _parse_iso(date_str)
    if dt is None:
        return []
    age_days = (datetime.today() - dt).days
    if age_days > OLD_INCIDENT_DAYS:
        return [
            f"Incident date is {age_days} days ago (>{OLD_INCIDENT_DAYS}d); "
            "late filing may indicate backdating."
        ]
    return []


def check_same_day_filing(date_str: Optional[str]) -> list[str]:
    """
    Flag when the incident date is today or yesterday — simultaneous
    claim filing on day of incident is unusual for non-emergency claims.
    """
    if not date_str:
        return []
    dt = _parse_iso(date_str)
    if dt is None:
        return []
    age_days = (datetime.today() - dt).days
    if 0 <= age_days <= 1:
        return [
            f"Claim filed same day as incident ({date_str}); "
            "uncommon outside emergency situations."
        ]
    return []


# ── Fraud Heuristics ───────────────────────────────────────────────────────────

# Phrases in descriptions that strongly indicate fraud or process abuse
_SUSPICIOUS_PHRASES = [
    "cash only", "no receipt", "no receipts", "do not contact",
    "urgent", "immediate payment", "wire transfer", "western union",
    "undocumented", "off-site", "verbal only", "no records",
]


def run_fraud_heuristics(data: dict, raw_text: str) -> list[str]:
    """
    Apply fraud detection heuristics.
    Returns a list of human-readable flag strings.
    """
    flags = []

    amount      = data.get("claim_amount")
    policy      = data.get("policy_number")
    name        = data.get("claimant_name")
    description = (data.get("description") or "").strip()
    date_str    = data.get("incident_date")

    # ── Amount-based flags ────────────────────────────────────────────────────
    if amount is not None:
        if amount > HIGH_AMOUNT_THRESHOLD:
            flags.append(
                f"High-value claim: ${amount:,.2f} exceeds "
                f"review threshold of ${HIGH_AMOUNT_THRESHOLD:,}"
            )
        # Round-number heuristic — $1000 increments above $5000 are suspicious
        if amount >= 5_000 and amount % 1_000 == 0:
            flags.append(
                f"Claim amount is a suspiciously round number: ${amount:,.0f}"
            )

    # ── Identity flags ────────────────────────────────────────────────────────
    if not policy:
        flags.append("Missing policy number — coverage cannot be verified")
    if not name:
        flags.append("Missing claimant name — identity unverifiable")

    # ── Description quality ───────────────────────────────────────────────────
    if len(description) < 30:
        flags.append(
            "Claim description is very short or missing — insufficient detail for review"
        )
    else:
        # Only scan for suspicious phrases if description has real content
        desc_lower = description.lower()
        for phrase in _SUSPICIOUS_PHRASES:
            if phrase in desc_lower:
                flags.append(f"Suspicious phrase in description: '{phrase}'")

    # ── Date-based flags ──────────────────────────────────────────────────────
    # Future incident date — strong fraud signal
    if date_str:
        dt = _parse_iso(date_str)
        if dt and dt > datetime.today():
            flags.append(f"Incident date is in the future: {date_str} — likely fraudulent or data entry error")

    # Same-day filing: only flag if amount is also above a baseline (reduces noise)
    if amount is not None and amount > 1_000:
        flags += check_same_day_filing(date_str)
    flags += check_old_incident(date_str)

    return flags


# ── Main Validation Entry Point ────────────────────────────────────────────────

def validate_claim(data: dict, raw_text: str = "") -> dict:
    """
    Run all validation and fraud checks on extracted claim data.

    Args:
        data:     dict of extracted fields (from parsers.py)
        raw_text: original PDF text for cross-document checks

    Returns:
        {
            "missing_fields":   [...],   # required fields not found
            "inconsistencies":  [...],   # data errors / logical failures
            "flags":            [...],   # suspicious patterns for review
        }
    """
    missing_fields = check_missing_fields(data)

    inconsistencies = []
    inconsistencies += check_claim_amount(data.get("claim_amount"))
    inconsistencies += check_incident_date(data.get("incident_date"))
    inconsistencies += check_policy_number_format(data.get("policy_number"))
    inconsistencies += check_multiple_amounts(raw_text, data.get("claim_amount"))
    inconsistencies += check_multiple_dates(raw_text)

    flags = run_fraud_heuristics(data, raw_text)

    logger.info(
        f"Validation — missing: {missing_fields}, "
        f"inconsistencies: {len(inconsistencies)}, flags: {len(flags)}"
    )

    return {
        "missing_fields":  missing_fields,
        "inconsistencies": inconsistencies,
        "flags":           flags,
    }