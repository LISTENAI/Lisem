"""Optional pytest failure tracking without a pytest dependency in the SDK."""
from contextvars import ContextVar

failure_provider = ContextVar("lisem_failure_provider", default=None)
evidence_recorder = ContextVar("lisem_evidence_recorder", default=None)
