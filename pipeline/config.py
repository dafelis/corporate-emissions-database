"""Pipeline configuration — edit these parameters to control search behaviour."""

# Year range to search for emissions and financial data
TARGET_START_YEAR = 2019
TARGET_END_YEAR = 2025

# Max Exa searches per document type (emissions / financials) per company
MAX_SEARCHES = 10

# Below this confidence score, re-extract with a stronger model
CONFIDENCE_THRESHOLD = 70

# Models
MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_STRONG = "claude-opus-4-6"
