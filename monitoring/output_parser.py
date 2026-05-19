"""
Output parser — extracts structured signals from raw agent text per domain schema.
Writes parsed signals to agent_output_signals in Snowflake.

Each domain parser returns a dict conforming to the agent_output_signals schema.
"""

import json
import re
from typing import Any

import anthropic


# ---------------------------------------------------------------------------
# Keyword / pattern heuristics (fast path, no LLM cost)
# ---------------------------------------------------------------------------

_ESCALATION_TERMS = re.compile(
    r"\b(escalat|uar|suspicious activity report|sar\b|file a report|refer to|"
    r"recommend filing|further review|flag for|warrants investigation)\b",
    re.IGNORECASE,
)
_NO_ACTION_TERMS = re.compile(
    r"\b(no further action|close|not suspicious|routine|explainable|"
    r"consistent with|no red flags|cleared)\b",
    re.IGNORECASE,
)
_REFUSAL_TERMS = re.compile(
    r"\b(cannot|can't|outside my scope|not able to|decline|I'm unable|"
    r"requires human|should not|must not|I should not)\b",
    re.IGNORECASE,
)
_CITATION_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|\[Source:.*?\]|\(Source:.*?\)|"
    r"Secretary of State|FinCEN|OFAC|court records|adverse media)",
    re.IGNORECASE,
)
_FIVE_WS = {
    "who": re.compile(r"\bwho\b", re.IGNORECASE),
    "what": re.compile(r"\bwhat\b", re.IGNORECASE),
    "when": re.compile(r"\bwhen\b", re.IGNORECASE),
    "where": re.compile(r"\bwhere\b", re.IGNORECASE),
    "why": re.compile(r"\bwhy\b|\bhow\b", re.IGNORECASE),
}


def _detect_escalation(text: str) -> tuple[bool | None, str]:
    """Returns (escalation_recommended, signal_label)."""
    if bool(_REFUSAL_TERMS.search(text)):
        return None, "REFUSED"
    if bool(_ESCALATION_TERMS.search(text)):
        return True, "ESCALATE"
    if bool(_NO_ACTION_TERMS.search(text)):
        return False, "NO_ACTION"
    return None, "NEEDS_REVIEW"


def _detect_citations(text: str) -> tuple[bool, int]:
    hits = _CITATION_PATTERN.findall(text)
    return bool(hits), len(hits)


def _detect_refusal(text: str) -> bool:
    return bool(_REFUSAL_TERMS.search(text))


def _confidence_level(text: str) -> str:
    high = re.compile(r"\b(clearly|definitively|certain|strong evidence|confirms)\b", re.IGNORECASE)
    low = re.compile(r"\b(may|might|possibly|uncertain|unclear|limited information|cannot confirm)\b", re.IGNORECASE)
    if high.search(text):
        return "HIGH"
    if low.search(text):
        return "LOW"
    return "MEDIUM"


# ---------------------------------------------------------------------------
# Domain parsers
# ---------------------------------------------------------------------------

def parse_transaction_monitoring(raw_output: str, agent_name: str) -> dict:
    escalation, signal = _detect_escalation(raw_output)
    citations_present, citation_count = _detect_citations(raw_output)

    typologies = []
    for term in ["structuring", "smurfing", "layering", "rapid movement", "round dollar", "cash intensive"]:
        if re.search(term, raw_output, re.IGNORECASE):
            typologies.append(term)

    deadline_flagged = bool(re.search(r"\b(30.day|60.day|deadline|extension|filing window)\b", raw_output, re.IGNORECASE))

    return {
        "escalation_recommended": escalation,
        "escalation_signal": signal,
        "citation_count": citation_count,
        "citations_present": citations_present,
        "required_fields_present": bool(typologies) or signal != "NEEDS_REVIEW",
        "out_of_scope_refused": signal == "REFUSED",
        "confidence_level": _confidence_level(raw_output),
        "domain_signals": {
            "typologies_identified": typologies,
            "deadline_flagged": deadline_flagged,
            "narrative_present": len(raw_output) > 200,
        },
    }


def parse_kyc_cdd_edd(raw_output: str, agent_name: str) -> dict:
    escalation, signal = _detect_escalation(raw_output)
    citations_present, citation_count = _detect_citations(raw_output)

    cip_elements = {
        "name": bool(re.search(r"\bname\b", raw_output, re.IGNORECASE)),
        "dob": bool(re.search(r"\b(date of birth|dob|born)\b", raw_output, re.IGNORECASE)),
        "address": bool(re.search(r"\baddress\b", raw_output, re.IGNORECASE)),
        "id_number": bool(re.search(r"\b(identification|ssn|ein|passport|id number)\b", raw_output, re.IGNORECASE)),
    }
    cip_complete = all(cip_elements.values())
    edd_triggered = bool(re.search(r"\b(edd|enhanced due diligence|pep|high.risk|adverse media)\b", raw_output, re.IGNORECASE))
    ownership_threshold = bool(re.search(r"\b(25%|beneficial owner|ownership threshold|control prong)\b", raw_output, re.IGNORECASE))

    return {
        "escalation_recommended": escalation,
        "escalation_signal": signal,
        "citation_count": citation_count,
        "citations_present": citations_present,
        "required_fields_present": cip_complete,
        "out_of_scope_refused": signal == "REFUSED",
        "confidence_level": _confidence_level(raw_output),
        "domain_signals": {
            "cip_elements": cip_elements,
            "cip_complete": cip_complete,
            "edd_triggered": edd_triggered,
            "beneficial_ownership_addressed": ownership_threshold,
        },
    }


def parse_sar_uar_filing(raw_output: str, agent_name: str) -> dict:
    escalation, signal = _detect_escalation(raw_output)
    citations_present, citation_count = _detect_citations(raw_output)

    five_ws = {k: bool(v.search(raw_output)) for k, v in _FIVE_WS.items()}
    narrative_complete = sum(five_ws.values()) >= 4

    deadline_mention = bool(re.search(r"\b(30.day|60.day|deadline|extension)\b", raw_output, re.IGNORECASE))
    dual_control = bool(re.search(r"\b(dual.control|two.person|supervisor|officer approval|separate approval)\b", raw_output, re.IGNORECASE))
    no_legal_conclusion = not bool(re.search(r"\b(guilty|illegal|criminal|violated the law|committed fraud)\b", raw_output, re.IGNORECASE))

    return {
        "escalation_recommended": escalation,
        "escalation_signal": signal,
        "citation_count": citation_count,
        "citations_present": citations_present,
        "required_fields_present": narrative_complete,
        "out_of_scope_refused": signal == "REFUSED",
        "confidence_level": _confidence_level(raw_output),
        "domain_signals": {
            "five_ws_present": five_ws,
            "narrative_complete": narrative_complete,
            "deadline_addressed": deadline_mention,
            "dual_control_referenced": dual_control,
            "no_legal_conclusion": no_legal_conclusion,
        },
    }


def parse_osint_investigation(raw_output: str, agent_name: str) -> dict:
    escalation, signal = _detect_escalation(raw_output)
    citations_present, citation_count = _detect_citations(raw_output)

    sources_checked = []
    for source in ["secretary of state", "ofac", "adverse media", "court", "linkedin", "web", "social"]:
        if re.search(source, raw_output, re.IGNORECASE):
            sources_checked.append(source)

    legal_conclusion = bool(re.search(r"\b(guilty|determined to be|is a criminal|confirmed fraud)\b", raw_output, re.IGNORECASE))

    return {
        "escalation_recommended": escalation,
        "escalation_signal": signal,
        "citation_count": citation_count,
        "citations_present": citations_present,
        "required_fields_present": citation_count >= 1 and len(sources_checked) >= 2,
        "out_of_scope_refused": signal == "REFUSED" or (legal_conclusion and signal == "REFUSED"),
        "confidence_level": _confidence_level(raw_output),
        "domain_signals": {
            "sources_checked": sources_checked,
            "source_count": len(sources_checked),
            "citations_present": citations_present,
            "no_legal_conclusion": not legal_conclusion,
        },
    }


def parse_sanctions_screening(raw_output: str, agent_name: str) -> dict:
    escalation, signal = _detect_escalation(raw_output)
    citations_present, citation_count = _detect_citations(raw_output)

    ofac_referenced = bool(re.search(r"\bofac\b|sdn list|specially designated", raw_output, re.IGNORECASE))
    auto_clear = bool(re.search(r"\b(clear(ed)?|remove from|no match|not a match)\b", raw_output, re.IGNORECASE))
    human_review_required = bool(re.search(r"\b(human review|manual review|analyst|compliance officer)\b", raw_output, re.IGNORECASE))

    return {
        "escalation_recommended": escalation,
        "escalation_signal": signal,
        "citation_count": citation_count,
        "citations_present": citations_present,
        "required_fields_present": ofac_referenced,
        "out_of_scope_refused": (auto_clear and signal == "REFUSED") or signal == "REFUSED",
        "confidence_level": _confidence_level(raw_output),
        "domain_signals": {
            "ofac_referenced": ofac_referenced,
            "human_review_required": human_review_required,
            "autonomous_clear_attempted": auto_clear and not human_review_required,
        },
    }


_DOMAIN_PARSERS = {
    "TRANSACTION_MONITORING": parse_transaction_monitoring,
    "KYC_CDD_EDD": parse_kyc_cdd_edd,
    "SAR_UAR_FILING": parse_sar_uar_filing,
    "OSINT_INVESTIGATION": parse_osint_investigation,
    "SANCTIONS_SCREENING": parse_sanctions_screening,
}


def get_parser(domain: str):
    """Return the domain parser or a generic fallback."""
    return _DOMAIN_PARSERS.get(domain, _generic_parser)


def _generic_parser(raw_output: str, agent_name: str) -> dict:
    escalation, signal = _detect_escalation(raw_output)
    citations_present, citation_count = _detect_citations(raw_output)
    return {
        "escalation_recommended": escalation,
        "escalation_signal": signal,
        "citation_count": citation_count,
        "citations_present": citations_present,
        "required_fields_present": len(raw_output) > 100,
        "out_of_scope_refused": signal == "REFUSED",
        "confidence_level": _confidence_level(raw_output),
        "domain_signals": {},
    }


def write_output_signals(conn, invocation_id: str, agent_name: str, domain: str, signals: dict):
    """Persist parsed signals to agent_output_signals."""
    domain_signals_json = json.dumps(signals.get("domain_signals", {}))
    sql = """
        INSERT INTO compliance.ai_agent_monitoring.agent_output_signals
        (invocation_id, agent_name, domain, escalation_recommended, escalation_signal,
         citation_count, citations_present, required_fields_present,
         out_of_scope_refused, confidence_level, domain_signals)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, PARSE_JSON(%s))
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            invocation_id,
            agent_name,
            domain,
            signals.get("escalation_recommended"),
            signals.get("escalation_signal"),
            signals.get("citation_count"),
            signals.get("citations_present"),
            signals.get("required_fields_present"),
            signals.get("out_of_scope_refused"),
            signals.get("confidence_level"),
            domain_signals_json,
        ))
