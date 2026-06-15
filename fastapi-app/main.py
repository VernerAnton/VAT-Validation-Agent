import os
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, date

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from shared.mcp_client import AsyncMcpClient
from shared.vba_parser import parse_vba, entries_to_vba, CountryEntry, format_rate
from shared.constants import (
    PERMANENT_REVIEW_COUNTRIES,
    SPECIAL_RATES,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_MAX_TOKENS,
)

FILE_VAULT_URL = os.environ.get("FILE_VAULT_URL", "http://localhost:3001")
WEBSEARCH_URL  = os.environ.get("WEBSEARCH_URL",  "http://localhost:3002")
SANDBOX_URL    = os.environ.get("SANDBOX_URL",    "http://localhost:3003")

_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
    yield
    await _http_client.aclose()


app = FastAPI(title="VAT Validation Agent", lifespan=lifespan)

templates = Jinja2Templates(directory="fastapi-app/templates")
app.mount("/static", StaticFiles(directory="fastapi-app/static"), name="static")


def _sandbox() -> AsyncMcpClient:
    return AsyncMcpClient(SANDBOX_URL, _http_client)

def _vault() -> AsyncMcpClient:
    return AsyncMcpClient(FILE_VAULT_URL, _http_client)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/vba")
async def get_vba():
    try:
        content = await _vault().call_tool("get_vat_file")
        return PlainTextResponse(content)
    except Exception as e:
        raise HTTPException(502, f"File vault error: {e}")


class ScanStartRequest(BaseModel):
    countries: list[str]
    test_mode: bool = False


@app.post("/api/scan/start")
async def scan_start(body: ScanStartRequest):
    job = {
        "job_id": str(uuid.uuid4()),
        "status": "pending",
        "countries": body.countries,
        "test_mode": body.test_mode,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    sb = _sandbox()
    await sb.call_tool("write_draft", {
        "filename": "scan_job.json",
        "content": json.dumps(job),
    })
    # Clear any stale progress from a previous scan
    await sb.call_tool("write_draft", {
        "filename": "validation_progress.json",
        "content": json.dumps({"status": "pending", "results": {}}),
    })
    return job


@app.get("/api/scan/status")
async def scan_status():
    try:
        raw = await _sandbox().call_tool("read_draft", {"filename": "validation_progress.json"})
        data = json.loads(raw)
        return data
    except Exception:
        return {"status": "idle"}


@app.get("/api/scan/results")
async def scan_results():
    try:
        raw = await _sandbox().call_tool("read_draft", {"filename": "scan_results.json"})
        return json.loads(raw)
    except Exception:
        raise HTTPException(404, "No results available yet")


class ReviewRequest(BaseModel):
    country: str
    action: str  # "approve" | "reject"


@app.post("/api/scan/review")
async def scan_review(body: ReviewRequest):
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be 'approve' or 'reject'")
    sb = _sandbox()
    try:
        raw = await sb.call_tool("read_draft", {"filename": "scan_results.json"})
        results = json.loads(raw)
    except Exception:
        raise HTTPException(404, "No results available")

    country_results = results.get("results", {})
    if body.country not in country_results:
        raise HTTPException(404, f"Country {body.country} not in results")

    country_results[body.country]["review_action"] = body.action
    await sb.call_tool("write_draft", {
        "filename": "scan_results.json",
        "content": json.dumps(results),
    })
    return {"ok": True, "country": body.country, "action": body.action}


@app.post("/api/scan/export")
async def scan_export():
    sb = _sandbox()
    try:
        raw_results = await sb.call_tool("read_draft", {"filename": "scan_results.json"})
        scan = json.loads(raw_results)
    except Exception:
        raise HTTPException(404, "No results available")

    try:
        raw_vba = await _vault().call_tool("get_vat_file")
        entries, header, footer = parse_vba(raw_vba)
    except Exception as e:
        raise HTTPException(502, f"Could not load VBA: {e}")

    country_results = scan.get("results", {})
    corrected: list[CountryEntry] = []
    for entry in entries:
        res = country_results.get(entry.iso_code)
        if res and not res.get("is_match") and res.get("review_action") == "approve":
            corrected.append(CountryEntry(
                index=entry.index,
                iso_code=entry.iso_code,
                name=entry.name,
                vat_rate=res["found_rate"],
            ))
        else:
            corrected.append(entry)

    vba_out = entries_to_vba(corrected, header, footer)
    return Response(
        content=vba_out,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="VAT-Database-Corrected.txt"'},
    )


@app.get("/api/logs")
async def list_logs():
    try:
        raw = await _sandbox().call_tool("list_drafts", {})
        files = [ln.strip() for ln in raw.splitlines() if ln.strip().startswith("scan_log_")]
        return sorted(files, reverse=True)
    except Exception as e:
        raise HTTPException(502, f"Sandbox error: {e}")


@app.get("/api/logs/{filename}")
async def get_log(filename: str):
    if not filename.startswith("scan_log_"):
        raise HTTPException(400, "Invalid log filename")
    try:
        content = await _sandbox().call_tool("read_draft", {"filename": filename})
        return PlainTextResponse(content)
    except Exception as e:
        raise HTTPException(404, f"Log not found: {e}")
