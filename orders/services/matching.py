"""Normalised, deterministic text matching shared by the chatbot tools.

The model phrases queries the way a person talks ("milk powders", "1kg of
fortune oil", "nidoo") while catalogue rows contain pack sizes, punctuation,
accents and brand suffixes.  Every chatbot product lookup resolves through
this module instead of ad-hoc ``LIKE`` substrings, so exact matches always
win, case/spacing/plural variants behave consistently across every tool, and
a conservative typo tolerance never guesses an unrelated product.

The module is deliberately pure (no Django imports) so the rules are easy to
unit-test and cannot leak database access into the matcher.
"""

import re
import unicodedata
from difflib import SequenceMatcher


# Numeric size unit embedded in a product name or query, e.g. "1kg", "500ml",
# "1 L", "2 lt".  Longer units are listed before their prefixes so "1 lt" is
# captured as one size token ("1l") rather than an "l" size plus a stray "t".
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|ml|lt|ltr|litre|litres|g|l)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")

# Generic filler words that carry no identifying signal on their own, so
# natural phrases like "1kg of sugar" score only on "1kg" + "sugar".
_STOPWORDS = {"of", "the", "a", "an", "and", "with", "for", "to", "in", "on", "pls", "please"}

_SIZE_UNITS = {
    "kg": "kg", "ml": "ml", "lt": "l", "ltr": "l",
    "litre": "l", "litres": "l", "g": "g", "l": "l",
}

# A fuzzy (typo-tolerant) token comparison only applies when both tokens are
# long enough for the similarity ratio to be meaningful, and the ratio itself
# is high ("choclate" ~ "chocolate", "poweder" ~ "powder").  Short or generic
# words therefore can never cross-match unrelated products ("milk" ~ "milo").
_FUZZY_MIN_LENGTH = 4
_FUZZY_MIN_RATIO = 0.84


def _strip_accents(text):
    """Decompose Unicode and drop combining marks (Nestlé → nestle)."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_text(text):
    """Lowercase, strip accents/punctuation and collapse whitespace.

    Used for whole-string comparisons (e.g. an exact product-name match that
    must tolerate "MILO's", double spaces or "Nestlé" vs "Nestle").
    """
    if text is None:
        return ""
    text = _strip_accents(str(text)).lower()
    return " ".join(_WORD_RE.findall(text))


def _fold_plural(word):
    """Naive, symmetric singular folding: "powders" → "powder".

    Applied to both query and catalogue tokens, so a mismatch can only happen
    when the catalogue word itself is unusual; words ending in "ss" (e.g.
    "glass") and words of three characters or fewer are left untouched.
    """
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text):
    """Split *text* into normalised, comparable tokens.

    Size expressions such as ``1kg`` / ``1 L`` collapse into a single token
    (``1l``), case is folded, accents are stripped, generic stopwords and
    single-character fragments (including possessive "'s") are dropped, and
    simple plurals are folded so "milk powders" tokenises the same way as
    "Milk Powder".
    """
    if not text:
        return []
    text = _strip_accents(str(text)).lower()
    tokens = [f"{m.group(1)}{_SIZE_UNITS[m.group(2)]}" for m in _SIZE_RE.finditer(text)]
    text = _SIZE_RE.sub(" ", text)
    for word in _WORD_RE.findall(text):
        if word in _STOPWORDS or len(word) < 2:
            continue
        tokens.append(_fold_plural(word))
    return tokens


def tokens_match(left, right):
    """True when two tokens are equal or conservatively typo-similar."""
    if left == right:
        return True
    if len(left) < _FUZZY_MIN_LENGTH or len(right) < _FUZZY_MIN_LENGTH:
        return False
    return SequenceMatcher(None, left, right).ratio() >= _FUZZY_MIN_RATIO


def tokens_cover(query_tokens, candidate_tokens):
    """True when every query token is matched (exactly or fuzzily)."""
    return all(any(tokens_match(token, other) for other in candidate_tokens)
               for token in query_tokens)


def score_tokens(query_tokens, name_tokens, aux_tokens=()):
    """Rank how well *query_tokens* describe one product.

    *name_tokens* come from the product name; *aux_tokens* from its brand and
    category words.  Returns ``None`` when any query token is uncovered (the
    product is not a match at all).  Otherwise a tuple that sorts best-first:

      (exact name hits, fuzzy name hits, exact aux hits, fuzzy aux hits,
       -surplus name tokens)

    Exact name matches always beat fuzzy ones, which beat matches that only
    land on brand/category words — so "milk powder" can never prefer a
    chocolate malt drink merely because its category text is similar, and the
    most specific product (fewest surplus words) surfaces first.  An empty
    query scores neutral zeros, meaning "no preference" for list-style calls.
    """
    if not query_tokens:
        return (0, 0, 0, 0, 0)
    name_set = set(name_tokens)
    aux_set = set(aux_tokens)
    exact = fuzzy = aux_exact = aux_fuzzy = 0
    for token in query_tokens:
        if token in name_set:
            exact += 1
        elif any(tokens_match(token, other) for other in name_tokens):
            fuzzy += 1
        elif token in aux_set:
            aux_exact += 1
        elif any(tokens_match(token, other) for other in aux_tokens):
            aux_fuzzy += 1
        else:
            return None
    surplus = max(0, len(name_tokens) - len(query_tokens))
    return (exact, fuzzy, aux_exact, aux_fuzzy, -surplus)
