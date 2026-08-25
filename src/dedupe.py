"""Deduplication.

This is not a filter. One story arriving from five feeds is one story, not
five. Nothing is removed on grounds of relevance, interest or volume — only
identity.

Order matters and is the whole point:

    1. dedupe within a language   (cheap string work, no API cost)
    2. translate the survivors    (classify.py)
    3. dedupe across languages    (on the English titles)

Run cross-language dedup before translation and it cannot work — fuzzy title
matching does not survive a language boundary. Run within-language dedup after
translation and you pay to translate the same headline five times.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

# Titles that survived a previous run are remembered so a story trickling in
# over three days does not appear three times.
SEEN_TTL_DAYS = 5

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")

# Publisher suffixes that break otherwise-identical titles apart.
_TAIL = re.compile(
    r"\s*[-–—|·]\s*[^-–—|·]{2,40}$"
)

_STOP = {
    "en": {"the", "a", "an", "of", "in", "on", "for", "to", "and", "is", "as", "at"},
    "es": {"el", "la", "los", "las", "de", "del", "en", "y", "un", "una", "por", "para"},
    "pt": {"o", "a", "os", "as", "de", "do", "da", "em", "e", "um", "uma", "para"},
    "ar": {"في", "من", "على", "إلى", "عن", "الذي", "التي", "مع", "أن", "هذا"},
}


def normalise(title: str, lang: str = "en") -> str:
    """Strip a title down to something comparable."""
    text = _TAIL.sub("", title)
    if lang == "ar":
        # Compose first: NFKD splits the hamza off an alif, and the loose
        # hamza then gets punctuated into a space, tearing the word in half.
        text = unicodedata.normalize("NFC", text)
        text = re.sub(r"[\u064B-\u0652\u0640\u0653-\u0655\u0670]", "", text)
        text = re.sub(r"[إأآٱا]", "ا", text)                 # alif variants
        text = text.replace("ى", "ي").replace("ة", "ه")
        text = text.replace("ؤ", "و").replace("ئ", "ي")
    else:
        # Latin scripts: decompose and drop the accents.
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", text.lower())
    words = [w for w in _SPACE.split(text) if w and w not in _STOP.get(lang, set())]
    return " ".join(words)


def _collapse(items, threshold: int, key_lang):
    """Greedy clustering. First item in a cluster wins; the rest record it."""
    kept: list = []
    keys: list[str] = []

    for item in items:
        lang = key_lang(item)
        title = item.title_en if lang == "_en" else item.title
        key = normalise(title, "en" if lang == "_en" else lang)
        if not key:
            kept.append(item)
            keys.append(key)
            continue

        match_idx = None
        for i, existing in enumerate(keys):
            if not existing:
                continue
            if fuzz.token_set_ratio(key, existing) >= threshold:
                match_idx = i
                break

        if match_idx is None:
            kept.append(item)
            keys.append(key)
        else:
            winner = kept[match_idx]
            dupes = getattr(winner, "duplicates", None)
            if dupes is None:
                winner.duplicates = []
            winner.duplicates.append(item.source_id)

    return kept


def within_language(items, threshold: int = 88):
    """Stage 1 — collapse duplicates inside each language."""
    by_lang: dict[str, list] = {}
    for item in items:
        by_lang.setdefault(item.lang, []).append(item)

    out = []
    for lang, group in by_lang.items():
        out.extend(_collapse(group, threshold, key_lang=lambda i: i.lang))
    return out


def across_languages(items, threshold: int = 84):
    """Stage 3 — collapse the same story told in two languages.

    Threshold is looser than stage 1: machine-translated titles of the same
    event agree on substance and disagree on wording.
    """
    translated = [i for i in items if i.title_en]
    untranslated = [i for i in items if not i.title_en]
    kept = _collapse(translated, threshold, key_lang=lambda i: "_en")
    return kept + untranslated


def drop_seen(items, seen: dict) -> list:
    """Remove items already delivered in an earlier report."""
    return [i for i in items if i.doc_id not in seen]
