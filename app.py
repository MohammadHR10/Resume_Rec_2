import streamlit as st
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf

st.set_page_config(page_title="Resume Recommender", layout="wide")
st.title("📄 Resume Recommender with Mistral AI")

# Job description inputs
job_title = st.text_input("Job Title")
department = st.selectbox("Department", ["Engineering", "Marketing", "Design", "Data", "Other"])
job_description = st.text_area("Job Description", height=200)

uploaded_files = st.file_uploader("Upload Resumes (PDF only)", type="pdf", accept_multiple_files=True)

if st.button("🔍 Recommend Candidates"):
    if not uploaded_files or not job_description:
        st.warning("Please enter the job description and upload at least one resume.")
    else:
        with st.spinner("Analyzing resumes with Mistral..."):
            for resume_file in uploaded_files:
                resume_text = extract_text_from_pdf(resume_file)
                
                prompt = f"""
You are an expert hiring manager evaluating candidates for this specific role.

JOB REQUIREMENTS:
Title: {job_title}
Department: {department}
Description: {job_description}

CANDIDATE RESUME:
{resume_text}

Please provide a comprehensive evaluation with:

1. **Overall Score: X/10** 
2. **Key Strengths:** (3-4 specific points from their resume)
3. **Potential Concerns:** (2-3 areas where they might not be perfect fit)
4. **Specific Skills Match:** How their technical skills align with job requirements
5. **Experience Relevance:** How their work experience relates to this role
6. **Recommendation:** Hire/Consider/Pass and why

Be specific to THIS candidate - mention their name, specific projects, and actual experience. Avoid generic responses.
"""

                result = call_mistral(prompt)
                
                st.markdown(f"### 📄 {resume_file.name}")
                if "choices" in result:
                    st.write(result["choices"][0]["message"]["content"])
                else:
                    st.error("❌ Failed to get response from Mistral.")
