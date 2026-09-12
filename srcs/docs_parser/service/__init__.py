"""The REST service around docparser: FastAPI app, GPU queue, persistence.

Run it as ``uvicorn service.main:app`` from the repository root (which is what
the Dockerfile does). The library in ``src/docparser`` knows nothing about any
of this; the boundary runs the other way -- worker.py imports the library, and
only inside the child process that owns the GPU.
"""
