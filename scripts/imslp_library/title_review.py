"""Validated work-level Chinese title reviews.

The production category builders historically keyed translations by the
English title.  IMSLP work IDs are the stable identity needed for a complete
review because the same English title can occur for different works and one
work can appear in several instrumentation categories.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping


VALID_STATUSES = {"accepted", "corrected"}
VALID_BASES = {
    "common_name",
    "music_terminology",
    "semantic_correction",
    "format_consistency",
    "corpus_review",
}

MISTRANSLATION_RULES = (
    ("pieces_as_objects", r"\b(?:pieces?|pi[eè]ces?|piezas?|pezzi|st[uü]cke|morceaux)\b", r"件"),
    ("orders_as_dishes", r"\b[oó]rdenes\b", r"道菜"),
    ("prelude_as_stage_opening", r"\b(?:preludes?|preludios?|pr[eé]ludes?)\b", r"序幕"),
    ("air_as_substance", r"\bairs?\b", r"空气"),
    ("saltarello_as_wire", r"\bsaltarell[oi]?\b", r"跳线"),
    ("suite_as_room", r"\bsuites?\b", r"套房"),
    ("etude_as_research", r"\b(?:etudes?|[eé]tudes?|estudios?|studies)\b", r"研究|学习"),
    ("contredanse_as_contradiction", r"\bcontredanses?\b", r"矛盾"),
    ("polonaise_as_person", r"\bpolonaises?\b", r"波兰人"),
    ("mass_as_crowd", r"\b(?:mass(?:es)?|missa|messe)\b", r"群众|质量"),
    ("minuet_missing_qu", r"\b(?:minuets?|menuets?|minuett[oi]?)\b", r"小步舞(?!曲)"),
    ("fugue_as_escape", r"\b(?:fugue|fuga|fuge|fugas)\b", r"逃走|航班|逃脱"),
    ("waltz_as_wrong", r"\b(?:waltz(?:es)?|valse(?:s)?|walzer)\b", r"错误|虚假|瓦尔泽"),
    ("variations_as_changes", r"\b(?:variations?|variaciones|variazioni|variationen)\b", r"变化|变体"),
    ("ricercar_as_rice", r"\b(?:ricercar(?:e|i)?|ricercare)\b", r"米饭|水稻|里瑟卡|莱斯卡"),
    ("courante_as_current", r"\b(?:courante|corrente)\b", r"当前|现状|职务|电流"),
    ("polacca_as_polish", r"\b(?:polacca|polaca)\b", r"抛光|波兰语"),
    ("bolero_as_jacket", r"\bbol[ée]ros?\b", r"短上衣"),
    ("contradanza_as_contrast", r"\bcontradanza\b", r"对比度"),
    ("sonatina_as_sonata", r"\bsonatina\b", r"(?<!小)奏鸣曲"),
    (
        "duet_as_large_ensemble",
        r"\b(?:duos?|duets?|duettinos?|duetti)\b",
        r"[三六]\s*重奏|二人组|双人组|双人舞|对奏|双重奏",
    ),
    ("minuet_as_menu", r"\b(?:minuets?|menuets?|minuett[oi]?)\b", r"菜单"),
    ("gigue_as_jitter", r"\bgigue\b", r"抖动"),
    ("bourree_as_brewed", r"\bbourr[ée]e?\b", r"酿"),
    ("courses_as_steps", r"\b(?:[oó]rdenes|ordenes)\b", r"阶"),
    (
        "generic_pieces_missing_noun",
        r"^\s*\d+\s+(?:pieces?|pi[eè]ces?|morceaux|piezas?|pezzi|st[uü]cke)\s*(?:,|$)",
        r"《\s*\d+\s*首\s*[，,]",
    ),
    (
        "pieces_as_chunks",
        r"\b(?:pieces?|pi[eè]ces?|morceaux|piezas?|pezzi|st[uü]cke)\b",
        r"小块|小片段",
    ),
    ("progressive_as_avant_garde", r"\bprogres(?:s)?iv\w*\b", r"前卫"),
    ("society_pieces_as_association", r"pi[eè]ces? de soci[eé]t[eé]", r"协会|社会作品"),
    (
        "guitar_piece_missing_form",
        r"\bguitar piece no\.?\s*\d+\b",
        r"吉他第(?:\s*\d+|[一二三四五六七八九十]+)首",
    ),
    ("andante_as_walking", r"\bandante\b", r"行走|步行|走路"),
    ("allegro_as_joy", r"\ballegro\b", r"喜乐|欢乐"),
    ("andantino_not_diminutive", r"\bandantino\b", r"(?<!小)行板|安丹蒂诺"),
    ("allegretto_not_diminutive", r"\ballegretto\b", r"(?<!小)快板|阿勒格雷托"),
    ("larghetto_mistranslated", r"\blarghetto\b", r"(?<!小)广板|慢板|拉盖托|长音"),
    (
        "fantasia_missing_form",
        r"\b(?:fantasias?|fantasies|fantaisies?|fantasien?)\b",
        r"幻想(?=》|[，,、与和第0-9一二三四五六七八九十])",
    ),
    ("ballet_as_troupe", r"\b(?:ballet|baletto|ballett)\b", r"芭蕾舞团"),
    (
        "volta_literal_translation",
        r"^(?:la\s+)?volt[ae]s?\b|\bvoltes?\b",
        r"电压|伏特|时间|避难所|泰晤士报|第[一二三四0-9][^》]*轮",
    ),
    (
        "branle_literal_translation",
        r"\b(?:branle|bransle)\b",
        r"打手枪|打飞机|同性恋|运动|布兰(?:斯)?勒",
    ),
    ("canarie_as_canary", r"\bcanarie\b", r"金丝雀"),
    ("follia_as_madness", r"\bfollia\b", r"疯狂"),
    (
        "ground_literal_translation",
        r"^(?:(?:a|italian)\s+)?ground\b|greensleeves\s+to\s+a\s+ground",
        r"地面|落地",
    ),
    (
        "magnificat_wrong_name",
        r"\bmagnificat\b",
        r"圣母颂|放大曲|颂赞曲|赋格颂赞|赋格颂歌|音颂扬|音颂赞",
    ),
    ("aubade_untranslated", r"\baubade\b", r"奥巴德"),
    ("almain_literal_translation", r"\balmain\b", r"德国|国际金融公司"),
    ("tiento_as_tent", r"\btiento\b", r"帐篷|Tenta"),
    ("danceries_as_drama", r"\bdanceries\b", r"舞剧"),
    ("in_nomine_as_nomination", r"\bin\s+nomines?\b", r"提名|以名义"),
)


class TitleReviewError(ValueError):
    """Raised when a reviewed-title catalog violates its identity contract."""


def title_quality_flags(title_en: str, title_zh: str) -> list[str]:
    """Return high-confidence music-domain mistranslation flags."""

    flags = []
    for name, source_pattern, target_pattern in MISTRANSLATION_RULES:
        if re.search(source_pattern, title_en, re.IGNORECASE) and re.search(target_pattern, title_zh):
            flags.append(name)
    return flags


_CHINESE_CARDINALS = {
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "七",
    8: "八",
    9: "九",
    10: "十",
    11: "十一",
    12: "十二",
    13: "十三",
    14: "十四",
    15: "十五",
    16: "十六",
    17: "十七",
    18: "十八",
    19: "十九",
    20: "二十",
}


def title_identity_flags(title_en: str, title_zh: str) -> list[str]:
    """Check that translation edits retain keys and work/catalog numbers."""

    flags: list[str] = []
    key_match = re.search(
        r"\bin\s+([A-G])(?:-?(flat|sharp)|([♭#]))?\s+(major|minor)\b",
        title_en,
        re.IGNORECASE,
    )
    if key_match:
        note, accidental_word, accidental_symbol, mode = key_match.groups()
        accidental = accidental_word or accidental_symbol or ""
        prefix = "降" if accidental.casefold() in {"flat", "♭"} else "升" if accidental.casefold() in {"sharp", "#"} else ""
        expected = f"{prefix}{note.upper()}{'大调' if mode.casefold() == 'major' else '小调'}"
        if expected not in title_zh:
            flags.append("key_mismatch")

    for opus in re.findall(r"\bOp\.?\s*(\d+[a-z]?)", title_en, re.IGNORECASE):
        if opus.casefold() not in title_zh.casefold():
            flags.append("opus_number_missing")
            break

    for raw_number in re.findall(r"\bNo\.?\s*(\d+)", title_en, re.IGNORECASE):
        number = int(raw_number)
        chinese = _CHINESE_CARDINALS.get(number)
        candidates = [
            rf"第\s*{re.escape(raw_number)}",
            rf"(?:第\s*)?{re.escape(raw_number)}\s*号",
            rf"之\s*{re.escape(raw_number)}(?:\D|$)",
        ]
        if chinese:
            candidates.append(rf"第\s*{chinese}")
            candidates.append(rf"(?:第\s*)?{chinese}\s*号")
        candidates.append(rf"\bNo\.?\s*{re.escape(raw_number)}\b")
        if not any(re.search(candidate, title_zh, re.IGNORECASE) for candidate in candidates):
            flags.append("work_number_missing")
            break

    catalog_pattern = re.compile(
        r"(?<![A-Za-z])"
        r"(BWV|BuxWV|TWV|Krebs-WV|WeissSW|Drexel|Cary|WoO|MS|WSW|"
        r"VdGS|FVB|IFC|ZD|RV|HWV|Sz|LV|Ch|K\.Anh\.C|[KDSZPFRH]|MT)"
        r"\.?\s*"
        r"(\d+(?:[.:/][A-Za-z0-9]+)*(?:[a-z](?:/[a-z])?)?(?:[-–]\d+)?)"
    )

    def normalize_catalog(value: str) -> str:
        return re.sub(r"[\s.:：,，\-‐-―]", "", value).casefold()

    normalized_zh = normalize_catalog(title_zh)
    for prefix, number in catalog_pattern.findall(title_en):
        expected = normalize_catalog(prefix + number)
        if expected not in normalized_zh:
            flags.append("catalog_number_missing")
            break
    return flags


def _nonempty_string(row: Mapping[str, object], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TitleReviewError(f"{name} must be a non-empty string")
    return value.strip()


def load_reviewed_titles(path: Path) -> dict[str, dict[str, object]]:
    """Load and validate a canonical work-ID keyed title review catalog."""

    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TitleReviewError("review catalog must use schema_version 1")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise TitleReviewError("entries must be a list")
    reviewed: dict[str, dict[str, object]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise TitleReviewError("review entry must be an object")
        work_id = _nonempty_string(raw, "work_id")
        if work_id in reviewed:
            raise TitleReviewError(f"duplicate work_id: {work_id}")
        _nonempty_string(raw, "title_en")
        title_zh = _nonempty_string(raw, "title_zh")
        if not (title_zh.startswith("《") and title_zh.endswith("》")) or title_zh == "《》":
            raise TitleReviewError(f"invalid title_zh for work_id {work_id}")
        status = _nonempty_string(raw, "status")
        if status not in VALID_STATUSES:
            raise TitleReviewError(f"invalid status for work_id {work_id}: {status}")
        basis = _nonempty_string(raw, "basis")
        if basis not in VALID_BASES:
            raise TitleReviewError(f"invalid basis for work_id {work_id}: {basis}")
        _nonempty_string(raw, "reason")
        _nonempty_string(raw, "reviewer")
        refs = raw.get("source_refs")
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise TitleReviewError(f"source_refs must be a string list for work_id {work_id}")
        reviewed[work_id] = raw
    return reviewed


def resolve_reviewed_title(
    work: Mapping[str, object],
    existing_title_zh: str,
    reviewed: Mapping[str, Mapping[str, object]],
) -> str:
    """Return the reviewed title for a work, falling back to its current title."""

    work_id = str(work.get("work_id", ""))
    row = reviewed.get(work_id)
    if row is None:
        return existing_title_zh
    title_en = str(work.get("title_en", ""))
    if row.get("title_en") != title_en:
        raise TitleReviewError(
            f"title mismatch for work_id {work_id}: {row.get('title_en')!r} != {title_en!r}"
        )
    return str(row["title_zh"])


def _validate_display_title(title_zh: object, *, work_id: str) -> str:
    if not isinstance(title_zh, str):
        raise TitleReviewError(f"invalid title_zh for work_id {work_id}")
    title_zh = title_zh.strip()
    if not (title_zh.startswith("《") and title_zh.endswith("》")) or title_zh == "《》":
        raise TitleReviewError(f"invalid title_zh for work_id {work_id}")
    return title_zh


def build_canonical_review(
    corpus: list[dict[str, object]],
    reviews: list[dict[str, object]],
    *,
    reviewed_at: str,
) -> dict[str, object]:
    """Merge disjoint round-robin review shards into one complete catalog."""

    if not corpus:
        raise TitleReviewError("review corpus must not be empty")
    if not reviews:
        raise TitleReviewError("review shards must not be empty")
    shard_count = len(reviews)
    review_by_shard: dict[int, dict[str, object]] = {}
    for review in reviews:
        if not isinstance(review, dict) or review.get("schema_version") != 1:
            raise TitleReviewError("review shard must use schema_version 1")
        shard = review.get("shard")
        if not isinstance(shard, int) or not 1 <= shard <= shard_count:
            raise TitleReviewError(f"invalid review shard: {shard!r}")
        if shard in review_by_shard:
            raise TitleReviewError(f"duplicate review shard: {shard}")
        review_by_shard[shard] = review
    if set(review_by_shard) != set(range(1, shard_count + 1)):
        raise TitleReviewError("review shards must be contiguous")

    corpus_by_id: dict[str, tuple[int, dict[str, object]]] = {}
    expected_audited = {shard: 0 for shard in review_by_shard}
    for index, row in enumerate(corpus):
        if not isinstance(row, dict):
            raise TitleReviewError("corpus entry must be an object")
        work_id = _nonempty_string(row, "work_id")
        if work_id in corpus_by_id:
            raise TitleReviewError(f"duplicate corpus work_id: {work_id}")
        _nonempty_string(row, "title_en")
        _nonempty_string(row, "title_zh")
        shard = index % shard_count + 1
        expected_audited[shard] += 1
        corpus_by_id[work_id] = (shard, row)

    corrections: dict[str, tuple[int, dict[str, object]]] = {}
    for shard, review in review_by_shard.items():
        if review.get("audited_count") != expected_audited[shard]:
            raise TitleReviewError(
                f"audited_count mismatch for shard {shard}: "
                f"{review.get('audited_count')!r} != {expected_audited[shard]}"
            )
        entries = review.get("entries")
        if not isinstance(entries, list):
            raise TitleReviewError(f"entries must be a list for shard {shard}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise TitleReviewError(f"review entry must be an object for shard {shard}")
            work_id = _nonempty_string(entry, "work_id")
            if work_id in corrections:
                raise TitleReviewError(f"duplicate correction work_id: {work_id}")
            if work_id not in corpus_by_id:
                raise TitleReviewError(f"unknown correction work_id: {work_id}")
            expected_shard, source = corpus_by_id[work_id]
            if expected_shard != shard:
                raise TitleReviewError(
                    f"work_id {work_id} belongs to shard {expected_shard}, not {shard}"
                )
            if entry.get("title_en") != source.get("title_en"):
                raise TitleReviewError(f"title_en mismatch for work_id {work_id}")
            if entry.get("old_title_zh") != source.get("title_zh"):
                raise TitleReviewError(f"old_title_zh mismatch for work_id {work_id}")
            _validate_display_title(entry.get("new_title_zh"), work_id=work_id)
            basis = _nonempty_string(entry, "basis")
            if basis not in VALID_BASES - {"corpus_review"}:
                raise TitleReviewError(f"invalid correction basis for work_id {work_id}: {basis}")
            _nonempty_string(entry, "reason")
            refs = entry.get("source_refs")
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise TitleReviewError(f"source_refs must be a string list for work_id {work_id}")
            corrections[work_id] = (shard, entry)

    canonical = []
    for work_id, (shard, source) in corpus_by_id.items():
        correction = corrections.get(work_id)
        if correction:
            entry = correction[1]
            final_title = _validate_display_title(entry["new_title_zh"], work_id=work_id)
            canonical.append(
                {
                    "work_id": work_id,
                    "title_en": source["title_en"],
                    "title_zh": final_title,
                    "status": "corrected",
                    "basis": entry["basis"],
                    "reason": entry["reason"],
                    "reviewer": f"title_review_{shard}",
                    "source_refs": entry["source_refs"],
                }
            )
        else:
            final_title = _validate_display_title(source["title_zh"], work_id=work_id)
            canonical.append(
                {
                    "work_id": work_id,
                    "title_en": source["title_en"],
                    "title_zh": final_title,
                    "status": "accepted",
                    "basis": "corpus_review",
                    "reason": "Passed the assigned corpus terminology, identity and format review; existing reference translation retained.",
                    "reviewer": f"title_review_{shard}",
                    "source_refs": [],
                }
            )
    canonical.sort(key=lambda row: (int(str(row["work_id"])), str(row["work_id"])))
    return {
        "schema_version": 1,
        "reviewed_at": reviewed_at,
        "policy": (
            "Use established Mainland Chinese classical-music names when available; "
            "otherwise retain a faithful reference translation with standard musical terminology."
        ),
        "summary": {
            "work_count": len(canonical),
            "corrected_count": len(corrections),
            "accepted_count": len(canonical) - len(corrections),
            "shard_count": shard_count,
        },
        "entries": canonical,
    }


def apply_review_supplements(
    canonical: Mapping[str, object],
    supplements: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """Apply ordered, identity-checked correction supplements.

    Every supplement records the title it reviewed in ``current_title_zh``.
    Requiring an exact match prevents a later file from silently overwriting a
    decision made against a different catalog revision.
    """

    if canonical.get("schema_version") != 1:
        raise TitleReviewError("canonical review must use schema_version 1")
    payload = deepcopy(dict(canonical))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise TitleReviewError("canonical entries must be a list")
    rows: dict[str, dict[str, object]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            raise TitleReviewError("canonical entry must be an object")
        work_id = _nonempty_string(raw, "work_id")
        if work_id in rows:
            raise TitleReviewError(f"duplicate canonical work_id: {work_id}")
        rows[work_id] = raw

    supplement_counts: list[int] = []
    for supplement in supplements:
        if supplement.get("schema_version") != 1:
            raise TitleReviewError("review supplement must use schema_version 1")
        reviewer = _nonempty_string(supplement, "reviewer")
        changes = supplement.get("entries")
        if not isinstance(changes, list):
            raise TitleReviewError("supplement entries must be a list")
        seen: set[str] = set()
        for change in changes:
            if not isinstance(change, dict):
                raise TitleReviewError("supplement entry must be an object")
            work_id = _nonempty_string(change, "work_id")
            if work_id in seen:
                raise TitleReviewError(f"duplicate supplement work_id: {work_id}")
            seen.add(work_id)
            row = rows.get(work_id)
            if row is None:
                raise TitleReviewError(f"unknown supplement work_id: {work_id}")
            if change.get("title_en") != row.get("title_en"):
                raise TitleReviewError(f"title_en mismatch for work_id {work_id}")
            current = _nonempty_string(change, "current_title_zh")
            if current != row.get("title_zh"):
                raise TitleReviewError(f"current_title_zh mismatch for work_id {work_id}")
            new_title = _validate_display_title(change.get("new_title_zh"), work_id=work_id)
            basis = _nonempty_string(change, "basis")
            if basis not in VALID_BASES - {"corpus_review"}:
                raise TitleReviewError(f"invalid supplement basis for work_id {work_id}: {basis}")
            reason = _nonempty_string(change, "reason")
            refs = change.get("source_refs")
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise TitleReviewError(f"source_refs must be a string list for work_id {work_id}")
            row.update(
                title_zh=new_title,
                status="corrected",
                basis=basis,
                reason=reason,
                reviewer=reviewer,
                source_refs=refs,
            )
        supplement_counts.append(len(changes))

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        payload["summary"] = summary
    corrected_count = sum(row.get("status") == "corrected" for row in rows.values())
    summary.update(
        work_count=len(rows),
        corrected_count=corrected_count,
        accepted_count=len(rows) - corrected_count,
        supplement_counts=supplement_counts,
    )
    return payload


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _configured_category_names(root: Path) -> list[str]:
    names: list[str] = []
    for filename in ("categories.json", "mixed_categories.json"):
        path = root / "config" / filename
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("categories", []):
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str) and name not in names:
                names.append(name)
    return names


def apply_reviewed_titles(
    root: Path,
    reviewed: Mapping[str, Mapping[str, object]],
    *,
    category_names: Iterable[str] | None = None,
) -> dict[str, int]:
    """Apply reviewed titles to generated category catalogs and seed maps."""

    names = list(category_names) if category_names is not None else _configured_category_names(root)
    category_count = catalog_records = changed_records = 0
    for name in names:
        metadata = root / name / "metadata"
        catalog_path = metadata / "catalog.json"
        if not catalog_path.is_file():
            continue
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        if not isinstance(catalog, list):
            raise TitleReviewError(f"catalog must be a list: {catalog_path}")
        title_map_path = metadata / "translations_zh.json"
        title_map = (
            json.loads(title_map_path.read_text(encoding="utf-8"))
            if title_map_path.is_file()
            else {}
        )
        if not isinstance(title_map, dict):
            raise TitleReviewError(f"title map must be an object: {title_map_path}")
        for work in catalog:
            catalog_records += 1
            old = str(work.get("title_zh", ""))
            new = resolve_reviewed_title(work, old, reviewed)
            if old != new:
                changed_records += 1
            work["title_zh"] = new
            title_map[str(work["title_en"])] = new
        write_json_atomic(catalog_path, catalog)
        write_json_atomic(title_map_path, title_map)
        category_count += 1
    return {
        "categories": category_count,
        "catalog_records": catalog_records,
        "changed_records": changed_records,
    }


def audit_reviewed_titles(
    root: Path,
    reviewed: Mapping[str, Mapping[str, object]],
    *,
    category_names: Iterable[str] | None = None,
) -> dict[str, object]:
    """Audit review coverage, identity, terminology and applied catalogs."""

    names = list(category_names) if category_names is not None else _configured_category_names(root)
    catalog_by_id: dict[str, list[dict[str, object]]] = defaultdict(list)
    category_records = 0
    for name in names:
        path = root / name / "metadata/catalog.json"
        if not path.is_file():
            continue
        catalog = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(catalog, list):
            raise TitleReviewError(f"catalog must be a list: {path}")
        for work in catalog:
            category_records += 1
            catalog_by_id[str(work.get("work_id", ""))].append(work)

    catalog_ids = set(catalog_by_id)
    reviewed_ids = set(reviewed)
    title_identity_mismatches = 0
    catalog_title_mismatches = 0
    inconsistent_work_ids = 0
    quality_counts: Counter[str] = Counter()
    identity_counts: Counter[str] = Counter()
    format_counts: Counter[str] = Counter()
    exact_title_translations: dict[str, set[str]] = defaultdict(set)
    for work_id, works in catalog_by_id.items():
        row = reviewed.get(work_id)
        if row is None:
            continue
        expected_en = str(row.get("title_en", ""))
        expected_zh = str(row.get("title_zh", ""))
        if any(str(work.get("title_en", "")) != expected_en for work in works):
            title_identity_mismatches += 1
        if any(str(work.get("title_zh", "")) != expected_zh for work in works):
            catalog_title_mismatches += 1
        if len({str(work.get("title_zh", "")) for work in works}) > 1:
            inconsistent_work_ids += 1
        quality_counts.update(title_quality_flags(expected_en, expected_zh))
        identity_counts.update(title_identity_flags(expected_en, expected_zh))
        if expected_zh.startswith("《《") or "》》" in expected_zh or re.search(r"《[^》]*《", expected_zh):
            format_counts["nested_book_titles"] += 1
        if ", " in expected_zh:
            format_counts["ascii_comma"] += 1
        if re.search(r"\bfor\b", expected_zh, re.IGNORECASE):
            format_counts["raw_for"] += 1
        exact_title_translations[expected_en].add(expected_zh)

    return {
        "categories": len(names),
        "category_records": category_records,
        "unique_work_ids": len(catalog_ids),
        "reviewed_work_ids": len(reviewed_ids),
        "missing_review_ids": sorted(catalog_ids - reviewed_ids, key=lambda value: (int(value), value)),
        "extra_review_ids": sorted(reviewed_ids - catalog_ids, key=lambda value: (int(value), value)),
        "title_identity_mismatches": title_identity_mismatches,
        "catalog_title_mismatches": catalog_title_mismatches,
        "cross_category_inconsistent_work_ids": inconsistent_work_ids,
        "quality_flag_counts": dict(sorted(quality_counts.items())),
        "identity_flag_counts": dict(sorted(identity_counts.items())),
        "format_flag_counts": dict(sorted(format_counts.items())),
        "inconsistent_exact_english_titles": sum(
            len(values) > 1 for values in exact_title_translations.values()
        ),
    }
