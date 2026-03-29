# ClaimIQ — Insurance Claim Processing Agent

An AI-powered pipeline that automatically extracts, validates, and makes decisions on insurance claim PDFs. Built with a FastAPI backend, React frontend, and a rule-based fraud detection engine.

---

## What it does

- Accepts PDF insurance claims (Cigna Medical Claim Form and HCFA-1500 formats)
- Extracts structured fields: claimant name, policy number, claim amount, incident date, claim type, and description
- Runs fraud detection and validation checks
- Returns an **ACCEPT**, **FLAG**, or **REJECT** decision with reasoning
- Displays everything in a dashboard UI with upload, filtering, and detail views

---

## Project structure

```
insurance-claim-agent/
│
├── src/
│   ├── agent.py              # Orchestrator — runs the full pipeline per claim
│   ├── parsers.py            # PDF text extraction + field parsing
│   ├── fraud_detector.py     # Validation rules + fraud heuristics
│   ├── database.py           # Atomic JSON read/write for results
│   └── utils.py              # Date parsing, regex helpers, logging
│
├── frontend/
│   ├── src/
│   │   ├── App.jsx           # Full dashboard UI
│   │   └── main.jsx          # React entry point
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
│
├── api.py                    # FastAPI server (4 endpoints)
├── main.py                   # Batch runner for local processing
├── generate_test_data.py     # Generates 200 synthetic test PDFs
├── requirements.txt
└── .gitignore
```

---

## Getting started

### Prerequisites

- Python 3.10+
- Node.js 18+

### 1. Clone the repo

```bash
git clone <your-repo-url>
cd insurance-claim-agent
```

### 2. Set up Python environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Install frontend dependencies

```bash
cd frontend
npm install
cd ..
```

---

## Running the app

You need two terminals running simultaneously.

**Terminal 1 — API backend:**
```bash
venv\Scripts\activate       # Windows
source venv/bin/activate    # Mac/Linux

uvicorn api:app --reload
```

API runs at `http://localhost:8000`
Interactive docs at `http://localhost:8000/docs`

**Terminal 2 — React frontend:**
```bash
cd frontend
npm run dev
```

Dashboard runs at `http://localhost:3000`

---

## API endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/claims` | Upload a PDF, returns decision |
| `GET` | `/claims` | List all claims (filter by `?status=FLAG`) |
| `GET` | `/claims/summary` | Count breakdown by status |
| `GET` | `/claims/{id}` | Full detail for one claim |
| `DELETE` | `/claims/{id}` | Remove a claim |

---

## Generating test data

To populate `sample_claims/` with 200 synthetic PDFs:

```bash
python generate_test_data.py
```

Distribution: 80 clean (ACCEPT), 50 fraudulent (FLAG), 40 incomplete (REJECT), 30 edge cases (FLAG).

To run the batch pipeline against them:

```bash
python main.py
```

Results are saved to `output/results.json`.

---

## Decision logic

| Status | Condition |
|--------|-----------|
| **ACCEPT** | All required fields present, no fraud flags, no inconsistencies |
| **FLAG** | Fraud indicators detected, high-value claim, suspicious patterns, or non-critical missing fields |
| **REJECT** | Missing claimant name or claim amount, or invalid claim amount |

### Fraud heuristics

- Claim amount exceeds $10,000
- Future incident date
- Round-number amounts above $5,000
- Suspicious phrases: "wire transfer", "cash only", "no receipts", "urgent"
- Same-day filing on high-value claims
- Incident older than 730 days
- Conflicting dollar amounts in document

---

## Supported form types

- **Cigna Medical Claim Form** (591692c Rev. 11/2023)
- **HCFA-1500** (standard CMS health insurance claim form)

The parser handles both layouts including pdfplumber's fragmented column extraction from table-based PDFs.

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| PDF extraction | pdfplumber |
| Field parsing | Regex + keyword-proximity heuristics |
| Backend | FastAPI + uvicorn |
| Frontend | React 18 + Vite |
| Storage | JSON flat file (`output/results.json`) |
| Test data | reportlab |
