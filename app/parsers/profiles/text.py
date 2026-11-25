"""Text helpers shared by the fingerprint and the reader."""

import re

_SPACES = re.compile(r"\s+")


def dedouble_word(word: str) -> str:
    """Undoes faux-bold text where every character is drawn twice
    ("TToottaall" -> "Total"), as Equity Bank's template does for some
    labels. A word only counts as doubled when it has an even length of 4+
    and every pair matches, so real words like "Mississippi" or "Tt" stay."""
    if len(word) < 4 or len(word) % 2:
        return word
    pairs = [word[i : i + 2] for i in range(0, len(word), 2)]
    if all(p[0] == p[1] for p in pairs):
        return "".join(p[0] for p in pairs)
    return word


def _doubled(word: str) -> bool:
    return len(word) >= 2 and len(word) % 2 == 0 and all(word[i] == word[i + 1] for i in range(0, len(word), 2))


def dedouble_line(words: list[str]) -> list[str]:
    """Faux-bold undone for a whole line - only when every word on it is
    doubled, so a real "5500" or "Ammaan" in a description is never
    shortened (ab-320)."""
    if (
        words
        and all(_doubled(w) for w in words)
        and any(len(w) >= 4 for w in words)
        # A label is always there ("TToottaall"); a line of numbers alone
        # ("5500") is real data.
        and any(c.isalpha() for w in words for c in w)
    ):
        return [w[::2] for w in words]
    return words


def normalise(text: str) -> str:
    """Lower-case, faux-bold undone, whitespace collapsed, trailing ':' and
    '.' dropped - the form layout words are compared and stored in."""
    words = [dedouble_word(w) for w in _SPACES.split(text.strip()) if w]
    return " ".join(words).lower().rstrip(":.").strip()
