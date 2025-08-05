from fastapi import FastAPI, File, UploadFile, Form
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf

app = FastAPI(title="Resume Recommender API")

@app.post("/recommend")
async def recommend_resume(
    job_title: str = Form(...),
    department: str = Form(...),
    job_description: str = Form(...),
    resume_file: UploadFile = File(...)
):
    try:
        resume_text = extract_text_from_pdf(resume_file.file)

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
        if "choices" in result:
            return {"recommendation": result["choices"][0]["message"]["content"]}
        else:
            return {"error": "No valid response from Mistral", "details": result}

    except Exception as e:
        return {"error": str(e)}
