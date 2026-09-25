"""Database-optional persistence contracts for versioned legal corpora.

Importing :mod:`legal_rag.storage` never imports SQLAlchemy or a database
driver.  The existing offline CLI therefore remains usable without the M3
``database`` optional dependency.
"""

from .contracts import (
    LawVersionSpec,
    PGVECTOR_VECTOR_MAX_DIMENSIONS,
    StorageContractError,
    StorageImportBundle,
    build_storage_import_bundle,
    validate_storage_import_bundle,
)

__all__ = [
    "LawVersionSpec",
    "PGVECTOR_VECTOR_MAX_DIMENSIONS",
    "StorageContractError",
    "StorageImportBundle",
    "build_storage_import_bundle",
    "validate_storage_import_bundle",
]
