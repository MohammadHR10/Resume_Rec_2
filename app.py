from typing import List, Dict, Any, Optional, Tuple, Type, Union
from pydantic import BaseModel, Field, create_model, ValidationError
import streamlit as st
from mistral_client import call_mistral, call_second_scorer, call_arbiter
from pdf_extract import extract_text_from_pdf
import json, re, zipfile, io, datetime, base64
from pathlib import Path
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side


# ---------- Post-processing: deterministic business rules ----------
def apply_business_rules(data: dict) -> dict:
    """Apply deterministic business rules AFTER LLM returns scores.

    The LLM decides the recommendation itself (it understands whatever scoring
    format is in use). This function only:
    1. Validates all scores use the same format
    2. Overrides recommendation to "Pass" if minimum requirements fail
    """
    sf = data.get("scoring_format") or {}
    scale = sf.get("scale", [])
    best = sf.get("best")
    worst = sf.get("worst")

    score_fields = [k for k in data if k.endswith("_score") and k != "scoring_format"]
    score_values = {k: data[k] for k in score_fields if data.get(k) is not None}

    # --- Step 1: Format validation ---
    if scale:
        scale_lower = [str(v).lower() for v in scale]
        for field, val in score_values.items():
            val_lower = str(val).lower()
            if val_lower not in scale_lower:
                try:
                    num_val = float(val)
                    num_scale = [float(s) for s in scale]
                    if min(num_scale) <= num_val <= max(num_scale):
                        continue
                except (ValueError, TypeError):
                    pass
                for s in scale:
                    if str(s).lower() == val_lower:
                        data[field] = s
                        break

    # --- Step 2: Requirements override ---
    # If any field named *requirement* or *qualification* has the worst score, force Pass
    for field, val in score_values.items():
        field_lower = field.lower()
        is_requirement = ("requirement" in field_lower or "qualification" in field_lower) and "preferred" not in field_lower
        if is_requirement and worst is not None:
            if str(val).lower() == str(worst).lower():
                data["recommendation"] = "Pass"
                data["overall_score"] = worst
                break
            try:
                num_val = float(val)
                num_worst = float(worst)
                num_best = float(best)
                midpoint = (num_best + num_worst) / 2
                if num_val < midpoint:
                    data["recommendation"] = "Pass"
                    data["overall_score"] = worst if isinstance(worst, (int, float)) else val
                    break
            except (ValueError, TypeError):
                pass

    return data


# ---------- Batch calibration (cross-candidate consistency) ----------

def _parse_arbiter_response(raw: str, expected_len: int) -> Optional[list]:
    """Parse the arbiter's JSON array response."""
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        start = cleaned.find('[')
        end = cleaned.rfind(']') + 1
        if start != -1 and end > start:
            parsed = json.loads(cleaned[start:end])
            if isinstance(parsed, list) and len(parsed) == expected_len:
                return parsed
    except (json.JSONDecodeError, Exception):
        pass
    return None


def arbiter_final_judgment(batch: list, scoring_format: dict,
                           job_title: str = "", job_description: str = "",
                           custom_fields: list = None) -> list:
    """GPT 120b acts as the final arbiter. For each candidate it receives:
    - The extracted evidence (qualifications)
    - Model A's scores, explanations, and concerns
    - Model B's scores, explanations, and concerns
    It reviews both judgments against the job description and produces
    the definitive final scores and recommendation for every candidate.

    Returns list of final score dicts (same order as input).
    """
    if not batch:
        return []
    if len(batch) == 1:
        return [batch[0].get('scores_a', batch[0].get('scores', {}))]

    scale_info = json.dumps(scoring_format, ensure_ascii=False)

    custom_fields_desc = ""
    if custom_fields:
        cf_lines = []
        for f in custom_fields:
            fname = f['name'].strip().replace(' ', '_').lower()
            cf_lines.append(f"- {fname}: {f.get('instruction', 'N/A')}")
        custom_fields_desc = "\n".join(cf_lines)

    candidate_summaries = []
    for i, entry in enumerate(batch):
        evidence_str = entry.get('evidence', '')

        scores_a = json.dumps(entry.get('scores_a', {}), indent=2, ensure_ascii=False)
        expl_a = json.dumps(entry.get('explanations_a', {}), indent=2, ensure_ascii=False)
        concerns_a = ", ".join(entry.get('concerns_a', [])) or "None"

        scores_b = json.dumps(entry.get('scores_b', {}), indent=2, ensure_ascii=False)
        expl_b = json.dumps(entry.get('explanations_b', {}), indent=2, ensure_ascii=False)
        concerns_b = ", ".join(entry.get('concerns_b', [])) or "None"

        candidate_summaries.append(
            f"--- CANDIDATE {i+1} ---\n"
            f"QUALIFICATIONS:\n{evidence_str}\n\n"
            f"MODEL A SCORES:\n{scores_a}\n"
            f"MODEL A REASONING:\n{expl_a}\n"
            f"MODEL A CONCERNS: {concerns_a}\n\n"
            f"MODEL B SCORES:\n{scores_b}\n"
            f"MODEL B REASONING:\n{expl_b}\n"
            f"MODEL B CONCERNS: {concerns_b}"
        )

    all_candidates = "\n\n".join(candidate_summaries)

    jd_section = ""
    if job_title or job_description:
        jd_section = f"""
JOB BEING EVALUATED:
Title: {job_title}
Description: {job_description[:2000] if job_description else 'N/A'}
"""

    cf_section = ""
    if custom_fields_desc:
        cf_section = f"""
CUSTOM EVALUATION FIELDS AND THEIR DEFINITIONS:
{custom_fields_desc}
"""

    prompt = f"""You are the FINAL ARBITER in a fair-hiring evaluation panel. Two independent scoring models (A and B) have each evaluated {len(batch)} candidates. You now see both models' scores, reasoning, and concerns for every candidate, along with the job requirements and each candidate's qualifications.

Your job: produce the DEFINITIVE final scores for each candidate.
{jd_section}{cf_section}
SCORING FORMAT IN USE: {scale_info}

HOW TO DECIDE:
1. For each candidate, compare Model A and Model B's scores and reasoning. Where they AGREE, that score is likely correct — keep it. Where they DISAGREE, read both explanations and the candidate's qualifications to determine which model's judgment is more accurate.
2. Check each score against the actual evidence. If a model gave a high score but the qualifications don't support it (or concerns contradict it), adjust downward. If a model underscored despite strong evidence, adjust upward.
3. Ensure CROSS-CANDIDATE CONSISTENCY: if two candidates have equivalent qualifications, they MUST receive the same scores. Do not let names, gender, ethnicity, religion, or any non-professional attribute influence your judgment.
4. The recommendation must be one of: "Recommended" (strong fit), "Consider" (partial fit), "Pass" (not a fit). It must logically follow from the scores and evidence.
5. Custom field scores must align with their definitions above.
6. Use the exact scoring format specified above for all score fields.

{all_candidates}

RESPONSE FORMAT:
Return STRICT JSON only — no prose, no markdown fences. Return an array with exactly {len(batch)} objects, one per candidate in order.
Each object must have exactly the same score field keys as shown in MODEL A SCORES above, with your final values.

Return ONLY the JSON array."""

    result = call_arbiter(prompt)

    if not (isinstance(result, dict) and "choices" in result):
        return [entry.get('scores_a', {}) for entry in batch]

    raw = result["choices"][0]["message"]["content"]
    parsed = _parse_arbiter_response(raw, len(batch))
    if parsed:
        return parsed

    return [entry.get('scores_a', {}) for entry in batch]


# ---------- Evidence extraction (bias-free fact extraction) ----------

def build_extraction_prompt(resume_text: str) -> str:
    """Build prompt that instructs the LLM to output the resume unchanged
    except with bias markers deleted."""
    return f"""Return the resume below EXACTLY as written, but DELETE any content that reveals the candidate's identity, demographics, or background rather than their professional capability.

For each sentence, phrase, or line in the resume, ask yourself:
"Does this demonstrate a job-relevant skill, experience, or qualification? Or does this reveal something about who the candidate is as a person — their identity, demographics, or background?"

If it's the second, DELETE it. Be thorough. When in doubt, delete.

Categories of identity/background content to delete (non-exhaustive — use judgment):
- Names, nicknames, initials of the candidate
- Pronouns and gendered language (he/she/his/her/him) and honorifics (Mr., Mrs., Ms., etc.)
- Contact info (email, phone, address, LinkedIn, GitHub, personal URLs)
- Nationality, citizenship, visa status, country/region of origin
- Age, date of birth, marital/family status
- Race, ethnicity, religion, gender, sexual orientation
- Memberships or affiliations in ANY organization that is not clearly a professional/technical body directly relevant to the job. This includes — but is not limited to — societies, associations, congresses, brotherhoods, fellowships, clubs, communities, or groups tied to religion, ethnicity, nationality, region, culture, or identity. Examples include things like "Society of North America", "American Congress", "Brotherhood of ...", "North American Association", "Jain Society", etc. If the organization's purpose is not obviously technical (IEEE, ACM, AMA, etc.) or the role's professional field, treat it as identity-revealing and delete it.
- Metadata codes, tags, or labels that are not actual resume content
- Photograph references, physical descriptions, disability references

Leave all professional content (education, skills, work experience, dates, projects, achievements, certifications, technical publications, job-relevant leadership) EXACTLY as written — same wording, same formatting.

Output only the cleaned resume text — no JSON, no commentary, no markdown, no preamble.

RESUME:
{resume_text}"""


def _parse_llm_json(result: dict) -> Optional[str]:
    """Extract and parse JSON from an LLM response, return formatted JSON string or None."""
    if not (isinstance(result, dict) and "choices" in result):
        return None
    raw = result["choices"][0]["message"]["content"]
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        start = cleaned.find('{')
        end = cleaned.rfind('}') + 1
        if start != -1 and end > start:
            facts = json.loads(cleaned[start:end])
            return json.dumps(facts, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, Exception):
        pass
    return raw


def extract_candidate_name(resume_text: str) -> str:
    """Use LLM to extract the candidate's full name from raw resume text."""
    prompt = (
        "What is the full legal name of the candidate in this resume? "
        "Return ONLY the name — no quotes, no explanation, no extra text.\n\n"
        + resume_text[:1000]
    )
    result = call_mistral(prompt)
    if isinstance(result, dict) and "choices" in result:
        name = result["choices"][0]["message"]["content"].strip()
        name = name.strip('"').strip("'").strip('.').strip()
        if 2 <= len(name) <= 80 and '\n' not in name:
            return name
    return ""


def extract_evidence(resume_text: str) -> str:
    """Use LLM to produce a bias-free rewrite of the resume, preserving all
    professional content verbatim. The scoring LLMs see only this cleaned version."""
    prompt = build_extraction_prompt(resume_text)
    result = call_mistral(prompt)
    if isinstance(result, dict) and "choices" in result:
        cleaned = result["choices"][0]["message"]["content"].strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        return cleaned if cleaned else resume_text
    return resume_text


# ---------- LLM-based field extraction for anonymization ----------
def extract_fields_with_llm(text: str, fields: List[str]) -> Dict[str, List[str]]:
    """Use LLM to dynamically extract bias-related content from resume text.

    Pass 1 of the two-pass approach: the LLM identifies all content matching
    the user-selected categories and returns exact strings for redaction.
    Returns dict mapping category -> list of exact strings found.
    """
    if not text or not fields:
        return {}

    fields_str = ", ".join(fields)
    prompt = f"""You are a resume redaction assistant. Find and extract all text in a resume that matches the requested categories so it can be removed.

Categories to find: {fields_str}

Resume:
{text[:4000]}

Instructions:
- Return a JSON object where each key is a category and the value is an array of exact strings from the resume
- Copy the EXACT text as it appears so it can be found and replaced
- Be thorough: scan the entire resume from top to bottom
- Think broadly about each category — include any text, phrase, organization name, membership, affiliation, pronoun, title, or reference that reveals information about that category
- For contact info categories (name, email, phone, address, linkedin, github): extract the full value as written
- For any other category: find every phrase, sentence fragment, organization name, or keyword in the resume that relates to it
- If nothing is found for a category, use an empty array []
- Return ONLY valid JSON, no markdown fences or explanation

Example format:
{{"category1": ["exact text 1", "exact text 2"], "category2": ["exact text 3"]}}"""

    try:
        result = call_mistral(prompt)

        if isinstance(result, dict) and "choices" in result:
            content = result["choices"][0]["message"]["content"]
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1 and end > start:
                raw = json.loads(content[start:end + 1])
                normalized: Dict[str, List[str]] = {}
                for key, val in raw.items():
                    if val is None:
                        continue
                    if isinstance(val, str):
                        if val:
                            normalized[key.lower()] = [val]
                    elif isinstance(val, list):
                        flat = [str(v) for v in val if v]
                        if flat:
                            normalized[key.lower()] = flat
                    else:
                        sv = str(val)
                        if sv:
                            normalized[key.lower()] = [sv]
                return normalized
    except Exception:
        pass
    return {}
 
# ---------- Case-insensitive string replacement (no regex) ----------
def case_insensitive_replace(text: str, old: str, new: str) -> str:
    """Replace all occurrences of 'old' with 'new', case-insensitively.
    
    Uses simple string matching instead of regex for predictable behavior.
    """
    if not old or len(old) < 2:
        return text
    
    result = []
    text_lower = text.lower()
    old_lower = old.lower()
    i = 0
    
    while i < len(text):
        if text_lower[i:i+len(old_lower)] == old_lower:
            result.append(new)
            i += len(old)
        else:
            result.append(text[i])
            i += 1
    
    return ''.join(result)


def case_insensitive_replace_word(text: str, word: str, replacement: str) -> str:
    """Replace word with replacement, case-insensitively, only at word boundaries.
    
    A word boundary is defined as: start/end of string, or non-alphanumeric character.
    """
    if not word or len(word) < 2:
        return text
    
    result = []
    text_lower = text.lower()
    word_lower = word.lower()
    i = 0
    
    while i < len(text):
        # Check if we have a match at this position
        if text_lower[i:i+len(word_lower)] == word_lower:
            # Check word boundary before
            before_ok = (i == 0) or (not text[i-1].isalnum())
            # Check word boundary after
            end_pos = i + len(word)
            after_ok = (end_pos >= len(text)) or (not text[end_pos].isalnum())
            
            if before_ok and after_ok:
                result.append(replacement)
                i += len(word)
                continue
        
        result.append(text[i])
        i += 1
    
    return ''.join(result)


def strip_bias_codes(text: str) -> str:
    """Remove synthetic bias-group test codes like '– BG3/G3', '– BE1_G1', '(BE1_R1)'.
    Uses pure string scanning, no regex.
    """
    dash_chars = ('\u2013', '\u2014', '-')
    prefixes = ('BG', 'BE')
    lines = text.split('\n')
    cleaned = []

    for line in lines:
        new_line = line
        for dash in dash_chars:
            idx = new_line.find(dash)
            while idx != -1:
                raw_after = new_line[idx + len(dash):]
                stripped_spaces = len(raw_after) - len(raw_after.lstrip())
                after = raw_after.lstrip()
                matched = False
                for pfx in prefixes:
                    if after.startswith(pfx) and len(after) > len(pfx) and after[len(pfx)].isdigit():
                        end = len(pfx)
                        while end < len(after) and (after[end].isalnum() or after[end] in ('/', '_')):
                            end += 1
                        start = idx
                        while start > 0 and new_line[start - 1] == ' ':
                            start -= 1
                        cut_end = idx + len(dash) + stripped_spaces + end
                        new_line = new_line[:start] + new_line[cut_end:]
                        new_line = new_line.rstrip()
                        matched = True
                        break
                if not matched:
                    idx = new_line.find(dash, idx + 1)
                else:
                    idx = new_line.find(dash, 0)
        # Also handle parenthesized codes like "(BE1_G1)"
        pidx = new_line.find('(')
        while pidx != -1:
            inner = new_line[pidx + 1:]
            close = inner.find(')')
            if close != -1:
                code = inner[:close].strip()
                is_bias = False
                for pfx in prefixes:
                    if code.startswith(pfx) and len(code) > len(pfx) and code[len(pfx)].isdigit():
                        is_bias = True
                        break
                if is_bias:
                    new_line = new_line[:pidx].rstrip() + new_line[pidx + close + 2:]
                    pidx = new_line.find('(', pidx)
                else:
                    pidx = new_line.find('(', pidx + 1)
            else:
                break
        cleaned.append(new_line)

    return '\n'.join(cleaned)


# ---------- Anonymization helper ----------
def anonymize_text(text: str, fields: Optional[List[str]]) -> Tuple[str, Dict[str, List[str]]]:
    """Anonymize resume text using LLM-based extraction and redaction.

    Pass 1 – LLM identifies all bias-related strings matching user categories.
    Redact  – replace every found string with ***.
    Returns (anonymized_text, extracted_dict).
    """
    if not text or not fields:
        return text, {}

    s = text
    cats = [f.strip().lower() for f in fields if isinstance(f, str) and f.strip()]

    if not cats:
        return text, {}

    # When "name" is active, auto-include PII fields that embed the name
    auto_pii = {"email", "linkedin", "github", "phone"}
    if "name" in cats:
        for pii in auto_pii:
            if pii not in cats:
                cats.append(pii)

    # Strip synthetic bias-group test codes first
    s = strip_bias_codes(s)

    # LLM extraction (Pass 1)
    extracted = extract_fields_with_llm(s, cats)

    if not extracted:
        return s, {}

    REDACT_TAG = "***"

    # PHASE 1: Redact PII fields (email, linkedin, github, phone, address) FIRST
    pii_fields = {"email", "linkedin", "github", "phone", "address", "location"}
    for field in list(extracted.keys()):
        if field not in pii_fields:
            continue
        values = extracted[field]
        if not values:
            continue
        sorted_values = sorted(values, key=len, reverse=True)
        for val in sorted_values:
            if len(val) > 2:
                s = case_insensitive_replace(s, val, REDACT_TAG)

    # PHASE 2: Redact sensitive categories (race, religion, gender, etc.)
    for field in list(extracted.keys()):
        if field in pii_fields or field == "name":
            continue
        values = extracted[field]
        if not values:
            continue
        sorted_values = sorted(values, key=len, reverse=True)
        for val in sorted_values:
            if len(val) > 2:
                s = case_insensitive_replace(s, val, REDACT_TAG)

    # PHASE 3: Redact name LAST (full name first, then individual parts)
    if "name" in extracted and extracted["name"]:
        name_values = extracted["name"]
        sorted_names = sorted(name_values, key=len, reverse=True)
        for val in sorted_names:
            if len(val) > 2:
                s = case_insensitive_replace(s, val, REDACT_TAG)
        name_parts = sorted_names[0].split()
        for part in name_parts:
            if len(part) > 2:
                s = case_insensitive_replace(s, part, REDACT_TAG)

    return s, extracted

# ---------- String-based PII helpers (no regex) ----------

def _is_email_char(c: str) -> bool:
    return c.isalnum() or c in ('.', '-', '_', '+')

def _is_domain_char(c: str) -> bool:
    return c.isalnum() or c in ('.', '-')

def _find_email(text: str) -> Optional[str]:
    """Find first email address by scanning for '@'."""
    idx = text.find('@')
    while idx != -1:
        start = idx - 1
        while start >= 0 and _is_email_char(text[start]):
            start -= 1
        start += 1
        end = idx + 1
        while end < len(text) and _is_domain_char(text[end]):
            end += 1
        local = text[start:idx]
        domain = text[idx+1:end]
        if local and '.' in domain and len(domain) >= 3:
            if domain[-1] == '.':
                domain = domain[:-1]
                end -= 1
            return text[start:end]
        idx = text.find('@', idx + 1)
    return None

def _find_phone(text: str) -> Optional[str]:
    """Find first phone number by scanning for digit clusters with separators."""
    phone_seps = set('0123456789 ().-+')
    i = 0
    while i < len(text):
        c = text[i]
        if c == '+' or c.isdigit() or c == '(':
            j = i
            digit_count = 0
            while j < len(text) and text[j] in phone_seps:
                if text[j].isdigit():
                    digit_count += 1
                j += 1
            while j > i and not text[j-1].isdigit():
                j -= 1
            if digit_count >= 7 and j > i:
                return text[i:j].strip()
        i += 1
    return None

def _find_url(text: str, domain_marker: str) -> Optional[str]:
    """Find a URL containing the given domain marker (e.g. 'linkedin.com/')."""
    text_lower = text.lower()
    for marker in [f"https://www.{domain_marker}", f"http://www.{domain_marker}",
                   f"https://{domain_marker}", f"http://{domain_marker}", domain_marker]:
        pos = text_lower.find(marker)
        if pos != -1:
            start = pos
            if marker == domain_marker and start > 0:
                k = start - 1
                while k >= 0 and text[k] not in (' ', '\n', '\t', '\r', '|'):
                    k -= 1
                start = k + 1
            end = pos + len(marker)
            while end < len(text) and text[end] not in (' ', '\n', '\t', '\r', '|', ',', ';'):
                end += 1
            url = text[start:end].rstrip('.')
            if url:
                return url
    return None

US_STATES_ABBR = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY',
    'LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND',
    'OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY','DC'
}

US_STATE_NAMES = {
    'Alabama','Alaska','Arizona','Arkansas','California','Colorado','Connecticut',
    'Delaware','Florida','Georgia','Hawaii','Idaho','Illinois','Indiana','Iowa',
    'Kansas','Kentucky','Louisiana','Maine','Maryland','Massachusetts','Michigan',
    'Minnesota','Mississippi','Missouri','Montana','Nebraska','Nevada','New Hampshire',
    'New Jersey','New Mexico','New York','North Carolina','North Dakota','Ohio',
    'Oklahoma','Oregon','Pennsylvania','Rhode Island','South Carolina','South Dakota',
    'Tennessee','Texas','Utah','Vermont','Virginia','Washington','West Virginia',
    'Wisconsin','Wyoming'
}

STREET_SUFFIXES = [
    'street', 'st', 'st.', 'avenue', 'ave', 'ave.', 'road', 'rd', 'rd.',
    'boulevard', 'blvd', 'blvd.', 'lane', 'ln', 'ln.', 'drive', 'dr', 'dr.',
    'court', 'ct', 'ct.', 'way', 'circle', 'cir', 'cir.', 'place', 'pl', 'pl.'
]

def _split_by_separators(line: str) -> List[str]:
    """Split a line by |, \u2013, \u2014 separators."""
    parts = []
    current = []
    for ch in line:
        if ch in ('|', '\u2013', '\u2014'):
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    parts.append(''.join(current).strip())
    return parts

def _looks_like_city_state(s: str) -> Optional[str]:
    """Check if string looks like 'City, State' or 'City, ST' or 'City, ST ZIP'. Returns the match or None."""
    s = s.strip()
    comma_idx = s.find(',')
    if comma_idx < 1:
        return None
    city = s[:comma_idx].strip()
    rest = s[comma_idx+1:].strip()
    if not city or not city[0].isupper():
        return None
    if not all(c.isalpha() or c.isspace() for c in city):
        return None
    parts = rest.split()
    if not parts:
        return None
    state_candidate = parts[0]
    if state_candidate in US_STATES_ABBR or state_candidate in US_STATE_NAMES:
        return s
    if len(parts) >= 2:
        two_word = parts[0] + ' ' + parts[1]
        if two_word in US_STATE_NAMES:
            return s
    return None

def _has_street_suffix(line: str) -> bool:
    """Check if a line contains a street address suffix."""
    words = line.lower().split()
    return any(w in STREET_SUFFIXES for w in words)

def _find_address(lines: List[str], full_text: str) -> Optional[str]:
    """Find address using string matching with priority order."""
    addr_labels = ['address', 'location', 'current address', 'residence']
    
    # Priority 1: Explicit labeled address (e.g., "Address: 123 Main St")
    for line in lines:
        line_lower = line.lower().strip()
        for label in addr_labels:
            if line_lower.startswith(label):
                rest = line[len(label):].lstrip()
                if rest and rest[0] in (':', '-', '\u2013'):
                    value = rest[1:].strip()
                    if value:
                        pipe_idx = value.find('|')
                        if pipe_idx != -1 and '@' in value[pipe_idx:]:
                            value = value[:pipe_idx].strip()
                        return value
    
    # Priority 2: Header lines with pipes/dashes containing "City, State"
    for line in lines[:5]:
        if '|' in line or '\u2013' in line or '\u2014' in line:
            parts = _split_by_separators(line)
            for part in parts[1:]:
                result = _looks_like_city_state(part)
                if result:
                    return result
    
    # Priority 3: Standalone "City, State" line in first 5 lines
    for line in lines[:5]:
        if '|' not in line and '@' not in line and 'http' not in line.lower():
            result = _looks_like_city_state(line)
            if result:
                return result
    
    # Priority 4: Street address in first 10 lines
    for line in lines[:10]:
        if _has_street_suffix(line) and len(line) > 5:
            words = line.split()
            if words and (words[0][0].isdigit()):
                return line
    
    # Priority 5: "City, ST ZIP" or "City, ST" anywhere in text
    for line in lines:
        result = _looks_like_city_state(line.strip())
        if result:
            return result
        if '|' in line:
            for part in _split_by_separators(line):
                result = _looks_like_city_state(part)
                if result:
                    return result
    
    return None

def _find_labeled_field(lines: List[str], labels: List[str]) -> Optional[str]:
    """Find a labeled field like 'Pronouns: he/him' or 'Nationality: American'."""
    for line in lines:
        line_lower = line.lower().strip()
        for label in labels:
            if line_lower.startswith(label):
                rest = line[len(label):].lstrip()
                if rest and rest[0] in (':', '-', '\u2013'):
                    value = rest[1:].strip()
                    if value:
                        return value
    return None

def _is_capitalized_word(w: str) -> bool:
    """Check if a word starts with uppercase and rest is lowercase alpha."""
    if len(w) < 2:
        return False
    return w[0].isupper() and all(c.isalpha() for c in w)

def _is_name_candidate(s: str) -> bool:
    """Check if string looks like 'FirstName LastName' (2-4 capitalized words)."""
    words = s.split()
    if not (2 <= len(words) <= 4):
        return False
    if not (5 <= len(s) <= 50):
        return False
    return all(_is_capitalized_word(w) for w in words)

SECTION_HEADERS = {
    'resume', 'curriculum vitae', 'cv', 'summary', 'objective', 'profile',
    'experience', 'education', 'skills', 'projects', 'certifications',
    'references', 'contact', 'professional summary', 'work experience',
    'technical skills', 'qualifications', 'achievements', 'publications',
    'executive summary', 'career summary', 'career objective',
    'personal statement', 'about me', 'about', 'overview',
    'volunteer experience', 'volunteering', 'leadership',
    'interests', 'hobbies', 'activities', 'awards', 'honors',
    'technical expertise', 'core competencies', 'professional profile',
    'key skills', 'areas of expertise', 'professional experience',
}

NOISE_WORDS = {
    'cisco', 'hp', 'ibm', 'microsoft', 'google', 'apple', 'amazon',
    'oracle', 'dell', 'intel', 'vmware', 'aws', 'azure', 'linux'
}

def _is_section_header(text: str) -> bool:
    """Check if text matches any known section header (exact or with trailing colon/dash)."""
    cleaned = text.lower().strip().rstrip(':').rstrip('-').rstrip('–').strip()
    return cleaned in SECTION_HEADERS


def _find_name(lines: List[str]) -> Optional[str]:
    """Find candidate name from first 10 lines using string heuristics."""
    # First: check for explicit "Name:" or "Full Name:" label
    for line in lines[:10]:
        line_lower = line.lower().strip()
        for label in ['full name', 'name']:
            if line_lower.startswith(label):
                rest = line[len(label):].lstrip()
                if rest and rest[0] in (':', '-', '\u2013'):
                    value = rest[1:].strip()
                    cleaned = ' '.join(value.split())
                    if 2 <= len(cleaned) <= 80:
                        return cleaned

    for l in lines[:10]:
        l_lower = l.lower().strip()

        if _is_section_header(l) or l_lower in NOISE_WORDS:
            continue
        if '@' in l or 'http' in l_lower or 'www.' in l_lower:
            continue

        digit_count = sum(1 for c in l if c.isdigit())
        if digit_count > 3:
            continue

        # Split by separators (|, \u2013, \u2014, " - ")
        if '|' in l or '\u2013' in l or '\u2014' in l or ' - ' in l:
            parts = _split_by_separators(l)
            if ' - ' in l and len(parts) == 1:
                idx = l.find(' - ')
                parts = [l[:idx].strip(), l[idx+3:].strip()]
            candidate = parts[0].strip()
            if _is_name_candidate(candidate) and not _is_section_header(candidate):
                return candidate

        if _is_name_candidate(l) and not _is_section_header(l):
            return l

        # Fallback: 2-4 words, mostly alphabetic
        words = l.split()
        if 2 <= len(words) <= 4 and 5 <= len(l) <= 50:
            letter_count = sum(1 for c in l if c.isalpha() or c.isspace())
            if letter_count / len(l) >= 0.8:
                if words[0][0].isupper():
                    if not _is_section_header(l):
                        return l

    return None


# ---------- Personal info extraction (best-effort) ----------
def extract_personal_info(text: str) -> Dict[str, Optional[str]]:
    """Best-effort extraction of common personal info from raw resume text.
    Returns keys: name, email, phone, address, linkedin, github, pronouns, nationality, marital_status
    Values may be None if not confidently found.
    """
    info: Dict[str, Optional[str]] = {
        "name": None,
        "email": None,
        "phone": None,
        "address": None,
        "linkedin": None,
        "github": None,
        "pronouns": None,
        "nationality": None,
        "marital_status": None,
    }

    if not text:
        return info

    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = [l.strip() for l in normalized.split('\n') if l.strip()]
    head = " ".join(lines[:5]) if lines else ""

    # --- Email: scan for '@' and walk outward ---
    info["email"] = _find_email(text)

    # --- Phone: find digit clusters with separators ---
    info["phone"] = _find_phone(text)

    # --- LinkedIn ---
    info["linkedin"] = _find_url(text, "linkedin.com/")

    # --- GitHub ---
    info["github"] = _find_url(text, "github.com/")

    # --- Address ---
    info["address"] = _find_address(lines, text)

    # --- Pronouns ---
    info["pronouns"] = _find_labeled_field(lines, ["pronouns"])

    # --- Nationality ---
    info["nationality"] = _find_labeled_field(lines, ["nationality"])

    # --- Marital status ---
    info["marital_status"] = _find_labeled_field(lines, ["marital status"])

    # --- Name ---
    info["name"] = _find_name(lines)

    return info

# ---------- Excel sizing helper ----------
def adjust_sheet_dimensions(ws, header_rows: int = 1, min_width: int = 12, max_width: int = 80, padding: int = 5):
    """Dynamically adjust column widths and row heights based on content.
    - header_rows: number of header rows to treat specially (apply larger height)
    - min_width/max_width: width bounds
    - padding: extra width to add beyond longest cell content
    """
    # Column widths (robust to merged cells)
    def _col_letter(col_idx: int) -> str:
        result = ""
        while col_idx > 0:
            col_idx -= 1
            result = chr(col_idx % 26 + ord('A')) + result
            col_idx //= 26
        return result

    # Determine number of columns (avoid merged cell artifacts by scanning header row 1)
    max_col = ws.max_column
    for col_idx in range(1, max_col + 1):
        max_len = 0
        # Iterate cells in this column safely
        for row_idx in range(1, ws.max_row + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            # Skip merged header placeholders without value attributes
            val = getattr(cell, 'value', None)
            if not val:
                continue
            for line in str(val).split("\n"):
                if len(line) > max_len:
                    max_len = len(line)
        col_letter = _col_letter(col_idx)
        width = max(min_width, min(max_width, max_len + padding))
        ws.column_dimensions[col_letter].width = width

    # Row heights
    # Header rows: slightly larger
    for r in range(1, header_rows + 1):
        ws.row_dimensions[r].height = 30 if r == 1 else 22
    # Data rows: estimate height based on number of lines in wrapped cells
    for row_idx in range(header_rows + 1, ws.max_row + 1):
        max_lines = 1
        for cell in ws[row_idx]:
            if cell.value:
                lines = str(cell.value).count('\n') + 1
                if lines > max_lines:
                    max_lines = lines
            cell.alignment = Alignment(wrap_text=True, vertical='top')
        ws.row_dimensions[row_idx].height = min(200, max(18, max_lines * 15))
from cover_letter_analyzer import (
    CoverLetterAnalysis, 
    analyze_cover_letter_with_ai, 
    process_cover_letter_files,
    create_cover_letter_excel_report,
    clean_json_output_cover_letter,
    get_download_link_cover_letter
)

# ---------- Base fields ----------
class Consideration(BaseModel):
    field: str
    instruction: str
    applied: bool
    impact: str

# Core sections - REMOVED experience_relevance_score (duplicate)
# Scores now accept any format (string, number, etc.) for dynamic scoring systems
BASE_FIELDS: Dict[str, Tuple[Type[Any], Any]] = {
    'scoring_format': (Optional[Dict[str, Any]], None),

    'key_strengths': (List[str], ...),
    'key_strengths_score': (Union[str, int, float], ...),
    'key_strengths_explanation': (str, ...),

    'experience_score': (Union[str, int, float], ...),
    'experience_explanation': (str, ...),

    'skills_match_score': (Union[str, int, float], ...),
    'skills_match_explanation': (str, ...),

    'potential_concerns': (List[str], ...),
    'recommendation': (str, ...),

    'candidate_name': (str, ...),
    'job_title': (str, ...),
    'department': (str, ...),

    'overall_score': (Union[str, int, float], ...),
    'overall_explanation': (str, ...),

    'custom_considerations': (List[Consideration], ...),
}

# ---------- Dynamic model helpers ----------
def take_dynamic_input(t: str, enum_vals: Optional[list] = None) -> Tuple[Type[Any], Any]:
    """Return the Pydantic field type for supported custom field types.
    Only 'string' and 'boolean' are supported. Defaults to str for anything else.
    """
    if t == "boolean":
        return (bool, ...)
    # Default to string for unsupported or 'string'
    return (str, ...)

# For each custom field X, add:
#   X (typed value), X_score: dynamic (can be any format), X_explanation: str
def build_dynamic_model(custom_fields: list) -> Type[BaseModel]:
    fields = dict(BASE_FIELDS)
    # Only include supported types: string and boolean
    for f in [cf for cf in custom_fields if cf.get('type') in ('string', 'boolean')]:
        enum_vals = None
        # Normalize field name: replace spaces with underscores for valid Python identifiers
        field_name = f['name'].strip().replace(' ', '_').lower()
        # Make custom fields Optional with default None so missing fields don't break validation
        if f['type'] == 'boolean':
            fields[field_name] = (Optional[bool], Field(default=None))
        else:
            fields[field_name] = (Optional[str], Field(default=None))
        # Dynamic scoring: accept string (e.g., "Red", "High") or number (e.g., 5, 85)
        fields[f"{field_name}_score"] = (Optional[Union[str, int, float]], Field(default=None))
        fields[f"{field_name}_explanation"] = (Optional[str], Field(default=None))
    Model = create_model('EvaluationModel', **fields)
    Model.model_config = {"extra": "ignore"}  # Allow extra fields from LLM without failing
    return Model

# ---------- JSON key normalizer for LLM responses ----------
def normalize_json_keys(data: dict) -> dict:
    """Normalize JSON keys from LLM response to match Pydantic model field names.
    - Converts spaces to underscores
    - Lowercases all keys
    - Handles nested structures
    """
    if not isinstance(data, dict):
        return data
    
    normalized = {}
    for key, value in data.items():
        # Normalize key: strip, lowercase, replace spaces with underscores
        norm_key = key.strip().lower().replace(' ', '_')
        # Recursively normalize nested dicts
        if isinstance(value, dict):
            normalized[norm_key] = normalize_json_keys(value)
        elif isinstance(value, list):
            normalized[norm_key] = [
                normalize_json_keys(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            normalized[norm_key] = value
    return normalized

# ---------- ZIP file processing ----------
def extract_pdfs_from_zip(zip_file) -> List[Tuple[str, bytes]]:
    """
    Extract PDF files from a ZIP archive.
    Returns a list of tuples (filename, pdf_content)
    """
    pdf_files = []
    
    try:
        with zipfile.ZipFile(zip_file, 'r') as zip_ref:
            for file_info in zip_ref.infolist():
                # Skip directories and non-PDF files
                if file_info.is_dir() or not file_info.filename.lower().endswith('.pdf'):
                    continue
                
                # Skip macOS hidden files and metadata
                if '__MACOSX' in file_info.filename or file_info.filename.startswith('._'):
                    continue
                
                # Extract the PDF content
                pdf_content = zip_ref.read(file_info.filename)
                
                # Get just the filename without path
                filename = Path(file_info.filename).name
                
                # Skip if this is a duplicate filename (in case of weird ZIP structure)
                if any(existing_name == filename for existing_name, _ in pdf_files):
                    continue
                
                pdf_files.append((filename, pdf_content))
                
    except zipfile.BadZipFile:
        st.error("❌ Invalid ZIP file. Please upload a valid ZIP archive.")
        return []
    except Exception as e:
        st.error(f"❌ Error reading ZIP file: {str(e)}")
        return []
    
    return pdf_files

def process_uploaded_files(uploaded_files, uploaded_zip):
    """
    Process both individual PDF files and ZIP files containing PDFs.
    Returns a list of tuples (filename, file_object_or_content)
    """
    all_files = []
    
    # Process individual PDF files
    if uploaded_files:
        for file in uploaded_files:
            all_files.append((file.name, file))
    
    # Process ZIP file
    if uploaded_zip:
        st.info(f"📁 Processing ZIP file: {uploaded_zip.name}")
        pdf_files = extract_pdfs_from_zip(uploaded_zip)
        
        if pdf_files:
            st.success(f"✅ Found {len(pdf_files)} PDF files in ZIP archive")
            for filename, pdf_content in pdf_files:
                # Create a file-like object from the PDF content
                file_obj = io.BytesIO(pdf_content)
                file_obj.name = filename  # Add name attribute for compatibility
                all_files.append((filename, file_obj))
        else:
            st.warning("⚠️ No PDF files found in the ZIP archive")
    
    return all_files

# ---------- UI ----------
st.set_page_config(page_title="Resume & Cover Letter Analyzer", layout="wide")
st.title("📄 Resume & Cover Letter Analyzer with AI")

# Create tabs for different functionalities
tab1, tab2 = st.tabs(["📄 Resume Analysis", "✉️ Cover Letter Analysis"])

with tab1:
    st.header("Resume Recommender with Mistral AI")
    
    # ---------- JD inputs ----------
    job_title = st.text_input("Job Title")
    department = st.selectbox("Department", ["Engineering", "Marketing", "Design", "Data", "Other"])
    job_description = st.text_area("Job Description", height=200)

    # ---------- Core Criteria Definitions ----------
    st.subheader("Core Evaluation Criteria")

    # Initialize session state for core criteria definitions
    if 'core_criteria_defs' not in st.session_state:
        st.session_state.core_criteria_defs = {
            'key_strengths': {
                'custom': False,
                'definition': "Evaluate key_strengths based on job requirements. Score from 1 (Poor) to 5 (Exceptional)."
            },
            'experience': {
                'custom': False,
                'definition': "Evaluate experience covering both years of experience AND relevance to this specific role. Score from 1 (Poor) to 5 (Exceptional)."
            },
            'skills_match': {
                'custom': False,
                'definition': "Evaluate skills_match for technical/functional skill alignment. Score from 1 (Poor) to 5 (Exceptional)."
            }
        }

    with st.expander("Define Core Criteria"):
        st.info("📊 **Dynamic Scoring**: Customize what Key Strengths, Experience, and Skills Match mean for your evaluation. You can define ANY scoring system (e.g., 1-5, Red/Yellow/Green, percentages, A/B/C grades, etc.). The AI will adapt to your instructions.")
        
        # Key Strengths definition
        st.write("**Key Strengths Definition**")
        key_strengths_custom = st.checkbox(
            "Customize Key Strengths definition", 
            value=st.session_state.core_criteria_defs['key_strengths']['custom'],
            key="key_strengths_custom"
        )
        
        if key_strengths_custom:
            key_strengths_def = st.text_area(
                "Define what Key Strengths means for this evaluation:",
                value=st.session_state.core_criteria_defs['key_strengths']['definition'],
                placeholder="Example: Evaluate leadership abilities, technical expertise, and communication skills. Rate as Strong/Moderate/Weak.",
                help="Define HOW to evaluate and score this field (e.g., '1-5', 'High/Medium/Low', 'Red/Yellow/Green', percentages, etc.)",
                height=80,
                key="key_strengths_def"
            )
        else:
            key_strengths_def = "Evaluate key_strengths based on job requirements. Score from 1 (Poor) to 5 (Exceptional)."
        
        # Experience definition
        st.write("**Experience Definition**")
        experience_custom = st.checkbox(
            "Customize Experience definition", 
            value=st.session_state.core_criteria_defs['experience']['custom'],
            key="experience_custom"
        )
        
        if experience_custom:
            experience_def = st.text_area(
                "Define what Experience means for this evaluation:",
                value=st.session_state.core_criteria_defs['experience']['definition'],
                placeholder="Example: Evaluate industry-specific background and relevant project work. Rate as Exceeds/Meets/Below expectations.",
                help="Define HOW to evaluate and score this field (e.g., '1-5', 'A/B/C', percentages, qualitative assessment, etc.)",
                height=80,
                key="experience_def"
            )
        else:
            experience_def = "Evaluate experience covering both years of experience AND relevance to this specific role. Score from 1 (Poor) to 5 (Exceptional)."
        
        # Skills Match definition
        st.write("**Skills Match Definition**")
        skills_match_custom = st.checkbox(
            "Customize Skills Match definition", 
            value=st.session_state.core_criteria_defs['skills_match']['custom'],
            key="skills_match_custom"
        )
        
        if skills_match_custom:
            skills_match_def = st.text_area(
                "Define what Skills Match means for this evaluation:",
                value=st.session_state.core_criteria_defs['skills_match']['definition'],
                placeholder="Example: Evaluate technical proficiencies listed in job description. Score as percentage match (0-100%).",
                help="Define HOW to evaluate and score this field (e.g., '1-5', percentages, 'Complete/Partial/None', etc.)",
                height=80,
                key="skills_match_def"
            )
        else:
            skills_match_def = "Evaluate skills_match for technical/functional skill alignment. Score from 1 (Poor) to 5 (Exceptional)."
        
        # Save button
        if st.button("Apply Core Criteria Definitions"):
            st.session_state.core_criteria_defs['key_strengths']['custom'] = key_strengths_custom
            st.session_state.core_criteria_defs['key_strengths']['definition'] = key_strengths_def
            
            st.session_state.core_criteria_defs['experience']['custom'] = experience_custom
            st.session_state.core_criteria_defs['experience']['definition'] = experience_def
            
            st.session_state.core_criteria_defs['skills_match']['custom'] = skills_match_custom
            st.session_state.core_criteria_defs['skills_match']['definition'] = skills_match_def
            
            st.success("✅ Core criteria definitions updated!")
    
    # ---------- Custom fields ----------
    st.subheader("Custom Evaluation Fields")
    if 'custom_fields' not in st.session_state:
        st.session_state.custom_fields = []
    
    with st.expander("Add Custom Field"):
        field_name = st.text_input("Field Name")
        field_type = st.selectbox("Field Type", ["string", "boolean"], help="Only string and boolean are supported.")
        enum_values = []
        if field_type == "enum":
            enum_input = st.text_area("Enum Values (one per line)")
            if enum_input:
                enum_values = [v.strip() for v in enum_input.split('\n') if v.strip()]
    
        instruction = st.text_area(
            "Instruction for how to use this category in evaluation",
            placeholder=(
                "Examples:\n"
                "- Evaluate University location. Score as 'In-State' or 'Out-of-State'.\n"
                "- Count publications. Rate as High (≥3), Medium (1-2), or Low (0).\n"
                "- Assess leadership experience. Score 1-10 based on years and impact.\n"
            ),
            help="Define WHAT to evaluate and HOW to score it. You can use any scoring system (numbers, colors, categories, etc.)",
            height=120
        )
    
        if st.button("Add Field"):
            if field_name and field_type in ["string", "boolean"]:
                new_field = {
                    'name': field_name,
                    'type': field_type,
                    'enum_vals': None,
                    'instruction': (instruction or "").strip() or
                                   "Evaluate this field and provide a score with explanation based on resume evidence."
                }
                st.session_state.custom_fields.append(new_field)
                st.success(f"✅ Added field: {field_name}")
                st.rerun()
    
    # Display + remove
    if st.session_state.custom_fields:
        st.write("**Current Custom Fields:**")
        for i, field in enumerate(st.session_state.custom_fields):
            col1, col2, col3 = st.columns([3, 2, 1])
            with col1:
                st.write(f"• {field['name']} ({field['type']})")
                st.caption(f"Use: {field.get('instruction','')}")
            with col2:
                if field['type'] == 'enum' and field['enum_vals']:
                    st.write(f"Options: {', '.join(field['enum_vals'])}")
            with col3:
                if st.button("Remove", key=f"remove_{i}"):
                    st.session_state.custom_fields.pop(i)
                    st.rerun()
    
    # File upload section
    st.markdown("### 📁 Upload Resumes")

    # ---------- Anonymize Fields (Bias Mitigation) ----------
    st.markdown("### 🛡️ Anonymize Fields (Bias Mitigation)")
    st.caption("Add categories (e.g., name, email, phone, address, gender, race, religion, age, dob, nationality, marital_status, pronouns, linkedin, github) you want redacted before AI evaluation. Use one at a time; remove to stop redacting.")

    if 'anonymize_fields' not in st.session_state:
        st.session_state.anonymize_fields = []

    with st.expander("Add Anonymize Category"):
        anon_cat = st.text_input("Category to Anonymize", placeholder="e.g., name")
        if st.button("Add Anonymize Category"):
            if anon_cat and anon_cat.strip():
                norm = anon_cat.strip()
                if norm.lower() not in [c.lower() for c in st.session_state.anonymize_fields]:
                    st.session_state.anonymize_fields.append(norm)
                    st.success(f"Added anonymize category: {norm}")
                    st.rerun()
                else:
                    st.info("Category already added.")

    if st.session_state.anonymize_fields:
        st.write("**Active Anonymizations:**")
        for i, cat in enumerate(st.session_state.anonymize_fields):
            c1, c2 = st.columns([4,1])
            with c1:
                st.write(f"• {cat}")
            with c2:
                if st.button("Remove", key=f"remove_anon_{i}"):
                    st.session_state.anonymize_fields.pop(i)
                    st.rerun()
    
    upload_method = st.radio(
        "Choose upload method:",
        ["Upload Individual Resumes", "Upload ZIP File"],
        index=0,
        help="Individual Resumes: Select multiple PDF files. ZIP File: Upload one ZIP containing multiple PDF resumes."
    )
    
    uploaded_files = None
    uploaded_zip = None
    
    if upload_method == "Upload Individual Resumes":
        uploaded_files = st.file_uploader(
            "Select multiple PDF resume files", 
            type="pdf", 
            accept_multiple_files=True,
            key="pdf_uploader"
        )
    
    if upload_method == "Upload ZIP File":
        uploaded_zip = st.file_uploader(
            "Upload ZIP file containing PDF resumes", 
            type="zip",
            key="zip_uploader"
        )
    
    # Process all uploaded files
    all_resume_files = process_uploaded_files(uploaded_files, uploaded_zip)
    
    # Display file count summary
    if all_resume_files:
        st.info(f"📊 Ready to process {len(all_resume_files)} resume(s)")
        with st.expander("View file list"):
            for i, (filename, _) in enumerate(all_resume_files, 1):
                st.write(f"{i}. {filename}")
    
    # ---------- Prompt/schema ----------
    def schema_text(job_title: str, department: str, job_description: str, custom_fields: list) -> str:
        lines = [
            '"scoring_format": {',
            '  "scale": ["<worst_value>", "<middle_value>", "<best_value>"],',
            '  "best": "<the best/highest value in your scale>",',
            '  "worst": "<the worst/lowest value in your scale>"',
            '},',
            '"key_strengths": ["strength1", "strength2", "strength3"],',
            '"key_strengths_score": "<score using format from SCORING DEFINITIONS>",',
            '"key_strengths_explanation": "<why this score was given for key strengths>",',
            '"experience_score": "<score using SAME format>",',
            '"experience_explanation": "<why this score was given for experience and relevance to role>",',
            '"skills_match_score": "<score using SAME format>",',
            '"skills_match_explanation": "<short, concrete rationale>",',
            '"potential_concerns": ["concern1", "concern2"],',
            '"candidate_name": "<extract from resume or use \\"Candidate\\">",',
            f'"job_title": "{job_title}",',
            f'"department": "{department}",'
        ]
    
        for f in [cf for cf in custom_fields if cf.get('type') in ('string','boolean')]:
            field_name = f["name"].strip().replace(' ', '_').lower()
            if f["type"] == "boolean":
                lines.append(f'"{field_name}": <true|false|null>,')
            else:
                lines.append(f'"{field_name}": "<string or null if not found>",')
            lines.append(f'"{field_name}_score": "<score using SAME format as core scores>",')
            lines.append(f'"{field_name}_explanation": "<short rationale tied to resume evidence>",')
    
        lines.append('"overall_score": "<score using SAME format as other scores>",')
        lines.append('"overall_explanation": "<1-2 sentences summarizing key drivers from subscores>",')
        lines.append('"recommendation": "Recommended|Consider|Pass",')
    
        lines.append('"custom_considerations": [')
        lines.append('  { "field": "<field name>", "instruction": "<the HR rule text>", "applied": <true|false>, "impact": "<what changed and effect on overall>" }')
        lines.append(']')
    
        return "{\n" + "\n".join(lines) + "\n}"
    
    def build_eval_prompt(
        job_title: str,
        department: str,
        job_description: str,
        custom_fields: list,
        evidence_text: str
    ) -> str:
        schema = schema_text(job_title, department, job_description, custom_fields)
        rules_payload = json.dumps(
            [{"field": f["name"].strip().replace(' ', '_').lower(), "instruction": f.get("instruction", "")} for f in custom_fields],
            ensure_ascii=False
        )
        
        if 'core_criteria_defs' in st.session_state:
            key_strengths_def = st.session_state.core_criteria_defs['key_strengths']['definition']
            experience_def = st.session_state.core_criteria_defs['experience']['definition']
            skills_match_def = st.session_state.core_criteria_defs['skills_match']['definition']
        else:
            key_strengths_def = "Evaluate key_strengths based on job requirements."
            experience_def = "Evaluate experience covering both years of experience AND relevance to this specific role."
            skills_match_def = "Evaluate skills_match for technical/functional skill alignment."
    
        return f"""You are an expert hiring manager evaluating a candidate. Return STRICT JSON only—no prose/markdown/fences.

The candidate's qualifications are provided below. Identity information (name, pronouns, contact, affiliations, demographics) has already been removed so you can focus purely on professional content. Evaluate strictly on demonstrated skills, experience, and qualifications.

SCORING DEFINITIONS (identify the scoring format from these):
- Key Strengths: {key_strengths_def}
- Experience: {experience_def}
- Skills Match: {skills_match_def}

SCORING FORMAT RULES:
1. Read the SCORING DEFINITIONS above to identify the scoring format (e.g., Yes/No/Maybe, 1-100, A/B/C, Good/Medium/Poor, etc.)
2. Use that EXACT SAME format for ALL *_score fields — core AND custom. No mixing.
3. In the "scoring_format" object, list the full ordered scale from worst to best, and specify best/worst values.
4. Based on your overall assessment, set "recommendation" to exactly one of: "Recommended" (strong fit), "Consider" (partial fit), or "Pass" (not a fit). This must reflect your scores — if the overall score is high, recommend; if low, pass.

REQUIRED JSON (exact keys/types):
{schema}

JOB:
Title: {job_title}
Department: {department}
Description: {job_description}

CANDIDATE RESUME (RAW):
{evidence_text}

CUSTOM FIELD INSTRUCTIONS:
{rules_payload}

EVALUATION RULES:
1) Score each field using ONLY the format from SCORING DEFINITIONS — every *_score must use the same format
2) For each custom field, provide value, score, AND explanation — never leave score empty or null
3) overall_score should reflect the aggregate of all subscores
4) Base scores SOLELY on demonstrated professional skills, experience, and qualifications in the resume — NEVER on identity, demographics, names, pronouns, or affiliations
5) Calculate years of experience from work history dates if the resume shows them; consider role relevance to the job description
6) Keep text values concise, avoid special characters or newlines in strings
7) Return ONLY the JSON object"""
    
    # ---------- Pre-Evaluation Check Functions ----------
    def validate_job_details(job_title, department, job_description):
        prompt = (
            f"Given the job title '{job_title}', department '{department}', and job description '{job_description}', "
            f"summarize the key requirements in 3-5 bullets."
        )
        return call_mistral(prompt)
    
    def validate_custom_fields(custom_fields):
        out = []
        for field in custom_fields:
            prompt = (
                f"Custom field '{field['name']}' with instruction '{field['instruction']}'. "
                f"Explain briefly how to evaluate this field based on the instruction provided. "
                f"If the instruction specifies a scoring system (e.g., 1-5, colors, percentages), describe that system. "
                f"Give one example of how you would score a candidate using resume evidence."
            )
            out.append(call_mistral(prompt))
        return out
    
    def run_pre_evaluation_checks(job_title, department, job_description, custom_fields):
        job_validation = validate_job_details(job_title, department, job_description)
        custom_field_validations = validate_custom_fields(custom_fields)
        return job_validation, custom_field_validations
    
    # ---------- Enhanced JSON sanitizer ----------
    def create_excel_report(evaluations_with_metadata):
        """
        Create a comprehensive Excel report with all evaluation results including detailed explanations
        
        Args:
            evaluations_with_metadata: List of dictionaries containing evaluation objects and metadata
            
        Returns:
            BytesIO object containing the Excel file
        """
        wb = Workbook()
        ws = wb.active
        ws.title = "Resume Evaluations"
        
        # Define styles
        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True, size=12)
        subheader_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        subheader_font = Font(color="FFFFFF", bold=True, size=10)
        score_fill = PatternFill(start_color="D5E8D4", end_color="D5E8D4", fill_type="solid")  # Light green for scores
        explanation_fill = PatternFill(start_color="FFE6CC", end_color="FFE6CC", fill_type="solid")  # Light orange for explanations
        border = Border(
            left=Side(style='thin'), 
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        # Create comprehensive headers
        score_headers = [
            "Candidate Name", "Job Title", "Department", "Overall Score", "Recommendation",
            "Key Strengths Score", "Experience Score", "Skills Match Score"
        ]
        
        explanation_headers = [
            "Key Strengths List", "Key Strengths Explanation", "Experience Explanation", 
            "Skills Match Explanation", "Potential Concerns", "Overall Explanation"
        ]
        
        # Add custom field headers if any (only supported types)
        custom_field_names = []
        custom_value_headers = []
        custom_score_headers = []
        custom_explanation_headers = []
        
        if evaluations_with_metadata and "custom_fields" in evaluations_with_metadata[0]:
            for field in evaluations_with_metadata[0]["custom_fields"]:
                if field.get('type') not in ("string", "boolean"):
                    continue
                # Use normalized field name (matching Pydantic model)
                field_name = field['name'].strip().replace(' ', '_').lower()
                field_display = field['name'].replace('_', ' ').title()  # Display uses original for readability
                custom_field_names.append(field_name)
                custom_value_headers.append(f"{field_display} Value")
                custom_score_headers.append(f"{field_display} Score")
                custom_explanation_headers.append(f"{field_display} Explanation")
        
        # Combine all headers
        # Place custom Values and Scores within the SCORES section
        all_score_headers = score_headers + custom_value_headers + custom_score_headers
        all_explanation_headers = explanation_headers + custom_explanation_headers
        all_headers = all_score_headers + all_explanation_headers + ["Custom Considerations", "Extracted Evidence", "Notes"]
        
        # Create main header row
        ws.merge_cells('A1:' + chr(ord('A') + len(all_score_headers) - 1) + '1')
        ws.cell(row=1, column=1, value="SCORES & BASIC INFO").font = header_font
        ws.cell(row=1, column=1).fill = header_fill
        ws.cell(row=1, column=1).alignment = Alignment(horizontal='center', vertical='center')
        ws.cell(row=1, column=1).border = border
        
        start_col = len(all_score_headers) + 1
        end_col = len(all_score_headers) + len(all_explanation_headers)
        ws.merge_cells(f'{chr(ord("A") + start_col - 1)}1:{chr(ord("A") + end_col - 1)}1')
        ws.cell(row=1, column=start_col, value="EXPLANATIONS & REASONING").font = header_font
        ws.cell(row=1, column=start_col).fill = header_fill
        ws.cell(row=1, column=start_col).alignment = Alignment(horizontal='center', vertical='center')
        ws.cell(row=1, column=start_col).border = border
        
        # Add remaining headers for other columns
        remaining_start = len(all_score_headers) + len(all_explanation_headers) + 1
        for i, header in enumerate(["Custom Considerations", "Extracted Evidence", "Notes"]):
            col_num = remaining_start + i
            ws.cell(row=1, column=col_num, value=header).font = header_font
            ws.cell(row=1, column=col_num).fill = header_fill
            ws.cell(row=1, column=col_num).alignment = Alignment(horizontal='center', vertical='center')
            ws.cell(row=1, column=col_num).border = border
        
        # Create sub-header row
        for col_num, header in enumerate(all_score_headers, 1):
            cell = ws.cell(row=2, column=col_num, value=header)
            cell.font = subheader_font
            cell.fill = score_fill
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        
        for col_num, header in enumerate(all_explanation_headers, len(all_score_headers) + 1):
            cell = ws.cell(row=2, column=col_num, value=header)
            cell.font = subheader_font
            cell.fill = explanation_fill
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        
        # Add remaining sub-headers
        remaining_headers = ["Custom Considerations", "Extracted Evidence", "Notes"]
        for i, header in enumerate(remaining_headers):
            col_num = len(all_score_headers) + len(all_explanation_headers) + 1 + i
            cell = ws.cell(row=2, column=col_num, value=header)
            cell.font = subheader_font
            cell.fill = subheader_fill
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border
        
        # Add evaluation data
        for row_num, eval_item in enumerate(evaluations_with_metadata, 3):
            eval_data = eval_item["evaluation"]
            
            # SCORES & BASIC INFO SECTION
            col = 1
            ws.cell(row=row_num, column=col, value=eval_data.candidate_name).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.job_title).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.department).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.overall_score).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.recommendation).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.key_strengths_score).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.experience_score).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.skills_match_score).border = border
            col += 1
            
            # Custom field values (typed: strings/booleans)
            for field_name in custom_field_names:
                val = getattr(eval_data, field_name, None) if hasattr(eval_data, field_name) else None
                ws.cell(row=row_num, column=col, value=val).border = border
                col += 1

            # Custom field scores
            for field_name in custom_field_names:
                score_attr = f"{field_name}_score"
                score_value = getattr(eval_data, score_attr, None) if hasattr(eval_data, score_attr) else None
                ws.cell(row=row_num, column=col, value=score_value).border = border
                col += 1
            
            # EXPLANATIONS & REASONING SECTION
            ws.cell(row=row_num, column=col, value=", ".join(eval_data.key_strengths)).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.key_strengths_explanation).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.experience_explanation).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.skills_match_explanation).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=", ".join(eval_data.potential_concerns)).border = border
            col += 1
            ws.cell(row=row_num, column=col, value=eval_data.overall_explanation).border = border
            col += 1
            
            # Custom field explanations
            for field_name in custom_field_names:
                explanation_attr = f"{field_name}_explanation"
                explanation_value = getattr(eval_data, explanation_attr, None) if hasattr(eval_data, explanation_attr) else None
                ws.cell(row=row_num, column=col, value=explanation_value).border = border
                col += 1
            
            # Custom considerations
            considerations_text = ""
            if hasattr(eval_data, 'custom_considerations') and eval_data.custom_considerations:
                considerations_list = []
                for item in eval_data.custom_considerations:
                    status = "APPLIED" if item.applied else "NOT APPLIED"
                    considerations_list.append(f"{item.field} → {status} | Instruction: {item.instruction} | Impact: {item.impact}")
                considerations_text = "\n".join(considerations_list)
            
            ws.cell(row=row_num, column=col, value=considerations_text).border = border
            col += 1
            
            # Extracted Evidence
            evidence = eval_item.get("evidence_text", "")
            if len(str(evidence)) > 32000:
                evidence = str(evidence)[:32000] + "... (truncated)"
            ws.cell(row=row_num, column=col, value=str(evidence)).border = border
            ws.cell(row=row_num, column=col).alignment = Alignment(wrap_text=True, vertical='top')
            col += 1
            
            # Notes (empty column for manual notes)
            ws.cell(row=row_num, column=col, value="").border = border
        
        # Dynamic sizing
        adjust_sheet_dimensions(ws, header_rows=2)
        
        # Save to BytesIO
        excel_file = io.BytesIO()
        wb.save(excel_file)
        excel_file.seek(0)
        return excel_file
    
    def get_download_link(excel_file, filename):
        """
        Generate a download link for an Excel file
        
        Args:
            excel_file: BytesIO object with Excel data
            filename: Name to use for the download
            
        Returns:
            HTML string with download link
        """
        b64 = base64.b64encode(excel_file.getvalue()).decode()
        href = f'<a href="data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{b64}" download="{filename}">📥 Download Excel Report</a>'
        return href

    def get_download_link_json(data_obj: Any, filename: str) -> str:
        """Generate a download link for a JSON object."""
        try:
            raw = json.dumps(data_obj, ensure_ascii=False, indent=2)
        except Exception:
            raw = json.dumps({"error": "Failed to serialize"})
        b64 = base64.b64encode(raw.encode("utf-8")).decode()
        href = f'<a href="data:application/json;base64,{b64}" download="{filename}">📥 Download JSON Mapping</a>'
        return href

    def create_anonymization_excel_report(mapping: List[Dict[str, Any]], anonymize_fields: List[str]) -> io.BytesIO:
        """Create a separate Excel report revealing anonymized field mappings for each candidate."""
        wb = Workbook()
        ws = wb.active
        ws.title = "Anonymization Mapping"

        header_fill = PatternFill(start_color="4C6EF5", end_color="4C6EF5", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True, size=12)
        border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))

        # Build headers: Candidate, File, then Original value for each selected anonymized field
        norm_fields = [f.strip().lower() for f in anonymize_fields if f.strip()]
        columns = ["Candidate", "File"] + [f.replace("_", " ").title() for f in norm_fields]

        for cidx, col in enumerate(columns, 1):
            cell = ws.cell(row=1, column=cidx, value=col)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = border

        # Rows: show Candidate, File, then the original values for each anonymized field
        for r, entry in enumerate(mapping, start=2):
            ws.cell(row=r, column=1, value=entry.get("placeholder")).border = border
            ws.cell(row=r, column=2, value=entry.get("file")).border = border
            data = entry.get("data", {})
            for cidx, field in enumerate(norm_fields, start=3):
                field_info = data.get(field, {})
                original_val = field_info.get("original", "")
                ws.cell(row=r, column=cidx, value=original_val).border = border

        adjust_sheet_dimensions(ws, header_rows=1)

        excel_file = io.BytesIO()
        wb.save(excel_file)
        excel_file.seek(0)
        return excel_file
    
    def clean_json_output(raw_text: str) -> Optional[str]:
        """
        Tries to extract and sanitize a JSON object from LLM output.
        Handles control characters and malformed JSON, especially complex nested structures.
        """
        # First, try to find the JSON object with a more flexible regex
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if not json_match:
            return None
        
        s = json_match.group(0).strip()
        
        # If the raw JSON looks good already, try parsing it first
        try:
            json.loads(s)
            return s  # If it parses successfully, return as-is
        except json.JSONDecodeError:
            pass  # Continue with cleaning
        
        # Step 1: Replace smart quotes and problematic characters first
        s = s.replace('"', '"').replace('"', '"')  # Smart quotes to regular quotes
        s = s.replace(''', "'").replace(''', "'")  # Smart apostrophes
        
        # Step 2: Remove or replace control characters (but preserve structure)
        s = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', s)  # Remove control chars but keep \n, \r, \t
        
        # Step 3: Normalize whitespace without breaking structure
        s = re.sub(r'\r\n', '\n', s)  # Normalize line endings
        s = re.sub(r'\r', '\n', s)    # Convert remaining CR to LF
        
        # Step 4: Fix common JSON structural issues
        # Remove trailing commas before } or ]
        s = re.sub(r',(\s*[}\]])', r'\1', s)
        
        # Remove leading commas after { or [
        s = re.sub(r'([{\[])\s*,', r'\1', s)
        
        # Fix multiple consecutive commas
        s = re.sub(r',\s*,+', ',', s)
        
        # Step 5: Fix boolean values
        s = re.sub(r':\s*True\b', ': true', s)
        s = re.sub(r':\s*False\b', ': false', s)
        
        # Step 6: Fix unquoted string values (common LLM mistake)
        # Match patterns like: "key": UnquotedValue, or "key": UnquotedValue}
        # Common unquoted values: High, Medium, Low, Consider, Pass, Recommended, Not Met, Met, N/A, etc.
        unquoted_patterns = [
            (r':\s*High\s*([,}])', r': "High"\1'),
            (r':\s*Medium\s*([,}])', r': "Medium"\1'),
            (r':\s*Low\s*([,}])', r': "Low"\1'),
            (r':\s*Consider\s*([,}])', r': "Consider"\1'),
            (r':\s*Pass\s*([,}])', r': "Pass"\1'),
            (r':\s*Recommended\s*([,}])', r': "Recommended"\1'),
            (r':\s*Not Recommended\s*([,}])', r': "Not Recommended"\1'),
            (r':\s*Not Met\s*([,}])', r': "Not Met"\1'),
            (r':\s*Met\s*([,}])', r': "Met"\1'),
            (r':\s*Partially Met\s*([,}])', r': "Partially Met"\1'),
            (r':\s*N/A\s*([,}])', r': "N/A"\1'),
            (r':\s*None\s*([,}])', r': null\1'),
            (r':\s*Strong\s*([,}])', r': "Strong"\1'),
            (r':\s*Weak\s*([,}])', r': "Weak"\1'),
            (r':\s*Yes\s*([,}])', r': "Yes"\1'),
            (r':\s*No\s*([,}])', r': "No"\1'),
        ]
        for pattern, replacement in unquoted_patterns:
            s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)
        
        # Step 7: Clean up spacing around structural elements
        s = re.sub(r'\s*,\s*', ', ', s)  # Normalize comma spacing
        s = re.sub(r'\s*:\s*', ': ', s)  # Normalize colon spacing
        
        return s
    
    # ---------- Run ----------
    if st.button("🔍 Recommend Candidates"):
        # Reuse the already-processed files from line 654, don't process again
        # all_resume_files is already populated above
        
        if not all_resume_files or not job_description:
            st.warning("Please enter the job description and upload at least one resume or a ZIP file containing resumes.")
        else:
            job_validation, custom_field_validations = run_pre_evaluation_checks(
                job_title, department, job_description, st.session_state.custom_fields
            )
    
            st.write("**Job Validation:**")
            st.write(job_validation)
            st.write("**Custom Field Validations:**")
            for v in custom_field_validations:
                st.write(v)
    
            EvaluationModel = build_dynamic_model(st.session_state.custom_fields)
            
            # Initialize list to store evaluation results
            if 'evaluations' not in st.session_state:
                st.session_state.evaluations = []
            else:
                st.session_state.evaluations = []  # Clear previous evaluations

            # Initialize anonymization mapping per run
            st.session_state.anonymization_mapping = []
            st.session_state.candidate_index = 0

            # Panel: collect both models' outputs for arbiter
            arbiter_batch = []
            batch_scoring_format = {}
    
            with st.spinner("Analyzing resumes (Model A: 90b + Model B: Nemotron 49b)..."):
                for resume_filename, file_object in all_resume_files:
                    resume_text = extract_text_from_pdf(file_object)
                    original_text = resume_text
                    candidate_name_from_llm = extract_candidate_name(original_text)
                    extracted = extract_personal_info(original_text)
                    anonymized_text, llm_extracted = anonymize_text(resume_text, st.session_state.get('anonymize_fields'))

                    evidence_text = extract_evidence(resume_text)

                    prompt = build_eval_prompt(
                        job_title, department, job_description, st.session_state.custom_fields, evidence_text
                    )
                    result = call_mistral(prompt)

                    prompt_b = build_eval_prompt(
                        job_title, department, job_description, st.session_state.custom_fields, evidence_text
                    )
                    result_b = call_second_scorer(prompt_b)
    
                    st.markdown(f"### 📄 {resume_filename}")
                    if isinstance(result, dict) and "choices" in result:
                        raw_text = result["choices"][0]["message"]["content"]
                        try:
                            cleaned = clean_json_output(raw_text)
                            if not cleaned:
                                st.error("❌ Could not find a JSON object in the model output.")
                                st.write("**Raw Response:**")
                                st.write(raw_text)
                                continue
    
                            try:
                                data = json.loads(cleaned)
                            except json.JSONDecodeError as e:
                                st.error(f"❌ JSON parsing failed: {e}")
                                st.write("**Attempted to clean:**")
                                st.code(cleaned, language="json")
                                st.write("**Raw Response:**")
                                st.write(raw_text)
                                
                                # Try multiple fallback parsing strategies
                                try:
                                    # Strategy 1: More aggressive JSON extraction and cleaning
                                    start = raw_text.find('{')
                                    end = raw_text.rfind('}') + 1
                                    if start != -1 and end > start:
                                        simple_json = raw_text[start:end]
                                        
                                        # Ultra-aggressive cleanup
                                        simple_json = simple_json.replace('"', '"').replace('"', '"')
                                        simple_json = simple_json.replace(''', "'").replace(''', "'")
                                        simple_json = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', simple_json)
                                        simple_json = simple_json.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
                                        
                                        # Fix common JSON issues
                                        simple_json = re.sub(r',(\s*[}\]])', r'\1', simple_json)  # Remove trailing commas
                                        simple_json = re.sub(r'([{\[])\s*,', r'\1', simple_json)  # Remove leading commas
                                        simple_json = re.sub(r',\s*,+', ',', simple_json)  # Fix multiple commas
                                        simple_json = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', simple_json)  # Quote field names
                                        simple_json = re.sub(r':\s*True\b', ': true', simple_json)  # Fix booleans
                                        simple_json = re.sub(r':\s*False\b', ': false', simple_json)
                                        simple_json = re.sub(r'\s+', ' ', simple_json)  # Normalize spaces
                                        
                                        # Try to balance braces and brackets
                                        open_braces = simple_json.count('{')
                                        close_braces = simple_json.count('}')
                                        if open_braces > close_braces:
                                            simple_json += '}' * (open_braces - close_braces)
                                        
                                        open_brackets = simple_json.count('[')
                                        close_brackets = simple_json.count(']')
                                        if open_brackets > close_brackets:
                                            simple_json += ']' * (open_brackets - close_brackets)
                                        
                                        data = json.loads(simple_json)
                                        st.info("✅ Enhanced fallback parsing succeeded!")
                                    else:
                                        continue
                                except json.JSONDecodeError as e2:
                                    # Strategy 2: Character-by-character reconstruction
                                    try:
                                        # Find the problematic character around position 1119
                                        error_pos = getattr(e2, 'pos', 1119)
                                        
                                        # Try to fix the specific area around the error
                                        fixed_json = raw_text.replace('"', '"').replace('"', '"')
                                        fixed_json = fixed_json.replace(''', "'").replace(''', "'")
                                        
                                        start = fixed_json.find('{')
                                        end = fixed_json.rfind('}') + 1
                                        if start != -1 and end > start:
                                            fixed_json = fixed_json[start:end]
                                            
                                            # Remove problematic characters around error position
                                            if error_pos < len(fixed_json):
                                                # Look for common issues around the error position
                                                context_start = max(0, error_pos - 50)
                                                context_end = min(len(fixed_json), error_pos + 50)
                                                context = fixed_json[context_start:context_end]
                                                
                                                # Fix common issues in the context
                                                context = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', context)
                                                context = re.sub(r',(\s*[}\]])', r'\1', context)
                                                context = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', context)
                                                
                                                # Reconstruct the JSON
                                                fixed_json = fixed_json[:context_start] + context + fixed_json[context_end:]
                                            
                                            # Final cleanup
                                            fixed_json = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', fixed_json)
                                            fixed_json = re.sub(r',(\s*[}\]])', r'\1', fixed_json)
                                            
                                            data = json.loads(fixed_json)
                                            st.info("✅ Position-specific fix succeeded!")
                                        else:
                                            continue
                                    except Exception as e3:
                                        st.error(f"❌ All parsing strategies failed. Last error: {str(e3)}")
                                        st.write("**Debug info:**")
                                        st.write(f"Original error position: {getattr(e, 'pos', 'unknown')}")
                                        st.write(f"Cleaned JSON length: {len(cleaned) if cleaned else 'N/A'}")
                                        continue
    
                            # Normalize JSON keys before validation (spaces → underscores, lowercase)
                            data = normalize_json_keys(data)
                            
                            # Apply deterministic business rules (recommendation, format validation, requirements override)
                            data = apply_business_rules(data)
                            
                            # Validate with dynamic Pydantic model
                            try:
                                evaluation = EvaluationModel(**data)
                            except ValidationError as ve:
                                # Log validation error but try to proceed with partial data
                                st.warning(f"⚠️ Validation warning: Some fields may be missing or malformed. {ve}")
                                # Fill missing required fields with None and retry
                                model_fields = EvaluationModel.model_fields
                                for field_name in model_fields:
                                    if field_name not in data:
                                        data[field_name] = None
                                evaluation = EvaluationModel.model_construct(**data)

                            # Use LLM-extracted name (from raw resume, before cleaning)
                            if candidate_name_from_llm:
                                try:
                                    evaluation.candidate_name = candidate_name_from_llm
                                except Exception:
                                    pass

                            # If any field is anonymized, capture the mapping
                            anonymize_list = [c.strip().lower() for c in st.session_state.get('anonymize_fields', [])]
                            if anonymize_list:
                                has_name = any('name' in field for field in anonymize_list)
                                if has_name:
                                    st.session_state.candidate_index += 1
                                    placeholder = f"Candidate {st.session_state.candidate_index}"
                                    try:
                                        evaluation.candidate_name = placeholder
                                    except Exception:
                                        pass
                                else:
                                    placeholder = evaluation.candidate_name if hasattr(evaluation, 'candidate_name') else "Unknown"
                                
                                # Build redaction map from LLM extraction results
                                redaction_map: Dict[str, Any] = {}
                                for cat in anonymize_list:
                                    values = llm_extracted.get(cat, [])
                                    # Keep only values that were actually present and got redacted
                                    kept_vals: List[str] = []
                                    seen_vals: set = set()
                                    for v in values:
                                        if v and v not in seen_vals:
                                            seen_vals.add(v)
                                            if v.lower() in original_text.lower() and v.lower() not in anonymized_text.lower():
                                                kept_vals.append(v)

                                    if kept_vals:
                                        joined = " | ".join(kept_vals)
                                        redaction_map[cat] = {"original": joined, "status": "redacted"}
                                
                                # Only append if there's data to record
                                if redaction_map or len(anonymize_list) > 0:
                                    st.session_state.anonymization_mapping.append({
                                        "placeholder": placeholder,
                                        "file": resume_filename,
                                        "data": redaction_map
                                    })
    
                            # ------- UI: core sections (REMOVED experience_relevance) -------
                            col1, col2 = st.columns([1, 2])
                            with col1:
                                st.metric("Key Strengths", f"{evaluation.key_strengths_score}")
                                st.caption(f"💭 {evaluation.key_strengths_explanation}")
    
                                st.metric("Experience", f"{evaluation.experience_score}")
                                st.caption(f"💭 {evaluation.experience_explanation}")
    
                                st.metric("Skills Match", f"{evaluation.skills_match_score}")
                                st.caption(f"💭 {evaluation.skills_match_explanation}")
    
                            with col2:
                                st.write(f"**Candidate:** {evaluation.candidate_name}")
                                st.caption(f"Role: {evaluation.job_title} · Dept: {evaluation.department}")
                                st.write("**💪 Key Strengths**")
                                for s in evaluation.key_strengths:
                                    st.write(f"• {s}")
                                st.write("**⚠️ Potential Concerns**")
                                for c in evaluation.potential_concerns:
                                    st.write(f"• {c}")
    
                            # ------- Custom fields -------
                            if st.session_state.custom_fields:
                                st.write("**📊 Custom Fields**")
                                for field in st.session_state.custom_fields:
                                    # Use normalized field name to match Pydantic model
                                    fname = field['name'].strip().replace(' ', '_').lower()
                                    label = field['name'].replace('_', ' ').title()  # Display original for readability
                                    val = getattr(evaluation, fname, None)
                                    sval = getattr(evaluation, f"{fname}_score", None)
                                    expl = getattr(evaluation, f"{fname}_explanation", None)
    
                                    if sval is not None:
                                        st.metric(label, f"{sval}")
                                        if val is not None:
                                            st.caption(f"• Value: {val}")
                                        if expl:
                                            st.caption(f"💭 {expl}")
                                    else:
                                        if val is not None:
                                            st.write(f"**{label}:** {val}")
    
                            # ------- How instructions were applied -------
                            if getattr(evaluation, "custom_considerations", None):
                                st.write("**🧠 How Your Instructions Were Applied**")
                                for item in evaluation.custom_considerations:
                                    st.write(
                                        f"- **{item.field}** → "
                                        f"{'APPLIED' if item.applied else 'NOT APPLIED'} | "
                                        f"_Instruction_: {item.instruction} | "
                                        f"_Impact_: {item.impact}"
                                    )
    
                            # ------- OVERALL (LAST): use model's score directly -------
                            st.divider()
                            cols = st.columns([1, 4])
                            with cols[0]:
                                st.metric("Overall Score", f"{evaluation.overall_score}")
                            with cols[1]:
                                st.info(f"**Recommendation:** {evaluation.recommendation}")
                                st.caption(f"💭 {evaluation.overall_explanation}")
                                
                            # Store successful evaluation for Excel export
                            eval_with_metadata = {
                                "evaluation": evaluation,
                                "custom_fields": st.session_state.custom_fields,
                                "resume_filename": resume_filename,
                                "evidence_text": evidence_text
                            }
                            st.session_state.evaluations.append(eval_with_metadata)

                            # Collect Model A data for arbiter
                            scores_a = {k: v for k, v in data.items() if k.endswith("_score")}
                            scores_a["recommendation"] = data.get("recommendation", "")
                            sf = data.get("scoring_format") or {}
                            if sf and not batch_scoring_format:
                                batch_scoring_format = sf
                            explanations_a = {k: v for k, v in data.items() if k.endswith("_explanation")}
                            concerns_a = data.get("potential_concerns", [])

                            # Parse Model B output
                            scores_b = {}
                            explanations_b = {}
                            concerns_b = []
                            if isinstance(result_b, dict) and "choices" in result_b:
                                raw_b = result_b["choices"][0]["message"]["content"]
                                cleaned_b = clean_json_output(raw_b)
                                if cleaned_b:
                                    try:
                                        data_b = json.loads(cleaned_b)
                                        data_b = normalize_json_keys(data_b)
                                        scores_b = {k: v for k, v in data_b.items() if k.endswith("_score")}
                                        scores_b["recommendation"] = data_b.get("recommendation", "")
                                        explanations_b = {k: v for k, v in data_b.items() if k.endswith("_explanation")}
                                        concerns_b = data_b.get("potential_concerns", [])
                                    except (json.JSONDecodeError, Exception):
                                        pass

                            arbiter_batch.append({
                                "candidate_name": getattr(evaluation, "candidate_name", "Unknown"),
                                "evidence": evidence_text,
                                "scores_a": scores_a,
                                "explanations_a": explanations_a,
                                "concerns_a": concerns_a,
                                "scores_b": scores_b,
                                "explanations_b": explanations_b,
                                "concerns_b": concerns_b,
                            })
    
                        except (ValueError, ValidationError) as e:
                            st.error(f"❌ Failed to validate evaluation: {str(e)}")
                            st.write("**Raw Response:**")
                            st.write(raw_text)
                        except Exception as e:
                            st.error(f"❌ Unexpected error: {str(e)}")
                            st.write("**Raw Response:**")
                            st.write(raw_text)
                    else:
                        st.error("❌ Failed to get response from Mistral.")
                        
            # ---------- ARBITER (GPT 120b final judgment from both models) ----------
            if len(arbiter_batch) >= 2:
                with st.spinner("GPT 120b arbiter reviewing both models' judgments for all candidates..."):
                    final_scores = arbiter_final_judgment(
                        arbiter_batch, batch_scoring_format,
                        job_title=job_title, job_description=job_description,
                        custom_fields=st.session_state.custom_fields
                    )

                changes_made = 0
                for idx, final in enumerate(final_scores):
                    if idx >= len(st.session_state.evaluations):
                        break
                    eval_obj = st.session_state.evaluations[idx]["evaluation"]
                    original_scores = arbiter_batch[idx]["scores_a"]

                    for field, new_val in final.items():
                        old_val = original_scores.get(field)
                        if str(new_val) != str(old_val):
                            changes_made += 1
                            try:
                                setattr(eval_obj, field, new_val)
                            except Exception:
                                pass

                if changes_made > 0:
                    st.success(f"Arbiter (GPT 120b) finalized scores — adjusted {changes_made} score(s) across {len(arbiter_batch)} candidates after reviewing both models.")
                else:
                    st.info("Arbiter confirmed both models agree — no adjustments needed.")

            # After all evaluations, offer Excel download if we have results
            if st.session_state.evaluations:
                st.divider()
                st.subheader("📊 Export Results")
                
                # Generate Excel report
                today = datetime.datetime.now().strftime("%Y-%m-%d")
                job_title_slug = job_title.lower().replace(" ", "_")
                filename = f"resume_evaluations_{job_title_slug}_{today}.xlsx"
                
                excel_file = create_excel_report(st.session_state.evaluations)
                
                # Display download button
                st.markdown(get_download_link(excel_file, filename), unsafe_allow_html=True)
                st.caption("Export all evaluation results to Excel for offline review")

                # If any field is anonymized, provide mapping export options
                if st.session_state.get('anonymization_mapping') and st.session_state.get('anonymize_fields'):
                    st.divider()
                    st.subheader("🧩 Export Anonymization Mapping")
                    today = datetime.datetime.now().strftime("%Y-%m-%d")
                    job_title_slug = job_title.lower().replace(" ", "_")

                    mapping_excel = create_anonymization_excel_report(
                        st.session_state.anonymization_mapping,
                        st.session_state.get('anonymize_fields', [])
                    )
                    mapping_excel_name = f"anonymization_mapping_{job_title_slug}_{today}.xlsx"
                    st.markdown(get_download_link(mapping_excel, mapping_excel_name), unsafe_allow_html=True)

                    # JSON download
                    mapping_json_name = f"anonymization_mapping_{job_title_slug}_{today}.json"
                    st.markdown(
                        get_download_link_json(st.session_state.anonymization_mapping, mapping_json_name),
                        unsafe_allow_html=True
                    )
                    st.caption("Download the mapping that reveals anonymized fields for each candidate placeholder.")
    
with tab2:
    st.header("Cover Letter AI Detection")
    st.caption("Simple AI vs Human detection for cover letters")
    
    # ---------- File Upload Section ----------
    st.subheader("📤 Upload Cover Letters")
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.markdown("**Option 1: Individual Files**")
        uploaded_cover_letters = st.file_uploader(
            "Upload Cover Letters", 
            type=['pdf'],
            accept_multiple_files=True,
            key="cover_letters",
            help="Upload individual PDF cover letters"
        )
    
    with col2:
        st.markdown("**Option 2: ZIP Archive**")
        uploaded_cover_zip = st.file_uploader(
            "Upload ZIP file containing cover letters",
            type=['zip'],
            key="cover_zip",
            help="Upload a ZIP file containing multiple PDF cover letters"
        )
    
    # ---------- Display uploaded files ----------
    all_cover_files = process_cover_letter_files(uploaded_cover_letters, uploaded_cover_zip)
    
    if all_cover_files:
        st.success(f"📁 Ready to analyze {len(all_cover_files)} cover letter(s)")
        with st.expander("View uploaded files"):
            for filename, _ in all_cover_files:
                st.write(f"• {filename}")
    
    # ---------- Analysis Section ----------
    if st.button("🔍 Analyze Cover Letters", key="analyze_covers"):
        if not all_cover_files:
            st.warning("Please upload at least one cover letter file or ZIP archive.")
        else:
            # Initialize session state for cover letter analyses
            if 'cover_analyses' not in st.session_state:
                st.session_state.cover_analyses = []
            else:
                st.session_state.cover_analyses = []  # Clear previous analyses
            
            with st.spinner("Analyzing cover letters for AI detection..."):
                for cover_filename, file_content in all_cover_files:
                    st.markdown(f"### 📄 {cover_filename}")
                    
                    # Extract text from PDF
                    cover_text = extract_text_from_pdf(io.BytesIO(file_content))
                    # Apply anonymization (reuse the same categories configured in Resume tab)
                    cover_text, _ = anonymize_text(cover_text, st.session_state.get('anonymize_fields'))
                    
                    if not cover_text.strip():
                        st.error("❌ Could not extract text from this file.")
                        continue
                    
                    # Analyze with AI
                    result = analyze_cover_letter_with_ai(cover_text)
                    
                    if result:
                        try:
                            # Clean and parse JSON response
                            cleaned = clean_json_output_cover_letter(result)
                            if not cleaned:
                                st.error("❌ Could not extract analysis from AI response.")
                                st.write("**Raw Response:**")
                                st.write(result)
                                continue
                            
                            # Parse JSON
                            try:
                                data = json.loads(cleaned)
                            except json.JSONDecodeError as e:
                                st.error(f"❌ JSON parsing failed: {e}")
                                st.write("**Attempted to clean:**")
                                st.code(cleaned, language="json")
                                continue
                            
                            # Set filename in data
                            data['file_name'] = cover_filename
                            
                            # Validate with Pydantic model
                            analysis = CoverLetterAnalysis(**data)
                            
                            # Store analysis for Excel export
                            st.session_state.cover_analyses.append(analysis)
                            
                            # ------- Display Results -------
                            col1, col2 = st.columns([1, 1])
                            
                            with col1:
                                # Classification with color coding
                                if analysis.classification == "AI-Generated":
                                    st.error(f"🤖 **{analysis.classification}**")
                                    st.metric("AI Probability", f"{analysis.ai_generated_probability:.1f}%")
                                else:
                                    st.success(f"👤 **{analysis.classification}**")
                                    st.metric("Human Probability", f"{100-analysis.ai_generated_probability:.1f}%")
                                
                                st.metric("Confidence", analysis.confidence_level)
                            
                            with col2:
                                st.write(f"**Applicant:** {analysis.applicant_name}")
                                
                                st.write("**🔍 Key Indicators:**")
                                for indicator in analysis.key_indicators:
                                    st.write(f"• {indicator}")
                            
                            st.divider()
                        
                        except ValidationError as e:
                            st.error(f"❌ Failed to validate analysis: {str(e)}")
                            st.write("**Raw Response:**")
                            st.write(result)
                        except Exception as e:
                            st.error(f"❌ Unexpected error: {str(e)}")
                            st.write("**Raw Response:**")
                            st.write(result)
                    else:
                        st.error("❌ Failed to get response from AI.")
            
            # After all analyses, offer Excel download if we have results
            if st.session_state.cover_analyses:
                st.divider()
                st.subheader("📊 Export Cover Letter Analysis")
                
                # Generate Excel report
                today = datetime.datetime.now().strftime("%Y-%m-%d")
                filename = f"cover_letter_analysis_{today}.xlsx"
                
                excel_file = create_cover_letter_excel_report(st.session_state.cover_analyses)
                
                # Display download button
                st.markdown(get_download_link_cover_letter(excel_file, filename), unsafe_allow_html=True)
                st.caption("Export all cover letter analyses to Excel for offline review")
                
                # Display summary statistics
                ai_count = sum(1 for a in st.session_state.cover_analyses if a.classification == "AI-Generated")
                human_count = len(st.session_state.cover_analyses) - ai_count
                
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Total Analyzed", len(st.session_state.cover_analyses))
                with col2:
                    st.metric("AI-Generated", ai_count, delta=f"{ai_count/len(st.session_state.cover_analyses)*100:.1f}%")
                with col3:
                    st.metric("Human-Written", human_count, delta=f"{human_count/len(st.session_state.cover_analyses)*100:.1f}%")