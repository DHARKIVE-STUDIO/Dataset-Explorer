import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Query, UploadFile
from pydantic import BaseModel, Field
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import ai
import assistant
import charts
import config
import loader
import query
import storage
from errors import ApiError
from profiling import column_stats

log = logging.getLogger("explorer")
ALLOWED_SUFFIXES = {".csv", ".tsv"}
MAX_STATS_COLUMNS = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    yield


app = FastAPI(title="Dataset Explorer", version="1.0.0", lifespan=lifespan)


def _error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


@app.exception_handler(ApiError)
async def _api_error(request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content=_error_body(exc.code, exc.message, exc.details))


@app.exception_handler(RequestValidationError)
async def _validation_error(request, exc: RequestValidationError):
    fields = [".".join(str(p) for p in e.get("loc", [])[1:]) for e in exc.errors()]
    return JSONResponse(
        status_code=422,
        content=_error_body("invalid_request", "Some request parameters are invalid.",
                            {"fields": fields, "errors": jsonable_encoder(exc.errors())}),
    )


@app.exception_handler(Exception)
async def _unexpected_error(request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content=_error_body(
        "internal_error", "Something went wrong on the server. Check the server log for details."))


def _public(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k != "file_path"}


def _dataset_or_404(dataset_id: str) -> dict:
    meta = storage.get_dataset(dataset_id)
    if meta is None:
        raise ApiError(404, "dataset_not_found", "No dataset with that id. It may have been deleted.")
    return meta


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/datasets")
def list_datasets():
    return {"datasets": [_public(m) for m in storage.list_datasets()]}


@app.post("/api/datasets", status_code=201)
def upload_dataset(file: UploadFile = File(...)):
    name = Path(file.filename or "").name.strip()
    suffix = Path(name).suffix.lower()
    if not name or suffix not in ALLOWED_SUFFIXES:
        shown = suffix or "no extension"
        raise ApiError(415, "unsupported_file_type",
                       f"Upload a .csv or .tsv file. You sent {shown}.")

    raw = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(raw) > config.MAX_UPLOAD_BYTES:
        mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise ApiError(413, "file_too_large", f"Files are limited to {mb} MB.")
    if not raw.strip():
        raise ApiError(422, "empty_file", "The file is empty.")

    text, encoding = loader.decode_bytes(raw)
    delimiter = loader.sniff_delimiter(text)
    df = loader.parse_csv(text, delimiter)
    if df.shape[1] == 0:
        raise ApiError(422, "no_columns", "No columns were found. Check the delimiter and header row.")
    if df.shape[0] == 0:
        raise ApiError(422, "no_rows", "The file has a header but no data rows.")
    if df.shape[1] > config.MAX_COLUMNS:
        raise ApiError(422, "too_many_columns",
                       f"This file has {df.shape[1]} columns. The limit is {config.MAX_COLUMNS}.")

    dataset_id = uuid.uuid4().hex
    path = config.UPLOAD_DIR / f"{dataset_id}{suffix}"
    path.write_bytes(raw)
    meta = storage.create_dataset(
        dataset_id=dataset_id, name=name[:200], file_path=str(path), size_bytes=len(raw),
        row_count=int(df.shape[0]), column_count=int(df.shape[1]),
        encoding=encoding, delimiter=delimiter,
    )
    return _public(meta)


@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str):
    return _public(_dataset_or_404(dataset_id))


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: str):
    meta = storage.delete_dataset(dataset_id)
    if meta is None:
        raise ApiError(404, "dataset_not_found", "No dataset with that id. It may have been deleted.")
    loader.forget(dataset_id)
    return {"deleted": dataset_id}


@app.get("/api/datasets/{dataset_id}/profile")
def get_profile(dataset_id: str):
    meta = _dataset_or_404(dataset_id)
    _, analysis = loader.get_analysis(meta)
    return {**analysis["profile"], "correlations": analysis["correlations"]}


@app.get("/api/datasets/{dataset_id}/rows")
def get_rows(
    dataset_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    filters: str | None = None,
    sort_by: str | None = None,
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    meta = _dataset_or_404(dataset_id)
    df, analysis = loader.get_analysis(meta)
    types = analysis["types"]
    filtered = query.apply_filters(df, types, query.parse_filters(filters))
    if sort_by:
        filtered = query.sort_frame(filtered, types, sort_by, sort_dir)
    page = filtered.iloc[offset:offset + limit]
    columns = [str(c) for c in df.columns]
    return {
        "columns": columns,
        "types": [types[c] for c in columns],
        "total": int(len(filtered)),
        "offset": offset,
        "limit": limit,
        "rows": query.to_rows(page),
    }


@app.get("/api/datasets/{dataset_id}/stats")
def get_stats(dataset_id: str, columns: str | None = None):
    meta = _dataset_or_404(dataset_id)
    df, analysis = loader.get_analysis(meta)
    types = analysis["types"]
    names = [c.strip() for c in columns.split(",") if c.strip()] if columns else list(types)
    if len(names) > MAX_STATS_COLUMNS:
        raise ApiError(400, "too_many_columns", f"Ask for at most {MAX_STATS_COLUMNS} columns at once.")
    for n in names:
        if n not in types:
            raise ApiError(400, "unknown_column", f"There is no column named '{n}'.")
    return {"stats": {n: column_stats(df, n, types[n]) for n in names}}


@app.get("/api/datasets/{dataset_id}/chart")
def get_chart(
    dataset_id: str,
    kind: str = "bar",
    x: str | None = None,
    y: str | None = None,
    agg: str = "count",
    scale: str = "auto",
    bins: int = 20,
):
    if not x:
        raise ApiError(400, "missing_column", "Pick an x column for the chart.")
    ys = [s.strip() for s in y.split(",") if s.strip()] if y else []
    meta = _dataset_or_404(dataset_id)
    df, analysis = loader.get_analysis(meta)
    return charts.build_chart(df, analysis["types"], kind=kind, x=x, ys=ys,
                              agg=agg, scale=scale, bins=bins)


class AssistantRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@app.post("/api/datasets/{dataset_id}/assistant")
def ask_assistant(dataset_id: str, body: AssistantRequest):
    meta = _dataset_or_404(dataset_id)
    _, analysis = loader.get_analysis(meta)
    return assistant.ask(analysis["profile"], analysis["correlations"], body.question.strip())


@app.get("/api/datasets/{dataset_id}/heatmap")
def get_heatmap(dataset_id: str, columns: str | None = None):
    cols = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
    meta = _dataset_or_404(dataset_id)
    df, analysis = loader.get_analysis(meta)
    return charts.build_heatmap(df, analysis["types"], columns=cols)


@app.get("/api/datasets/{dataset_id}/insights")
def get_insights(dataset_id: str, refresh: bool = False):
    meta = _dataset_or_404(dataset_id)
    _, analysis = loader.get_analysis(meta)
    return ai.get_insights(meta, analysis["profile"], analysis["correlations"], refresh=refresh)


app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")
