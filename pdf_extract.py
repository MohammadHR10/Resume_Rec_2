from pdfminer.high_level import extract_text
import io

def extract_text_from_pdf(uploaded_file):
    # Convert Streamlit uploaded file to bytes
    pdf_bytes = uploaded_file.read()
    
    # Create a BytesIO object for pdfminer
    pdf_file = io.BytesIO(pdf_bytes)
    
    # Extract text
    text = extract_text(pdf_file)
    
    # Debug output
    print(f"Extracted text length: {len(text)}")
    print(f"First 200 characters: {text[:200]}")
    
    return text