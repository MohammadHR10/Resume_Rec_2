from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field
import streamlit as st
from mistral_client import call_mistral
from pdf_extract import extract_text_from_pdf
import json, re, zipfile, io, datetime, base64
from pathlib import Path
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

class CoverLetterAnalysis(BaseModel):
    """Model for cover letter AI detection results"""
    applicant_name: str = Field(description="Name of the applicant")
    file_name: str = Field(description="Name of the cover letter file")
    ai_generated_probability: float = Field(ge=0, le=100, description="Probability that the cover letter is AI-generated (0-100%)")
    classification: str = Field(description="AI-Generated or Human-Written")
    confidence_level: str = Field(description="High, Medium, or Low confidence")
    key_indicators: List[str] = Field(description="Key indicators that led to this classification")

def analyze_cover_letter_with_ai(cover_letter_text: str) -> dict:
    """
    Enhanced AI detection for cover letters using research-backed detection patterns.
    """
    prompt = f"""You are an expert AI content detector. Analyze this cover letter using advanced detection criteria to determine if it's AI-generated or human-written.

Cover Letter:
{cover_letter_text}

CRITICAL AI DETECTION SIGNALS (evaluate each carefully):

1. **Repetitive Phrase Patterns**: AI often repeats sentence structures and transitions
   - Excessive use of: "Furthermore," "Moreover," "Additionally," "In addition"
   - Parallel sentence construction across paragraphs
   - Recycled transition phrases

2. **Unnatural Perfection**: AI text is flawless but lacks human imperfections
   - Zero typos or informal elements despite length
   - Consistently formal tone without personality variation
   - No colloquialisms or conversational elements

3. **Generic Vocabulary & Clichés**: AI uses safe, common corporate expressions
   - Overuse of: "leverage," "passionate about," "proven track record," "excited opportunity"
   - Lack of specific, unique vocabulary
   - Corporate jargon without substance

4. **Vague Specificity**: AI provides detail that sounds specific but is actually generic
   - Numbers/metrics without context ("increased efficiency by 20%")
   - Generic achievements that could apply to anyone
   - Lack of truly personal anecdotes or unique experiences

5. **Uniform Paragraph Structure**: AI maintains consistent formatting too perfectly
   - All paragraphs similar length (100-150 words)
   - Predictable: intro → body 1 → body 2 → closing pattern
   - No natural flow variation

6. **Keyword Stuffing**: AI over-optimizes for job posting keywords
   - Unnatural repetition of job title/company name
   - Forced inclusion of every job requirement mentioned
   - Reads like a checklist response

7. **Emotional Flatness**: AI lacks genuine emotional resonance
   - Generic enthusiasm without authentic passion indicators
   - No vulnerable moments or authentic struggles mentioned
   - Overly confident without humility

HUMAN INDICATORS (signals of authenticity):
- Minor grammatical quirks or informal language
- Specific, verifiable stories with context
- Personality and unique voice
- Natural flow variations and rhythm changes
- Genuine vulnerability or honest self-reflection
- Unexpected word choices or creative phrasing

Return ONLY this JSON format with NO additional text:
{{
    "applicant_name": "Name from letter",
    "file_name": "Set separately",
    "ai_generated_probability": 75.5,
    "classification": "AI-Generated" or "Human-Written",
    "confidence_level": "High" or "Medium" or "Low",
    "key_indicators": [
        "Specific indicator 1 with evidence from text",
        "Specific indicator 2 with evidence from text",
        "Specific indicator 3 with evidence from text"
    ]
}}

Be thorough and precise. Weight AI signals heavily but acknowledge genuine human writing when present.
    """
    
    try:
        result = call_mistral(prompt)
        if isinstance(result, dict) and "choices" in result:
            return result["choices"][0]["message"]["content"]
        return None
    except Exception as e:
        st.error(f"Error analyzing cover letter: {str(e)}")
        return None

def extract_pdfs_from_zip_cover_letters(zip_file) -> List[Tuple[str, bytes]]:
    """
    Extract PDF files from a ZIP archive for cover letters.
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

def process_cover_letter_files(uploaded_files, uploaded_zip):
    """
    Process uploaded cover letter files and return a list of (filename, file_content) tuples
    """
    all_files = []
    
    # Process individual files
    if uploaded_files:
        for uploaded_file in uploaded_files:
            if uploaded_file.type == "application/pdf":
                all_files.append((uploaded_file.name, uploaded_file.getvalue()))
            else:
                st.warning(f"⚠️ Skipping {uploaded_file.name} - only PDF files are supported")
    
    # Process ZIP file
    if uploaded_zip:
        zip_files = extract_pdfs_from_zip_cover_letters(uploaded_zip)
        all_files.extend(zip_files)
    
    return all_files

def create_cover_letter_excel_report(analyses: List[CoverLetterAnalysis]) -> io.BytesIO:
    """
    Create an Excel report with cover letter analysis results
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Cover Letter Analysis"
    
    # Define styles
    header_fill = PatternFill(start_color="2E4057", end_color="2E4057", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=12)
    ai_fill = PatternFill(start_color="FFCCCB", end_color="FFCCCB", fill_type="solid")  # Light red for AI
    human_fill = PatternFill(start_color="D4EDDA", end_color="D4EDDA", fill_type="solid")  # Light green for Human
    border = Border(
        left=Side(style='thin'), 
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )
    
    # Headers
    headers = [
        "Applicant Name", "File Name", "Classification", "AI Probability (%)", 
        "Confidence Level", "Key Indicators"
    ]
    
    # Apply header styles
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
    
    # Add data
    for row_num, analysis in enumerate(analyses, 2):
        ws.cell(row=row_num, column=1, value=analysis.applicant_name).border = border
        ws.cell(row=row_num, column=2, value=analysis.file_name).border = border
        
        # Classification cell with color coding
        class_cell = ws.cell(row=row_num, column=3, value=analysis.classification)
        class_cell.border = border
        if analysis.classification == "AI-Generated":
            class_cell.fill = ai_fill
        else:
            class_cell.fill = human_fill
        
        ws.cell(row=row_num, column=4, value=analysis.ai_generated_probability).border = border
        ws.cell(row=row_num, column=5, value=analysis.confidence_level).border = border
        ws.cell(row=row_num, column=6, value=", ".join(analysis.key_indicators)).border = border
    
    # Auto-adjust column widths
    def get_column_letter_simple(col_idx):
        """Simple function to convert column index to letter"""
        result = ""
        while col_idx > 0:
            col_idx -= 1
            result = chr(col_idx % 26 + ord('A')) + result
            col_idx //= 26
        return result
    
    for col_idx in range(1, len(headers) + 1):
        max_length = 0
        column_letter = get_column_letter_simple(col_idx)
        
        # Check header length
        header_length = len(str(headers[col_idx - 1]))
        max_length = max(max_length, header_length)
        
        # Check data length
        for row_idx in range(2, ws.max_row + 1):
            try:
                cell = ws.cell(row=row_idx, column=col_idx)
                if cell.value:
                    cell_length = len(str(cell.value))
                    if cell_length > max_length:
                        max_length = cell_length
            except:
                continue
        
        # Set width
        if max_length < 15:
            adjusted_width = 15
        elif max_length > 50:
            adjusted_width = 50
        else:
            adjusted_width = max_length + 5
        
        ws.column_dimensions[column_letter].width = adjusted_width
    
    # Set text wrapping for all cells
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical='top')
    
    # Set row height for header
    ws.row_dimensions[1].height = 30
    
    # Save to BytesIO
    excel_file = io.BytesIO()
    wb.save(excel_file)
    excel_file.seek(0)
    return excel_file

def clean_json_output_cover_letter(raw_text: str) -> Optional[str]:
    """
    Clean and extract JSON from AI response for cover letter analysis
    """
    # Try to find JSON object
    json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if not json_match:
        return None
    
    s = json_match.group(0).strip()
    
    # Basic cleaning
    s = s.replace('"', '"').replace('"', '"')  # Smart quotes
    s = s.replace(''', "'").replace(''', "'")  # Smart apostrophes
    s = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', s)  # Control chars
    s = re.sub(r',(\s*[}\]])', r'\1', s)  # Trailing commas
    s = re.sub(r'([{\[])\s*,', r'\1', s)  # Leading commas
    
    return s

def get_download_link_cover_letter(excel_file: io.BytesIO, filename: str) -> str:
    """
    Generate download link for cover letter Excel report
    """
    b64 = base64.b64encode(excel_file.getvalue()).decode()
    href = f'<a href="data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{b64}" download="{filename}">📥 Download Cover Letter Analysis Report</a>'
    return href