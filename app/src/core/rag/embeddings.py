"""Lazy embedding boundary and Hugging Face text embedder."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Protocol, Sequence


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


class HuggingFaceTextEmbedder:
    """Load the configured Hugging Face encoder on the first embedding request."""

    def __init__(self, model_name: str, dimension: int, *, max_length: int = 256) -> None:
        self.model_name = model_name
        self.dimension = int(dimension)
        self.max_length = max(8, int(max_length))
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
        import torch
        from transformers import AutoModel, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        model = AutoModel.from_pretrained(self.model_name)
        model.eval()
        hidden_size = int(getattr(model.config, "hidden_size", 0) or 0)
        if hidden_size != self.dimension:
            raise ValueError("Configured embedding dimension does not match model metadata.")
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model

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
