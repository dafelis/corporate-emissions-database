"""Look up LEI (Legal Entity Identifier) via the GLEIF public API.

Resolution order:
1. ISIN → LEI. The ISIN comes from yfinance via the stored ticker and
   GLEIF's `filter[isin]` maps it to one legal entity — but yfinance's ISIN
   is unreliable (it has returned a Puerto Rico fund for InterContinental
   Hotels and a Polish company for LSEG), so the result is accepted only
   when the resolved legal name is consistent with the company name.
2. Name search — GLEIF full-text plus fuzzy completions — with guards a
   bare word-overlap score lacks: funds, pension schemes, share plans and
   trusts are dropped; the exact "<name> PLC" parent is preferred over
   holdings/finance subsidiaries; anything below medium similarity is
   rejected rather than returned as "low confidence".
"""

import re

import httpx


GLEIF_SEARCH = "https://api.gleif.org/api/v1/lei-records"
GLEIF_FUZZY = "https://api.gleif.org/api/v1/fuzzycompletions"

# Countries where FTSE 100 companies are typically registered. Only a
# flag now, not a veto: a global universe includes e.g. IAG (Spain).
ALLOWED_COUNTRIES = {"GB", "IE", "JE", "GG", "IM", "CH", "ZA", "AU", "NL", "LU"}

# Legal names containing these are vehicles attached to a company, not the
# company: rejected unless the search name itself contains the word.
_VEHICLE_TOKENS = re.compile(
    r"\b(plan|trust|trustee|trustees|scheme|pension|fund|nominee|nominees|"
    r"employee|benefit|foundation|charit\w*|section|esop|sip|oeic|icvc)\b",
    re.I,
)

# Typical subsidiary markers; penalised (not rejected) unless the search
# name itself contains them. "Limited" is deliberately absent: it appears
# inside "PUBLIC LIMITED COMPANY", and listed parents in other markets are
# "Limited" too — the listed-form bonus already ranks PLC above LIMITED.
_SUBSIDIARY_TOKENS = re.compile(
    r"\b(holdings?|finance|financing|funding|investments?|international|"
    r"treasury|services|capital|b\.?v\.?|dac|s\.?[àa]\s?r\.?l\.?|gmbh|llc)\b",
    re.I,
)

_STOP_WORDS = {
    "plc", "ltd", "limited", "group", "holdings", "inc", "corp",
    "corporation", "sa", "se", "nv", "ag", "the", "of", "and", "&",
    "public", "company",
}


def _clean_name(name: str) -> str:
    """Normalise a display name into a search name.

    "ABF (Associated British Foods)" -> "Associated British Foods"
    "Sainsbury's (J)"                -> "J Sainsbury"
    "Smith & Nephew"                 -> unchanged
    """
    m = re.search(r"\(([^)]+)\)", name)
    outer = re.sub(r"\s*\([^)]*\)", "", name).strip()
    if m:
        inner = m.group(1).strip()
        if len(inner) > len(outer):
            return inner
        if len(inner) <= 2 and outer:
            return f"{inner} {outer.replace(chr(39) + 's', '')}".strip()
    return outer or name.strip()


def _words(name: str) -> set[str]:
    return {
        w.lower() for w in re.findall(r"[a-zA-Z0-9]+", name)
        if w.lower() not in _STOP_WORDS and len(w) > 1
    }


def _squash(name: str, strict: bool = False) -> str:
    """Lower-case alphanumerics only, so 'Auto Trader' == 'AutoTrader'.

    Legal-form suffixes are always dropped. With strict=False, 'group' and
    'holdings' are dropped too (for similarity); with strict=True they are
    kept, so that "RELX PLC" is an exact match for "RELX" but "RELX GROUP
    PLC" is not.
    """
    # Keep non-Latin letters: stripping them collapsed a Greek subsidiary
    # "COCA - COLA HBC ΥΠΗΡΕΣΙΕΣ ..." to "cocacolahbc", an exact match.
    s = re.sub(r"[\W_]", "", name.lower())
    suffixes = ["publiclimitedcompany", "plc", "limited", "ltd"]
    if not strict:
        suffixes += ["group", "holdings"]
    for suffix in suffixes:
        if s.endswith(suffix) and len(s) > len(suffix) + 1:  # "bpplc" -> "bp"
            s = s[: -len(suffix)]
    return s


def _one_name_similarity(search_name: str, legal_name: str) -> float:
    """0–1: word overlap, or high if one squashed name contains the other.

    Very short names ("BP", "DCC") match only exactly — otherwise "BP"
    is contained in every BP subsidiary.
    """
    a, b = _words(search_name), _words(legal_name)
    overlap = len(a & b) / max(len(a), len(b)) if a and b else 0.0
    sa, sb = _squash(search_name), _squash(legal_name)
    if sa and sb and (sa == sb):
        return 1.0
    if len(sa) <= 3:
        return 0.0
    if sa and sb and len(sa) >= 4 and (sa in sb or sb in sa):
        return max(overlap, 0.8)
    return overlap


def _name_similarity(search_name: str, legal_name: str, other_names: list[str] | None = None) -> float:
    """Best similarity across the legal name and GLEIF's other/previous names.

    Renamed companies (Intermediate Capital Group -> ICG plc, Spirax-Sarco
    -> Spirax Group) are found through their previous legal name.
    """
    names = [legal_name] + [n for n in (other_names or []) if n]
    return max(_one_name_similarity(search_name, n) for n in names)


def _record_to_candidate(record: dict) -> dict:
    entity = record.get("attributes", {}).get("entity", {})
    country = (
        entity.get("legalAddress", {}).get("country", "")
        or entity.get("headquartersAddress", {}).get("country", "")
    )
    return {
        "lei": record.get("id") or record.get("attributes", {}).get("lei"),
        "legal_name": entity.get("legalName", {}).get("name", ""),
        "other_names": [o.get("name") for o in entity.get("otherNames", []) or [] if o.get("name")],
        "country": country,
        "category": entity.get("category"),
        "legal_form": (entity.get("legalForm") or {}).get("id"),
        "status": entity.get("status"),
    }


def _search_gleif(query: str, size: int = 20) -> list[dict]:
    """GLEIF full-text search."""
    resp = httpx.get(
        GLEIF_SEARCH,
        params={
            "filter[fulltext]": query,
            "filter[entity.status]": "ACTIVE",
            "page[size]": size,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return [_record_to_candidate(r) for r in resp.json().get("data", [])]


def _search_gleif_legal_name(query: str, size: int = 10) -> list[dict]:
    """GLEIF legal-name filter: matches on the legal name field only.

    Full-text search tokenises "P.L.C." differently from "plc" and buries
    "BP P.L.C." under hundreds of BP subsidiaries; this filter finds it.
    """
    resp = httpx.get(
        GLEIF_SEARCH,
        params={
            "filter[entity.legalName]": query,
            "filter[entity.status]": "ACTIVE",
            "page[size]": size,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return [_record_to_candidate(r) for r in resp.json().get("data", [])]


def _fuzzy_gleif(query: str) -> list[dict]:
    """GLEIF fuzzy legal-name completions, resolved to full records."""
    resp = httpx.get(
        GLEIF_FUZZY,
        params={"field": "entity.legalName", "q": query},
        timeout=30,
    )
    resp.raise_for_status()
    leis = []
    for r in resp.json().get("data", []):
        rel = (r.get("relationships") or {}).get("lei-records") or {}
        lei = ((rel.get("data") or {}).get("id"))
        if lei and lei not in leis:
            leis.append(lei)
    out = []
    for lei in leis[:8]:
        try:
            rec = httpx.get(f"{GLEIF_SEARCH}/{lei}", timeout=30)
            rec.raise_for_status()
            out.append(_record_to_candidate(rec.json().get("data", {})))
        except Exception:
            continue
    return out


def get_isin(ticker: str) -> str | None:
    """ISIN for a ticker via yfinance, or None. Treat as a hint, not a fact."""
    if not ticker:
        return None
    try:
        import yfinance as yf
        isin = yf.Ticker(ticker).isin
    except Exception:
        return None
    if not isin or isin == "-" or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
        return None
    return isin


def lookup_lei_by_isin(isin: str) -> dict | None:
    """Resolve an ISIN to its issuing legal entity via GLEIF's ISIN mapping."""
    resp = httpx.get(
        GLEIF_SEARCH,
        params={"filter[isin]": isin, "page[size]": 3},
        timeout=30,
    )
    resp.raise_for_status()
    records = resp.json().get("data", [])
    if not records:
        return None
    cand = _record_to_candidate(records[0])
    return cand if cand["lei"] else None


def _score(cand: dict, search_name: str) -> float:
    """Similarity plus structural preferences for the listed parent."""
    score = _name_similarity(search_name, cand["legal_name"], cand.get("other_names"))
    legal = cand["legal_name"].upper()
    search_has_sub = bool(_SUBSIDIARY_TOKENS.search(search_name))
    if re.search(r"\b(P\.?L\.?C\.?|PUBLIC LIMITED COMPANY|SE|N\.?V\.?|S\.?A\.?|AG|SPA|S\.?P\.?A\.?)\s*$", legal):
        score += 0.15  # listed-company legal forms
    target = _squash(search_name, strict=True)
    if any(_squash(n, strict=True) == target
           for n in [cand["legal_name"]] + (cand.get("other_names") or [])):
        score += 0.3  # exactly "<name> PLC" (now or formerly)
    if _SUBSIDIARY_TOKENS.search(legal) and not search_has_sub:
        score -= 0.25
    return score


def _best_match(candidates: list[dict], search_name: str) -> dict | None:
    """Pick the listed parent from GLEIF candidates, or None."""
    search_has_vehicle_word = bool(_VEHICLE_TOKENS.search(search_name))
    scored = []
    for c in candidates:
        if (c.get("status") or "ACTIVE") != "ACTIVE":
            continue  # e.g. an inactive Belgian "Prudential"
        if (c.get("category") or "GENERAL") != "GENERAL":
            continue
        if _VEHICLE_TOKENS.search(c["legal_name"]) and not search_has_vehicle_word:
            continue
        similarity = _name_similarity(search_name, c["legal_name"], c.get("other_names"))
        if similarity < 0.3:
            continue
        scored.append((_score(c, search_name), similarity, c))

    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    _, similarity, best = scored[0]

    country_ok = best["country"] in ALLOWED_COUNTRIES
    confidence = "high" if similarity >= 0.5 else "medium"
    reasons = []
    if similarity < 0.5:
        reasons.append(
            f"Moderate name similarity ({similarity:.0%}): "
            f"searched '{search_name}', found '{best['legal_name']}'"
        )
    if not country_ok:
        reasons.append(f"Registered in '{best['country']}' — outside the usual FTSE 100 countries")
    return {
        **best,
        "similarity": similarity,
        "country_ok": country_ok,
        "confidence": confidence,
        "flag_reason": "; ".join(reasons) if reasons else None,
        "method": "name",
    }


def lookup_lei(company_name: str, ticker: str | None = None) -> dict | None:
    """Look up a company's LEI: by ISIN when consistent with the name, else by name.

    Returns dict with keys: lei, legal_name, country, confidence, flag_reason,
    method ('isin:<ISIN>' or 'name'). None if no plausible match.
    """
    search_name = _clean_name(company_name)

    isin = get_isin(ticker) if ticker else None
    if isin:
        try:
            cand = lookup_lei_by_isin(isin)
        except Exception:
            cand = None
        if (cand and (cand.get("category") or "GENERAL") == "GENERAL"
                and (cand.get("status") or "ACTIVE") == "ACTIVE"):
            similarity = _name_similarity(search_name, cand["legal_name"], cand.get("other_names"))
            if similarity >= 0.3:
                country_ok = cand["country"] in ALLOWED_COUNTRIES
                return {
                    **cand,
                    "similarity": similarity,
                    "country_ok": country_ok,
                    "confidence": "high",
                    "flag_reason": (None if country_ok else
                                    f"Registered in '{cand['country']}' — outside the usual FTSE 100 countries"),
                    "method": f"isin:{isin}",
                }
        # yfinance ISIN inconsistent with the name — ignore it.

    candidates: list[dict] = []
    for query in dict.fromkeys([search_name, f"{search_name} plc"]):
        try:
            candidates.extend(_search_gleif(query))
        except Exception:
            pass
        try:
            candidates.extend(_fuzzy_gleif(query))
        except Exception:
            pass
    # The listed parent's exact legal name, in its three spellings.
    for query in dict.fromkeys([f"{search_name} plc", f"{search_name} p.l.c.",
                                f"{search_name} public limited company"]):
        try:
            candidates.extend(_search_gleif_legal_name(query))
        except Exception:
            pass

    seen, unique = set(), []
    for c in candidates:
        if c["lei"] and c["lei"] not in seen:
            seen.add(c["lei"])
            unique.append(c)

    return _best_match(unique, search_name)
