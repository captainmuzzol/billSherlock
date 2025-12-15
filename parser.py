import pdfplumber
import pandas as pd
from datetime import datetime
import re

def parse_pdf_bill(file_path):
    transactions = []
    
    with pdfplumber.open(file_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            if not tables:
                continue
            
            for table in tables:
                # Find the header row index
                header_index = -1
                for i, row in enumerate(table):
                    # Check if row contains specific headers
                    # row is a list of strings (or None)
                    row_str = [str(x) if x else '' for x in row]
                    if "交易单号" in row_str and "交易时间" in row_str:
                        header_index = i
                        break
                
                start_index = header_index + 1 if header_index != -1 else 0
                
                # If we found a header, use it to map columns if needed, 
                # but usually the structure is fixed:
                # 0: 交易单号
                # 1: 交易时间
                # 2: 交易类型
                # 3: 收/支/其他
                # 4: 交易方式
                # 5: 金额(元)
                # 6: 交易对方
                # 7: 商户单号
                
                for row in table[start_index:]:
                    if not row or all(cell is None or str(cell).strip() == "" for cell in row):
                        continue
                        
                    # Basic cleaning: remove newlines
                    cleaned_row = [str(cell).replace('\n', ' ').strip() if cell else '' for cell in row]
                    
                    # Ensure we have enough columns (8 columns based on observation)
                    if len(cleaned_row) < 8:
                        continue
                        
                    # Check if it's a valid data row (e.g. has a transaction ID)
                    # Transaction ID usually starts with numbers.
                    # Sometimes the footer or summary rows are included.
                    if not cleaned_row[0] or "共" in cleaned_row[0]: # "共X笔" summary row
                        continue
                        
                    try:
                        # Parse Amount
                        amount_str = cleaned_row[5].replace(',', '').replace('¥', '')
                        amount = float(amount_str) if amount_str else 0.0
                        
                        # Parse Date
                        date_str = cleaned_row[1]
                        # Handle potential format issues
                        try:
                            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            try:
                                # Try with space if newline was replaced by space
                                dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                            except:
                                # Fallback or keep as None/String if needed? 
                                # Let's try to fix common issue "YYYY-MM-DD HH:MM:SS"
                                # If it fails, maybe it's just date or just time?
                                # Assume valid format for now based on PDF check
                                dt = None

                        transaction = {
                            "transaction_id": cleaned_row[0],
                            "transaction_time": dt,
                            "transaction_type": cleaned_row[2],
                            "category": cleaned_row[3],
                            "method": cleaned_row[4],
                            "amount": amount,
                            "counterparty": cleaned_row[6],
                            "merchant_id": cleaned_row[7]
                        }
                        transactions.append(transaction)
                    except Exception as e:
                        print(f"Skipping row due to error: {e}, Row: {cleaned_row}")
                        continue
                        
    return transactions
