from __future__ import annotations

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
