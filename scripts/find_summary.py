import fitz
from pathlib import Path

def extract_text(pdf_path):
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text

folder = Path("Error SS")

# Extract original CV text
original_text = extract_text(folder / "Shrestha Ghosh.CV_.pdf")

# Extract generated CV text
generated_text = extract_text(folder / "CV_Salesforce_Account_Executive_-_Digital_SMB.pdf")

print("=" * 80)
print("ORIGINAL CV - FULL TEXT (first 3000 chars)")
print("=" * 80)
print(original_text[:3000])

print("\n" + "=" * 80)
print("GENERATED CV - FULL TEXT (first 3000 chars)")
print("=" * 80)
print(generated_text[:3000])
