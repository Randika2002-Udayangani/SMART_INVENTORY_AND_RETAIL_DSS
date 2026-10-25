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
from collections import namedtuple
from difflib import SequenceMatcher


# Numeric size unit embedded in a product name or query, e.g. "1kg", "500ml",
# "1 L", "2 lt".  Longer units are listed before their prefixes so "1 lt" is
# captured as one size token ("1l") rather than an "l" size plus a stray "t".
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|ml|lt|ltr|litre|litres|g|l)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")

# Generic filler words that carry no identifying signal on their own, so
# natural phrases like "1kg of sugar" score only on "1kg" + "sugar".
_STOPWORDS = {"of", "the", "a", "an", "and", "with", "for", "to", "in", "on", "pls", "please",
             "product", "products", "item", "items", "thing", "things"}

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


# ---------------------------------------------------------------------------
# Product-type resolution
# ---------------------------------------------------------------------------

_PRODUCT_TYPE_RULES = {
    "milk_powder": {
        "include_phrases": ["milk powder", "powdered milk"],
        "exclude_phrases": ["coconut", "chocolate", "cocoa", "malt"],
        "confidence": "high",
    },
    "liquid_milk": {
        "include_phrases": ["uht milk", "fresh milk", "liquid milk"],
        "exclude_phrases": ["powder", "chocolate", "coconut", "yoghurt", "yogurt"],
        "confidence": "high",
    },
    "yoghurt": {
        "include_phrases": ["yoghurt", "yogurt"],
        "exclude_phrases": ["drinking"],
        "confidence": "high",
    },
    "coconut_milk": {
        "include_phrases": ["coconut milk"],
        "exclude_phrases": [],
        "confidence": "high",
    },
    "chocolate_milk": {
        "include_phrases": ["chocolate milk"],
        "exclude_phrases": [],
        "confidence": "high",
    },
    "milk_chocolate": {
        "include_phrases": ["milk chocolate"],
        "exclude_phrases": [],
        "confidence": "high",
    },
    "drinking_yoghurt": {
        "include_phrases": ["drinking yoghurt", "drinking yogurt"],
        "exclude_phrases": [],
        "confidence": "medium",
    },
    "tea": {
        "include_phrases": ["tea"],
        "exclude_phrases": ["tea bag", "tea bags", "iced tea", "milk tea"],
        "confidence": "medium",
    },
    "cooking_oil": {
        "include_phrases": ["cooking oil", "vegetable oil", "sunflower oil", "coconut oil", "olive oil"],
        "exclude_phrases": [],
        "confidence": "high",
    },
    "rice": {
        "include_phrases": ["rice"],
        "exclude_phrases": ["rice flour", "rice snacks", "rice crackers", "rice noodles", "pasta"],
        "confidence": "medium",
    },
    "rice_pasta": {
        "include_phrases": ["rice pasta", "pasta rice"],
        "exclude_phrases": [],
        "confidence": "high",
    },
}

_BROAD_FAMILY_TYPES = {
    "milk_family": {"milk_powder", "liquid_milk", "coconut_milk", "chocolate_milk", "yoghurt", "drinking_yoghurt"},
}

_BRAND_PREFIX_ORDER = None


def set_brand_prefixes(brand_names):
    """Register known brand names for prefix-based brand detection."""
    global _BRAND_PREFIX_ORDER
    _BRAND_PREFIX_ORDER = sorted(
        {normalize_text(b) for b in brand_names if b},
        key=len,
        reverse=True,
    )


def resolve_query_type(query_text):
    """Classify a free-text query into a product type and operation.

    Returns a :class:`_QueryResolution` with the detected product type,
    confidence, brand, size, operation and remaining search terms.  When no
    specific type can be established, ``product_type`` is ``None`` and the
    caller should fall back to unrestricted name matching.
    """
    normalised = normalize_text(query_text)
    tokens = tokenize(query_text)

    product_type = _classify_product_type(normalised)
    if product_type is None and "milk product" in normalised:
        # A broad family query is validated against positively classified
        # dairy or milk-based records below; keyword presence alone is not enough.
        product_type = "milk_family"
    confidence = "medium" if product_type in _BROAD_FAMILY_TYPES else (
        _PRODUCT_TYPE_RULES[product_type]["confidence"] if product_type else None)
    brand = _detect_brand(normalised, tokens)
    size = _detect_size(tokens)
    operation = _detect_operation(normalised, tokens)
    search_terms = _remaining_search_terms(tokens, product_type, brand, size, operation)

    return _QueryResolution(
        product_type=product_type,
        confidence=confidence,
        brand=brand,
        size=size,
        operation=operation,
        search_terms=search_terms,
    )


def _classify_product_type(normalised_text):
    """Return the best-matching product type for a normalised string, or None.

    Uses whole-word matching (phrases must appear as complete words, not
    substrings) so "rice" never matches inside "price" and "tea" never
    matches inside "teapot".  Plurals are folded so "milk powders" matches
    the "milk powder" type.
    """
    words = set(tokenize(normalised_text))
    best = None
    for type_name, rule in _PRODUCT_TYPE_RULES.items():
        include_hit = False
        for phrase in rule["include_phrases"]:
            phrase_words = tokenize(phrase)
            # A single-word phrase must appear as a whole word.
            if len(phrase_words) == 1:
                if phrase_words[0] in words:
                    include_hit = True
                    break
            else:
                text_words = tokenize(normalised_text)
                for i in range(len(text_words) - len(phrase_words) + 1):
                    if text_words[i:i + len(phrase_words)] == phrase_words:
                        include_hit = True
                        break
                if include_hit:
                    break
        if not include_hit:
            continue
        exclude_hit = False
        for phrase in rule["exclude_phrases"]:
            phrase_words = tokenize(phrase)
            if len(phrase_words) == 1:
                if phrase_words[0] in words:
                    exclude_hit = True
                    break
            else:
                text_words = tokenize(normalised_text)
                for i in range(len(text_words) - len(phrase_words) + 1):
                    if text_words[i:i + len(phrase_words)] == phrase_words:
                        exclude_hit = True
                        break
                if exclude_hit:
                    break
        if exclude_hit:
            continue
        if best is None or _longer_include_phrase(rule, _PRODUCT_TYPE_RULES[best]):
            best = type_name
    return best


def _longer_include_phrase(rule_a, rule_b):
    """True when rule_a's longest include phrase is longer than rule_b's."""
    max_a = max((len(p) for p in rule_a["include_phrases"]), default=0)
    max_b = max((len(p) for p in rule_b["include_phrases"]), default=0)
    return max_a > max_b


def _detect_brand(normalised_text, tokens):
    """Detect a brand name as a prefix of the normalised text."""
    if _BRAND_PREFIX_ORDER is None:
        return None
    for brand in _BRAND_PREFIX_ORDER:
        if normalised_text.startswith(brand + " ") or normalised_text == brand:
            return brand
    return None


def _detect_size(tokens):
    """Return the first size token found in the query, e.g. '1kg', '400g'."""
    for token in tokens:
        if any(token.endswith(unit) for unit in ("kg", "g", "l", "ml")):
            if token[0].isdigit():
                return token
    return None


def _detect_operation(normalised_text, tokens):
    """Infer the requested operation from the query."""
    if any(w in normalised_text for w in ("cheapest", "lowest price", "best price")):
        return "cheapest"
    if any(w in normalised_text for w in ("compare", "versus", "vs", " or ")):
        return "compare"
    if any(w in normalised_text for w in ("price", "cost", "how much")):
        return "price"
    if any(w in normalised_text for w in ("stock", "available", "quantity")):
        return "stock"
    if any(w in normalised_text for w in ("sell", "best selling", "top selling")):
        return "sales"
    return "list"


def _remaining_search_terms(tokens, product_type, brand, size, operation):
    """Return tokens that should still be used for product-name matching."""
    consumed = set()
    if product_type in _PRODUCT_TYPE_RULES:
        rule = _PRODUCT_TYPE_RULES[product_type]
        for phrase in rule["include_phrases"]:
            for tok in tokenize(phrase):
                consumed.add(tok)
    if brand:
        consumed.update(tokenize(brand))
    if size:
        consumed.add(size)
    for op_word in ("cheapest", "compare", "price", "stock", "selling"):
        consumed.add(op_word)
    return [t for t in tokens if t not in consumed]


_QueryResolution = namedtuple(
    "_QueryResolution",
    ["product_type", "confidence", "brand", "size", "operation", "search_terms"],
)


def classify_product_by_name(product_name, category_name=None):
    """Classify a catalogue product by its name (and optional category).

    Returns (product_type, confidence) or (None, None) when the evidence is
    insufficient.  Category evidence raises confidence when it corroborates
    the name-based classification.

    Uses whole-word matching so "rice" never matches inside "price" etc.
    """
    normalised = normalize_text(product_name)
    words = set(normalised.split())
    cat_normalised = normalize_text(category_name) if category_name else ""
    cat_words = set(cat_normalised.split()) if cat_normalised else set()

    detected = None
    for type_name, rule in _PRODUCT_TYPE_RULES.items():
        include_hit = False
        for phrase in rule["include_phrases"]:
            phrase_words = phrase.split()
            if len(phrase_words) == 1:
                if phrase_words[0] in words:
                    include_hit = True
                    break
            else:
                text_words = normalised.split()
                for i in range(len(text_words) - len(phrase_words) + 1):
                    if text_words[i:i + len(phrase_words)] == phrase_words:
                        include_hit = True
                        break
                if include_hit:
                    break
        if not include_hit:
            continue
        exclude_hit = False
        for phrase in rule["exclude_phrases"]:
            phrase_words = phrase.split()
            if len(phrase_words) == 1:
                if phrase_words[0] in words:
                    exclude_hit = True
                    break
            else:
                text_words = normalised.split()
                for i in range(len(text_words) - len(phrase_words) + 1):
                    if text_words[i:i + len(phrase_words)] == phrase_words:
                        exclude_hit = True
                        break
                if exclude_hit:
                    break
        if exclude_hit:
            continue
        cat_corroborates = False
        if cat_normalised:
            for phrase in rule["include_phrases"]:
                phrase_words = phrase.split()
                if len(phrase_words) == 1:
                    if phrase_words[0] in cat_words:
                        cat_corroborates = True
                        break
                else:
                    cat_text_words = cat_normalised.split()
                    for i in range(len(cat_text_words) - len(phrase_words) + 1):
                        if cat_text_words[i:i + len(phrase_words)] == phrase_words:
                            cat_corroborates = True
                            break
                    if cat_corroborates:
                        break
        base_confidence = rule["confidence"]
        if cat_corroborates:
            base_confidence = "high" if base_confidence != "low" else "medium"
        if detected is None or _confidence_rank(base_confidence) > _confidence_rank(detected[1]):
            detected = (type_name, base_confidence)

    return detected if detected else (None, None)


def product_type_matches(product_name, category_name, product_type):
    """Check a catalogue row against a resolved subtype or broad family.

    Unclassified rows do not satisfy a type constraint: a shared keyword or a
    generic category is insufficient evidence.  This keeps uncertain rows out
    of narrow searches and comparisons until catalogue data is reviewed.
    """
    detected, _confidence = classify_product_by_name(product_name, category_name)
    if product_type in _BROAD_FAMILY_TYPES:
        return detected in _BROAD_FAMILY_TYPES[product_type]
    return detected == product_type


def _confidence_rank(confidence):
    return {"high": 3, "medium": 2, "low": 1}.get(confidence, 0)
