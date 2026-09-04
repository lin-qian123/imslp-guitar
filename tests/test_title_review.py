import json
import runpy
from pathlib import Path

import pytest

from imslp_library.title_review import (
    TitleReviewError,
    apply_reviewed_titles,
    apply_review_supplements,
    audit_reviewed_titles,
    build_canonical_review,
    load_reviewed_titles,
    resolve_reviewed_title,
    title_identity_flags,
    title_quality_flags,
)


ROOT = Path(__file__).resolve().parents[1]


def write_review(path, entries):
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reviewed_at": "2026-09-04",
                "entries": entries,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_reviewed_work_id_overrides_machine_translation(tmp_path):
    review_path = tmp_path / "review.json"
    write_review(
        review_path,
        [
            {
                "work_id": "1157993",
                "title_en": "Sonata de Guitarra de 6 órdenes",
                "title_zh": "《六组弦吉他奏鸣曲》",
                "status": "corrected",
                "basis": "music_terminology",
                "reason": "órdenes means courses, not dishes",
                "reviewer": "title_review_1",
                "source_refs": [],
            }
        ],
    )

    reviewed = load_reviewed_titles(review_path)

    assert resolve_reviewed_title(
        {"work_id": "1157993", "title_en": "Sonata de Guitarra de 6 órdenes"},
        "《6 道菜吉他奏鸣曲》",
        reviewed,
    ) == "《六组弦吉他奏鸣曲》"


def test_unreviewed_work_keeps_existing_translation(tmp_path):
    review_path = tmp_path / "review.json"
    write_review(review_path, [])

    assert resolve_reviewed_title(
        {"work_id": "1", "title_en": "Prelude"},
        "《前奏曲》",
        load_reviewed_titles(review_path),
    ) == "《前奏曲》"


def test_review_rejects_duplicate_work_ids(tmp_path):
    review_path = tmp_path / "review.json"
    row = {
        "work_id": "1",
        "title_en": "Prelude",
        "title_zh": "《前奏曲》",
        "status": "accepted",
        "basis": "music_terminology",
        "reason": "standard form name",
        "reviewer": "title_review_1",
        "source_refs": [],
    }
    write_review(review_path, [row, row])

    with pytest.raises(TitleReviewError, match="duplicate work_id"):
        load_reviewed_titles(review_path)


def test_review_rejects_title_identity_mismatch(tmp_path):
    review_path = tmp_path / "review.json"
    write_review(
        review_path,
        [
            {
                "work_id": "1",
                "title_en": "Prelude",
                "title_zh": "《前奏曲》",
                "status": "accepted",
                "basis": "music_terminology",
                "reason": "standard form name",
                "reviewer": "title_review_1",
                "source_refs": [],
            }
        ],
    )
    reviewed = load_reviewed_titles(review_path)

    with pytest.raises(TitleReviewError, match="title mismatch"):
        resolve_reviewed_title(
            {"work_id": "1", "title_en": "Fugue"},
            "《赋格》",
            reviewed,
        )


@pytest.mark.parametrize("title_zh", ["", "前奏曲", "《》"])
def test_review_rejects_invalid_chinese_title(tmp_path, title_zh):
    review_path = tmp_path / "review.json"
    write_review(
        review_path,
        [
            {
                "work_id": "1",
                "title_en": "Prelude",
                "title_zh": title_zh,
                "status": "accepted",
                "basis": "music_terminology",
                "reason": "standard form name",
                "reviewer": "title_review_1",
                "source_refs": [],
            }
        ],
    )

    with pytest.raises(TitleReviewError, match="title_zh"):
        load_reviewed_titles(review_path)


def test_category_builder_applies_work_id_review_before_title_mapping():
    namespace = runpy.run_path(str(ROOT / "scripts/build_category_library.py"))
    works = [
        {
            "work_id": "1157993",
            "title_en": "Sonata de Guitarra de 6 órdenes",
        },
        {"work_id": "2", "title_en": "Prelude"},
    ]
    translations = {
        "Sonata de Guitarra de 6 órdenes": "《6 道菜吉他奏鸣曲》",
        "Prelude": "《前奏曲》",
    }
    reviewed = {
        "1157993": {
            "work_id": "1157993",
            "title_en": "Sonata de Guitarra de 6 órdenes",
            "title_zh": "《六组弦吉他奏鸣曲》",
        }
    }

    namespace["apply_title_translations"](works, translations, reviewed)

    assert works[0]["title_zh"] == "《六组弦吉他奏鸣曲》"
    assert works[1]["title_zh"] == "《前奏曲》"


def test_build_canonical_review_covers_accepted_and_corrected_entries():
    corpus = [
        {
            "work_id": "1",
            "title_en": "Prelude",
            "title_zh": "《序幕》",
            "composer": "Example, A.",
            "categories": ["For guitar"],
            "flags": ["prelude_as_stage_opening"],
        },
        {
            "work_id": "2",
            "title_en": "Fugue",
            "title_zh": "《赋格》",
            "composer": "Example, B.",
            "categories": ["For guitar"],
            "flags": [],
        },
    ]
    reviews = [
        {
            "schema_version": 1,
            "shard": 1,
            "audited_count": 1,
            "entries": [
                {
                    "work_id": "1",
                    "title_en": "Prelude",
                    "old_title_zh": "《序幕》",
                    "new_title_zh": "《前奏曲》",
                    "reason": "Use the standard musical-form term.",
                    "basis": "music_terminology",
                    "source_refs": [],
                }
            ],
        },
        {"schema_version": 1, "shard": 2, "audited_count": 1, "entries": []},
    ]

    payload = build_canonical_review(corpus, reviews, reviewed_at="2026-09-04")

    assert payload["summary"] == {
        "work_count": 2,
        "corrected_count": 1,
        "accepted_count": 1,
        "shard_count": 2,
    }
    assert payload["entries"][0]["title_zh"] == "《前奏曲》"
    assert payload["entries"][0]["status"] == "corrected"
    assert payload["entries"][1]["title_zh"] == "《赋格》"
    assert payload["entries"][1]["status"] == "accepted"


def test_build_canonical_review_rejects_stale_correction():
    corpus = [
        {
            "work_id": "1",
            "title_en": "Prelude",
            "title_zh": "《序幕》",
            "composer": "Example, A.",
            "categories": ["For guitar"],
            "flags": [],
        }
    ]
    reviews = [
        {
            "schema_version": 1,
            "shard": 1,
            "audited_count": 1,
            "entries": [
                {
                    "work_id": "1",
                    "title_en": "Prelude",
                    "old_title_zh": "《前奏》",
                    "new_title_zh": "《前奏曲》",
                    "reason": "standard term",
                    "basis": "music_terminology",
                    "source_refs": [],
                }
            ],
        }
    ]

    with pytest.raises(TitleReviewError, match="old_title_zh mismatch"):
        build_canonical_review(corpus, reviews, reviewed_at="2026-09-04")


def test_build_canonical_review_allows_invalid_source_format_when_corrected():
    corpus = [
        {
            "work_id": "1",
            "title_en": "Prelude",
            "title_zh": "前奏曲",
            "composer": "Example, A.",
            "categories": ["For guitar"],
            "flags": ["not_book_title_wrapped"],
        }
    ]
    reviews = [
        {
            "schema_version": 1,
            "shard": 1,
            "audited_count": 1,
            "entries": [
                {
                    "work_id": "1",
                    "title_en": "Prelude",
                    "old_title_zh": "前奏曲",
                    "new_title_zh": "《前奏曲》",
                    "reason": "standard title formatting",
                    "basis": "format_consistency",
                    "source_refs": [],
                }
            ],
        }
    ]

    payload = build_canonical_review(corpus, reviews, reviewed_at="2026-09-04")

    assert payload["entries"][0]["title_zh"] == "《前奏曲》"


def test_apply_review_supplements_updates_canonical_entry_in_order():
    canonical = {
        "schema_version": 1,
        "summary": {"work_count": 1, "corrected_count": 0, "accepted_count": 1},
        "entries": [
            {
                "work_id": "1",
                "title_en": "Prelude",
                "title_zh": "《序幕》",
                "status": "accepted",
                "basis": "corpus_review",
                "reason": "reviewed",
                "reviewer": "title_review_1",
                "source_refs": [],
            }
        ],
    }
    supplements = [
        {
            "schema_version": 1,
            "reviewer": "terminology_qc",
            "entries": [
                {
                    "work_id": "1",
                    "title_en": "Prelude",
                    "current_title_zh": "《序幕》",
                    "new_title_zh": "《前奏》",
                    "basis": "music_terminology",
                    "reason": "First terminology pass.",
                    "source_refs": [],
                }
            ],
        },
        {
            "schema_version": 1,
            "reviewer": "common_name_qc",
            "entries": [
                {
                    "work_id": "1",
                    "title_en": "Prelude",
                    "current_title_zh": "《前奏》",
                    "new_title_zh": "《前奏曲》",
                    "basis": "common_name",
                    "reason": "Use the established form name.",
                    "source_refs": [],
                }
            ],
        },
    ]

    payload = apply_review_supplements(canonical, supplements)

    assert payload["entries"][0]["title_zh"] == "《前奏曲》"
    assert payload["entries"][0]["reviewer"] == "common_name_qc"
    assert payload["summary"]["corrected_count"] == 1
    assert payload["summary"]["accepted_count"] == 0
    assert payload["summary"]["supplement_counts"] == [1, 1]


def test_apply_review_supplements_rejects_stale_current_title():
    canonical = {
        "schema_version": 1,
        "summary": {"work_count": 1, "corrected_count": 1, "accepted_count": 0},
        "entries": [
            {
                "work_id": "1",
                "title_en": "Prelude",
                "title_zh": "《前奏曲》",
                "status": "corrected",
                "basis": "music_terminology",
                "reason": "reviewed",
                "reviewer": "title_review_1",
                "source_refs": [],
            }
        ],
    }
    supplements = [
        {
            "schema_version": 1,
            "reviewer": "stale_review",
            "entries": [
                {
                    "work_id": "1",
                    "title_en": "Prelude",
                    "current_title_zh": "《序幕》",
                    "new_title_zh": "《前奏》",
                    "basis": "music_terminology",
                    "reason": "stale",
                    "source_refs": [],
                }
            ],
        }
    ]

    with pytest.raises(TitleReviewError, match="current_title_zh mismatch"):
        apply_review_supplements(canonical, supplements)


def test_apply_reviewed_titles_updates_catalog_and_local_title_map(tmp_path):
    root = tmp_path
    (root / "config").mkdir()
    (root / "config/categories.json").write_text(
        json.dumps({"categories": [{"name": "For guitar"}]}), encoding="utf-8"
    )
    (root / "config/mixed_categories.json").write_text(
        json.dumps({"categories": []}), encoding="utf-8"
    )
    metadata = root / "For guitar/metadata"
    metadata.mkdir(parents=True)
    (metadata / "catalog.json").write_text(
        json.dumps(
            [
                {
                    "work_id": "1",
                    "title_en": "Prelude",
                    "title_zh": "《序幕》",
                }
            ]
        ),
        encoding="utf-8",
    )
    (metadata / "translations_zh.json").write_text(
        json.dumps({"Prelude": "《序幕》"}), encoding="utf-8"
    )
    reviewed = {
        "1": {
            "work_id": "1",
            "title_en": "Prelude",
            "title_zh": "《前奏曲》",
        }
    }

    summary = apply_reviewed_titles(root, reviewed)

    assert summary == {"categories": 1, "catalog_records": 1, "changed_records": 1}
    catalog = json.loads((metadata / "catalog.json").read_text())
    local_map = json.loads((metadata / "translations_zh.json").read_text())
    assert catalog[0]["title_zh"] == "《前奏曲》"
    assert local_map["Prelude"] == "《前奏曲》"


def test_audit_reviewed_titles_reports_coverage_and_catalog_mismatch(tmp_path):
    root = tmp_path
    (root / "config").mkdir()
    (root / "config/categories.json").write_text(
        json.dumps(
            {"categories": [{"name": "For guitar"}, {"name": "For 2 guitars"}]}
        ),
        encoding="utf-8",
    )
    (root / "config/mixed_categories.json").write_text(
        json.dumps({"categories": []}), encoding="utf-8"
    )
    for category, title_zh in (
        ("For guitar", "《序幕》"),
        ("For 2 guitars", "《前奏曲》"),
    ):
        metadata = root / category / "metadata"
        metadata.mkdir(parents=True)
        (metadata / "catalog.json").write_text(
            json.dumps(
                [
                    {
                        "work_id": "1",
                        "title_en": "Prelude",
                        "title_zh": title_zh,
                    }
                ]
            ),
            encoding="utf-8",
        )
    reviewed = {
        "1": {
            "work_id": "1",
            "title_en": "Prelude",
            "title_zh": "《前奏曲》",
            "status": "corrected",
        }
    }

    report = audit_reviewed_titles(root, reviewed)

    assert report["category_records"] == 2
    assert report["unique_work_ids"] == 1
    assert report["missing_review_ids"] == []
    assert report["extra_review_ids"] == []
    assert report["catalog_title_mismatches"] == 1
    assert report["cross_category_inconsistent_work_ids"] == 1
    assert report["quality_flag_counts"] == {}


@pytest.mark.parametrize(
    ("title_en", "title_zh", "expected_flag"),
    [
        ("5 Pieces", "《5 件》", "pieces_as_objects"),
        ("Prelude", "《序幕》", "prelude_as_stage_opening"),
        ("Air varié", "《各种空气》", "air_as_substance"),
        ("Saltarello", "《跳线》", "saltarello_as_wire"),
        ("Suite in D major", "《D大调套房》", "suite_as_room"),
        ("Etude No.1", "《第一号研究》", "etude_as_research"),
        ("Contredanses, Op.8", "《矛盾，作品8》", "contredanse_as_contradiction"),
        ("Polonaise No.1", "《第一号波兰人》", "polonaise_as_person"),
        ("Mass in C major", "《C大调群众》", "mass_as_crowd"),
        ("Minuet", "《小步舞》", "minuet_missing_qu"),
        ("Fuga prima", "《先逃走》", "fugue_as_escape"),
        ("Valse", "《错误的》", "waltz_as_wrong"),
        ("Variaciones", "《变化》", "variations_as_changes"),
        ("Ricercari per organo", "《风琴米饭》", "ricercar_as_rice"),
        ("Courante", "《当前》", "courante_as_current"),
        ("Polaca", "《抛光》", "polacca_as_polish"),
        ("2 Boleros", "《2短上衣》", "bolero_as_jacket"),
        ("Contradanza", "《对比度》", "contradanza_as_contrast"),
        ("Guitar Sonatina", "《吉他奏鸣曲》", "sonatina_as_sonata"),
        ("6 Duos for 2 Cellos", "《双大提琴六重奏》", "duet_as_large_ensemble"),
        ("Menuet difícil", "《困难的菜单》", "minuet_as_menu"),
        ("Gigue", "《抖动》", "gigue_as_jitter"),
        ("Bourrée", "《酿的》", "bourree_as_brewed"),
        ("Principios para guitarra de 6 ordenes", "《六阶吉他演奏原理》", "courses_as_steps"),
        ("3 Pieces, Op.2", "《3首，作品2》", "generic_pieces_missing_noun"),
        ("2 Pequeñas piezas", "《2小块》", "pieces_as_chunks"),
        ("4 Piezas Progresivas", "《4首前卫作品》", "progressive_as_avant_garde"),
        ("Pièces de société", "《协会作品》", "society_pieces_as_association"),
        ("Guitar Piece No.2", "《吉他第二首》", "guitar_piece_missing_form"),
        ("Andante et allegro", "《行走且喜乐》", "andante_as_walking"),
        ("Andantino in A major", "《A大调行板》", "andantino_not_diminutive"),
        ("Allegretto in G major", "《G大调快板》", "allegretto_not_diminutive"),
        ("Larghetto et Variations", "《长音及变奏曲》", "larghetto_mistranslated"),
        ("Fantasie, MJ 3", "《幻想，MJ 3》", "fantasia_missing_form"),
        ("Ballet Anglois", "《英国芭蕾舞团》", "ballet_as_troupe"),
        ("Volte de France", "《法国之电压》", "volta_literal_translation"),
        ("Branle anglais", "《英语打手枪》", "branle_literal_translation"),
        ("Canarie la Contre Chèvre", "《金丝雀对抗山羊》", "canarie_as_canary"),
        ("La Follia", "《疯狂》", "follia_as_madness"),
        ("Italian Ground", "《意大利地面》", "ground_literal_translation"),
        ("Magnificat Fugue", "《圣母颂赋格》", "magnificat_wrong_name"),
        ("Aubade, Op.84", "《奥巴德，作品84》", "aubade_untranslated"),
        ("Almain, IFC 3", "《德国，国际金融公司3》", "almain_literal_translation"),
        ("Tiento 19 del cuarto tono", "《第四调式帐篷19》", "tiento_as_tent"),
        ("Danceries, Livre 2", "《舞剧，第2册》", "danceries_as_drama"),
        ("21 In Nomines", "《21名提名》", "in_nomine_as_nomination"),
        ("Voltes 1 & 2", "《第一轮和第二轮》", "volta_literal_translation"),
    ],
)
def test_title_quality_flags_known_literal_mistranslations(
    title_en, title_zh, expected_flag
):
    assert expected_flag in title_quality_flags(title_en, title_zh)


def test_title_quality_flags_accepts_standard_musical_terms():
    examples = [
        ("5 Pieces", "《5首小品》"),
        ("Prelude", "《前奏曲》"),
        ("Air varié", "《咏叹调变奏曲》"),
        ("Saltarello", "《萨尔塔雷洛舞曲》"),
        ("Suite in D major", "《D大调组曲》"),
        ("Etude No.1", "《第1号练习曲》"),
        ("Contredanses, Op.8", "《对舞曲，作品8》"),
        ("Polonaise No.1", "《第1号波洛奈兹舞曲》"),
        ("Mass in C major", "《C大调弥撒曲》"),
        ("Minuet", "《小步舞曲》"),
        ("The Study of Harmony", "《和声研究》"),
        ("Fuga prima", "《第一赋格曲》"),
        ("Valse", "《圆舞曲》"),
        ("Variaciones", "《变奏曲》"),
        ("Ricercari per organo", "《管风琴利切尔卡尔集》"),
        ("Courante", "《库朗特舞曲》"),
        ("Polaca", "《波兰舞曲》"),
        ("2 Boleros", "《两首波莱罗舞曲》"),
        ("Contradanza", "《对舞曲》"),
        ("Guitar Sonatina", "《吉他小奏鸣曲》"),
        ("6 Duos for 2 Cellos", "《六首双大提琴二重奏》"),
        ("Menuet difícil", "《困难小步舞曲》"),
        ("Gigue", "《吉格舞曲》"),
        ("Bourrée", "《布列舞曲》"),
        ("Principios para guitarra de 6 ordenes", "《六组复弦吉他演奏原理》"),
        ("3 Pieces, Op.2", "《3首小品，作品2》"),
        ("2 Pequeñas piezas", "《两首小品》"),
        ("4 Piezas Progresivas", "《四首循序渐进小品》"),
        ("Pièces de société", "《社交小品》"),
        ("Guitar Piece No.2", "《第二号吉他小品》"),
        ("Andante et allegro", "《行板与快板》"),
        ("Andantino in A major", "《A大调小行板》"),
        ("Allegretto in G major", "《G大调小快板》"),
        ("Larghetto et Variations", "《小广板与变奏曲》"),
        ("Fantasie, MJ 3", "《幻想曲，MJ 3》"),
        ("Fantaisie-sonate", "《幻想奏鸣曲》"),
        ("Valse Fantasie", "《幻想圆舞曲》"),
        ("Ballet Anglois", "《英国芭蕾舞曲》"),
        ("Volte de France", "《法国沃尔塔舞曲》"),
        ("Branle anglais", "《英国布朗勒舞曲》"),
        ("Canarie la Contre Chèvre", "《卡纳里舞曲〈La Contre Chèvre〉》"),
        ("La Follia", "《福利亚》"),
        ("Italian Ground", "《意大利固定低音曲》"),
        ("Magnificat Fugue", "《尊主颂赋格曲》"),
        ("Aubade, Op.84", "《晨歌，作品84》"),
        ("Almain, IFC 3", "《阿勒曼德舞曲，IFC 3》"),
        ("Tiento 19 del cuarto tono", "《第四调式第19号蒂恩托》"),
        ("Danceries, Livre 2", "《舞曲集，第2册》"),
        ("21 In Nomines", "《21首〈以主之名〉曲》"),
        ("Voltes 1 & 2", "《第1、2号沃尔塔舞曲》"),
    ]

    assert all(not title_quality_flags(title_en, title_zh) for title_en, title_zh in examples)


@pytest.mark.parametrize(
    ("title_en", "title_zh", "expected_flag"),
    [
        ("Piano Sonata in E major", "《D大调钢琴奏鸣曲》", "key_mismatch"),
        ("The Seasons, Op.37a", "《四季，作品37》", "opus_number_missing"),
        ("Guitar Sonata No.2", "《第3号吉他奏鸣曲》", "work_number_missing"),
        (
            "Toccata, Adagio and Fugue in C major, BWV 564",
            "《C大调托卡塔、慢板与赋格，BWV》",
            "catalog_number_missing",
        ),
        (
            "Toccata and Fugue in D minor, P.469",
            "《D小调托卡塔与赋格，第469页》",
            "catalog_number_missing",
        ),
        (
            "Romanian Folk Dances, Sz.56",
            "《罗马尼亚民间舞曲，第56号》",
            "catalog_number_missing",
        ),
    ],
)
def test_title_identity_flags_detects_lost_structural_information(
    title_en, title_zh, expected_flag
):
    assert expected_flag in title_identity_flags(title_en, title_zh)


def test_title_identity_flags_accepts_chinese_numbering_and_keys():
    examples = [
        ("Piano Sonata in E major", "《E大调钢琴奏鸣曲》"),
        ("The Seasons, Op.37a", "《四季，作品37a》"),
        ("Guitar Sonata No.2", "《第二号吉他奏鸣曲》"),
        ("Ricercar No.2", "《利切尔卡尔二号》"),
        ("Suite No.3", "《3号组曲》"),
        ("Piano Sonata No.14, Op.27 No.2", "《升C小调第十四钢琴奏鸣曲“月光”，Op.27之2》"),
        ("Prelude and Fugue in B-flat minor", "《降B小调前奏曲与赋格》"),
        (
            "Toccata, Adagio and Fugue in C major, BWV 564",
            "《C大调托卡塔、慢板与赋格，BWV 564》",
        ),
        ("Toccata and Fugue in D minor, P.469", "《D小调托卡塔与赋格，P.469》"),
        ("Quartetto, TWV 43:G2", "《四重奏，TWV 43：G2》"),
        ("Romanian Folk Dances, Sz.56", "《罗马尼亚民间舞曲，Sz.56》"),
    ]

    assert all(not title_identity_flags(title_en, title_zh) for title_en, title_zh in examples)
