import streamlit as st
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf
from typing import List, Literal
from pydantic import BaseModel, Field

class Evaluation(BaseModel):
    overall_score: float = Field(ge=0, le=10)
    key_strengths: List[str]
    potential_concerns: List[str]
    skills_match: str
    experience_relevance: str
    recommendation: Literal["Hire", "Consider", "Pass"]
    candidate_name: str
    job_title: str
    department: str

    

st.set_page_config(page_title="Resume Recommender", layout="wide")
st.title("📄 Resume Recommender with Mistral AI")

# Job description inputs
job_title = st.text_input("Job Title")
department = st.selectbox("Department", ["Engineering", "Marketing", "Design", "Data", "Other"])
job_description = st.text_area("Job Description", height=200)

uploaded_files = st.file_uploader("Upload Resumes (PDF only)", type="pdf", accept_multiple_files=True)

def build_eval_prompt(job_title: str, department: str, job_description: str, resume_text: str) -> str:
    return f"""You are an expert hiring manager. Return your evaluation as **STRICT JSON** only — no prose, no markdown, no code fences, no explanatory text.

REQUIRED JSON Schema (keys and types MUST match exactly):
{{
  "overall_score": <number between 0-10>,
  "key_strengths": ["strength1", "strength2", "strength3"],
  "potential_concerns": ["concern1", "concern2"],
  "skills_match": "<detailed analysis of technical skills alignment>",
  "experience_relevance": "<analysis of work experience relevance>", 
  "recommendation": "<exactly one of: Hire, Consider, Pass>",
  "candidate_name": "<extract from resume or use 'Candidate'>",
  "job_title": "{job_title}",
  "department": "{department}"
}}

JOB REQUIREMENTS:
Title: {job_title}
Department: {department}  
Description: {job_description}

CANDIDATE RESUME:
{resume_text}

CRITICAL RULES:
1. Return ONLY the JSON object - no other text
2. Use double quotes for all strings
3. No trailing commas
4. overall_score must be a number (not string)
5. key_strengths must have 2-4 items
6. potential_concerns must have 1-3 items  
7. recommendation must be exactly "Hire", "Consider", or "Pass"
8. Be specific to THIS candidate - mention actual projects, skills, experience from their resume"""

if st.button("🔍 Recommend Candidates"):
    if not uploaded_files or not job_description:
        st.warning("Please enter the job description and upload at least one resume.")
    else:
        with st.spinner("Analyzing resumes with Mistral..."):
            for resume_file in uploaded_files:
                resume_text = extract_text_from_pdf(resume_file)
                prompt = build_eval_prompt(job_title, department, job_description, resume_text)
                result = call_mistral(prompt)
                
                st.markdown(f"### 📄 {resume_file.name}")
                if "choices" in result:
                    raw_text = result["choices"][0]["message"]["content"]
                    
                    try:
                        # Extract and display structured JSON
                        import json
                        import re
                        
                        json_pattern = r'\{[\s\S]*\}'
                        match = re.search(json_pattern, raw_text)
                        
                        if match:
                            json_str = match.group(0)
                            # Clean up common issues
                            cleaned = re.sub(r',(\s*[}\]])', r'\1', json_str.strip())
                            evaluation_data = json.loads(cleaned)
                            
                            # Validate with Pydantic
                            evaluation = Evaluation(**evaluation_data)
                            
                            # Display structured results
                            col1, col2 = st.columns([1, 2])
                            
                            with col1:
                                st.metric("Overall Score", f"{evaluation.overall_score}/10")
                                st.info(f"**Recommendation:** {evaluation.recommendation}")
                                st.write(f"**Candidate:** {evaluation.candidate_name}")
                            
                            with col2:
                                st.write("**💪 Key Strengths:**")
                                for strength in evaluation.key_strengths:
                                    st.write(f"• {strength}")
                                
                                st.write("**⚠️ Potential Concerns:**")
                                for concern in evaluation.potential_concerns:
                                    st.write(f"• {concern}")
                            
                            st.write("**🔧 Skills Match:**")
                            st.write(evaluation.skills_match)
                            
                            st.write("**📈 Experience Relevance:**")
                            st.write(evaluation.experience_relevance)
                            
                        else:
                            st.write("**Raw Response:**")
                            st.write(raw_text)
                            
                    except (json.JSONDecodeError, ValueError) as e:
                        st.error(f"❌ Failed to parse JSON: {str(e)}")
                        st.write("**Raw Response:**")
                        st.write(raw_text)
                else:
                    st.error("❌ Failed to get response from Mistral.")
