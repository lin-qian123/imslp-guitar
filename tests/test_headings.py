from __future__ import annotations

import pytest

from imslp_library.headings import normalize_heading, parse_heading_tree
from tests.basic_helpers import load_text_fixture, make_file_template, make_wikitext


def _fixture(name: str) -> str:
    return load_text_fixture(f"wikitext/{name}")


def test_parser_uses_only_the_files_region_and_separates_branches():
    text = "==Outside Before==\n{{#fte:imslpfile\n|File Name 1=outside.pdf\n|File ID=1\n}}\n" + _fixture("original_guitar.wiki") + "\n==Outside After=="
    tree = parse_heading_tree(text)

    assert [node.raw for node in tree.roots] == ["Scores and Parts", "Arrangements and Transcriptions"]
    assert [chunk.filename for chunk in tree.roots[0].file_templates] == ["original.pdf"]
    assert [chunk.filename for chunk in tree.roots[1].children[0].file_templates] == ["piano.pdf"]
    assert "outside.pdf" not in [chunk.filename for chunk in tree.file_templates]


def test_nodes_preserve_raw_normalized_ancestry_spans_and_relationship_tokens():
    mixed = parse_heading_tree(_fixture("mixed_bass.wiki"))
    target = mixed.roots[0].children[0]
    assert target.raw == "For 3 Guitars and Double Bass or Bass Guitar (Rest)"
    assert target.normalized == "for 3 guitars and double bass or bass guitar (rest)"
    assert target.ancestry == (
        "Arrangements and Transcriptions",
        "For 3 Guitars and Double Bass or Bass Guitar (Rest)",
    )
    assert target.parent is mixed.roots[0]
    assert target.level == 4
    assert target.source_span[0] < target.source_span[1]

    nested = parse_heading_tree(_fixture("nested_other_instrument.wiki"))
    descendant = nested.roots[0].children[0].children[0]
    assert descendant.raw == "With Bass Guitar"
    assert descendant.normalized == "with bass guitar"
    assert descendant.parent.raw == "For 3 Guitars"
    assert descendant.file_templates[0].filename == "child-bass.pdf"


def test_heading_normalization_decodes_markup_entities_unicode_and_whitespace_without_losing_punctuation():
    decomposed = "  ''For''  3 Guitar\u0073 &amp; [[Bass guitar|Bass]] / Voice (Hoge\u0308r, Anton)  "
    assert normalize_heading(decomposed) == "for 3 guitars & bass / voice (hogër, anton)"


def test_parsed_raw_heading_remains_exact_while_normalized_heading_removes_markup():
    raw = "''For'' {{Noitalic|3}} Guitars &amp; Voice"
    tree = parse_heading_tree(
        make_wikitext(f"==={raw}===\n" + make_file_template("score.pdf", "9"), "orchestra")
    )
    assert tree.roots[0].raw == raw
    assert tree.roots[0].normalized == "for 3 guitars & voice"


def test_file_template_chunks_keep_source_span_raw_text_and_non_pdf_attachment():
    tree = parse_heading_tree(_fixture("exact_three_guitars.wiki"))
    chunks = tree.roots[0].children[0].file_templates
    assert [(chunk.file_id, chunk.filename) for chunk in chunks] == [
        ("101", "three-guitars.pdf"),
        ("102", "source.mscz"),
    ]
    assert all(chunk.raw.startswith("{{#fte:imslpfile") for chunk in chunks)
    assert all(chunk.source_span[0] < chunk.source_span[1] for chunk in chunks)


def test_comment_and_inline_fake_markers_cannot_steal_the_real_files_region():
    fake = (
        "<!--\n| *****FILES*****\n===Fake===\n"
        + make_file_template("fake.pdf", "1")
        + "\n| *****WORK INFO*****\n-->\n"
        + "inline | *****FILES***** is not a marker\n"
    )
    real = make_wikitext(
        "<!--\n| *****WORK INFO*****\n===Commented Fake===\n"
        + make_file_template("commented-fake.pdf", "8")
        + "\n-->\n===Scores and Parts===\n"
        + make_file_template("real.pdf", "2"),
        "guitar",
    )
    tree = parse_heading_tree(fake + real)
    assert [node.raw for node in tree.roots] == ["Scores and Parts"]
    assert [chunk.filename for chunk in tree.file_templates] == ["real.pdf"]


@pytest.mark.parametrize(
    "text",
    [
        "===Scores and Parts===\n" + make_file_template("score.pdf", "3") + "\n| *****WORK INFO*****",
        "| *****WORK INFO*****\n===Scores and Parts===\n"
        + make_file_template("score.pdf", "3")
        + "\n| *****FILES*****",
        "| *****FILES*****\n===Scores and Parts===\n" + make_file_template("score.pdf", "3"),
    ],
)
def test_missing_or_reversed_marker_pairs_produce_an_empty_stable_tree(text):
    tree = parse_heading_tree(text)
    assert tree.roots == []
    assert tree.file_templates == ()
    assert tree.files_region_span == (0, 0)


EXACT_FIXTURES = {
    "exact_three_guitars.wiki": make_wikitext(
        "===Arrangements and Transcriptions===\n====For 3 Guitars (Smith, John)====\n"
        + make_file_template("three-guitars.pdf", "101")
        + "\n"
        + make_file_template("source.mscz", "102"),
        "orchestra",
    ),
    "mixed_bass.wiki": make_wikitext(
        "===Arrangements and Transcriptions===\n====For 3 Guitars and Double Bass or Bass Guitar (Rest)====\n"
        + make_file_template("mixed-score.pdf", "201"),
        "orchestra",
    ),
    "nested_other_instrument.wiki": make_wikitext(
        "===Arrangements and Transcriptions===\n====For 3 Guitars====\n=====With Bass Guitar=====\n"
        + make_file_template("child-bass.pdf", "301"),
        "orchestra",
    ),
    "original_guitar.wiki": make_wikitext(
        "===Scores and Parts===\n"
        + make_file_template("original.pdf", "401")
        + "\n===Arrangements and Transcriptions===\n====For Piano====\n"
        + make_file_template("piano.pdf", "402"),
        "guitar",
    ),
    "work_level_exact.wiki": make_wikitext(
        "===Scores and Parts===\n" + make_file_template("work-level.pdf", "501"),
        "3 guitars",
    ),
    "work_level_mixed.wiki": make_wikitext(
        "===Scores and Parts===\n" + make_file_template("mixed-work-level.pdf", "502"),
        "3 guitars and double bass",
    ),
    "flexible_2_and_3.wiki": make_wikitext(
        "===Arrangements and Transcriptions===\n====For 2 and 3 Guitars (Doe, Jane)====\n"
        + make_file_template("flexible.pdf", "601"),
        "orchestra",
    ),
}


@pytest.mark.parametrize("filename,expected", EXACT_FIXTURES.items())
def test_checked_in_wikitext_fixtures_are_exact_builder_bytes(filename, expected):
    assert _fixture(filename) == expected
