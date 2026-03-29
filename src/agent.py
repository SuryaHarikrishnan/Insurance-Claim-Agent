"""
agent.py - Orchestrator for the insurance claim processing pipeline.

Coordinates: PDF load → text extraction → field extraction →
             fraud validation → decision making → result assembly.
"""

from pathlib import Path
from src.parsers import parse_pdf
from src.fraud_detector import validate_claim
from src.utils import get_logger

logger = get_logger()


# ─── Decision Engine ───────────────────────────────────────────────────────────

# Fields that, if missing, trigger an automatic REJECT
CRITICAL_FIELDS = {"claimant_name", "policy_number", "claim_amount"}


def make_decision(validation: dict) -> dict:
    """
    Determine claim status based on validation results.

    Priority order:
      REJECT  — missing claimant_name or claim_amount (identity/amount unknowable)
                OR missing policy_number with NO other fraud signals
                OR unparseable amount (non-positive)
      FLAG    — fraud indicators present, OR missing policy_number alongside
                other flags (fraudulent intent likely), OR minor issues
      ACCEPT  — all required fields present, no flags, no inconsistencies
    """
    missing        = validation.get("missing_fields", [])
    inconsistencies = validation.get("inconsistencies", [])
    flags          = validation.get("flags", [])

    # ── Hard REJECT: identity or amount completely unknown ────────────────────
    # claimant_name and claim_amount are absolutely required to process any claim.
    hard_missing = [f for f in missing if f in {"claimant_name", "claim_amount"}]
    if hard_missing:
        return {
            "status": "REJECT",
            "reason": (
                f"Missing critical field(s): {', '.join(hard_missing)}. "
                "Claim cannot be processed without this information."
            ),
        }

    # ── Non-positive amount → reject (can't adjudicate a $0 claim) ───────────
    bad_amount = [i for i in inconsistencies if "non-positive" in i.lower()]
    if bad_amount:
        return {
            "status": "REJECT",
            "reason": "Invalid claim amount: " + "; ".join(bad_amount),
        }

    # ── Missing policy_number: REJECT only when no fraud signals present ──────
    # If fraud flags are present alongside missing policy, it's a FLAG case
    # (the fraud is the primary concern, not the missing field).
    if "policy_number" in missing and not flags:
        return {
            "status": "REJECT",
            "reason": (
                "Missing policy_number. Claim cannot be verified without a valid policy. "
                + (f"Additional issues: {'; '.join(inconsistencies)}" if inconsistencies else "")
            ).strip(),
        }

    # ── Any flags, inconsistencies, or non-critical missing fields → FLAG ─────
    if flags or inconsistencies or missing:
        reasons = []
        if flags:
            reasons.append("Fraud/suspicious indicators: " + "; ".join(flags))
        if inconsistencies:
            reasons.append("Data issues: " + "; ".join(inconsistencies))
        if missing:
            reasons.append(f"Missing field(s): {', '.join(missing)}")
        return {
            "status": "FLAG",
            "reason": " | ".join(reasons),
        }

    # ── All clear ─────────────────────────────────────────────────────────────
    return {
        "status": "ACCEPT",
        "reason": "All required fields present, no inconsistencies detected, claim appears valid.",
    }


# ─── Main Agent Entry Point ────────────────────────────────────────────────────

def process_claim(pdf_path: str) -> dict:
    """
    Full processing pipeline for a single insurance claim PDF.

    Args:
        pdf_path: Absolute or relative path to the PDF file.

    Returns:
        Result dict with keys: file_name, extracted_data, validation, decision.
        On hard failure, returns a minimal error result.
    """
    path = Path(pdf_path)
    file_name = path.name
    logger.info(f"{'='*60}")
    logger.info(f"Processing: {file_name}")

    # ── Step 1 & 2: Load PDF + Extract Text ──────────────────────────────────
    try:
        raw_text, extracted_data = parse_pdf(str(path))
    except Exception as e:
        logger.error(f"Fatal error parsing {file_name}: {e}")
        return _error_result(file_name, f"PDF parsing failed: {e}")

    if raw_text is None:
        return _error_result(file_name, "Could not extract text from PDF (corrupted or empty)")

    # ── Step 3: Fraud Detection & Validation ─────────────────────────────────
    try:
        validation = validate_claim(extracted_data, raw_text)
    except Exception as e:
        logger.error(f"Validation error for {file_name}: {e}")
        validation = {"missing_fields": [], "inconsistencies": [str(e)], "flags": []}

    # ── Step 4: Decision ──────────────────────────────────────────────────────
    decision = make_decision(validation)

    logger.info(f"Decision for {file_name}: {decision['status']} — {decision['reason'][:80]}")

    return {
        "file_name": file_name,
        "extracted_data": extracted_data,
        "validation": validation,
        "decision": decision,
    }


def _error_result(file_name: str, reason: str) -> dict:
    """Build a standardised error result when processing cannot proceed."""
    return {
        "file_name": file_name,
        "extracted_data": {},
        "validation": {
            "missing_fields": ["all"],
            "inconsistencies": [],
            "flags": [],
        },
        "decision": {
            "status": "REJECT",
            "reason": reason,
        },
    }