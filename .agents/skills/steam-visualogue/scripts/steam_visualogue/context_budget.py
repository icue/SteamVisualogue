"""Deterministic, model-independent budgets for Agent handoff artifacts.

This module is deliberately small and dependency free. Packet builders and
result validators use it as their only source of byte, token, record, image,
and pixel limits.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PACKET_MAX_UTF8_BYTES = 72 * 1024
PACKET_MAX_ESTIMATED_TOKENS = 24_000
ASSIGNMENT_ENVELOPE_RESERVE_BYTES = 16 * 1024
RESULT_MAX_UTF8_BYTES = 24 * 1024
MERGE_ARTIFACT_MAX_UTF8_BYTES = 24 * 1024
READER_QUALITY_PACKET_MAX_UTF8_BYTES = 160 * 1024
MAX_IMAGES_PER_PACKET = 8
MAX_SOURCE_PIXELS_PER_PACKET = 10_000_000
MAX_EVIDENCE_CARDS_PER_CURATION_SHARD = 30
MAX_FINDINGS_PER_CURATION_SHARD = 8
MAX_FINAL_SEMANTIC_FINDINGS = 20
MAX_ACHIEVEMENT_CANDIDATE_GAMES = 60
MAX_ACHIEVEMENT_PACKETS = 12
MAX_ACHIEVEMENTS_PER_GAME_PER_PACKET = 12
MAX_FULL_RESOLUTION_PAGES_PER_PACKET = 6
MAX_FACTUAL_QUALITY_PAGES_PER_PACKET = 6

class BudgetViolation(ValueError):
    """A serialized handoff artifact exceeds one or more deterministic limits."""

    def __init__(self, code: str, actual: int | float, limit: int | float, *, item_id: str | None = None) -> None:
        self.code = str(code)
        self.actual = actual
        self.limit = limit
        self.item_id = item_id
        suffix = f" item={item_id}" if item_id else ""
        super().__init__(f"{self.code}{suffix}: actual={actual}, limit={limit}")


class AgentPacketItemTooLarge(BudgetViolation):
    """One indivisible source record cannot fit in a packet budget."""

    def __init__(self, item_id: str, actual: int | float, limit: int | float, *, code: str = "packet_item_too_large") -> None:
        super().__init__(code, actual, limit, item_id=item_id)


def canonical_json(value: Any) -> str:
    """Serialize Agent-readable JSON with one stable byte representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def estimated_tokens_from_text(text: str) -> int:
    encoded = str(text).encode("utf-8")
    return max(math.ceil(len(encoded) / 3), math.ceil(len(str(text)) / 2))


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def sha256_path_hex(path: str | Path) -> str:
    """Return the same file digest without the typed ``sha256:`` prefix."""

    return sha256_path(path).split(":", 1)[-1]


@dataclass(frozen=True)
class BudgetMetrics:
    utf8_bytes: int
    character_count: int
    estimated_tokens: int
    item_count: int = 0
    image_count: int = 0
    total_pixels: int = 0
    safe_to_dispatch: bool = True
    failures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "utf8_bytes": self.utf8_bytes,
            "character_count": self.character_count,
            "estimated_tokens": self.estimated_tokens,
            "item_count": self.item_count,
            "image_count": self.image_count,
            "total_pixels": self.total_pixels,
            "safe_to_dispatch": self.safe_to_dispatch,
            "failures": list(self.failures),
        }


def measure_serialized(
    serialized: str | bytes,
    *,
    item_count: int = 0,
    image_count: int = 0,
    total_pixels: int = 0,
    packet: bool = True,
    result: bool = False,
    merge: bool = False,
) -> BudgetMetrics:
    """Measure final serialized bytes exactly as written to disk."""

    if isinstance(serialized, bytes):
        raw = serialized
        text = raw.decode("utf-8")
    else:
        text = str(serialized)
        raw = text.encode("utf-8")
    failures: list[str] = []
    if result:
        if len(raw) > RESULT_MAX_UTF8_BYTES:
            failures.append("result_bytes")
    elif merge:
        if len(raw) > MERGE_ARTIFACT_MAX_UTF8_BYTES:
            failures.append("merge_bytes")
    elif packet:
        if len(raw) > PACKET_MAX_UTF8_BYTES:
            failures.append("packet_bytes")
        token_count = max(math.ceil(len(raw) / 3), math.ceil(len(text) / 2))
        if token_count > PACKET_MAX_ESTIMATED_TOKENS:
            failures.append("packet_tokens")
        if int(image_count) > MAX_IMAGES_PER_PACKET:
            failures.append("packet_images")
        if int(total_pixels) > MAX_SOURCE_PIXELS_PER_PACKET:
            failures.append("packet_pixels")
    token_count = max(math.ceil(len(raw) / 3), math.ceil(len(text) / 2))
    return BudgetMetrics(
        utf8_bytes=len(raw),
        character_count=len(text),
        estimated_tokens=token_count,
        item_count=max(0, int(item_count)),
        image_count=max(0, int(image_count)),
        total_pixels=max(0, int(total_pixels)),
        safe_to_dispatch=not failures,
        failures=tuple(failures),
    )


def measure_payload(
    payload: Any,
    *,
    item_count: int = 0,
    image_count: int = 0,
    total_pixels: int = 0,
) -> BudgetMetrics:
    return measure_serialized(
        canonical_json(payload),
        item_count=item_count,
        image_count=image_count,
        total_pixels=total_pixels,
    )


def assert_packet_budget(
    payload: Any,
    *,
    item_count: int = 0,
    image_count: int = 0,
    total_pixels: int = 0,
    item_id: str | None = None,
) -> BudgetMetrics:
    metrics = measure_payload(
        payload,
        item_count=item_count,
        image_count=image_count,
        total_pixels=total_pixels,
    )
    if metrics.utf8_bytes > PACKET_MAX_UTF8_BYTES:
        raise AgentPacketItemTooLarge(item_id or "packet", metrics.utf8_bytes, PACKET_MAX_UTF8_BYTES) if item_id else BudgetViolation("packet_bytes", metrics.utf8_bytes, PACKET_MAX_UTF8_BYTES)
    if metrics.estimated_tokens > PACKET_MAX_ESTIMATED_TOKENS:
        raise AgentPacketItemTooLarge(item_id or "packet", metrics.estimated_tokens, PACKET_MAX_ESTIMATED_TOKENS, code="packet_tokens") if item_id else BudgetViolation("packet_tokens", metrics.estimated_tokens, PACKET_MAX_ESTIMATED_TOKENS)
    if metrics.image_count > MAX_IMAGES_PER_PACKET:
        raise BudgetViolation("packet_images", metrics.image_count, MAX_IMAGES_PER_PACKET)
    if metrics.total_pixels > MAX_SOURCE_PIXELS_PER_PACKET:
        raise BudgetViolation("packet_pixels", metrics.total_pixels, MAX_SOURCE_PIXELS_PER_PACKET)
    return metrics


def assert_result_budget(payload: Any) -> BudgetMetrics:
    metrics = measure_serialized(canonical_json(payload), result=True)
    if metrics.utf8_bytes > RESULT_MAX_UTF8_BYTES:
        raise BudgetViolation("result_bytes", metrics.utf8_bytes, RESULT_MAX_UTF8_BYTES)
    return metrics


def assert_reader_quality_packet_budget(payload: Any) -> BudgetMetrics:
    """Check the full-deck reader packet against its dedicated byte budget."""

    metrics = measure_serialized(canonical_json(payload), packet=False)
    if metrics.utf8_bytes > READER_QUALITY_PACKET_MAX_UTF8_BYTES:
        raise BudgetViolation(
            "reader_quality_packet_bytes",
            metrics.utf8_bytes,
            READER_QUALITY_PACKET_MAX_UTF8_BYTES,
        )
    return metrics


def assert_merge_budget(payload: Any) -> BudgetMetrics:
    metrics = measure_serialized(canonical_json(payload), merge=True)
    if metrics.utf8_bytes > MERGE_ARTIFACT_MAX_UTF8_BYTES:
        raise BudgetViolation("merge_bytes", metrics.utf8_bytes, MERGE_ARTIFACT_MAX_UTF8_BYTES)
    return metrics


def metrics_for_path(
    path: str | Path,
    *,
    item_count: int = 0,
    image_count: int = 0,
    total_pixels: int = 0,
    result: bool = False,
    merge: bool = False,
) -> BudgetMetrics:
    raw = Path(path).read_bytes()
    return measure_serialized(
        raw,
        item_count=item_count,
        image_count=image_count,
        total_pixels=total_pixels,
        packet=not result and not merge,
        result=result,
        merge=merge,
    )


__all__ = [
    "ASSIGNMENT_ENVELOPE_RESERVE_BYTES",
    "AgentPacketItemTooLarge",
    "BudgetMetrics",
    "BudgetViolation",
    "MAX_ACHIEVEMENTS_PER_GAME_PER_PACKET",
    "MAX_EVIDENCE_CARDS_PER_CURATION_SHARD",
    "MAX_FINAL_SEMANTIC_FINDINGS",
    "MAX_FINDINGS_PER_CURATION_SHARD",
    "MAX_FACTUAL_QUALITY_PAGES_PER_PACKET",
    "MAX_FULL_RESOLUTION_PAGES_PER_PACKET",
    "MAX_ACHIEVEMENT_CANDIDATE_GAMES",
    "MAX_ACHIEVEMENT_PACKETS",
    "MAX_IMAGES_PER_PACKET",
    "MAX_SOURCE_PIXELS_PER_PACKET",
    "MERGE_ARTIFACT_MAX_UTF8_BYTES",
    "PACKET_MAX_ESTIMATED_TOKENS",
    "PACKET_MAX_UTF8_BYTES",
    "RESULT_MAX_UTF8_BYTES",
    "READER_QUALITY_PACKET_MAX_UTF8_BYTES",
    "assert_merge_budget",
    "assert_packet_budget",
    "assert_reader_quality_packet_budget",
    "assert_result_budget",
    "canonical_json",
    "canonical_json_bytes",
    "estimated_tokens_from_text",
    "measure_payload",
    "measure_serialized",
    "metrics_for_path",
    "sha256_bytes",
    "sha256_path",
    "sha256_path_hex",
]
