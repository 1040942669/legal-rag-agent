"""Local web demo for the staged chat pipeline.

The package needs the optional ``service`` dependencies (FastAPI, uvicorn) and
is not imported by :mod:`legal_rag` or the CLI.  ``legal-rag-web`` serves one
static page that streams every pipeline node over server-sent events.
"""
