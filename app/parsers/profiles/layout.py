"""Page geometry shared by the fingerprint and the reader: words grouped
into lines, lines into x-clusters, and the date / money token shapes."""

import re
from dataclasses import dataclass

from app.parsers.profiles.text import dedouble_line

DATE_TOKEN = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}$")
MONEY_TOKEN = re.compile(r"^[-(]?[\d,]+\.\d{2}\)?(?:DR|CR)?$", re.IGNORECASE)
# A gap wider than this between two words starts a new cluster (column).
CLUSTER_GAP = 6.0


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float


@dataclass
class Line:
    words: list[Word]

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def has_digits(self) -> bool:
        return any(ch.isdigit() for w in self.words for ch in w.text)


@dataclass
class Cluster:
    words: list[Word]

    @property
    def x0(self) -> float:
        return self.words[0].x0

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def page_lines(page, *, y_tolerance: float = 3.0) -> list[Line]:
    """pdfplumber words, faux-bold undone, grouped into lines top to bottom
    (words whose tops are within y_tolerance of the line's first word)."""
    words = [
        Word(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"])
        for w in page.extract_words(keep_blank_chars=False, use_text_flow=False)
    ]
    words.sort(key=lambda w: (w.top, w.x0))
    lines: list[Line] = []
    for word in words:
        if lines and abs(word.top - lines[-1].words[0].top) <= y_tolerance:
            lines[-1].words.append(word)
        else:
            lines.append(Line(words=[word]))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
        for word, text in zip(line.words, dedouble_line([w.text for w in line.words]), strict=True):
            word.text = text
    return lines


def clusters(line: Line, *, gap: float = CLUSTER_GAP) -> list[Cluster]:
    """The line split wherever two words are more than `gap` apart."""
    out: list[Cluster] = []
    for word in line.words:
        if out and word.x0 - out[-1].x1 <= gap:
            out[-1].words.append(word)
        else:
            out.append(Cluster(words=[word]))
    return out


def is_body_line(line: Line) -> bool:
    """A transaction row: it holds a date-shaped and a money-shaped token."""
    texts = [w.text for w in line.words]
    return any(DATE_TOKEN.match(t) for t in texts) and any(MONEY_TOKEN.match(t) for t in texts)


def money_words(line: Line) -> list[Word]:
    return [w for w in line.words if MONEY_TOKEN.match(w.text)]
