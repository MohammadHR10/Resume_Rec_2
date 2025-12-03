"""
Test script to see what text looks like when extracted from PDF
"""
from pdf_extract import extract_text_from_pdf
import sys

def test_pdf_extraction(pdf_path):
    """
    Extract and display text from a PDF file to see the raw format
    """
    print("=" * 80)
    print(f"EXTRACTING TEXT FROM: {pdf_path}")
    print("=" * 80)
    print()
    
    try:
        # Extract text from PDF
        with open(pdf_path, 'rb') as f:
            extracted_text = extract_text_from_pdf(f)
        
        print("RAW EXTRACTED TEXT:")
        print("-" * 80)
        print(extracted_text)
        print("-" * 80)
        print()
        
        print("TEXT STATISTICS:")
        print(f"- Total characters: {len(extracted_text)}")
        print(f"- Total lines: {len(extracted_text.splitlines())}")
        print(f"- First 500 characters:")
        print(extracted_text[:500])
        print()
        
        print("LINE-BY-LINE VIEW (first 20 lines):")
        print("-" * 80)
        lines = extracted_text.splitlines()
        for i, line in enumerate(lines[:20], 1):
            print(f"Line {i:2d}: {repr(line)}")
        print("-" * 80)
        
    except Exception as e:
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
        test_pdf_extraction(pdf_path)
    else:
        print("Usage: python test_pdf_text_extraction.py <path_to_pdf_file>")
        print("\nExample:")
        print("  python test_pdf_text_extraction.py uploads/resume.pdf")
