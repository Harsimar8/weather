import fitz

doc = fitz.open("wr.pdf")

with open("pdf_content.md", "w", encoding="utf-8") as f:
    for page_num, page in enumerate(doc):
        f.write(f"\n## Page {page_num + 1}\n")
        f.write(page.get_text())