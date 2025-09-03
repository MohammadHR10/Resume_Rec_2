from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf
from app import Evaluation, build_eval_prompt
import json
import re

app = FastAPI(title="Resume Recommender API")

def extract_json_from_response(text: str) -> dict:
    """Extract JSON from AI response"""
    json_pattern = r'\{[\s\S]*\}'
    match = re.search(json_pattern, text)
    
    if match:
        json_str = match.group(0)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # Clean up common issues
            cleaned = re.sub(r',(\s*[}\]])', r'\1', json_str.strip())
            return json.loads(cleaned)
    else:
        raise ValueError("No JSON found in response")

@app.post("/recommend", response_model=Evaluation)
async def recommend_resume(
    job_title: str = Form(...),
    department: str = Form(...),
    job_description: str = Form(...),
    custom_considerations: str = Form(...),
    resume_file: UploadFile = File(...)
):
    try:
        resume_text = extract_text_from_pdf(resume_file.file)
        prompt = build_eval_prompt(job_title, department, job_description, custom_considerations, resume_text)
        result = call_mistral(prompt)
        
        if "choices" not in result or not result["choices"]:
            raise HTTPException(status_code=502, detail="No response from Mistral AI")
            
        raw_text = result["choices"][0]["message"]["content"]
        json_data = extract_json_from_response(raw_text)
        evaluation = Evaluation(**json_data)
        
        # Ensure job fields are set
        evaluation.job_title = job_title
        evaluation.department = department
        evaluation.custom_considerations = custom_considerations
        
        return evaluation
        
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=422, detail=f"JSON parsing failed: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
