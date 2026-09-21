"""Diagnostic: what does Exa return for Anglo American older years?"""

import os
from dotenv import load_dotenv
from exa_py import Exa

load_dotenv()

exa = Exa(api_key=os.environ["EXA_API_KEY"])

company = "Anglo American"

for year in [2020, 2021, 2022, 2024]:
    query = (
        f"{company} greenhouse gas emissions scope 1 2 3 "
        f"{year} {year + 1} sustainability report ESG annual report"
    )
    print(f"\n{'='*70}")
    print(f"YEAR: {year}")
    print(f"QUERY: {query}")
    print(f"{'='*70}")

    response = exa.search(query, num_results=10, type="auto")

    for i, r in enumerate(response.results):
        is_pdf = r.url.lower().split("?")[0].endswith(".pdf")
        print(f"  {i+1}. {'[PDF]' if is_pdf else '[WEB]'} {r.title or '(no title)'}")
        print(f"     {r.url[:120]}")

    if not response.results:
        print("  (no results)")
