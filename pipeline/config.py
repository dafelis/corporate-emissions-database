"""Pipeline configuration — edit these parameters to control search behaviour."""

# Year range to search for emissions and financial data
TARGET_START_YEAR = 2020
TARGET_END_YEAR = 2025

# Max Exa searches per document type (emissions / financials) per company
MAX_SEARCHES = 10

# Below this confidence score, re-extract with a stronger model
CONFIDENCE_THRESHOLD = 70

# Models
MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_STRONG = "claude-opus-4-6"

# Budget caps (USD) — pipeline pauses and asks when exceeded
BUDGET_PER_COMPANY = 5.0    # max spend per company before pausing
BUDGET_GLOBAL = 20.0        # max total spend before pausing

# Evidence page text-match score threshold for skipping Claude verification.
# Lower = fewer verification calls = cheaper, but weaker evidence that the
# cited page actually shows the numbers (affects screenshot/provenance quality).
# Scoring in _rank_evidence_pages: reporting year absent = page discarded;
# year present +1; each extracted scope value found on the page +2;
# each of "scope 1"/"scope 2"/"scope 3" present +1. Max realistic score 10.
VERIFICATION_SKIP_THRESHOLD = 5

# Max companies to process concurrently (1 = sequential)
CONCURRENT_COMPANIES = 4
