import pdfplumber
import pandas as pd
import sys

pdf_path = "/Users/xumuzhi/Coding/Wljcy/wljcyServer/billExtra/微信支付交易明细证明(20210601-20210901).pdf"

try:
    with pdfplumber.open(pdf_path) as pdf:
        print(f"Total pages: {len(pdf.pages)}")
        
        # Try extracting text from first page to see header
        page0 = pdf.pages[0]
        text = page0.extract_text()
        print("--- Page 0 Text ---")
        print(text[:500]) # Print first 500 chars
        print("-------------------")
        
        # Try extracting tables
        tables = []
        for i, page in enumerate(pdf.pages):
            extracted_tables = page.extract_tables()
            if extracted_tables:
                print(f"Page {i} has {len(extracted_tables)} tables")
                for table in extracted_tables:
                    # Clean up table data
                    df = pd.DataFrame(table[1:], columns=table[0])
                    print(df.head())
                    tables.append(df)
                    break # Just show first table of first page with tables
            if tables:
                break
                
except Exception as e:
    print(f"Error reading PDF: {e}")
