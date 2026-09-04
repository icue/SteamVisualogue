"""Compile the single editorial deck contract.

The compiler is deliberately the only boundary between authored editorial
choices and the renderer.  It resolves names and measures, checks the
narrative graph, and records the semantic facts that downstream layout and
quality checks are allowed to consume.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .evidence import evidence_catalog, fact_value
from .label_localization import compute_label_fingerprint, scan_label_references
from .locales import catalog_for, normalize_report_locale
from .measurements import largest_remainder_allocation, resolve_measure
from .paths import SCHEMA_ROOT


# Keep the role vocabulary machine-owned.  It is not localized and is never
# emitted as reader-visible text by the publish layout.
PRESENTATION_KINDS = frozenset(
    {
        "opening",
        "hero",
        "archive-density",
        "evidence-ledger",
        "quantitative-comparison",
        "qualitative-comparison",
        "series-atlas",
        "pattern-atlas",
        "temporal-strata",
        "achievement-anomaly",
        "abstract-portrait",
        "closing",
    }
)
NARRATIVE_MOVES = frozenset(
    {"establish", "quantify", "contrast", "deepen", "complicate", "connect", "synthesize", "close"}
)
CLAIM_KINDS = frozenset(
    {"relation", "magnitude", "contrast", "pattern", "anomaly", "consequence", "tension", "synthesis"}
)

_GAME_ID = re.compile(r"^game:([1-9][0-9]*)$")
_ACHIEVEMENT_ID = re.compile(r"^achievement:([1-9][0-9]*):([^:]+)$")
_GAME_ASSET = re.compile(r"^game:[1-9][0-9]*:(?:header|portrait)$")
_ACHIEVEMENT_ASSET = re.compile(r"^achievement:[1-9][0-9]*:[^:]+:(?:unlocked|locked)$")
_GENERATED_ASSET = re.compile(r"^generated:sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"\{\{([^{}#|]+(?::[^{}#|]+)*)#([^{}|]+)\|([^{}]+)\}\}")
_WHITESPACE = re.compile(r"\s+")
_VISIBLE_READER_COPY_FIELDS = frozenset({"headline", "support", "caption"})
_GENERIC_CAPTION_REASON = re.compile(
    r"^(?:需要)?(?:说明|解释)(?:这张|该)?(?:图片|图像|视觉(?:元素)?)$"
    r"|^(?:image|visual)(?: caption| explanation)?$"
    r"|^(?:explain|describe) the (?:image|visual)$",
    re.IGNORECASE,
)

class EditorialDeckError(ValueError):
    """A safe, locatable error in the current deck contract."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        page: int | None = None,
        field: str | None = None,
        suggested_scope: str | None = None,
    ) -> None:
        self.code = str(code)
        self.message = str(message or code)
        self.page = page
        self.field = field or ""
        self.suggested_scope = suggested_scope or (f"page {page}" if page is not None else "deck plan")
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "page": self.page,
            "field": self.field,
            "message": self.message,
            "suggested_scope": self.suggested_scope,
        }


def _schema_path(name: str = "deck-plan.schema.json") -> Path:
    return SCHEMA_ROOT / name


def _validate_schema(document: Any, name: str = "deck-plan.schema.json") -> None:
    try:
        from jsonschema import Draft202012Validator

        schema = json.loads(_schema_path(name).read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda error: (tuple(str(part) for part in error.absolute_path), str(error.message)),
        )
    except FileNotFoundError as exc:
        raise EditorialDeckError("schema_unavailable", str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise EditorialDeckError("schema_invalid", str(exc)) from exc
    if errors:
        error = errors[0]
        path = tuple(error.absolute_path)
        page: int | None = None
        field = "/".join(str(part) for part in path)
        if len(path) >= 2 and path[0] == "pages" and isinstance(document, Mapping):
            index = path[1]
            if isinstance(index, int):
                row = document.get("pages", [])[index] if isinstance(document.get("pages"), list) and index < len(document["pages"]) else None
                page = int(row.get("page", index + 1)) if isinstance(row, Mapping) and str(row.get("page", "")).isdigit() else index + 1
                field = "/".join(str(part) for part in path[2:]) or "page"
        raise EditorialDeckError("schema_invalid", f"at {field or '<root>'}: {error.message}", page=page, field=field) from None


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk(child, path + (str(key),))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _walk(child, path + (str(index),))


def _catalog(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if any(key in value for key in ("metrics", "games", "achievements", "patterns", "cards")):
        return evidence_catalog(value)
    return {str(key): dict(row) for key, row in value.items() if isinstance(row, Mapping)}


def _entity_label(labels: Mapping[str, Any] | None, catalog: Mapping[str, Mapping[str, Any]], entity_id: str) -> str:
    if isinstance(labels, Mapping):
        direct = labels.get(entity_id)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        sections = "achievements" if _ACHIEVEMENT_ID.fullmatch(entity_id) else "games"
        section = labels.get(sections)
        if isinstance(section, Mapping):
            row = section.get(entity_id)
            if isinstance(row, str) and row.strip():
                return row.strip()
            if isinstance(row, Mapping):
                for key in ("display_name", "name", "displayName"):
                    if str(row.get(key) or "").strip():
                        return str(row[key]).strip()
    row = catalog.get(entity_id)
    if isinstance(row, Mapping):
            return str(fact_value(row, "name") or fact_value(row, "canonical_name") or entity_id).strip() or entity_id
    return entity_id


def _record_value(catalog: Mapping[str, Mapping[str, Any]], evidence_id: str, fact: str, page: int, field: str) -> Any:
    row = catalog.get(evidence_id)
    if not isinstance(row, Mapping):
        raise EditorialDeckError("evidence_not_found", f"unknown evidence '{evidence_id}'", page=page, field=field)
    value = fact_value(row, fact)
    if value is None:
        raise EditorialDeckError("evidence_fact_not_found", f"evidence '{evidence_id}' has no fact '{fact}'", page=page, field=field)
    return value


def _format_token(value: Any, evidence_id: str, fact: str, fmt: str, catalog: Mapping[str, Mapping[str, Any]], locale: str, page: int, field: str, labels: Mapping[str, Any] | None) -> str:
    kind_parts = [part.strip() for part in fmt.split(":", 1)]
    kind = kind_parts[0].casefold()
    if not kind or (len(kind_parts) == 2 and (not kind_parts[1].isdigit() or int(kind_parts[1]) > 6)):
        raise EditorialDeckError("copy_token_invalid", f"unsupported token precision '{fmt}'", page=page, field=field)
    if kind in {"text", "raw"}:
        if len(kind_parts) != 1:
            raise EditorialDeckError("copy_token_invalid", f"token format '{fmt}' cannot take a precision", page=page, field=field)
        if fact == "name":
            return _entity_label(labels, catalog, evidence_id)
        return str(value)
    if kind == "name":
        if len(kind_parts) != 1:
            raise EditorialDeckError("copy_token_invalid", f"token format '{fmt}' cannot take a precision", page=page, field=field)
        return _entity_label(labels, catalog, evidence_id)
    if kind == "description":
        if len(kind_parts) != 1:
            raise EditorialDeckError("copy_token_invalid", f"token format '{fmt}' cannot take a precision", page=page, field=field)
        return str(value)
    precision = int(kind_parts[1]) if len(kind_parts) == 2 and kind_parts[1].isdigit() else 0
    try:
        if kind in {"name", "description"}:
            return str(value)
        if kind in {"integer", "number", "hours", "days", "percent", "year", "date"}:
            reference = {"evidence_id": evidence_id, "fact": fact, "format": {"kind": kind, "precision": precision}}
            return str(resolve_measure(reference, catalog, locale).display_value)
    except (TypeError, ValueError, KeyError) as exc:
        raise EditorialDeckError("copy_token_invalid", str(exc), page=page, field=field) from exc
    raise EditorialDeckError("copy_token_invalid", f"unsupported token format '{fmt}'", page=page, field=field)


def _resolve_text(value: Any, allowed: set[str], catalog: Mapping[str, Mapping[str, Any]], locale: str, labels: Mapping[str, Any] | None, page: int, field: str) -> str:
    text = str(value or "")

    def replace(match: re.Match[str]) -> str:
        evidence_id, fact, fmt = (part.strip() for part in match.groups())
        if evidence_id not in allowed:
            raise EditorialDeckError("copy_evidence_outside_closure", f"copy references '{evidence_id}' without page evidence closure", page=page, field=field)
        raw = _record_value(catalog, evidence_id, fact, page, field)
        return _format_token(raw, evidence_id, fact, fmt, catalog, locale, page, field, labels)

    resolved = _TOKEN.sub(replace, text)
    if "{{" in resolved or "}}" in resolved:
        raise EditorialDeckError("copy_token_invalid", "reader copy contains a malformed evidence token", page=page, field=field)
    return resolved


def _resolve_text_tree(value: Any, allowed: set[str], catalog: Mapping[str, Mapping[str, Any]], locale: str, labels: Mapping[str, Any] | None, page: int, field: str) -> Any:
    if isinstance(value, str):
        return _resolve_text(value, allowed, catalog, locale, labels, page, field)
    if isinstance(value, list):
        return [_resolve_text_tree(item, allowed, catalog, locale, labels, page, f"{field}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            child_field = f"{field}.{key}" if field else str(key)
            if str(key) in {"evidence_id", "evidence_ids", "fact", "format", "game_id", "achievement_id", "asset_id", "state", "generated", "cluster_id", "claim_id", "kind"}:
                result[str(key)] = deepcopy(child)
            else:
                result[str(key)] = _resolve_text_tree(child, allowed, catalog, locale, labels, page, child_field)
        return result
    return deepcopy(value)


def _presentation_kind(value: Any, page: int) -> str:
    raw = str(value or "").strip().casefold().replace("_", "-")
    if raw not in PRESENTATION_KINDS:
        raise EditorialDeckError("presentation_kind_invalid", f"unsupported presentation kind '{value}'", page=page, field="presentation.kind")
    return raw


def _subject_ids(value: Any) -> tuple[set[str], set[str], set[str]]:
    games: set[str] = set()
    achievements: set[str] = set()
    assets: set[str] = set()
    for _, node in _walk(value):
        if not isinstance(node, Mapping):
            continue
        game_id = node.get("game_id")
        if isinstance(game_id, str) and _GAME_ID.fullmatch(game_id):
            games.add(game_id)
        achievement_id = node.get("achievement_id")
        if isinstance(achievement_id, str) and _ACHIEVEMENT_ID.fullmatch(achievement_id):
            achievements.add(achievement_id)
            games.add(f"game:{_ACHIEVEMENT_ID.fullmatch(achievement_id).group(1)}")
        asset_id = node.get("asset_id")
        if isinstance(asset_id, str) and (_GAME_ASSET.fullmatch(asset_id) or _ACHIEVEMENT_ASSET.fullmatch(asset_id) or _GENERATED_ASSET.fullmatch(asset_id)):
            assets.add(asset_id)
            game_match = re.match(r"^game:([1-9][0-9]*):", asset_id)
            if game_match:
                games.add(f"game:{game_match.group(1)}")
            achievement_match = re.match(r"^(achievement:[1-9][0-9]*:[^:]+):", asset_id)
            if achievement_match:
                achievements.add(achievement_match.group(1))
                games.add(f"game:{achievement_match.group(1).split(':', 2)[1]}")
        raw_visual_asset = node.get("raw_visual_asset")
        if isinstance(raw_visual_asset, str) and (_GAME_ASSET.fullmatch(raw_visual_asset) or _ACHIEVEMENT_ASSET.fullmatch(raw_visual_asset) or _GENERATED_ASSET.fullmatch(raw_visual_asset)):
            assets.add(raw_visual_asset)
    return games, achievements, assets


def _evidence_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    for _, node in _walk(value):
        if isinstance(node, Mapping):
            one = node.get("evidence_id")
            if isinstance(one, str) and one.strip():
                refs.add(one.strip())
            many = node.get("evidence_ids")
            if isinstance(many, Sequence) and not isinstance(many, (str, bytes)):
                refs.update(str(item).strip() for item in many if str(item).strip())
    return refs


def _claim_text(claim: Mapping[str, Any]) -> str:
    return _WHITESPACE.sub(" ", str(claim.get("text") or "").casefold()).strip()


def _method_error(text: str, locale: str) -> str | None:
    if locale == "zh-CN":
        checks = {
            "backstage_prose": r"候选|本页|这里比较|柱形表达|可观测|涉入|取样|阈值|证据哈希|审阅者|评审|图表制作|短窗口|收藏向成就",
            "source_provenance": r"证据编号|来源路径|字段名称|数据来源|内部 id|内部ID",
            "numeric_format": r"\d+\s*/\s*\d+",
        }
    else:
        checks = {
            "backstage_prose": r"candidate|sampling|threshold|on this page|bar chart|evidence hash|reviewer|qa status|selected by|meaningful play|short window|the record",
            "source_provenance": r"evidence id|evidence path|source field|internal id|qa status",
        }
    for code, pattern in checks.items():
        if re.search(pattern, text, re.IGNORECASE):
            return code
    return None


def _conclusion_signature(text: str, locale: str) -> str | None:
    folded = _WHITESPACE.sub(" ", text.casefold()).strip()
    if locale == "zh-CN":
        if re.search(r"没有(?:一个|单一|唯一)中心|不存在(?:一个|单一|唯一)中心|没有单一重心", folded):
            return "no-single-center"
    elif re.search(r"\bno (?:single|one) (?:center|centre|core)\b|\bwithout (?:a )?single (?:center|centre|core)\b", folded):
        return "no-single-center"
    return None


def _headline_gain_error(headline: str, kind: str, locale: str) -> str | None:
    folded = _WHITESPACE.sub(" ", headline.casefold()).strip()
    if not folded:
        return "headline_missing"
    if locale == "zh-CN":
        if re.fullmatch(r"(?:标签|类型|分类|角色扮演|策略|冒险|动作|模拟|解谜|工具|rpg)", folded):
            return "headline_no_information_gain"
        if re.fullmatch(r".*(?:被列为|属于|分类为|带有).*(?:标签|类型|rpg|角色扮演|策略|冒险|动作).*", folded) and not re.search(r"但|却|反而|超过|相差|因此|意味着|更|最", folded):
            return "headline_no_information_gain"
        if kind == "quantitative-comparison" and not re.search(r"倍|相差|多出|少了|远高|远低|几乎|只剩|长出|短了|意味着|因此|超过|至少|大约|约", folded):
            return "headline_repeats_visible_comparison"
    else:
        if re.fullmatch(r"(?:tag|genre|category|rpg|strategy|adventure|action|simulation|puzzle|tool)", folded):
            return "headline_no_information_gain"
        if re.fullmatch(r".*(?:listed under|tagged as|classified as|has the .* tag|is an? (?:rpg|strategy|adventure|action|simulation|puzzle)).*", folded) and not re.search(r"but|while|yet|however|more|less|times|means|leads|because|despite", folded, re.IGNORECASE):
            return "headline_no_information_gain"
        if kind == "quantitative-comparison" and (
            not re.search(r"times|roughly|nearly|almost|difference|gap|only|far |means|leads|beats?|beating|larger|smaller|higher|lower|double|triple|fold|twice|more than twice|less than half|\b\d+(?:\.\d+)?x\b", folded, re.IGNORECASE)
            or (re.search(r"\d|%", folded) and not re.search(r"difference|gap|means|leads|beats?|beating|larger|smaller|higher|lower|double|triple|fold|twice|more than twice|less than half|roughly|nearly|almost|times", folded, re.IGNORECASE))
        ):
            return "headline_repeats_visible_comparison"
    # A bare displayed value or sort label is not a reader claim.
    if re.fullmatch(r"[\d\s.,%:%+-]+", folded):
        return "headline_no_information_gain"
    return None


def _validate_comparison(page: Mapping[str, Any], kind: str, content: Mapping[str, Any], names: Mapping[str, str]) -> None:
    page_number = int(page["page"])
    required = ("shared_question", "shared_dimension", "relationship_claim_id", "items")
    missing = [field for field in required if field not in content]
    if missing:
        raise EditorialDeckError("comparison_contract_missing", "comparison requires shared_question, shared_dimension, relationship_claim_id, and two items", page=page_number, field="presentation.content")
    for field in ("shared_question", "shared_dimension", "relationship_claim_id"):
        if not isinstance(content.get(field), str) or not content[field].strip():
            raise EditorialDeckError("comparison_contract_invalid", f"comparison field '{field}' must be a non-empty string", page=page_number, field=f"presentation.content.{field}")
    if str(content["relationship_claim_id"]) != str(page["claim"]["claim_id"]):
        raise EditorialDeckError("comparison_relationship_mismatch", "relationship_claim_id must match the page claim_id", page=page_number, field="presentation.content.relationship_claim_id")
    items = content.get("items")
    if not isinstance(items, list) or len(items) != 2:
        raise EditorialDeckError("comparison_item_count_invalid", "comparison must contain exactly two items", page=page_number, field="presentation.content.items")
    if not all(isinstance(item, Mapping) and isinstance(item.get("subject"), Mapping) for item in items):
        raise EditorialDeckError("comparison_subject_missing", "each comparison item must bind one subject", page=page_number, field="presentation.content.items")
    game_ids = [str(item["subject"].get("game_id") or "") for item in items]
    if any(not game_id for game_id in game_ids) or len(set(game_ids)) != 2:
        raise EditorialDeckError("comparison_subject_invalid", "comparison items must bind two distinct game subjects", page=page_number, field="presentation.content.items")
    if kind == "quantitative-comparison":
        for index, item in enumerate(items):
            asset_id = str(item["subject"].get("asset_id") or "")
            parts = asset_id.split(":")
            if len(parts) == 3 and parts[0] == "game" and parts[2] != "portrait":
                raise EditorialDeckError(
                    "comparison_portrait_asset_required",
                    "quantitative-comparison game subjects must use game:<appid>:portrait assets",
                    page=page_number,
                    field=f"presentation.content.items[{index}].subject.asset_id",
                )
    if kind == "qualitative-comparison":
        for index, item in enumerate(items):
            asset_id = str(item["subject"].get("asset_id") or "")
            parts = asset_id.split(":")
            if len(parts) == 3 and parts[0] == "game" and parts[2] != "header":
                raise EditorialDeckError(
                    "comparison_landscape_asset_required",
                    "qualitative-comparison game subjects must use game:<appid>:header assets",
                    page=page_number,
                    field=f"presentation.content.items[{index}].subject.asset_id",
                )
        quantitative_words = re.compile(r"(?:\d|%|percent|percentage|hours?|minutes?|days?|比例|百分比|时长|小时|分钟|天数|数量|更多|更少|最高|最低|倍)", re.IGNORECASE)
        for index, item in enumerate(items):
            statement = str(item.get("statement") or "")
            if not statement.strip():
                raise EditorialDeckError("qualitative_statement_missing", "each qualitative comparison item needs a statement", page=page_number, field=f"presentation.content.items[{index}].statement")
            game_id = str(item["subject"].get("game_id") or "")
            name = names.get(game_id, "")
            if name and name.casefold() in statement.casefold():
                raise EditorialDeckError("qualitative_identity_duplicate", "qualitative item statement must not repeat its subject name", page=page_number, field=f"presentation.content.items[{index}].statement")
            for key in ("title", "card_title", "display_name", "name", "label", "tag", "genre", "category"):
                visible_label = str(item.get(key) or "").strip()
                if len(visible_label) > 2 and visible_label.casefold() in statement.casefold():
                    raise EditorialDeckError("qualitative_label_duplicate", "qualitative item statement must not repeat its visible card label", page=page_number, field=f"presentation.content.items[{index}].statement")
            if quantitative_words.search(statement):
                raise EditorialDeckError("qualitative_uses_quantitative_encoding", "qualitative comparison statements cannot encode a quantitative measure", page=page_number, field=f"presentation.content.items[{index}].statement")
    elif any(not isinstance(item.get("measure"), Mapping) for item in items):
        raise EditorialDeckError("quantitative_measure_missing", "each quantitative comparison item needs a measure", page=page_number, field="presentation.content.items")


def _atlas_items(content: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = content.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, Mapping)]
    return []


def _game_ids_in_value(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, str):
        if _GAME_ID.fullmatch(value):
            result.add(value)
        return result
    if isinstance(value, Mapping):
        game_id = value.get("game_id")
        if isinstance(game_id, str) and _GAME_ID.fullmatch(game_id):
            result.add(game_id)
        appid = value.get("appid")
        if isinstance(appid, (int, str)) and str(appid).isdigit() and int(appid) > 0:
            result.add(f"game:{int(appid)}")
        for child in value.values():
            result.update(_game_ids_in_value(child))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            result.update(_game_ids_in_value(child))
    return result


def _atlas_group_evidence_id(
    content: Mapping[str, Any],
    kind: str,
    page_evidence: set[str],
    catalog: Mapping[str, Mapping[str, Any]],
) -> str | None:
    keys = (
        "series_evidence_id",
        "pattern_evidence_id",
        "group_evidence_id",
        "group_evidence",
        "evidence_id",
    )
    for key in keys:
        value = content.get(key)
        if isinstance(value, str) and value in page_evidence:
            return value
    root_ids = content.get("evidence_ids")
    if isinstance(root_ids, Sequence) and not isinstance(root_ids, (str, bytes)):
        for value in root_ids:
            evidence_id = str(value)
            record = catalog.get(evidence_id)
            if evidence_id in page_evidence and isinstance(record, Mapping):
                record_type = str(record.get("type") or "")
                if (kind == "series-atlas" and "series" in record_type) or (
                    kind == "pattern-atlas" and "pattern" in record_type
                ):
                    return evidence_id
    return None


def _atlas_group_members(record: Mapping[str, Any]) -> set[str]:
    members = _game_ids_in_value(record.get("related_ids", []))
    facts = record.get("facts")
    if isinstance(facts, Sequence) and not isinstance(facts, (str, bytes)):
        for fact in facts:
            if not isinstance(fact, Mapping):
                continue
            name = str(fact.get("name") or "")
            if name in {"game_rows", "members", "member_ids", "games", "game_ids"}:
                members.update(_game_ids_in_value(fact.get("value")))
    return members


def _validate_atlas(
    page: Mapping[str, Any],
    kind: str,
    content: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
) -> None:
    """Validate the semantic contract shared by series and pattern atlases."""

    page_number = int(page["page"])
    page_evidence = {str(item) for item in page["claim"].get("evidence_ids", [])}
    items = _atlas_items(content)
    if not 3 <= len(items) <= 4:
        raise EditorialDeckError("atlas_item_count_invalid", "atlas must contain exactly three or four items", page=page_number, field="presentation.content.items")
    game_ids: list[str] = []
    measures: list[Mapping[str, Any]] = []
    for index, item in enumerate(items):
        subject = item.get("subject")
        if not isinstance(subject, Mapping):
            raise EditorialDeckError("atlas_subject_missing", "each atlas item must bind a subject", page=page_number, field=f"presentation.content.items[{index}].subject")
        game_id = str(subject.get("game_id") or "")
        if not _GAME_ID.fullmatch(game_id) or game_id not in catalog:
            raise EditorialDeckError("atlas_subject_invalid", "each atlas item must bind a current game subject", page=page_number, field=f"presentation.content.items[{index}].subject.game_id")
        game_ids.append(game_id)
        asset_id = str(subject.get("asset_id") or "")
        if asset_id != f"{game_id}:portrait":
            raise EditorialDeckError("atlas_portrait_asset_required", "series-atlas and pattern-atlas subjects must use game:<appid>:portrait assets", page=page_number, field=f"presentation.content.items[{index}].subject.asset_id")
        statement = str(item.get("statement") or "").strip()
        if not statement:
            raise EditorialDeckError("atlas_statement_missing", "each atlas item needs a non-empty statement", page=page_number, field=f"presentation.content.items[{index}].statement")
        evidence_ids = item.get("evidence_ids")
        if not isinstance(evidence_ids, Sequence) or isinstance(evidence_ids, (str, bytes)) or not evidence_ids:
            raise EditorialDeckError("atlas_evidence_binding_missing", "each atlas item needs evidence_ids", page=page_number, field=f"presentation.content.items[{index}].evidence_ids")
        missing = sorted(set(str(value) for value in evidence_ids) - page_evidence)
        if missing:
            raise EditorialDeckError("atlas_evidence_closure_missing", "atlas item evidence is outside the page closure: " + ", ".join(missing), page=page_number, field=f"presentation.content.items[{index}].evidence_ids")
        measure = item.get("measure")
        if measure is not None:
            if not isinstance(measure, Mapping):
                raise EditorialDeckError("atlas_measure_invalid", "atlas measures must be objects", page=page_number, field=f"presentation.content.items[{index}].measure")
            measures.append(measure)
    if len(set(game_ids)) != len(game_ids):
        raise EditorialDeckError("atlas_subject_duplicate", "atlas items must bind unique game subjects", page=page_number, field="presentation.content.items")
    if measures and len(measures) != len(items):
        raise EditorialDeckError("atlas_measure_missing", "either every atlas item has a measure or none do", page=page_number, field="presentation.content.items")
    if len(measures) > 1 and any(not measures_are_comparable_from_dict(measures[0], measure) for measure in measures[1:]):
        raise EditorialDeckError("atlas_measure_incompatible", "atlas measures must share dimension and canonical unit", page=page_number, field="presentation.content.items")
    group_id = _atlas_group_evidence_id(content, kind, page_evidence, catalog)
    if not group_id:
        raise EditorialDeckError("atlas_group_evidence_missing", "atlas must declare one series or pattern group evidence record", page=page_number, field="presentation.content.group_evidence_id")
    group_members = _atlas_group_members(catalog[group_id])
    missing_members = sorted(set(game_ids) - group_members)
    if missing_members:
        raise EditorialDeckError("atlas_group_membership_invalid", "atlas items are not all members of the declared group: " + ", ".join(missing_members), page=page_number, field="presentation.content.items")


def _validate_subject_nodes(content: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]], page: int) -> None:
    """Validate every typed subject before it can become a visible binding."""

    for _, node in _walk(content):
        if not isinstance(node, Mapping):
            continue
        game_id = node.get("game_id")
        if game_id is not None:
            if not isinstance(game_id, str) or not _GAME_ID.fullmatch(game_id):
                raise EditorialDeckError("subject_game_invalid", "game_id must be a current game ID", page=page, field="presentation.content")
            if game_id not in catalog:
                raise EditorialDeckError("subject_game_not_found", f"subject references unsupported game '{game_id}'", page=page, field="presentation.content")
        achievement_id = node.get("achievement_id")
        if achievement_id is not None:
            if not isinstance(achievement_id, str) or not _ACHIEVEMENT_ID.fullmatch(achievement_id):
                raise EditorialDeckError("subject_achievement_invalid", "achievement_id must be a current achievement ID", page=page, field="presentation.content")
            if achievement_id not in catalog:
                raise EditorialDeckError("subject_achievement_not_found", f"subject references unsupported achievement '{achievement_id}'", page=page, field="presentation.content")
        asset_id = node.get("asset_id")
        if asset_id is not None:
            if not isinstance(asset_id, str) or not (_GAME_ASSET.fullmatch(asset_id) or _ACHIEVEMENT_ASSET.fullmatch(asset_id) or _GENERATED_ASSET.fullmatch(asset_id)):
                raise EditorialDeckError("subject_asset_invalid", "asset_id must be a current source or generated asset ID", page=page, field="presentation.content")
        raw_visual_asset = node.get("raw_visual_asset")
        if raw_visual_asset is not None:
            if not isinstance(raw_visual_asset, str) or not (_GAME_ASSET.fullmatch(raw_visual_asset) or _ACHIEVEMENT_ASSET.fullmatch(raw_visual_asset) or _GENERATED_ASSET.fullmatch(raw_visual_asset)):
                raise EditorialDeckError("raw_visual_asset_invalid", "raw_visual_asset must be a current source or generated asset ID", page=page, field="presentation.content.raw_visual_asset")


def _check_scope_claim(claim: Mapping[str, Any], evidence_ids: set[str], page: int, locale: str) -> None:
    text = str(claim.get("text") or "")
    if locale == "zh-CN":
        broad = re.search(r"整座库|整个收藏|反复出现|所有作品", text)
    else:
        broad = re.search(r"whole library|entire library|across the library|repeatedly across", text, re.IGNORECASE)
    if broad and len(evidence_ids) < 3:
        raise EditorialDeckError("scope_overclaim", "a broad library-wide claim needs broader evidence closure", page=page, field="claim.text")


def _frame_and_claims(plan: Mapping[str, Any], available: set[str]) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]], set[str]]:
    frame = plan.get("editorial_frame")
    if not isinstance(frame, Mapping):
        raise EditorialDeckError("editorial_frame_missing", "editorial_frame is required", field="editorial_frame")
    mode = str(plan.get("mode") or "")
    claims: dict[str, Mapping[str, Any]] = {}
    roots: set[str] = set()
    if mode == "thesis-led":
        if "clusters" in frame or not isinstance(frame.get("guiding_question"), str) or not isinstance(frame.get("thesis"), Mapping):
            raise EditorialDeckError("thesis_frame_invalid", "thesis-led requires guiding_question and thesis and forbids clusters", field="editorial_frame")
        thesis = frame["thesis"]
        thesis_id = str(thesis.get("claim_id") or "")
        thesis_evidence = thesis.get("evidence_ids")
        if not thesis_id or not isinstance(thesis_evidence, list) or not thesis_evidence:
            raise EditorialDeckError("thesis_claim_invalid", "thesis must include claim_id, text, and evidence_ids", field="editorial_frame.thesis")
        missing = sorted(set(str(item) for item in thesis_evidence) - available)
        if missing:
            raise EditorialDeckError("evidence_not_found", "thesis references unsupported evidence: " + ", ".join(missing), field="editorial_frame.thesis.evidence_ids")
        roots.add(thesis_id)
    elif mode == "constellation-led":
        if "thesis" in frame or not isinstance(frame.get("organizing_question"), str) or not isinstance(frame.get("clusters"), list) or not 2 <= len(frame["clusters"]) <= 4:
            raise EditorialDeckError("constellation_frame_invalid", "constellation-led requires organizing_question and two to four clusters and forbids thesis", field="editorial_frame")
        cluster_ids: set[str] = set()
        for index, cluster in enumerate(frame["clusters"]):
            if not isinstance(cluster, Mapping) or not str(cluster.get("cluster_id") or "").strip():
                raise EditorialDeckError("cluster_invalid", "each cluster needs a stable cluster_id", field=f"editorial_frame.clusters[{index}]")
            cluster_id = str(cluster["cluster_id"])
            if cluster_id in cluster_ids:
                raise EditorialDeckError("cluster_duplicate", f"duplicate cluster_id '{cluster_id}'", field=f"editorial_frame.clusters[{index}].cluster_id")
            cluster_ids.add(cluster_id)
    else:
        raise EditorialDeckError("mode_invalid", "mode must be thesis-led or constellation-led", field="mode")
    pages = plan.get("pages")
    if not isinstance(pages, list):
        raise EditorialDeckError("pages_missing", "pages must be an array", field="pages")
    for index, page in enumerate(pages):
        if not isinstance(page, Mapping):
            continue
        claim = page.get("claim")
        if isinstance(claim, Mapping):
            claim_id = str(claim.get("claim_id") or "")
            if claim_id:
                if claim_id in claims:
                    raise EditorialDeckError("claim_duplicate", f"duplicate claim_id '{claim_id}'", page=int(page.get("page", index + 1)), field="claim.claim_id")
                claims[claim_id] = claim
    return dict(frame), claims, roots


def _validate_narrative(plan: Mapping[str, Any], available: set[str], locale: str) -> tuple[dict[str, Any], set[str]]:
    pages = plan.get("pages")
    if not isinstance(pages, list) or not 12 <= len(pages) <= 18:
        raise EditorialDeckError("page_count_invalid", "deck must contain 12–18 pages", field="pages")
    frame, claims, roots = _frame_and_claims(plan, available)
    mode = str(plan["mode"])
    page_claims: dict[int, Mapping[str, Any]] = {}
    cluster_by_page: dict[int, set[str]] = {}
    developed: set[str] = set()
    for expected, page in enumerate(pages, 1):
        if not isinstance(page, Mapping):
            raise EditorialDeckError("page_invalid", "page must be an object", page=expected)
        number = page.get("page")
        if number != expected:
            raise EditorialDeckError("page_number_not_consecutive", "pages must be numbered consecutively", page=int(number) if isinstance(number, int) else expected, field="page")
        move = str(page.get("narrative_move") or "")
        if move not in NARRATIVE_MOVES:
            raise EditorialDeckError("narrative_move_invalid", f"unsupported narrative_move '{move}'", page=expected, field="narrative_move")
        if (move == "establish" and expected != 1) or (move == "close" and expected != len(pages)):
            raise EditorialDeckError("narrative_move_position_invalid", "establish is only page 1 and close is only the final page", page=expected, field="narrative_move")
        if expected == 1 and move != "establish":
            raise EditorialDeckError("opening_move_missing", "page 1 must establish the deck", page=expected, field="narrative_move")
        if expected == len(pages) and move != "close":
            raise EditorialDeckError("closing_move_missing", "the final page must close the deck", page=expected, field="narrative_move")
        presentation = page.get("presentation")
        presentation_kind = _presentation_kind(presentation.get("kind") if isinstance(presentation, Mapping) else None, expected)
        if expected == 1 and presentation_kind != "opening":
            raise EditorialDeckError("opening_presentation_missing", "page 1 must use the opening presentation", page=expected, field="presentation.kind")
        if expected == len(pages) and presentation_kind != "closing":
            raise EditorialDeckError("closing_presentation_missing", "the final page must use the closing presentation", page=expected, field="presentation.kind")
        if presentation_kind == "opening" and expected != 1:
            raise EditorialDeckError("opening_presentation_repeated", "opening presentation may only appear on page 1", page=expected, field="presentation.kind")
        if presentation_kind == "closing" and expected != len(pages):
            raise EditorialDeckError("closing_presentation_repeated", "closing presentation may only appear on the final page", page=expected, field="presentation.kind")
        question = str(page.get("reader_question") or "").strip()
        if not question:
            raise EditorialDeckError("reader_question_missing", "every page needs a reader question", page=expected, field="reader_question")
        claim = page.get("claim")
        if not isinstance(claim, Mapping):
            raise EditorialDeckError("claim_missing", "every page needs one structured claim", page=expected, field="claim")
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id or claim_id not in claims:
            raise EditorialDeckError("claim_invalid", "claim_id must be unique and non-empty", page=expected, field="claim.claim_id")
        page_claims[expected] = claim
        claim_kind = str(claim.get("kind") or "")
        if claim_kind not in CLAIM_KINDS:
            raise EditorialDeckError("claim_kind_invalid", f"unsupported claim kind '{claim_kind}'", page=expected, field="claim.kind")
        evidence_ids = claim.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise EditorialDeckError("claim_evidence_missing", "every claim needs evidence_ids", page=expected, field="claim.evidence_ids")
        missing = sorted(set(str(item) for item in evidence_ids) - available)
        if missing:
            raise EditorialDeckError("evidence_not_found", "claim references unsupported evidence: " + ", ".join(missing), page=expected, field="claim.evidence_ids")
        _check_scope_claim(claim, set(str(item) for item in evidence_ids), expected, locale)
        develops = claim.get("develops")
        if not isinstance(develops, list):
            raise EditorialDeckError("claim_develops_invalid", "develops must be an array", page=expected, field="claim.develops")
        for parent in develops:
            parent_id = str(parent)
            if parent_id not in roots and parent_id not in claims:
                raise EditorialDeckError("claim_parent_missing", f"claim develops unknown claim '{parent_id}'", page=expected, field="claim.develops")
            developed.add(parent_id)
        if mode == "thesis-led" and "cluster_id" in claim:
            raise EditorialDeckError("thesis_cluster_forbidden", "thesis-led pages cannot use cluster_id", page=expected, field="claim.cluster_id")
        if mode == "constellation-led" and expected not in {1, len(pages)}:
            cluster_id = str(claim.get("cluster_id") or "")
            declared = {str(row.get("cluster_id")) for row in frame.get("clusters", []) if isinstance(row, Mapping)}
            if not cluster_id or cluster_id not in declared:
                raise EditorialDeckError("cluster_assignment_missing", "every constellation content page needs a declared cluster_id", page=expected, field="claim.cluster_id")
            cluster_by_page[expected] = {cluster_id}
    # Development must point backwards (or to the frame root), preventing a
    # page from claiming a relationship that is only introduced later.
    for number, claim in page_claims.items():
        parents = {str(item) for item in claim.get("develops", [])}
        later_ids = {str(page_claims[other].get("claim_id")) for other in page_claims if other >= number}
        if parents & later_ids:
            raise EditorialDeckError("claim_development_order_invalid", "a claim may only develop an earlier claim", page=number, field="claim.develops")

    if mode == "thesis-led":
        connected: set[str] = set(roots)
        changed = True
        while changed:
            changed = False
            for number, claim in page_claims.items():
                claim_id = str(claim["claim_id"])
                parents = {str(item) for item in claim.get("develops", [])}
                if number > 1 and parents & connected and claim_id not in connected:
                    connected.add(claim_id)
                    changed = True
        for number, claim in page_claims.items():
            if number > 1 and str(claim["claim_id"]) not in connected:
                raise EditorialDeckError("claim_not_connected", "page claim does not develop the thesis or one of its descendants", page=number, field="claim.develops")
        closing = page_claims[len(pages)]
        closing_parents = {str(item) for item in closing.get("develops", [])}
        if len(closing_parents) < 2:
            raise EditorialDeckError("closing_not_synthesis", "thesis closing must synthesize at least two evidence chains", page=len(pages), field="claim.develops")
        branches: set[tuple[str, ...]] = set()
        for parent in closing_parents:
            branch_ids: set[str] = set()
            pending = [parent]
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                node = claims.get(current)
                if isinstance(node, Mapping):
                    branch_ids.update(str(item) for item in node.get("evidence_ids", []))
                    pending.extend(str(item) for item in node.get("develops", []))
                elif current in roots and isinstance(frame.get("thesis"), Mapping):
                    branch_ids.update(str(item) for item in frame["thesis"].get("evidence_ids", []))
            if branch_ids:
                branches.add(tuple(sorted(branch_ids)))
        if len(branches) < 2:
            raise EditorialDeckError("closing_not_synthesis", "thesis closing must use at least two distinct evidence chains", page=len(pages), field="claim.develops")
    else:
        declared_clusters = {
            str(row.get("cluster_id"))
            for row in frame.get("clusters", [])
            if isinstance(row, Mapping)
        }
        cluster_pages: dict[str, list[int]] = {cluster_id: [] for cluster_id in declared_clusters}
        for number, cluster_set in cluster_by_page.items():
            for cluster_id in cluster_set:
                cluster_pages.setdefault(cluster_id, []).append(number)
        for cluster_id, numbers in cluster_pages.items():
            if len(numbers) < 2:
                raise EditorialDeckError(
                    "cluster_too_small",
                    "each constellation cluster needs at least two content pages",
                    page=min(numbers) if numbers else 1,
                    field="claim.cluster_id",
                    suggested_scope=f"cluster {cluster_id}",
                )
            seen: set[str] = set()
            for number in sorted(numbers):
                claim = page_claims[number]
                if seen and not ({str(item) for item in claim.get("develops", [])} & seen):
                    raise EditorialDeckError("cluster_claim_not_developed", "claims inside a cluster must form a development chain", page=number, field="claim.develops")
                seen.add(str(claim["claim_id"]))
        # A new cluster must visibly pick up a claim from an earlier cluster;
        # otherwise the constellation is only a sequence of unrelated facts.
        claim_cluster = {
            str(page_claims[number].get("claim_id")): next(iter(cluster_set))
            for number, cluster_set in cluster_by_page.items()
        }
        claim_page = {
            str(page_claims[number].get("claim_id")): number
            for number in cluster_by_page
        }
        cluster_order = sorted(
            ((min(numbers), cluster_id) for cluster_id, numbers in cluster_pages.items()),
            key=lambda item: item[0],
        )
        prior_clusters: set[str] = set()
        for _, cluster_id in cluster_order:
            numbers = sorted(cluster_pages[cluster_id])
            if prior_clusters and not any(
                any(
                    claim_cluster.get(str(parent)) in prior_clusters
                    and claim_page.get(str(parent), 0) < number
                    for parent in page_claims[number].get("develops", [])
                )
                for number in numbers
            ):
                raise EditorialDeckError(
                    "cluster_transition_missing",
                    "each later constellation cluster needs a claim that connects to an earlier cluster transition",
                    page=numbers[0],
                    field="claim.develops",
                    suggested_scope=f"cluster {cluster_id} transition",
                )
            prior_clusters.add(cluster_id)
        closing = page_claims[len(pages)]
        closing_clusters = {cluster_id for parent in closing.get("develops", []) for number, cluster_set in cluster_by_page.items() if str(page_claims[number].get("claim_id")) == str(parent) for cluster_id in cluster_set}
        if len(closing_clusters) < 2:
            raise EditorialDeckError("closing_not_synthesis", "constellation closing must connect claims from at least two clusters", page=len(pages), field="claim.develops")
    # Opening and closing may not quietly repeat the same proposition.
    first_text = _claim_text(page_claims[1])
    closing_text = _claim_text(page_claims[len(pages)])
    if first_text and first_text == closing_text:
        raise EditorialDeckError("opening_closing_repetition", "opening and closing must not restate the same claim", page=len(pages), field="claim.text")
    return frame, developed


def _subject_display_names(content: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]], labels: Mapping[str, Any] | None) -> dict[str, str]:
    games, _, _ = _subject_ids(content)
    return {game_id: _entity_label(labels, catalog, game_id) for game_id in games}


def _compile_content(page: Mapping[str, Any], raw_content: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]], labels: Mapping[str, Any] | None, locale: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    number = int(page["page"])
    page_evidence = {str(item) for item in page["claim"]["evidence_ids"]}
    kind = _presentation_kind((page.get("presentation") or {}).get("kind"), number)
    if not isinstance(raw_content, Mapping):
        raise EditorialDeckError("presentation_content_missing", "presentation.content must be an object", page=number, field="presentation.content")
    content = _resolve_text_tree(raw_content, page_evidence, catalog, locale, labels, number, "presentation.content")
    if not isinstance(content, dict):
        raise EditorialDeckError("presentation_content_invalid", "presentation.content must be an object", page=number, field="presentation.content")
    names = _subject_display_names(content, catalog, labels)
    if kind in {"quantitative-comparison", "qualitative-comparison"}:
        _validate_comparison(page, kind, content, names)

    measures: list[dict[str, Any]] = []
    legends: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []

    def resolve_measure_node(node: Any, field: str) -> dict[str, Any]:
        if not isinstance(node, Mapping):
            raise EditorialDeckError("measure_invalid", "measure must be an object", page=number, field=field)
        try:
            resolved = resolve_measure(node, catalog, locale)
        except (TypeError, ValueError, KeyError) as exc:
            raise EditorialDeckError("measure_invalid", str(exc), page=number, field=field) from exc
        result = resolved.as_dict()
        measures.append(result)
        return result

    def visit(node: Any, field: str = "presentation.content") -> Any:
        if isinstance(node, Mapping):
            result: dict[str, Any] = {}
            for key, child in node.items():
                child_field = f"{field}.{key}"
                if str(key) == "measure":
                    result[str(key)] = resolve_measure_node(child, child_field)
                elif str(key) == "subject" and isinstance(child, Mapping):
                    resolved_subject = visit(child, child_field)
                    if isinstance(resolved_subject, dict):
                        game_id = str(resolved_subject.get("game_id") or "")
                        if game_id:
                            resolved_subject["display_name"] = names.get(game_id, _entity_label(labels, catalog, game_id))
                    result[str(key)] = resolved_subject
                elif str(key) == "achievement" and isinstance(child, Mapping):
                    resolved_achievement = visit(child, child_field)
                    if isinstance(resolved_achievement, dict):
                        achievement_id = str(resolved_achievement.get("achievement_id") or "")
                        if achievement_id:
                            resolved_achievement["display_name"] = _entity_label(labels, catalog, achievement_id)
                    result[str(key)] = resolved_achievement
                else:
                    result[str(key)] = visit(child, child_field)
            return result
        if isinstance(node, list):
            return [visit(child, f"{field}[{index}]") for index, child in enumerate(node)]
        return node

    compiled_content = visit(content)
    if kind in {"series-atlas", "pattern-atlas"}:
        _validate_atlas(page, kind, compiled_content, catalog)
    # archive-density keeps the denominator as a direct measure reference,
    # unlike bins whose references live under a ``measure`` field. Resolve it
    # here so authors do not need to duplicate canonical values or dimensions.
    if kind == "archive-density" and isinstance(content.get("denominator"), Mapping):
        compiled_content["denominator"] = resolve_measure_node(
            content["denominator"],
            "presentation.content.denominator",
        )
    games, achievements, assets = _subject_ids(compiled_content)
    for _, node in _walk(compiled_content):
        if isinstance(node, Mapping) and isinstance(node.get("subject"), Mapping):
            subject = node["subject"]
            game_id = str(subject.get("game_id") or "")
            asset_id = str(subject.get("asset_id") or "")
            if game_id and game_id not in names:
                names[game_id] = _entity_label(labels, catalog, game_id)
            if game_id and asset_id and not (asset_id.startswith(game_id + ":") or _GENERATED_ASSET.fullmatch(asset_id)):
                raise EditorialDeckError("subject_asset_mismatch", "subject asset is not owned by the subject game", page=number, field="presentation.content")

    if kind == "archive-density":
        denominator = compiled_content.get("denominator")
        bins = compiled_content.get("bins")
        if not isinstance(denominator, Mapping) or not isinstance(bins, list) or not 2 <= len(bins) <= 5:
            raise EditorialDeckError("archive_content_invalid", "archive-density needs denominator and two to five bins", page=number, field="presentation.content")
        if denominator.get("dimension") != "count" or not denominator.get("canonical_value"):
            raise EditorialDeckError("archive_dimension_invalid", "archive denominator must be a positive count", page=number, field="presentation.content.denominator")
        numeric: list[float] = []
        for index, row in enumerate(bins):
            if not isinstance(row, Mapping) or not isinstance(row.get("measure"), Mapping) or row["measure"].get("dimension") != "count":
                raise EditorialDeckError("archive_bin_invalid", "archive bins must be count measures", page=number, field=f"presentation.content.bins[{index}]")
            numeric.append(float(row["measure"].get("canonical_value", 0)))
        try:
            allocations, remainder = largest_remainder_allocation(numeric, denominator["canonical_value"], units=100)
        except (TypeError, ValueError) as exc:
            raise EditorialDeckError("archive_bin_invalid", str(exc), page=number, field="presentation.content.bins") from exc
        denominator_value = float(denominator["canonical_value"])
        used = sum(numeric)
        if used > denominator_value:
            raise EditorialDeckError("archive_bin_invalid", "archive bins exceed their denominator", page=number, field="presentation.content.bins")
        for index, row in enumerate(bins):
            row["allocation_units"] = allocations[index]
            row["allocation_percent"] = allocations[index]
            legends.append({"label": str(row.get("label") or ""), "value": row["measure"].get("raw_value"), "allocation_units": allocations[index], "evidence_ids": list(row.get("evidence_ids", []))})
        if denominator_value > used:
            bins.append({"label": catalog_for(locale).text("archive_remainder", "Other"), "generated": True, "value": denominator_value - used, "allocation_units": remainder, "allocation_percent": remainder, "evidence_ids": list(page_evidence)})
            legends.append({"label": catalog_for(locale).text("archive_remainder", "Other"), "value": denominator_value - used, "allocation_units": remainder, "evidence_ids": list(page_evidence), "generated": True})
    if kind == "achievement-anomaly":
        item = compiled_content.get("item")
        if isinstance(item, Mapping) and isinstance(item.get("subject"), Mapping):
            asset_id = str(item["subject"].get("asset_id") or "")
            parts = asset_id.split(":")
            if len(parts) != 3 or parts[0] != "game" or not parts[1].isdigit() or parts[2] != "portrait":
                raise EditorialDeckError(
                    "anomaly_portrait_asset_required",
                    "achievement-anomaly game subjects must use game:<appid>:portrait assets",
                    page=number,
                    field="presentation.content.item.subject.asset_id",
                )
    if kind == "quantitative-comparison":
        items = compiled_content.get("items")
        if not isinstance(items, list) or len(items) != 2:
            raise EditorialDeckError("comparison_item_count_invalid", "quantitative comparison needs two items", page=number, field="presentation.content.items")
        item_measures = [item.get("measure") for item in items if isinstance(item, Mapping)]
        if len(item_measures) != 2 or not all(isinstance(item, Mapping) for item in item_measures) or not measures_are_comparable_from_dict(item_measures[0], item_measures[1]):
            raise EditorialDeckError("comparison_dimension_invalid", "quantitative comparison items must share a dimension and canonical unit", page=number, field="presentation.content.items")
        bindings.extend({"game_id": str(item["subject"].get("game_id")), "asset_id": str(item["subject"].get("asset_id")), "measure": item.get("measure")} for item in items if isinstance(item, Mapping))
    elif kind in {"qualitative-comparison", "series-atlas", "pattern-atlas", "temporal-strata", "achievement-anomaly"}:
        items: list[Any] = []
        if kind == "achievement-anomaly" and isinstance(compiled_content.get("item"), Mapping):
            items = [compiled_content["item"]]
        else:
            for key in ("items", "strata"):
                if isinstance(compiled_content.get(key), list):
                    items = list(compiled_content[key])
                    break
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("subject"), Mapping):
                bindings.append({"game_id": str(item["subject"].get("game_id")), "asset_id": str(item["subject"].get("asset_id")), "measure": item.get("measure"), "evidence_ids": list(item.get("evidence_ids", []))})
    return compiled_content, measures, legends, bindings, [{"field": "presentation.content", "kind": kind, "games": sorted(games), "achievements": sorted(achievements), "assets": sorted(assets), "measures": [dict(item) for item in measures]}]


def measures_are_comparable_from_dict(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return bool(left.get("dimension") == right.get("dimension") and left.get("canonical_unit") == right.get("canonical_unit") and left.get("canonical_value") is not None and right.get("canonical_value") is not None)


def _matched_game_ids(text: str, names: Mapping[str, str], games: Iterable[str]) -> list[str]:
    lowered = text.casefold()
    matches = [game_id for game_id in games if names.get(game_id) and names[game_id].casefold() in lowered]
    return [
        game_id
        for game_id in matches
        if not any(
            other != game_id
            and len(names.get(other, "")) > len(names.get(game_id, ""))
            and names[game_id].casefold() in names[other].casefold()
            and names[other].casefold() in lowered
            for other in matches
        )
    ]


def _identity_owner(headline: str, names: Mapping[str, str], games: set[str]) -> str:
    named = _matched_game_ids(headline, names, games)
    # A page-level owner is safe only when the headline names its single
    # subject. Multi-subject pages keep all identities with the renderer.
    return "headline" if len(games) == 1 and len(named) == 1 else ("renderer" if games else "none")


def _validate_identity_copy(reader_copy: Mapping[str, Any], names: Mapping[str, str], games: set[str], owner: str, page: int) -> None:
    """Ensure the identity owner is the only reader-facing name occurrence."""

    if not games:
        return
    headline = str(reader_copy.get("headline") or "")
    named_games = _matched_game_ids(headline, names, games)
    if owner == "headline":
        if len(named_games) != 1:
            raise EditorialDeckError("identity_owner_ambiguous", "headline identity must name exactly one visible game", page=page, field="reader_copy.headline")
        for field, value in _copy_fields(reader_copy).items():
            if field != "headline" and _matched_game_ids(value, names, named_games):
                raise EditorialDeckError("identity_duplicate", "a game name may appear only once on a page", page=page, field=f"reader_copy.{field}")
    elif owner == "renderer":
        for field, value in _copy_fields(reader_copy).items():
            if _matched_game_ids(value, names, games):
                raise EditorialDeckError("identity_duplicate", "renderer-owned identity must not repeat a game name in reader copy", page=page, field=f"reader_copy.{field}")


def _copy_fields(reader_copy: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in reader_copy.items() if isinstance(value, str) and value.strip()}


def _copy_signature(value: Any) -> str:
    return _WHITESPACE.sub(" ", str(value or "").casefold()).strip()


def _validate_caption_policy(reader_copy: Mapping[str, Any], page: int) -> None:
    """Require an explicit editorial reason before a caption can enter a deck."""

    caption = str(reader_copy.get("caption") or "").strip()
    has_opt_in = "caption_required" in reader_copy
    has_reason = "caption_reason" in reader_copy
    if not caption:
        if has_opt_in or has_reason:
            raise EditorialDeckError(
                "caption_required_without_caption",
                "caption_required and caption_reason require reader_copy.caption",
                page=page,
                field="reader_copy.caption_required" if has_opt_in else "reader_copy.caption_reason",
            )
        return
    if reader_copy.get("caption_required") is not True:
        raise EditorialDeckError(
            "caption_opt_in_required",
            "reader_copy.caption requires caption_required=true",
            page=page,
            field="reader_copy.caption_required",
        )
    reason = str(reader_copy.get("caption_reason") or "").strip()
    if not reason:
        raise EditorialDeckError(
            "caption_reason_missing",
            "caption_reason must explain why the visual or encoding is not self-explanatory",
            page=page,
            field="reader_copy.caption_reason",
        )
    if _GENERIC_CAPTION_REASON.fullmatch(_copy_signature(reason)):
        raise EditorialDeckError(
            "caption_reason_generic",
            "caption_reason must identify the non-obvious visual or encoding, not request a generic explanation",
            page=page,
            field="reader_copy.caption_reason",
        )


def _validate_caption_information_gain(reader_copy: Mapping[str, Any], claim_text: str, page: int) -> None:
    caption_signature = _copy_signature(reader_copy.get("caption"))
    if not caption_signature:
        return
    for field in ("headline", "support"):
        if caption_signature == _copy_signature(reader_copy.get(field)):
            raise EditorialDeckError(
                "caption_no_information_gain",
                "caption must add interpretation beyond the page's headline and support copy",
                page=page,
                field="reader_copy.caption",
            )
    if claim_text and caption_signature == _copy_signature(claim_text):
        raise EditorialDeckError(
            "caption_no_information_gain",
            "caption must add interpretation beyond the page claim",
            page=page,
            field="reader_copy.caption",
        )


def _reader_audit(pages: list[dict[str, Any]], locale: str) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    page_verdicts: list[dict[str, Any]] = []
    endpoint_signature: str | None = None
    if len(pages) >= 2:
        first_combined = " ".join((_claim_text(pages[0].get("claim", {})) if isinstance(pages[0].get("claim"), Mapping) else "", str(pages[0]["reader_copy"].get("headline") or "")))
        last_combined = " ".join((_claim_text(pages[-1].get("claim", {})) if isinstance(pages[-1].get("claim"), Mapping) else "", str(pages[-1]["reader_copy"].get("headline") or "")))
        first_signature = _conclusion_signature(first_combined, locale)
        last_signature = _conclusion_signature(last_combined, locale)
        if first_signature and first_signature == last_signature:
            endpoint_signature = first_signature
            issues.append({"code": "opening_closing_repetition", "page": int(pages[-1]["page"]), "field": "claim.text", "message": "opening and closing repeat the same core conclusion", "suggested_scope": f"page {pages[-1]['page']} claim.text"})
    for page in pages:
        number = int(page["page"])
        copy = page["reader_copy"]
        headline = str(copy.get("headline") or "")
        kind = str(page.get("presentation", {}).get("kind") or "")
        gain = _headline_gain_error(headline, kind, locale)
        method = _method_error(" ".join(_copy_fields(copy).values()), locale)
        if gain:
            issues.append({"code": gain, "page": number, "field": "reader_copy.headline", "message": "headline does not add a reader-facing relation, magnitude, contrast, pattern, consequence, or synthesis", "suggested_scope": f"page {number} reader_copy.headline"})
        if method:
            issues.append({"code": method, "page": number, "field": "reader_copy", "message": "reader copy exposes backstage method or provenance language", "suggested_scope": f"page {number} reader_copy"})
        page_verdicts.append({"page": number, "headline_gain": "fail" if gain else "pass", "reason": "rewrite the headline to add a relationship or meaning" if gain else "headline adds a reader-facing observation beyond the encoding"})
    for first_index, first in enumerate(pages):
        first_text = _WHITESPACE.sub(" ", str(first["reader_copy"].get("headline") or "").casefold()).strip()
        for later in pages[first_index + 1 :]:
            later_text = _WHITESPACE.sub(" ", str(later["reader_copy"].get("headline") or "").casefold()).strip()
            if first_text and first_text == later_text and not (endpoint_signature and later is pages[-1] and first is pages[0]):
                issues.append({"code": "headline_repeated", "page": int(later["page"]), "field": "reader_copy.headline", "message": "the same headline returns without a new observation", "suggested_scope": f"page {later['page']} reader_copy.headline"})
    seen_claims: dict[str, int] = {}
    seen_signatures: dict[str, int] = {}
    for page in pages:
        number = int(page["page"])
        claim = page.get("claim") if isinstance(page.get("claim"), Mapping) else {}
        claim_text = _claim_text(claim)
        if claim_text and claim_text in seen_claims and not (endpoint_signature and page is pages[-1] and seen_claims[claim_text] == int(pages[0]["page"])):
            issues.append({"code": "claim_repeated", "page": number, "field": "claim.text", "message": "the same structured claim returns without a new observation", "suggested_scope": f"page {number} claim.text"})
        elif claim_text:
            seen_claims[claim_text] = number
        combined = " ".join((claim_text, str(page["reader_copy"].get("headline") or "")))
        signature = _conclusion_signature(combined, locale)
        if signature and signature in seen_signatures and not (endpoint_signature and page is pages[-1] and signature == endpoint_signature):
            issues.append({"code": "claim_repeated", "page": number, "field": "claim.text", "message": "the same core conclusion returns without a new synthesis", "suggested_scope": f"page {number} claim.text"})
        elif signature:
            seen_signatures[signature] = number
    return {"passed": not issues, "issues": issues, "pages": page_verdicts}


def deck_schema_fingerprint() -> str:
    payload = _schema_path().read_bytes()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def compile_editorial_deck(
    deck_plan: Mapping[str, Any],
    semantic_findings: Mapping[str, Any] | None,
    evidence: Mapping[str, Any],
    localized_labels: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compile one deck plan into the only planning artifact downstream may read."""

    if not isinstance(deck_plan, Mapping):
        raise EditorialDeckError("schema_invalid", "deck plan must be an object")
    _validate_schema(deck_plan)
    locale = normalize_report_locale(str(deck_plan.get("locale") or ""))
    catalog = _catalog(evidence)
    locale_catalog = catalog_for(locale)
    references = scan_label_references(deck_plan)
    labels_for_compile = localized_labels if isinstance(localized_labels, Mapping) and localized_labels else None
    if labels_for_compile is not None:
        try:
            label_locale = normalize_report_locale(str(labels_for_compile.get("report_locale") or ""))
        except ValueError as exc:
            raise EditorialDeckError("localized_labels_stale", "localized labels do not match the current locale, catalog, plan references, or fingerprint", field="localized_labels") from exc
        expected_games = set(references["games"])
        expected_achievements = set(references["achievements"])
        if (
            label_locale != locale
            or labels_for_compile.get("catalog_version") != locale_catalog.catalog_version
            or set(labels_for_compile.get("games", {})) != expected_games
            or set(labels_for_compile.get("achievements", {})) != expected_achievements
            or labels_for_compile.get("label_fingerprint") != compute_label_fingerprint(labels_for_compile)
        ):
            raise EditorialDeckError("localized_labels_stale", "localized labels do not match the current locale, catalog, plan references, or fingerprint", field="localized_labels")
    else:
        labels_for_compile = {
            "report_locale": locale,
            "catalog_version": locale_catalog.catalog_version,
            "games": {
                game_id: {"display_name": _entity_label(None, catalog, game_id), "source": "canonical"}
                for game_id in references["games"]
            },
            "achievements": {
                achievement_id: {
                    "display_name": _entity_label(None, catalog, achievement_id),
                    "description": str(fact_value(catalog[achievement_id], "description") or ""),
                    "source": "canonical",
                }
                for achievement_id in references["achievements"]
                if achievement_id in catalog
            },
            "failures": [],
        }
        labels_for_compile["label_fingerprint"] = compute_label_fingerprint(labels_for_compile)
    available = set(catalog)
    # Semantic findings are bounded upstream, but their evidence closure still
    # belongs to the deterministic compiler.
    if isinstance(semantic_findings, Mapping):
        for index, finding in enumerate(semantic_findings.get("findings", [])) if isinstance(semantic_findings.get("findings"), list) else ():
            if isinstance(finding, Mapping):
                missing = sorted(set(str(item) for item in finding.get("evidence_ids", [])) - available)
                if missing:
                    raise EditorialDeckError("semantic_evidence_not_found", "semantic finding references unsupported evidence: " + ", ".join(missing), field=f"semantic_findings.findings[{index}].evidence_ids")
    frame, developed = _validate_narrative(deck_plan, available, locale)
    raw_pages = deck_plan["pages"]
    compiled_pages: list[dict[str, Any]] = []
    encoded_claims: list[dict[str, Any]] = []
    identity_owners: dict[str, str] = {}
    for raw_page in raw_pages:
        number = int(raw_page["page"])
        claim = raw_page["claim"]
        claim_evidence = {str(item) for item in claim["evidence_ids"]}
        presentation = raw_page["presentation"]
        raw_content = presentation.get("content", {}) if isinstance(presentation, Mapping) else {}
        content, measures, legends, bindings, encodings = _compile_content(raw_page, raw_content, catalog, labels_for_compile, locale)
        _validate_subject_nodes(content, catalog, number)
        nested = _evidence_refs(content)
        missing_closure = sorted(nested - claim_evidence)
        if missing_closure:
            raise EditorialDeckError("evidence_closure_missing", "presentation evidence is outside the page claim closure: " + ", ".join(missing_closure), page=number, field="claim.evidence_ids")
        names = _subject_display_names(content, catalog, labels_for_compile)
        games, achievements, assets = _subject_ids(content)
        missing_subject_evidence = sorted(games - claim_evidence)
        if missing_subject_evidence:
            raise EditorialDeckError("subject_evidence_missing", "every visible game subject must be included in the page evidence closure: " + ", ".join(missing_subject_evidence), page=number, field="claim.evidence_ids")
        missing_achievement_evidence = sorted(achievements - claim_evidence)
        if missing_achievement_evidence:
            raise EditorialDeckError("achievement_evidence_missing", "every visible achievement subject must be included in the page evidence closure: " + ", ".join(missing_achievement_evidence), page=number, field="claim.evidence_ids")
        reader_raw = raw_page["reader_copy"]
        _validate_caption_policy(reader_raw, number)
        visible_reader_raw = {
            key: value
            for key, value in reader_raw.items()
            if key in _VISIBLE_READER_COPY_FIELDS
        }
        reader_copy = _resolve_text_tree(visible_reader_raw, claim_evidence, catalog, locale, labels_for_compile, number, "reader_copy")
        if not isinstance(reader_copy, dict) or not str(reader_copy.get("headline") or "").strip():
            raise EditorialDeckError("headline_missing", "reader_copy.headline is required", page=number, field="reader_copy.headline")
        _validate_caption_information_gain(reader_copy, "", number)
        # Optional empty slots are removed so no renderer is tempted to fill a
        # page with placeholder prose.
        reader_copy = {key: value for key, value in reader_copy.items() if key == "headline" or not (isinstance(value, str) and not value.strip())}
        headline_signature = _WHITESPACE.sub(" ", str(reader_copy["headline"]).casefold()).strip()
        reader_copy = {
            key: value
            for key, value in reader_copy.items()
            if key == "headline" or _WHITESPACE.sub(" ", str(value).casefold()).strip() != headline_signature
        }
        owner = _identity_owner(str(reader_copy["headline"]), names, games)
        _validate_identity_copy(reader_copy, names, games, owner, number)
        identity_owners[str(number)] = owner
        page_encodings = [dict(item) for item in encodings]
        page_encodings[0]["claim_id"] = str(claim["claim_id"])
        encoded_claims.append({"page": number, "claim_id": str(claim["claim_id"]), "items": page_encodings})
        compiled_claim = deepcopy(dict(claim))
        compiled_claim["text"] = _resolve_text(compiled_claim.get("text", ""), claim_evidence, catalog, locale, labels_for_compile, number, "claim.text")
        _validate_caption_information_gain(reader_copy, str(compiled_claim.get("text") or ""), number)
        compiled_page = {
            "page": number,
            "narrative_move": str(raw_page["narrative_move"]),
            "reader_question": str(raw_page["reader_question"]),
            "claim": compiled_claim,
            "reader_copy": reader_copy,
            "presentation": {"kind": _presentation_kind(presentation.get("kind"), number), "content": content},
            "evidence_ids": sorted(claim_evidence),
            "visible_game_ids": sorted(games),
            "asset_ids": sorted(assets),
            "measure_bindings": measures,
            "legend_bindings": legends,
            "item_bindings": bindings,
            "encoding_kind": _presentation_kind(presentation.get("kind"), number),
            "encoded_claims": page_encodings,
            "visible_identity_owner": owner,
            "developed_claim_ids": sorted(str(item) for item in claim.get("develops", [])),
        }
        compiled_pages.append(compiled_page)

    audit = _reader_audit(compiled_pages, locale)
    if not audit["passed"]:
        issue = audit["issues"][0]
        raise EditorialDeckError(str(issue["code"]), str(issue["message"]), page=int(issue["page"]), field=str(issue["field"]), suggested_scope=str(issue["suggested_scope"]))
    # Compare opening and closing headlines after token resolution as well.
    if _WHITESPACE.sub(" ", str(compiled_pages[0]["reader_copy"]["headline"]).casefold()).strip() == _WHITESPACE.sub(" ", str(compiled_pages[-1]["reader_copy"]["headline"]).casefold()).strip():
        raise EditorialDeckError("opening_closing_repetition", "opening and closing must not share the same reader headline", page=len(compiled_pages), field="reader_copy.headline")
    core = {
        "format": "steam-visualogue-compiled-deck",
        "locale": locale,
        "catalog_version": locale_catalog.catalog_version,
        "label_fingerprint": str(labels_for_compile["label_fingerprint"]),
        "title": str(deck_plan["title"]),
        "mode": str(deck_plan["mode"]),
        "editorial_frame": deepcopy(frame),
        "pages": compiled_pages,
        "encoded_claims": encoded_claims,
        "visible_identity_owner": identity_owners,
        "developed_claim_ids": sorted(developed),
        "deck_schema_fingerprint": deck_schema_fingerprint(),
        "reader_audit": audit,
    }
    payload = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    core["compiled_deck_fingerprint"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    return core


__all__ = [
    "CLAIM_KINDS",
    "EditorialDeckError",
    "NARRATIVE_MOVES",
    "PRESENTATION_KINDS",
    "compile_editorial_deck",
    "deck_schema_fingerprint",
    "measures_are_comparable_from_dict",
]
