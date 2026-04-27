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

# Extract new generated CV text
generated_text = extract_text(folder / "CV_Salesforce_Account_Executive_-_Digital_SMB (1).pdf")

print("=" * 80)
print("ORIGINAL CV - FULL TEXT")
print("=" * 80)
print(original_text[:4000])

print("\n" + "=" * 80)
print("GENERATED CV (NEW) - FULL TEXT")
print("=" * 80)
print(generated_text[:4000])

# Check for missing sections
print("\n" + "=" * 80)
print("SECTION COMPARISON")
print("=" * 80)

original_lower = original_text.lower()
generated_lower = generated_text.lower()

sections = [
    "contact",
    "email",
    "phone",
    "linkedin",
    "education",
    "scholastic",
    "awards",
    "achievements",
    "projects",
    "skills"
]

for section in sections:
    orig_has = section in original_lower
    gen_has = section in generated_lower
    status = "✓" if gen_has else "✗"
    print(f"{status} {section}: original={orig_has}, generated={gen_has}")
