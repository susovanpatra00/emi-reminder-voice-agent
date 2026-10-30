"""Turn numbers and dates into the words a person would actually say.

The LLM never sees "12500" or "2026-11-05". It sees "twelve thousand five
hundred rupees" and "the fifth of November", so the TTS can't read them like a
form and the model can't reformat them wrongly.
"""

from __future__ import annotations

import re
from datetime import date

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
    7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth",
    13: "thirteenth", 14: "fourteenth", 15: "fifteenth", 16: "sixteenth",
    17: "seventeenth", 18: "eighteenth", 19: "nineteenth", 20: "twentieth",
    21: "twenty-first", 22: "twenty-second", 23: "twenty-third", 24: "twenty-fourth",
    25: "twenty-fifth", 26: "twenty-sixth", 27: "twenty-seventh", 28: "twenty-eighth",
    29: "twenty-ninth", 30: "thirtieth", 31: "thirty-first",
}

_MONTHS = [
    "", "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} hundred")
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def number_to_words(n: int) -> str:
    """Indian numbering: 125000 -> 'one lakh twenty-five thousand'."""
    if n == 0:
        return "zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append(f"{number_to_words(crore)} crore")
    if lakh:
        parts.append(f"{_below_hundred(lakh)} lakh")
    if thousand:
        parts.append(f"{_below_hundred(thousand)} thousand")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def rupees(amount: int | float) -> str:
    """12500 -> 'twelve thousand five hundred rupees'."""
    whole = int(round(amount))
    return f"{number_to_words(whole)} rupee{'s' if whole != 1 else ''}"


def spoken_date(d: date, today: date | None = None, with_weekday: bool = False) -> str:
    """date(2026, 11, 5) -> 'the fifth of November'.

    Relative words are used when they are more natural: 'today', 'tomorrow',
    'yesterday'. The year is only said when it differs from the current year.
    """
    if today is not None:
        delta = (d - today).days
        if delta == 0:
            return "today"
        if delta == 1:
            return "tomorrow"
        if delta == -1:
            return "yesterday"
    text = f"the {_ORDINALS[d.day]} of {_MONTHS[d.month]}"
    if with_weekday:
        text = f"{_WEEKDAYS[d.weekday()]}, {text}"
    if today is not None and d.year != today.year:
        text += f" {number_to_words(d.year)}"
    return text


def spoken_days(n: int) -> str:
    return f"{number_to_words(n)} day{'s' if n != 1 else ''}"


def spell_digits(digits: str) -> str:
    """'7712' -> 'seven seven one two' (how a person reads out an account ending)."""
    return " ".join(_ONES[int(c)] for c in digits if c.isdigit())


# ---------------------------------------------------------------------------
# Reading digits back from speech-to-text output
# ---------------------------------------------------------------------------

_DIGIT_WORDS = {
    # English
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "to": "2", "too": "2",
    "three": "3", "four": "4", "for": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9",
    # Hindi (romanised, as STT often returns it)
    "shunya": "0", "ek": "1", "do": "2", "teen": "3", "char": "4", "chaar": "4",
    "paanch": "5", "panch": "5", "chhe": "6", "che": "6", "chah": "6", "saat": "7",
    "aath": "8", "nau": "9",
    # Hindi (Devanagari)
    "शून्य": "0", "एक": "1", "दो": "2", "तीन": "3", "चार": "4", "पांच": "5", "पाँच": "5",
    "छह": "6", "छः": "6", "सात": "7", "आठ": "8", "नौ": "9",
}
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def extract_digits(text: str) -> str:
    """'four eight two one' / '48 21' / 'char aath do ek' -> '4821'.

    Handles 'double five' -> '55' since people say numbers that way.
    """
    text = text.translate(_DEVANAGARI_DIGITS).lower()
    tokens = re.findall(r"[0-9]+|[^\s\d,.;:!?\-\x27\"]+", text)
    out: list[str] = []
    repeat = 1
    for tok in tokens:
        if tok.isdigit():
            out.append(tok * repeat)
            repeat = 1
        elif tok in ("double", "dabal"):
            repeat = 2
        elif tok in ("triple", "tripal"):
            repeat = 3
        elif tok in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tok] * repeat)
            repeat = 1
    return "".join(out)
