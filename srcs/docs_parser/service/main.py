"""Small internal API around the GPU document parser.

The service accepts PDFs, parses one at a time, and keeps job state in SQLite.
Parsing stays asynchronous because OCR and figure captioning can take minutes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import fitz
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel

from .store import UPLOAD_DIR, Job, JobStatus, JobStore
from .worker import GpuWorker

log = logging.getLogger("docparser.service")

MAX_QUEUE = int(os.getenv("DOCPARSER_MAX_QUEUE", "8"))
MAX_UPLOAD_BYTES = int(os.getenv("DOCPARSER_MAX_UPLOAD_MB", "100")) * 1024 * 1024
MAX_JOBS = int(os.getenv("DOCPARSER_MAX_JOBS", "500"))
RESULT_TTL = int(os.getenv("DOCPARSER_RESULT_TTL", "86400"))

store = JobStore()
worker = GpuWorker(store)
tasks: set[asyncio.Task] = set()


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus
    queue_position: int


class JobState(BaseModel):
    job_id: str
    filename: str
    status: JobStatus
    duration_seconds: float | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class Health(BaseModel):
    status: str
    queued: int
    running: str | None
    models_ready: bool
    error: str | None = None


def spawn(job: Job) -> None:
    task = asyncio.create_task(worker.run(job))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store.prune(MAX_JOBS, RESULT_TTL)

    for job in store.recover():
        worker.pending += 1
        spawn(job)

    warmup = asyncio.create_task(worker.warm())
    tasks.add(warmup)
    warmup.add_done_callback(tasks.discard)

    yield
    worker.shutdown()
    store.close()


app = FastAPI(
    title="Safaqat Document Parser",
    version="0.2.0",
    description="PDF to RAG-ready Markdown and JSON with multilingual OCR.",
    lifespan=lifespan,
)


@app.get("/health", response_model=Health)
def health(response: Response) -> Health:
    if worker.warmup_error:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        service_status = "error"
    elif not worker.models_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        service_status = "warming"
    else:
        service_status = "ok"
    return Health(
        status=service_status,
        queued=max(0, worker.pending - (1 if worker.running else 0)),
        running=worker.running,
        models_ready=worker.models_ready,
        error=worker.warmup_error,
    )


@app.post("/parse", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def parse_pdf(
    file: UploadFile = File(..., description="Tender document in PDF format"),
    max_pages: int | None = Form(default=None, ge=1),
) -> JobAccepted:
    if not worker.models_ready:
        detail = (
            "The GPU parser is unavailable"
            if worker.warmup_error
            else "The GPU parser is still warming its models"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
            headers={"Retry-After": "60"},
        )
    if worker.pending >= MAX_QUEUE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The parser queue is full; retry shortly",
            headers={"Retry-After": "60"},
        )

    filename = file.filename or "document.pdf"
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF documents are supported",
        )

    worker.pending += 1
    job_id = uuid.uuid4().hex[:12]
    upload_path = UPLOAD_DIR / f"{job_id}.pdf"

    try:
        await receive_pdf(file, upload_path)
        validate_pdf(upload_path)
    except BaseException:
        worker.pending -= 1
        upload_path.unlink(missing_ok=True)
        raise

    job = Job(
        id=job_id,
        filename=filename,
        pdf_path=str(upload_path),
        max_pages=max_pages,
    )
    store.save(job)
    spawn(job)
    log.info("job %s queued: %s", job.id, job.filename)

    return JobAccepted(
        job_id=job.id,
        status=job.status,
        queue_position=max(0, worker.pending - 1),
    )


@app.get("/jobs/{job_id}", response_model=JobState)
def job_state(job_id: str, include_result: bool = True) -> JobState:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parse job not found",
        )

    return JobState(
        job_id=job.id,
        filename=job.filename,
        status=job.status,
        duration_seconds=job.duration,
        error=job.error,
        result=job.result if include_result else None,
    )


async def receive_pdf(file: UploadFile, destination: Path) -> None:
    size = 0
    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"The PDF exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
                )
            output.write(chunk)

    with destination.open("rb") as source:
        if not source.read(5).startswith(b"%PDF-"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The uploaded file is not a PDF",
            )


def validate_pdf(path: Path) -> None:
    try:
        with fitz.open(path) as document:
            if document.page_count == 0:
                raise ValueError("the PDF has no pages")
            if document.needs_pass:
                raise ValueError("password-protected PDFs are not supported")
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The PDF cannot be opened: {error}",
        ) from error
