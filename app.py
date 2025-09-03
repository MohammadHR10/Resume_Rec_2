import streamlit as st
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf
from typing import List, Literal, Any, Dict, Optional, Tuple, Type
from pydantic import BaseModel, Field, create_model, ValidationError
import json, re

# ---------- Base fields ----------
class Consideration(BaseModel):
    field: str
    instruction: str
    applied: bool
    impact: str

BASE_FIELDS: Dict[str, Tuple[Type[Any], Any]] = {
    'overall_score': (float, Field(ge=0, le=10)),
    'key_strengths': (List[str], ...),
    'potential_concerns': (List[str], ...),
    'skills_match': (str, ...),
    'experience_relevance': (str, ...),
    'recommendation': (Literal["Hire", "Consider", "Pass"], ...),
    'candidate_name': (str, ...),
    'job_title': (str, ...),
    'department': (str, ...),
    # LLM must explain how each dynamic instruction affected the result
    'custom_considerations': (List[Consideration], ...),
}

def take_dynamic_input(t: str, enum_vals: Optional[list] = None) -> Tuple[Type[Any], Any]:
    if t == "enum":
        if enum_vals:
            return (Literal[tuple(enum_vals)], ...)  # type: ignore
        return (str, ...)
    mapping = {"string": str, "integer": int, "float": float, "boolean": bool}
    return (mapping.get(t, str), ...)

def build_dynamic_model(custom_fields: list) -> Type[BaseModel]:
    fields = dict(BASE_FIELDS)  # Start with base fields
    for f in custom_fields:
        enum_vals = f.get('enum_vals') if f['type'] == 'enum' else None
        fields[f['name']] = take_dynamic_input(f['type'], enum_vals)
    Model = create_model('EvaluationModel', **fields)
    Model.model_config = {"extra": "forbid"}  # Forbid extra fields
    return Model

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

    # Free-form instruction (this is the dynamic directive you care about)
    instruction = st.text_area(
        "Instruction for how to use this category in evaluation",
        placeholder=(
            "Examples:\n"
            "- If anything is provided here and it's relevant to the JD, include it in the evaluation.\n"
            #"- Prefer candidates who previously worked with our organization.\n"
            #"- If this field is present and strong, bump overall assessment; otherwise ignore."
        ),
        height=120
    )

    if st.button("Add Field"):
        if field_name:
            new_field = {
                'name': field_name,
                'type': field_type,
                'enum_vals': enum_values if field_type == 'enum' else None,
                'instruction': instruction.strip() or "If relevant to the job description, include in evaluation consideration."
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
        '"overall_score": <number between 0-10>,',
        '"key_strengths": ["strength1", "strength2", "strength3"],',
        '"potential_concerns": ["concern1", "concern2"],',
        '"skills_match": "<detailed analysis of technical skills alignment>",',
        '"experience_relevance": "<analysis of work experience relevance>",',
        '"recommendation": "<exactly one of: Hire, Consider, Pass>",',
        '"candidate_name": "<extract from resume or use \\"Candidate\\">",',
        f'"job_title": "{job_title}",',
        f'"department": "{department}"'
    ]

    for f in custom_fields:
        if f["type"] == "string":
            lines.append(f'"{f["name"]}": "<string>"')
        elif f["type"] == "integer":
            lines.append(f'"{f["name"]}": <integer>')
        elif f["type"] == "float":
            lines.append(f'"{f["name"]}": <number>')
        elif f["type"] == "boolean":
            lines.append(f'"{f["name"]}": <true|false>')
        elif f["type"] == "enum":
            opts = ", ".join([f'"{v}"' for v in (f.get("enum_vals") or [])]) or '"<string>"'
            lines.append(f'"{f["name"]}": <one of: {opts}>')
        else:
            lines.append(f'"{f["name"]}": "<string>"')

    # Demand an item per instruction with echo + applied flag + impact
    lines.append('"custom_considerations": [')
    lines.append('  { "field": "<field name>", "instruction": "<the HR rule text>", "applied": <true|false>, "impact": "<how it changed the evaluation>" }')
    lines.append(']')

    return "{\n" + ",\n".join(lines) + "\n}"

def build_eval_prompt(
    job_title: str,
    department: str,
    job_description: str,     
    resume_text: str,
    custom_fields, # <-- list of dicts from session_state
) -> str:
    schema = schema_text(job_title, department, job_description, custom_fields)

    rules_payload = json.dumps(
        [{"field": f["name"], "instruction": f.get("instruction","")} for f in st.session_state.custom_fields],
        ensure_ascii=False
    )

    return f"""You are an expert hiring manager. Return your evaluation as STRICT JSON only — no prose, no markdown, no code fences.

REQUIRED JSON Schema (keys and types MUST match exactly):
{schema}

JOB REQUIREMENTS:
Title: {job_title}
Department: {department}
Description: {job_description}

CANDIDATE RESUME:
{resume_text}

CATEGORY INSTRUCTIONS (authoritative; include ALL in custom_considerations):
{rules_payload}

EVALUATION INSTRUCTIONS:
1. Analyze the candidate vs the job.
2. Follow every CATEGORY INSTRUCTION; for each one emit an item in custom_considerations with field, instruction, applied (true/false), and impact.
3. If specific scoring guidance is given, follow it.
4. Output only the JSON object.
"""


# ---------- Run ----------
if st.button("🔍 Recommend Candidates"):
    if not uploaded_files or not job_description:
        st.warning("Please enter the job description and upload at least one resume.")
    else:
        EvaluationModel = build_dynamic_model(st.session_state.custom_fields)

        with st.spinner("Analyzing resumes with Mistral..."):
            for resume_file in uploaded_files:
                resume_text = extract_text_from_pdf(resume_file)
                prompt = build_eval_prompt(job_title, department, job_description, resume_text, st.session_state.custom_fields)
                result = call_mistral(prompt)

                st.markdown(f"### 📄 {resume_file.name}")
                if "choices" in result:
                    raw_text = result["choices"][0]["message"]["content"]
                    try:
                        json_pattern = r'\{[\s\S]*\}'
                        match = re.search(json_pattern, raw_text)
                        if match:
                            json_str = match.group(0)
                            cleaned = re.sub(r',(\s*[}\]])', r'\1', json_str.strip())
                            evaluation_data = json.loads(cleaned)

                            # Validate with dynamic Pydantic model
                            evaluation = EvaluationModel(**evaluation_data)

                            # ------- UI -------
                            col1, col2 = st.columns([1, 2])
                            with col1:
                                st.metric("Overall Score", f"{evaluation.overall_score}/10")
                                st.info(f"**Recommendation:** {evaluation.recommendation}")
                                st.write(f"**Candidate:** {evaluation.candidate_name}")
                                st.caption(f"Role: {evaluation.job_title} · Dept: {evaluation.department}")
                            with col2:
                                st.write("**💪 Key Strengths**")
                                for s in evaluation.key_strengths:
                                    st.write(f"• {s}")
                                st.write("**⚠️ Potential Concerns**")
                                for c in evaluation.potential_concerns:
                                    st.write(f"• {c}")

                            st.write("**🔧 Skills Match**")
                            st.write(evaluation.skills_match)
                            st.write("**📈 Experience Relevance**")
                            st.write(evaluation.experience_relevance)

                            # Custom fields (values)
                            if st.session_state.custom_fields:
                                st.write("**📊 Custom Fields (values)**")
                                for field in st.session_state.custom_fields:
                                    val = getattr(evaluation, field['name'], None)
                                    st.write(f"**{field['name'].replace('_',' ').title()}:** {val}")

                            # How instructions were applied
                            if getattr(evaluation, "custom_considerations", None):
                                st.write("**🧠 How Your Instructions Were Applied**")
                                for item in evaluation.custom_considerations:
                                    st.write(
                                        f"- **{item.field}** → "
                                        f"{'APPLIED' if item.applied else 'NOT APPLIED'} | "
                                        f"_Instruction_: {item.instruction} | "
                                        f"_Impact_: {item.impact}"
                                    )
                        else:
                            st.write("**Raw Response:**")
                            st.write(raw_text)

                    except (json.JSONDecodeError, ValueError, ValidationError) as e:
                        st.error(f"❌ Failed to parse/validate JSON: {str(e)}")
                        st.write("**Raw Response:**")
                        st.write(raw_text)
                    except Exception as e:
                        st.error(f"❌ Unexpected error: {str(e)}")
                        st.write("**Raw Response:**")
                        st.write(raw_text)
                else:
                    st.error("❌ Failed to get response from Mistral.")
