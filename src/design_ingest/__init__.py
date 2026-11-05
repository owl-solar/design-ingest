"""design-ingest: read engineering design inputs into one structured format.

    from design_ingest import ingest, ingest_dir
    doc = ingest("site.kmz")
    doc.data["summary"]          # format-specific results
    doc.chunks                   # text pieces with page/sheet/layer
    doc.tables                   # tables
    print(doc.to_markdown())     # ready for an LLM prompt
"""
from .doctype import classify, parse_filename
from .core import ingest, ingest_dir, register, supported_extensions
from .quantities import Quantity, parse_quantities
from .registers import extract_registers
from .schema import SCHEMA_VERSION, Chunk, Document, ParseError, Table
from .specs import canonical_specs, extract_specs

__version__ = "0.5.0"


def extract(*args, **kwargs):
    """LLM extraction into a pydantic schema - see design_ingest.extract module."""
    from .extract import extract as _extract

    return _extract(*args, **kwargs)


__all__ = [
    "ingest", "ingest_dir", "extract", "register", "supported_extensions",
    "Document", "Chunk", "Table", "ParseError", "__version__", "SCHEMA_VERSION",
    "classify", "parse_filename", "Quantity", "parse_quantities", "extract_registers",
    "extract_specs", "canonical_specs",
]
