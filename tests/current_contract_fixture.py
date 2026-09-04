from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from PIL import Image

from steam_visualogue.editorial_deck import compile_editorial_deck
from steam_visualogue.context_budget import sha256_path_hex
from steam_visualogue.fingerprint import compute_asset_manifest_fingerprint, compute_visual_brief_fingerprint
from steam_visualogue.io_utils import write_json
from steam_visualogue.publish_layout import compose_publish_layout


def _record(identifier: str, record_type: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": identifier, "type": record_type, "facts": facts}


def current_plan_and_evidence(locale: str = "en-US", *, page_count: int = 15) -> tuple[dict[str, Any], dict[str, Any]]:
    if not 12 <= page_count <= 18:
        raise ValueError("current fixture supports 12 to 18 pages")
    games = [
        _record(f"game:{index}", "game", [
            {"name": "name", "value": f"Game {index}"},
            {"name": "playtime_minutes", "value": minutes},
            {"name": "release_year", "value": 2010 + index},
            {"name": "achievement_completion", "value": completion},
        ])
        for index, minutes, completion in ((1, 600, 0.20), (2, 480, 0.40), (3, 120, 0.80), (4, 360, 0.55), (5, 180, 0.65))
    ]
    metrics = [
        _record("metric:library-count", "metric", [{"name": "count", "value": 42}]),
        _record("metric:time-a", "metric", [{"name": "playtime_minutes", "value": 600}]),
        _record("metric:time-b", "metric", [{"name": "playtime_minutes", "value": 120}]),
        _record("metric:completion", "metric", [{"name": "achievement_completion", "value": 0.52}]),
    ]
    evidence = {
        "run_id": "current-fixture",
        "evidence_fingerprint": "sha256:" + "0" * 64,
        "metrics": metrics,
        "games": games,
        "achievements": [],
        "patterns": [],
        "cards": [],
    }

    def page(number: int, move: str, claim_id: str, claim_kind: str, evidence_ids: list[str], develops: list[str], headline: str, content: dict[str, Any] | None = None, presentation_kind: str | None = None) -> dict[str, Any]:
        if presentation_kind is None:
            presentation_kind = "opening" if number == 1 else ("closing" if number == page_count else ("hero" if number == 2 else "abstract-portrait"))
        return {
            "page": number,
            "narrative_move": move,
            "reader_question": "What does this pattern say about the way the library is lived?",
            "claim": {"claim_id": claim_id, "kind": claim_kind, "text": headline, "evidence_ids": evidence_ids, "develops": develops},
            "reader_copy": {"headline": headline},
            "presentation": {"kind": presentation_kind, "content": content or {}},
        }

    pages = [
        page(1, "establish", "claim:opening", "tension", ["metric:library-count"], [], "The library starts wide, then finds a few deep channels."),
        page(2, "deepen", "claim:hero", "relation", ["game:1"], ["claim:thesis"], "One title becomes the first deep channel.", {"subject": {"game_id": "game:1", "asset_id": "game:1:portrait"}}, "hero"),
        page(3, "quantify", "claim:completion", "magnitude", ["metric:completion"], ["claim:hero"], "Completion is selective, not uniform."),
        page(4, "contrast", "claim:quant", "contrast", ["game:2", "game:3"], ["claim:completion"], "A fourfold gap changes the route.", {
            "shared_question": "Which title holds attention longer?",
            "shared_dimension": "time spent",
            "relationship_claim_id": "claim:quant",
            "items": [
                {"subject": {"game_id": "game:2", "asset_id": "game:2:portrait"}, "measure": {"evidence_id": "game:2", "fact": "playtime_minutes", "format": {"kind": "hours", "precision": 1}}, "evidence_ids": ["game:2"]},
                {"subject": {"game_id": "game:3", "asset_id": "game:3:portrait"}, "measure": {"evidence_id": "game:3", "fact": "playtime_minutes", "format": {"kind": "hours", "precision": 1}}, "evidence_ids": ["game:3"]},
            ],
        }, "quantitative-comparison"),
        page(5, "contrast", "claim:qual", "contrast", ["game:4", "game:5"], ["claim:quant"], "Two kinds of staying can share one shelf.", {
            "shared_question": "What makes a title worth returning to?",
            "shared_dimension": "reason to return",
            "relationship_claim_id": "claim:qual",
            "items": [
                {"subject": {"game_id": "game:4", "asset_id": "game:4:header"}, "statement": "It turns a difficult task into a familiar rhythm.", "evidence_ids": ["game:4"]},
                {"subject": {"game_id": "game:5", "asset_id": "game:5:header"}, "statement": "It leaves a bounded experience that feels complete.", "evidence_ids": ["game:5"]},
            ],
        }, "qualitative-comparison"),
    ]
    later = [
        ("connect", "claim:6", "metric:library-count", "Breadth supplies more than a list of categories."),
        ("deepen", "claim:7", "metric:time-a", "The longest return is concentrated, not constant."),
        ("complicate", "claim:8", "metric:time-b", "Shorter sessions still change the shape of the shelf."),
        ("connect", "claim:9", "metric:completion", "The middle ground is where the library keeps its tension."),
        ("deepen", "claim:10", "metric:library-count", "A broad shelf can still have a narrow center of gravity."),
        ("complicate", "claim:11", "metric:time-a", "Depth appears as a rhythm rather than a permanent state."),
        ("connect", "claim:12", "metric:time-b", "The smaller signal matters because it breaks the easy story."),
        ("deepen", "claim:13", "metric:completion", "Selective completion leaves room for unfinished curiosity."),
        ("connect", "claim:14", "metric:time-a", "The pattern is a balance between return and release."),
        ("deepen", "claim:15", "metric:time-b", "Selective return gives a broad shelf its center."),
        ("connect", "claim:16", "metric:library-count", "The center holds because the outer field stays open."),
        ("deepen", "claim:17", "metric:completion", "The second look reveals a pattern that the list alone cannot show."),
    ]
    quotes = {
        6: "Breadth opens more than one way into the shelf.",
        7: "The longest return gives the shelf its weight.",
        8: "Short visits still leave a visible trace.",
        9: "The middle ground keeps return from becoming a rule.",
        10: "A narrow pull can organize a much wider field.",
        11: "Depth accumulates through rhythm, not permanence.",
        12: "The smaller signal keeps the larger reading honest.",
        13: "Unfinished paths are part of the shelf's shape.",
        14: "Return and release can describe the same library.",
        15: "A chosen center gives the wider collection a rhythm.",
        16: "The outer field stays open around a selected center.",
        17: "A second look turns a collection into a lived pattern.",
    }
    for index in range(6, page_count):
        move, claim_id, evidence_id, headline = later[index - 6]
        pages.append(page(index, move, claim_id, "pattern" if index in {6, 9, 12, 16} else "consequence", [evidence_id], [pages[-1]["claim"]["claim_id"]], headline, {}, "abstract-portrait"))
    pages.append(page(page_count, "close", "claim:closing", "synthesis", ["metric:time-a", "metric:time-b"], ["claim:quant", pages[-1]["claim"]["claim_id"]], "Breadth is the invitation; depth is the choice.", {"quote": "The shelf opens through breadth, then holds through selective return."}, "closing"))
    plan = {
        "format": "steam-visualogue-deck-plan",
        "locale": "en-US",
        "title": "A Broad Shelf, Selective Depth",
        "mode": "thesis-led",
        "editorial_frame": {
            "guiding_question": "What does this pattern say about the way the library is lived?",
            "thesis": {"claim_id": "claim:thesis", "text": "A broad shelf becomes deep through selective returns.", "evidence_ids": ["metric:library-count", "metric:time-a"]},
        },
        "pages": pages,
    }
    if locale == "zh-CN":
        headlines = {
            1: "书架很宽，但真正留下来的路径并不多。",
            2: "一个标题先把浅尝变成了长期回访。",
            3: "完成率的差异，让投入的选择显出层次。",
            4: "四倍的时长差，把注意力的落点拉开了。",
            5: "两种回访方式，共同撑起一条收藏路径。",
            6: "宽度不只是分类清单，还会留下不同入口。",
            7: "最长的一次回访集中在少数选择上。",
            8: "短时段也会改变书架的轮廓。",
            9: "中间地带让投入和放下保持张力。",
            10: "宽书架仍可能被一条窄重心牵引。",
            11: "深度更像回访形成的节奏，而不是常态。",
            12: "较小的信号打破了过于简单的解释。",
            13: "选择性完成，为未完的好奇留下空间。",
            14: "这组痕迹在回访与释放之间保持平衡。",
            15: "选择性回访让宽书架拥有了重心。",
            16: "重心存在，是因为外围仍然保持开放。",
            17: "再次打开，让收藏变成被生活过的模式。",
            18: "当轮廓持续变化，深度也就显出了痕迹。",
        }
        plan["locale"] = locale
        plan["title"] = "宽度打开选择，深度留下路径"
        plan["editorial_frame"] = {
            "guiding_question": "怎样的书架既足够宽，又能留下深度？",
            "thesis": {"claim_id": "claim:thesis", "text": "宽书架通过有选择的回访变得更深。", "evidence_ids": ["metric:library-count", "metric:time-a"]},
        }
        zh_quotes = {
            3: "完成率让注意力选择停留的位置显了出来。",
            6: "宽度为进入书架打开了不止一条路。",
            7: "最长的一次回访给书架留下了重量。",
            8: "短暂的停留也会留下清晰痕迹。",
            9: "中间地带让回访不至于变成规则。",
            10: "窄窄的牵引也能整理更宽的场域。",
            11: "深度靠节奏累积，而不是靠持续占据。",
            12: "较小的信号让更大的判断保持诚实。",
            13: "未完成的路径也是书架形状的一部分。",
            14: "回访与释放可以共同描述同一个书架。",
            15: "有选择的回访让宽书架拥有重心。",
            16: "开放的外围让重心有了呼吸。",
            17: "再次打开，让收藏变成被生活过的模式。",
            18: "轮廓持续变化，深度也留下了痕迹。",
        }
        for page_row in plan["pages"]:
            number = int(page_row["page"])
            headline = headlines[number]
            page_row["reader_question"] = "这些痕迹说明了怎样的游玩方式？"
            page_row["claim"]["text"] = headline
            page_row["reader_copy"]["headline"] = headline
            content = page_row["presentation"].get("content", {})
            if isinstance(content, dict) and "quote" in content:
                content["quote"] = zh_quotes.get(number, headline)
        plan["pages"][3]["presentation"]["content"].update({"shared_question": "哪一款作品更能留住注意力？", "shared_dimension": "投入时长"})
        plan["pages"][4]["presentation"]["content"].update({"shared_question": "什么让一款作品值得再次打开？", "shared_dimension": "回访理由"})
        plan["pages"][4]["presentation"]["content"]["items"] = [
            {"subject": {"game_id": "game:4", "asset_id": "game:4:header"}, "statement": "它把困难任务变成熟悉的节奏。", "evidence_ids": ["game:4"]},
            {"subject": {"game_id": "game:5", "asset_id": "game:5:header"}, "statement": "它把有限体验收束成完整感。", "evidence_ids": ["game:5"]},
        ]
    return plan, evidence


def atlas_plan_and_evidence(locale: str = "en-US") -> tuple[dict[str, Any], dict[str, Any]]:
    plan, evidence = current_plan_and_evidence(locale)
    group_id = "pattern:same-series-group:fixture"
    evidence["patterns"].append({
        "id": group_id,
        "type": "same_series_group",
        "facts": [{"name": "game_rows", "value": [{"game_id": "game:1", "appid": "1"}, {"game_id": "game:2", "appid": "2"}, {"game_id": "game:3", "appid": "3"}]}],
        "related_ids": ["game:1", "game:2", "game:3"],
    })
    page = plan["pages"][1]
    page["claim"] = {
        "claim_id": "claim:hero",
        "kind": "pattern",
        "text": "Three returns share one underlying rhythm.",
        "evidence_ids": [group_id, "game:1", "game:2", "game:3"],
        "develops": ["claim:thesis"],
    }
    page["reader_copy"] = {"headline": "Three returns share one underlying rhythm."}
    page["presentation"] = {
        "kind": "series-atlas",
        "content": {
            "group_evidence_id": group_id,
            "items": [
                {"subject": {"game_id": "game:1", "asset_id": "game:1:portrait"}, "measure": {"evidence_id": "game:1", "fact": "playtime_minutes", "format": {"kind": "hours", "precision": 1}}, "statement": "A long return sets the scale.", "evidence_ids": [group_id, "game:1"]},
                {"subject": {"game_id": "game:2", "asset_id": "game:2:portrait"}, "measure": {"evidence_id": "game:2", "fact": "playtime_minutes", "format": {"kind": "hours", "precision": 1}}, "statement": "A steady return holds the middle.", "evidence_ids": [group_id, "game:2"]},
                {"subject": {"game_id": "game:3", "asset_id": "game:3:portrait"}, "measure": {"evidence_id": "game:3", "fact": "playtime_minutes", "format": {"kind": "hours", "precision": 1}}, "statement": "A brief return still belongs to the rhythm.", "evidence_ids": [group_id, "game:3"]},
            ],
        },
    }
    plan["pages"][3]["presentation"] = {
        "kind": "abstract-portrait",
        "content": {},
    }
    plan["pages"][3]["claim"] = {
        "claim_id": "claim:quant",
        "kind": "consequence",
        "text": "A single focus keeps the rhythm clear.",
        "evidence_ids": ["metric:time-a"],
        "develops": ["claim:thesis"],
    }
    plan["pages"][3]["reader_copy"] = {"headline": "A single focus keeps the rhythm clear."}
    return plan, evidence


def _visual_brief(compiled: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for asset_id, record in sorted(manifest.get("assets", {}).items()):
        candidates.append({
            "asset_id": asset_id,
            "path": f"assets/{record['path']}",
            "width": record["width"],
            "height": record["height"],
            "aspect_ratio": round(record["width"] / record["height"], 6),
            "target_roles": ["Hero Game", "Hook Cover"],
        })
    payload = {
        "evidence_fingerprint": "sha256:" + "0" * 64,
        "visual_fingerprint": "sha256:" + "1" * 64,
        "compiled_deck_fingerprint": compiled["compiled_deck_fingerprint"],
        "asset_manifest_fingerprint": compute_asset_manifest_fingerprint(manifest),
        "library_palette": {},
        "comparison_palette": {},
        "sampling": {
            "strategy": "top32-stratified-tail32",
            "eligible_games": 5,
            "selected_games": 5,
            "successful_games": 5,
            "sample_fraction": 1.0,
            "selected_playtime_fraction": 1.0,
            "representation_coverage": {"titles": 1.0, "lived_weight": 1.0},
        },
        "confidence": "high",
        "failure_count": 0,
        "candidate_assets": candidates,
        "accepted_inspections": [],
        "deck_policy": {"max_pages_per_game": 2, "max_pages_per_asset": 1, "min_page_gap_for_repeated_game": 2},
        "role_contracts": {"hero": {"encoding_kind": "single-subject-anchor", "content": "Give one claim a visual anchor."}},
    }
    payload["visual_brief_fingerprint"] = compute_visual_brief_fingerprint(payload)
    return payload


def build_metadata_fixture(root: Path, *, working_size: tuple[int, int] = (1080, 1440), final_size: tuple[int, int] = (1080, 1440), locale: str = "en-US", page_count: int = 15) -> dict[str, Any]:
    plan, evidence = current_plan_and_evidence(locale, page_count=page_count)
    assets_dir = root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    records: dict[str, Any] = {}
    for index, game_id in enumerate(f"game:{value}" for value in range(1, 6)):
        portrait_filename = f"{index}.png"
        records[f"{game_id}:portrait"] = {"status": "ready", "path": portrait_filename, "width": 600, "height": 900, "source": "steam"}
        header_filename = f"{index}-header.png"
        records[f"{game_id}:header"] = {"status": "ready", "path": header_filename, "width": 1200, "height": 600, "source": "steam"}
    manifest = {"assets": records}
    write_json(assets_dir / "manifest.json", manifest)
    compiled = compile_editorial_deck(plan, {"findings": []}, evidence, None)
    direction = {"working_size": list(working_size), "final_size": list(final_size), "composition": "adaptive-editorial", "density": "focused-with-anchored-lower-field", "rhythm": "varied", "closure": "quiet-synthesis", "palette": {"ground": "#111820", "ink": "#F2EEE6", "primary": "#D36A4A", "secondary": "#4D7EA8", "accent": "#E7C35A", "muted": "#77818A"}}
    visual_brief = _visual_brief(compiled, manifest)
    layout = compose_publish_layout(compiled, direction, manifest, visual_brief)
    return {"plan": plan, "evidence": evidence, "manifest": manifest, "assets_dir": assets_dir, "compiled": compiled, "layout": layout, "art_direction": direction, "visual_brief": visual_brief}


def build_render_fixture(root: Path, *, working_size: tuple[int, int] = (1080, 1440), final_size: tuple[int, int] = (1080, 1440), locale: str = "en-US", page_count: int = 15) -> dict[str, Any]:
    fixture = build_metadata_fixture(root, working_size=working_size, final_size=final_size, locale=locale, page_count=page_count)
    assets_dir = fixture["assets_dir"]
    records = fixture["manifest"]["assets"]
    for index, game_id in enumerate(f"game:{value}" for value in range(1, 6)):
        portrait_path = assets_dir / f"{index}.png"
        Image.new("RGB", (600, 900), (36 + index * 22, 58 + index * 17, 82 + index * 13)).save(portrait_path, format="PNG")
        records[f"{game_id}:portrait"]["sha256"] = sha256_path_hex(portrait_path)
        header_path = assets_dir / f"{index}-header.png"
        Image.new("RGB", (1200, 600), (48 + index * 22, 70 + index * 17, 94 + index * 13)).save(header_path, format="PNG")
        records[f"{game_id}:header"]["sha256"] = sha256_path_hex(header_path)
    write_json(assets_dir / "manifest.json", fixture["manifest"])
    fixture["visual_brief"] = _visual_brief(fixture["compiled"], fixture["manifest"])
    fixture["layout"] = compose_publish_layout(fixture["compiled"], fixture["art_direction"], fixture["manifest"], fixture["visual_brief"])
    return fixture


def build_current_fixture(root: Path, *, working_size: tuple[int, int] = (1080, 1440), final_size: tuple[int, int] = (1080, 1440), locale: str = "en-US", page_count: int = 15) -> dict[str, Any]:
    return build_render_fixture(root, working_size=working_size, final_size=final_size, locale=locale, page_count=page_count)


def constellation_plan(locale: str = "en-US") -> dict[str, Any]:
    plan, _ = current_plan_and_evidence(locale)
    result = copy.deepcopy(plan)
    result["mode"] = "constellation-led"
    result["editorial_frame"] = {
        "organizing_question": "怎样的深度可以共存在同一个书架上？" if locale == "zh-CN" else "What kinds of depth coexist on one shelf?",
        "clusters": [
            {"cluster_id": "cluster:time", "title": "时间找到落点" if locale == "zh-CN" else "Time finds a center", "question": "注意力在哪里聚拢？" if locale == "zh-CN" else "Where does attention gather?"},
            {"cluster_id": "cluster:return", "title": "回访改变轮廓" if locale == "zh-CN" else "Returns change the shape", "question": "什么让一次回访变得重要？" if locale == "zh-CN" else "What makes a return matter?"},
        ],
    }
    for page in result["pages"][1:7]:
        page["claim"]["cluster_id"] = "cluster:time"
    for page in result["pages"][7:-1]:
        page["claim"]["cluster_id"] = "cluster:return"
    result["pages"][1]["claim"]["develops"] = []
    result["pages"][-1]["claim"]["develops"] = ["claim:7", "claim:14"]
    return result
