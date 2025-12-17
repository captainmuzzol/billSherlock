from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Query, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse, HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
import shutil
import os
import mimetypes
import uvicorn
import requests
import re
from datetime import datetime, timedelta
from pydantic import BaseModel

import models, database, parser

models.Base.metadata.create_all(bind=database.engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Global state for report proxy
REPORT_CONTEXT = {
    "root_dir": None,
    "main_file": None
}

# Pydantic Models
class ReportPathRequest(BaseModel):
    suspect_id: int
    file_path: Optional[str] = None
    search_name: Optional[str] = None

class SuspectCreate(BaseModel):
    name: str
    password: str

class SuspectVerify(BaseModel):
    suspect_id: int
    password: str

class SuspectRead(BaseModel):
    id: int
    name: str
    created_at: datetime
    file_count: int = 0
    last_update: Optional[datetime] = None
    report_path: Optional[str] = None
    report_filename: Optional[str] = None
    
    class Config:
        from_attributes = True

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("static/favicon.ico")

@app.get("/")
def read_root():
    return RedirectResponse(url="/static/index.html")

@app.post("/suspects", response_model=SuspectRead)
def create_suspect(suspect: SuspectCreate, db: Session = Depends(database.get_db)):
    if len(suspect.password) < 3:
        raise HTTPException(status_code=400, detail="Password must be at least 3 characters")
        
    existing = db.query(models.Suspect).filter(models.Suspect.name == suspect.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Suspect with this name already exists")
        
    db_suspect = models.Suspect(name=suspect.name, password=suspect.password)
    db.add(db_suspect)
    db.commit()
    db.refresh(db_suspect)
    return db_suspect

@app.get("/suspects", response_model=List[SuspectRead])
def read_suspects(search: Optional[str] = None, db: Session = Depends(database.get_db)):
    query = db.query(models.Suspect)
    if search:
        query = query.filter(models.Suspect.name.contains(search))
    
    suspects = query.all()
    results = []
    for s in suspects:
        # Calculate stats
        tx_query = db.query(models.Transaction).filter(models.Transaction.suspect_id == s.id)
        file_count = tx_query.group_by(models.Transaction.source_file).count()
        last_tx = tx_query.order_by(models.Transaction.transaction_time.desc()).first()
        
        results.append({
            "id": s.id,
            "name": s.name,
            "created_at": s.created_at,
            "file_count": file_count,
            "last_update": last_tx.transaction_time if last_tx else s.created_at,
            "report_path": s.report_path,
            "report_filename": s.report_filename
        })
    return results

@app.post("/upload")
def upload_file(
    suspect_id: int = Form(...),
    files: List[UploadFile] = File(...), 
    db: Session = Depends(database.get_db)
):
    suspect = db.query(models.Suspect).filter(models.Suspect.id == suspect_id).first()
    if not suspect:
        raise HTTPException(status_code=404, detail="Suspect not found")
        
    results = []
    for file in files:
        file_location = f"temp_{file.filename}"
        with open(file_location, "wb+") as file_object:
            shutil.copyfileobj(file.file, file_object)
        
        try:
            data = parser.parse_bill_file(file_location)
            
            # Save to DB
            count = 0
            for item in data:
                # Check duplicates for this suspect
                exists = db.query(models.Transaction).filter(
                    models.Transaction.transaction_id == item['transaction_id'],
                    models.Transaction.suspect_id == suspect_id
                ).first()
                
                if not exists:
                    db_item = models.Transaction(**item, source_file=file.filename, suspect_id=suspect_id)
                    db.add(db_item)
                    count += 1
            
            db.commit()
            results.append({"filename": file.filename, "parsed_count": len(data), "inserted_count": count})
        except Exception as e:
            results.append({"filename": file.filename, "error": str(e)})
        finally:
            if os.path.exists(file_location):
                os.remove(file_location)
                
    return results

@app.post("/suspects/verify")
def verify_suspect_password(verify: SuspectVerify, db: Session = Depends(database.get_db)):
    suspect = db.query(models.Suspect).filter(models.Suspect.id == verify.suspect_id).first()
    if not suspect:
        raise HTTPException(status_code=404, detail="Suspect not found")
    if suspect.password != verify.password:
        raise HTTPException(status_code=401, detail="Incorrect password")
    return {"message": "Verified"}

@app.delete("/suspects/{suspect_id}")
def delete_suspect(suspect_id: int, db: Session = Depends(database.get_db)):
    suspect = db.query(models.Suspect).filter(models.Suspect.id == suspect_id).first()
    if not suspect:
        raise HTTPException(status_code=404, detail="Suspect not found")
    
    # Delete transactions first
    db.query(models.Transaction).filter(models.Transaction.suspect_id == suspect_id).delete()
    
    # Delete suspect
    db.delete(suspect)
    db.commit()
    return {"message": "Suspect deleted"}

@app.get("/suspects/{suspect_id}/files")
def get_suspect_files(suspect_id: int, db: Session = Depends(database.get_db)):
    # Get distinct source files
    files = db.query(models.Transaction.source_file).filter(
        models.Transaction.suspect_id == suspect_id
    ).distinct().all()
    return [f[0] for f in files]

@app.delete("/suspects/{suspect_id}/files")
def delete_suspect_file(suspect_id: int, filename: str, db: Session = Depends(database.get_db)):
    # Delete transactions for this file
    result = db.query(models.Transaction).filter(
        models.Transaction.suspect_id == suspect_id,
        models.Transaction.source_file == filename
    ).delete()
    db.commit()
    return {"message": f"Deleted {result} transactions from {filename}"}

def parse_filter_time(time_str: str, is_end_of_range: bool = False):
    if not time_str:
        return None
    try:
        # Try full datetime
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        if is_end_of_range:
            return dt + timedelta(seconds=1)
        return dt
    except ValueError:
        pass

    try:
        # Try datetime without seconds
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        if is_end_of_range:
            return dt + timedelta(minutes=1)
        return dt
    except ValueError:
        pass
        
    try:
        # Try date only
        dt = datetime.strptime(time_str, "%Y-%m-%d")
        if is_end_of_range:
            return dt + timedelta(days=1)
        return dt
    except ValueError:
        pass
        
    return None

@app.get("/transactions")
def get_transactions(
    skip: int = 0, 
    limit: int = 100, 
    suspect_id: Optional[int] = None,
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None,
    counterparty: Optional[str] = None,
    category: Optional[str] = None,
    transaction_type: Optional[str] = None,
    method: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    db: Session = Depends(database.get_db)
):
    query = db.query(models.Transaction)
    
    if suspect_id:
        query = query.filter(models.Transaction.suspect_id == suspect_id)
    
    if start_date:
        dt = parse_filter_time(start_date)
        if dt:
            query = query.filter(models.Transaction.transaction_time >= dt)
    if end_date:
        dt = parse_filter_time(end_date, is_end_of_range=True)
        if dt:
            query = query.filter(models.Transaction.transaction_time < dt)
    if counterparty:
        # Support multiple counterparties separated by comma (Chinese or English)
        keywords = counterparty.replace("，", ",").split(",")
        keywords = [k.strip() for k in keywords if k.strip()]
        
        if len(keywords) > 0:
            # Create an OR condition for all keywords
            # Use exact match as requested
            conditions = [models.Transaction.counterparty == k for k in keywords]
            query = query.filter(or_(*conditions))
    if category:
        query = query.filter(models.Transaction.category == category)
    if transaction_type:
        query = query.filter(models.Transaction.transaction_type.contains(transaction_type))
    if method:
        query = query.filter(models.Transaction.method.contains(method))
    if min_amount is not None:
        query = query.filter(models.Transaction.amount >= min_amount)
    if max_amount is not None:
        query = query.filter(models.Transaction.amount <= max_amount)
        
    total = query.count()
    total_amount = query.with_entities(func.sum(models.Transaction.amount)).scalar() or 0
    transactions = query.order_by(models.Transaction.transaction_time.desc()).offset(skip).limit(limit).all()
    
    return {"total": total, "total_amount": total_amount, "data": transactions}

@app.get("/stats/summary")
def get_summary(
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None,
    suspect_id: Optional[int] = None,
    specific_amount: Optional[float] = None,
    time_range: Optional[str] = None, # "day", "night", "all"
    db: Session = Depends(database.get_db)
):
    query = db.query(models.Transaction)
    
    if suspect_id:
        query = query.filter(models.Transaction.suspect_id == suspect_id)
    
    if start_date:
        dt = parse_filter_time(start_date)
        if dt:
            query = query.filter(models.Transaction.transaction_time >= dt)
    if end_date:
        dt = parse_filter_time(end_date, is_end_of_range=True)
        if dt:
            query = query.filter(models.Transaction.transaction_time < dt)
    if specific_amount is not None:
        query = query.filter(models.Transaction.amount == specific_amount)
    
    if time_range == "day":
        # 06:00 - 17:59
        query = query.filter(func.strftime("%H", models.Transaction.transaction_time) >= "06")
        query = query.filter(func.strftime("%H", models.Transaction.transaction_time) <= "17")
    elif time_range == "night":
        # 18:00 - 05:59 (Next Day) -> This means HOUR >= 18 OR HOUR <= 05
        query = query.filter(
            (func.strftime("%H", models.Transaction.transaction_time) >= "18") | 
            (func.strftime("%H", models.Transaction.transaction_time) <= "05")
        )

    total_income = query.filter(models.Transaction.category == "收入").with_entities(func.sum(models.Transaction.amount)).scalar() or 0
    total_expense = query.filter(models.Transaction.category == "支出").with_entities(func.sum(models.Transaction.amount)).scalar() or 0
    
    return {"total_income": total_income, "total_expense": total_expense}

@app.get("/stats/by-counterparty")
def get_stats_by_counterparty(
    limit: int = 10, 
    category: Optional[str] = None, 
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None,
    suspect_id: Optional[int] = None,
    specific_amount: Optional[float] = None,
    time_range: Optional[str] = None,
    db: Session = Depends(database.get_db)
):
    query = db.query(
        models.Transaction.counterparty, 
        func.sum(models.Transaction.amount).label("total")
    )
    
    if suspect_id:
        query = query.filter(models.Transaction.suspect_id == suspect_id)

    if category:
        query = query.filter(models.Transaction.category == category)
    else:
        # Default behavior if category is not provided: include both income and expense?
        # The user requested "来往（包括收入）TOP 10", so we should NOT filter by category if not specified, or specifically include both.
        # But usually we might want to filter out "其他" if it's not relevant money movement.
        # Let's assume we want to sum up amount regardless of category (Income + Expense)
        pass

    if start_date:
        dt = parse_filter_time(start_date)
        if dt:
            query = query.filter(models.Transaction.transaction_time >= dt)
    if end_date:
        dt = parse_filter_time(end_date, is_end_of_range=True)
        if dt:
            query = query.filter(models.Transaction.transaction_time < dt)
    if specific_amount is not None:
        query = query.filter(models.Transaction.amount == specific_amount)
        
    if time_range == "day":
        # 06:00 - 17:59
        query = query.filter(func.strftime("%H", models.Transaction.transaction_time) >= "06")
        query = query.filter(func.strftime("%H", models.Transaction.transaction_time) <= "17")
    elif time_range == "night":
        # 18:00 - 05:59
        query = query.filter(
            (func.strftime("%H", models.Transaction.transaction_time) >= "18") | 
            (func.strftime("%H", models.Transaction.transaction_time) <= "05")
        )

    results = query.group_by(models.Transaction.counterparty).order_by(func.sum(models.Transaction.amount).desc()).limit(limit).all()
    
    return [{"name": r[0], "value": r[1]} for r in results]

@app.get("/stats/by-date")
def get_stats_by_date(
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None,
    suspect_id: Optional[int] = None,
    specific_amount: Optional[float] = None,
    time_range: Optional[str] = None,
    db: Session = Depends(database.get_db)
):
    query = db.query(
        func.strftime("%Y-%m-%d", models.Transaction.transaction_time).label("date"),
        models.Transaction.category,
        func.sum(models.Transaction.amount)
    )
    
    if suspect_id:
        query = query.filter(models.Transaction.suspect_id == suspect_id)

    if start_date:
        dt = parse_filter_time(start_date)
        if dt:
            query = query.filter(models.Transaction.transaction_time >= dt)
    if end_date:
        dt = parse_filter_time(end_date, is_end_of_range=True)
        if dt:
            query = query.filter(models.Transaction.transaction_time < dt)
    if specific_amount is not None:
        query = query.filter(models.Transaction.amount == specific_amount)

    if time_range == "day":
        # 06:00 - 17:59
        query = query.filter(func.strftime("%H", models.Transaction.transaction_time) >= "06")
        query = query.filter(func.strftime("%H", models.Transaction.transaction_time) <= "17")
    elif time_range == "night":
        # 18:00 - 05:59
        query = query.filter(
            (func.strftime("%H", models.Transaction.transaction_time) >= "18") | 
            (func.strftime("%H", models.Transaction.transaction_time) <= "05")
        )

    results = query.group_by("date", models.Transaction.category).all()
    
    # Process into structured format
    data = {}
    for r in results:
        date = r[0]
        cat = r[1]
        amount = r[2]
        if date not in data:
            data[date] = {"income": 0, "expense": 0}
        
        if cat == "收入":
            data[date]["income"] += amount
        elif cat == "支出":
            data[date]["expense"] += amount
            
    sorted_dates = sorted(data.keys())
    return {
        "dates": sorted_dates,
        "income": [data[d]["income"] for d in sorted_dates],
        "expense": [data[d]["expense"] for d in sorted_dates]
    }

import asyncio
from concurrent.futures import ThreadPoolExecutor

# Create a limited thread pool specifically for AI tasks
# This prevents AI requests from starving the main thread pool used for uploads and other operations
# Local LLM is CPU/VRAM bound, so limiting concurrency is also good for performance
ai_executor = ThreadPoolExecutor(max_workers=2)

def call_ollama_sync(prompt, ollama_host):
    # Ensure scheme is present
    if not ollama_host.startswith("http://") and not ollama_host.startswith("https://"):
        ollama_host = "http://" + ollama_host
        
    # Fix for Windows: cannot connect to 0.0.0.0 directly
    if "0.0.0.0" in ollama_host:
        ollama_host = ollama_host.replace("0.0.0.0", "127.0.0.1")
        
    ollama_url = f"{ollama_host}/api/generate"
    
    payload = {
        "model": "qwen3:1.7b",
        "prompt": prompt,
        "stream": False
    }
    
    try:
        # Increased timeout to 120s because queuing might happen
        resp = requests.post(ollama_url, json=payload, timeout=120)
        if resp.status_code == 200:
            raw_response = resp.json().get("response", "AI 分析服务暂无响应")
            # Clean <think> tags
            cleaned_response = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
            return {"success": True, "analysis": cleaned_response}
        else:
            return {"success": False, "error": f"AI 服务响应错误: {resp.status_code}"}
    except Exception as e:
        return {"success": False, "error": f"AI 分析连接失败: {str(e)}"}

@app.get("/stats/ai-analysis")
async def get_ai_analysis(
    suspect_id: int,
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None,
    db: Session = Depends(database.get_db)
):
    suspect = db.query(models.Suspect).filter(models.Suspect.id == suspect_id).first()
    if not suspect:
        raise HTTPException(status_code=404, detail="Suspect not found")

    # 0. Check Cache (Only if no date filters, as date filters change the context)
    # If users are filtering by date, we probably should re-analyze or just warn that analysis is based on full data.
    # The requirement says "Unless bills appended". It implies analysis is usually on the whole dataset or the current view.
    # Let's assume the "Signature" is based on the TOTAL number of transactions for this suspect.
    # If the user is filtering, we might want to skip caching OR cache based on filter signature.
    # For simplicity and robustness, let's cache based on the *current filter query* + *total data count*.
    
    total_tx_count = db.query(models.Transaction).filter(models.Transaction.suspect_id == suspect_id).count()
    current_signature = f"{total_tx_count}_{start_date or 'ALL'}_{end_date or 'ALL'}"
    
    if suspect.analysis_signature == current_signature and suspect.ai_analysis:
        return {"analysis": suspect.ai_analysis}

    # 1. Fetch Top 10 Counterparties
    cp_query = db.query(
        models.Transaction.counterparty, 
        func.sum(models.Transaction.amount).label("total")
    ).filter(models.Transaction.suspect_id == suspect_id)
    
    if start_date:
        cp_query = cp_query.filter(models.Transaction.transaction_time >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        cp_query = cp_query.filter(models.Transaction.transaction_time < end_dt)
        
    top_cps = cp_query.group_by(models.Transaction.counterparty)\
        .order_by(func.sum(models.Transaction.amount).desc())\
        .limit(10).all()
    
    if not top_cps:
        return {"analysis": "暂无足够交易数据进行分析。"}

    top_cps_str = ", ".join([f"{name}({amount:.2f})" for name, amount in top_cps])

    # 2. Day vs Night Stats
    def get_period_stats(time_range):
        q = db.query(
            models.Transaction.category,
            func.sum(models.Transaction.amount)
        ).filter(models.Transaction.suspect_id == suspect_id)
        
        if start_date:
            q = q.filter(models.Transaction.transaction_time >= datetime.strptime(start_date, "%Y-%m-%d"))
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(models.Transaction.transaction_time < end_dt)
            
        if time_range == "day":
            q = q.filter(func.strftime("%H", models.Transaction.transaction_time) >= "06")
            q = q.filter(func.strftime("%H", models.Transaction.transaction_time) <= "17")
        elif time_range == "night":
            q = q.filter(
                (func.strftime("%H", models.Transaction.transaction_time) >= "18") | 
                (func.strftime("%H", models.Transaction.transaction_time) <= "05")
            )
            
        res = q.group_by(models.Transaction.category).all()
        income = 0
        expense = 0
        for cat, amt in res:
            if cat == "收入": income = amt
            elif cat == "支出": expense = amt
        return income, expense

    day_inc, day_exp = get_period_stats("day")
    night_inc, night_exp = get_period_stats("night")

    # 3. Call Ollama (Async via Executor)
    prompt = f"""
    作为一名金融分析专家，请根据以下嫌疑人的交易数据进行简要分析，指出可能的可疑点。
    
    【数据概览】
    - 交易对象TOP10：{top_cps_str}
    - 交易时间分析：
      - 日间(06:00-18:00)总收入：{day_inc:.2f}，总支出：{day_exp:.2f}
      - 夜间(18:00-06:00)总收入：{night_inc:.2f}，总支出：{night_exp:.2f}
    
    请用简练、犀利的口吻（类似于侦探或审计专家），简短地给出你的核心点评和风险提示（200字以内）。关注大额交易或频繁交易、以及异常的交易对象，排除正常的对象（如超市购物等）。
    """
    
    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    
    # Run in separate thread pool to avoid blocking main loop and other requests
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(ai_executor, call_ollama_sync, prompt, ollama_host)
    
    if result["success"]:
        # Save to cache
        suspect.ai_analysis = result["analysis"]
        suspect.analysis_signature = current_signature
        db.commit()
        return {"analysis": result["analysis"]}
    else:
        return {"analysis": result["error"]}

@app.post("/api/set_report_path")
def set_report_path(request: ReportPathRequest, db: Session = Depends(database.get_db)):
    # Find suspect
    suspect = db.query(models.Suspect).filter(models.Suspect.id == request.suspect_id).first()
    if not suspect:
        raise HTTPException(status_code=404, detail="Suspect not found")

    path = None
    
    # Priority: Direct path
    if request.file_path:
        path = request.file_path.strip()
        # Remove quotes if user copied as path
        if (path.startswith('"') and path.endswith('"')) or (path.startswith("'") and path.endswith("'")):
            path = path[1:-1]
    
    # Fallback: Search by name (Deprecated, but kept for compatibility if needed, though user advised against it)
    # logic removed as per user instruction "search is wrong"
            
    if not path:
        raise HTTPException(status_code=400, detail="请提供有效的本地绝对路径")

    if not os.path.exists(path):
         raise HTTPException(status_code=400, detail="路径不存在 (请确保服务器有权限访问该路径)")
    
    # Determine main file
    main_file = None
    root_dir = path
    
    if os.path.isdir(path):
        # Try to find main html file
        try:
            candidates = [f for f in os.listdir(path) if f.lower().endswith('.html')]
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"无法读取目录: {str(e)}")

        # Priority 1: Contains "取证分析报告"
        for f in candidates:
            if "取证分析报告" in f:
                main_file = f
                break
        # Priority 2: index.html
        if not main_file and "index.html" in candidates:
            main_file = "index.html"
        # Priority 3: First html file
        if not main_file and candidates:
            main_file = candidates[0]
            
        if not main_file:
             raise HTTPException(status_code=400, detail="该目录下未找到 HTML 报告文件")
    else:
        root_dir = os.path.dirname(path)
        main_file = os.path.basename(path)
    
    # Save to DB
    suspect.report_path = path
    suspect.report_filename = main_file
    db.commit()
    
    return {"status": "ok", "filename": main_file, "full_path": path}

@app.get("/report_proxy/{suspect_id}/{file_path:path}")
def report_proxy(suspect_id: int, file_path: str, db: Session = Depends(database.get_db)):
    suspect = db.query(models.Suspect).filter(models.Suspect.id == suspect_id).first()
    if not suspect or not suspect.report_path:
        raise HTTPException(status_code=404, detail="Report path not set for this suspect")
    
    path = suspect.report_path
    
    # Determine root and main file (re-logic from set_report_path, or assume set_report_path stored a valid path)
    if os.path.isdir(path):
         root_dir = path
         # We assume the main file is not needed for proxying assets, but if file_path is empty/index, we might need it.
         # But the frontend usually requests specific files.
         # However, if file_path is just the filename of the main report...
    else:
         root_dir = os.path.dirname(path)
         
    # Security check
    full_path = os.path.abspath(os.path.join(root_dir, file_path))
    if not full_path.startswith(os.path.abspath(root_dir)):
         raise HTTPException(status_code=403, detail="Access denied")
    
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
        
    mime_type, _ = mimetypes.guess_type(full_path)
    if not mime_type:
        mime_type = "application/octet-stream"
    
    # Check if HTML
    is_html = mime_type.startswith("text/html") or file_path.lower().endswith(('.html', '.htm'))
    
    if is_html:
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                
            # Injection script
            script = r"""
            <script>
            (function() {
                // BillExtra Investigation Bridge
                if (window.__billExtraBridgeLoaded) return;
                window.__billExtraBridgeLoaded = true;
                
                console.log("BillExtra Bridge Active");
                const channel = new BroadcastChannel('bill_investigation_channel');

                document.addEventListener('click', function(e) {
                    let target = e.target;
                    
                    // Traverse up to find a message container (often 'm-message' or 'tr')
                    let container = target.closest('.m-message') || target.closest('tr') || target.closest('.contentitem');
                    
                    if (container) {
                       // Find ALL time strings inside this container
                       // Regex for date time: YYYY-MM-DD HH:MM:SS
                       const timeRegex = /\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}/g;
                       const matches = container.innerText.match(timeRegex);
                       
                       if (matches && matches.length > 0) {
                           matches.sort();
                           const startTime = matches[0];
                           const endTime = matches[matches.length - 1];

                           console.log("Sending time range:", startTime, "-", endTime);
                           channel.postMessage({
                               type: 'time_sync', 
                               start_time: startTime,
                               end_time: endTime
                           });
                           
                           // Visual feedback
                           let originalBg = container.style.backgroundColor;
                           let originalTransition = container.style.transition;
                           
                           container.style.transition = 'background-color 0.3s';
                           container.style.backgroundColor = 'rgba(250, 204, 21, 0.4)'; // Yellow highlight
                           
                           setTimeout(() => {
                               container.style.backgroundColor = originalBg;
                               setTimeout(() => {
                                   container.style.transition = originalTransition;
                               }, 300);
                           }, 800);
                       }
                    }
                });
            })();
            </script>
            """
            
            # Insert before </head> or </body>
            if "</head>" in content:
                content = content.replace("</head>", script + "</head>")
            elif "</body>" in content:
                content = content.replace("</body>", script + "</body>")
            else:
                content += script
                
            return HTMLResponse(content=content)
        except Exception as e:
             # Fallback if encoding fails
             return FileResponse(full_path)
             
    return FileResponse(full_path)

# --- Filesystem API for Browser ---

@app.get("/api/fs/drives")
def list_drives():
    """List available drives (Windows) or root (Linux/Mac)"""
    drives = []
    if os.name == 'nt':
        import string
        from ctypes import windll
        bitmask = windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drives.append(f"{letter}:\\")
            bitmask >>= 1
    else:
        drives.append("/")
    return drives

@app.get("/api/fs/shortcuts")
def list_shortcuts():
    """List common shortcuts (Desktop, Downloads)"""
    shortcuts = []
    home = os.path.expanduser("~")
    
    # Common candidate names for Desktop and Downloads
    desktop_names = ["Desktop", "桌面"]
    download_names = ["Downloads", "下载"]
    
    # Find Desktop
    for name in desktop_names:
        path = os.path.join(home, name)
        if os.path.exists(path) and os.path.isdir(path):
            shortcuts.append({"name": "桌面 (Desktop)", "path": path})
            break
            
    # Find Downloads
    for name in download_names:
        path = os.path.join(home, name)
        if os.path.exists(path) and os.path.isdir(path):
            shortcuts.append({"name": "下载 (Downloads)", "path": path})
            break
            
    # Add Home as well
    shortcuts.append({"name": "用户主目录 (Home)", "path": home})
    
    return shortcuts

@app.post("/api/fs/list")
def list_directory(path: str = Form(...)):
    """List contents of a directory"""
    if not os.path.exists(path):
         raise HTTPException(status_code=404, detail="Directory not found")
    if not os.path.isdir(path):
         raise HTTPException(status_code=400, detail="Not a directory")
         
    items = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                    # Filter: Only show directories or HTML files (potential reports)
                    if is_dir or entry.name.lower().endswith('.html'):
                        items.append({
                            "name": entry.name,
                            "path": entry.path,
                            "is_dir": is_dir
                        })
                except PermissionError:
                    continue
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    # Sort: Directories first, then files
    items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
    return items

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8180, reload=True)
