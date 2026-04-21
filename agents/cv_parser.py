import fitz  # PyMuPDF
import os


def parse_cv(cv_path):
    text = ""
    try:
        if not os.path.exists(cv_path):
            print(f"CV file not found at: {cv_path}")
            return ""
        
        doc = fitz.open(cv_path)
        for page in doc:
            text += page.get_text()
        doc.close()
        
        print(f"CV parsed successfully: {len(text)} characters extracted")
    except Exception as e:
        print(f"Error parsing CV: {e}")
    return text.strip()


def get_cv_sections(cv_text):
    """Splits CV text into rough sections for better AI processing"""
    sections = {
        "full_text": cv_text,
        "word_count": len(cv_text.split()),
        "char_count": len(cv_text)
    }
    return sections