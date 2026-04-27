import fitz
import sys
from pathlib import Path

def extract_text(pdf_path, max_chars=10000):
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text[:max_chars]

if __name__ == "__main__":
    folder = Path("Error SS")
    
    # Original CV
    print("=" * 80)
    print("ORIGINAL CV: Shrestha Ghosh.CV_.pdf")
    print("=" * 80)
    original_text = extract_text(folder / "Shrestha Ghosh.CV_.pdf")
    print(original_text)
    print("\n")
    
    # Generated CV - Salesforce
    print("=" * 80)
    print("GENERATED CV: CV_Salesforce_Account_Executive_-_Digital_SMB.pdf")
    print("=" * 80)
    generated_text = extract_text(folder / "CV_Salesforce_Account_Executive_-_Digital_SMB.pdf")
    print(generated_text)
    print("\n")
    
    # Generated Cover Letter - Salesforce
    print("=" * 80)
    print("GENERATED COVER LETTER: CoverLetter_Salesforce_Account_Executive_-_Digital_SMB.pdf")
    print("=" * 80)
    cover_letter_text = extract_text(folder / "CoverLetter_Salesforce_Account_Executive_-_Digital_SMB.pdf")
    print(cover_letter_text)
    print("\n")
