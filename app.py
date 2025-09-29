from typing import List, Dict, Any, Optional, Tuple, Type, Literal
from pydantic import BaseModel, Field, create_model, ValidationError
import streamlit as st
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf
import json, re, zipfile, io
from pathlib import Path

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
    'recommendation': (Literal["Hire", "Consider", "Pass"], ...),

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
    if t == "enum":
        if enum_vals:
            return (Literal[tuple(enum_vals)], ...)  # type: ignore
        return (str, ...)
    mapping = {"string": str, "integer": int, "float": float, "boolean": bool}
    return (mapping.get(t, str), ...)

# For each custom field X, add:
#   X (typed value), X_score: 1–5, X_explanation: str
def build_dynamic_model(custom_fields: list) -> Type[BaseModel]:
    fields = dict(BASE_FIELDS)
    for f in custom_fields:
        enum_vals = f.get('enum_vals') if f['type'] == 'enum' else None
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
                
                # Extract the PDF content
                pdf_content = zip_ref.read(file_info.filename)
                
                # Get just the filename without path
                filename = Path(file_info.filename).name
                
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
st.set_page_config(page_title="Resume Recommender", layout="wide")
st.title("📄 Resume Recommender with Mistral AI")

# ---------- JD inputs ----------
job_title = st.text_input("Job Title")
department = st.selectbox("Department", ["Engineering", "Marketing", "Design", "Data", "Other"])
job_description = st.text_area("Job Description", height=200)

# ---------- Custom fields ----------
st.subheader("Custom Evaluation Fields")
if 'custom_fields' not in st.session_state:
    st.session_state.custom_fields = []

with st.expander("Add Custom Field"):
    field_name = st.text_input("Field Name")
    field_type = st.selectbox("Field Type", ["string", "integer", "float", "boolean", "enum"])
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
        if field_name:
            new_field = {
                'name': field_name,
                'type': field_type,
                'enum_vals': enum_values if field_type == 'enum' else None,
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
        '"recommendation": "<exactly one of: Hire, Consider, Pass>",',
        '"candidate_name": "<extract from resume or use \\"Candidate\\">",',
        f'"job_title": "{job_title}",',
        f'"department": "{department}",'
    ]

    for f in custom_fields:
        # value
        if f["type"] == "string":
            lines.append(f'"{f["name"]}": "<string>",')
        elif f["type"] == "integer":
            lines.append(f'"{f["name"]}": <integer>,')
        elif f["type"] == "float":
            lines.append(f'"{f["name"]}": <number>,')
        elif f["type"] == "boolean":
            lines.append(f'"{f["name"]}": <true|false>,')
        elif f["type"] == "enum":
            opts = ", ".join([f'"{v}"' for v in (f.get("enum_vals") or [])]) or '"<string>"'
            lines.append(f'"{f["name"]}": <one of: {opts}>,')
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
1) Score key_strengths (1–5) based on job requirements
2) Score experience (1–5) covering both years of experience AND relevance to this specific role
3) Score skills_match (1–5) for technical/functional skill alignment
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
    # Process files first to check if we have any
    all_resume_files = process_uploaded_files(uploaded_files, uploaded_zip)
    
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

        with st.spinner("Analyzing resumes with Mistral..."):
            for resume_filename, file_object in all_resume_files:
                resume_text = extract_text_from_pdf(file_object)
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