from __future__ import annotations

import re
from dataclasses import dataclass

from .client import AllCategory


CANONICAL_NAME_RE = re.compile(r"For [A-Za-z0-9À-ÖØ-öø-ÿ .,'’()+-]+")
GUITAR_RE = re.compile(r"\bguitars?\b", re.IGNORECASE)
ARRANGEMENT_RE = re.compile(r"\s+\(arr\)$", re.IGNORECASE)

# Solo and pure-guitar targets belong to config/categories.json and must not be
# duplicated in the mixed-instrument extension.
PURE_GUITAR_RE = re.compile(
    r"For (?:(?:\d+(?: and \d+)?|\d+-\d+) guitars?|"
    r"(?:\d+[- ]string |ten string |terz |spanish |classical )?guitar"
    r"(?: ensemble| orchestra)?)(?: \(arr\))?$",
    re.IGNORECASE,
)

SCOPE_EXCLUSIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("non_classical_guitar", re.compile(
        r"\b(?:electric|bass|hawaiian|steel|slide) guitars?\b|\bguitar synthesizer\b",
        re.IGNORECASE,
    )),
    ("voice_or_chorus", re.compile(
        r"\b(?:voices?|vocal|narrators?|reciters?|speakers?|mezzo(?:-soprano)?s?|"
        r"sopranos?(?!\s+(?:saxophones?|recorders?))|"
        r"altos?(?!\s+(?:flutes?|recorders?|saxophones?))|"
        r"tenors?(?!\s+(?:saxophones?|recorders?))|"
        r"baritones?(?!\s+saxophones?)|chorus|choir|childrens chorus|"
        r"female chorus|mixed chorus)\b",
        re.IGNORECASE,
    )),
    ("large_ensemble", re.compile(
        r"\b(?:orchestra|concert band|symphonic band|wind ensemble)\b",
        re.IGNORECASE,
    )),
    ("electronic_or_tape", re.compile(
        r"\b(?:electronics?|electronic sounds?|synthesizers?|tape|computer)\b",
        re.IGNORECASE,
    )),
)

FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("strings", re.compile(
        r"\b(?:violins?|violas?|cellos?|violoncellos?|double bass(?:es)?|"
        r"contrabass(?:es)?|viols?|strings?|baryton|erhus?|theremins?)\b",
        re.IGNORECASE,
    )),
    ("woodwinds", re.compile(
        r"\b(?:flutes?|piccolos?|recorders?|oboes?|clarinets?|bassoons?|"
        r"saxophones?|woodwinds?|english horn|cor anglais|chalumeaux?|duduks?|"
        r"ocarinas?|pan-?flutes?)\b",
        re.IGNORECASE,
    )),
    ("brass", re.compile(
        r"\b(?:trumpets?|trombones?|horns?|tubas?|cornetts?|cornets?|"
        r"euphoniums?|flugelhorns?|brass)\b",
        re.IGNORECASE,
    )),
    ("keyboard_reed", re.compile(
        r"\b(?:pianos?|piano 4 hands|harpsichords?|organs?|harmoniums?|"
        r"accordions?|bandoneons?|concertinas?|harmonicas?|keyboards?|celestas?)\b",
        re.IGNORECASE,
    )),
    ("plucked", re.compile(
        r"\b(?:mandolins?|mandolas?|mandocellos?|mandoloncellos?|lutes?|"
        r"theorbos?|harps?|bandurrias?|banjos?|ukuleles?|balalaikas?|"
        r"zithers?|sitars?)\b",
        re.IGNORECASE,
    )),
    ("percussion", re.compile(
        r"\b(?:percussion|timpani|vibraphones?|marimbas?|xylophones?|"
        r"glockenspiels?|drums?|drum set|cymbals?|tambourines?)\b",
        re.IGNORECASE,
    )),
    ("generic_chamber", re.compile(
        r"\b(?:(?:treble|bass|unspecified) instruments?|continuo|"
        r"chamber ensemble|instrumental ensemble)\b",
        re.IGNORECASE,
    )),
)

FAMILY_ZH = {
    "strings": "吉他与弦乐",
    "woodwinds": "吉他与木管",
    "brass": "吉他与铜管",
    "keyboard_reed": "吉他与键盘/自由簧",
    "plucked": "吉他与拨弦乐器",
    "percussion": "吉他与打击乐",
    "mixed_chamber": "吉他室内乐（混合编制）",
}

INSTRUMENT_TRANSLATIONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE), replacement)
    for pattern, replacement in (
        (r"double bass(?:es)?", "低音提琴"),
        (r"english horn", "英国管"),
        (r"cor anglais", "英国管"),
        (r"treble instruments?", "高音乐器"),
        (r"bass instruments?", "低音乐器"),
        (r"unspecified instruments?", "未指定乐器"),
        (r"violoncellos?|cellos?", "大提琴"),
        (r"contrabass(?:es)?", "低音提琴"),
        (r"violins?", "小提琴"),
        (r"viola d'amore|viola damore", "维奥尔德阿莫雷琴"),
        (r"violas?", "中提琴"),
        (r"viols?", "维奥尔琴"),
        (r"strings?", "弦乐"),
        (r"pan-?flutes?", "排箫"),
        (r"alto flutes?", "中音长笛"),
        (r"bass flutes?", "低音长笛"),
        (r"flutes?", "长笛"),
        (r"piccolos?", "短笛"),
        (r"recorders?", "竖笛"),
        (r"oboes?", "双簧管"),
        (r"clarinets?", "单簧管"),
        (r"bassoons?", "大管"),
        (r"soprano saxophones?", "高音萨克斯管"),
        (r"alto saxophones?", "中音萨克斯管"),
        (r"tenor saxophones?", "次中音萨克斯管"),
        (r"baritone saxophones?", "上低音萨克斯管"),
        (r"saxophones?", "萨克斯管"),
        (r"woodwinds?", "木管乐器"),
        (r"duduks?", "杜杜克管"),
        (r"trumpets?", "小号"),
        (r"trombones?", "长号"),
        (r"horns?", "圆号"),
        (r"tubas?", "大号"),
        (r"cornetts?|cornets?", "短号"),
        (r"euphoniums?", "上低音号"),
        (r"flugelhorns?", "柔音号"),
        (r"pianos?", "钢琴"),
        (r"harpsichords?", "羽管键琴"),
        (r"organs?", "管风琴"),
        (r"harmoniums?", "簧风琴"),
        (r"accordions?", "手风琴"),
        (r"bandoneons?", "班多钮手风琴"),
        (r"concertinas?", "六角手风琴"),
        (r"harmonicas?", "口琴"),
        (r"keyboards?", "键盘乐器"),
        (r"celestas?", "钢片琴"),
        (r"mandolins?", "曼陀林"),
        (r"mandolas?", "曼陀拉"),
        (r"mandocellos?|mandoloncellos?", "曼陀大提琴"),
        (r"lutes?", "鲁特琴"),
        (r"theorbos?", "西奥伯琴"),
        (r"harps?", "竖琴"),
        (r"bandurrias?", "班杜里亚琴"),
        (r"banjos?", "班卓琴"),
        (r"ukuleles?", "尤克里里"),
        (r"balalaikas?", "巴拉莱卡琴"),
        (r"zithers?", "齐特琴"),
        (r"sitars?", "西塔琴"),
        (r"percussion", "打击乐"),
        (r"timpani", "定音鼓"),
        (r"vibraphones?", "颤音琴"),
        (r"marimbas?", "马林巴"),
        (r"xylophones?", "木琴"),
        (r"glockenspiels?", "钟琴"),
        (r"ocarinas?", "陶笛"),
        (r"erhus?", "二胡"),
        (r"theremins?", "特雷门琴"),
        (r"hand saw", "音乐锯"),
        (r"bells?", "钟"),
        (r"guitars?", "吉他"),
        (r"continuo", "通奏低音"),
    )
)


@dataclass(frozen=True, slots=True)
class MixedCategory:
    name: str
    kind: str
    display_group: str
    name_zh: str
    target_instrument: str
    guitar_count: int
    page_count: int
    file_count: int
    subcategory_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "url": "https://imslp.org/wiki/Category:" + self.name.replace(" ", "_"),
            "kind": self.kind,
            "display_group": self.display_group,
            "name_zh": self.name_zh,
            "target_instrument": self.target_instrument,
            "guitar_count": self.guitar_count,
            "page_count": self.page_count,
            "file_count": self.file_count,
            "subcategory_count": self.subcategory_count,
        }


def _target_instrument(name: str) -> str:
    target = ARRANGEMENT_RE.sub("", name)
    return re.sub(r"^For\s+", "", target, flags=re.IGNORECASE).strip()


def _guitar_count(target: str) -> int:
    counts = [int(value) for value in re.findall(r"\b(\d+)\s+guitars?\b", target, re.IGNORECASE)]
    return max(counts, default=1)


def category_name_zh(name: str) -> str:
    arrangement = bool(ARRANGEMENT_RE.search(name))
    translated = _target_instrument(name)
    for pattern, replacement in INSTRUMENT_TRANSLATIONS:
        translated = pattern.sub(replacement, translated)
    translated = re.sub(r"\s*,\s*", "、", translated)
    translated = re.sub(r"\s+and\s+", "、", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\s+with\s+", "与", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\s+or\s+", "或", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\s+", " ", translated).strip()
    return f"{translated}·{'改编' if arrangement else '原作'}"


def classify_mixed_category(
    category: AllCategory,
    *,
    pure_category_names: frozenset[str] = frozenset(),
) -> tuple[MixedCategory | None, str | None]:
    name = category.name
    if category.page_count <= 0:
        return None, "empty"
    if (
        not CANONICAL_NAME_RE.fullmatch(name)
        or name.count("(") != name.count(")")
        or not GUITAR_RE.search(name)
    ):
        return None, "malformed_or_unrelated"
    if name in pure_category_names or PURE_GUITAR_RE.fullmatch(name):
        return None, "pure_guitar"
    for reason, pattern in SCOPE_EXCLUSIONS:
        if pattern.search(name):
            return None, reason

    target = _target_instrument(name)
    # These categories provide alternative solo versions rather than music
    # for guitar and another instrument at the same time.
    if re.fullmatch(r"(?:piano|organ|harpsichord|violin|flute) or guitars?", target, re.IGNORECASE):
        return None, "alternative_solo"
    if re.fullmatch(r"guitars? or (?:piano|organ|harpsichord|violin|flute)", target, re.IGNORECASE):
        return None, "alternative_solo"
    if (
        re.fullmatch(r"[^,]+ or guitars?", target, re.IGNORECASE)
        or re.fullmatch(r"guitars? or [^,]+", target, re.IGNORECASE)
    ) and not re.search(r"\b(?:and|with)\b", target, re.IGNORECASE):
        return None, "alternative_solo"

    without_guitar = GUITAR_RE.sub("", target)
    families = {
        family for family, pattern in FAMILY_PATTERNS if pattern.search(without_guitar)
    }
    if not families:
        return None, "no_supported_partner_instrument"
    display_group = (
        next(iter(families))
        if len(families) == 1 and "generic_chamber" not in families
        else "mixed_chamber"
    )
    return MixedCategory(
        name=name,
        kind="arrangement" if ARRANGEMENT_RE.search(name) else "original",
        display_group=display_group,
        name_zh=category_name_zh(name),
        target_instrument=target,
        guitar_count=_guitar_count(target),
        page_count=category.page_count,
        file_count=category.file_count,
        subcategory_count=category.subcategory_count,
    ), None


__all__ = [
    "FAMILY_ZH",
    "MixedCategory",
    "category_name_zh",
    "classify_mixed_category",
]
