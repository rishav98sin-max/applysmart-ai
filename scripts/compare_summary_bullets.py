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

# Find summary section in both
print("=" * 80)
print("ORIGINAL CV - PROFESSIONAL SUMMARY")
print("=" * 80)
# Find summary section (usually near top, before "Experience")
lines = original_text.split('\n')
in_summary = False
summary_lines = []
for line in lines:
    if 'summary' in line.lower() or 'profile' in line.lower():
        in_summary = True
    if in_summary:
        summary_lines.append(line)
        if 'experience' in line.lower() and len(summary_lines) > 2:
            break
print('\n'.join(summary_lines[:20]))  # First 20 lines of summary

print("\n" + "=" * 80)
print("GENERATED CV - PROFESSIONAL SUMMARY")
print("=" * 80)
lines = generated_text.split('\n')
in_summary = False
summary_lines = []
for line in lines:
    if 'summary' in line.lower() or 'profile' in line.lower():
        in_summary = True
    if in_summary:
        summary_lines.append(line)
        if 'experience' in line.lower() and len(summary_lines) > 2:
            break
print('\n'.join(summary_lines[:20]))  # First 20 lines of summary

# Compare bullet points from a specific role
print("\n" + "=" * 80)
print("ORIGINAL CV - SAP LABS BULLETS")
print("=" * 80)
lines = original_text.split('\n')
in_sap = False
sap_bullets = []
for i, line in enumerate(lines):
    if 'SAP' in line and 'Labs' in line:
        in_sap = True
    if in_sap:
        sap_bullets.append(line)
        if i > len(lines) - 1 or ('Genesis' in lines[i+1] if i+1 < len(lines) else False):
            break
print('\n'.join(sap_bullets[:15]))

print("\n" + "=" * 80)
print("GENERATED CV - SAP LABS BULLETS (Tailored)")
print("=" * 80)
lines = generated_text.split('\n')
in_sap = False
sap_bullets = []
for i, line in enumerate(lines):
    if 'SAP' in line and 'Labs' in line:
        in_sap = True
    if in_sap:
        sap_bullets.append(line)
        if i > len(lines) - 1 or ('Genesis' in lines[i+1] if i+1 < len(lines) else False):
            break
print('\n'.join(sap_bullets[:15]))
