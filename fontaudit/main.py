"""FastAPI 应用：字体管理、审查任务、分页查询、JSON/CSV 导出。

数据（任务参数 + 结果）全部落 SQLite，服务重启后：
- 已完成的任务与结果照常可查；
- 崩溃时处于 pending/running 的任务标记为 interrupted，可 POST /tasks/{id}/rerun 重跑。
"""
from __future__ import annotations

import csv
import io
import json
import os
import uuid
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import Response

from .analyzer import run_task
from .db import Database, utcnow
from .fonts import FACE_CACHE, inspect_font
from .models import TaskCreate, parse_codepoint

EXPORT_TABLES = {
    "findings": ["id", "task_id", "chain", "kind", "text_index", "lang", "start", "end",
                 "cluster", "codepoints", "script", "font_id", "related_font_id", "detail"],
    "segments": ["id", "task_id", "chain", "text_index", "start", "end", "text", "font_id"],
    "diffs": ["id", "task_id", "text_index", "start", "end", "cluster", "codepoints",
              "script", "kind", "chain_a", "chain_b", "repro", "context"],
}


def create_app(db_path: str | None = None, fonts_dir: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("FONTAUDIT_DB", "fontaudit.db")
    fonts_dir = fonts_dir or os.environ.get("FONTAUDIT_FONTS", "fonts_store")
    Path(fonts_dir).mkdir(parents=True, exist_ok=True)
    db = Database(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 重启恢复：上次异常中断的任务标记为 interrupted，可重跑
        db.execute("UPDATE tasks SET status='interrupted' WHERE status IN ('pending','running')")
        yield

    app = FastAPI(title="字体回退链审查 API", version="1.0.0", lifespan=lifespan)
    app.state.db = db
    app.state.fonts_dir = fonts_dir

    # ---------- 工具 ----------

    def get_task_or_404(task_id: int) -> dict:
        row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise HTTPException(404, f"任务 {task_id} 不存在")
        return row

    def task_view(row: dict) -> dict:
        view = dict(row)
        view["params"] = json.loads(row["params"])
        tid = row["id"]
        kinds = db.query("SELECT kind, COUNT(*) n FROM findings WHERE task_id=? GROUP BY kind", (tid,))
        view["summary"] = {
            "findings_total": sum(r["n"] for r in kinds),
            "findings_by_kind": {r["kind"]: r["n"] for r in kinds},
            "segments": db.query_one("SELECT COUNT(*) n FROM segments WHERE task_id=?", (tid,))["n"],
            "diffs": db.query_one("SELECT COUNT(*) n FROM diffs WHERE task_id=?", (tid,))["n"],
        }
        return view

    def paged(table: str, task_id: int, page: int, page_size: int,
              filters: dict[str, object]) -> dict:
        where, args = ["task_id=?"], [task_id]
        for col, val in filters.items():
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        clause = " AND ".join(where)
        total = db.query_one(f"SELECT COUNT(*) n FROM {table} WHERE {clause}", args)["n"]
        rows = db.query(f"SELECT * FROM {table} WHERE {clause} ORDER BY id LIMIT ? OFFSET ?",
                        args + [page_size, (page - 1) * page_size])
        return {"total": total, "page": page, "page_size": page_size, "items": rows}

    # ---------- 字体管理 ----------

    @app.post("/fonts", status_code=201, summary="上传 TTF/OTF 字体")
    async def upload_font(file: UploadFile):
        data = await file.read()
        try:
            meta = inspect_font(BytesIO(data))
        except Exception as exc:
            raise HTTPException(400, f"无法解析字体文件: {exc}")
        suffix = Path(file.filename or "font").suffix or ".ttf"
        stored = f"{uuid.uuid4().hex}{suffix}"
        path = Path(fonts_dir) / stored
        path.write_bytes(data)
        font_id = db.execute(
            "INSERT INTO fonts(filename, path, format, family, subfamily, num_glyphs,"
            " num_cmap, has_cmap14, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (file.filename or stored, str(path), meta["format"], meta["family"],
             meta["subfamily"], meta["num_glyphs"], meta["num_cmap"],
             int(meta["has_cmap14"]), utcnow()))
        return db.query_one("SELECT * FROM fonts WHERE id=?", (font_id,))

    @app.get("/fonts", summary="字体列表")
    def list_fonts():
        return {"items": db.query("SELECT * FROM fonts ORDER BY id")}

    @app.get("/fonts/{font_id}", summary="字体详情")
    def get_font(font_id: int):
        row = db.query_one("SELECT * FROM fonts WHERE id=?", (font_id,))
        if not row:
            raise HTTPException(404, f"字体 {font_id} 不存在")
        return row

    @app.delete("/fonts/{font_id}", summary="删除字体")
    def delete_font(font_id: int):
        row = db.query_one("SELECT * FROM fonts WHERE id=?", (font_id,))
        if not row:
            raise HTTPException(404, f"字体 {font_id} 不存在")
        db.execute("DELETE FROM fonts WHERE id=?", (font_id,))
        FACE_CACHE.evict(font_id)
        try:
            Path(row["path"]).unlink(missing_ok=True)
        except OSError:
            pass
        return {"deleted": font_id}

    # ---------- 审查任务 ----------

    @app.post("/tasks", status_code=201, summary="创建审查任务（后台执行）")
    def create_task(payload: TaskCreate, background_tasks: BackgroundTasks):
        if not payload.corpus:
            raise HTTPException(400, "corpus 不能为空")
        if not payload.chain:
            raise HTTPException(400, "chain 不能为空")
        font_ids = set(payload.chain) | set(payload.chain_b or []) | set(payload.required_fonts.values())
        for fid in sorted(font_ids):
            if not db.query_one("SELECT id FROM fonts WHERE id=?", (fid,)):
                raise HTTPException(400, f"字体 id={fid} 不存在")
        for script in payload.required_fonts:
            if len(script) != 4 or not script.isalpha():
                raise HTTPException(400, f"非法脚本代码: {script!r}（应为 ISO 15924 四字母码）")
        try:
            allowlist = [parse_codepoint(v) for v in payload.pua_allowlist]
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        params = {
            "corpus": [item.model_dump() for item in payload.corpus],
            "chain": payload.chain,
            "chain_b": payload.chain_b,
            "normalization": payload.normalization,
            "ignore_pua": payload.ignore_pua,
            "pua_allowlist": allowlist,
            "required_fonts": payload.required_fonts,
        }
        task_id = db.execute(
            "INSERT INTO tasks(name, params, status, created_at) VALUES (?,?,?,?)",
            (payload.name, json.dumps(params, ensure_ascii=False), "pending", utcnow()))
        background_tasks.add_task(run_task, db, task_id)
        return task_view(get_task_or_404(task_id))

    @app.get("/tasks", summary="任务列表（分页）")
    def list_tasks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=500)):
        total = db.query_one("SELECT COUNT(*) n FROM tasks")["n"]
        rows = db.query("SELECT * FROM tasks ORDER BY id DESC LIMIT ? OFFSET ?",
                        (page_size, (page - 1) * page_size))
        return {"total": total, "page": page, "page_size": page_size,
                "items": [task_view(r) for r in rows]}

    @app.get("/tasks/{task_id}", summary="任务详情（含结果汇总）")
    def get_task(task_id: int):
        return task_view(get_task_or_404(task_id))

    @app.post("/tasks/{task_id}/rerun", status_code=202, summary="重跑任务")
    def rerun_task(task_id: int, background_tasks: BackgroundTasks):
        row = get_task_or_404(task_id)
        if row["status"] not in ("done", "failed", "interrupted"):
            raise HTTPException(409, f"任务状态为 {row['status']}，不能重跑")
        for table in EXPORT_TABLES:
            db.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
        db.execute("UPDATE tasks SET status='pending', started_at=NULL, finished_at=NULL,"
                   " error=NULL WHERE id=?", (task_id,))
        background_tasks.add_task(run_task, db, task_id)
        return task_view(get_task_or_404(task_id))

    @app.delete("/tasks/{task_id}", summary="删除任务及其结果")
    def delete_task(task_id: int):
        get_task_or_404(task_id)
        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return {"deleted": task_id}

    # ---------- 结果查询 ----------

    @app.get("/tasks/{task_id}/findings", summary="问题列表（分页，可按类型/脚本/链过滤）")
    def list_findings(task_id: int,
                      kind: str | None = None, script: str | None = None,
                      chain: str | None = None, text_index: int | None = None,
                      page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
        get_task_or_404(task_id)
        return paged("findings", task_id, page, page_size,
                     {"kind": kind, "script": script, "chain": chain, "text_index": text_index})

    @app.get("/tasks/{task_id}/segments", summary="字体分段（每段文本实际采用的字体）")
    def list_segments(task_id: int, chain: str | None = None, text_index: int | None = None,
                      font_id: int | None = None,
                      page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
        get_task_or_404(task_id)
        return paged("segments", task_id, page, page_size,
                     {"chain": chain, "text_index": text_index, "font_id": font_id})

    @app.get("/tasks/{task_id}/diffs", summary="两条回退链的差异（分页）")
    def list_diffs(task_id: int, kind: str | None = None, text_index: int | None = None,
                   page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
        get_task_or_404(task_id)
        return paged("diffs", task_id, page, page_size,
                     {"kind": kind, "text_index": text_index})

    @app.get("/tasks/{task_id}/export", summary="导出结果为 JSON 或 CSV")
    def export(task_id: int, what: str = "findings", format: str = "json"):
        get_task_or_404(task_id)
        if what not in EXPORT_TABLES:
            raise HTTPException(400, f"what 必须是 {sorted(EXPORT_TABLES)} 之一")
        cols = EXPORT_TABLES[what]
        rows = db.query(f"SELECT * FROM {what} WHERE task_id=? ORDER BY id", (task_id,))
        filename = f"task{task_id}_{what}"
        if format == "json":
            return Response(json.dumps(rows, ensure_ascii=False, indent=1),
                            media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{filename}.json"'})
        if format == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            return Response(buf.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})
        raise HTTPException(400, "format 必须是 json 或 csv")

    @app.get("/", summary="服务信息")
    def root():
        return {"service": "font-fallback-audit", "version": app.version,
                "issue_kinds": ["missing_glyph", "notdef", "cluster_split", "zwj_broken",
                                "variation_lost", "required_font_mismatch", "normalization"]}

    return app


app = create_app()
