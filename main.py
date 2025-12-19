from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Query, Form, BackgroundTasks
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
import asyncio
import subprocess
import uuid
import zipfile
import json
import threading
import aiofiles
import tempfile
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

REPORTS_DIR = os.path.abspath(os.path.join(os.getcwd(), "forensic_reports"))
os.makedirs(REPORTS_DIR, exist_ok=True)
REPORT_UPLOAD_SEMAPHORE = asyncio.Semaphore(2)
REPORT_ACCESS_LOG_PATH = os.path.join(REPORTS_DIR, "report_access.json")
REPORT_ACCESS_LOCK = threading.Lock()
ARCHIVE_EXTRACT_TIMEOUT_SECONDS = int(os.getenv("ARCHIVE_EXTRACT_TIMEOUT_SECONDS", "500"))

REPORT_UPLOAD_JOBS: dict[str, dict] = {}
REPORT_UPLOAD_JOBS_LOCK = threading.Lock()

BILL_UPLOAD_SEMAPHORE = asyncio.Semaphore(int(os.getenv("BILL_UPLOAD_CONCURRENCY", "1")))
BILL_UPLOAD_JOBS: dict[str, dict] = {}
BILL_UPLOAD_JOBS_LOCK = threading.Lock()

def _write_json_atomic(path: str, payload: dict):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, path)

def _write_bill_job_result(job_id: str, suspect_id: int, job: dict):
    safe_job_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(job_id or ""))
    safe_suspect_id = re.sub(r"[^0-9]", "", str(suspect_id or "0")) or "0"
    filename = f"bill_upload_result_{safe_suspect_id}_{safe_job_id}.json"
    path = os.path.abspath(os.path.join(os.getcwd(), filename))
    _write_json_atomic(path, job or {})
    return filename

def _set_report_job(job_id: str, patch: dict):
    if not job_id:
        return
    with REPORT_UPLOAD_JOBS_LOCK:
        current = REPORT_UPLOAD_JOBS.get(job_id) or {}
        merged = dict(current)
        merged.update(patch or {})
        REPORT_UPLOAD_JOBS[job_id] = merged

def _get_report_job(job_id: str):
    with REPORT_UPLOAD_JOBS_LOCK:
        job = REPORT_UPLOAD_JOBS.get(job_id)
        if not job:
            return None
        return dict(job)

def _set_bill_job(job_id: str, patch: dict):
    if not job_id:
        return
    with BILL_UPLOAD_JOBS_LOCK:
        current = BILL_UPLOAD_JOBS.get(job_id) or {}
        merged = dict(current)
        merged.update(patch or {})
        BILL_UPLOAD_JOBS[job_id] = merged

def _get_bill_job(job_id: str):
    with BILL_UPLOAD_JOBS_LOCK:
        job = BILL_UPLOAD_JOBS.get(job_id)
        if not job:
            return None
        return dict(job)

def _chunk_list(items: list, size: int):
    if size <= 0:
        size = 1
    for i in range(0, len(items), size):
        yield items[i : i + size]

def _insert_transactions_for_suspect(db: Session, suspect_id: int, source_filename: str, data: list[dict]):
    tx_ids = []
    for item in data:
        tid = item.get("transaction_id") if isinstance(item, dict) else None
        if tid:
            tx_ids.append(str(tid))

    unique_ids = list(dict.fromkeys(tx_ids))
    existing: set[str] = set()
    for chunk in _chunk_list(unique_ids, 900):
        rows = (
            db.query(models.Transaction.transaction_id)
            .filter(models.Transaction.suspect_id == suspect_id, models.Transaction.transaction_id.in_(chunk))
            .all()
        )
        for r in rows:
            if r and r[0]:
                existing.add(str(r[0]))

    to_insert = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tid = item.get("transaction_id")
        if not tid:
            continue
        if str(tid) in existing:
            continue
        to_insert.append(models.Transaction(**item, source_file=source_filename, suspect_id=suspect_id))

    if to_insert:
        db.add_all(to_insert)
    db.commit()
    return len(to_insert)

async def _process_bill_upload_job(job_id: str, suspect_id: int, stored_files: list[dict], job_dir: str):
    _set_bill_job(job_id, {"status": "queued", "updated_at": datetime.now().isoformat(timespec="seconds")})
    await BILL_UPLOAD_SEMAPHORE.acquire()
    try:
        _set_bill_job(job_id, {"status": "processing", "updated_at": datetime.now().isoformat(timespec="seconds")})
        db = database.SessionLocal()
        try:
            suspect = db.query(models.Suspect).filter(models.Suspect.id == suspect_id).first()
            if not suspect:
                _set_bill_job(job_id, {"status": "error", "detail": "Suspect not found", "updated_at": datetime.now().isoformat(timespec="seconds")})
                return

            results = []
            total_files = len(stored_files or [])
            for idx, f in enumerate(stored_files or []):
                filename = (f.get("filename") or "").strip() if isinstance(f, dict) else ""
                fpath = (f.get("path") or "").strip() if isinstance(f, dict) else ""
                _set_bill_job(
                    job_id,
                    {
                        "current_filename": filename,
                        "current_file_index": idx + 1,
                        "total_files": total_files,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    },
                )

                try:
                    data = await asyncio.to_thread(parser.parse_bill_file, fpath)
                    times = []
                    for item in data or []:
                        if isinstance(item, dict):
                            t = item.get("transaction_time")
                            if t:
                                times.append(t)
                    min_time = min(times) if times else None
                    max_time = max(times) if times else None
                    distinct_days = set()
                    for t in times:
                        try:
                            distinct_days.add(t.date().isoformat())
                        except Exception:
                            continue
                    inserted = _insert_transactions_for_suspect(db, suspect_id, filename, data or [])
                    results.append(
                        {
                            "filename": filename,
                            "parsed_count": len(data or []),
                            "inserted_count": inserted,
                            "min_time": min_time.isoformat(timespec="seconds") if min_time else None,
                            "max_time": max_time.isoformat(timespec="seconds") if max_time else None,
                            "distinct_days": len(distinct_days),
                            "diagnostics_file": None,
                        }
                    )
                except Exception as e:
                    db.rollback()
                    results.append({"filename": filename, "error": str(e)})
                finally:
                    try:
                        if fpath and os.path.exists(fpath):
                            os.remove(fpath)
                    except Exception:
                        pass

                _set_bill_job(job_id, {"results": list(results), "updated_at": datetime.now().isoformat(timespec="seconds")})

            _set_bill_job(
                job_id,
                {
                    "status": "done",
                    "results": list(results),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
            try:
                final_job = _get_bill_job(job_id) or {}
                out_file = _write_bill_job_result(job_id, suspect_id, final_job)
                _set_bill_job(job_id, {"output_file": out_file, "updated_at": datetime.now().isoformat(timespec="seconds")})
            except Exception:
                pass
        finally:
            db.close()
    except Exception as e:
        _set_bill_job(job_id, {"status": "error", "detail": str(e), "updated_at": datetime.now().isoformat(timespec="seconds")})
        try:
            final_job = _get_bill_job(job_id) or {}
            out_file = _write_bill_job_result(job_id, suspect_id, final_job)
            _set_bill_job(job_id, {"output_file": out_file, "updated_at": datetime.now().isoformat(timespec="seconds")})
        except Exception:
            pass
    finally:
        BILL_UPLOAD_SEMAPHORE.release()
        try:
            await asyncio.to_thread(_delete_tree, job_dir)
        except Exception:
            pass

def _load_report_access_unlocked():
    if not os.path.exists(REPORT_ACCESS_LOG_PATH):
        return {}
    try:
        with open(REPORT_ACCESS_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}

def _write_report_access_unlocked(data: dict):
    os.makedirs(os.path.dirname(REPORT_ACCESS_LOG_PATH), exist_ok=True)
    tmp_path = REPORT_ACCESS_LOG_PATH + ".tmp"
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp_path, REPORT_ACCESS_LOG_PATH)

def _update_report_access(report_root: str, opened_at: Optional[datetime] = None):
    if not report_root:
        return
    if opened_at is None:
        opened_at = datetime.now()
    ts = opened_at.isoformat(timespec="seconds")
    with REPORT_ACCESS_LOCK:
        data = _load_report_access_unlocked()
        data[os.path.abspath(report_root)] = ts
        _write_report_access_unlocked(data)

def _remove_report_access(report_root: str):
    if not report_root:
        return
    with REPORT_ACCESS_LOCK:
        data = _load_report_access_unlocked()
        data.pop(os.path.abspath(report_root), None)
        _write_report_access_unlocked(data)

def _cleanup_stale_reports(max_age_days: int = 30):
    cutoff = datetime.now() - timedelta(days=max_age_days)
    to_delete: list[str] = []
    with REPORT_ACCESS_LOCK:
        data = _load_report_access_unlocked()
        new_data: dict[str, str] = {}
        for raw_path, raw_ts in data.items():
            report_root = os.path.abspath(str(raw_path))
            ts = str(raw_ts)

            if not _is_within_reports_dir(report_root):
                continue
            if not os.path.exists(report_root):
                continue

            try:
                last_opened = datetime.fromisoformat(ts)
            except Exception:
                last_opened = datetime.now()

            if last_opened < cutoff:
                to_delete.append(report_root)
                continue

            new_data[report_root] = ts

        _write_report_access_unlocked(new_data)

    delete_targets = []
    seen = set()
    for path in to_delete:
        if not _is_within_reports_dir(path):
            continue
        target = _get_report_container_dir(path)
        if target in seen:
            continue
        seen.add(target)
        delete_targets.append(target)

    for target in delete_targets:
        if _is_within_reports_dir(target):
            _delete_tree(target)

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

class AdminPurgeReportsRequest(BaseModel):
    confirm: str

class ReportsStatsResponse(BaseModel):
    suspect_dirs: int
    report_versions: int
    total_files: int
    total_bytes: int
    last_modified: Optional[str] = None

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("static/favicon.ico")

@app.get("/")
def read_root():
    return RedirectResponse(url="/static/index.html")

def _get_reports_stats():
    suspect_dirs = 0
    report_versions = 0
    total_files = 0
    total_bytes = 0
    last_mtime: Optional[float] = None

    try:
        entries = [e for e in os.listdir(REPORTS_DIR) if e and not e.startswith(".")]
    except Exception:
        entries = []

    for name in entries:
        if name == os.path.basename(REPORT_ACCESS_LOG_PATH) or name.endswith(".tmp"):
            continue
        full = os.path.join(REPORTS_DIR, name)
        if os.path.isdir(full):
            suspect_dirs += 1
            try:
                versions = [v for v in os.listdir(full) if v and not v.startswith(".") and os.path.isdir(os.path.join(full, v))]
            except Exception:
                versions = []
            report_versions += len(versions)

    for root, _, files in os.walk(REPORTS_DIR):
        for fname in files:
            if fname == os.path.basename(REPORT_ACCESS_LOG_PATH) or fname.endswith(".tmp"):
                continue
            fpath = os.path.join(root, fname)
            try:
                st = os.stat(fpath)
                total_files += 1
                total_bytes += int(st.st_size)
                if last_mtime is None or st.st_mtime > last_mtime:
                    last_mtime = float(st.st_mtime)
            except Exception:
                continue

    last_modified = None
    if last_mtime is not None:
        try:
            last_modified = datetime.fromtimestamp(last_mtime).isoformat(timespec="seconds")
        except Exception:
            last_modified = None

    return {
        "suspect_dirs": suspect_dirs,
        "report_versions": report_versions,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "last_modified": last_modified,
    }

def _purge_all_reports(background_tasks: BackgroundTasks, db: Session):
    db.query(models.Suspect).update({
        models.Suspect.report_path: None,
        models.Suspect.report_filename: None,
    })
    db.commit()

    with REPORT_ACCESS_LOCK:
        _write_report_access_unlocked({})

    try:
        entries = [e for e in os.listdir(REPORTS_DIR) if e and not e.startswith(".")]
    except Exception:
        entries = []

    for name in entries:
        if name == os.path.basename(REPORT_ACCESS_LOG_PATH) or name.endswith(".tmp"):
            continue
        full = os.path.join(REPORTS_DIR, name)
        if _is_within_reports_dir(full):
            background_tasks.add_task(_delete_tree, full)

@app.get("/api/admin/reports/stats", response_model=ReportsStatsResponse)
def admin_reports_stats():
    return _get_reports_stats()

@app.post("/api/admin/reports/purge")
def admin_reports_purge(
    req: AdminPurgeReportsRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db),
):
    if (req.confirm or "").strip() != "DELETE_ALL_REPORTS":
        raise HTTPException(status_code=400, detail="确认口令不正确")

    _purge_all_reports(background_tasks, db)
    return {"status": "ok"}

@app.get("/admin", include_in_schema=False)
def admin_page():
    html = """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>管理 - 账单神探</title>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,PingFang SC,Hiragino Sans GB,Microsoft YaHei,sans-serif;background:#0b1220;color:#e5e7eb;margin:0}
    .wrap{max-width:900px;margin:0 auto;padding:28px 18px}
    .card{background:#111827;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:18px}
    .muted{color:rgba(229,231,235,.7)}
    .row{display:flex;gap:14px;flex-wrap:wrap}
    .btn{appearance:none;border:1px solid rgba(255,255,255,.12);background:#0f172a;color:#e5e7eb;border-radius:10px;padding:10px 12px;cursor:pointer}
    .btn:hover{background:#111b33}
    .btn-danger{background:#7f1d1d;border-color:rgba(255,255,255,.15)}
    .btn-danger:hover{background:#991b1b}
    .input{width:100%;box-sizing:border-box;border:1px solid rgba(255,255,255,.15);background:#0b1020;color:#e5e7eb;border-radius:10px;padding:10px 12px;outline:none}
    .k{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}
    a{color:#93c5fd;text-decoration:none}
    a:hover{text-decoration:underline}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"row\" style=\"align-items:center;justify-content:space-between;margin-bottom:14px\">
      <div>
        <div style=\"font-size:18px;font-weight:700\">管理 - 取证报告</div>
        <div class=\"muted\" style=\"margin-top:6px\">此页面可删除后台所有已上传的取证报告文件（不可恢复）。</div>
      </div>
      <div class=\"row\" style=\"align-items:center\">
        <a href=\"/\" class=\"muted\">返回系统</a>
      </div>
    </div>

    <div class=\"card\">
      <div class=\"row\" style=\"justify-content:space-between\">
        <div>
          <div class=\"muted\">当前报告占用</div>
          <div id=\"stats\" style=\"margin-top:8px\">加载中...</div>
        </div>
        <div>
          <button id=\"refresh\" class=\"btn\">刷新</button>
        </div>
      </div>

      <div style=\"height:14px\"></div>

      <div class=\"muted\">强确认：在下方输入 <span class=\"k\">DELETE_ALL_REPORTS</span> 后才能执行删除。</div>
      <div style=\"height:8px\"></div>
      <input id=\"confirm\" class=\"input k\" placeholder=\"DELETE_ALL_REPORTS\" />
      <div style=\"height:12px\"></div>
      <button id=\"purge\" class=\"btn btn-danger\" disabled>删除后台所有已上传的报告文件</button>
      <div id=\"msg\" class=\"muted\" style=\"margin-top:12px\"></div>
    </div>
  </div>

  <script>
    const statsEl = document.getElementById('stats');
    const msgEl = document.getElementById('msg');
    const confirmEl = document.getElementById('confirm');
    const purgeBtn = document.getElementById('purge');
    const refreshBtn = document.getElementById('refresh');

    const formatBytes = (n) => {
      if (!Number.isFinite(n)) return '0 B';
      const units = ['B','KB','MB','GB','TB'];
      let v = n;
      let i = 0;
      while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
      }
      const fixed = i === 0 ? 0 : 2;
      return v.toFixed(fixed) + ' ' + units[i];
    };

    const setMsg = (t) => {
      msgEl.textContent = t || '';
    };

    const loadStats = async () => {
      setMsg('');
      statsEl.textContent = '加载中...';
      const res = await fetch('/api/admin/reports/stats');
      if (!res.ok) {
        statsEl.textContent = '加载失败';
        return;
      }
      const data = await res.json();
      const parts = [];
      parts.push('嫌疑人目录: ' + (data.suspect_dirs ?? 0));
      parts.push('报告版本: ' + (data.report_versions ?? 0));
      parts.push('文件数: ' + (data.total_files ?? 0));
      parts.push('占用: ' + formatBytes(data.total_bytes ?? 0));
      if (data.last_modified) parts.push('最近写入: ' + data.last_modified);
      statsEl.innerHTML = '<div class=\"k\">' + parts.join('<br/>') + '</div>';
    };

    const syncConfirm = () => {
      const ok = (confirmEl.value || '').trim() === 'DELETE_ALL_REPORTS';
      purgeBtn.disabled = !ok;
    };

    confirmEl.addEventListener('input', syncConfirm);
    refreshBtn.addEventListener('click', () => loadStats());
    purgeBtn.addEventListener('click', async () => {
      if (purgeBtn.disabled) return;
      purgeBtn.disabled = true;
      setMsg('正在提交删除任务...');
      const res = await fetch('/api/admin/reports/purge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: 'DELETE_ALL_REPORTS' })
      });
      if (!res.ok) {
        let detail = '删除失败';
        try {
          const payload = await res.json();
          detail = payload.detail || detail;
        } catch (_) {}
        setMsg(detail);
        syncConfirm();
        return;
      }
      setMsg('已触发删除（后台执行），请稍后刷新查看占用变化。');
      confirmEl.value = '';
      syncConfirm();
      await loadStats();
    });

    syncConfirm();
    loadStats();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)

@app.post("/suspects", response_model=SuspectRead)
def create_suspect(
    suspect: SuspectCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(database.get_db)
):
    if len(suspect.password) < 3:
        raise HTTPException(status_code=400, detail="Password must be at least 3 characters")
        
    existing = db.query(models.Suspect).filter(models.Suspect.name == suspect.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Suspect with this name already exists")
        
    db_suspect = models.Suspect(name=suspect.name, password=suspect.password)
    db.add(db_suspect)
    db.commit()
    db.refresh(db_suspect)

    if background_tasks:
        background_tasks.add_task(_cleanup_stale_reports, 30)
    else:
        _cleanup_stale_reports(30)

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
async def upload_file(
    suspect_id: int = Form(...),
    files: List[UploadFile] = File(...),
    db: Session = Depends(database.get_db),
):
    suspect = db.query(models.Suspect).filter(models.Suspect.id == suspect_id).first()
    if not suspect:
        raise HTTPException(status_code=404, detail="Suspect not found")

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    job_id = uuid.uuid4().hex
    job_dir = tempfile.mkdtemp(prefix=f"bill_upload_{suspect_id}_{job_id}_")
    stored_files = []

    for f in files:
        filename = os.path.basename((f.filename or "").strip()) or "upload"
        file_path = os.path.join(job_dir, filename)

        async with aiofiles.open(file_path, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                await out.write(chunk)

        stored_files.append({"filename": filename, "path": file_path})

    _set_bill_job(
        job_id,
        {
            "status": "queued",
            "suspect_id": suspect_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "total_files": len(stored_files),
            "current_file_index": 0,
            "current_filename": None,
            "results": [],
        },
    )
    asyncio.create_task(_process_bill_upload_job(job_id, suspect_id, stored_files, job_dir))
    return {"status": "accepted", "job_id": job_id}

@app.get("/api/bill/upload_status")
def bill_upload_status(job_id: str = Query(...)):
    job = _get_bill_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job

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
        raise HTTPException(status_code=503, detail=result["error"])

def _safe_extract_zip(archive_path: str, dest_dir: str):
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            target_path = os.path.abspath(os.path.join(dest_dir, member.filename))
            if not target_path.startswith(os.path.abspath(dest_dir) + os.sep):
                raise HTTPException(status_code=400, detail="压缩包内容不合法")
        zf.extractall(dest_dir)

def _find_rar_extract_tool():
    preferred = []
    bz = shutil.which("bz") or shutil.which("bz.exe")
    if bz:
        preferred.append(("bz", bz))

    seven_zip = shutil.which("7z") or shutil.which("7za")
    if seven_zip:
        preferred.append(("7z", seven_zip))

    unrar = shutil.which("unrar")
    if unrar:
        preferred.append(("unrar", unrar))

    if preferred:
        return preferred[0]

    if os.name == "nt":
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Bandizip", "bz.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Bandizip", "bz.exe"),
        ]
        for p in candidates:
            if os.path.exists(p):
                return ("bz", p)

    return (None, None)

def _extract_archive(archive_path: str, dest_dir: str):
    os.makedirs(dest_dir, exist_ok=True)
    if zipfile.is_zipfile(archive_path):
        _safe_extract_zip(archive_path, dest_dir)
        return

    lower = archive_path.lower()
    if lower.endswith(".rar"):
        kind, tool = _find_rar_extract_tool()
        if not tool:
            raise HTTPException(
                status_code=400,
                detail="服务器缺少解压工具（7z/unrar/bz.exe），请安装 7-Zip 或将 Bandizip 安装目录加入 PATH（确保可直接运行 bz.exe），或上传 zip 格式压缩包",
            )

        if kind == "unrar" or os.path.basename(tool).lower().startswith("unrar"):
            cmd = [tool, "x", "-y", archive_path, dest_dir]
        elif kind == "bz" or os.path.basename(tool).lower() in {"bz.exe", "bz"}:
            cmd = [tool, "x", "-y", "-aoa", f"-o:{dest_dir}", archive_path]
        else:
            cmd = [tool, "x", "-y", f"-o{dest_dir}", archive_path]

        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=ARCHIVE_EXTRACT_TIMEOUT_SECONDS,
                creationflags=(subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0),
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=400, detail="解压超时，请确认压缩包大小与内容")
        except subprocess.CalledProcessError:
            raise HTTPException(status_code=400, detail="解压失败，请确认压缩包格式正确")
        return

    raise HTTPException(status_code=400, detail="仅支持上传 zip 或 rar 格式压缩包")

def _detect_report_root(base_dir: str):
    try:
        entries = [e for e in os.listdir(base_dir) if e and not e.startswith(".")]
    except Exception:
        return base_dir

    if not entries:
        return base_dir

    dirs = [e for e in entries if os.path.isdir(os.path.join(base_dir, e))]
    files = [e for e in entries if os.path.isfile(os.path.join(base_dir, e))]
    has_html_at_root = any(f.lower().endswith((".html", ".htm")) for f in files)
    if len(dirs) == 1 and not has_html_at_root:
        return os.path.join(base_dir, dirs[0])
    return base_dir

def _find_main_html(report_root: str):
    candidates = []
    for root, _, files in os.walk(report_root):
        for name in files:
            lower = name.lower()
            if lower.endswith(".html") or lower.endswith(".htm"):
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, report_root)
                candidates.append(rel_path)

    if not candidates:
        raise HTTPException(status_code=400, detail="压缩包内未找到 HTML 报告文件")

    for rel in candidates:
        if "取证分析报告" in rel:
            return rel

    for rel in candidates:
        if os.path.basename(rel).lower() == "index.html":
            return rel

    return sorted(candidates, key=lambda p: p.lower())[0]

def _delete_tree(path: str):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        return

def _get_report_container_dir(report_root: str):
    try:
        abs_root = os.path.abspath(report_root)
        if not abs_root.startswith(REPORTS_DIR + os.sep):
            return abs_root
        rel = os.path.relpath(abs_root, REPORTS_DIR)
        parts = rel.split(os.sep)
        if len(parts) >= 2:
            return os.path.join(REPORTS_DIR, parts[0], parts[1])
        return abs_root
    except Exception:
        return report_root

def _is_within_reports_dir(path: str):
    try:
        abs_path = os.path.abspath(path)
        return abs_path.startswith(REPORTS_DIR + os.sep)
    except Exception:
        return False

async def _process_report_upload_job(job_id: str, suspect_id: int, archive_path: str, work_dir: str):
    _set_report_job(job_id, {"status": "queued", "updated_at": datetime.now().isoformat(timespec="seconds")})
    await REPORT_UPLOAD_SEMAPHORE.acquire()
    try:
        _set_report_job(job_id, {"status": "processing", "updated_at": datetime.now().isoformat(timespec="seconds")})
        db = database.SessionLocal()
        try:
            suspect = db.query(models.Suspect).filter(models.Suspect.id == suspect_id).first()
            if not suspect:
                _set_report_job(job_id, {"status": "error", "detail": "Suspect not found", "updated_at": datetime.now().isoformat(timespec="seconds")})
                return

            await asyncio.to_thread(_extract_archive, archive_path, work_dir)
            await asyncio.to_thread(_delete_tree, archive_path)

            report_root = await asyncio.to_thread(_detect_report_root, work_dir)
            main_rel = (await asyncio.to_thread(_find_main_html, report_root)).replace("\\", "/")

            old_report_root = suspect.report_path
            suspect.report_path = report_root
            suspect.report_filename = main_rel
            db.commit()

            if old_report_root and old_report_root != report_root and _is_within_reports_dir(old_report_root):
                await asyncio.to_thread(_remove_report_access, old_report_root)
                old_delete_target = _get_report_container_dir(old_report_root)
                await asyncio.to_thread(_delete_tree, old_delete_target)

            await asyncio.to_thread(_update_report_access, report_root)

            _set_report_job(
                job_id,
                {
                    "status": "done",
                    "filename": main_rel,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
        finally:
            db.close()
    except HTTPException as e:
        _set_report_job(
            job_id,
            {
                "status": "error",
                "detail": getattr(e, "detail", "服务器处理失败"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        await asyncio.to_thread(_delete_tree, work_dir)
    except Exception as e:
        _set_report_job(
            job_id,
            {
                "status": "error",
                "detail": str(e),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        await asyncio.to_thread(_delete_tree, work_dir)
    finally:
        REPORT_UPLOAD_SEMAPHORE.release()

@app.post("/api/report/upload")
async def upload_report(
    background_tasks: BackgroundTasks,
    suspect_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db)
):
    suspect = db.query(models.Suspect).filter(models.Suspect.id == suspect_id).first()
    if not suspect:
        raise HTTPException(status_code=404, detail="Suspect not found")

    filename = (file.filename or "").strip()
    lower = filename.lower()
    if not (lower.endswith(".zip") or lower.endswith(".rar")):
        if lower.endswith((".html", ".htm")):
            raise HTTPException(status_code=400, detail="请上传取证报告压缩包（zip/rar），不支持直接上传 HTML")
        raise HTTPException(status_code=400, detail="仅支持上传 zip 或 rar 格式压缩包")

    report_version = uuid.uuid4().hex
    work_dir = os.path.join(REPORTS_DIR, str(suspect_id), report_version)
    os.makedirs(work_dir, exist_ok=True)

    archive_path = os.path.join(work_dir, filename)
    async with aiofiles.open(archive_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            await out.write(chunk)

    job_id = uuid.uuid4().hex
    _set_report_job(
        job_id,
        {
            "status": "queued",
            "suspect_id": suspect_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    asyncio.create_task(_process_report_upload_job(job_id, suspect_id, archive_path, work_dir))
    return {"status": "accepted", "job_id": job_id}

@app.get("/api/report/upload_status")
def report_upload_status(job_id: str = Query(...)):
    job = _get_report_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job

@app.post("/api/set_report_path")
def set_report_path(request: ReportPathRequest, db: Session = Depends(database.get_db)):
    raise HTTPException(status_code=400, detail="服务器部署场景不支持选择本地路径，请上传取证报告压缩包（zip/rar）")

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

    try:
        if suspect.report_filename and file_path.replace("\\", "/") == suspect.report_filename.replace("\\", "/"):
            _update_report_access(path)
    except Exception:
        pass
        
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8180, reload=True)
