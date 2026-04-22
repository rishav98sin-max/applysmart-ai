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
            # Use dict mode for better character encoding preservation
            # This prevents special bullet characters (→, ▸, •) from becoming ?
            page_dict = page.get_text("dict")
            for block in page_dict["blocks"]:
                if "lines" in block:
                    for line in block["lines"]:
                        for span in line["spans"]:
                            text += span["text"]
                            # Add appropriate spacing
                            if span.get("flags", 0) & 2 ** 0:  # Superscript
                                pass
                            else:
                                text += " "
                elif "text" in block:
                    text += block["text"] + " "
        doc.close()
        
        # Clean up extra whitespace while preserving structure
        text = " ".join(text.split())
        
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