"""Report-locale contracts, reader-facing catalogues, and presentation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from pathlib import Path
from typing import Any, Mapping

from .io_utils import read_json, write_json


SUPPORTED_REPORT_LOCALES = ("en-US", "zh-CN")
RUN_CONFIG_FORMAT = "steam-visualogue-run-config"
SETTINGS_FORMAT = "steam-visualogue-settings"
SETTINGS_FILE_NAME = ".steam-visualogue-settings.json"
CATALOG_VERSION = "steam-visualogue-catalog"

_ALIASES = {
    "en": "en-US",
    "en-us": "en-US",
    "english": "en-US",
    "英文": "en-US",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "chinese": "zh-CN",
    "simplified-chinese": "zh-CN",
    "中文": "zh-CN",
    "简体中文": "zh-CN",
}

_SYSTEM_STRINGS = {
    "en-US": {
        "product_name": "STEAM VISUALOGUE",
        "page": "PAGE",
        "archive_remainder": "OTHER / NO COMPARABLE DATA",
    },
    "zh-CN": {
        "product_name": "STEAM VISUALOGUE",
        "page": "页",
        "archive_remainder": "其他／无可比数据",
    },
}

_PATTERNS = {
    "en-US": {
        "internal_data_state": (
            r"(?:\b(?:achievement\s+)?description\s+(?:is\s+)?(?:unavailable|missing|unknown)\b|"
            r"\b(?:metadata|source data)\s+(?:is\s+)?(?:unavailable|missing|unknown)\b|"
            r"\bsemantic(?:ally)?\s+(?:status\s+)?unchecked\b)",
        ),
        "backstage_method": (
            r"(?:\bscope\s*:|\bevidence[- ]grounded\b[^.!?]{0,100}\b(?:portrait|report|essay|account)\b|"
            r"\bownership\b[^.!?]{0,100}\brecorded\s+playtime\b[^.!?]{0,100}\b(?:different|separate)\s+(?:layers|measures|signals)\b|"
            r"\bobservable\s+achievement\s+activity\b[^.!?]{0,100}\bnot\b[^.!?]{0,80}\b(?:complete|full|whole)\s+(?:history|record)\b|"
            r"\bsemantic\s+inversion\b|\bnot\s+(?:a\s+)?difficulty\s+(?:score|rating)\b|"
            r"\b(?:frequency|rarity)\s+(?:measure|measures|metric|metrics)\b[^.!?]{0,100}\bnot\b[^.!?]{0,60}\bdifficulty\b|"
            r"\bcannot\s+(?:stand\s+in|substitute)\s+for\b)",
        ),
        "editorial_process": (
            r"(?:\bmarks?\s+(?:thin|thins|thicken|thickens|encode|encodes|represent|represents)\b|"
            r"\b(?:gives?|adds?)\b[^.!?]{0,60}\b(?:grain|texture)\b|"
            r"\bdestinations?\b[^.!?]{0,60}\binvitations?\b|\bthe\s+line\s+returns\b|"
            r"\bachievement\s+time\b[^.!?]{0,80}\bshape\b|"
            r"\b(?:wide\s+field|deep\s+anchors?|open\s+edges?)\b|"
            r"\b(?:this|the)\s+(?:page|layout|composition|visual|graphic|chart)\b|"
            r"\b(?:visual\s+hierarchy|page\s+rhythm|quiet\s+close|story\s+turn)\b)",
        ),
        "page_narration": (
            r"(?:\b(?:this|the)\s+(?:page|layout|composition|deck|report)\b[^.!?]{0,80}\b(?:shows?|presents?|uses?|places?|contains?|is\s+built|was\s+assembled)\b|"
            r"\bon\s+this\s+page\b|\bpage\s+(?:layout|construction|structure)\b)",
        ),
        "source_provenance": (
            r"(?:\b(?:source|source\s+field|data\s+field|record|metadata|provenance|dataset|evidence\s+id)\b[^.!?]{0,90}\b(?:comes?\s+from|is\s+from|contains?|includes?|provides?|provided|bound|mapped|selected|was\s+used)\b|"
            r"\b(?:field|record)\s+(?:name|value)\b)",
        ),
        "review_process": (
            r"(?:\b(?:reviewer|review\s+process|prompt|model|agent|assignment|selection\s+process)\b|"
            r"\b(?:selected|chosen|generated)\s+by\b|\baccording\s+to\s+the\s+prompt\b)",
        ),
        "defensive_disclaimer": (
            r"(?:\b(?:this\s+(?:report|page|analysis)|we|the\s+analysis)\b[^.!?]{0,100}\b(?:does\s+not|doesn't|cannot|can't|will\s+not|won't|not\s+intended|outside\s+the\s+scope|do\s+not\s+infer|makes?\s+no\s+claim)\b|"
            r"\b(?:not\s+a\s+diagnosis|not\s+evidence|cannot\s+prove|does\s+not\s+prove|not\s+discussed)\b)",
        ),
        "assembly_language": (
            r"(?:\b(?:this\s+report|the\s+report|the\s+deck|the\s+essay|the\s+closing|the\s+ending)\b[^.!?]{0,100}\b(?:combines?|assembles?|arranges?|brings?\s+together|returns?\s+to|moves?\s+through|is\s+built)\b|"
            r"\b(?:read|look\s+at|notice|see)\s+(?:the\s+)?(?:next\s+)?(?:page|report|deck)\b)",
        ),
        "encoding_restated": (
            r"(?:\b(?:all|these|the)\s+(?:values|numbers|figures)\b[^.!?]{0,60}\b(?:use|share|are\s+shown\s+in)\b[^.!?]{0,40}\b(?:the\s+same\s+unit|hours?|minutes?|days?|percent|scale)\b|"
            r"\b(?:same\s+denominator|denominator\s+is|unit\s+is|bars?\s+use|scale\s+is)\b)",
        ),
        "generic_support": (
            r"(?:\bthis\s+is\s+(?:a\s+)?(?:concrete\s+)?example\b|\b(?:this|an?|the)\s+item\s+is\s+(?:a\s+)?(?:concrete\s+)?example\b|\bserves?\s+as\s+an?\s+example\b|"
            r"\b(?:supports?|illustrates?)\s+(?:(?:the|a)\s+)?(?:pattern|claim|story)\b|"
            r"\bmakes?\s+(?:the\s+)?contrast\s+concrete\b|"
            r"\bcombines?\s+(?:the\s+)?(?:earlier|previous)\s+(?:measures?|values?)\b)",
        ),
    },
    "zh-CN": {
        "internal_data_state": (
            r"(?:元数据(?:缺失|不可用|未知)|描述(?:缺失|不可用|未知)|未经语义检查|语义状态未检查)",
        ),
        "backstage_method": (
            r"(?:范围\s*[:：]|证据驱动的?(?:报告|画像|文章)|语义(?:反转|检查|分类)|"
            r"(?:不代表|只是)(?:难度|频率指标)|(?:完整|全部)历史|频率(?:指标|度量)|"
            r"所有权与记录的?游玩时间|无法代表|不能替代)",
        ),
        "editorial_process": (
            r"(?:(?:本页|此页|页面|布局|构图|图表)(?:展示|呈现|说明)|"
            r"(?:标记|线条|纹理)(?:编码|代表|增加)|视觉层次|页面节奏|安静收束|故事转折|"
            r"(?:宽阔区域|深层锚点|开放边缘))",
        ),
        "page_narration": (
            r"(?:(?:本页|此页|页面|布局|构图|报告|册子|图表)(?:展示|呈现|说明|安排|使用|构成)|"
            r"(?:在本页|在这一页)|页面(?:布局|结构|构造))",
        ),
        "source_provenance": (
            r"(?:(?:来源|源字段|字段|记录|元数据|数据|证据编号)(?:来自|包含|包括|提供|绑定|映射|选取|记录)|"
            r"(?:来源)?记录(?:来自|包含|包括|提供|绑定|映射|选取)|"
            r"(?:字段|记录)(?:名称|数值))",
        ),
        "review_process": (
            r"(?:审阅者|评审过程|提示词|模型|代理|任务|选择过程|挑选|选中|生成)",
        ),
        "defensive_disclaimer": (
            r"(?:(?:本报告|本页|这一页|我们|分析)(?:不讨论|不推断|无法|不能|不是|不代表|不声称|范围之外)|"
            r"(?:不能证明|无法证明|不构成诊断|并非证据))",
        ),
        "assembly_language": (
            r"(?:(?:本报告|报告|本册|结尾|结语|这一页)(?:组合|汇总|拼接|安排|回到|把[^。！？]{0,30}放在一起)|"
            r"(?:将|把)前面的(?:指标|数值|内容)(?:组合|汇总)|"
            r"(?:请|读者请)?(?:阅读|查看)(?:下一页|本页|本报告))",
        ),
        "encoding_restated": (
            r"(?:(?:所有|这些|图中)(?:数值|数字|值)(?:使用|采用)(?:相同|同一)单位|"
            r"(?:分母|单位|尺度)(?:相同|是|为)|(?:数字|数值)以(?:小时|分钟|天|百分比)显示)",
        ),
        "generic_support": (
            r"(?:(?:这是|作为)一个(?:具体)?例子|(?:这一|该)项是一个(?:具体)?例子|支持(?:这一|该)模式|让(?:这一)?对照更具体|"
            r"将前面的(?:指标|数值|内容)组合)",
        ),
    },
}

_READER_CLAIM_TERMS = {
    "en-US": {
        "metrics": {
            "playtime": "playtime",
            "time": "time",
            "hour": "hours",
            "hours": "hours",
            "h": "hours",
            "minute": "minutes",
            "minutes": "minutes",
            "day": "days",
            "days": "days",
            "completion": "completion",
            "complete": "completion",
            "percent": "percent",
            "percentage": "percent",
            "ratio": "ratio",
            "count": "count",
            "number": "count",
            "titles": "titles",
            "title": "titles",
            "rank": "rank",
            "rarity": "rarity",
        },
        "categories": {
            "rpg": "rpg",
            "role playing": "rpg",
            "role-playing": "rpg",
            "strategy": "strategy",
            "strategic": "strategy",
            "adventure": "adventure",
            "action": "action",
            "simulation": "simulation",
            "sim": "simulation",
            "puzzle": "puzzle",
            "tool": "tool",
        },
        "relationships": {
            "category": ("belongs", "genre", "category", "type"),
            "comparison": ("more", "less", "most", "least", "higher", "lower", "contrast", "compare"),
            "return-gap": ("return", "returned", "gap", "interval"),
            "completion": ("complete", "completion"),
            "attention": ("attention", "playtime"),
        },
        "scopes": {
            "overall": "overall",
            "whole library": "whole-library",
            "selected": "selected",
            "per page": "per-page",
            "across the deck": "deck-wide",
        },
        "contrast": ("but", "however", "while", "unlike", "contrast", "whereas", "yet"),
        "consequence": ("therefore", "so", "leads", "means", "result", "results", "because"),
        "synthesis": ("overall", "together", "across", "combined", "finally", "synthesis"),
    },
    "zh-CN": {
        "metrics": {
            "时长": "time",
            "游玩": "playtime",
            "小时": "hours",
            "分钟": "minutes",
            "天": "days",
            "天数": "days",
            "完成": "completion",
            "完成度": "completion",
            "百分比": "percent",
            "比例": "ratio",
            "数量": "count",
            "标题": "titles",
            "排名": "rank",
            "稀有度": "rarity",
        },
        "categories": {
            "角色扮演": "rpg",
            "策略": "strategy",
            "冒险": "adventure",
            "动作": "action",
            "模拟": "simulation",
            "解谜": "puzzle",
            "工具": "tool",
        },
        "relationships": {
            "category": ("属于", "类型", "类别"),
            "comparison": ("更多", "更少", "最高", "最低", "对照", "比较"),
            "return-gap": ("回返", "间隔", "回来"),
            "completion": ("完成", "完成度"),
            "attention": ("游玩", "注意力", "时长"),
        },
        "scopes": {
            "总体": "overall",
            "整个收藏": "whole-library",
            "选中": "selected",
            "每页": "per-page",
            "整册": "deck-wide",
        },
        "contrast": ("相比", "相反", "然而", "却", "而"),
        "consequence": ("因此", "意味着", "带来", "让", "所以"),
        "synthesis": ("整体", "总的来说", "合在一起", "最后", "共同"),
    },
}


@dataclass(frozen=True)
class LocaleCatalog:
    report_locale: str
    catalog_version: str
    steam_language: str
    strings: Mapping[str, str]
    font_stack: tuple[str, ...]
    bold_font_stack: tuple[str, ...]
    forbidden_line_start: tuple[str, ...]
    forbidden_line_end: tuple[str, ...]
    internal_data_state_patterns: tuple[re.Pattern[str], ...]
    backstage_method_patterns: tuple[re.Pattern[str], ...]
    editorial_process_patterns: tuple[re.Pattern[str], ...]
    reader_copy_patterns: Mapping[str, tuple[re.Pattern[str], ...]]
    reader_claim_terms: Mapping[str, Any]

    def text(self, key: str, default: str = "") -> str:
        return str(self.strings.get(key, default))


def normalize_report_locale(value: str) -> str:
    """Normalize a supported locale or one of the documented input aliases."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("report locale must be en-US or zh-CN")
    candidate = value.strip()
    canonical = _ALIASES.get(candidate.casefold(), _ALIASES.get(candidate, candidate))
    if canonical not in SUPPORTED_REPORT_LOCALES:
        raise ValueError("unsupported report locale; expected en-US or zh-CN")
    return canonical


def _validate_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_run_config(value: Any) -> dict[str, str]:
    document = _validate_object(value, "run-config.json")
    if set(document) != {"format", "report_locale"}:
        raise ValueError("run-config.json contains unsupported fields")
    if document.get("format") != RUN_CONFIG_FORMAT:
        raise ValueError("run-config.json has an unsupported format")
    locale = normalize_report_locale(str(document.get("report_locale") or ""))
    if document.get("report_locale") != locale:
        raise ValueError("run-config.json must store a canonical report locale")
    return {"format": RUN_CONFIG_FORMAT, "report_locale": locale}


def load_run_config(run_dir: Path) -> dict[str, str]:
    path = Path(run_dir) / "run-config.json"
    if not path.is_file():
        raise FileNotFoundError("Missing run artifact: run-config.json")
    return _validate_run_config(read_json(path))


def ensure_run_config(run_dir: Path, report_locale: str) -> dict[str, str]:
    """Atomically create a run config, or verify an existing immutable one."""

    canonical = normalize_report_locale(report_locale)
    root = Path(run_dir)
    path = root / "run-config.json"
    if path.is_file():
        config = load_run_config(root)
        if config["report_locale"] != canonical:
            raise ValueError(
                "run-config.json already fixes a different report locale; create a new run"
            )
        return config
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError(
            "run directory contains artifacts but no run-config.json; create a new run"
        )
    write_json(path, {"format": RUN_CONFIG_FORMAT, "report_locale": canonical})
    return _validate_run_config(read_json(path))


def load_default_report_locale(workspace: Path) -> str | None:
    path = Path(workspace) / SETTINGS_FILE_NAME
    if not path.is_file():
        return None
    document = _validate_object(read_json(path), SETTINGS_FILE_NAME)
    if set(document) != {"format", "default_report_locale"}:
        raise ValueError(f"{SETTINGS_FILE_NAME} contains unsupported fields")
    if document.get("format") != SETTINGS_FORMAT:
        raise ValueError(f"{SETTINGS_FILE_NAME} has an unsupported format")
    return normalize_report_locale(str(document.get("default_report_locale") or ""))


def catalog_for(report_locale: str) -> LocaleCatalog:
    canonical = normalize_report_locale(report_locale)
    chinese = canonical == "zh-CN"
    return LocaleCatalog(
        report_locale=canonical,
        catalog_version=CATALOG_VERSION,
        steam_language="schinese" if chinese else "english",
        strings=_SYSTEM_STRINGS[canonical],
        font_stack=(
            ("Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", "CJK fallback")
            if chinese
            else ("Arial", "DejaVu Sans", "Latin sans-serif", "CJK fallback")
        ),
        bold_font_stack=(
            ("Microsoft YaHei Bold", "Noto Sans CJK SC Bold", "PingFang SC", "CJK fallback")
            if chinese
            else ("Arial Bold", "DejaVu Sans Bold", "Latin sans-serif", "CJK fallback")
        ),
        forbidden_line_start=("，", "。", "！", "？", "；", "：", "、", "》", "」", "』", "】", "…")
        if chinese else (),
        forbidden_line_end=("（", "《", "「", "『", "【") if chinese else (),
        internal_data_state_patterns=tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in _PATTERNS[canonical]["internal_data_state"]
        ),
        backstage_method_patterns=tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in _PATTERNS[canonical]["backstage_method"]
        ),
        editorial_process_patterns=tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in _PATTERNS[canonical]["editorial_process"]
        ),
        reader_copy_patterns={
            key: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
            for key, patterns in _PATTERNS[canonical].items()
        },
        reader_claim_terms=_READER_CLAIM_TERMS[canonical],
    )


def format_locale_date(value: Any, report_locale: str) -> str:
    """Format an ISO date for reader copy without changing its evidence value."""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        raise ValueError("date value is not ISO formatted") from None
    if normalize_report_locale(report_locale) == "zh-CN":
        return f"{parsed.year}年{parsed.month}月{parsed.day}日"
    return parsed.isoformat()


def format_locale_number(value: Any, precision: int, report_locale: str) -> str:
    del report_locale
    return f"{float(value):,.{max(0, int(precision))}f}"


def format_visible_measure(measure: Mapping[str, Any], report_locale: str) -> str:
    """Return the exact measure text owned by the renderer."""

    text = str(measure.get("display_value") or "").strip()
    kind = str((measure.get("format") or {}).get("kind") or "") if isinstance(measure.get("format"), Mapping) else ""
    locale = normalize_report_locale(report_locale)
    unit = {
        "hours": "小时" if locale == "zh-CN" else "h",
        "days": "天" if locale == "zh-CN" else "days",
        "percent": "",
        "integer": "",
        "number": "",
        "year": "",
        "date": "",
    }.get(kind, "")
    return text + (unit if unit and not text.endswith(unit) else "")


__all__ = [
    "LocaleCatalog",
    "CATALOG_VERSION",
    "RUN_CONFIG_FORMAT",
    "SETTINGS_FILE_NAME",
    "SUPPORTED_REPORT_LOCALES",
    "catalog_for",
    "ensure_run_config",
    "format_locale_date",
    "format_locale_number",
    "format_visible_measure",
    "load_default_report_locale",
    "load_run_config",
    "normalize_report_locale",
]
