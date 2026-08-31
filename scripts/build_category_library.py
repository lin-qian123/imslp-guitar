#!/usr/bin/env python
"""Build and maintain one local IMSLP pure-guitar category library.

The script deliberately separates metadata discovery from file downloading.
IMSLP may require an interactive human verification before serving score files;
HTML challenge pages are never accepted as PDFs.

Category-specific values are supplied through ``IMSLP_LIBRARY_ROOT``,
``IMSLP_CATEGORY_NAME``, ``IMSLP_CATEGORY_KIND`` and ``IMSLP_GUITAR_COUNT``.
``IMSLP_TARGET_INSTRUMENT`` may override the heading/instrumentation text for
non-numbered IMSLP categories such as ``For guitar`` or ``For guitar ensemble``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(os.environ.get("IMSLP_LIBRARY_ROOT", Path(__file__).resolve().parents[1])).expanduser().resolve()
METADATA_DIR = ROOT / "metadata"
CACHE_DIR = METADATA_DIR / ".cache" / "pages"
SCORES_DIR = ROOT / "scores"
LOGS_DIR = ROOT / "logs"
API_URL = "https://imslp.org/api.php"
CATEGORY_NAME = os.environ.get("IMSLP_CATEGORY_NAME", "For 3 guitars (arr)").strip()
CATEGORY_KIND = os.environ.get("IMSLP_CATEGORY_KIND", "arrangement").strip().casefold()
GUITAR_COUNT = int(os.environ.get("IMSLP_GUITAR_COUNT", "3"))
if CATEGORY_KIND not in {"original", "arrangement"}:
    raise RuntimeError("IMSLP_CATEGORY_KIND must be 'original' or 'arrangement'")
if GUITAR_COUNT < 1:
    raise RuntimeError("IMSLP_GUITAR_COUNT must be positive")
CATEGORY = f"Category:{CATEGORY_NAME}"
TARGET_INSTRUMENT = os.environ.get("IMSLP_TARGET_INSTRUMENT", "").strip()
if not TARGET_INSTRUMENT:
    TARGET_INSTRUMENT = re.sub(r"\s*\(arr\)\s*$", "", CATEGORY_NAME, flags=re.IGNORECASE)
    TARGET_INSTRUMENT = re.sub(r"^For\s+", "", TARGET_INSTRUMENT, flags=re.IGNORECASE).strip()
if not TARGET_INSTRUMENT:
    raise RuntimeError("Unable to derive the target IMSLP instrumentation")
CATEGORY_URL = "https://imslp.org/wiki/" + urllib.parse.quote(CATEGORY.replace(" ", "_"), safe="_():,'-.~")
USER_AGENT = f"Codex-IMSLP-{CATEGORY_NAME.replace(' ', '-')}-Library/1.0 (personal research library)"
LIBRARY_TITLE = f"IMSLP {CATEGORY_NAME} 乐谱库"
PDF_KIND_LABEL = os.environ.get("IMSLP_PDF_KIND_LABEL", "").strip()
if not PDF_KIND_LABEL:
    PDF_KIND_LABEL = "吉他 PDF" if GUITAR_COUNT == 1 else f"{GUITAR_COUNT}把吉他 PDF"

MIXED_INSTRUMENT_TOKENS = (
    "accordion", "bass guitar", "bassoon", "cello", "clarinet", "contrabass",
    "double bass", "drum", "electric", "flute", "harp", "harpsichord", "horn",
    "keyboard", "lute", "mandolin", "oboe", "organ", "percussion", "piano",
    "recorder", "saxophone", "trombone", "trumpet", "tuba", "ukulele", "viola",
    "violin", "voice", "vocal", "woodwind",
)
WORK_LEVEL_TARGET_SECTIONS = {
    "Work-level target-guitar score",
    # Kept for the already-migrated three-guitar catalog.
    "Work-level three-guitar score",
}

MEMBERS_JSON = METADATA_DIR / "category_members.json"
CATALOG_JSON = METADATA_DIR / "catalog.json"
MANIFEST_JSON = METADATA_DIR / "score_manifest.json"
TRANSLATIONS_JSON = METADATA_DIR / "translations_zh.json"
COMPOSER_TRANSLATIONS_JSON = METADATA_DIR / "composer_translations_zh.json"
CATALOG_CSV = ROOT / "catalog.csv"
MANIFEST_CSV = ROOT / "score_manifest.csv"
DOWNLOAD_LOG = LOGS_DIR / "download_status.csv"
DOWNLOAD_OVERRIDES_JSON = METADATA_DIR / "download_overrides.json"
EXCLUDED_NON_TARGET_JSON = METADATA_DIR / "excluded_non_guitar_files.json"
QUARANTINE_DIR = ROOT / "quarantine" / "non-guitar"
REVISION_QUARANTINE_DIR = ROOT / "quarantine" / "revision-mismatch"
REVISION_ALIAS_RECOVERY_JSON = METADATA_DIR / "revision_alias_recovery.json"
ACTIVE_QUARANTINE_RECOVERY_JSON = METADATA_DIR / "active_quarantine_recovery.json"

HEADING_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$")
FILE_TEMPLATE_RE = re.compile(r"\{\{#fte:imslpfile\b", re.IGNORECASE)
FIELD_RE_TEMPLATE = r"^\|{name}\s*(\d*)\s*=\s*(.*?)\s*$"

# File descriptions are the strongest per-file instrumentation evidence on an
# IMSLP work page.  These patterns intentionally use Unicode word boundaries:
# for example, ``viola`` must not match Portuguese ``violão`` and ``harp`` must
# not match ``sharp``.  A file that combines guitar with another instrument is
# excluded as well, because this library contains target-guitar PDFs only.
NON_TARGET_FILE_PATTERNS = (
    ("accordion", r"accordion"),
    ("bass guitar", r"bass\s+guitar|electric\s+bass|bassgitarre|baixol[aã]o|\bbaixo\b"),
    ("bassoon", r"bassoon|fagott"),
    ("bagpipe", r"bagpipe|dudelsack"),
    ("banjo", r"banjo"),
    ("cavaquinho", r"cavaquinho"),
    ("cello", r"cello|violoncello|violoncelle"),
    ("clarinet", r"clarinet(?:te)?|klarinette|clarinete|clavinet"),
    ("double bass", r"double\s+bass|contrabass|string\s+bass"),
    ("dulcimer", r"dulcimer"),
    ("ensemble", r"ensemble"),
    ("flute", r"flute|flauta|fl[öu]te|querfl[öo]-?te|\bflo-te\b"),
    ("glockenspiel", r"glockenspiel"),
    ("harp", r"harp"),
    ("harpsichord", r"harpsichord|cembalo"),
    ("horn", r"french\s+horn|franzo-sischer\s+horn|corno|\bhorn\b"),
    ("koto", r"koto"),
    ("lute", r"lute|theorbo"),
    ("mandolin", r"mandolin|mandola|bandolim"),
    ("marimba", r"marimba"),
    ("oboe", r"oboe|hautbois"),
    ("orchestra", r"orchestra"),
    ("organ", r"organ"),
    ("percussion", r"percussion|\bpercu\b|drum(?:-set)?"),
    ("piano", r"piano(?:forte)?|klavier"),
    ("recorder", r"recorder"),
    ("saxophone", r"sax(?:ophon|ophone)?|sassofono"),
    ("sitar", r"sitar"),
    ("timpani", r"timpani"),
    ("trombone", r"trombone"),
    ("trumpet", r"trumpet|trompete"),
    ("tuba", r"tuba"),
    ("ukulele", r"ukulele|ukelele|guitalele"),
    ("viola", r"viola"),
    ("vibraphone", r"vibra(?:phon|phone|fon)"),
    ("violin", r"violin|violinen|violine|violino|violon|vln"),
    ("voice", r"voice|vocal|choir|chor|ma-nnerchor|singer"),
    ("xylophone", r"xylophon|xylophone"),
)
NON_TARGET_DESCRIPTION_RE = tuple(
    (
        label,
        re.compile(rf"(?<!\w)(?:{pattern})(?!\w)", re.IGNORECASE),
    )
    for label, pattern in NON_TARGET_FILE_PATTERNS
)

# For original-work categories only, a parenthesized or terminal instrument
# tag in the filename is reliable evidence for an alternate-instrument file.
# This deliberately does not reject arbitrary title words such as
# ``Romanza ... al Violoncello`` or arrangement filenames naming their source.
NON_TARGET_FILENAME_TAG_RE = re.compile(
    r"(?:"
    r"\((?P<parenthesized>piano|pianoforte|banjo|viola|violin|violine|vln|flute|clarinet(?:te)?|oboe|cello)\)"
    r"|[-_](?P<terminal>piano|pianoforte|banjo|ensemble|lute|viola|violin|violine|vln|flute|clarinet(?:te)?|oboe|cello)\.pdf$"
    r"|_-_(?P<structured>3_violinen|accordion|bassgitarre|bassoon|bagpipe|celesta|dulcimer|duett|fagott|banjo|cello|clarinet(?:te)?|klarinette|cembalo|dudelsack|flute|flo-te|glockenspiel|harfe|harp|horn|keyboard|klavier|koto|ma-nnerchor|marimba|oboe|percu|piano|querflo-te|sax|sopran-sax|sitar|timpani|trio|trombone|trompete|ukelele|viola|violin|violinen|violine|vibrafon|vibraphon|vibraphone|vln|xylophon)(?=[,._&+\-])"
    r")",
    re.IGNORECASE,
)
NON_TARGET_FILENAME_EXCEPTIONS = {
    # Historical Portuguese ``viola`` here denotes the guitar-family target
    # instrument; the IMSLP work itself is a guitar method, not a viola part.
    "PMLP229290-ribeiro_nova_arte_de_viola.pdf",
}
MANUAL_NON_TARGET_FILENAMES = {
    "PMLP1350556-downes_g.661.-23.-_Select_Airs_&_Lessons_Regency_Harp_Lute.pdf",
    "PMLP419607-Solo_Duo_Vol_1_combo.pdf",
    "PMLP638673-Morphy_Lute.pdf",
    "PMLP1142955-Zuth_J-Handbuch_der_Laute_und_Gitarre.pdf",
    "PMLP140523-young's_vocal_and_instrumental_miscellany_1793.pdf",
    "PMLP1004976-Odell,_Herbert_Forrest-A_Dreamlet-Complete_Parts.pdf",
    "PMLP1115382-regencia-vol1.pdf",
    "PMLP1116298-regencia-vol2.pdf",
    "PMLP444786-carulli_op333_no1-3.pdf",
    "PMLP579588-String4tet.pdf",
}
NON_TARGET_DESCRIPTION_EXCEPTIONS = {
    "PMLP1281364-RockzaCaelum65_220524Oosika.pdf",
}
TARGET_EVIDENCE_FILENAME_EXCEPTIONS = {
    "PMLP229290-ribeiro_nova_arte_de_viola.pdf",
}
MULTI_GUITAR_DESCRIPTION_RE = re.compile(
    r"(?<!\w)(?:duets?|duos?|2\s*guitars?|two\s+guitars?|trio\s+de\s+guitares?|3\s*guitars?)(?!\w)",
    re.IGNORECASE,
)
MULTI_GUITAR_FILENAME_RE = re.compile(
    r"(?:voice[_-]guitar|guitar[_-]duet|sonata[_-]2[_-]guitars|"
    r"1[_ -](?:ou|or)[_ -]2[_ -]guitar|duetti.*due[_ -]chitarre|"
    r"(?:^|[_-])(?:duo|duos|duet|duets|duetti)(?:[_-](?:tab))?\.pdf$|"
    r"string[_-]trio|(?:^|[_-])2g(?:[_+.-]|$)|(?:trans[._ -]*)piano)",
    re.IGNORECASE,
)


KNOWN_TITLES_ZH = {
    "The Entertainer": "《演艺人》",
    "The One Horse Open Sleigh": "《一匹马拉的雪橇》（《铃儿响叮当》原题）",
    "Stille Nacht, heilige Nacht, H.145": "《平安夜》，H.145",
    "Wachet auf, ruft uns die Stimme, BWV 140": "《醒来吧，守望者的声音呼唤》，BWV 140",
    "12 Danzas españolas": "《12首西班牙舞曲》",
    "Danza mora": "《摩尔舞曲》",
    "Dolly Suite, Op.56": "《洋娃娃组曲》，Op.56",
    "French Suite No.1 in D minor, BWV 812": "D小调第1号法国组曲，BWV 812",
    "Musikalisches Opfer, BWV 1079": "《音乐的奉献》，BWV 1079",
    "Préludes, Livre 1, CD 125": "《前奏曲集》第1册，CD 125",
    "Lieder ohne Worte, Op.19b": "《无词歌》，Op.19b",
    "Mallorca, Op.202": "《马略卡》，Op.202",
    "Solace": "《慰藉》",
    "Home, Sweet Home": "《家，甜蜜的家》",
    "Ding Dong Merrily on High": "《叮咚！欢乐在高天》",
    "Pastime with Good Company": "《与良友共度时光》",
    "Sonatina, Sz.55": "《小奏鸣曲》，Sz.55",
    "Ma fin est mon commencement": "《我的终点就是我的起点》",
    "Puis qu'en oubli": "《既然我被遗忘》",
    "J'ay pris amours": "《我已选择爱情》",
    "Allez regrets": "《离去吧，悔恨》",
    "Les petits riens, K.Anh.10/299b": "《小小的无事》，K.Anh.10/299b",
    "So oft ich meine Tobackspfeife, BWV 515": "《每当我抽烟斗时》，BWV 515",
    "Pastorale in D": "D大调田园曲",
    "Pavan": "帕凡舞曲",
    "Bolero": "波莱罗舞曲",
    "Morpeth Rant": "《莫珀斯乡村舞曲》",
    "Temporal Waves": "《时间之波》",
    "Timberlake": "《林中湖》",
    "Coconut Samba": "《椰子桑巴》",
}

FORM_TERMS = [
    ("Prelude and Fugue", "前奏曲与赋格"),
    ("Trio Sonata", "三重奏鸣曲"),
    ("Keyboard Sonata", "键盘奏鸣曲"),
    ("Piano Sonata", "钢琴奏鸣曲"),
    ("Organ Sonata", "管风琴奏鸣曲"),
    ("Piano Trio", "钢琴三重奏"),
    ("String Trio", "弦乐三重奏"),
    ("Organ Trio", "管风琴三重奏"),
    ("Baryton Trio", "巴里顿琴三重奏"),
    ("Divertimento", "嬉游曲"),
    ("Fantasia", "幻想曲"),
    ("Fantasie", "幻想曲"),
    ("Capriccio", "随想曲"),
    ("Canzonetta", "小坎佐纳"),
    ("Canzona", "坎佐纳"),
    ("Canzone", "坎佐纳"),
    ("Canzon", "坎佐纳"),
    ("Ricercar", "利切尔卡尔"),
    ("Toccata", "托卡塔"),
    ("Tocata", "托卡塔"),
    ("Galliard", "加利亚德舞曲"),
    ("Galliarda", "加利亚德舞曲"),
    ("Paduana", "帕多瓦舞曲"),
    ("Sonatina", "小奏鸣曲"),
    ("Sonata", "奏鸣曲"),
    ("Sinfonia", "三部创意曲"),
    ("Fugue", "赋格"),
    ("Fuga", "赋格"),
    ("Suite", "组曲"),
    ("Ouverture", "序曲"),
    ("Overture", "序曲"),
    ("Serenade", "小夜曲"),
    ("Adagio", "柔板"),
    ("Andante", "行板"),
    ("Allemande", "阿勒曼德舞曲"),
    ("Almande", "阿勒曼德舞曲"),
    ("Menuetto", "小步舞曲"),
    ("Ballades", "叙事歌集"),
]

KEY_ZH = {
    "C": "C", "C-sharp": "升C", "C-flat": "降C",
    "D": "D", "D-sharp": "升D", "D-flat": "降D",
    "E": "E", "E-sharp": "升E", "E-flat": "降E",
    "F": "F", "F-sharp": "升F", "F-flat": "降F",
    "G": "G", "G-sharp": "升G", "G-flat": "降G",
    "A": "A", "A-sharp": "升A", "A-flat": "降A",
    "B": "B", "B-sharp": "升B", "B-flat": "降B",
}


def ensure_dirs() -> None:
    for path in (METADATA_DIR, CACHE_DIR, SCORES_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def api_get(params: dict[str, Any], retries: int = 5) -> dict[str, Any]:
    query = urllib.parse.urlencode({**params, "format": "json"}, doseq=True)
    request = urllib.request.Request(
        f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt + 1 == retries:
                raise RuntimeError(f"IMSLP API request failed: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def batched(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def get_category_members(refresh: bool = False) -> list[dict[str, Any]]:
    if MEMBERS_JSON.exists() and not refresh:
        return json.loads(MEMBERS_JSON.read_text(encoding="utf-8"))

    members: list[dict[str, Any]] = []
    cmcontinue: str | None = None
    while True:
        params: dict[str, Any] = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": CATEGORY,
            "cmnamespace": 0,
            "cmlimit": 500,
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        payload = api_get(params)
        members.extend(payload.get("query", {}).get("categorymembers", []))
        cmcontinue = (
            payload.get("continue", {}).get("cmcontinue")
            or payload.get("query-continue", {}).get("categorymembers", {}).get("cmcontinue")
        )
        if not cmcontinue:
            break
    members.sort(key=lambda item: item["title"].casefold())
    MEMBERS_JSON.write_text(json.dumps(members, ensure_ascii=False, indent=2), encoding="utf-8")
    return members


def get_page_wikitext(members: list[dict[str, Any]], refresh: bool = False) -> dict[int, str]:
    texts: dict[int, str] = {}
    missing: list[dict[str, Any]] = []
    for member in members:
        pageid = int(member["pageid"])
        cache_path = CACHE_DIR / f"{pageid}.txt"
        if cache_path.exists() and not refresh:
            texts[pageid] = cache_path.read_text(encoding="utf-8")
        else:
            missing.append(member)

    page_batch_size = 50
    for batch_number, batch in enumerate(batched(missing, page_batch_size), start=1):
        payload = api_get(
            {
                "action": "query",
                "prop": "revisions",
                "pageids": "|".join(str(item["pageid"]) for item in batch),
                "rvprop": "content",
            }
        )
        pages = payload.get("query", {}).get("pages", {})
        for page in pages.values():
            pageid = int(page["pageid"])
            revisions = page.get("revisions") or []
            text = revisions[0].get("*", "") if revisions else ""
            texts[pageid] = text
            (CACHE_DIR / f"{pageid}.txt").write_text(text, encoding="utf-8")
        print(
            f"metadata pages: {min(batch_number * page_batch_size, len(missing))}/{len(missing)}",
            file=sys.stderr,
        )
        time.sleep(0.15)
    return texts


def clean_wiki_value(value: str) -> str:
    return html.unescape(value.strip())


def get_field(chunk: str, name: str, index: str = "") -> str:
    pattern = re.compile(FIELD_RE_TEMPLATE.format(name=re.escape(name)), re.MULTILINE | re.IGNORECASE)
    values = {match.group(1): clean_wiki_value(match.group(2)) for match in pattern.finditer(chunk)}
    return values.get(index, values.get("", ""))


def normalized_heading(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lstrip("*").casefold())


def nearest_heading(body: str, offset: int) -> str:
    headings = list(re.finditer(r"^(={2,6})\s*(.*?)\s*\1\s*$", body[:offset], re.MULTILINE))
    return headings[-1].group(2).strip() if headings else ""


def is_target_arrangement_heading(value: str) -> bool:
    heading = normalized_heading(value)
    target_instrument = normalized_heading(TARGET_INSTRUMENT)
    if GUITAR_COUNT > 1 and re.fullmatch(rf"{GUITAR_COUNT} guitars?", target_instrument):
        instrument = rf"for {GUITAR_COUNT}(?: \d+[- ]string)? guitars?"
    else:
        instrument = rf"for {re.escape(target_instrument)}"
    match = re.fullmatch(rf"{instrument}(?: \((?P<annotation>[^()]*)\))?", heading)
    if not match:
        return False
    annotation = (match.group("annotation") or "").casefold()
    return not any(token in annotation for token in MIXED_INSTRUMENT_TOKENS)


def work_matches_target_instrumentation(wikitext: str) -> bool:
    match = re.search(
        r"^\|Instrumentation\s*=\s*(.*?)$", wikitext, re.MULTILINE | re.IGNORECASE
    )
    if not match:
        return False
    instrumentation = normalized_heading(match.group(1))
    target = rf"(?<!\w){re.escape(normalized_heading(TARGET_INSTRUMENT))}(?!\w)"
    return bool(re.search(target, instrumentation)) and not any(
        token in instrumentation for token in MIXED_INSTRUMENT_TOKENS
    )


def extract_category_files(wikitext: str) -> list[dict[str, str]]:
    files_marker = wikitext.find("| *****FILES*****")
    work_info_marker = wikitext.find("| *****WORK INFO*****", max(files_marker, 0))
    if files_marker < 0:
        score_wikitext = ""
    else:
        score_wikitext = wikitext[files_marker : work_info_marker if work_info_marker >= 0 else len(wikitext)]
    sections: list[tuple[str, str]] = []
    if CATEGORY_KIND == "original":
        arrangement = re.search(
            r"^={2,6}\s*Arrangements and Transcriptions\s*={2,6}\s*$",
            score_wikitext,
            re.MULTILINE | re.IGNORECASE,
        )
        original_body = score_wikitext[: arrangement.start()] if arrangement else score_wikitext
        if original_body:
            sections.append(("Original work score", original_body))
    else:
        lines = score_wikitext.splitlines()
        index = 0
        while index < len(lines):
            match = HEADING_RE.match(lines[index].strip())
            if not match or not is_target_arrangement_heading(match.group(2)):
                index += 1
                continue
            level = len(match.group(1))
            heading = match.group(2).strip()
            end = index + 1
            while end < len(lines):
                next_heading = HEADING_RE.match(lines[end].strip())
                if next_heading and len(next_heading.group(1)) <= level:
                    break
                end += 1
            sections.append((heading, "\n".join(lines[index + 1 : end])))
            index = end

        # Some category members are themselves target-guitar editions rather
        # than ordinary works with a separate arrangement heading.
        if not sections and work_matches_target_instrumentation(wikitext):
            sections.append(("Work-level target-guitar score", score_wikitext))

    records: list[dict[str, str]] = []
    for heading, body in sections:
        starts = [match.start() for match in FILE_TEMPLATE_RE.finditer(body)]
        for position, start in enumerate(starts):
            stop = starts[position + 1] if position + 1 < len(starts) else len(body)
            chunk = body[start:stop]
            filename_pattern = re.compile(
                FIELD_RE_TEMPLATE.format(name=re.escape("File Name")), re.MULTILINE | re.IGNORECASE
            )
            for match in filename_pattern.finditer(chunk):
                file_index = match.group(1)
                filename = clean_wiki_value(match.group(2))
                if not filename.lower().endswith(".pdf"):
                    continue
                records.append(
                    {
                        "section": heading,
                        "file_heading": nearest_heading(body, start),
                        "filename": filename,
                        "description": get_field(chunk, "File Description", file_index),
                        "arranger": get_field(chunk, "Arranger"),
                        "editor": get_field(chunk, "Editor"),
                        "copyright": get_field(chunk, "Copyright"),
                        "publisher": get_field(chunk, "Publisher Information"),
                    }
                )
    unique: dict[str, dict[str, str]] = {}
    for record in records:
        unique.setdefault(record["filename"], record)
    return list(unique.values())


def file_heading_state(value: str) -> str:
    """Classify a local score heading as target, non-target, or generic."""
    heading = normalized_heading(value)
    if not heading or heading in {
        "scores", "scores and parts", "sheet music", "complete score",
        "music files", "parts", "arrangements and transcriptions",
    }:
        return "generic"
    if any(pattern.search(heading) for _, pattern in NON_TARGET_DESCRIPTION_RE):
        # Compound guitar-family labels still denote the target instrument.
        if re.search(r"(?:harp|lyre|english|spanish|baroque|classical|steel)[ -]?guitar", heading):
            cleaned = re.sub(
                r"(?:harp|lyre|english|spanish|baroque|classical|steel)[ -]?guitar",
                "guitar", heading,
            )
            if not any(pattern.search(cleaned) for _, pattern in NON_TARGET_DESCRIPTION_RE):
                return "target"
        return "non-target"
    if GUITAR_COUNT > 1:
        if re.search(rf"(?<!\w){GUITAR_COUNT}\s+guitars?(?!\w)", heading):
            return "target"
        ensemble_words = {
            2: r"duos?|duets?",
            3: r"trios?",
            4: r"quartets?",
            5: r"quintets?",
            6: r"sextets?",
        }.get(GUITAR_COUNT)
        if ensemble_words and re.search(rf"(?<!\w)(?:{ensemble_words})(?!\w)", heading):
            return "target"
    if MULTI_GUITAR_DESCRIPTION_RE.search(heading):
        return "non-target"
    if re.search(r"(?<!\w)(?:guitar(?:re)?|guitare|gitarre|guitarra|viol[aã]o)(?!\w)", heading):
        return "target"
    return "generic"


def score_file_has_target_evidence(record: dict[str, Any]) -> bool:
    filename = urllib.parse.unquote(str(record.get("filename", "")))
    if filename in TARGET_EVIDENCE_FILENAME_EXCEPTIONS:
        return True
    if file_heading_state(str(record.get("file_heading", ""))) == "target":
        return True
    description = str(record.get("description", "")).strip()
    if re.match(
        r"(?i)^(?:guitar(?:\s+(?:solo|version|part|1))?|lyre\s+or\s+guitar|solo(?:\s|$))",
        description,
    ):
        return True
    evidence = filename
    return bool(re.search(
        r"(?i)(?:guitar\s*(?:solo|version|part|1)|(?:solo|for)[ _-]*guitar|gitarre|guitarra|viol[aã]o|(?:^|[_-])1g(?:[_+.-]|$))",
        evidence,
    ))


def work_instrumentation_is_mixed(value: str) -> bool:
    instrumentation = html.unescape(value).casefold()
    instrumentation = re.sub(r"''|<br\s*/?>|\{\{.*?\}\}", " ", instrumentation)
    instrumentation = re.sub(r"\s+", " ", instrumentation).strip()
    if not instrumentation:
        return False
    if re.search(r"\b(?:various|see individual|see above)\b", instrumentation):
        return True
    if re.search(
        r"(?<!\w)(?:1\s*(?:or|-)\s*2|2nd)(?:\s+guitars?)?(?!\w)|more\s+guitars?",
        instrumentation,
    ):
        return True
    guitar_counts = {
        int(match.group(1))
        for match in re.finditer(r"(?<!\w)(\d+)\s+guitars?(?!\w)", instrumentation)
    }
    if any(count != GUITAR_COUNT for count in guitar_counts):
        return True
    if instrumentation.startswith("|tags="):
        codes = set(re.findall(r"(?<!\w)(?:vn|va|vc|db|hp|lute|lyre|pf|org|fl|ob|cl|bn)(?!\w)", instrumentation))
        if codes:
            return True

    cleaned = re.sub(
        r"(?:harp[ -]?guitar|lyre[ -]?guitar|harp[ -]?lute\s*\(type of guitar\)|"
        r"english guitar|spanish guitar|baroque guitar|classical guitar|steel guitar)",
        "guitar",
        instrumentation,
    )
    other_matches = [
        pattern.search(cleaned) for _, pattern in NON_TARGET_DESCRIPTION_RE
    ]
    if not any(other_matches):
        return False
    # A single-staff alternative such as "cello (or guitar)" is still a
    # usable guitar score; combinations, optional parts and collections are
    # treated as mixed.
    if (
        re.search(r"(?<!\w)(?:guitar(?:re)?|guitare|gitarre|guitarra)(?!\w)", cleaned)
        and re.search(r"(?<!\w)or(?!\w)", cleaned)
        and not re.search(r"[,;+]|\band\b|\bwith\b|ad\s+lib|voice|vocal", cleaned)
    ):
        return False
    return True


def score_file_exclusion_reason(record: dict[str, Any]) -> str:
    """Return file-level non-target instrumentation evidence, if any."""
    # Exact arrangement headings already identify the target guitar version.
    # Their file descriptions and names often mention the source instrument
    # (for example, a cello suite or lute notes), so do not reject those.
    # The work-level fallback is heterogeneous, however, and can contain an
    # explicitly labelled violin/piano sibling next to the guitar original;
    # apply the same file-level checks to that fallback only.
    if CATEGORY_KIND == "arrangement" and record.get("section") not in WORK_LEVEL_TARGET_SECTIONS:
        return ""
    filename = urllib.parse.unquote(str(record.get("filename", "")))
    if filename in MANUAL_NON_TARGET_FILENAMES:
        return "人工确认该 PDF 为其他乐器或混合编制合集"
    heading_state = file_heading_state(str(record.get("file_heading", "")))
    if heading_state == "non-target":
        return f"文件所在小节不是目标吉他编制：{record.get('file_heading', '')}"
    if heading_state == "target":
        return ""
    description = html.unescape(str(record.get("description", ""))).strip()
    mixed_work = GUITAR_COUNT == 1 and work_instrumentation_is_mixed(
        str(record.get("work_instrumentation", ""))
    )
    if mixed_work and MULTI_GUITAR_DESCRIPTION_RE.search(description):
        return f"文件说明为多吉他编制：{MULTI_GUITAR_DESCRIPTION_RE.search(description).group(0)}"
    matches = [] if filename in NON_TARGET_DESCRIPTION_EXCEPTIONS else [
        label for label, pattern in NON_TARGET_DESCRIPTION_RE if pattern.search(description)
    ]
    if (
        matches
        and set(matches) <= {"ensemble", "orchestra"}
        and re.search(r"(?i)guitar|gitarre|guitare|guitarra", description)
    ):
        matches = []
    if matches:
        return "文件说明含其他或混合乐器：" + ", ".join(sorted(set(matches)))

    if mixed_work and MULTI_GUITAR_FILENAME_RE.search(filename):
        return "文件名标记为多吉他、合奏或其他乐器版本"
    match = None if filename in NON_TARGET_FILENAME_EXCEPTIONS else NON_TARGET_FILENAME_TAG_RE.search(filename)
    if match:
        instrument = next(value for value in match.groupdict().values() if value)
        if (
            instrument.casefold() == "duett"
            and GUITAR_COUNT == 2
            and re.search(r"(?i)gitarren|guitars?|guitares?|guitarras?", filename)
        ):
            return ""
        if (
            instrument.casefold() == "trio"
            and GUITAR_COUNT == 3
            and re.search(r"(?i)gitarren|guitars?|guitares?|guitarras?", filename)
        ):
            return ""
        return f"原作分类文件名标记为其他乐器版本：{instrument}"
    return ""


def quarantine_excluded_files(records: list[dict[str, Any]]) -> int:
    """Move already-downloaded excluded files out of the active score tree."""
    moved = 0
    for record in records:
        source = ROOT / record["relative_path"]
        if not source.is_file():
            continue
        relative = Path(record["relative_path"])
        try:
            relative = relative.relative_to("scores")
        except ValueError:
            relative = Path(safe_component(record["composer"])) / safe_component(record["filename"], 180)
        destination = QUARANTINE_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            stem = destination.stem
            suffix = destination.suffix
            counter = 2
            while destination.exists():
                destination = destination.with_name(f"{stem}__{counter}{suffix}")
                counter += 1
        shutil.move(str(source), str(destination))
        record["quarantine_path"] = destination.relative_to(ROOT).as_posix()
        moved += 1
    return moved


def quarantine_revision_mismatches() -> int:
    """Move parseable but metadata-mismatched PDFs out of the active tree."""
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    records_by_physical_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in manifest:
        records_by_physical_path[filesystem_path_key(record["relative_path"])].append(record)
    moved = 0
    for record in manifest:
        source = ROOT / record["relative_path"]
        if not source.is_file() or is_valid_pdf(source, record_expected_size(record)):
            continue
        siblings = records_by_physical_path[filesystem_path_key(record["relative_path"])]
        if any(
            sibling is not record
            and is_valid_pdf(source, record_expected_size(sibling))
            and (
                not record_expected_sha1(sibling)
                or file_sha1(source) == record_expected_sha1(sibling)
            )
            for sibling in siblings
        ):
            # On case-insensitive volumes, two IMSLP records that differ only
            # by filename case share one physical path. Keep bytes that exactly
            # belong to the valid sibling instead of quarantining them for the
            # obsolete alias.
            continue
        if not is_parseable_pdf(source):
            continue
        relative = Path(record["relative_path"])
        try:
            relative = relative.relative_to("scores")
        except ValueError:
            relative = Path(safe_component(record["composer"])) / safe_component(record["filename"], 180)
        destination = REVISION_QUARANTINE_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(
                f"{destination.stem}-{int(time.time())}{destination.suffix}"
            )
        shutil.move(str(source), str(destination))
        moved += 1
    print(f"quarantined revision mismatches: {moved}", file=sys.stderr)
    return moved


def recover_revision_aliases() -> int:
    """Copy quarantined content to an exact same-work manifest target.

    IMSLP occasionally replaces a file while retaining an older filename in
    page metadata. A mirror may then serve the replacement under the obsolete
    URL. The obsolete record must remain unresolved, but the downloaded bytes
    can safely satisfy another record from the same work when both its expected
    size and SHA-1 match exactly. The quarantine copy is retained as evidence.
    """
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    records_by_path = {record["relative_path"]: record for record in manifest}
    recovered: list[dict[str, Any]] = []
    if not REVISION_QUARANTINE_DIR.is_dir():
        write_json_atomic(REVISION_ALIAS_RECOVERY_JSON, {
            "category_name": CATEGORY_NAME,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "recovered_count": 0,
            "records": [],
        })
        return 0

    for source in sorted(REVISION_QUARANTINE_DIR.rglob("*.pdf")):
        if not is_parseable_pdf(source):
            continue
        quarantine_relative = source.relative_to(REVISION_QUARANTINE_DIR)
        original_path = (Path("scores") / quarantine_relative).as_posix()
        original_record = records_by_path.get(original_path)
        if not original_record:
            continue
        observed_size = source.stat().st_size
        observed_sha1 = file_sha1(source)
        candidates = [
            record for record in manifest
            if record["work_id"] == original_record["work_id"]
            and record["relative_path"] != original_path
            and record_expected_size(record) == observed_size
            and record_expected_sha1(record) == observed_sha1
            and not (ROOT / record["relative_path"]).exists()
        ]
        if len(candidates) != 1:
            continue
        target = candidates[0]
        destination = ROOT / target["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if not is_valid_pdf(destination, record_expected_size(target)):
            destination.unlink(missing_ok=True)
            continue
        recovered.append({
            "source": source.relative_to(ROOT).as_posix(),
            "target": target["relative_path"],
            "work_id": target["work_id"],
            "size": observed_size,
            "sha1": observed_sha1,
        })

    write_json_atomic(REVISION_ALIAS_RECOVERY_JSON, {
        "category_name": CATEGORY_NAME,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "recovered_count": len(recovered),
        "records": recovered,
    })
    print(f"recovered exact revision aliases: {len(recovered)}", file=sys.stderr)
    return len(recovered)


def restore_active_quarantine_files() -> int:
    """Restore exact active-manifest PDFs that were previously quarantined."""
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    restored: list[dict[str, Any]] = []
    for record in manifest:
        destination = ROOT / record["relative_path"]
        if destination.exists():
            continue
        relative = Path(record["relative_path"])
        try:
            relative = relative.relative_to("scores")
        except ValueError:
            continue
        expected = QUARANTINE_DIR / relative
        candidates = [expected]
        if expected.parent.is_dir():
            candidates.extend(sorted(expected.parent.glob(f"{expected.stem}__*{expected.suffix}")))
        exact = [
            candidate for candidate in dict.fromkeys(candidates)
            if is_valid_pdf(candidate, record_expected_size(record))
            and (
                not record_expected_sha1(record)
                or file_sha1(candidate) == record_expected_sha1(record)
            )
        ]
        if len(exact) != 1:
            continue
        source = exact[0]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        restored.append({
            "source": source.relative_to(ROOT).as_posix(),
            "target": record["relative_path"],
            "work_id": record["work_id"],
        })

    write_json_atomic(ACTIVE_QUARANTINE_RECOVERY_JSON, {
        "category_name": CATEGORY_NAME,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "restored_count": len(restored),
        "records": restored,
    })
    print(f"restored active files from quarantine: {len(restored)}", file=sys.stderr)
    return len(restored)


def parse_work_title(page_title: str) -> tuple[str, str]:
    match = re.match(r"^(.*) \(([^()]*)\)$", page_title)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return page_title.strip(), "Unknown"


def safe_component(value: str, limit: int = 110) -> str:
    value = re.sub(r"[\x00-\x1f/:]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if len(value) <= limit:
        return value or "Untitled"
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return f"{value[: limit - 10].rstrip()}_{digest}"


def filesystem_path_key(value: str) -> str:
    """Normalize a relative path for case-insensitive macOS/exFAT volumes."""
    return unicodedata.normalize("NFC", value).casefold()


def page_url(page_title: str) -> str:
    return "https://imslp.org/wiki/" + urllib.parse.quote(page_title.replace(" ", "_"), safe="_(),'-.~")


def resolve_image_info(filenames: list[str]) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    file_batch_size = 50
    for batch_number, batch in enumerate(batched(filenames, file_batch_size), start=1):
        titles = "|".join(f"File:{name}" for name in batch)
        payload = api_get(
            {
                "action": "query",
                "prop": "imageinfo",
                "titles": titles,
                "iiprop": "url|size|mime|sha1",
            }
        )
        normalized_to_requested = {
            item.get("to", "").removeprefix("File:"): item.get("from", "").removeprefix("File:")
            for item in payload.get("query", {}).get("normalized", [])
        }
        for page in payload.get("query", {}).get("pages", {}).values():
            title = page.get("title", "")
            normalized_name = title.removeprefix("File:")
            name = normalized_to_requested.get(normalized_name, normalized_name)
            info_list = page.get("imageinfo") or []
            if not info_list:
                resolved[name] = {"missing": True}
                continue
            info = dict(info_list[0])
            url = info.get("url", "")
            if url.startswith("//"):
                url = "https:" + url
            info["url"] = url
            resolved[name] = info
        print(
            f"file metadata: {min(batch_number * file_batch_size, len(filenames))}/{len(filenames)}",
            file=sys.stderr,
        )
        time.sleep(0.12)
    return resolved


def normalized_filename_key(filename: str) -> str:
    return urllib.parse.unquote(html.unescape(filename)).replace("_", " ").casefold()


def fetch_file_ids_for_work(work: dict[str, Any], retries: int = 4) -> dict[str, str]:
    request = urllib.request.Request(
        work["imslp_url"],
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    page_html = ""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                page_html = response.read().decode("utf-8", errors="replace")
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt + 1 == retries:
                raise RuntimeError(f"work page failed: {work['page_title']}: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))

    pattern = re.compile(
        r'<div id="IMSLP(\d+)"[^>]*>.*?<span class="hidden"><a href="/images/([^"?#]+)"',
        re.DOTALL,
    )
    result: dict[str, str] = {}
    for match in pattern.finditer(page_html):
        basename = urllib.parse.unquote(html.unescape(match.group(2))).rsplit("/", 1)[-1]
        result[normalized_filename_key(basename)] = match.group(1)
    return result


def static_download_url(record: dict[str, Any], file_id: str) -> str:
    parsed = urllib.parse.urlparse(record["download_url"])
    marker = "/images/"
    if marker not in parsed.path:
        return ""
    relative = parsed.path.split(marker, 1)[1]
    directory, basename = relative.rsplit("/", 1)
    directory = "/".join(urllib.parse.quote(urllib.parse.unquote(part), safe="._-~") for part in directory.split("/"))
    basename = urllib.parse.quote(urllib.parse.unquote(basename), safe="._-(),'~")
    return f"https://s9.cn.imslp.org/files/imglnks/usimg/{directory}/IMSLP{file_id}-{basename}"


def resolve_file_ids(refresh: bool = False, workers: int = 1) -> list[dict[str, Any]]:
    if not CATALOG_JSON.exists() or not MANIFEST_JSON.exists():
        raise RuntimeError("Run metadata first")
    works = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    records_by_work: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in manifest:
        records_by_work[record["work_id"]].append(record)

    pending = [
        work for work in works
        if records_by_work.get(work["work_id"])
        and (refresh or any(not record.get("file_id") for record in records_by_work[work["work_id"]]))
    ]
    completed = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_file_ids_for_work, work): work for work in pending}
        for future in as_completed(futures):
            work = futures[future]
            try:
                file_ids = future.result()
            except Exception as exc:
                failures.append(f"{work['page_title']}: {exc}")
                print(f"file id warning: {failures[-1]}", file=sys.stderr)
                completed += 1
                continue
            for record in records_by_work[work["work_id"]]:
                file_id = file_ids.get(normalized_filename_key(record["filename"]))
                if file_id:
                    record["file_id"] = file_id
                    record["verified_download_url"] = static_download_url(record, file_id)
            completed += 1
            if completed % 100 == 0:
                write_json_atomic(MANIFEST_JSON, manifest)
            if completed % 20 == 0 or completed == len(pending):
                print(f"file ids: {completed}/{len(pending)} work pages", file=sys.stderr)

    manifest.sort(key=lambda item: (item["composer"].casefold(), item["title_en"].casefold(), item["filename"].casefold()))
    write_json_atomic(MANIFEST_JSON, manifest)
    write_csv_outputs(works, manifest)
    missing = [record for record in manifest if not record.get("file_id")]
    print(f"file ids resolved: {len(manifest) - len(missing)}/{len(manifest)}", file=sys.stderr)
    if failures:
        print(f"file id work-page warnings: {len(failures)}", file=sys.stderr)
    return missing


def translate_title_fallback(title: str) -> str:
    if title in KNOWN_TITLES_ZH:
        return KNOWN_TITLES_ZH[title]

    translated = title
    changed = False
    for english, chinese in FORM_TERMS:
        pattern = rf"\b{re.escape(english)}\b"
        if re.search(pattern, translated, re.IGNORECASE):
            translated = re.sub(pattern, chinese, translated, count=1, flags=re.IGNORECASE)
            changed = True
            break

    key_pattern = re.compile(r"\bin\s+([A-G](?:-flat|-sharp)?)\s+(major|minor)\b", re.IGNORECASE)
    key_match = key_pattern.search(translated)
    if key_match:
        key = KEY_ZH.get(key_match.group(1).replace("flat", "flat").replace("sharp", "sharp"), key_match.group(1))
        mode = "大调" if key_match.group(2).lower() == "major" else "小调"
        translated = key_pattern.sub(f"{key}{mode}", translated, count=1)
        changed = True

    translated = re.sub(r"\bNo\.(\d+)", r"第\1号", translated)
    if GUITAR_COUNT == 1:
        translated = re.sub(
            r"\bfor (?:1 )?Guitar\b", "为吉他而作", translated, flags=re.IGNORECASE
        )
    else:
        translated = re.sub(
            rf"\bfor {GUITAR_COUNT} Guitars?\b",
            f"为{GUITAR_COUNT}把吉他而作",
            translated,
            flags=re.IGNORECASE,
        )
    translated = re.sub(r"\bfor 3 Violins\b", "为三把小提琴而作", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bfor 3 Viols\b", "为三把维奥尔琴而作", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bfor Organ\b", "为管风琴而作", translated, flags=re.IGNORECASE)
    if translated != title:
        changed = True
    return translated if changed else f"暂无通行中译（原题：{title}）"


def load_translations() -> dict[str, str]:
    if not TRANSLATIONS_JSON.exists():
        return {}
    return json.loads(TRANSLATIONS_JSON.read_text(encoding="utf-8"))


def load_composer_translations() -> dict[str, str]:
    if not COMPOSER_TRANSLATIONS_JSON.exists():
        return {}
    return json.loads(COMPOSER_TRANSLATIONS_JSON.read_text(encoding="utf-8"))


def apply_composer_translations(
    works: list[dict[str, Any]], manifest: list[dict[str, Any]]
) -> None:
    translations = load_composer_translations()
    for record in [*works, *manifest]:
        composer = record["composer"]
        record["composer_zh"] = translations.get(composer, f"暂无中译（原名：{composer}）")


def build_metadata(refresh: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ensure_dirs()
    members = get_category_members(refresh=refresh)
    texts = get_page_wikitext(members, refresh=refresh)

    works: list[dict[str, Any]] = []
    raw_files: list[dict[str, Any]] = []
    excluded_files: list[dict[str, Any]] = []
    for member in members:
        pageid = int(member["pageid"])
        title_en, composer = parse_work_title(member["title"])
        work_id = str(pageid)
        work_dir = Path("scores") / safe_component(composer) / safe_component(title_en)
        wikitext = texts.get(pageid, "")
        instrumentation_match = re.search(
            r"^\|Instrumentation\s*=\s*(.*?)$", wikitext, re.MULTILINE | re.IGNORECASE
        )
        work_instrumentation = (
            clean_wiki_value(instrumentation_match.group(1)) if instrumentation_match else ""
        )
        extracted = extract_category_files(wikitext)
        for record in extracted:
            record["work_instrumentation"] = work_instrumentation
        base_reasons = {
            record["filename"]: score_file_exclusion_reason(record) for record in extracted
        }
        has_explicit_non_target_file = any(base_reasons.values())
        mixed_work = (
            CATEGORY_KIND == "original"
            and GUITAR_COUNT == 1
            and work_instrumentation_is_mixed(work_instrumentation)
        )
        selected: list[dict[str, str]] = []
        excluded_for_work: list[dict[str, Any]] = []
        for record in extracted:
            reason = base_reasons[record["filename"]]
            if (
                not reason and mixed_work
                and not score_file_has_target_evidence(record)
                and not has_explicit_non_target_file
            ):
                reason = f"作品编制含其他乐器且该 PDF 未标明为纯目标吉他版本：{work_instrumentation}"
            if reason:
                excluded_record = {
                    **record,
                    "work_id": work_id,
                    "page_title": member["title"],
                    "title_en": title_en,
                    "composer": composer,
                    "imslp_url": page_url(member["title"]),
                    "relative_path": (work_dir / safe_component(record["filename"], 180)).as_posix(),
                    "exclusion_reason": reason,
                }
                excluded_for_work.append(excluded_record)
                excluded_files.append(excluded_record)
            else:
                selected.append(record)
        work = {
            "work_id": work_id,
            "pageid": pageid,
            "page_title": member["title"],
            "title_en": title_en,
            "composer": composer,
            "imslp_url": page_url(member["title"]),
            "instrumentation": work_instrumentation,
            "relative_directory": work_dir.as_posix(),
            "score_file_count": len(selected),
            "excluded_non_target_file_count": len(excluded_for_work),
        }
        if member["title"] == "Collected Transcriptions, 1996-2018 (Yates, Richard)":
            work["exclusion_reason"] = "页面只提供含独奏、二重奏、三重奏和四重奏的混合编制合集；按要求不下载。"
        elif extracted and not selected:
            work["exclusion_reason"] = "该页识别到的 PDF 均为其他乐器或混合编制，已排除。"
        works.append(work)
        for record in selected:
            raw_files.append(
                {
                    **record,
                    "work_id": work_id,
                    "page_title": member["title"],
                    "title_en": title_en,
                    "composer": composer,
                    "imslp_url": work["imslp_url"],
                    "relative_path": (work_dir / safe_component(record["filename"], 180)).as_posix(),
                }
            )

    quarantined_count = quarantine_excluded_files(excluded_files)
    write_json_atomic(EXCLUDED_NON_TARGET_JSON, {
        "category_name": CATEGORY_NAME,
        "target_instrument": TARGET_INSTRUMENT,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "excluded_count": len(excluded_files),
        "quarantined_local_file_count": quarantined_count,
        "records": excluded_files,
    })
    if excluded_files:
        print(
            f"file-level purity filter: excluded={len(excluded_files)}, "
            f"quarantined_downloads={quarantined_count}",
            file=sys.stderr,
        )

    requested_filenames = sorted({record["filename"] for record in raw_files}, key=str.casefold)
    image_info: dict[str, dict[str, Any]] = {}
    cached_records: dict[tuple[str, str], dict[str, Any]] = {}
    if MANIFEST_JSON.exists() and not refresh:
        for cached in json.loads(MANIFEST_JSON.read_text(encoding="utf-8")):
            cached_records[(cached.get("work_id", ""), cached.get("filename", ""))] = cached
            if cached.get("download_url") and cached.get("expected_size"):
                image_info[cached["filename"]] = {
                    "url": cached["download_url"],
                    "size": cached["expected_size"],
                    "mime": cached.get("mime", ""),
                    "sha1": cached.get("sha1_imslp", ""),
                }
    unresolved = [filename for filename in requested_filenames if filename not in image_info]
    image_info.update(resolve_image_info(unresolved))
    manifest: list[dict[str, Any]] = []
    download_overrides = (
        json.loads(DOWNLOAD_OVERRIDES_JSON.read_text(encoding="utf-8"))
        if DOWNLOAD_OVERRIDES_JSON.exists() else {}
    )
    for record in raw_files:
        info = image_info.get(record["filename"], {"missing": True})
        cached = cached_records.get((record["work_id"], record["filename"]), {})
        manifest_record = {
                **record,
                "download_url": info.get("url", ""),
                "expected_size": info.get("size"),
                "mime": info.get("mime", ""),
                "sha1_imslp": info.get("sha1", ""),
                "metadata_missing": bool(info.get("missing")),
            }
        if cached.get("file_id"):
            manifest_record["file_id"] = cached["file_id"]
            manifest_record["verified_download_url"] = cached.get("verified_download_url", "")
        override = download_overrides.get(record["filename"])
        if override:
            manifest_record.update(override)
        manifest.append(manifest_record)

    translations = load_translations()
    for work in works:
        work["title_zh"] = translations.get(work["title_en"], translate_title_fallback(work["title_en"]))
    apply_composer_translations(works, manifest)

    works.sort(key=lambda item: (item["composer"].casefold(), item["title_en"].casefold()))
    manifest.sort(key=lambda item: (item["composer"].casefold(), item["title_en"].casefold(), item["filename"].casefold()))
    write_json_atomic(CATALOG_JSON, works)
    write_json_atomic(MANIFEST_JSON, manifest)
    write_csv_outputs(works, manifest)
    return works, manifest


def write_csv_outputs(works: list[dict[str, Any]], manifest: list[dict[str, Any]]) -> None:
    work_files: dict[str, list[str]] = defaultdict(list)
    for record in manifest:
        work_files[record["work_id"]].append(record["relative_path"])
    with CATALOG_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "composer", "composer_zh", "title_en", "title_zh", "imslp_url", "score_file_count",
            "relative_directory", "score_paths",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for work in works:
            row = {name: work.get(name, "") for name in fieldnames}
            row["score_paths"] = " | ".join(work_files.get(work["work_id"], []))
            writer.writerow(row)

    with MANIFEST_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "composer", "composer_zh", "title_en", "filename", "description", "section", "copyright",
            "file_id", "verified_download_url", "download_url", "expected_size", "sha1_imslp",
            "download_expected_size", "download_sha1", "source_note", "relative_path", "imslp_url",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manifest)


def is_valid_pdf(path: Path, expected_size: int | None = None) -> bool:
    if not path.is_file() or path.stat().st_size < 5:
        return False
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            return False
    if expected_size and path.stat().st_size != expected_size:
        return False
    return True


def record_expected_size(record: dict[str, Any]) -> int | None:
    return record.get("download_expected_size") or record.get("expected_size")


def record_expected_sha1(record: dict[str, Any]) -> str:
    return record.get("download_sha1") or record.get("sha1_imslp", "")


def file_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download_url_candidates(record: dict[str, Any]) -> list[str]:
    """Return equivalent IMSLP mirrors in deterministic fallback order."""
    primary = record.get("verified_download_url") or record.get("download_url", "")
    candidates = [primary] if primary else []
    parsed = urllib.parse.urlparse(primary)
    match = re.search(r"/files/imglnks/(?:usimg|euimg|caimg)/(.*)$", parsed.path)
    if match:
        relative = match.group(1)
        candidates.extend([
            f"https://vmirror.imslp.org/files/imglnks/usimg/{relative}",
            f"https://imslp.eu/files/imglnks/euimg/{relative}",
            f"https://petruccimusiclibrary.ca/files/imglnks/caimg/{relative}",
        ])
    direct = record.get("download_url", "")
    if direct:
        candidates.append(direct)
    return list(dict.fromkeys(url for url in candidates if url))


STATIC_MIRROR_HOSTS = {
    "vmirror.imslp.org",
    "imslp.eu",
    "petruccimusiclibrary.ca",
}


def is_parseable_pdf(path: Path) -> bool:
    """Reject truncated mirror responses before adopting an older revision."""
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        result = subprocess.run(
            [pdfinfo, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 4096))
        return b"%%EOF" in handle.read()


def download_one(
    record: dict[str, Any],
    bot_token: str,
    accept_mirror_revision: bool = False,
) -> dict[str, Any]:
    destination = ROOT / record["relative_path"]
    expected_size = record_expected_size(record)
    expected_sha1 = record_expected_sha1(record)
    status = ""
    detail = ""
    observed_size: int | None = None
    observed_sha1 = ""
    source_url = ""
    if is_valid_pdf(destination, expected_size):
        status = "already_downloaded"
    else:
        download_urls = download_url_candidates(record)
        if not download_urls:
            status = "missing_download_url"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial = destination.with_suffix(destination.suffix + ".part")
            for candidate_number, download_url in enumerate(download_urls):
                headers = {
                    "User-Agent": USER_AGENT,
                    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
                    "Accept-Encoding": "identity",
                    "Referer": record["imslp_url"],
                }
                if bot_token and "/images/" in urllib.parse.urlparse(download_url).path:
                    headers["Cookie"] = f"BOT_DETECT_CLEARED={bot_token}"
                for attempt in range(3):
                    request = urllib.request.Request(download_url, headers=headers)
                    try:
                        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as handle:
                            first = response.read(8192)
                            if not first.startswith(b"%PDF-"):
                                page_text = first.decode("utf-8", errors="ignore")
                                if "Bot Check" in page_text or "verify you are human" in page_text:
                                    status = "blocked_bot_check"
                                    detail = "IMSLP requires interactive human verification"
                                else:
                                    status = "invalid_response"
                                    detail = response.headers.get("Content-Type", "")
                            else:
                                handle.write(first)
                                while True:
                                    chunk = response.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    handle.write(chunk)
                                # Validate the complete file, not Python's
                                # still-buffered on-disk prefix.
                                handle.flush()
                                observed_size = partial.stat().st_size
                                observed_sha1 = file_sha1(partial)
                                size_mismatch = bool(expected_size and observed_size != expected_size)
                                sha1_mismatch = bool(expected_sha1 and observed_sha1 != expected_sha1)
                                candidate_host = urllib.parse.urlparse(download_url).netloc.casefold()
                                if (
                                    accept_mirror_revision
                                    and candidate_host in STATIC_MIRROR_HOSTS
                                    and (size_mismatch or sha1_mismatch)
                                    and is_parseable_pdf(partial)
                                ):
                                    partial.replace(destination)
                                    status = "downloaded_mirror_revision"
                                    source_url = download_url
                                    detail = (
                                        f"official IMSLP mirror revision adopted; API size/SHA-1 "
                                        f"{expected_size}/{expected_sha1 or '-'}, mirror size/SHA-1 "
                                        f"{observed_size}/{observed_sha1}"
                                    )
                                elif size_mismatch:
                                    status = "size_mismatch"
                                    detail = f"expected {expected_size}, got {observed_size}"
                                elif sha1_mismatch:
                                    status = "sha1_mismatch"
                                    detail = "downloaded SHA-1 does not match IMSLP metadata"
                                else:
                                    partial.replace(destination)
                                    status = "downloaded"
                                    source_url = download_url
                                    if candidate_number:
                                        detail = f"fallback mirror: {urllib.parse.urlparse(download_url).netloc}"
                        if status in {"downloaded", "downloaded_mirror_revision", "blocked_bot_check", "invalid_response", "size_mismatch", "sha1_mismatch"}:
                            break
                    except urllib.error.HTTPError as exc:
                        status = "http_error"
                        detail = f"HTTP {exc.code}"
                        if exc.code not in {429, 500, 502, 503, 504}:
                            break
                    except Exception as exc:  # keep the batch resumable
                        status = "error"
                        detail = str(exc)
                    finally:
                        if partial.exists():
                            partial.unlink()
                    if attempt < 2:
                        time.sleep(1.0 * (attempt + 1))
                if status in {"downloaded", "downloaded_mirror_revision", "blocked_bot_check"}:
                    break

    return {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "composer": record["composer"],
        "title_en": record["title_en"],
        "filename": record["filename"],
        "relative_path": record["relative_path"],
        "status": status,
        "detail": detail,
        "observed_size": observed_size or "",
        "observed_sha1": observed_sha1,
        "source_url": source_url,
    }


def download_scores(
    limit: int | None = None,
    delay: float = 0.0,
    workers: int = 1,
    accept_mirror_revision: bool = False,
    retry_failures: bool = False,
) -> list[dict[str, Any]]:
    if not MANIFEST_JSON.exists():
        raise RuntimeError("Run metadata first")
    all_manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    manifest = all_manifest
    if retry_failures:
        if not DOWNLOAD_LOG.exists():
            raise RuntimeError("No previous download log is available for --retry-failures")
        with DOWNLOAD_LOG.open(encoding="utf-8-sig", newline="") as handle:
            previous_rows = list(csv.DictReader(handle))
        successful = {"already_downloaded", "downloaded", "downloaded_mirror_revision"}
        failed_filenames = {
            row["filename"] for row in previous_rows if row.get("status") not in successful
        }
        manifest = [record for record in manifest if record["filename"] in failed_filenames]
    if limit is not None:
        manifest = manifest[:limit]
    bot_token = os.environ.get("IMSLP_BOT_TOKEN", "").strip()
    statuses: list[dict[str, Any]] = []

    with DOWNLOAD_LOG.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "timestamp", "composer", "title_en", "filename", "relative_path",
            "status", "detail", "observed_size", "observed_sha1", "source_url",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        handle.flush()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            for number, status_row in enumerate(
                executor.map(
                    lambda record: download_one(record, bot_token, accept_mirror_revision),
                    manifest,
                ),
                start=1,
            ):
                statuses.append(status_row)
                writer.writerow(status_row)
                handle.flush()
                print(
                    f"download {number}/{len(manifest)}: {status_row['status']}: {status_row['filename']}",
                    file=sys.stderr,
                )
                if delay:
                    time.sleep(delay)
    adopted = {
        row["filename"]: row
        for row in statuses
        if row["status"] == "downloaded_mirror_revision"
    }
    if adopted:
        overrides = (
            json.loads(DOWNLOAD_OVERRIDES_JSON.read_text(encoding="utf-8"))
            if DOWNLOAD_OVERRIDES_JSON.exists() else {}
        )
        for filename, row in adopted.items():
            override = {
                "verified_download_url": row["source_url"],
                "download_expected_size": int(row["observed_size"]),
                "download_sha1": row["observed_sha1"],
                "source_note": row["detail"],
            }
            overrides[filename] = {**overrides.get(filename, {}), **override}
        write_json_atomic(DOWNLOAD_OVERRIDES_JSON, overrides)
        for record in all_manifest:
            if record["filename"] in adopted:
                record.update(overrides[record["filename"]])
        write_json_atomic(MANIFEST_JSON, all_manifest)
        works = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
        write_csv_outputs(works, all_manifest)
    return statuses


def markdown_link(path: str, label: str) -> str:
    quoted = urllib.parse.quote(path, safe="/._-~")
    return f"[{label}]({quoted})"


def render_readme() -> None:
    if not CATALOG_JSON.exists() or not MANIFEST_JSON.exists():
        raise RuntimeError("Run metadata first")
    works = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    translations = load_translations()
    for work in works:
        work["title_zh"] = translations.get(work["title_en"], translate_title_fallback(work["title_en"]))
    apply_composer_translations(works, manifest)
    CATALOG_JSON.write_text(json.dumps(works, ensure_ascii=False, indent=2), encoding="utf-8")
    MANIFEST_JSON.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv_outputs(works, manifest)
    files_by_work: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in manifest:
        files_by_work[record["work_id"]].append(record)

    # External-volume metadata reads are comparatively expensive. Validate
    # every score once and reuse the result while rendering both catalogs.
    valid_local_paths = {
        record["relative_path"]
        for record in manifest
        if is_valid_pdf(ROOT / record["relative_path"], record_expected_size(record))
    }
    downloaded_files = len(valid_local_paths)
    works_with_entries = sum(1 for work in works if work["score_file_count"] > 0)
    composers = sorted({work["composer"] for work in works}, key=str.casefold)
    composer_zh_by_en = {work["composer"]: work["composer_zh"] for work in works}

    lines = [
        f"# {LIBRARY_TITLE}",
        "",
        f"> 数据源：[IMSLP – {CATEGORY_NAME}]({CATEGORY_URL})  ",
        f"> 目录生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}  ",
        f"> 作品页：{len(works)}；作曲家/署名组：{len(composers)}；识别到{PDF_KIND_LABEL}：{len(manifest)}；本地有效 PDF：{downloaded_files}",
        "",
        (
            f"本目录对应 IMSLP 的 **{CATEGORY_NAME}** 分类，仅收录目标吉他编制的 PDF，"
            "不会把同一作品页上的钢琴谱或其他乐器编制误收进来。音乐家和作品的中文名均为检索用参考译名；"
            "没有可靠通行译名时保留英文原名。"
        ),
        "",
        "## 使用说明",
        "",
        "- 点击“IMSLP”查看原作品页。",
        "- 已成功下载的文件显示为可点击的本地 PDF；未下载项显示“待下载”。",
        "- `catalog.csv` 是作品级目录，`score_manifest.csv` 是文件级清单。",
        "- IMSLP 可能要求交互式人机验证，下载工具拒绝把 HTML 验证页伪装成 PDF。",
        "",
        "## 完整目录（按作曲家）",
        "",
    ]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for work in works:
        grouped[work["composer"]].append(work)

    for composer in composers:
        lines.extend([f"### {composer}｜{composer_zh_by_en[composer]}", ""])
        for work in grouped[composer]:
            local_links: list[str] = []
            for record in files_by_work.get(work["work_id"], []):
                if record["relative_path"] in valid_local_paths:
                    label = record.get("description") or record["filename"]
                    local_links.append(markdown_link(record["relative_path"], label))
            score_text = "；".join(local_links) if local_links else (
                "待下载" if work["score_file_count"] else work.get("exclusion_reason", "未识别到目标吉他 PDF")
            )
            lines.extend(
                [
                    f"- **英文名：** {work['title_en']}",
                    f"  - **中文名：** {work['title_zh']}",
                    f"  - **来源：** [IMSLP]({work['imslp_url']})",
                    f"  - **乐谱：** {score_text}",
                    "",
                ]
            )

    (ROOT / "README.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    (ROOT / "乐谱库目录.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    html_parts = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(LIBRARY_TITLE)}</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:1180px;margin:0 auto;padding:32px 24px;color:#202124;background:#faf9f6}",
        "h1{margin-bottom:.25rem} .meta{color:#5f6368;margin-bottom:1.5rem}",
        "#search{width:100%;box-sizing:border-box;padding:12px 14px;font-size:16px;border:1px solid #c8c8c8;border-radius:8px;background:white;position:sticky;top:8px;z-index:2}",
        "details{background:white;border:1px solid #e2e0dc;border-radius:10px;margin:14px 0;padding:8px 14px}",
        "summary{font-size:20px;font-weight:650;cursor:pointer;padding:8px 0}",
        ".work{padding:11px 4px;border-top:1px solid #eceae6}.en{font-weight:650}.zh{color:#3c4043;margin-top:3px}",
        ".links{margin-top:6px;font-size:14px}a{color:#1457a6;text-decoration:none}a:hover{text-decoration:underline}.pending{color:#9a6700}",
        "</style></head><body>",
        f"<h1>{html.escape(LIBRARY_TITLE)}</h1>",
        f'<div class="meta">作品 {len(works)} · 作曲家/署名组 {len(composers)} · {PDF_KIND_LABEL} {len(manifest)} · 已下载 {downloaded_files}</div>',
        '<input id="search" type="search" placeholder="搜索作曲家、英文名或中文名……" aria-label="搜索目录">',
        '<main id="catalog">',
    ]
    for composer in composers:
        composer_zh = composer_zh_by_en[composer]
        composer_display = f"{composer}｜{composer_zh}"
        html_parts.append(
            f'<details open data-composer="{html.escape(composer_display)}">'
            f'<summary>{html.escape(composer_display)}</summary>'
        )
        for work in grouped[composer]:
            local_html: list[str] = []
            for record in files_by_work.get(work["work_id"], []):
                if record["relative_path"] in valid_local_paths:
                    label = record.get("description") or record["filename"]
                    href = urllib.parse.quote(record["relative_path"], safe="/._-~")
                    local_html.append(f'<a href="{href}">{html.escape(label)}</a>')
            if local_html:
                score_html = "；".join(local_html)
            elif work["score_file_count"]:
                score_html = '<span class="pending">待下载</span>'
            else:
                score_html = f'<span class="pending">{html.escape(work.get("exclusion_reason", "未识别到目标吉他 PDF"))}</span>'
            search_text = f"{composer} {composer_zh} {work['title_en']} {work['title_zh']}".casefold()
            html_parts.extend([
                f'<article class="work" data-search="{html.escape(search_text)}">',
                f'<div class="en">{html.escape(work["title_en"])}</div>',
                f'<div class="zh">{html.escape(work["title_zh"])}</div>',
                f'<div class="links"><a href="{html.escape(work["imslp_url"])}">IMSLP 原页</a> · 乐谱：{score_html}</div>',
                "</article>",
            ])
        html_parts.append("</details>")
    html_parts.extend([
        "</main>",
        "<script>",
        "const q=document.querySelector('#search');q.addEventListener('input',()=>{const s=q.value.trim().toLocaleLowerCase();document.querySelectorAll('.work').forEach(w=>w.hidden=s&&!w.dataset.search.includes(s));document.querySelectorAll('details').forEach(d=>{const n=[...d.querySelectorAll('.work')].some(w=>!w.hidden);d.hidden=!n;if(s&&n)d.open=true;});});",
        "</script></body></html>",
    ])
    (ROOT / "index.html").write_text("\n".join(html_parts) + "\n", encoding="utf-8")
    print(
        f"rendered README: works={len(works)}, works_with_entries={works_with_entries}, "
        f"manifest_files={len(manifest)}, downloaded={downloaded_files}",
        file=sys.stderr,
    )


def verify() -> int:
    works = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
    composer_translations = load_composer_translations()
    missing_catalog_entries = []
    invalid_pdfs = []
    sha1_mismatches = []
    manifest_paths = {record["relative_path"] for record in manifest}
    manifest_physical_paths = {filesystem_path_key(path) for path in manifest_paths}
    local_pdf_paths = {
        path.relative_to(ROOT).as_posix()
        for path in SCORES_DIR.rglob("*")
        if path.is_file()
        and (
            path.suffix.casefold() == ".pdf"
            or filesystem_path_key(path.relative_to(ROOT).as_posix()) in manifest_physical_paths
        )
    }
    local_physical_paths = {
        filesystem_path_key(path): path for path in local_pdf_paths
    }
    for record in manifest:
        path = ROOT / record["relative_path"]
        if not is_valid_pdf(path, record_expected_size(record)):
            invalid_pdfs.append(record["relative_path"])
        elif record_expected_sha1(record) and file_sha1(path) != record_expected_sha1(record):
            sha1_mismatches.append(record["relative_path"])
    readme = (ROOT / "README.md").read_text(encoding="utf-8") if (ROOT / "README.md").exists() else ""
    index_html = (ROOT / "index.html").read_text(encoding="utf-8") if (ROOT / "index.html").exists() else ""
    for work in works:
        if (
            work["title_en"] not in readme
            or work["title_zh"] not in readme
            or work.get("composer_zh", "") not in readme
            or work.get("composer_zh", "") not in index_html
        ):
            missing_catalog_entries.append(work["page_title"])
    missing_composer_translations = sorted({
        work["composer"] for work in works
        if work["composer"] not in composer_translations or not composer_translations[work["composer"]].strip()
    }, key=str.casefold)
    downloaded = sum(
        1 for record in manifest if is_valid_pdf(ROOT / record["relative_path"], record_expected_size(record))
    )
    missing_readme_links = [
        path for path in manifest_paths if urllib.parse.quote(path, safe="/._-~") not in readme
    ]
    missing_html_links = [
        path for path in manifest_paths if urllib.parse.quote(path, safe="/._-~") not in index_html
    ]
    impure_sections = [
        f"{record['page_title']} :: {record['section']}" for record in manifest
        if (
            CATEGORY_KIND == "original"
            and record["section"] != "Original work score"
        ) or (
            CATEGORY_KIND == "arrangement"
            and record["section"] not in WORK_LEVEL_TARGET_SECTIONS
            and not is_target_arrangement_heading(record["section"])
        )
    ]
    impure_score_files = [
        f"{record['page_title']} :: {record['filename']} :: {score_file_exclusion_reason(record)}"
        for record in manifest if score_file_exclusion_reason(record)
    ]
    part_files = list(SCORES_DIR.rglob("*.part"))
    print(json.dumps({
        "works": len(works),
        "score_records": len(manifest),
        "downloaded_valid_pdfs": downloaded,
        "invalid_local_pdfs": invalid_pdfs,
        "sha1_mismatches": sha1_mismatches,
        "extra_local_pdfs": sorted(
            path for key, path in local_physical_paths.items()
            if key not in manifest_physical_paths
        ),
        "missing_local_pdfs": sorted(
            record["relative_path"] for record in manifest
            if not (ROOT / record["relative_path"]).is_file()
        ),
        "partial_files": [path.relative_to(ROOT).as_posix() for path in part_files],
        "impure_score_sections": impure_sections,
        "impure_score_files": impure_score_files,
        "catalog_entries_missing_from_readme": missing_catalog_entries,
        "missing_composer_translations": missing_composer_translations,
        "score_links_missing_from_readme": missing_readme_links,
        "score_links_missing_from_html": missing_html_links,
    }, ensure_ascii=False, indent=2))
    failed = any([
        invalid_pdfs, sha1_mismatches,
        {key for key in local_physical_paths if key not in manifest_physical_paths},
        [record for record in manifest if not (ROOT / record["relative_path"]).is_file()],
        part_files, impure_sections, impure_score_files,
        missing_catalog_entries, missing_composer_translations,
        missing_readme_links, missing_html_links,
    ])
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    metadata_parser = subparsers.add_parser("metadata", help="Build IMSLP work and score manifests")
    metadata_parser.add_argument("--refresh", action="store_true")
    metadata_parser.add_argument("--no-render", action="store_true")
    download_parser = subparsers.add_parser("download", help="Download manifest PDFs with strict validation")
    download_parser.add_argument("--limit", type=int)
    download_parser.add_argument("--delay", type=float, default=0.0)
    download_parser.add_argument("--workers", type=int, default=1)
    download_parser.add_argument("--no-render", action="store_true")
    download_parser.add_argument(
        "--accept-mirror-revision",
        action="store_true",
        help="Accept a parseable PDF from an official IMSLP mirror when its revision differs from API metadata",
    )
    download_parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="Retry only files whose latest download-log status was unsuccessful",
    )
    ids_parser = subparsers.add_parser("file-ids", help="Resolve IMSLP file IDs and canonical static URLs")
    ids_parser.add_argument("--refresh", action="store_true")
    ids_parser.add_argument("--workers", type=int, default=1)
    ids_parser.add_argument("--no-render", action="store_true")
    subparsers.add_parser("render", help="Render README and CSV indexes")
    subparsers.add_parser("verify", help="Verify metadata, README coverage, and local PDFs")
    subparsers.add_parser(
        "quarantine-revision-mismatches",
        help="Move parseable PDFs whose sizes do not match the active IMSLP revision",
    )
    subparsers.add_parser(
        "recover-revision-aliases",
        help="Copy quarantined PDFs to exact same-work size/SHA-1 manifest matches",
    )
    subparsers.add_parser(
        "restore-active-quarantine",
        help="Restore exact active-manifest PDFs that were previously quarantined",
    )
    args = parser.parse_args()

    if args.command == "metadata":
        build_metadata(refresh=args.refresh)
        if not args.no_render:
            render_readme()
    elif args.command == "file-ids":
        resolve_file_ids(refresh=args.refresh, workers=args.workers)
        if not args.no_render:
            render_readme()
    elif args.command == "download":
        manifest = json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        if any(not record.get("verified_download_url") for record in manifest):
            resolve_file_ids(workers=args.workers)
        download_scores(
            limit=args.limit,
            delay=args.delay,
            workers=args.workers,
            accept_mirror_revision=args.accept_mirror_revision,
            retry_failures=args.retry_failures,
        )
        if not args.no_render:
            render_readme()
    elif args.command == "render":
        render_readme()
    elif args.command == "verify":
        return verify()
    elif args.command == "quarantine-revision-mismatches":
        quarantine_revision_mismatches()
    elif args.command == "recover-revision-aliases":
        recover_revision_aliases()
    elif args.command == "restore-active-quarantine":
        restore_active_quarantine_files()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
