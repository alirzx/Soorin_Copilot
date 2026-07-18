"""Treat retrieved documents as untrusted data before model inclusion."""

from __future__ import annotations

import re


INJECTION_PATTERNS = (
    (re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.I), "ignore_previous_instructions"),
    (re.compile(r"disregard\s+(the\s+)?(system|developer|previous)\s+message", re.I), "disregard_system_message"),
    (re.compile(r"reveal\s+(your\s+)?(system\s+prompt|hidden\s+instructions)", re.I), "reveal_hidden_prompt"),
    (re.compile(r"(exfiltrate|leak|dump)\s+(secrets|credentials|tokens|keys)", re.I), "secret_exfiltration"),
)


def prompt_injection_flags(text: str) -> tuple[str, ...]:
    return tuple(label for pattern, label in INJECTION_PATTERNS if pattern.search(text or ""))
