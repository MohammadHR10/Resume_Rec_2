from pdfminer.high_level import extract_text
from pdfminer.pdfparser import PDFSyntaxError
import io
import logging

# Import fallback libraries
try:
    import PyPDF2
    PYPDF2_AVAILABLE = True
except ImportError:
    PYPDF2_AVAILABLE = False

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def extract_text_from_pdf(uploaded_file):
    """
    Robust PDF text extraction with multiple fallback methods:
    1. pdfminer.six (best for most text PDFs)
    2. pdfplumber (handles more complex layouts and tables)
    3. PyPDF2 (most forgiving for problematic PDFs)
    
    Returns extracted text or empty string if all methods fail.
    Does NOT modify any data - just extracts text.
    """
    # Read bytes from uploaded file
    try:
        pdf_bytes = uploaded_file.read()
    except Exception as e:
        logger.error(f"Failed to read uploaded file: {e}")
        return ""
    
    if not pdf_bytes:
        logger.warning("Empty file uploaded")
        return ""
    
    # Validate PDF header
    if not pdf_bytes[:8].startswith(b"%PDF-"):
        logger.warning("File does not start with %PDF- header - may not be a valid PDF")
        # Continue anyway - sometimes header is offset
    
    # Method 1: Try pdfminer.six first (original method)
    try:
        pdf_file = io.BytesIO(pdf_bytes)
        text = extract_text(pdf_file)
        if text and text.strip():
            logger.info(f"✓ pdfminer extracted text (length: {len(text)})")
            print(f"Extracted text length: {len(text)}")
            print(f"First 200 characters: {text[:200]}")
            return text
        else:
            logger.info("pdfminer returned empty text, trying fallback methods")
    except PDFSyntaxError as e:
        logger.warning(f"pdfminer PDFSyntaxError: {e} - trying fallback methods")
    except Exception as e:
        logger.warning(f"pdfminer failed: {e} - trying fallback methods")
    
    # Method 2: Try pdfplumber (better for complex layouts)
    if PDFPLUMBER_AVAILABLE:
        try:
            pdf_file = io.BytesIO(pdf_bytes)
            with pdfplumber.open(pdf_file) as pdf:
                text_parts = []
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                
                if text_parts:
                    text = "\n".join(text_parts)
                    logger.info(f"✓ pdfplumber extracted text (length: {len(text)})")
                    print(f"Extracted text length: {len(text)}")
                    print(f"First 200 characters: {text[:200]}")
                    return text
                else:
                    logger.info("pdfplumber returned empty text, trying PyPDF2")
        except Exception as e:
            logger.warning(f"pdfplumber failed: {e} - trying PyPDF2")
    
    # Method 3: Try PyPDF2 (most forgiving)
    if PYPDF2_AVAILABLE:
        try:
            pdf_file = io.BytesIO(pdf_bytes)
            reader = PyPDF2.PdfReader(pdf_file)
            text_parts = []
            
            for page_num, page in enumerate(reader.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                except Exception as e:
                    logger.warning(f"PyPDF2 failed on page {page_num}: {e}")
                    continue
            
            if text_parts:
                text = "\n".join(text_parts)
                logger.info(f"✓ PyPDF2 extracted text (length: {len(text)})")
                print(f"Extracted text length: {len(text)}")
                print(f"First 200 characters: {text[:200]}")
                return text
            else:
                logger.warning("PyPDF2 returned empty text")
        except Exception as e:
            logger.error(f"PyPDF2 failed: {e}")
    
    # All methods failed
    logger.error("All PDF extraction methods failed - file may be corrupted, encrypted, or image-only (scanned)")
    return ""