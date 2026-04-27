import fitz
from pathlib import Path
import difflib

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
print("DETAILED COMPARISON")
print("=" * 80)

# Split into lines for comparison
orig_lines = original_text.split('\n')
gen_lines = generated_text.split('\n')

print(f"\nOriginal CV: {len(orig_lines)} lines")
print(f"Generated CV: {len(gen_lines)} lines")

# Find summary sections
print("\n" + "=" * 80)
print("SUMMARY COMPARISON")
print("=" * 80)

orig_summary_lines = []
gen_summary_lines = []

in_orig_summary = False
in_gen_summary = False

for i, line in enumerate(orig_lines):
    if 'professional experience' in line.lower() or 'professional summary' in line.lower():
        in_orig_summary = True
        continue
    if in_orig_summary:
        if 'work experience' in line.lower() and i > 10:
            break
        if line.strip():
            orig_summary_lines.append(line)

for i, line in enumerate(gen_lines):
    if 'professional experience' in line.lower() or 'professional summary' in line.lower():
        in_gen_summary = True
        continue
    if in_gen_summary:
        if 'work experience' in line.lower() and i > 10:
            break
        if line.strip():
            gen_summary_lines.append(line)

print("\nOriginal Summary (first 500 chars):")
print(''.join(orig_summary_lines)[:500])
print("\nGenerated Summary (first 500 chars):")
print(''.join(gen_summary_lines)[:500])

print("\n" + "=" * 80)
print("BULLET CHANGES")
print("=" * 80)

# Find bullet points with ?
orig_bullets = [line for line in orig_lines if '?' in line and line.strip()]
gen_bullets = [line for line in gen_lines if '?' in line and line.strip()]

print(f"\nOriginal CV bullets with ?: {len(orig_bullets)}")
print(f"Generated CV bullets with ?: {len(gen_bullets)}")

if gen_bullets:
    print("\nGenerated CV bullets with ?:")
    for bullet in gen_bullets[:10]:
        print(f"  {bullet[:100]}")

# Check end of CV
print("\n" + "=" * 80)
print("END OF CV COMPARISON")
print("=" * 80)

print("\nOriginal CV last 500 chars:")
print(original_text[-500:])
print("\nGenerated CV last 500 chars:")
print(generated_text[-500:])

# Check for formatting differences
print("\n" + "=" * 80)
print("FORMATTING DIFFERENCES")
print("=" * 80)

orig_bullet_chars = [c for c in original_text if c in '•●▪■○·']
gen_bullet_chars = [c for c in generated_text if c in '•●▪■○·']

print(f"\nOriginal bullet chars: {len(orig_bullet_chars)}")
print(f"Generated bullet chars: {len(gen_bullet_chars)}")
