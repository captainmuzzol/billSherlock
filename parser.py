import pdfplumber
import pandas as pd
from datetime import datetime
import re
import os
import hashlib

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
    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    s = s.replace(".", "-")
    
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y-%m-%d"
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def _normalize_header_cell(val):
    s = clean_str(val)
    if not s:
        return ""
    return s.replace(" ", "").replace("\u3000", "")

def _make_synthetic_id(*parts: str):
    raw = "|".join([p.strip() for p in parts if p is not None])
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return f"synthetic_{digest}"

def _extract_tables_with_fallback(page):
    try:
        tables = page.extract_tables()
        if tables:
            return tables
    except Exception:
        tables = []

    fallback_settings = [
        {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "snap_tolerance": 3,
            "join_tolerance": 3,
            "intersection_tolerance": 5,
            "edge_min_length": 3,
            "min_words_vertical": 1,
            "min_words_horizontal": 1,
            "text_tolerance": 2,
        }
    ]

    for settings in fallback_settings:
        try:
            tables = page.extract_tables(table_settings=settings)
            if tables:
                return tables
        except Exception:
            continue

    return []

def _pick_best_numeric_id(raw: str):
    if not raw:
        return ""
    compact = re.sub(r"\s+", "|", str(raw))
    candidates = re.findall(r"\d{16,}", compact)
    if not candidates:
        return ""
    candidates.sort(key=lambda x: (-len(x), x))
    return candidates[0]

def _parse_wechat_text_block(block: str):
    if not block:
        return None
    text = re.sub(r"\s+", " ", str(block)).strip()
    if not text:
        return None

    m_date = re.search(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", text)
    if not m_date:
        return None
    m_clock = re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", text)
    if m_clock:
        tx_time = parse_datetime(f"{m_date.group(0)} {m_clock.group(0)}")
    else:
        tx_time = parse_datetime(m_date.group(0))

    tid = _pick_best_numeric_id(text)
    if not tid:
        return None

    category = "其他"
    if "收入" in text:
        category = "收入"
    elif "支出" in text:
        category = "支出"
    amount_val = None
    m_amt = re.search(r"(?:¥\s*)?([+-]?\d{1,10}\.\d{2})(?!\d)", text)
    if not m_amt:
        m_amt = re.search(r"([+-]?\d+(?:\.\d{1,2})?)\s*元", text)
    if m_amt:
        amount_val = parse_amount(m_amt.group(1))
    if category == "其他" and amount_val is not None and amount_val < 0:
        category = "支出"

    amt = float(amount_val) if amount_val is not None else 0.0
    if amt < 0:
        amt = abs(amt)

    method = ""
    m_method = re.search(r"(零钱通|零钱|银行卡|信用卡|亲属卡|组合支付|余额|分付)", text)
    if m_method:
        method = m_method.group(1)

    tx_type = ""
    try:
        start = m_date.end()
        if m_clock and m_clock.start() < start:
            start = m_clock.end()
        cat_positions = []
        for kw in ("收入", "支出", "其他"):
            pos = text.find(kw, start)
            if pos != -1:
                cat_positions.append(pos)
        end = min(cat_positions) if cat_positions else -1
        core = text[start:end].strip() if end != -1 else text[start:].strip()
        core = re.sub(r"\b\d{16,}\b", " ", core)
        core = re.sub(r"\s+", " ", core).strip()
        if core:
            tx_type = core.split(" ")[0].strip()
    except Exception:
        tx_type = ""

    counterparty = ""
    if m_amt:
        tail = text[m_amt.end() :].strip()
        tail = tail.split("/")[0].strip()
        tail = re.sub(r"\b\d{16,}\b", " ", tail)
        tail = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", tail)
        tail = re.sub(r"\s+", " ", tail).strip()
        counterparty = tail

    return {
        "transaction_id": tid,
        "transaction_time": tx_time,
        "transaction_type": tx_type,
        "category": category,
        "method": method,
        "amount": amt,
        "counterparty": counterparty,
        "merchant_id": "",
    }

def _parse_wechat_text_page(text: str):
    if not text:
        return []
    lines = [ln.strip() for ln in str(text).splitlines() if ln and ln.strip()]
    merged = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        if (
            re.fullmatch(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", cur)
            and i + 1 < len(lines)
            and re.match(r"^\d{1,2}:\d{2}(?::\d{2})?\b", lines[i + 1])
        ):
            merged.append(cur + " " + lines[i + 1].lstrip())
            i += 2
            continue
        merged.append(cur)
        i += 1
    lines = merged
    txs = []
    seen = set()

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _pick_best_numeric_id(line):
            i += 1
            continue
        if not re.search(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", line):
            i += 1
            continue

        block = line
        used = 1
        needs_more = (
            not _pick_best_numeric_id(block)
            or not re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", block)
            or (not re.search(r"\b\d{1,10}\.\d{2}\b", block) and "¥" not in block and not re.search(r"\d+(?:\.\d{1,2})?\s*元", block))
        )
        if needs_more:
            for j in range(1, 4):
                if i + j >= len(lines):
                    break
                nxt = lines[i + j]
                if re.search(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", nxt) and _pick_best_numeric_id(nxt):
                    break
                block = block + " " + nxt
                used = j + 1
                if (
                    _pick_best_numeric_id(block)
                    and re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", block)
                    and (re.search(r"\b\d{1,10}\.\d{2}\b", block) or "¥" in block or re.search(r"\d+(?:\.\d{1,2})?\s*元", block))
                ):
                    break

        tx = _parse_wechat_text_block(block)
        if tx and tx.get("transaction_id") and tx.get("transaction_time"):
            tid = str(tx["transaction_id"])
            if tid not in seen:
                seen.add(tid)
                txs.append(tx)

        i += used

    return txs

def _guess_pdf_bill_type_from_table(table):
    if not table:
        return None
    checked = 0
    for row in table:
        if not row:
            continue
        cleaned = [clean_str(x) for x in row if x is not None]
        joined = " ".join(cleaned).strip()
        if not joined:
            continue
        checked += 1
        if checked > 8:
            break
        if "支付宝" in joined or "交易订单号" in joined or "收/支" in joined:
            return "alipay"
        if re.search(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", joined) and _pick_best_numeric_id(joined):
            return "wechat"
    return None

def inspect_pdf_sample(file_path: str, max_samples: int = 12):
    result = {"file": os.path.basename(file_path), "pages": 0, "samples": []}
    try:
        with pdfplumber.open(file_path) as pdf:
            pages = len(pdf.pages)
            result["pages"] = pages
            if pages <= 0:
                return result

            indices = []
            head = min(4, pages)
            for i in range(head):
                indices.append(i)
            if pages > 8:
                indices.append(pages // 2)
                indices.append((pages // 2) + 1 if (pages // 2) + 1 < pages else pages // 2)
            tail_start = max(0, pages - 4)
            for i in range(tail_start, pages):
                indices.append(i)

            dedup = []
            seen = set()
            for idx in indices:
                if idx in seen:
                    continue
                seen.add(idx)
                dedup.append(idx)
            indices = dedup[: max_samples]

            for idx in indices:
                page = pdf.pages[idx]
                text = None
                try:
                    text = page.extract_text()
                except Exception:
                    text = None
                text = text or ""
                snippet = text.strip().replace("\r", "")[:600]
                chars_count = 0
                try:
                    chars_count = len(getattr(page, "chars", []) or [])
                except Exception:
                    chars_count = 0
                images_count = 0
                try:
                    images_count = len(getattr(page, "images", []) or [])
                except Exception:
                    images_count = 0

                has_tables = False
                try:
                    tables = page.extract_tables()
                    has_tables = bool(tables)
                except Exception:
                    has_tables = False

                has_datetime = bool(re.search(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", text))
                result["samples"].append(
                    {
                        "page_index": idx + 1,
                        "text_len": len(text),
                        "chars_count": chars_count,
                        "images_count": images_count,
                        "has_tables": has_tables,
                        "has_date_pattern": has_datetime,
                        "text_snippet": snippet,
                    }
                )
    except Exception as e:
        result["error"] = str(e)
    return result

def parse_pdf_bill(file_path):
    transactions = []
    
    with pdfplumber.open(file_path) as pdf:
        if len(pdf.pages) > 0:
            # Check if the PDF is scanned (image-only)
            first_page_text = pdf.pages[0].extract_text()
            if not first_page_text or len(first_page_text.strip()) < 10:
                raise ValueError("检测到扫描件或纯图片 PDF，系统无法提取文本。请使用 OCR 工具转换为可编辑 PDF 或 Excel 后再上传。")

        first_page_text = pdf.pages[0].extract_text() if len(pdf.pages) > 0 else ""
        first_page_text = first_page_text or ""
        is_wechat_pdf = ("微信支付交易明细证明" in first_page_text) or ("交易单号" in first_page_text and "交易时间" in first_page_text)
        if is_wechat_pdf:
            seen_ids = set()
            for page in pdf.pages:
                page_text = None
                try:
                    page_text = page.extract_text() if page else None
                except Exception:
                    page_text = None
                for tx in _parse_wechat_text_page(page_text):
                    tid = str(tx.get("transaction_id") or "")
                    if not tid or tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    transactions.append(tx)
            return transactions

        current_bill_type = None
        for page in pdf.pages:
            before_page = len(transactions)
            tables = _extract_tables_with_fallback(page)
            if not tables:
                page_text = page.extract_text() if page else None
                transactions.extend(_parse_wechat_text_page(page_text))
                continue
            
            for table in tables:
                # Find header row
                header_index = -1
                bill_type = "unknown"
                
                for i, row in enumerate(table):
                    normalized_cells = [_normalize_header_cell(x) for x in row]
                    normalized_joined = "".join(normalized_cells)
                    
                    # WeChat PDF Signature
                    if "交易单号" in normalized_joined and "交易时间" in normalized_joined:
                        header_index = i
                        bill_type = "wechat"
                        break
                    
                    # Alipay PDF Signature
                    if "收/支" in normalized_joined and "交易订单号" in normalized_joined:
                        header_index = i
                        bill_type = "alipay"
                        break
                
                if header_index != -1 and bill_type in ("wechat", "alipay"):
                    current_bill_type = bill_type
                    start_index = header_index + 1
                else:
                    if current_bill_type in ("wechat", "alipay"):
                        bill_type = current_bill_type
                        start_index = 0
                    else:
                        guessed = _guess_pdf_bill_type_from_table(table)
                        if not guessed:
                            continue
                        bill_type = guessed
                        current_bill_type = guessed
                        start_index = 0
                
                for row in table[start_index:]:
                    if not row or all(cell is None or str(cell).strip() == "" for cell in row):
                        continue
                    
                    cleaned_row = [clean_str(cell) for cell in row]
                    joined_raw = " ".join(cleaned_row).strip()
                    joined_compact = joined_raw.replace(" ", "")
                    joined_for_id = "|".join([clean_id(c) for c in cleaned_row if c])
                    
                    # Filter out summary/footer rows
                    if not joined_raw:
                        continue
                    if re.search(r"共\d+笔", joined_compact):
                        continue

                    try:
                        transaction = None
                        
                        if bill_type == "wechat":
                            if len(cleaned_row) < 7:
                                continue
                            # WeChat mapping
                            # 0: 交易单号, 1: 交易时间, 2: 交易类型, 3: 收/支/其他, 
                            # 4: 交易方式, 5: 金额, 6: 交易对方, 7: 商户单号
                            tid = clean_id(cleaned_row[0]) if len(cleaned_row) > 0 else ""
                            if not tid:
                                m = re.search(r"\d{16,}", joined_for_id)
                                if m:
                                    tid = m.group(0)

                            tx_time = parse_datetime(cleaned_row[1]) if len(cleaned_row) > 1 else None
                            if tx_time is None:
                                m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?", joined_raw)
                                if m:
                                    tx_time = parse_datetime(m.group(0))
                            if tx_time is None:
                                m = re.search(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", joined_raw)
                                if m:
                                    tx_time = parse_datetime(m.group(0))

                            merchant = clean_id(cleaned_row[7]) if len(cleaned_row) > 7 else ""
                            amount = parse_amount(cleaned_row[5]) if len(cleaned_row) > 5 else 0.0
                            counterparty = cleaned_row[6] if len(cleaned_row) > 6 else ""
                            tx_type = cleaned_row[2] if len(cleaned_row) > 2 else ""
                            category = cleaned_row[3] if len(cleaned_row) > 3 else ""
                            method = cleaned_row[4] if len(cleaned_row) > 4 else ""

                            if not tid:
                                tid = _make_synthetic_id(
                                    str(tx_time) if tx_time else "",
                                    str(amount),
                                    counterparty,
                                    tx_type,
                                    category,
                                    method,
                                    merchant,
                                )

                            transaction = {
                                "transaction_id": tid,
                                "transaction_time": tx_time,
                                "transaction_type": tx_type,
                                "category": category,
                                "method": method,
                                "amount": amount,
                                "counterparty": counterparty,
                                "merchant_id": merchant,
                            }
                        
                        elif bill_type == "alipay":
                            if len(cleaned_row) < 8: continue
                            # Alipay PDF mapping based on inspection:
                            # 0: 收/支, 1: 交易对方, 2: 商品说明, 3: 收/付款方式
                            # 4: 金额, 5: 交易订单号, 6: 商家订单号, 7: 交易时间
                            
                            # Note: Sometimes Alipay splits rows weirdly, but usually table extraction handles it.
                            # Skip if transaction ID is missing or not numeric-ish
                            tid = clean_id(cleaned_row[5]) if len(cleaned_row) > 5 else ""
                            if not tid:
                                m = re.search(r"\d{16,}", joined_for_id)
                                if m:
                                    tid = m.group(0)
                            
                            transaction = {
                                "transaction_id": tid or _make_synthetic_id(joined_compact),
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

            if len(transactions) == before_page:
                page_text = page.extract_text() if page else None
                transactions.extend(_parse_wechat_text_page(page_text))
                        
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
