import pdfplumber
import pandas as pd
from datetime import datetime
import re
import os

def parse_bill_file(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.pdf':
        return parse_pdf_bill(file_path)
    elif ext in ['.xlsx', '.xls']:
        return parse_excel_bill(file_path)
    elif ext in ['.jpg', '.jpeg', '.png', '.bmp', '.gif']:
        raise ValueError("不支持纯图片格式的账单。系统无法识别图片内容，请上传 PDF 或 Excel 电子账单。")
    else:
        raise ValueError(f"不支持的文件格式: {ext}")

def clean_str(val):
    if val is None:
        return ""
    return str(val).replace('\n', ' ').strip()

def clean_id(val):
    if val is None:
        return ""
    # IDs shouldn't have spaces usually, especially if they were just wrapped
    return str(val).replace('\n', '').replace(' ', '').strip()

def parse_amount(val):
    if val is None:
        return 0.0
    s = str(val).replace(',', '').replace('¥', '').strip()
    try:
        return float(s)
    except:
        return 0.0

def parse_datetime(val):
    if val is None:
        return None
    s = str(val).strip()
    # Handle newline if present (common in Alipay)
    s = s.replace('\n', ' ').replace('  ', ' ')
    
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d"
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def parse_pdf_bill(file_path):
    transactions = []
    
    with pdfplumber.open(file_path) as pdf:
        if len(pdf.pages) > 0:
            # Check if the PDF is scanned (image-only)
            first_page_text = pdf.pages[0].extract_text()
            if not first_page_text or len(first_page_text.strip()) < 10:
                raise ValueError("检测到扫描件或纯图片 PDF，系统无法提取文本。请使用 OCR 工具转换为可编辑 PDF 或 Excel 后再上传。")

        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            
            for table in tables:
                # Find header row
                header_index = -1
                bill_type = "unknown"
                
                for i, row in enumerate(table):
                    row_str = [clean_str(x) for x in row]
                    
                    # WeChat PDF Signature
                    if "交易单号" in row_str and "交易时间" in row_str:
                        header_index = i
                        bill_type = "wechat"
                        break
                    
                    # Alipay PDF Signature
                    if "收/支" in row_str and "交易订单号" in row_str:
                        header_index = i
                        bill_type = "alipay"
                        break
                
                if header_index == -1:
                    continue
                
                start_index = header_index + 1
                
                for row in table[start_index:]:
                    if not row or all(cell is None or str(cell).strip() == "" for cell in row):
                        continue
                    
                    cleaned_row = [clean_str(cell) for cell in row]
                    
                    # Filter out summary/footer rows
                    if not cleaned_row[0] or "共" in cleaned_row[0] or "笔" in cleaned_row[0]:
                        continue

                    try:
                        transaction = None
                        
                        if bill_type == "wechat":
                            if len(cleaned_row) < 8: continue
                            # WeChat mapping
                            # 0: 交易单号, 1: 交易时间, 2: 交易类型, 3: 收/支/其他, 
                            # 4: 交易方式, 5: 金额, 6: 交易对方, 7: 商户单号
                            transaction = {
                                "transaction_id": clean_id(cleaned_row[0]),
                                "transaction_time": parse_datetime(cleaned_row[1]),
                                "transaction_type": cleaned_row[2],
                                "category": cleaned_row[3],
                                "method": cleaned_row[4],
                                "amount": parse_amount(cleaned_row[5]),
                                "counterparty": cleaned_row[6],
                                "merchant_id": clean_id(cleaned_row[7])
                            }
                        
                        elif bill_type == "alipay":
                            if len(cleaned_row) < 8: continue
                            # Alipay PDF mapping based on inspection:
                            # 0: 收/支, 1: 交易对方, 2: 商品说明, 3: 收/付款方式
                            # 4: 金额, 5: 交易订单号, 6: 商家订单号, 7: 交易时间
                            
                            # Note: Sometimes Alipay splits rows weirdly, but usually table extraction handles it.
                            # Skip if transaction ID is missing or not numeric-ish
                            if not cleaned_row[5]: continue
                            
                            transaction = {
                                "transaction_id": clean_id(cleaned_row[5]),
                                "transaction_time": parse_datetime(cleaned_row[7]),
                                "transaction_type": cleaned_row[2], # 商品说明 as type
                                "category": cleaned_row[0],
                                "method": cleaned_row[3],
                                "amount": parse_amount(cleaned_row[4]),
                                "counterparty": cleaned_row[1],
                                "merchant_id": clean_id(cleaned_row[6])
                            }
                        
                        if transaction and transaction["transaction_id"]:
                            transactions.append(transaction)
                            
                    except Exception as e:
                        print(f"Skipping row due to error: {e}, Row: {cleaned_row}")
                        continue
                        
    return transactions

def parse_excel_bill(file_path):
    transactions = []
    
    # Read entire excel without header first
    try:
        df = pd.read_excel(file_path, header=None)
    except Exception as e:
        print(f"Error reading Excel file: {e}")
        return []
    
    # Find header row index
    header_index = -1
    bill_type = "unknown"
    
    for i, row in df.iterrows():
        row_values = [str(x).strip() for x in row.values if pd.notna(x)]
        row_str = " ".join(row_values).replace('\n', '')
        
        # Alipay Excel Signature
        if "收/支" in row_values and "交易订单号" in row_values: 
            header_index = i
            bill_type = "alipay"
            break
        
        # WeChat Excel Signature (Common in CSV-to-Excel conversions)
        # Columns: 交易时间, 交易类型, 交易对方, 商品, 收/支, 金额(元), 支付方式, ...
        if "交易时间" in row_values and "交易类型" in row_values and ("金额(元)" in row_values or "金额" in row_values):
            header_index = i
            bill_type = "wechat"
            break
            
        # Check for newline variants just in case
        if "收/支" in row_str and "交易订单号" in row_str:
            header_index = i
            bill_type = "alipay"
            break
            
    if header_index == -1:
        print("Could not find header row in Excel file")
        return []
    
    # Re-read or slice dataframe
    # Using the row at header_index as columns
    headers = df.iloc[header_index].astype(str).str.replace('\n', '').str.strip().tolist()
    
    # Create new DF with correct headers
    data_df = df.iloc[header_index + 1:].copy()
    data_df.columns = headers
    
    for _, row in data_df.iterrows():
        try:
            if bill_type == "alipay":
                # Alipay Excel mapping:
                # 收/支, 交易对方, 商品说明, 收/付款方式, 金额, 交易订单号, 商家订单号, 交易时间
                
                # Check for required fields
                if pd.isna(row.get('交易订单号')) or pd.isna(row.get('交易时间')):
                    continue
                    
                tid = clean_id(row.get('交易订单号'))
                # Skip footer rows like "共X笔"
                if not tid or not tid[0].isdigit():
                    continue
                
                transaction = {
                    "transaction_id": tid,
                    "transaction_time": parse_datetime(row.get('交易时间')),
                    "transaction_type": clean_str(row.get('商品说明')),
                    "category": clean_str(row.get('收/支')),
                    "method": clean_str(row.get('收/付款方式')),
                    "amount": parse_amount(row.get('金额')),
                    "counterparty": clean_str(row.get('交易对方')),
                    "merchant_id": clean_id(row.get('商家订单号'))
                }
                transactions.append(transaction)

            elif bill_type == "wechat":
                # WeChat Excel mapping:
                # 交易时间, 交易类型, 交易对方, 商品, 收/支, 金额(元), 支付方式, 当前状态, 交易单号, 商户单号, 备注
                
                # Handle "金额(元)" vs "金额"
                amount_col = "金额(元)" if "金额(元)" in headers else "金额"
                
                if pd.isna(row.get('交易单号')) or pd.isna(row.get('交易时间')):
                    continue

                tid = clean_id(row.get('交易单号'))
                # Skip footer rows
                if not tid or not tid[0].isdigit(): # WeChat IDs are numeric-ish (sometimes start with 420...)
                     # Check if it's a valid ID. WeChat IDs are long numbers.
                     # Sometimes header repetition or footer summary
                     if len(tid) < 5: continue
                
                transaction = {
                    "transaction_id": tid,
                    "transaction_time": parse_datetime(row.get('交易时间')),
                    "transaction_type": clean_str(row.get('交易类型')),
                    "category": clean_str(row.get('收/支')),
                    "method": clean_str(row.get('支付方式')),
                    "amount": parse_amount(row.get(amount_col)),
                    "counterparty": clean_str(row.get('交易对方')),
                    "merchant_id": clean_id(row.get('商户单号'))
                }
                
                # Normalize category for WeChat (sometimes they have different terms?)
                # Usually "收入", "支出", or "/"
                if transaction['category'] == '/':
                    transaction['category'] = '其他'
                
                transactions.append(transaction)
                
        except Exception as e:
            print(f"Skipping excel row due to error: {e}")
            continue
            
    return transactions
