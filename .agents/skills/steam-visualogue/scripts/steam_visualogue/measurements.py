"""Typed, deterministic resolution of facts used as visual measures.

The editorial contracts deliberately keep natural-language copy tokens and
visual numbers separate. This module is the only place where a deck-plan
measure becomes a number that a renderer may scale, bin, or display.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
import re
from typing import Any, Mapping, Sequence

from .locales import format_locale_date, format_locale_number, normalize_report_locale


MEASURE_FORMAT_KINDS = frozenset({"integer", "number", "hours", "days", "percent", "year", "date"})


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Keep a numeric score within its inclusive range."""

    return max(low, min(high, value))


@dataclass(frozen=True)
class FactSpec:
    """The authoritative dimension and unit for one evidence fact family."""

    dimension: str
    canonical_unit: str
    input_unit: str | None = None
    value_kind: str = "number"
    percent_points: bool = False


@dataclass(frozen=True)
class ResolvedMeasure:
    """A validated fact and its reader-facing representation."""

    raw_value: Any
    dimension: str
    canonical_unit: str
    display_value: str
    evidence_id: str
    fact: str
    format_kind: str
    precision: int
    canonical_value: Decimal | None = None
    numeric_value: Decimal | None = None

    @property
    def value(self) -> Decimal | None:
        """Alias used by layout code when it needs the scaled value."""

        return self.canonical_value

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "raw_value": self.raw_value,
            "dimension": self.dimension,
            "canonical_unit": self.canonical_unit,
            "display_value": self.display_value,
            "evidence_id": self.evidence_id,
            "fact": self.fact,
            "format": {"kind": self.format_kind, "precision": self.precision},
        }
        if self.canonical_value is not None:
            result["canonical_value"] = _json_decimal(self.canonical_value)
        if self.numeric_value is not None:
            result["numeric_value"] = _json_decimal(self.numeric_value)
        return result


# Fact names are intentionally explicit.  The suffix fallback below only
# expands this registry for the names produced by the deterministic analytics
    # layer; authors cannot declare a new dimension in a deck plan.
FACT_REGISTRY: dict[str, FactSpec] = {
    # Durations
    "playtime_minutes": FactSpec("duration", "minutes", "minutes"),
    "playtime_hours": FactSpec("duration", "minutes", "hours"),
    "achievement_span_days": FactSpec("duration", "minutes", "days"),
    "largest_gap_days": FactSpec("duration", "minutes", "days"),
    "days": FactSpec("duration", "minutes", "days"),
    "duration_days": FactSpec("duration", "minutes", "days"),
    "duration_minutes": FactSpec("duration", "minutes", "minutes"),
    "duration_hours": FactSpec("duration", "minutes", "hours"),
    "era_span": FactSpec("duration", "years", "years"),
    # Ratios and percentage points
    "engagement_ratio": FactSpec("ratio", "percent", "ratio"),
    "meaningful_engagement_ratio": FactSpec("ratio", "percent", "ratio"),
    "achievement_completion": FactSpec("ratio", "percent", "ratio"),
    "completion": FactSpec("ratio", "percent", "ratio"),
    "concentration": FactSpec("ratio", "percent", "ratio"),
    "completion_mean": FactSpec("ratio", "percent", "ratio"),
    "completion_median": FactSpec("ratio", "percent", "ratio"),
    "old_game_share": FactSpec("ratio", "percent", "ratio"),
    "recent_game_share": FactSpec("ratio", "percent", "ratio"),
    "played_title_old_game_share": FactSpec("ratio", "percent", "ratio"),
    "played_title_recent_game_share": FactSpec("ratio", "percent", "ratio"),
    "playtime_share": FactSpec("ratio", "percent", "ratio"),
    "share": FactSpec("ratio", "percent", "ratio"),
    "global_percent": FactSpec("ratio", "percent", percent_points=True),
    "gap_percentage_points": FactSpec("ratio", "percent", percent_points=True),
    "percentage_points": FactSpec("ratio", "percent", percent_points=True),
    "percent": FactSpec("ratio", "percent", percent_points=True),
    # Counts
    "count": FactSpec("count", "count"),
    "game_count": FactSpec("count", "count"),
    "eligible_games": FactSpec("count", "count"),
    "owned_count": FactSpec("count", "count"),
    "played_count": FactSpec("count", "count"),
    "unplayed_count": FactSpec("count", "count"),
    "meaningfully_played_count": FactSpec("count", "count"),
    "completion_below_20_games": FactSpec("count", "count"),
    "completion_20_to_80_games": FactSpec("count", "count"),
    "completion_80_plus_games": FactSpec("count", "count"),
    "perfected_games": FactSpec("count", "count"),
    "completion_low_games": FactSpec("count", "count"),
    "completion_mid_games": FactSpec("count", "count"),
    "completion_high_games": FactSpec("count", "count"),
    "achievements_unlocked": FactSpec("count", "count"),
    "achievements_total": FactSpec("count", "count"),
    "rare_count": FactSpec("count", "count"),
    "ultra_rare_count": FactSpec("count", "count"),
    "timestamped_unlocks": FactSpec("count", "count"),
    "unlocks": FactSpec("count", "count"),
    "burst_count": FactSpec("count", "count"),
    "comeback_count": FactSpec("count", "count"),
    "activity_year_count": FactSpec("count", "count"),
    "max_unlocks_in_24h": FactSpec("count", "count"),
    # Calendar values
    "analysis_year": FactSpec("time_point", "year", value_kind="year"),
    "release_year": FactSpec("time_point", "year", value_kind="year"),
    "year": FactSpec("time_point", "year", value_kind="year"),
    "date": FactSpec("date", "date", value_kind="date"),
    "release_date": FactSpec("date", "date", value_kind="date"),
    "first_achievement_at": FactSpec("date", "date", value_kind="date"),
    "last_achievement_at": FactSpec("date", "date", value_kind="date"),
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?)?$")
_YEAR_RE = re.compile(r"^(?:19|20|21)\d{2}$")


def _json_decimal(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _fact_name(fact: Any) -> str:
    return str(fact or "").strip().split(".")[-1]


def fact_spec(fact: str) -> FactSpec:
    """Return the registry entry for a fact or reject an unknown dimension."""

    key = _fact_name(fact)
    spec = FACT_REGISTRY.get(key)
    if spec is not None:
        return spec
    # These are deterministic analytics naming families, not an author
    # supplied dimension declaration.  Keep the accepted family narrow.
    if key.endswith("_count") or key.endswith("_games") or key.endswith("_titles"):
        return FactSpec("count", "count", "count")
    if key.endswith("_minutes"):
        return FactSpec("duration", "minutes", "minutes")
    if key.endswith("_hours"):
        return FactSpec("duration", "minutes", "hours")
    if key.endswith("_days"):
        return FactSpec("duration", "minutes", "days")
    if key.endswith("_share") or key.endswith("_ratio") or key.endswith("_completion"):
        return FactSpec("ratio", "percent")
    raise ValueError(f"fact '{fact}' is not registered as a measurable fact")


def _catalog(value: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        raise TypeError("evidence catalog must be a mapping")
    if any(key in value for key in ("metrics", "games", "achievements", "patterns", "cards")):
        from .evidence import evidence_catalog

        return evidence_catalog(value)
    return value  # type: ignore[return-value]


def _find_fact(record: Mapping[str, Any], fact: str) -> Any:
    facts = record.get("facts")
    if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
        raise ValueError(f"Evidence '{record.get('id', '')}' has no fact '{fact}'")
    from .evidence import fact_value

    missing = object()
    value = fact_value(record, fact, missing)
    if value is missing:
        raise ValueError(f"Evidence '{record.get('id', '')}' has no fact '{fact}'")
    return value


def _decimal_number(value: Any, evidence_id: str, fact: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"Evidence '{evidence_id}#{fact}' is not a numeric fact")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Evidence '{evidence_id}#{fact}' is not a numeric fact") from None
    if not number.is_finite():
        raise ValueError(f"Evidence '{evidence_id}#{fact}' is not finite")
    return number


def _date_value(value: Any, evidence_id: str, fact: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        raise ValueError(f"Evidence '{evidence_id}#{fact}' is not an ISO date")
    text = value.strip().replace("z", "Z")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Evidence '{evidence_id}#{fact}' is not an ISO date") from None
    return text


def _format_precision(reference: Mapping[str, Any]) -> tuple[str, int]:
    spec = reference.get("format")
    if not isinstance(spec, Mapping):
        raise ValueError("measure format must be an object")
    kind = str(spec.get("kind") or "").strip().lower()
    if kind not in MEASURE_FORMAT_KINDS:
        raise ValueError(f"unsupported measure format '{kind}'")
    precision = spec.get("precision", 0)
    if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 6:
        raise ValueError("measure precision must be an integer from 0 to 6")
    return kind, precision


def resolve_measure(
    reference: Mapping[str, Any],
    evidence_catalog: Mapping[str, Any],
    report_locale: str = "en-US",
) -> ResolvedMeasure:
    """Resolve one structured measure against authoritative evidence.

    ``reference`` contains only ``evidence_id``, ``fact`` and ``format``.  No
    value, dimension, or unit is accepted from the page author.
    """

    if not isinstance(reference, Mapping):
        raise TypeError("measure reference must be an object")
    evidence_id = str(reference.get("evidence_id") or "").strip()
    fact = str(reference.get("fact") or "").strip()
    if not evidence_id or not fact:
        raise ValueError("measure requires evidence_id and fact")
    catalog = _catalog(evidence_catalog)
    record = catalog.get(evidence_id)
    if not isinstance(record, Mapping):
        raise ValueError(f"unknown evidence '{evidence_id}'")
    value = _find_fact(record, fact)
    spec = fact_spec(fact)
    kind, precision = _format_precision(reference)
    locale = normalize_report_locale(report_locale)

    canonical_value: Decimal | None = None
    numeric_value: Decimal | None = None
    if spec.value_kind == "date":
        raw_value = _date_value(value, evidence_id, fact)
        if kind != "date":
            raise ValueError(f"measure format '{kind}' is incompatible with date fact '{fact}'")
        display = format_locale_date(raw_value, locale)
    elif spec.value_kind == "year":
        number = _decimal_number(value, evidence_id, fact)
        if number != number.to_integral_value() or not 1 <= number <= 9999:
            raise ValueError(f"Evidence '{evidence_id}#{fact}' is not a valid year")
        raw_value = _json_decimal(number)
        canonical_value = number
        numeric_value = number
        if kind != "year":
            raise ValueError(f"measure format '{kind}' is incompatible with year fact '{fact}'")
        display = str(int(number))
    else:
        number = _decimal_number(value, evidence_id, fact)
        raw_value = _json_decimal(number)
        numeric_value = number
        if spec.dimension in {"count", "duration"} and number < 0:
            raise ValueError(f"Evidence '{evidence_id}#{fact}' cannot be negative")
        if spec.dimension == "ratio":
            if spec.percent_points:
                if number < 0 or number > 100:
                    raise ValueError(f"Evidence '{evidence_id}#{fact}' is outside 0–100 percent")
                percent_value = number
            else:
                if number < 0 or number > 1:
                    raise ValueError(f"Evidence '{evidence_id}#{fact}' is outside 0–1 ratio")
                percent_value = number * 100
            canonical_value = percent_value
        elif spec.dimension == "duration" and spec.canonical_unit == "minutes":
            canonical_value = number * {"hours": Decimal(60), "days": Decimal(1440)}.get(spec.input_unit, Decimal(1))
        else:
            canonical_value = number

        if kind == "hours":
            if spec.dimension != "duration":
                raise ValueError(f"measure format hours is incompatible with fact '{fact}'")
            shown = canonical_value / Decimal(60) if spec.canonical_unit == "minutes" else canonical_value * Decimal(24)
            display = format_locale_number(float(shown), precision, locale)
        elif kind == "days":
            if spec.dimension != "duration":
                raise ValueError(f"measure format days is incompatible with fact '{fact}'")
            if spec.canonical_unit == "minutes":
                shown = canonical_value / Decimal(1440)
            else:
                shown = canonical_value
            display = format_locale_number(float(shown), precision, locale)
        elif kind == "percent":
            if spec.dimension != "ratio":
                raise ValueError(f"measure format percent is incompatible with fact '{fact}'")
            display = format_locale_number(float(canonical_value), precision, locale) + "%"
        elif kind == "integer":
            if spec.dimension not in {"count", "duration"}:
                raise ValueError(f"measure format integer is incompatible with fact '{fact}'")
            display = format_locale_number(float(number), 0, locale)
        elif kind == "number":
            display = format_locale_number(float(number), precision, locale)
        else:
            raise ValueError(f"measure format '{kind}' is incompatible with fact '{fact}'")

    return ResolvedMeasure(
        raw_value=raw_value,
        dimension=spec.dimension,
        canonical_unit=spec.canonical_unit,
        display_value=display,
        evidence_id=evidence_id,
        fact=fact,
        format_kind=kind,
        precision=precision,
        canonical_value=canonical_value,
        numeric_value=numeric_value,
    )


def measures_are_comparable(left: ResolvedMeasure, right: ResolvedMeasure) -> bool:
    return (
        left.dimension == right.dimension
        and left.canonical_unit == right.canonical_unit
        and left.canonical_value is not None
        and right.canonical_value is not None
    )


def largest_remainder_allocation(
    values: Sequence[Any],
    denominator: Any,
    *,
    units: int = 100,
) -> tuple[list[int], int]:
    """Allocate a denominator and bins into stable integer percentage units."""

    if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
        raise ValueError("allocation units must be a positive integer")
    denominator_decimal = _decimal_number(denominator, "archive", "denominator")
    if denominator_decimal < 0:
        raise ValueError("allocation denominator cannot be negative")
    parsed: list[Decimal] = []
    for index, value in enumerate(values):
        number = _decimal_number(value, "archive", f"bin[{index}]")
        if number < 0:
            raise ValueError("allocation bins cannot be negative")
        parsed.append(number)
    if sum(parsed, Decimal(0)) > denominator_decimal:
        raise ValueError("allocation bins exceed denominator")
    if denominator_decimal == 0:
        return [0 for _ in parsed], units
    with localcontext() as context:
        context.prec = 50
        residual = denominator_decimal - sum(parsed, Decimal(0))
        raw = [value * Decimal(units) / denominator_decimal for value in parsed]
        raw.append(residual * Decimal(units) / denominator_decimal)
        floors = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in raw]
        remainder_units = units - sum(floors)
        fractions = [(raw[index] - Decimal(floors[index]), index) for index in range(len(raw))]
        fractions.sort(key=lambda item: (-item[0], item[1]))
        allocations = list(floors)
        for _, index in fractions[:remainder_units]:
            allocations[index] += 1
    return allocations[:-1], allocations[-1]


# Short name used by callers and external contract tests.
allocate_largest_remainder = largest_remainder_allocation


__all__ = [
    "FACT_REGISTRY",
    "MEASURE_FORMAT_KINDS",
    "FactSpec",
    "ResolvedMeasure",
    "allocate_largest_remainder",
    "clamp",
    "fact_spec",
    "largest_remainder_allocation",
    "measures_are_comparable",
    "resolve_measure",
]
