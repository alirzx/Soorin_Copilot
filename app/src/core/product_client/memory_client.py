"""Memory-specific Product transport layered on the shared authenticated client."""

from __future__ import annotations

from typing import Any

from src.core.product_client.client import ProductApiClient
from src.core.product_client.errors import ProductApiError, ProductApiHTTPError


class ProductMemoryNotFoundError(ProductApiError):
    pass


class ProductMemoryForbiddenError(ProductApiError):
    pass


class ProductMemoryConflictError(ProductApiError):
    pass


class ProductMemoryValidationError(ProductApiError):
    pass


class ProductMemoryClient:
    """Product memory calls with owner context, without a second auth lifecycle."""

    def __init__(self, product_client: ProductApiClient) -> None:
        self.product_client = product_client

    @property
    def thread_state_path(self) -> str:
        return self.product_client.settings.product_memory_thread_state_path.rstrip("/")

    @property
    def ltm_path(self) -> str:
        return self.product_client.settings.product_memory_ltm_path.rstrip("/")

    @staticmethod
    def _owner_headers(user_id: str) -> dict[str, str]:
        owner = str(user_id or "").strip()
        if not owner:
            raise ProductMemoryValidationError("Memory owner is required.")
        return {"X-User-ID": owner, "Content-Type": "application/json"}

    def _request(
        self, method: str, path: str, *, user_id: str, request_id: str = "", body: dict[str, Any] | None = None
    ) -> Any:
        try:
            payload, _status, _elapsed = self.product_client._request_json(
                method, path, request_id=request_id, json_body=body,
                extra_headers=self._owner_headers(user_id),
            )
            return payload
        except ProductApiHTTPError as exc:
            if exc.status_code == 404:
                raise ProductMemoryNotFoundError("Product memory record was not found.") from exc
            if exc.status_code == 403:
                raise ProductMemoryForbiddenError("Product memory access was forbidden.") from exc
            if exc.status_code == 409:
                raise ProductMemoryConflictError("Product memory revision is stale.") from exc
            if exc.status_code == 422:
                raise ProductMemoryValidationError("Product memory request was invalid.") from exc
            raise

    def get_thread_state(self, *, user_id: str, conversation_id: str, request_id: str = "") -> Any:
        return self._request("GET", f"{self.thread_state_path}/{conversation_id}", user_id=user_id, request_id=request_id)

    def put_thread_state(self, *, user_id: str, conversation_id: str, body: dict[str, Any], request_id: str = "") -> Any:
        return self._request("PUT", f"{self.thread_state_path}/{conversation_id}", user_id=user_id, request_id=request_id, body=body)

    def create_ltm(self, *, user_id: str, body: dict[str, Any], request_id: str = "") -> Any:
        return self._request("POST", self.ltm_path, user_id=user_id, request_id=request_id, body=body)

    def search_ltm(self, *, user_id: str, body: dict[str, Any], request_id: str = "") -> Any:
        return self._request("POST", f"{self.ltm_path}/search", user_id=user_id, request_id=request_id, body=body)

    def get_ltm(self, *, user_id: str, memory_id: str, request_id: str = "") -> Any:
        return self._request("GET", f"{self.ltm_path}/{memory_id}", user_id=user_id, request_id=request_id)

    def list_ltm_audit(self, *, user_id: str, memory_id: str, request_id: str = "") -> Any:
        return self._request("GET", f"{self.ltm_path}/{memory_id}/audit", user_id=user_id, request_id=request_id)

    def transition_ltm(self, *, user_id: str, memory_id: str, body: dict[str, Any], request_id: str = "") -> Any:
        return self._request("POST", f"{self.ltm_path}/{memory_id}/transition", user_id=user_id, request_id=request_id, body=body)
