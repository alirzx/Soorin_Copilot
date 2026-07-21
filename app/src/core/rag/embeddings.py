"""Lazy embedding boundary and Hugging Face text embedder."""

from __future__ import annotations

import importlib.util
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, Sequence


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingHealth:
    status: str
    model: str
    dimension: int
    loaded: bool
    error_classification: str | None = None


class Embedder(Protocol):
    model_name: str
    dimension: int

    def health(self) -> EmbeddingHealth: ...

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbeddingLoadError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HuggingFaceTextEmbedder:
    """Load the configured Hugging Face encoder on the first embedding request."""

    def __init__(
        self,
        model_name: str,
        dimension: int,
        *,
        max_length: int = 256,
        local_files_only: bool = True,
        cache_dir: str | None = None,
        revision: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.dimension = int(dimension)
        self.max_length = max(8, int(max_length))
        self.local_files_only = local_files_only
        self.cache_dir = cache_dir or None
        self.revision = revision or None
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None

    def health(self) -> EmbeddingHealth:
        dependencies_present = bool(
            importlib.util.find_spec("torch") and importlib.util.find_spec("transformers")
        )
        return EmbeddingHealth(
            status="ok" if dependencies_present else "unavailable",
            model=self.model_name,
            dimension=self.dimension,
            loaded=self._model is not None,
            error_classification=None if dependencies_present else "embedding_dependency_missing",
        )

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        started = time.perf_counter()
        import torch
        from transformers import AutoModel, AutoTokenizer

        load_args = {
            "cache_dir": self.cache_dir,
            "revision": self.revision,
            "local_files_only": self.local_files_only,
        }
        logger.info(
            "event=embedding_model_load_started model=%s revision=%s local_files_only=%s cache_configured=%s",
            self.model_name,
            self.revision or "default",
            self.local_files_only,
            bool(self.cache_dir),
        )
        try:
            tokenizer = AutoTokenizer.from_pretrained(self.model_name, **load_args)
            model = AutoModel.from_pretrained(self.model_name, **load_args)
        except (OSError, ValueError) as exc:
            code = (
                "embedding_revision_not_cached"
                if self.local_files_only and self.revision
                else "embedding_model_not_cached"
                if self.local_files_only
                else "embedding_offline_load_failed"
            )
            logger.warning(
                "event=embedding_model_load_failed model=%s revision=%s local_files_only=%s cache_configured=%s error_class=%s safe_error_code=%s latency_ms=%s",
                self.model_name,
                self.revision or "default",
                self.local_files_only,
                bool(self.cache_dir),
                type(exc).__name__,
                code,
                int((time.perf_counter() - started) * 1000),
            )
            raise EmbeddingLoadError(code) from exc
        model.eval()
        hidden_size = int(getattr(model.config, "hidden_size", 0) or 0)
        if hidden_size != self.dimension:
            raise ValueError("Configured embedding dimension does not match model metadata.")
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model
        logger.info(
            "event=embedding_model_loaded model=%s revision=%s local_files_only=%s cache_configured=%s latency_ms=%s",
            self.model_name,
            self.revision or "default",
            self.local_files_only,
            bool(self.cache_dir),
            int((time.perf_counter() - started) * 1000),
        )

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self._ensure_loaded()
        torch = self._torch
        inputs = self._tokenizer(
            [str(text) for text in texts],
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.max_length,
        )
        with torch.no_grad():
            hidden = self._model(**inputs).last_hidden_state
            mask = inputs["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
            pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
        return pooled.cpu().tolist()
