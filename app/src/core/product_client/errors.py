"""Product API error types."""


class ProductApiError(RuntimeError):
    """Base error for product API failures."""


class ProductApiConfigError(ProductApiError):
    """Raised when required product API configuration is missing."""


class ProductApiHTTPError(ProductApiError):
    """Raised when the product API returns an HTTP error."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
