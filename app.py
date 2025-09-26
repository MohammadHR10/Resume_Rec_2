from typing import List, Dict, Any, Optional, Tuple, Type, Literal
from pydantic import BaseModel, Field, create_model, ValidationError
import streamlit as st
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf
import json, re

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

uploaded_files = st.file_uploader("Upload Resumes (PDF only)", type="pdf", accept_multiple_files=True)

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
    Handles control characters and malformed JSON.
    """
    # Find JSON object
    json_match = re.search(r'\{(?:[^{}]|{[^{}]*})*\}', raw_text, re.DOTALL)
    if not json_match:
        return None
    
    s = json_match.group(0).strip()
    
    # Step 1: Remove or escape control characters
    # Replace problematic control characters
    s = s.replace('\n', ' ')  # Replace newlines with spaces
    s = s.replace('\r', ' ')  # Replace carriage returns with spaces  
    s = s.replace('\t', ' ')  # Replace tabs with spaces
    s = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', s)  # Remove other control characters
    
    # Step 2: Fix unescaped quotes inside string values
    # This is tricky - we need to escape quotes that are inside string values
    # but not the quotes that define the strings
    def escape_quotes_in_strings(match):
        content = match.group(1)
        # Escape any unescaped quotes inside the string content
        content = content.replace('"', '\\"')
        return f'"{content}"'
    
    # Find string values and escape quotes inside them
    s = re.sub(r'"([^"]*(?:\\"[^"]*)*)"', lambda m: f'"{m.group(1)}"', s)
    
    # Step 3: Remove trailing commas before } or ]
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    
    # Step 4: Remove leading commas after { or [
    s = re.sub(r'([{\[])\s*,', r'\1', s)
    
    # Step 5: Fix multiple consecutive commas
    s = re.sub(r',\s*,+', ',', s)
    
    # Step 6: Remove commas at line start (after cleaning newlines)
    s = re.sub(r',\s+', ', ', s)  # Normalize comma spacing
    
    # Step 7: Fix missing quotes around field names
    s = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', s)
    
    # Step 8: Fix boolean values
    s = re.sub(r': *True\b', ': true', s)
    s = re.sub(r': *False\b', ': false', s)
    
    # Step 9: Ensure arrays are properly formatted
    s = re.sub(r'\[\s*([^"\[\]{}]+)\s*\]', lambda m: f'["{m.group(1).strip()}"]', s)
    
    # Step 10: Remove any remaining trailing comma before final }
    s = re.sub(r',(\s*}$)', r'\1', s)
    
    # Step 11: Compress multiple spaces
    s = re.sub(r'\s+', ' ', s)
    
    return s

# ---------- Run ----------
if st.button("🔍 Recommend Candidates"):
    if not uploaded_files or not job_description:
        st.warning("Please enter the job description and upload at least one resume.")
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
            for resume_file in uploaded_files:
                resume_text = extract_text_from_pdf(resume_file)
                prompt = build_eval_prompt(
                    job_title, department, job_description, st.session_state.custom_fields, resume_text
                )
                result = call_mistral(prompt)

                st.markdown(f"### 📄 {resume_file.name}")
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
                                # Strategy 1: Remove everything before first { and after last }
                                start = raw_text.find('{')
                                end = raw_text.rfind('}') + 1
                                if start != -1 and end > start:
                                    simple_json = raw_text[start:end]
                                    # More aggressive cleanup
                                    simple_json = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', simple_json)
                                    simple_json = simple_json.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
                                    simple_json = re.sub(r',(\s*[}\]])', r'\1', simple_json)
                                    simple_json = re.sub(r'\s+', ' ', simple_json)
                                    
                                    data = json.loads(simple_json)
                                    st.info("✅ Fallback parsing succeeded!")
                                else:
                                    continue
                            except json.JSONDecodeError:
                                # Strategy 2: Try to manually fix common issues
                                try:
                                    # Replace smart quotes and other problematic characters
                                    fixed_json = raw_text.replace('"', '"').replace('"', '"')
                                    fixed_json = fixed_json.replace(''', "'").replace(''', "'")
                                    
                                    start = fixed_json.find('{')
                                    end = fixed_json.rfind('}') + 1
                                    if start != -1 and end > start:
                                        fixed_json = fixed_json[start:end]
                                        fixed_json = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', fixed_json)
                                        fixed_json = re.sub(r',(\s*[}\]])', r'\1', fixed_json)
                                        
                                        data = json.loads(fixed_json)
                                        st.info("✅ Smart quote fix succeeded!")
                                    else:
                                        continue
                                except:
                                    st.error("❌ All parsing strategies failed. Please check the raw response above.")
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