import fitz  # PyMuPDF
import os
import re as _re


def parse_cv(cv_path):
    text = ""
    try:
        if not os.path.exists(cv_path):
            print(f"CV file not found at: {cv_path}")
            return ""
        
        doc = fitz.open(cv_path)
        # Fonts whose names mark symbol/bullet glyphs — skip their spans so
        # the mis-decoded "?" bullets don't leak into the body text.
        _SYMBOLIC_FONT_RX = _re.compile(
            r"(symbol|wingding|webding|dingbat|marlett|bullet)",
            _re.IGNORECASE,
        )
        for page in doc:
            # Use dict mode for better character encoding preservation
            # This prevents special bullet characters (→, ▸, •) from becoming ?
            # while preserving line structure (important for CV sections/bullets)
            page_dict = page.get_text("dict")
            for block in page_dict.get("blocks", []):
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    line_text = ""
                    for span in line.get("spans", []):
                        font = span.get("font", "") or ""
                        if _SYMBOLIC_FONT_RX.search(font):
                            # Replace symbolic-font bullet glyph with a real bullet
                            # so downstream LLM prompts see a recognisable marker.
                            line_text += "• "
                            continue
                        line_text += span.get("text", "")
                    if line_text.strip():
                        text += line_text + "\n"
                text += "\n"  # Blank line between blocks (paragraphs/sections)
        doc.close()
        
        # Repair mis-decoded bullet prefixes ("?   thing" → "• thing") and
        # collapse excessive blank lines.
        text = _re.sub(r"(^|\n)\s*[?\uFFFD]+(?=\s{2,})\s+", r"\1• ", text)
        text = _re.sub(r"\n{3,}", "\n\n", text).strip()
        
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