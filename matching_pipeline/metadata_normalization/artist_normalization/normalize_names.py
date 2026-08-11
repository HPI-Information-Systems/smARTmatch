"""
Deterministic parser for raw auction artist strings: strips attribution/
nationality/honorific noise, splits out embedded dates and places, and
resolves the remainder to a normalized "Firstname Lastname" form.
"""

import csv
import json
import re
from pathlib import Path

_DIR = Path(__file__).parent
_INPUT = _DIR / "input" / "new_artist_auction.csv"
_OUT_JSONL = _DIR / "output" / "new_artist_names_normalized.jsonl"
_OUT_CSV = _DIR / "output" / "new_artist_names_unmatched.csv"

_DASH = r"[-–—]"
_BORN = r"(?:b\.?|geb\.?|born|n[ée]e?(?:\s+en)?)"
_TOK = r"[\w.\-'\/]+"  # one name-like token (slash allowed for "DUTCH/BELGIAN")

# ---------------------------------------------------------------------------
# Lookup sets
# ---------------------------------------------------------------------------

_NATIONALITIES = {
    "italian",
    "british",
    "french",
    "german",
    "dutch",
    "flemish",
    "spanish",
    "american",
    "austrian",
    "swiss",
    "belgian",
    "russian",
    "norwegian",
    "swedish",
    "danish",
    "portuguese",
    "polish",
    "hungarian",
    "czech",
    "chinese",
    "japanese",
    "indian",
    "scottish",
    "english",
    "irish",
    "welsh",
    "greek",
    "turkish",
    "romanian",
    "serbian",
    "croatian",
    "slovenian",
    "slovak",
    "ukrainian",
    "bulgarian",
    "finnish",
    "icelandic",
    "canadian",
    "australian",
    "argentinian",
    "brazilian",
    "mexican",
    "peruvian",
    "colombian",
    "venezuelan",
    "chilean",
    "ecuadorian",
    "cuban",
    "iranian",
    "egyptian",
    "moroccan",
    "south african",
    "nigerian",
    "israeli",
    "korean",
    "vietnamese",
    "thai",
    "singaporean",
    "malaysian",
    "indonesian",
    "philippine",
    "philippino",
    "filipino",
    "pakistani",
    "afghan",
    "iraqi",
    "syrian",
    "lebanese",
    "jordanian",
    "saudi",
    "emirati",
    "qatari",
    "kuwaiti",
    "bahraini",
    "omani",
    "yemeni",
    "libyan",
    "tunisian",
    "algerian",
    "sudanese",
    "ethiopian",
    "kenyan",
    "ghanaian",
    "senegalese",
    "ivorian",
    "cameroonian",
    "congolese",
    "zimbabwean",
    "tanzanian",
    "ugandan",
    "rwandan",
    "mozambican",
    "namibian",
    "botswanan",
    "zambian",
    "malawian",
    "malagasy",
    "new zealand",
    "nz",
    "american/canadian",
    "american/british",
    # Country names used as demonyms / in token form
    "italy",
    "france",
    "germany",
    "netherlands",
    "belgium",
    "england",
    "austria",
    "spain",
    "russia",
    "poland",
    "hungary",
    "switzerland",
    "denmark",
    "sweden",
    "norway",
    "finland",
    "ireland",
    "scotland",
    "israel",
    "korea",
    "vietnam",
    "thailand",
    "china",
    "japan",
    "america",  # singular (as in "Jim Torok (America, b. 1954)")
    # German country names (used as-is in auction data)
    "niederlande",
    "deutschland",
    "frankreich",
    "belgien",
    "österreich",
    "schweiz",
    "spanien",
    "portugal",
    "italien",
    "russland",
    "ungarn",
    "tschechien",
    "slowakei",
    "rumänien",
    "bulgarien",
    "kroatien",
    "serbien",
    "griechenland",
    "türkei",
    "großbritannien",
    "irland",
    "schottland",
    "dänemark",
    "norwegen",
    "finnland",
    "island",
    "kanada",
    "australien",
    "mexiko",
    "brasilien",
    "argentinien",
    "china",
    "japan",
    "indien",
    "iran",
    "irak",
    "ägypten",
    # German-language demonyms
    "italienisch",
    "französisch",
    "deutsch",
    "niederländisch",
    "flämisch",
    "spanisch",
    "amerikanisch",
    "österreichisch",
    "schweizerisch",
    "belgisch",
    "russisch",
    "norwegisch",
    "schwedisch",
    "dänisch",
    "portugiesisch",
    "polnisch",
    "ungarisch",
    "tschechisch",
    "chinesisch",
    "japanisch",
    "indisch",
    "schottisch",
    "englisch",
    "irisch",
    "griechisch",
    "türkisch",
    "rumänisch",
    "serbisch",
    "kroatisch",
}

_NON_NAME_CORE = {
    "workshop",
    "master",
    "follower",
    "circle",
    "attributed",
    "studio",
    "school",
    "manner",
    "after",
    "style",
    "copy",
    "monogrammist",
    "attr",
    "attrib",
    "and",
    "&",
    "or",
    "und",
    "sowie",
    "called",
    "known",
    "alias",
    "wohl",
    "genannt",
    "active",
    "act",
    "artist",
    "artists",
    "painter",
    "painters",
    "sculptor",
    "sculptors",
    "künstler",
    "maler",
    "meister",
    "il",  # Italian article (prevents "Il Tintoretto" as name)
    "zugeschr",
    "zugeschrieben",  # German attribution markers
    "bei",  # German "at/by" — marks publisher imprints, not artist names
}
_NON_NAME = _NON_NAME_CORE | _NATIONALITIES
_BAD_FIRSTNAME = {"d.i.", "i.e.", "called", "alias", "genannt", "wohl", "the"}
_COMPOUND_PREFIXES = {
    "von",
    "van",
    "de",
    "du",
    "la",
    "le",
    "ter",
    "den",
    "der",
    "af",
    "av",
    "di",
    "del",
    "della",
    "degli",
    "los",
    "las",
    "el",
}
_LOWERCASE_WORDS = _COMPOUND_PREFIXES | {"of", "the", "and", "in", "an", "a"}

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_ATTRIBUTION_STRIP = re.compile(
    r"^(?:attributed(?:\s+to)?|attr\.?(?:\s+to)?|attrib\.?(?:\s+to)?)\s+",
    re.IGNORECASE,
)
_ATTRIBUTION_QUALIFY = re.compile(
    r"^((?:circle|follower|workshop|studio|manner|school)\s+of\s+"
    r"|in\s+the\s+manner\s+of\s+"
    r"|style\s+of\s+"
    r"|copy\s+(?:after|of)\s+"
    r"|(?:after|nach)\s+"
    r")",
    re.IGNORECASE,
)
_PARENS_IGNORABLE = re.compile(
    r"^[?\s†*]+$"
    r"|^(?:sr\.?|jr\.?|snr\.?|jnr\.?)$"
    r"|^wohl(?:\s*/?\s*vielleicht)?$"
    r"|^vielleicht$",
    re.IGNORECASE,
)
_PARENS_STRIP_SUFFIX = re.compile(
    r",?\s*(?:attributed\s+to|attr\.?|attrib\.?)\s*$", re.IGNORECASE
)
_CIRCA_RE = re.compile(
    r"(?:circa|ca\.?|c\.)\s*(?=\d)|(?<!\w)um\s*(?=\d)",
    re.IGNORECASE,
)
_WOHL_PREFIX_RE = re.compile(r"^wohl\s+", re.IGNORECASE)
_STAR_PREFIX_RE = re.compile(r"^\*\s*")
_HON_PATTERN = re.compile(r"^(?:[A-Za-z]{1,3}\.){2,}$")
_HON_EXPLICIT = {"sr.", "jr.", "snr.", "jnr.", "esq.", "prof.", "professor"}
_UNKNOWN_RE = re.compile(
    r"^(?:unbekannt(?:e[rns]?)?\s*(?:\w+\s+)?(?:künstler|maler|meister|autor|master|artist)?"
    r"|unbek\.?\s*(?:\w+\s+)?(?:künstler|maler|meister)?"
    r"|unknown\s*(?:artist|painter|master)?"
    r"|anonyme?(?:ous)?"
    r"|inconnu[es]?"
    r")\s*$",
    re.IGNORECASE | re.UNICODE,
)
_COLLECTIVE_RE = re.compile(
    r"\b(?:school|schule)\b|^monogrammist(?:in)?\b",
    re.IGNORECASE | re.UNICODE,
)
_CENTURY_TEXT_RE = re.compile(
    r"^\d{1,2}(?:st|nd|rd|th)?(?:/\d{1,2}(?:st|nd|rd|th)?)?\s*\.?\s*(?:century|jahrhundert\.?|jh\.?|c\.?)$",
    re.IGNORECASE,
)
_ROMAN_NUMERAL_RE = re.compile(
    r"^M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$",
    re.IGNORECASE,
)
_NAME_SUFFIX_RE = re.compile(r"\s+d\.\s*([ÄJäj])\.", re.IGNORECASE)
_TITLE_PREFIX_RE = re.compile(
    r"^(?:Sir|Dame|Lord|Lady|Baron(?:ess)?|Count(?:ess)?|Prince(?:ss)?|Graf)\s+",
    re.IGNORECASE,
)
_BIRTH_GLYPH_RE = re.compile(rf"[,\s]*\*\s*(\d{{4}})(?:\s+({_TOK}))?", re.UNICODE)
_DEATH_GLYPH_RE = re.compile(rf"[,\s]*†\s*(\d{{4}})(?:\s+({_TOK}))?", re.UNICODE)


# ---------------------------------------------------------------------------
# Parenthetical parsing
# ---------------------------------------------------------------------------


def _is_nat_token(token: str) -> bool:
    return all(p.lower() in _NATIONALITIES for p in re.split(r"/", token))


_EMPTY = {"dob": None, "dod": None, "pob": None, "pod": None}


def _parse_parens_content(raw: str) -> dict | None:
    """Parse one parenthetical string into {dob, dod, pob, pod}. Returns None if unrecognized."""
    c = raw.strip()
    if not c or _PARENS_IGNORABLE.match(c):
        return _EMPTY

    c = _PARENS_STRIP_SUFFIX.sub("", c).strip()
    if not c:
        return _EMPTY

    c = _WOHL_PREFIX_RE.sub("", c)
    c = _STAR_PREFIX_RE.sub("", c)
    c = _CIRCA_RE.sub("", c).strip()
    c = re.sub(
        r"^(?:b\.|geb\.?|born|n[ée]e?(?:\s+en)?)\s*", "", c, flags=re.IGNORECASE
    ).strip()
    if not c:
        return _EMPTY

    # Strip leading nationality token(s): "American, b. 1960" → "1960"
    m = re.match(rf"^({_TOK}(?:/{_TOK})?),?\s+", c, re.IGNORECASE | re.UNICODE)
    if m and _is_nat_token(m.group(1)):
        c = c[m.end() :].strip()
        if not c:
            return _EMPTY
        c = _WOHL_PREFIX_RE.sub("", c)
        c = _STAR_PREFIX_RE.sub("", c)
        c = _CIRCA_RE.sub("", c).strip()
        c = re.sub(
            r"^(?:b\.|geb\.?|born|n[ée]e?(?:\s+en)?)\s*", "", c, flags=re.IGNORECASE
        ).strip()
        if not c:
            return _EMPTY

    c = re.sub(r"(\d{4})\s*/\s*$", r"\1", c).strip()
    c = re.sub(r"\b(?:early|late|mid)\b[\s-]*", "", c, flags=re.IGNORECASE).strip()
    c = re.sub(
        r"(\d{1,2}(?:st|nd|rd|th)?)-\d{1,2}(?:st|nd|rd|th)?\b",
        r"\1",
        c,
        flags=re.IGNORECASE,
    )
    c = re.sub(r"\b(?:died?|gest\.?)\s+(\d{4})", r"-\1", c, flags=re.IGNORECASE).strip()
    c = re.sub(r"(?<=\d)\s+to\s+(?=\d)", "-", c, flags=re.IGNORECASE)
    # "Stadt-YYYY" and "YYYY-Stadt" are German shorthand for a date range — split them
    c = re.sub(r"([A-Za-zÀ-ÖØ-öø-ÿ]{2,})-(\d{4})", r"\1 - \2", c)
    c = re.sub(r"(\d{4})-([A-Za-zÀ-ÖØ-öø-ÿ]{2,})", r"\1 - \2", c)
    if not c:
        return _EMPTY

    d = _DASH

    def _place(tok):
        return None if _is_nat_token(tok) else tok

    # F.  Token YYYY - Token YYYY  (e.g. "Stockholm 1659 - London 1743")
    m = re.match(rf"^({_TOK})\s+(\d{{4}})\s*{d}\s*({_TOK})\s+(\d{{4}})$", c, re.UNICODE)
    if m and _is_name_token(m.group(1)) and _is_name_token(m.group(3)):
        return {
            "pob": _place(m.group(1)),
            "dob": m.group(2),
            "pod": _place(m.group(3)),
            "dod": m.group(4),
        }

    # Ax. YYYY Token - YYYY Token
    m = re.match(rf"^(\d{{4}})\s+({_TOK})\s*{d}\s*(\d{{4}})\s+({_TOK})$", c, re.UNICODE)
    if m:
        return {
            "dob": m.group(1),
            "pob": _place(m.group(2)),
            "dod": m.group(3),
            "pod": _place(m.group(4)),
        }

    # Bx. YYYY Token - YYYY
    m = re.match(rf"^(\d{{4}})\s+({_TOK})\s*{d}\s*(\d{{4}})$", c, re.UNICODE)
    if m:
        return {
            "dob": m.group(1),
            "pob": _place(m.group(2)),
            "dod": m.group(3),
            "pod": None,
        }

    # Cx. YYYY Token
    m = re.match(rf"^(\d{{4}})\s+({_TOK})$", c, re.UNICODE)
    if m:
        return {"dob": m.group(1), "pob": _place(m.group(2)), "dod": None, "pod": None}

    # I-frac. YYYY/YY (e.g. "1530/31") — stored as-is
    if re.match(r"^\d{4}/\d{1,4}$", c):
        return {"dob": c, "dod": None, "pob": None, "pod": None}

    # fl./active — ignored
    if re.match(
        rf"^(?:fl\.?|act\.?|active)\s+\d{{4}}(?:\s*{d}\s*\d{{4}})?$", c, re.IGNORECASE
    ):
        return _EMPTY

    # D.  Token YYYY-YYYY  (token left → pob)
    m = re.match(rf"^({_TOK}),?\s+(\d{{4}})\s*{d}\s*(\d{{4}})\??$", c, re.UNICODE)
    if m and _is_name_token(m.group(1)):
        return {
            "dob": m.group(2),
            "dod": m.group(3),
            "pob": _place(m.group(1)),
            "pod": None,
        }

    # Db. Token YYYY-  (token left → pob)
    m = re.match(rf"^({_TOK}),?\s+(\d{{4}})\s*{d}\s*\??$", c, re.UNICODE)
    if m and _is_name_token(m.group(1)):
        return {"dob": m.group(2), "dod": None, "pob": _place(m.group(1)), "pod": None}

    # Dc. Token YYYY  (token left → pob)
    m = re.match(rf"^({_TOK}),?\s+(\d{{4}})$", c, re.UNICODE)
    if m and _is_name_token(m.group(1)):
        return {"dob": m.group(2), "dod": None, "pob": _place(m.group(1)), "pod": None}

    # E.  YYYY-YYYY Token  (token right → pod)
    m = re.match(rf"^(\d{{4}})\s*{d}\s*(\d{{4}}),?\s+({_TOK})$", c, re.UNICODE)
    if m and _is_name_token(m.group(3)):
        return {
            "dob": m.group(1),
            "dod": m.group(2),
            "pob": None,
            "pod": _place(m.group(3)),
        }

    # G-frac.  YYYY/YY-YYYY/YY  or  YYYY/YY-YYYY  or  YYYY-YYYY/YY
    m = re.match(rf"^(\d{{4}}/\d{{1,4}})\s*{d}\s*(\d{{4}}(?:/\d{{1,4}})?)$", c)
    if m:
        return {"dob": m.group(1), "dod": m.group(2), "pob": None, "pod": None}
    m = re.match(rf"^(\d{{4}})\s*{d}\s*(\d{{4}}/\d{{1,4}})$", c)
    if m:
        return {"dob": m.group(1), "dod": m.group(2), "pob": None, "pod": None}

    # G-month.  YYYY - D. MonthName YYYY  (e.g. "1950 - 8. Dezember 2023")
    _MONTHS = (
        r"januar|februar|märz|april|mai|juni|juli|august|september|oktober|november|dezember"
        r"|january|february|march|april|may|june|july|august|september|october|november|december"
    )
    m = re.match(
        rf"^(\d{{4}})\s*{d}\s*\d{{1,2}}\.?\s*(?:{_MONTHS})\s+(\d{{4}})$",
        c,
        re.IGNORECASE,
    )
    if m:
        return {"dob": m.group(1), "dod": m.group(2), "pob": None, "pod": None}

    # G.  YYYY-YYYY
    m = re.match(rf"^(\d{{4}})\s*{d}\s*(\d{{4}})\??$", c)
    if m:
        return {"dob": m.group(1), "dod": m.group(2), "pob": None, "pod": None}

    # Gx. YYYY-YYYY + trailing text (e.g. "1924-2022, Würzburger Künstler")
    m = re.match(rf"^(\d{{4}})\s*{d}\s*(\d{{4}})[,\s].+$", c)
    if m:
        return {"dob": m.group(1), "dod": m.group(2), "pob": None, "pod": None}

    # H.  YYYY-
    m = re.match(rf"^(\d{{4}})\s*{d}\s*\??$", c)
    if m:
        return {"dob": m.group(1), "dod": None, "pob": None, "pod": None}

    # I.  YYYY  or  b. YYYY
    m = re.match(rf"^\*?\s*(\d{{4}})$|^{_BORN}\s*(\d{{4}})$", c)
    if m:
        return {"dob": m.group(1) or m.group(2), "dod": None, "pob": None, "pod": None}

    if _CENTURY_TEXT_RE.match(c):
        return _EMPTY

    # Nationality + century text ("Italian, 19th Century") — ignored
    m = re.match(rf"^({_TOK}),?\s+", c, re.UNICODE)
    if m and _is_name_token(m.group(1)) and _is_nat_token(m.group(1)):
        if _CENTURY_TEXT_RE.match(c[m.end() :].strip()):
            return _EMPTY

    if not re.search(r"\d", c):
        return _EMPTY

    return None  # unrecognized → caller marks entry as unmatched


def _extract_wohl_content(raw: str) -> tuple[str | None, dict]:
    """For paren content like "wohl Oskar K. 1925 Erlangen-2004 Fürth": extract name tokens and dates."""
    c = _WOHL_PREFIX_RE.sub("", raw.strip())
    c = _STAR_PREFIX_RE.sub("", c).strip()
    c = _CIRCA_RE.sub("", c).strip()
    # Only strip lowercase "b." — uppercase "B." could be a name initial.
    c = re.sub(
        r"\b(?:geb\.?|born|n[ée]e?(?:\s+en)?)\s*", "", c, flags=re.IGNORECASE
    ).strip()
    c = re.sub(r"\bb\.\s*", "", c).strip()
    if not c:
        return None, dict(_EMPTY)

    toks = c.split()
    name_toks, date_start = [], len(toks)
    for i, tok in enumerate(toks):
        if re.match(r"^\*?\d{4}", tok):
            date_start = i
            break
        name_toks.append(tok)

    date_str = " ".join(toks[date_start:])

    if not name_toks:
        parsed = _parse_parens_content(date_str) if date_str else None
        if parsed and any(parsed.get(k) for k in ("dob", "dod", "pob", "pod")):
            return None, parsed
        return None, dict(_EMPTY)

    name = " ".join(name_toks)
    if _has_non_name_word(name) or not all(_is_name_token(t) for t in name_toks):
        return None, dict(_EMPTY)

    di: dict = dict(_EMPTY)
    if date_str:
        parsed = _parse_parens_content(date_str)
        if parsed and any(parsed.get(k) for k in ("dob", "dod", "pob", "pod")):
            di = parsed
        else:
            years = re.findall(r"\d{4}", date_str)
            di = {
                "dob": years[0] if years else None,
                "dod": years[-1] if len(years) > 1 else None,
                "pob": None,
                "pod": None,
            }
    return name, di


# ---------------------------------------------------------------------------
# Abbreviation / initial merging (for wohl name expansion)
# ---------------------------------------------------------------------------


def _is_abbrev_for(short: str, full: str) -> bool:
    """True if short could be an abbreviation of full."""
    if short.endswith("."):
        p = short[:-1]
        return bool(p) and len(full) > len(p) and full.upper().startswith(p.upper())
    return (
        "-" in full
        and len(full) > len(short)
        and full.upper().startswith(short.upper())
    )


def _best_token(mt: str, wt: str) -> str | None:
    if mt.upper() == wt.upper():
        return mt
    if _is_abbrev_for(mt, wt):
        return wt
    if _is_abbrev_for(wt, mt):
        return mt
    if "-" in wt and wt.upper()[0] == mt.upper()[0]:
        return mt
    return None


def _merge_initials(main_name: str, wohl_name: str) -> str | None:
    """Merge main_name and wohl_name by expanding abbreviations/initials."""
    mtoks = main_name.split()
    wtoks = wohl_name.split()

    if len(mtoks) == len(wtoks):
        result: list[str] = []
        for mt, wt in zip(mtoks, wtoks):
            best = _best_token(mt, wt)
            if best is None:
                return None
            result.append(best)
        return " ".join(result)

    if len(mtoks) == 1:
        mt, last_wt = mtoks[0], wtoks[-1]
        if _is_abbrev_for(last_wt, mt) or last_wt.upper() == mt.upper():
            return " ".join(wtoks[:-1] + [mt])
        mt_bare = mt[:-1] if mt.endswith(".") else mt
        if len(mt_bare) == 1 and mt_bare.isalpha():
            if any(wt.upper().startswith(mt_bare.upper()) for wt in wtoks):
                return " ".join(wtoks)
        return None

    if len(wtoks) < len(mtoks):
        result = list(mtoks)
        used: set[int] = set()
        for wt in wtoks:
            for i, mt in enumerate(mtoks):
                if i not in used and _is_abbrev_for(mt, wt):
                    result[i] = wt
                    used.add(i)
                    break
        return " ".join(result) if used else None

    first_best = _best_token(mtoks[0], wtoks[0])
    last_best = _best_token(mtoks[-1], wtoks[-1])
    if first_best is not None and last_best is not None:
        return " ".join([first_best] + wtoks[1:-1] + [last_best])

    k = len(mtoks)
    if len(wtoks) > k:
        suffix = wtoks[len(wtoks) - k :]
        if all(_is_abbrev_for(suffix[i], mtoks[i]) for i in range(k)):
            return " ".join(wtoks[: len(wtoks) - k] + mtoks)

    return None


# ---------------------------------------------------------------------------
# Date/info extraction from name string
# ---------------------------------------------------------------------------


def _extract_inline_dates(name: str, info: dict) -> str:
    """Strip date/place info embedded directly in the name string; update info in-place."""
    d = _DASH

    # Trailing YYYY[-YYYY], YYYY-, *YYYY, or lone year after comma
    for pat, keys in [
        (rf",?\s+(\d{{4}})\s*{d}\s*(\d{{4}})\s*$", ("dob", "dod")),
        (rf",?\s+(\d{{4}})\s*{d}\s*$", ("dob",)),
        (r",?\s+\*\s*(\d{4})\s*$", ("dob",)),
        (r",\s*(\d{4})\s*$", ("dob",)),
    ]:
        m = re.search(pat, name)
        if m:
            for i, k in enumerate(keys):
                if info[k] is None:
                    info[k] = m.group(i + 1)
            name = name[: m.start()].strip().rstrip(",").strip()
            break

    # * (born) and † (died) glyphs inline, with "ebenda" resolved to place-of-birth
    for glyph_re, dkey, pkey in [
        (_BIRTH_GLYPH_RE, "dob", "pob"),
        (_DEATH_GLYPH_RE, "dod", "pod"),
    ]:
        m = glyph_re.search(name)
        if m:
            if info[dkey] is None:
                info[dkey] = m.group(1)
            place = m.group(2)
            if (
                pkey == "pod"
                and place
                and place.lower().rstrip(".") in ("ebenda", "ebd")
            ):
                place = info["pob"]
            if info[pkey] is None and place:
                info[pkey] = place
            name = (
                re.sub(r"\s{2,}", " ", name[: m.start()] + name[m.end() :])
                .strip()
                .strip(",")
                .strip()
            )

    # "…, <date_info>" suffix — split at second comma if present, else first
    if "," in name:
        commas = [i for i, c in enumerate(name) if c == ","]
        idx = commas[1] if len(commas) >= 2 else commas[0]
        base, rem = name[:idx].strip(), name[idx + 1 :].strip()
        if rem:
            parsed = _parse_parens_content(rem)
            if parsed and any(parsed.get(k) for k in ("dob", "dod", "pob", "pod")):
                for k in ("dob", "dod", "pob", "pod"):
                    if info[k] is None and parsed.get(k):
                        info[k] = parsed[k]
                name = base

    return name


def _extract_all_parens(text: str) -> tuple[str, dict]:
    """Remove all (...) from text, parsing each for date/place info."""
    info: dict = {"dob": None, "dod": None, "pob": None, "pod": None, "wohl_name": None}

    def _sub(m: re.Match) -> str:
        raw = m.group(1)
        if _WOHL_PREFIX_RE.match(raw):
            wn, di = _extract_wohl_content(raw)
            if wn is not None and info["wohl_name"] is None:
                info["wohl_name"] = wn
            for k in ("dob", "dod", "pob", "pod"):
                if info[k] is None and di.get(k):
                    info[k] = di[k]
        else:
            parsed = _parse_parens_content(raw)
            if parsed is not None:
                for k in ("dob", "dod", "pob", "pod"):
                    if info[k] is None and parsed.get(k):
                        info[k] = parsed[k]
        return ""

    cleaned = re.sub(r"\(([^)]*)\)", _sub, text)
    return re.sub(r"\s+", " ", cleaned).strip(" ,"), info


# ---------------------------------------------------------------------------
# Name-string helpers
# ---------------------------------------------------------------------------


def _is_name_token(tok: str) -> bool:
    return not re.match(r"^\d+$", tok) and bool(re.match(rf"^{_TOK}$", tok, re.UNICODE))


def _has_non_name_word(text: str) -> bool:
    return any(t in _NON_NAME for t in re.split(r"[\s,.()\[\]/]+", text.lower()) if t)


def _strip_honorifics(name: str) -> str:
    """Strip trailing honorifics like R.A., Sr., Jr."""

    def _is_hon(t: str) -> bool:
        t = t.lower()
        return bool(_HON_PATTERN.match(t)) or t in _HON_EXPLICIT

    while True:
        if "," in name:
            before, _, after = name.rpartition(",")
            a = after.strip()
            if a and " " not in a and _is_hon(a):
                name = before.strip()
                continue
        parts = name.rsplit(None, 1)
        if len(parts) == 2 and _is_hon(parts[1]):
            name = parts[0].strip()
            continue
        break
    return name


def _try_lastname_firstname(name: str) -> tuple[str, str] | None:
    """Parse "Lastname, Firstname(s)" → (firstname, lastname) or None."""
    if "," not in name:
        return None
    lastname_raw, _, firstname_raw = name.partition(",")
    lastname, firstname = lastname_raw.strip(), firstname_raw.strip()
    if not lastname or not firstname:
        return None

    ltoks = lastname.split()
    if len(ltoks) == 1:
        if not _is_name_token(ltoks[0]):
            return None
    elif len(ltoks) == 2 and ltoks[0].lower() in _COMPOUND_PREFIXES:
        if not _is_name_token(ltoks[1]):
            return None
    elif (
        len(ltoks) == 3
        and ltoks[0].lower() in _COMPOUND_PREFIXES
        and ltoks[1].lower() in _COMPOUND_PREFIXES
    ):
        if not _is_name_token(ltoks[2]):
            return None
    elif len(ltoks) == 2:
        if not all(_is_name_token(t) for t in ltoks) or any(
            t.lower() in _NON_NAME_CORE for t in ltoks
        ):
            return None
    else:
        return None

    ftoks = firstname.split()
    if not ftoks:
        return None
    for tok in ftoks:
        if (
            tok.lower() in _BAD_FIRSTNAME
            or not _is_name_token(tok)
            or re.match(r"^\d", tok)
        ):
            return None
    if _has_non_name_word(firstname):
        return None

    return firstname, lastname


def _is_simple_name(name: str) -> bool:
    """True for plain firstname-style entries: space-separated name tokens, no comma."""
    if "," in name:
        return False
    toks = name.split()
    return (
        bool(toks)
        and all(_is_name_token(t) for t in toks)
        and not any(t.lower() in _NON_NAME_CORE for t in toks)
        and not all(t.lower() in _NATIONALITIES for t in toks)
    )


# ---------------------------------------------------------------------------
# Main normalization
# ---------------------------------------------------------------------------


def _smart_case(name: str) -> str:
    def _cap(word):
        def _cap_part(p):
            pl = p.lower()
            if pl in ("d.ä.", "d.ä"):
                return "d.Ä."
            if pl in ("d.j.", "d.j"):
                return "d.J."
            # Strip spurious trailing dot from full names ("ERNST." → "Ernst", but "Fr." kept)
            if p.endswith(".") and "." not in p[:-1] and len(p) >= 6:
                p = p[:-1]
            if "." in p:
                return p[:1].upper() + p[1:]
            if p.isalpha() and len(p) >= 2 and _ROMAN_NUMERAL_RE.match(p):
                return p.upper()
            mc = re.match(r"^(mac|mc)(.{2,})$", p, re.IGNORECASE)
            if mc:
                pref, rest = p[: mc.end(1)], p[mc.end(1) :]
                return (
                    pref[:1].upper()
                    + pref[1:].lower()
                    + rest[:1].upper()
                    + rest[1:].lower()
                )
            return p[:1].upper() + p[1:].lower()

        return "-".join(_cap_part(part) for part in word.split("-"))

    words = name.split()
    return " ".join(
        (
            word.lower()
            if (word.lower() in _LOWERCASE_WORDS and i > 0)
            else (
                word.upper() + "."
                if (len(word) == 1 and word.isalpha())
                else _cap(word)
            )
        )
        for i, word in enumerate(words)
    )


def _normalize(auction_id: str, artist_raw: str) -> dict | None:
    name = artist_raw
    from_json = False

    # Handle JSON-wrapped entries: {"name": "Alexandre Noll", "source": "sothebys"}
    try:
        obj = json.loads(artist_raw)
        if isinstance(obj, dict) and "name" in obj:
            extracted = str(obj["name"]).strip() if obj["name"] else ""
            if not extracted:
                return None
            name = artist_raw = extracted
            from_json = True
    except (ValueError, TypeError):
        pass

    if _UNKNOWN_RE.match(name):
        return _matched(auction_id, artist_raw, None, None, None, None, None)

    name = re.sub(r"[?]+\s*$", "", name).strip()
    name = _ATTRIBUTION_STRIP.sub("", name)
    name = re.sub(r"^wohl\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[,\s]+wohl\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(
        r"[,\s]+zugeschr(?:ieben)?\.?\s*$", "", name, flags=re.IGNORECASE
    ).strip()
    name = re.sub(
        r"[,\s]+(?:called|genannt|alias|known\s+as|also\s+known\s+as)\s+\S.*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    name = re.sub(
        r"[,\s]+the\s+(?:elder|younger)\s*$", "", name, flags=re.IGNORECASE
    ).strip()

    # "d.i." = German "das ist" — discard everything before, keep what follows
    m = re.search(r"\bd\.\s*i\.\s+(.+)", name, re.IGNORECASE)
    if m:
        name = m.group(1).strip()

    name = _TITLE_PREFIX_RE.sub("", name)

    qualifier: str | None = None
    m = _ATTRIBUTION_QUALIFY.match(name)
    if m:
        qualifier = m.group(1).strip()
        name = name[m.end() :]

    name_suffix = ""
    sm = _NAME_SUFFIX_RE.search(name)
    if sm:
        letter = sm.group(1).upper()
        name_suffix = "d.Ä." if letter == "Ä" else "d.J."
        name = (name[: sm.start()] + name[sm.end() :]).strip().strip(",").strip()

    def _build(n: str) -> str:
        full = f"{qualifier} {n}" if qualifier else n
        # Insert space where abbreviation dots run into the next word ("M.O.Brüser" → "M.O. Brüser")
        full = re.sub(r"(?<=[A-Za-z]\.)(?=[A-Za-zÀ-ÖØ-öø-ÿ]{2})", " ", full)
        return _smart_case(full) + (" " + name_suffix if name_suffix else "")

    name, info = _extract_all_parens(name)
    name = _extract_inline_dates(name, info)

    # Resolve "ebenda"/"ebd."/"ebd" pod to pob (same place as birth)
    if info.get("pod") and info["pod"].lower().rstrip(".") in ("ebenda", "ebd"):
        info["pod"] = info["pob"]

    # Strip trailing standalone nationality — only visible after date extraction in some cases
    m = re.match(r"^(.*?),\s*([A-Za-z]+)\s*$", name, re.DOTALL)
    if m and m.group(2).lower() in _NATIONALITIES:
        name = m.group(1).strip()

    name = re.sub(
        r"\s+\d{1,2}(?:st|nd|rd|th)?\.?(?:/\d{1,2}(?:st|nd|rd|th)?\.?)?\s*"
        r"(?:century|jahrhundert\.?|jh\.?)\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()

    wohl_name = info.pop("wohl_name", None)
    name = _strip_honorifics(name)

    if wohl_name and name:
        merged = _merge_initials(name, wohl_name)
        if merged:
            name = merged

    def _null():
        return _matched(
            auction_id,
            artist_raw,
            None,
            info["dob"],
            info["dod"],
            info["pob"],
            info["pod"],
        )

    if _UNKNOWN_RE.match(name.strip()):
        return _null()

    if _COLLECTIVE_RE.search(name):
        return _matched(
            auction_id,
            artist_raw,
            _build(name.strip()),
            info["dob"],
            info["dod"],
            info["pob"],
            info["pod"],
        )

    name_toks = name.strip().split()
    if not name_toks or all(t.lower() in _NATIONALITIES for t in name_toks):
        return _null()

    _MULTI_ARTIST = {"and", "und", "or", "&", "sowie"}
    if any(
        t
        for t in re.split(r"[\s,.()\[\]/;]+", name.lower())
        if t and t in (_NON_NAME_CORE - _MULTI_ARTIST)
    ):
        return _null()

    parsed = _try_lastname_firstname(name)
    if parsed:
        firstname, lastname = parsed
        return _matched(
            auction_id,
            artist_raw,
            _build(f"{firstname} {lastname}"),
            info["dob"],
            info["dod"],
            info["pob"],
            info["pod"],
        )

    if _is_simple_name(name):
        return _matched(
            auction_id,
            artist_raw,
            _build(name),
            info["dob"],
            info["dod"],
            info["pob"],
            info["pod"],
        )

    if from_json:
        return None  # skip unmatched JSON entries (website navigation artifacts, etc.)
    return _unmatched(auction_id, artist_raw)


def _matched(auction_id, artist_raw, name, dob, dod, pob, pod) -> dict:
    return dict(
        matched=True,
        auction_artwork_id=auction_id,
        artist_raw_data=artist_raw,
        artist_full_name=name,
        date_of_birth_raw_data=dob,
        date_of_death_raw_data=dod,
        place_of_birth_raw_data=pob,
        place_of_death_raw_data=pod,
    )


def _unmatched(auction_id, artist_raw) -> dict:
    return dict(
        matched=False, auction_artwork_id=auction_id, artist_raw_data=artist_raw
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    _OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    matched: list[dict] = []
    unmatched: list[dict] = []

    with open(_INPUT, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result = _normalize(row["auction_artwork_id"], row["artist_raw_data"])
            if result is not None:
                (matched if result["matched"] else unmatched).append(result)

    with open(_OUT_JSONL, "w", encoding="utf-8") as f:
        for r in matched:
            r.pop("matched")
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with open(_OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["auction_artwork_id", "artist_raw_data"])
        w.writeheader()
        for r in unmatched:
            w.writerow(
                {
                    "auction_artwork_id": r["auction_artwork_id"],
                    "artist_raw_data": r["artist_raw_data"],
                }
            )

    total = len(matched) + len(unmatched)
    print(f"Matched:    {len(matched):>6}  ({len(matched) / total:.1%})")
    print(f"Unmatched:  {len(unmatched):>6}  ({len(unmatched) / total:.1%})")
    print(f"Total:      {total:>6}")


if __name__ == "__main__":
    main()
