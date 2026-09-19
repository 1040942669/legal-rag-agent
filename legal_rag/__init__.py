"""Current Chinese law conversational RAG assistant."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("legal-rag-assistant")
except PackageNotFoundError:
    __version__ = "0+unknown"
