"""Error classes. Step Functions matches on the exception class name (`ErrorEquals`)."""


class TransientError(Exception):
    """Retryable: the workflow retries with exponential backoff."""


class PermanentError(Exception):
    """Not retryable: the workflow goes straight to compensation."""
