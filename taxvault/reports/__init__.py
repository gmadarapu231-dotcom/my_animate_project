"""Documents the practice hands to a client.

An estimate that only exists on a screen is not a deliverable. A client wants
something to read on the sofa, show a spouse, take to a lender and keep for
their records; a practice wants a record of exactly what it told someone and
when. That is a document, and this package builds them.
"""

from taxvault.reports.estimate import (
    EstimateDocument,
    build_estimate_document,
    render_html,
    render_pdf,
)

__all__ = [
    "EstimateDocument",
    "build_estimate_document",
    "render_html",
    "render_pdf",
]
