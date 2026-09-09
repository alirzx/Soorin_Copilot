"""Conservative deterministic guard for explicit prompt-injection attempts."""

from __future__ import annotations

import re


PROMPT_INJECTION_REFUSAL = (
    "I cannot follow instructions that conflict with Soorin Copilot’s "
    "cybersecurity scope and operational safeguards."
)

_STRONG_INJECTION_PATTERNS = (
    re.compile(
        r"\bignore\s+(?:(?:all|any)\s+)?(?:the\s+)?(?:previous|prior|system|developer)\s+"
        r"(?:instructions?|prompts?|messages?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:(?:can|could|would|will)\s+you\s+|please\s+)?"
        r"(?:reveal|show|print|display|repeat|provide|dump|extract)\b.{0,16}"
        r"\b(?:your|the)\b.{0,16}"
        r"\b(?:hidden|internal|system|developer)\b.{0,32}\b(?:prompts?|instructions?|messages?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\byour\b.{0,16}\b(?:system|developer|hidden|internal)\b.{0,32}"
        r"\b(?:prompts?|instructions?)\b"
        r".{0,48}\b(?:reveal|show|print|display|repeat|provide|dump|extract)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:override|bypass|disable|remove|weaken)\b.{0,48}"
        r"\b(?:safeguards?|safety|security|authorization|polic(?:y|ies)|restrictions?)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\btreat\b.{0,64}\b(?:retrieved|document|tool|user)\b.{0,32}"
        r"\binstructions?\b.{0,48}\b(?:higher|priority|override|authoritative)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\bforget\b.{0,32}\b(?:current|existing|system)\b.{0,24}\b(?:role|instructions?|policy)\b"
        r".{0,64}\b(?:unrestricted|new\s+policy|instead)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)


def prompt_injection_refusal(message: str) -> str | None:
    """Return a refusal only for strong, explicit instruction attacks."""
    text = message.strip()
    if not text:
        return None
    if any(pattern.search(text) for pattern in _STRONG_INJECTION_PATTERNS):
        return PROMPT_INJECTION_REFUSAL
    return None
