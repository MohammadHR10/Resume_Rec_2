"""
Shared models and utilities for both FastAPI (main.py) and Streamlit (app.py).
This module contains NO Streamlit dependencies to avoid import issues.
"""
from typing import List, Dict, Any, Optional, Tuple, Type, Literal, Union
from pydantic import BaseModel, Field, create_model
import json


# ---------- Base Pydantic Models ----------
class Consideration(BaseModel):
    field: str
    instruction: str
    applied: bool
    impact: str


class Evaluation(BaseModel):
    """Static evaluation model for the FastAPI endpoint."""
    key_strengths: List[str]
    key_strengths_score: Union[str, int, float]
    key_strengths_explanation: str
    
    experience_score: Union[str, int, float]
    experience_explanation: str
    
    skills_match_score: Union[str, int, float]
    skills_match_explanation: str
    
    potential_concerns: List[str]
    recommendation: Literal["Recommended", "Consider", "Pass"]
    
    candidate_name: str
    job_title: str
    department: str
    
    overall_score: Union[str, int, float]
    overall_explanation: str
    
    custom_considerations: List[Consideration]
    
    class Config:
        extra = "ignore"


# ---------- Prompt Builder (Streamlit-free version) ----------
def build_eval_prompt(
    job_title: str,
    department: str,
    job_description: str,
    custom_considerations: str,
    resume_text: str
) -> str:
    """Build evaluation prompt for the API endpoint (simplified version without custom fields)."""
    
    schema = """{
"key_strengths": ["strength1", "strength2", "strength3"],
"key_strengths_score": "<score value: e.g. 3, Medium, Green, 85%, B>",
"key_strengths_explanation": "<why this score was given>",
"experience_score": "<score value: e.g. 4, High, Red, 70%, A>",
"experience_explanation": "<why this score was given>",
"skills_match_score": "<score value: e.g. 2, Low, Yellow, 60%, C>",
"skills_match_explanation": "<short, concrete rationale>",
"potential_concerns": ["concern1", "concern2"],
"recommendation": "<exactly one of: Recommended, Consider, Pass>",
"candidate_name": "<extract from resume or use 'Candidate'>",
"job_title": "%s",
"department": "%s",
"overall_score": "<score value>",
"overall_explanation": "<1-2 sentences summarizing evaluation>",
"custom_considerations": [
  { "field": "<field name>", "instruction": "<rule text>", "applied": <true|false>, "impact": "<effect>" }
]
}""" % (job_title, department)

    return f"""You are an expert hiring manager. Return STRICT JSON only—no prose/markdown/fences.

CRITICAL FORMATTING RULE: 
- For ALL score fields, output ONLY the raw score value itself
- NEVER add "/5", "out of 5", "/3", or any suffix after scores
- Examples of CORRECT outputs: 3, Poor, Red, 85%, B
- Examples of INCORRECT outputs: 3/5, Poor/5, Red/5, 85%/5, B/5

REQUIRED JSON (exact keys/types):
{schema}

JOB:
Title: {job_title}
Department: {department}
Description: {job_description}

RESUME (verbatim evidence source):
{resume_text}

CUSTOM CONSIDERATIONS:
{custom_considerations}

EVALUATION RULES:
1) Evaluate key_strengths based on job requirements. Score from 1-5.
2) Evaluate experience covering both years AND relevance to this role. Score from 1-5.
3) Evaluate skills_match for technical/functional skill alignment. Score from 1-5.
4) Calculate overall_score considering ALL individual scores.
5) Be specific and cite evidence from the resume.

Return ONLY the JSON object, no other text."""
