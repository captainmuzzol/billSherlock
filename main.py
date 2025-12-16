from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Query, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
import shutil
import os
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

# Pydantic Models
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
            "last_update": last_tx.transaction_time if last_tx else s.created_at
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
        query = query.filter(models.Transaction.transaction_time >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        # Make end_date inclusive by moving to the next day and using <
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(models.Transaction.transaction_time < end_dt)
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
        query = query.filter(models.Transaction.transaction_time >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(models.Transaction.transaction_time < end_dt)
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
        query = query.filter(models.Transaction.transaction_time >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(models.Transaction.transaction_time < end_dt)
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
        query = query.filter(models.Transaction.transaction_time >= datetime.strptime(start_date, "%Y-%m-%d"))
    if end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(models.Transaction.transaction_time < end_dt)
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8180, reload=True)
