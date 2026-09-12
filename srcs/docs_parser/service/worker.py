"""The GPU: one parse at a time, in a child process.

PaddleOCR-VL needs ~4.9 GB free of 6 GB (engine.py raises below that), so two
parses at once do not run slowly -- they fail. Everything here exists to make
that safe: serialise the work, isolate it in a process that can die without
taking the server with it, and warm the models before any job is timed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from .store import Job, JobStore

log = logging.getLogger("docparser.service")

JOB_TIMEOUT = int(os.environ.get("DOCPARSER_JOB_TIMEOUT", "1800"))
WARMUP_TIMEOUT = int(os.environ.get("DOCPARSER_WARMUP_TIMEOUT", "3600"))


def _configure_child_logging() -> None:
    # PaddleOCRVL raises the root log level to WARNING, which would swallow
    # docparser's page progress. Give its logger a level of its own.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("docparser").setLevel(logging.INFO)


def _parse_document(path: str, max_pages: int | None) -> dict[str, Any]:
    """Parse one PDF in the isolated GPU worker process."""
    _configure_child_logging()

    # Imported here so the API process never loads torch, Paddle or CUDA.
    from docparser import DocumentParser, ParserConfig

    result = DocumentParser(path, ParserConfig(max_pages=max_pages)).parse()
    document = result.document

    return {
        "markdown": result.markdown,
        "json": result.json,
        "is_scanned": result.is_scanned,
        "language": result.language_hint,
        "is_signed": result.is_signed,
        "n_pages": document.n_pages,
        "n_blocks": len(document.blocks),
        "source_format": document.metadata.get("source_format"),
        "signature_evidence": document.signature_evidence,
    }


def _warm_models() -> None:
    """Download and load every model once, so no job pays for it.

    The weights are ~4 GB. Left to the first request, that download runs inside
    the job and counts against its timeout -- on a slow network the very first
    parse can be killed by its own clock. Each model is released before the next
    is loaded, exactly as a real parse does.
    """
    _configure_child_logging()

    from docparser.captioner import Captioner
    from docparser.classifier import FigureClassifier
    from docparser.config import ParserConfig
    from docparser.engine import VLEngine

    config = ParserConfig()

    engine = VLEngine(dpi=config.dpi, layout_model=config.layout_model)
    engine.load()
    engine.unload()

    FigureClassifier(config.classifier_model, backend=config.classifier_backend)  # CPU

    if config.caption_figures:
        captioner = Captioner(model_id=config.caption_model, load_in_4bit=config.caption_in_4bit)
        captioner.load()
        captioner.unload()


def _looks_like_gpu_trouble(exc: Exception) -> bool:
    """Does this error mean the child's CUDA context can no longer be trusted?

    Deliberately a broad net: killing a healthy worker costs one model reload,
    while keeping a poisoned one fails every job after it.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in ("cuda", "cudnn", "cublas", "out of memory", "memory", "device", "vram")
    )


class GpuWorker:
    """Runs parses one at a time in a child process.

    A child process rather than a thread: a CUDA out-of-memory or a segfault in
    Paddle would take this whole server down, but it only kills the child, and
    the driver reclaims its VRAM when it dies.
    """

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._pool: ProcessPoolExecutor | None = None
        self._lock = asyncio.Lock()  # one parse at a time
        self.pending = 0  # queued + running
        self.running: str | None = None
        self.models_ready = False
        self.warmup_error: str | None = None
        self._background: set[asyncio.Task] = set()  # keeps re-warm tasks alive

    def _ensure_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            # spawn, not fork: forking a process with CUDA behind it is unsafe.
            self._pool = ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))
        return self._pool

    def _kill_pool(self) -> None:
        """Throw the worker away; the next job spawns a clean one.

        Done after any failure: an OOM can leave the CUDA context unusable.
        `_processes` is private, but there is no public way to kill a worker
        stuck in native code -- shutdown(wait=True) would hang on it.
        """
        pool, self._pool = self._pool, None
        if pool is None:
            return
        for process in list(getattr(pool, "_processes", {}).values()):
            process.kill()
        pool.shutdown(wait=False, cancel_futures=True)

    async def warm(self) -> None:
        """Load the models before any job is timed. Takes the GPU like a parse does."""
        loop = asyncio.get_running_loop()
        async with self._lock:  # a request during warmup simply queues behind it
            log.info("warming models (first run downloads ~4 GB)")
            started = time.time()
            self.models_ready = False
            self.warmup_error = None
            try:
                future = loop.run_in_executor(self._ensure_pool(), _warm_models)
                await asyncio.wait_for(future, timeout=WARMUP_TIMEOUT)
                self.models_ready = True
                self.warmup_error = None
                log.info("models warm in %.0fs", time.time() - started)
            except Exception as exc:  # noqa: BLE001 - a cold start is not a fatal error
                self._kill_pool()
                self.warmup_error = f"{type(exc).__name__}: {exc}"
                log.error("GPU model warmup failed: %s", exc)

    async def run(self, job: Job) -> None:
        """Wait for the GPU, then parse. Never raises: failure lands on the job."""
        loop = asyncio.get_running_loop()
        try:
            async with self._lock:  # the queue: everyone else waits here
                job.status = "running"
                job.started_at = time.time()
                self.running = job.id
                self.store.save(job)
                log.info("job %s: parsing %s", job.id, job.filename)

                future = loop.run_in_executor(
                    self._ensure_pool(), _parse_document, job.pdf_path, job.max_pages
                )
                try:
                    job.result = await asyncio.wait_for(future, timeout=JOB_TIMEOUT)
                    job.status = "done"
                    self.models_ready = True
                    self.warmup_error = None
                    log.info("job %s: done in %ss", job.id, job.duration)

                except asyncio.TimeoutError:
                    self._kill_pool()
                    self._fail(job, f"parse exceeded the {JOB_TIMEOUT}s limit and was killed")

                except BrokenExecutor:  # segfault, or the kernel's OOM killer
                    self._kill_pool()
                    self._fail(job, "the parser process died (out of memory, or a crash in a model)")

                except Exception as exc:  # noqa: BLE001 - a bad PDF must not kill the server
                    # An exception that came back through the pool means the
                    # child is alive and its models still warm. Killing it would
                    # make the next job pay a ~1 min reload for someone else's
                    # bad file. Only errors that smell of GPU state get the axe:
                    # after an OOM the CUDA context cannot be trusted.
                    if _looks_like_gpu_trouble(exc):
                        self._kill_pool()
                    self._fail(job, f"{type(exc).__name__}: {exc}")
        finally:
            self.running = None
            self.pending -= 1

            # If the failure cost us the worker and nothing is waiting, reload
            # the models now, in the background, rather than letting the next
            # job to arrive pay for it. With jobs queued this would only push
            # in front of them, so it is skipped.
            if self._pool is None and self.pending == 0:
                rewarm = asyncio.get_running_loop().create_task(self.warm())
                self._background.add(rewarm)
                rewarm.add_done_callback(self._background.discard)

            # Only finish a job that actually finished. A shutdown cancels this
            # task, and a job still waiting for the GPU must keep its `queued`
            # row and its upload -- that is what the restart picks up. Deleting
            # them here would make the job unrecoverable, which is the whole
            # thing the store exists to prevent.
            if job.status in ("done", "error"):
                job.finished_at = time.time()
                Path(job.pdf_path).unlink(missing_ok=True)
                self.store.save(job)

    def _fail(self, job: Job, message: str) -> None:
        job.status = "error"
        job.error = message
        log.error("job %s: %s", job.id, message)

    def shutdown(self) -> None:
        self._kill_pool()
