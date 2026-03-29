"""
parsers.py - PDF text extraction and intelligent field parsing.

Supports two form layouts observed in the wild:
  • Cigna Medical Claim Form  (table-based, pdfplumber reads row-by-row)
  • HCFA-1500                 (table-based, columns merge into single lines)

Extraction strategy (priority order per field):
  1. Form-specific regex tuned to each layout's exact pdfplumber output
  2. Generic keyword-proximity search (works on free-text/prose claims too)
  3. Broad heuristic fallback (e.g. largest dollar amount, first valid date)
"""

import re
from datetime import datetime
from typing import Optional
from pathlib import Path

try:
    import pdfplumber
    PDF_BACKEND = "pdfplumber"
except ImportError:
    pdfplumber = None
    PDF_BACKEND = None

try:
    import fitz  # PyMuPDF
    if PDF_BACKEND is None:
        PDF_BACKEND = "pymupdf"
except ImportError:
    fitz = None

from src.utils import (
    clean_text, normalize_field_value, parse_date,
    extract_all_dates, extract_dollar_amounts, extract_policy_numbers,
    get_logger,
)

logger = get_logger()

_POLICY_BLOCKLIST = {
    "OMB-0938-0008",  # HCFA form approval number
    "0938-0008",
}


_NAME_STOPWORDS = {
    "medicare","medicaid","champus","champva","health","insurance","patient",
    "insured","primary","customer","service","address","claim","policy","group",
    "birth","date","last","first","middle","name","other","employer","school",
    "program","plan","carrier","physician","supplier","facility","signature",
    "certification","approved","notice","form","hcfa","cigna","medical","dental",
    "pharmacy","accident","illness","injury","employment","status","single",
    "married","employed","student","self","spouse","child","number","telephone",
    "daytime","account","federal","billing","provider","referring","diagnosis",
    "procedure","modifier",
}


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: str) -> Optional[str]:
    path = Path(pdf_path)
    if not path.exists():
        logger.error(f"PDF not found: {pdf_path}")
        return None

    if pdfplumber:
        try:
            pages = []
            with pdfplumber.open(str(path)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        pages.append(t)
            if pages:
                logger.info(f"[pdfplumber] Extracted {len(pages)} page(s) from {path.name}")
                return clean_text("\n".join(pages))
        except Exception as e:
            logger.warning(f"pdfplumber failed for {path.name}: {e}")

    if fitz:
        try:
            pages = []
            doc = fitz.open(str(path))
            for page in doc:
                t = page.get_text()
                if t:
                    pages.append(t)
            doc.close()
            if pages:
                logger.info(f"[PyMuPDF] Extracted {len(pages)} page(s) from {path.name}")
                return clean_text("\n".join(pages))
        except Exception as e:
            logger.warning(f"PyMuPDF failed for {path.name}: {e}")

    logger.error(f"No working PDF backend for {path.name}")
    return None


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _after_keyword(text, keywords, pattern, window=150):
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            snippet = text[m.end(): m.end() + window]
            hit = re.search(pattern, snippet, re.IGNORECASE)
            if hit:
                return normalize_field_value(hit.group(0))
    return None


def _text_after_keyword(text, keywords, window=100):
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            snippet = text[m.end(): m.end() + window]
            hit = re.match(r"[\s:]*([A-Za-z][^\n\r:;,\d]{2,60})", snippet)
            if hit:
                val = normalize_field_value(hit.group(1))
                if len(val) >= 3:
                    return val
    return None


def _is_valid_name(s):
    if not s or len(s) < 4:
        return False
    if s.strip("()").lower() in _NAME_STOPWORDS:
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    alpha_ratio = sum(c.isalpha() or c in " ,'-." for c in s) / len(s)
    return alpha_ratio >= 0.7


# ---------------------------------------------------------------------------
# Claimant name
# ---------------------------------------------------------------------------

def extract_claimant_name(text: str) -> Optional[str]:
    # 1. Cigna: "A1. PRIMARY CUSTOMER'S NAME (Last, First, M.I.) Green, Stephanie"
    m = re.search(
        r"(?:A1\.\s*)?PRIMARY\s+CUSTOMER'?S?\s+NAME\s*\([^)]*\)\s+"
        r"([A-Z][a-zA-Z'\-]+,\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?)",
        text, re.IGNORECASE
    )
    if m and _is_valid_name(m.group(1)):
        return normalize_field_value(m.group(1))

    # 2. HCFA: "2. PATIENT'S NAME (Last, First, MI) Alvarez, Katherine"
    m = re.search(
        r"(?:2\.\s*)?PATIENT'?S?\s+NAME\s*\([^)]*\)\s+"
        r"([A-Z][a-zA-Z'\-]+,\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?)",
        text, re.IGNORECASE
    )
    if m and _is_valid_name(m.group(1)):
        return normalize_field_value(m.group(1))

    # 3. HCFA insured name variant
    m = re.search(
        r"(?:4\.\s*)?INSURED'?S?\s+NAME\s+"
        r"([A-Z][a-zA-Z'\-]+,\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?)",
        text, re.IGNORECASE
    )
    if m and _is_valid_name(m.group(1)):
        return normalize_field_value(m.group(1))

    # 4. Generic keyword proximity
    val = _text_after_keyword(text, [
        "claimant name","claimant:","insured name","name of insured",
        "name of claimant","policyholder name","policyholder:",
        "submitted by","filed by","applicant:",
    ])
    if val and _is_valid_name(val):
        return val

    # 5. Any "Surname, Firstname" pair not in stopwords
    for m in re.finditer(r"\b([A-Z][a-zA-Z'\-]{2,}),\s+([A-Z][a-zA-Z'\-]{2,})\b", text):
        surname, first = m.group(1), m.group(2)
        if surname.lower() in _NAME_STOPWORDS or first.lower() in _NAME_STOPWORDS:
            continue
        candidate = f"{surname}, {first}"
        if _is_valid_name(candidate):
            return candidate

    return None


# ---------------------------------------------------------------------------
# Policy number
# ---------------------------------------------------------------------------

def extract_policy_number(text: str) -> Optional[str]:
    # 1. Cigna ACCOUNT NO. field — "E. ACCOUNT NO. HMO-2022-62350"
    #    The field is blank if next token is "F." (employer label) or similar
    m = re.search(
        r"ACCOUNT\s+NO\.?\s+([A-Z]{2,5}-\d{4}-\d{3,6})(?=\s)",
        text, re.IGNORECASE
    )
    if m:
        return m.group(1).upper()

    # 2. HCFA policy group — pdfplumber always fragments the value across 3 cells:
    #    Line 1: "11. INS HMO-2"       ← prefix + first 2 digits of year
    #    Line 2: "URED'S 022-31 URANC" ← remaining year digits + first part of number
    #    Line 3: "POLICY 643 E PLAN"   ← last digits of number
    #
    #    Full value: HMO-2 + 022-31 + 643  →  HMO-2022-31643
    m = re.search(
        r"11\.\s+INS\s+([A-Z]{2,5}-\d{1,2})"   # "11. INS HMO-2"
        r"[\s\S]{0,60}?"                          # skip to next line
        r"URED'?S?\s+(\d{2,3}-\d{2,4})"          # "URED'S 022-31"
        r"[\s\S]{0,60}?"                          # skip to next line
        r"POLICY\s+(\d{1,4})",                    # "POLICY 643"
        text, re.IGNORECASE
    )
    if m:
        # Stitch: prefix="HMO-2", mid="022-31", tail="643"
        # → "HMO-2" + "022" = "HMO-2022", then "-" + "31" + "643" = "-31643"
        prefix = m.group(1)          # e.g. "HMO-2"
        mid    = m.group(2)          # e.g. "022-31"
        tail   = m.group(3)          # e.g. "643"
        mid_parts = mid.split("-")   # ["022", "31"]
        stitched = prefix + mid_parts[0] + "-" + mid_parts[1] + tail
        # Should now look like "HMO-2022-31643"
        if re.match(r"[A-Z]{2,5}-\d{4}-\d{3,6}$", stitched, re.IGNORECASE):
            return stitched.upper()

    # 3. Full prefixed policy number anywhere  POL-2020-55082 / INS-2023-12345
    #    Skip known form-metadata codes (e.g. OMB approval numbers)
    for m in re.finditer(r"\b([A-Z]{2,5}-\d{4}-\d{3,6})\b", text, re.IGNORECASE):
        val = m.group(1).upper()
        if val not in _POLICY_BLOCKLIST:
            return val

    # 4. Stitch fragmented HCFA columns: "POL-20" ... "20-550" ... "82"
    m = re.search(
        r"((?:POL|INS|CGN|HMO|PPO)-\d{2,4})\D{0,40}(\d{2}-\d{3,6})",
        text, re.IGNORECASE
    )
    if m:
        stitched = m.group(1) + m.group(2)
        if re.match(r"[A-Z]{2,5}-\d{4}-\d{3,6}$", stitched, re.IGNORECASE):
            return stitched.upper()

    # 5. HCFA "1a. INSURED'S I.D. NUMBER U808631930"
    #    Only accept if it doesn't look like a bare Cigna member ID (U + 9 digits)
    m = re.search(
        r"(?:1a\.\s*)?INSURED'?S?\s+I\.?D\.?\s+NUMBER\s+([A-Z0-9]{6,15})",
        text, re.IGNORECASE
    )
    if m:
        val = m.group(1).upper()
        if not re.match(r"^U\d{9}$", val) and any(c.isdigit() for c in val):
            return val

    # 6. Cigna "D. CIGNA ID NUMBER" — member ID, usable as last resort
    m = re.search(
        r"(?:D\.\s*)?CIGNA\s+ID\s+NUMBER\s+([A-Z0-9]{6,15})",
        text, re.IGNORECASE
    )
    if m:
        val = m.group(1).upper()
        # Only return if clearly not a bare 9-digit member ID  
        if not re.match(r"^U\d{9}$", val):
            return val

    # 6b. Broader HCFA stitch: find any policy-like prefix in the HCFA policy block
    #     and stitch with numeric fragments that follow within ~300 chars
    m = re.search(r"\b((?:POL|INS|CGN|HMO|PPO)-\d{2,4})\b", text, re.IGNORECASE)
    if m:
        prefix = m.group(1)
        after  = text[m.end(): m.end() + 300]
        # Look for "NN-NNN" continuation fragment
        cont = re.search(r"(\d{2}-\d{3,6})", after)
        if cont:
            stitched = prefix + cont.group(1)
            norm = re.sub(r"([A-Z]{2,5})(\d{4})(\d{3,6})", r"\1-\2-\3", stitched)
            if re.match(r"[A-Z]{2,5}-\d{4}-\d{3,6}$", norm, re.IGNORECASE):
                return norm.upper()


    # 7. utils regex suite
    candidates = extract_policy_numbers(text)
    if candidates:
        return candidates[0]

    # 8. Generic fallback
    m = re.search(
        r"policy\s*(?:number|no|#|id)?\s*[:\s#]*([A-Z0-9][-A-Z0-9]{4,14})",
        text, re.IGNORECASE
    )
    if m:
        val = normalize_field_value(m.group(1)).upper()
        if any(c.isdigit() for c in val):
            return val

    return None


# ---------------------------------------------------------------------------
# Claim amount
# ---------------------------------------------------------------------------

def extract_claim_amount(text: str) -> Optional[float]:
    # 1. Standard "28. TOTAL CHARGE $X,XXX.XX"
    m = re.search(
        r"(?:28\.\s*)?TOTAL\s+CHARGE\s+\$\s*([\d,]+(?:\.\d{1,2})?)",
        text, re.IGNORECASE
    )
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # 2. "NOT PROVIDED" sentinel
    if re.search(r"TOTAL\s+CHARGE\s+NOT\s+PROVIDED", text, re.IGNORECASE):
        return None

    # 3. Keyword proximity
    amount_kws = [
        "total charge","claim amount","amount claimed","total claim",
        "damage cost","loss amount","requested amount","total loss",
        "amount of claim","claimed amount","balance due",
    ]
    dollar_pat = r"\$\s?([\d,]+(?:\.\d{1,2})?)"
    for kw in amount_kws:
        for m in re.finditer(re.escape(kw), text, re.IGNORECASE):
            snippet = text[m.end(): m.end() + 120]
            hit = re.search(dollar_pat, snippet)
            if hit:
                try:
                    return float(hit.group(1).replace(",", ""))
                except ValueError:
                    pass

    # 4. Largest dollar amount in document
    amounts = extract_dollar_amounts(text)
    if amounts:
        return max(amounts)

    return None


# ---------------------------------------------------------------------------
# Incident date
# ---------------------------------------------------------------------------

DATE_RE = (
    r"\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}"
    r"|\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2}"
    r"|(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}"
    r"|\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2,4}"
)


def _to_iso(raw: str) -> Optional[str]:
    dt = parse_date(raw)
    return dt.strftime("%Y-%m-%d") if dt else None


def extract_incident_date(text: str) -> Optional[str]:
    # 1. HCFA "14. DATE OF CURRENT ILLNESS / INJURY"
    m = re.search(
        r"(?:14\.\s*)?DATE\s+OF\s+CURRENT\s+(?:ILLNESS|INJURY|PREGNANCY)?"
        r"[^\d]{0,30}(" + DATE_RE + r")",
        text, re.IGNORECASE
    )
    if m:
        iso = _to_iso(m.group(1))
        if iso:
            return iso

    # 2. Cigna "D. DATE OF ACCIDENT / BEGINNING OF ILLNESS"
    m = re.search(
        r"(?:D\.\s*)?DATE\s+OF\s+(?:ACCIDENT|BEGINNING\s+OF\s+ILLNESS|LOSS)"
        r"[^\d]{0,30}(" + DATE_RE + r")",
        text, re.IGNORECASE
    )
    if m:
        iso = _to_iso(m.group(1))
        if iso:
            return iso

    # 3. Generic keyword proximity
    date_kws = [
        "incident date","date of incident","date of accident","accident date",
        "date of loss","loss date","date of service","date of occurrence",
        "occurred on","event date",
    ]
    raw = _after_keyword(text, date_kws, DATE_RE)
    if raw:
        iso = _to_iso(raw)
        if iso:
            return iso

    # 4. First plausible date in doc (not far future, year >= 2000)
    today = datetime.today()
    for raw in extract_all_dates(text):
        if len(raw) < 6:
            continue
        dt = parse_date(raw)
        if dt and dt.year >= 2000 and dt <= today:
            return dt.strftime("%Y-%m-%d")

    return None


# ---------------------------------------------------------------------------
# Claim type
# ---------------------------------------------------------------------------

def extract_claim_type(text: str) -> Optional[str]:
    is_medical_form = bool(re.search(
        r"(?:medical\s+claim\s+form|health\s+insurance\s+claim|HCFA|cigna\s+health)",
        text, re.IGNORECASE
    ))

    categories = {
        "Auto":       ["auto accident","motor vehicle","automobile","collision"],
        "Property":   ["property damage","dwelling","flood damage","fire damage",
                       "burglary","theft","home damage"],
        "Life":       ["death benefit","beneficiary","deceased","death claim"],
        "Disability": ["disability","unable to work","long-term disability"],
        "Travel":     ["trip cancellation","lost luggage","travel insurance"],
        "Liability":  ["liability","third party","lawsuit","negligence"],
        "Health":     ["medical","health","hospital","surgery","diagnosis",
                       "treatment","physician","clinical","procedure"],
    }

    text_lower = text.lower()
    for ctype in ["Auto","Property","Life","Disability","Travel","Liability"]:
        for kw in categories[ctype]:
            if kw in text_lower:
                return ctype

    if is_medical_form or any(kw in text_lower for kw in categories["Health"]):
        return "Health"

    m = re.search(r"(?:type of claim|claim type)[:\s]+([A-Za-z /]+)", text, re.IGNORECASE)
    if m:
        return normalize_field_value(m.group(1)).capitalize()

    return None


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------

def extract_description(text: str) -> Optional[str]:
    # 1. Cigna "C. DESCRIPTION OF INCIDENT ..."
    m = re.search(
        r"(?:C\.\s*)?DESCRIPTION\s+OF\s+(?:INCIDENT|CLAIM|LOSS|ILLNESS|INJURY)"
        r"\s+(.{20,400}?)(?=\n[A-Z]|\Z)",
        text, re.IGNORECASE | re.DOTALL
    )
    if m:
        val = normalize_field_value(m.group(1).replace("\n", " "))
        if len(val) >= 20:
            return val[:400]

    # 2. HCFA "21. DIAGNOSIS / NATURE OF ILLNESS OR INJURY ..."
    #
    #    pdfplumber produces two layouts depending on diagnosis length:
    #
    #    Layout A (short, single-line):
    #      "DIAGNOSIS / NATURE OF ILLNESS OR M25.511 – Pain in right shoulder"
    #      "INJURY / PREGNANCY INJURY"
    #      → full description is on line 1 after the ICD code
    #
    #    Layout B (long, wraps to line 2):
    #      "DIAGNOSIS / NATURE OF ILLNESS OR F32.1 – Major depressive disorder,"
    #      "INJURY / PREGNANCY INJURY single episode, moderate"
    #      → part 1 on line 1, continuation after "INJURY / PREGNANCY INJURY" on line 2
    #
    m = re.search(
        r"DIAGNOSIS\s*/\s*NATURE\s+OF\s+ILLNESS\s+OR\s+(.+?)\n"
        r"INJURY\s*/\s*PREGNANCY\s+INJURY\s*(.*?)(?=\n|$)",
        text, re.IGNORECASE
    )
    if m:
        line1_rest = m.group(1).strip()   # e.g. "F32.1 – Major depressive disorder,"
        line2_rest = m.group(2).strip()   # e.g. "single episode, moderate" (or empty)

        # Strip the ICD code prefix from line1 (e.g. "F32.1 – " or "M25.511 – ")
        line1_desc = re.sub(r"^[A-Z]\d+\.?\d*\s*[\u2013\u2014\-]\s*", "", line1_rest).strip()

        # Combine: if line2 has content it's a continuation of line1
        combined = (line1_desc + " " + line2_rest).strip() if line2_rest else line1_desc
        combined = normalize_field_value(combined)
        if len(combined) >= 8:
            return combined[:400]


    # 3. NOTES field (fraud forms inject this)
    m = re.search(r"NOTES?:\s*(.{10,400}?)(?=\n[A-Z]|\Z)", text, re.IGNORECASE)
    if m:
        val = normalize_field_value(m.group(1))
        if len(val) >= 10:
            return val[:400]

    # 4. Generic keyword proximity
    for kw in ["description of incident","incident description","description of loss",
               "description of claim","nature of claim","what happened",
               "claim description","description:"]:
        m2 = re.search(re.escape(kw), text, re.IGNORECASE)
        if m2:
            snippet = text[m2.end(): m2.end() + 400]
            snippet = re.split(r"\n[A-Z0-9][A-Za-z0-9 .]+:", snippet)[0]
            val = normalize_field_value(snippet)
            if len(val) >= 20:
                return val[:400]

    # 5. Longest paragraph fallback
    paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 60]
    if paragraphs:
        return sorted(paragraphs, key=len, reverse=True)[0][:400]

    return None


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def extract_structured_data(text: str) -> dict:
    return {
        "claimant_name": extract_claimant_name(text),
        "policy_number": extract_policy_number(text),
        "claim_amount":  extract_claim_amount(text),
        "incident_date": extract_incident_date(text),
        "claim_type":    extract_claim_type(text),
        "description":   extract_description(text),
    }


def parse_pdf(pdf_path: str) -> tuple[Optional[str], dict]:
    raw_text = extract_text_from_pdf(pdf_path)
    if raw_text is None:
        logger.error(f"Skipping extraction — could not read {pdf_path}")
        return None, {}

    data = extract_structured_data(raw_text)
    found = {k: v for k, v in data.items() if v is not None}
    logger.info(f"Extracted {len(found)}/6 fields: {list(found.keys())}")
    return raw_text, data