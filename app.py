from typing import List, Dict, Any, Optional, Tuple, Type, Literal
from pydantic import BaseModel, Field, create_model, ValidationError
import streamlit as st
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf
import json, re, zipfile, io, datetime, base64
from pathlib import Path
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
 
# ---------- Anonymization helper ----------
def anonymize_text(text: str, fields: Optional[List[str]]) -> str:
    """Redact user-selected categories from free text to reduce bias.
    Supported categories (case-insensitive):
    - name, email, phone, address, gender, age, date_of_birth/dob,
      nationality, marital_status, linkedin, github, pronouns
    Unknown categories: lines containing the term will be removed.
    """
    if not text or not fields:
        return text

    s = text
    cats = {f.strip().lower() for f in fields if isinstance(f, str) and f.strip()}

    # Helper to check if any field contains a keyword
    def has_keyword(keyword):
        return any(keyword in cat for cat in cats)

    # Email
    if any(c in cats for c in ["email", "e-mail"]) or has_keyword("email"):
        s = re.sub(r"[\w.\-+]+@[\w\-]+(?:\.[\w\-]+)+", "[REDACTED_EMAIL]", s)

    # Phone numbers
    if any(c in cats for c in ["phone", "phone_number", "contact"]) or has_keyword("phone"):
        s = re.sub(r"\+?\d[\d\s().\-]{7,}\d", "[REDACTED_PHONE]", s)

    # LinkedIn / GitHub URLs
    if any(c in cats for c in ["linkedin", "github", "portfolio", "website"]) or has_keyword("linkedin") or has_keyword("github"):
        s = re.sub(r"https?://(www\.)?(linkedin\.com|github\.com|bit\.ly|linktr\.ee|\S+\.(io|dev|app|site))/\S+",
                   "[REDACTED_LINK]", s, flags=re.IGNORECASE)

    # Address lines
    if "address" in cats or has_keyword("address") or has_keyword("location"):
        # Remove explicit Address: lines
        s = re.sub(r"(?im)^\s*(address|location|current\s*address)\s*[:\-].*$", "[REDACTED_ADDRESS]", s)
        # Mask common street patterns
        s = re.sub(r"\b\d{1,5}\s+\w+(?:\s\w+){0,4}\s(?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Boulevard|Blvd\.|Lane|Ln\.|Drive|Dr\.)\b[\w\s,.-]*",
                   "[REDACTED_ADDRESS]", s, flags=re.IGNORECASE)
        # Catch city/state/zip patterns like "Boston, MA 02101"
        s = re.sub(r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?", "[REDACTED_ADDRESS]", s)
        # Catch city/state patterns without ZIP like "Waltham, MA" or "San Francisco, CA"
        s = re.sub(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2}\b", "[REDACTED_ADDRESS]", s)

    # Name (best-effort): extract name from original, then replace all occurrences
    if "name" in cats:
        # Try to extract the actual candidate name from the ORIGINAL text before any redactions
        candidate_name = None
        lines = [l.strip() for l in re.split(r"\r?\n", text) if l.strip()]
        
        # Check for labeled name first in original text
        m = re.search(r"(?im)^\s*(name|full\s*name)\s*[:\-]\s*(.+)$", text)
        if m:
            candidate_name = re.sub(r"\s+", " ", m.group(2)).strip()
        else:
            # Heuristic: first line that looks like a name (letters/spaces, no numbers/emails)
            for l in lines[:5]:
                if ("@" in l) or re.search(r"\d", l):
                    continue
                if len(l.split()) <= 6 and re.search(r"[A-Za-z]", l):
                    candidate_name = l
                    break
        
        # If we found a name, replace all occurrences of it in the text
        if candidate_name and len(candidate_name) > 2:
            # Escape special regex characters in the name
            escaped_name = re.escape(candidate_name)
            # Replace full name
            s = re.sub(rf"\b{escaped_name}\b", "[REDACTED_NAME]", s, flags=re.IGNORECASE)
            
            # Also replace individual name parts (first/last name alone)
            name_parts = candidate_name.split()
            if len(name_parts) >= 2:
                first_name = name_parts[0]
                last_name = name_parts[-1]
                # Only replace if they're reasonably long to avoid false positives
                if len(first_name) > 2:
                    s = re.sub(rf"\b{re.escape(first_name)}\b", "[REDACTED_NAME]", s, flags=re.IGNORECASE)
                if len(last_name) > 2:
                    s = re.sub(rf"\b{re.escape(last_name)}\b", "[REDACTED_NAME]", s, flags=re.IGNORECASE)
        
        # Also remove labeled name lines
        s = re.sub(r"(?im)^\s*(name|full\s*name)\s*[:\-].*$", "[REDACTED_NAME]", s)

    # Gender
    if "gender" in cats or has_keyword("gender"):
        s = re.sub(r"(?im)^\s*gender\s*[:\-].*$", "[REDACTED_GENDER]", s)
        s = re.sub(r"\b(male|female|non\-?binary|woman|man|transgender|cisgender)\b",
                   "[REDACTED_GENDER]", s, flags=re.IGNORECASE)

    # Age / DOB
    if any(c in cats for c in ["age", "dob", "date_of_birth"]) or has_keyword("age") or has_keyword("dob") or has_keyword("birth"):
        s = re.sub(r"(?im)^\s*age\s*[:\-]\s*\d+\b.*$", "[REDACTED_AGE]", s)
        s = re.sub(r"(?im)^\s*(dob|date\s*of\s*birth)\s*[:\-].*$", "[REDACTED_DOB]", s)
        s = re.sub(r"\b\d{1,2}[\-/]\d{1,2}[\-/](\d{2,4})\b", "[REDACTED_DOB]", s)

    # Nationality / Marital status / Pronouns
    if "nationality" in cats or has_keyword("nationality"):
        s = re.sub(r"(?im)^\s*nationality\s*[:\-].*$", "[REDACTED_NATIONALITY]", s)
    if "marital_status" in cats or has_keyword("marital"):
        s = re.sub(r"(?im)^\s*marital\s*status\s*[:\-].*$", "[REDACTED_MARITAL_STATUS]", s)
    if "pronouns" in cats or has_keyword("pronoun"):
        s = re.sub(r"(?im)^\s*pronouns\s*[:\-].*$", "[REDACTED_PRONOUNS]", s)

    # Generic fallback: remove lines containing unknown terms
    known = {"name","email","e-mail","phone","phone_number","contact","address","gender","age","dob",
             "date_of_birth","nationality","marital_status","linkedin","github","portfolio","website","pronouns"}
    unknown = [c for c in cats if c not in known]
    for term in unknown:
        try:
            s = re.sub(rf"(?im)^.*\b{re.escape(term)}\b.*$", "[REDACTED]", s)
        except re.error:
            continue

    return s

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

    # Normalize newlines
    lines = [l.strip() for l in re.split(r"\r?\n", text) if l.strip()]
    head = " ".join(lines[:5]) if lines else ""

    # Email
    m = re.search(r"[\w.\-+]+@[\w\-]+(?:\.[\w\-]+)+", text)
    if m:
        info["email"] = m.group(0)

    # Phone
    m = re.search(r"\+?\d[\d\s().\-]{7,}\d", text)
    if m:
        info["phone"] = m.group(0)

    # LinkedIn
    m = re.search(r"https?://(?:www\.)?linkedin\.com/\S+", text, flags=re.I)
    if m:
        info["linkedin"] = m.group(0)

    # GitHub
    m = re.search(r"https?://(?:www\.)?github\.com/\S+", text, flags=re.I)
    if m:
        info["github"] = m.group(0)

    # Address (line starting with Address/Location or street pattern)
    m = re.search(r"(?im)^\s*(address|location|current\s*address)\s*[:\-]\s*(.+)$", text)
    if m:
        info["address"] = m.group(2).strip()
    else:
        # Try to find street address pattern
        m = re.search(r"\b\d{1,5}\s+\w+(?:\s\w+){0,4}\s(?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Boulevard|Blvd\.|Lane|Ln\.|Drive|Dr\.)\b[\w\s,.-]*",
                      text, flags=re.I)
        if m:
            street = m.group(0).strip()
            # Try to find associated city/state/zip pattern nearby
            m2 = re.search(r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?", text)
            if m2:
                info["address"] = f"{street}, {m2.group(0)}"
            else:
                info["address"] = street
        else:
            # Try city/state/zip pattern first
            m = re.search(r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?", text)
            if m:
                info["address"] = m.group(0).strip()
        else:
            # Try full state name (e.g., "Boston, Massachusetts")
            m = re.search(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', text)
            if m:
                city, state = m.groups()
                # Validate it looks like a US state (common ones)
                us_state_names = {
                    'Alabama','Alaska','Arizona','Arkansas','California','Colorado','Connecticut',
                    'Delaware','Florida','Georgia','Hawaii','Idaho','Illinois','Indiana','Iowa',
                    'Kansas','Kentucky','Louisiana','Maine','Maryland','Massachusetts','Michigan',
                    'Minnesota','Mississippi','Missouri','Montana','Nebraska','Nevada','New Hampshire',
                    'New Jersey','New Mexico','New York','North Carolina','North Dakota','Ohio',
                    'Oklahoma','Oregon','Pennsylvania','Rhode Island','South Carolina','South Dakota',
                    'Tennessee','Texas','Utah','Vermont','Virginia','Washington','West Virginia',
                    'Wisconsin','Wyoming'
                }
                if state in us_state_names:
                    info["address"] = m.group(0).strip()

    # Pronouns
    m = re.search(r"(?im)^\s*pronouns\s*[:\-]\s*(.+)$", text)
    if m:
        info["pronouns"] = m.group(1).strip()

    # Nationality
    m = re.search(r"(?im)^\s*nationality\s*[:\-]\s*(.+)$", text)
    if m:
        info["nationality"] = m.group(1).strip()

    # Marital status
    m = re.search(r"(?im)^\s*marital\s*status\s*[:\-]\s*(.+)$", text)
    if m:
        info["marital_status"] = m.group(1).strip()

    # Name – improved heuristics with better filtering
    m = re.search(r"(?im)^\s*(name|full\s*name)\s*[:\-]\s*(.+)$", text)
    if m:
        name_val = re.sub(r"\s+", " ", m.group(2)).strip()
        if 2 <= len(name_val) <= 80:
            info["name"] = name_val
    else:
        # Common resume section headers to exclude (case-insensitive)
        section_headers = {
            'resume', 'curriculum vitae', 'cv', 'summary', 'objective', 'profile',
            'experience', 'education', 'skills', 'projects', 'certifications',
            'references', 'contact', 'professional summary', 'work experience',
            'technical skills', 'qualifications', 'achievements', 'publications'
        }
        
        # Common company/product names that aren't names
        noise_words = {
            'cisco', 'hp', 'ibm', 'microsoft', 'google', 'apple', 'amazon',
            'oracle', 'dell', 'intel', 'vmware', 'aws', 'azure', 'linux'
        }
        
        # Search first 10 lines for best name candidate
        for l in lines[:10]:
            l_lower = l.lower().strip()
            
            # Skip if it's a section header
            if l_lower in section_headers:
                continue
            
            # Skip if it's a noise word
            if l_lower in noise_words:
                continue
            
            # Skip if contains URL, email, or phone patterns
            if ("@" in l) or ("http" in l_lower) or ("www." in l_lower):
                continue
            
            # Skip if has too many numbers (likely not a name)
            if len(re.findall(r'\d', l)) > 3:
                continue
            
            # Check for header format: "Name | Location" or "Name - Location"
            # Extract just the name part before pipe or dash
            if '|' in l:
                parts = l.split('|')
                candidate = parts[0].strip()
                # Validate the part before pipe looks like a name
                if 2 <= len(candidate.split()) <= 4 and len(candidate) >= 5:
                    # Check it's mostly letters and spaces
                    if re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}$', candidate):
                        info["name"] = candidate
                        break
            
            # Look for format: "FirstName LastName" at start (2-4 words, capitalized)
            # Must start with capital letter and be 2-4 words
            if re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}$', l):
                # Must be reasonable length (not too short, not too long)
                if 5 <= len(l) <= 50:
                    # Additional validation: should have at least first + last name
                    words = l.split()
                    if len(words) >= 2:
                        # Check words aren't section headers
                        if not any(w.lower() in section_headers for w in words):
                            info["name"] = l
                            break
            
            # Fallback: first line with 2-4 words, mostly alphabetic, proper length
            words = l.split()
            if 2 <= len(words) <= 4 and 5 <= len(l) <= 50:
                # Must be mostly alphabetic (at least 80% letters)
                letter_count = sum(c.isalpha() or c.isspace() for c in l)
                if letter_count / len(l) >= 0.8:
                    # Check first word is capitalized
                    if words[0][0].isupper():
                        # Not a section header
                        if l_lower not in section_headers:
                            info["name"] = l
                            break

    return info

# ---------- Helpers to detect originals for arbitrary categories ----------
def find_category_originals(text: str, category: str) -> List[str]:
    """Try to find original values for a user-provided category from raw text.
    - For 'university': capture common university name patterns.
    - Otherwise: return lines containing the category word (case-insensitive).
    Returns a list of candidate strings (deduped, order preserved, limited length).
    """
    if not text or not category:
        return []

    lines = [l.strip() for l in re.split(r"\r?\n", text) if l.strip()]
    out: List[str] = []
    cat = category.strip().lower()

    if cat == "university":
        # Look for common university name patterns
        uni_patterns = [
            r"(?i)\bUniversity of [A-Z][A-Za-z&.'\-]+(?: [A-Z][A-Za-z&.'\-]+)*\b.*",
            r"(?i)\b[A-Z][A-Za-z&.'\-]+(?: [A-Z][A-Za-z&.'\-]+)* University\b.*",
        ]
        for ln in lines:
            for pat in uni_patterns:
                if re.search(pat, ln):
                    out.append(ln)
                    break
    else:
        # Generic: any line that mentions the category word
        word_pat = re.compile(rf"(?i)\b{re.escape(cat)}\b")
        for ln in lines:
            if word_pat.search(ln):
                out.append(ln)

    # Deduplicate while preserving order
    seen = set()
    unique_out = []
    for s in out:
        if s not in seen:
            seen.add(s)
            unique_out.append(s)

    # Limit overly long captures
    return unique_out[:5]

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
BASE_FIELDS: Dict[str, Tuple[Type[Any], Any]] = {
    'key_strengths': (List[str], ...),
    'key_strengths_score': (float, Field(ge=1, le=5)),
    'key_strengths_explanation': (str, ...),

    'experience_score': (float, Field(ge=1, le=5)),
    'experience_explanation': (str, ...),

    'skills_match_score': (float, Field(ge=1, le=5)),
    'skills_match_explanation': (str, ...),

    'potential_concerns': (List[str], ...),
    'recommendation': (Literal["Recommended", "Consider", "Pass"], ...),

    'candidate_name': (str, ...),
    'job_title': (str, ...),
    'department': (str, ...),

    # Use model's overall score (no recomputing)
    'overall_score': (float, Field(ge=1, le=5)),
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
#   X (typed value), X_score: 1–5, X_explanation: str
def build_dynamic_model(custom_fields: list) -> Type[BaseModel]:
    fields = dict(BASE_FIELDS)
    # Only include supported types: string and boolean
    for f in [cf for cf in custom_fields if cf.get('type') in ('string', 'boolean')]:
        enum_vals = None
        fields[f['name']] = take_dynamic_input(f['type'], enum_vals)
        fields[f"{f['name']}_score"] = (Optional[float], Field(default=None, ge=1, le=5))
        fields[f"{f['name']}_explanation"] = (Optional[str], Field(default=None))
    Model = create_model('EvaluationModel', **fields)
    Model.model_config = {"extra": "forbid"}
    return Model

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
                'definition': "Score key_strengths (1–5) based on job requirements"
            },
            'experience': {
                'custom': False,
                'definition': "Score experience (1–5) covering both years of experience AND relevance to this specific role"
            },
            'skills_match': {
                'custom': False,
                'definition': "Score skills_match (1–5) for technical/functional skill alignment"
            }
        }

    with st.expander("Define Core Criteria"):
        st.info("You can customize what Key Strengths, Experience, and Skills Match mean for your evaluation, or use the default definitions.")
        
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
                placeholder="Example: Key strengths should prioritize leadership abilities, technical expertise, and communication skills",
                height=80,
                key="key_strengths_def"
            )
        else:
            key_strengths_def = "Score key_strengths (1–5) based on job requirements"
        
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
                placeholder="Example: Experience should emphasize industry-specific background and relevant project work",
                height=80,
                key="experience_def"
            )
        else:
            experience_def = "Score experience (1–5) covering both years of experience AND relevance to this specific role"
        
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
                placeholder="Example: Skills match should focus on technical proficiencies listed in job description",
                height=80,
                key="skills_match_def"
            )
        else:
            skills_match_def = "Score skills_match (1–5) for technical/functional skill alignment"
        
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
                "- If University is outside Texas, set <field>_score < 2 and explain why.\n"
                "- If publications ≥ 2, set <field>_score ≥ 4 with brief justification.\n"
            ),
            height=120
        )
    
        if st.button("Add Field"):
            if field_name and field_type in ["string", "boolean"]:
                new_field = {
                    'name': field_name,
                    'type': field_type,
                    'enum_vals': None,
                    'instruction': (instruction or "").strip() or
                                   "If relevant, set <field>_score (1–5) with one-sentence explanation referencing resume evidence."
                }
                st.session_state.custom_fields.append(new_field)
                st.success(f"Added field: {field_name}")
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
    st.caption("Add categories (e.g., name, email, phone, address, gender, age, dob, nationality, marital_status, pronouns, linkedin, github) you want redacted before AI evaluation. Use one at a time; remove to stop redacting.")

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
            # Core - REMOVED experience_relevance (duplicate)
            '"key_strengths": ["strength1", "strength2", "strength3"],',
            '"key_strengths_score": <number 1-5>,',
            '"key_strengths_explanation": "<why this score was given for key strengths>",',
            '"experience_score": <number 1-5>,',
            '"experience_explanation": "<why this score was given for experience and relevance to role>",',
            '"skills_match_score": <number 1-5>,',
            '"skills_match_explanation": "<short, concrete rationale>",',
            '"potential_concerns": ["concern1", "concern2"],',
            '"recommendation": "<exactly one of: Recommended, Consider, Pass>",',
            '"candidate_name": "<extract from resume or use \\"Candidate\\">",',
            f'"job_title": "{job_title}",',
            f'"department": "{department}",'
        ]
    
        # Only support string and boolean custom fields
        for f in [cf for cf in custom_fields if cf.get('type') in ('string','boolean')]:
            # value
            if f["type"] == "string":
                lines.append(f'"{f["name"]}": "<string>",')
            elif f["type"] == "boolean":
                lines.append(f'"{f["name"]}": <true|false>,')
            else:
                lines.append(f'"{f["name"]}": "<string>",')
            # score + explanation
            lines.append(f'"{f["name"]}_score": <number 1-5>,')
            lines.append(f'"{f["name"]}_explanation": "<short rationale tied to resume evidence>",')
    
        # Model provides overall score - no recomputing
        lines.append('"overall_score": <number 1-5>,')
        lines.append('"overall_explanation": "<1–2 sentences summarizing the key drivers from the subscores>",')
    
        lines.append('"custom_considerations": [')
        lines.append('  { "field": "<field name>", "instruction": "<the HR rule text>", "applied": <true|false>, "impact": "<what changed (e.g., university_score→1) and effect on overall>" }')
        lines.append(']')
    
        return "{\n" + "\n".join(lines) + "\n}"
    
    def build_eval_prompt(
        job_title: str,
        department: str,
        job_description: str,
        custom_fields: list,
        resume_text: str
    ) -> str:
        schema = schema_text(job_title, department, job_description, custom_fields)
        rules_payload = json.dumps(
            [{"field": f["name"], "instruction": f.get("instruction", "")} for f in custom_fields],
            ensure_ascii=False
        )
        
        # Get custom core criteria definitions if they exist, otherwise use defaults
        if 'core_criteria_defs' in st.session_state:
            key_strengths_def = st.session_state.core_criteria_defs['key_strengths']['definition']
            experience_def = st.session_state.core_criteria_defs['experience']['definition']
            skills_match_def = st.session_state.core_criteria_defs['skills_match']['definition']
        else:
            key_strengths_def = "Score key_strengths (1–5) based on job requirements"
            experience_def = "Score experience (1–5) covering both years of experience AND relevance to this specific role"
            skills_match_def = "Score skills_match (1–5) for technical/functional skill alignment"
    
        return f"""You are an expert hiring manager. Return STRICT JSON only—no prose/markdown/fences.
    
    SCORING SCALE (1-5): 5 Exceptional · 4 Strong · 3 Good · 2 Fair · 1 Poor
    
    REQUIRED JSON (exact keys/types):
    {schema}
    
    JOB:
    Title: {job_title}
    Department: {department}
    Description: {job_description}
    
    RESUME (verbatim evidence source):
    {resume_text}
    
    CATEGORY INSTRUCTIONS (authoritative; reflect ALL in custom_considerations):
    {rules_payload}
    
    EVALUATION RULES (follow ALL):
    1) {key_strengths_def}
    2) {experience_def}
    3) {skills_match_def}
    4) For EACH custom field, extract value AND provide score (1–5) AND explanation
    5) If instruction sets threshold/condition, set that field's score accordingly and note impact
    6) Calculate overall_score considering ALL individual scores (core + custom) and their relative importance
    7) If custom field has low score due to instruction, let it significantly impact overall_score
    8) overall_explanation should summarize key drivers from subscores
    9) Keep all text values concise and avoid special characters, newlines, or control characters
    10) Return ONLY the JSON object"""
    
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
                f"Explain briefly how to compute a 1–5 score and give one example using resume evidence."
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
                field_name = field['name']
                field_display = field_name.replace('_', ' ').title()
                custom_field_names.append(field_name)
                custom_value_headers.append(f"{field_display} Value")
                custom_score_headers.append(f"{field_display} Score")
                custom_explanation_headers.append(f"{field_display} Explanation")
        
        # Combine all headers
        # Place custom Values and Scores within the SCORES section
        all_score_headers = score_headers + custom_value_headers + custom_score_headers
        all_explanation_headers = explanation_headers + custom_explanation_headers
        all_headers = all_score_headers + all_explanation_headers + ["Custom Considerations", "Notes"]
        
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
        for i, header in enumerate(["Custom Considerations", "Notes"]):
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
        remaining_headers = ["Custom Considerations", "Notes"]
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
        
        # Step 6: Clean up spacing around structural elements
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
    
            with st.spinner("Analyzing resumes with Mistral..."):
                for resume_filename, file_object in all_resume_files:
                    resume_text = extract_text_from_pdf(file_object)
                    original_text = resume_text
                    # Extract personal info prior to anonymization (for mapping)
                    extracted = extract_personal_info(original_text)
                    # Apply anonymization before prompting
                    anonymized_text = anonymize_text(resume_text, st.session_state.get('anonymize_fields'))
                    resume_text = anonymized_text
                    prompt = build_eval_prompt(
                        job_title, department, job_description, st.session_state.custom_fields, resume_text
                    )
                    result = call_mistral(prompt)
    
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
    
                            # Validate with dynamic Pydantic model
                            evaluation = EvaluationModel(**data)

                            # If any field is anonymized, capture the mapping
                            anonymize_list = [c.strip().lower() for c in st.session_state.get('anonymize_fields', [])]
                            if anonymize_list:
                                # If name is anonymized, assign Candidate N
                                # Check if any field contains "name" (e.g., "name", "individual's name", "candidate name")
                                has_name = any('name' in field for field in anonymize_list)
                                if has_name:
                                    st.session_state.candidate_index += 1
                                    placeholder = f"Candidate {st.session_state.candidate_index}"
                                    # Override displayed candidate name
                                    try:
                                        evaluation.candidate_name = placeholder
                                    except Exception:
                                        pass
                                else:
                                    # If name not anonymized, use the actual candidate name as placeholder key
                                    placeholder = evaluation.candidate_name if hasattr(evaluation, 'candidate_name') else "Unknown"
                                
                                # Dynamically find what was redacted by comparing original vs anonymized text
                                redaction_map: Dict[str, Any] = {}
                                for cat in anonymize_list:
                                    candidates: List[str] = []
                                    
                                    # Map custom field names to standard extraction keys
                                    extraction_key = cat
                                    if 'name' in cat:
                                        extraction_key = 'name'
                                    elif 'address' in cat or 'location' in cat:
                                        extraction_key = 'address'
                                    elif 'email' in cat:
                                        extraction_key = 'email'
                                    elif 'phone' in cat:
                                        extraction_key = 'phone'
                                    
                                    # 1) Use extracted values if available
                                    if extraction_key in extracted and extracted[extraction_key]:
                                        candidates.append(str(extracted[extraction_key]))
                                    # 2) Look for label-style lines: "cat: value"
                                    label_pat = rf"(?im)^\s*{re.escape(cat)}\s*[:\-]\s*(.+)$"
                                    for m in re.finditer(label_pat, original_text):
                                        val = m.group(1).strip()
                                        if val:
                                            candidates.append(val)
                                    # 3) Use category heuristics (e.g., university) and generic line matches
                                    for s in find_category_originals(original_text, cat):
                                        candidates.append(s)

                                    # Dedup, then choose those that appear to be removed in anonymized_text
                                    seen_vals = set()
                                    kept_vals: List[str] = []
                                    for v in candidates:
                                        if v and v not in seen_vals:
                                            seen_vals.add(v)
                                            # Consider it redacted if it appears in original but not in anonymized
                                            if v in original_text and v not in anonymized_text:
                                                kept_vals.append(v)

                                    if kept_vals:
                                        # If multiple, join for display; store first as representative in JSON
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
                                st.metric("Key Strengths", f"{evaluation.key_strengths_score}/5")
                                st.caption(f"💭 {evaluation.key_strengths_explanation}")
    
                                st.metric("Experience", f"{evaluation.experience_score}/5")
                                st.caption(f"💭 {evaluation.experience_explanation}")
    
                                st.metric("Skills Match", f"{evaluation.skills_match_score}/5")
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
                                    fname = field['name']
                                    label = fname.replace('_', ' ').title()
                                    val = getattr(evaluation, fname, None)
                                    sval = getattr(evaluation, f"{fname}_score", None)
                                    expl = getattr(evaluation, f"{fname}_explanation", None)
    
                                    if sval is not None:
                                        st.metric(label, f"{sval}/5")
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
                                st.metric("Overall Score", f"{evaluation.overall_score}/5")
                            with cols[1]:
                                st.info(f"**Recommendation:** {evaluation.recommendation}")
                                st.caption(f"💭 {evaluation.overall_explanation}")
                                
                            # Store successful evaluation for Excel export
                            # Create a dictionary to store evaluation with metadata instead of modifying the model directly
                            eval_with_metadata = {
                                "evaluation": evaluation,
                                "custom_fields": st.session_state.custom_fields,
                                "resume_filename": resume_filename
                            }
                            st.session_state.evaluations.append(eval_with_metadata)
    
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
                    cover_text = anonymize_text(cover_text, st.session_state.get('anonymize_fields'))
                    
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