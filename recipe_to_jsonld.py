#!/usr/bin/python3

"""
Convert recipe web pages into schema.org Recipe JSON-LD using the
`recipe-scrapers` library and optionally upload to a Nextcloud Cookbook instance.

Install:
    pip install recipe-scrapers

Usage:
    python recipe_to_jsonld.py --url "https://example.com/some-recipe"
    python recipe_to_jsonld.py --url "https://example.com/some-recipe" --nextcloud-url "https://nextcloud.example.com" --nextcloud-user "myuser" --nextcloud-pass "mypass"
    python recipe_to_jsonld.py --file saved_page.html --url "https://example.com/some-recipe" --out output.html
"""

import argparse
import base64
import json
import re
import sys
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from nextcloud_cookbook_api.client import CookbookClient
from nextcloud_cookbook_api.models import Nutrition, Recipe

from recipe_scrapers import scrape_html
from recipe_scrapers._exceptions import RecipeScrapersExceptions

from urllib.error import URLError, HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Rough patterns for splitting an ingredient line into distinct ingredients.
# Old-style recipe pages (no markup) often jam several ingredients into one
# paragraph or table cell, separated by line breaks that get flattened to
# whitespace, or by a quantity+unit pattern repeating inline.
UNIT_PATTERN = re.compile(
    r"(?<![/\d])(?=(?:\d+[\d\s/.,]*|[\u00bd\u00bc\u00be\u2153\u2154\u215b\u215c\u215d\u215e])"
    r"\s*(?:g|kg|ml|l|tsp|tbsp|tbs|cup|cups|oz|lb|lbs)\b)",
    re.IGNORECASE,
)

# Filenames/paths that usually indicate a non-content image: nav icons,
# ads, tracking pixels, logos, spacers, social share buttons, etc.
IMAGE_SKIP_PATTERN = re.compile(
    r"(logo|icon|spacer|pixel|blank|banner|advert|sprite|button|social|"
    r"share|avatar|placeholder|badge|rating-star|arrow|nav[-_]|header|footer)",
    re.IGNORECASE,
)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def iso8601_duration(minutes):
    """Convert an integer number of minutes into an ISO 8601 duration (e.g. PT35M)."""
    if minutes is None:
        return None
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return None
    hours, mins = divmod(minutes, 60)
    parts = "PT"
    if hours:
        parts += f"{hours}H"
    if mins or not hours:
        parts += f"{mins}M"
    return parts


FRACTION_MAP = {
    "\u00bc": "1/4",
    "\u00bd": "1/2",
    "\u00be": "3/4",
    "\u2150": "1/7",
    "\u2151": "1/9",
    "\u2152": "1/10",
    "\u2153": "1/3",
    "\u2154": "2/3",
    "\u2155": "1/5",
    "\u2156": "2/5",
    "\u2157": "3/5",
    "\u2158": "4/5",
    "\u2159": "1/6",
    "\u215a": "5/6",
    "\u215b": "1/8",
    "\u215c": "3/8",
    "\u215d": "5/8",
    "\u215e": "7/8",
}
FRACTION_PATTERN = re.compile(r"(\d)?(" + "|".join(FRACTION_MAP.keys()) + ")")


def normalize_fractions(text):
    """Replace vulgar fraction symbols (e.g. ¼, ½, ¾) with plain ASCII
    equivalents (1/4, 1/2, 3/4). If the symbol directly follows a whole
    number (e.g. "1½"), a space is inserted so it reads as "1 1/2"."""
    if not text:
        return text

    text = re.sub(
        r"(\d+(?:\.\d+)?\s*(?:g|kg|ml|l|cm))\s*"
        r"\([^)]*\b(?:cup|cups|tbsp|tbs|tablespoons?|tsp|teaspoons?|oz|ounces?|lb|lbs|pounds?|inch|inches|in)\b[^)]*\)",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )

    def repl(match):
        whole, frac_char = match.group(1), match.group(2)
        frac = FRACTION_MAP[frac_char]
        return f"{whole} {frac}" if whole else frac

    return FRACTION_PATTERN.sub(repl, text)


def normalize_fractions_deep(obj):
    """Recursively apply normalize_fractions to every string value in a
    JSON-like structure (dict/list/str), leaving other types untouched."""
    if isinstance(obj, str):
        return normalize_fractions(obj)
    if isinstance(obj, dict):
        return {k: normalize_fractions_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_fractions_deep(v) for v in obj]
    return obj


# --- Imperial -> metric normalization -------------------------------------
#
# Scope, deliberately: weight (oz/lb <-> g/kg), length (in/inch <-> cm/mm),
# volume (cup/tbsp/tsp <-> ml), and oven temperature (°F <-> °C). Cup and
# tbsp/tsp conversions use fixed volumetric factors (not ingredient-density-
# aware), so a cup of flour and a cup of milk convert identically -- fine
# for consistency, imprecise for baking accuracy.

QTY_TOKEN = r"\d+\s\d+/\d+|\d+/\d+|\d+(?:\.\d+)?"

METRIC_UNITS = r"(?:kilograms?|kg|grams?|g|millilit(?:re|er)s?|ml|lit(?:re|er)s?|l|centimet(?:re|er)s?|cm|millimet(?:re|er)s?|mm)"
IMPERIAL_UNITS_CHAIN = r"(?:pounds?|lbs?|ounces?|oz|cups?|inches|inch|in|tablespoons?|tbsp|tbs|teaspoons?|tsp)"
IMPERIAL_UNITS_STANDALONE = r"(?:pounds?|lbs?|ounces?|oz|cups?|inches|inch|tablespoons?|tbsp|tbs|teaspoons?|tsp)"

_CHAIN_TOKEN = rf"(?:{QTY_TOKEN})\s?(?:{METRIC_UNITS}|{IMPERIAL_UNITS_CHAIN})\b"
CHAIN_RE = re.compile(rf"{_CHAIN_TOKEN}(?:\s*/\s*{_CHAIN_TOKEN})+", re.IGNORECASE)
STANDALONE_RE = re.compile(rf"(?:{QTY_TOKEN})\s?{IMPERIAL_UNITS_STANDALONE}\b", re.IGNORECASE)

_TOKEN_SPLIT_RE = re.compile(rf"^({QTY_TOKEN})\s?({METRIC_UNITS}|{IMPERIAL_UNITS_CHAIN})$", re.IGNORECASE)
_STANDALONE_SPLIT_RE = re.compile(rf"^({QTY_TOKEN})\s?({IMPERIAL_UNITS_STANDALONE})$", re.IGNORECASE)

TEMP_C = r"(\d+)\s?\u00b0?\s?C\b"
TEMP_F = r"(\d+)\s?\u00b0?\s?F\b"
CF_PAIR_RE = re.compile(rf"{TEMP_C}\s*[/(]\s*{TEMP_F}\)?")
FC_PAIR_RE = re.compile(rf"{TEMP_F}\s*[/(]\s*{TEMP_C}\)?")
LONE_F_RE = re.compile(TEMP_F)


def parse_quantity(qty_str):
    """Parse '6', '1 1/2', or '3/4' into a float. Returns None for anything
    unparseable (e.g. ranges like '6-8'), so the caller can leave those alone."""
    qty_str = qty_str.strip()
    m = re.match(r"^(\d+)\s(\d+)/(\d+)$", qty_str)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.match(r"^(\d+)/(\d+)$", qty_str)
    if m:
        return int(m.group(1)) / int(m.group(2))
    try:
        return float(qty_str)
    except ValueError:
        return None


def round_metric(value, unit):
    """Round a converted metric value to something recipe-realistic rather
    than a long decimal. Small quantities keep one decimal place so, e.g.,
    a converted 1/4 tsp doesn't collapse to '0g'/'1g' and lose its meaning."""
    if unit in ("g", "ml"):
        if value >= 100:
            return int(round(value / 5) * 5)
        if value >= 5:
            return int(round(value))
        return round(value * 2) / 2  # nearest 0.5
    if unit in ("kg", "l"):
        return round(value, 2)
    if unit == "cm":
        return round(value * 2) / 2  # nearest 0.5 cm
    return round(value, 1)


LIQUID_KEYWORDS = (
    "oil", "milk", "water", "juice", "vinegar", "wine", "beer", "stock", "broth",
    "cream", "syrup", "sauce", "extract", "honey", "buttermilk", "yogurt", "yoghurt",
    "molasses", "treacle", "liqueur", "brandy", "rum", "vanilla",
)


LIQUID_KEYWORD_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in LIQUID_KEYWORDS) + r")\b", re.IGNORECASE
)


def is_liquid_ingredient(context_text):
    """Best-effort check of whether an ingredient line describes a liquid
    (oil, milk, stock, etc.), based on keywords in its text. Used to decide
    whether a tsp/tbsp/cup measurement converts to ml (liquid) or g (dry).

    Matches on whole words only -- a naive substring check would (and
    once did) match "rum" inside "crumbs", misclassifying breadcrumbs (and
    anything else in that same ingredient string, e.g. a pinch of salt
    alongside them) as a liquid and converting them to ml instead of g."""
    if not context_text:
        return False
    return bool(LIQUID_KEYWORD_PATTERN.search(context_text))


# Rough weight-per-cup for common dry ingredients (grams). "Cup" is a volume
# unit, so a true weight conversion depends entirely on what's being
# measured -- a cup of packed leafy herbs weighs a fraction of a cup of
# grated cheese or granulated sugar. These are ballpark kitchen-reference
# figures, not exact for any specific product/brand.
CUP_WEIGHT_TABLE = [
    (("basil", "parsley", "cilantro", "coriander leaves", "mint", "spinach",
      "arugula", "rocket", "lettuce", "fresh herbs"), 30),
    (("cheese", "mozzarella", "cheddar", "parmesan", "feta"), 110),
    (("olives",), 150),
    (("flour",), 120),
    (("brown sugar",), 220),
    (("sugar",), 200),
    (("rice",), 185),
    (("oats",), 90),
    (("breadcrumbs", "panko"), 60),
    (("nuts", "almonds", "walnuts", "pecans", "cashews", "pine nuts"), 120),
    (("butter",), 227),
]
DEFAULT_CUP_WEIGHT_G = 120  # generic fallback for unmatched dry ingredients


def estimate_cup_weight_grams(qty, context):
    """Look up a rough grams-per-cup figure based on ingredient keywords in
    the surrounding context text, falling back to a generic default."""
    lower = (context or "").lower()
    for keywords, grams_per_cup in CUP_WEIGHT_TABLE:
        if any(kw in lower for kw in keywords):
            return qty * grams_per_cup
    return qty * DEFAULT_CUP_WEIGHT_G


def convert_imperial_token(qty_str, unit, context=""):
    """Convert a single imperial quantity+unit to its metric equivalent
    string. Returns the original text unchanged if the quantity can't be
    parsed (e.g. a range like '6-8 oz'). `context` is the surrounding
    ingredient text, used only to decide ml-vs-g for tsp/tbsp/cup."""
    original = f"{qty_str} {unit}".strip()
    qty = parse_quantity(qty_str)
    if qty is None:
        return original

    unit_l = unit.lower()
    if unit_l in ("oz", "ounce", "ounces"):
        grams = qty * 28.3495
        return f"{round_metric(grams, 'g')}g"
    if unit_l in ("lb", "lbs", "pound", "pounds"):
        grams = qty * 453.592
        if grams >= 1000:
            return f"{round_metric(grams / 1000, 'kg')}kg"
        return f"{round_metric(grams, 'g')}g"
    if unit_l in ("cup", "cups"):
        # Cup is a volume unit, so weight depends on what's being measured.
        # Liquids convert cleanly to ml; dry/solid ingredients (herbs,
        # cheese, olives, flour, ...) get a rough weight-per-cup estimate
        # instead, since "355ml grated cheese" isn't a meaningful quantity
        # for a dry ingredient.
        if is_liquid_ingredient(context):
            ml = qty * 236.588
            if ml >= 1000:
                return f"{round_metric(ml / 1000, 'l')}l"
            return f"{round_metric(ml, 'ml')}ml"
        grams = estimate_cup_weight_grams(qty, context)
        if grams >= 1000:
            return f"{round_metric(grams / 1000, 'kg')}kg"
        return f"{round_metric(grams, 'g')}g"
    if unit_l in ("tbsp", "tbs", "tablespoon", "tablespoons"):
        # tsp/tbsp are genuinely volume units. For liquids (oil, milk,
        # stock, extract, ...) the honest conversion is ml. For dry
        # ingredients (spices, salt, sugar, ...) recipe charts commonly
        # approximate weight as roughly 1:1 with volume (~15g per tbsp) --
        # a reasonable approximation for many spices/salt/sugar, less so
        # for very light (herbs) or very dense (honey) ingredients.
        if is_liquid_ingredient(context):
            ml = qty * 14.7868
            return f"{round_metric(ml, 'ml')}ml"
        grams = qty * 14.7868
        return f"{round_metric(grams, 'g')}g"
    if unit_l in ("tsp", "teaspoon", "teaspoons"):
        if is_liquid_ingredient(context):
            ml = qty * 4.92892
            return f"{round_metric(ml, 'ml')}ml"
        grams = qty * 4.92892
        return f"{round_metric(grams, 'g')}g"
    if unit_l in ("in", "inch", "inches"):
        cm = qty * 2.54
        return f"{round_metric(cm, 'cm')}cm"
    return original


def _process_chain(match):
    """Handle a run of qty+unit tokens joined by '/' (e.g. '175 g/6 oz',
    '18 cm / 7 in'). Keep only the metric token(s); if the whole chain is
    imperial with no metric alternative given, convert the first token."""
    tokens = [t.strip() for t in re.split(r"\s*/\s*", match.group(0))]
    metric_tokens, imperial_tokens = [], []
    for t in tokens:
        m = _TOKEN_SPLIT_RE.match(t)
        if not m:
            metric_tokens.append(t)  # unparsed segment: keep as-is, don't drop data
            continue
        qty_str, unit = m.group(1), m.group(2)
        if re.fullmatch(METRIC_UNITS, unit, re.IGNORECASE):
            metric_tokens.append(t)
        else:
            imperial_tokens.append((qty_str, unit))
    if metric_tokens:
        return "/".join(metric_tokens)
    if imperial_tokens:
        return convert_imperial_token(*imperial_tokens[0], context=match.string)
    return match.group(0)


def _process_standalone(match):
    m = _STANDALONE_SPLIT_RE.match(match.group(0))
    if not m:
        return match.group(0)
    return convert_imperial_token(m.group(1), m.group(2), context=match.string)


def normalize_measurements(text):
    """Strip redundant imperial units when a metric equivalent is already
    given (e.g. '175 g/6 oz' -> '175 g'), and convert imperial-only
    quantities (weight, length, volume, oven temperature) to metric when no
    metric alternative is present at all."""
    if not text:
        return text

    # Oven temperature: prefer/keep Celsius when both are given; convert a
    # lone Fahrenheit reading when no Celsius figure appears alongside it.
    text = CF_PAIR_RE.sub(lambda m: f"{m.group(1)}\u00b0C", text)
    text = FC_PAIR_RE.sub(lambda m: f"{m.group(2)}\u00b0C", text)
    text = LONE_F_RE.sub(lambda m: f"{round(( int(m.group(1)) - 32) * 5 / 9 / 5) * 5}\u00b0C", text)

    # Weight/length/volume: metric/imperial pairs joined by '/', then any
    # remaining standalone imperial-only quantities.
    text = CHAIN_RE.sub(_process_chain, text)
    text = STANDALONE_RE.sub(_process_standalone, text)

    return text


LEADING_DESCRIPTOR_RE = re.compile(
    r"^([A-Za-z][A-Za-z\s]{2,40}?)\s+of\s+"
    r"(\d+(?:\s\d+/\d+|/\d+)?)\s+"
    r"(.+)$"
)


def reorder_leading_descriptor(text):
    """
    Some ingredient phrasing puts the quantity in the middle of the
    sentence rather than at the start (e.g. 'Grated rind of 1 lemon',
    'Juice of 2 limes'). Tools that need a leading 'amount unit ingredient'
    format -- e.g. Nextcloud Cookbook's serving-size recalculation --
    can't parse those and will flag a syntax error. This moves the
    quantity to the front: 'Grated rind of 1 lemon' -> '1 lemon, grated
    rind'.
    """
    if not text:
        return text
    m = LEADING_DESCRIPTOR_RE.match(text.strip())
    if not m:
        return text
    descriptor, qty, rest = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    return f"{qty} {rest}, {descriptor.lower()}"


def normalize_ingredient_phrasing(ingredients):
    """Apply reorder_leading_descriptor to a recipeIngredient list."""
    if not isinstance(ingredients, list):
        return ingredients
    return [reorder_leading_descriptor(i) if isinstance(i, str) else i for i in ingredients]


NUMBER_START_RE = re.compile(r"^\s*\d")


NO_QUANTITY_MARKERS = ("to taste", "as needed", "for frying", "for serving", "optional")


def ensure_leading_quantity(ingredients):
    """
    Tools that recalculate ingredient amounts by serving size (e.g.
    Nextcloud Cookbook) require every ingredient line to start with a
    parseable amount. Lines with no quantity at all -- e.g. 'Fresh
    parsley, chopped' or 'Lemon juice' -- get a nominal '1 ' prefix so
    they parse as 'amount ingredient', even though '1' isn't a precise
    measurement for something like a garnish. Group-heading lines (e.g.
    'To serve:', marked with a trailing colon) are left alone since
    they aren't ingredients at all.

    Lines that already say the quantity is intentionally open-ended (e.g.
    'canola oil, as needed') are also left alone: unlike a garnish, where
    '1' is at least a plausible whole-item guess, prefixing '1' onto '...,
    as needed' is self-contradictory rather than merely imprecise.
    """
    if not isinstance(ingredients, list):
        return ingredients
    result = []
    for item in ingredients:
        if isinstance(item, str):
            stripped = item.strip()
            already_open_ended = any(marker in stripped.lower() for marker in NO_QUANTITY_MARKERS)
            if (
                stripped
                and not NUMBER_START_RE.match(stripped)
                and not stripped.endswith(":")
                and not already_open_ended
            ):
                item = f"1 {stripped}"
        result.append(item)
    return result


def normalize_measurements_deep(obj):
    """Recursively apply normalize_measurements to every string value in a
    JSON-like structure (dict/list/str), leaving other types untouched."""
    if isinstance(obj, str):
        return normalize_measurements(obj)
    if isinstance(obj, dict):
        return {k: normalize_measurements_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_measurements_deep(v) for v in obj]
    return obj


def clean_step_text(text):
    """Collapse embedded newline-plus-whitespace runs (e.g. from multi-line
    source markup) into a single space, so HowToStep text reads as one
    continuous line."""
    if not text:
        return text
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def safe(fn, default=None):
    """Call a scraper method, swallowing errors for fields a given site doesn't provide."""
    try:
        result = fn()
        return result if result not in ("", [], {}) else default
    except (RecipeScrapersExceptions, AttributeError, NotImplementedError, Exception):
        return default


def build_recipe_jsonld(scraper, canonical_url=None):
    """Map a recipe_scrapers scraper instance onto a schema.org Recipe dict."""

    instructions_list = safe(scraper.instructions_list, [])
    if instructions_list:
        instructions = [
            {"@type": "HowToStep", "text": clean_step_text(step)} for step in instructions_list
        ]
    else:
        raw = safe(scraper.instructions, "")
        instructions = [
            {"@type": "HowToStep", "text": clean_step_text(line)}
            for line in raw.split("\n")
            if line.strip()
        ]

    images = safe(scraper.image)
    image_list = images if isinstance(images, list) else ([images] if images else [])

    author = safe(scraper.author)
    author_obj = {"@type": "Person", "name": author} if author else None

    ratings_value = safe(scraper.ratings)
    ratings_count = safe(scraper.ratings_count)
    aggregate_rating = None
    if ratings_value:
        aggregate_rating = {
            "@type": "AggregateRating",
            "ratingValue": ratings_value,
        }
        if ratings_count:
            aggregate_rating["ratingCount"] = ratings_count

    nutrients = safe(scraper.nutrients)
    nutrition = None
    if nutrients:
        nutrition = {"@type": "NutritionInformation", **nutrients}

    recipe = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": safe(scraper.title),
        "description": safe(scraper.description),
        "author": author_obj,
        "image": image_list,
        "recipeYield": safe(scraper.yields),
        "prepTime": iso8601_duration(safe(scraper.prep_time)),
        "cookTime": iso8601_duration(safe(scraper.cook_time)),
        "totalTime": iso8601_duration(safe(scraper.total_time)),
        "recipeCategory": safe(scraper.category),
        "recipeCuisine": safe(scraper.cuisine),
        "keywords": safe(scraper.keywords),
        "recipeIngredient": safe(scraper.ingredients, []),
        "recipeInstructions": instructions,
        "aggregateRating": aggregate_rating,
        "nutrition": nutrition,
        "url": canonical_url or safe(scraper.canonical_url),
    }

    # Drop keys that are None / empty so the JSON-LD stays clean
    return {k: v for k, v in recipe.items() if v not in (None, "", [], {})}


def fetch_html(url):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    return urlopen(request).read().decode("utf-8", errors="replace")


def split_ingredient_block(text):
    """Split a chunk of run-together ingredient text into individual ingredients."""
    # First split on actual line breaks (from <br> tags or literal newlines).
    lines = [l.strip(" -\u2022\t") for l in text.split("\n") if l.strip()]
    if not lines:
        return []

    # Each resulting line may itself contain several ingredients crammed
    # together with no delimiter at all (common when a site's markup only
    # has a <br> here and there) -- split those further on quantity+unit
    # boundaries.
    result = []
    for line in lines:
        pieces = [p.strip(" ,") for p in UNIT_PATTERN.split(line) if p.strip(" ,")]
        result.extend(pieces if len(pieces) > 1 else [line])
    return result


def find_best_image(soup, url=None):
    """
    Best-effort search for a representative recipe photo, in priority order:
      1. og:image / twitter:image meta tags (most reliable when present)
      2. <link rel="image_src">
      3. The largest plausible content <img> on the page, skipping obvious
         chrome (logos, icons, ads, spacers, nav/social/share graphics)
    Returns an absolute URL, or None if nothing suitable is found.
    """
    def absolutize(src):
        return urljoin(url, src) if url else src

    for prop in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            return absolutize(tag["content"].strip())

    link_tag = soup.find("link", attrs={"rel": "image_src"})
    if link_tag and link_tag.get("href"):
        return absolutize(link_tag["href"].strip())

    best_img, best_area = None, 0
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        if not src.lower().endswith(IMAGE_EXTENSIONS) and "?" not in src:
            continue
        if IMAGE_SKIP_PATTERN.search(src):
            continue
        try:
            width = int(img.get("width", 0))
            height = int(img.get("height", 0))
        except (TypeError, ValueError):
            width = height = 0
        area = width * height
        if area > best_area:
            best_area = area
            best_img = src

    return absolutize(best_img) if best_img else None


def find_recipe_node(obj, _depth=0):
    """
    Recursively search a parsed JSON blob (e.g. a Next.js __NEXT_DATA__
    payload) for a dict that looks like a structured recipe object. Many
    modern news/publisher sites render recipes client-side from an embedded
    JSON app-state blob rather than emitting schema.org markup, so this is
    tried as a middle step between recipe-scrapers and the raw-HTML
    heuristic parser.
    """
    if _depth > 12:
        return None
    if isinstance(obj, dict):
        if "recipeIngredientsPrepared" in obj or "recipeInstructionsPrepared" in obj:
            return obj
        for v in obj.values():
            found = find_recipe_node(v, _depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_recipe_node(item, _depth + 1)
            if found:
                return found
    return None


def render_descriptor_text(node):
    """Flatten a rich-text 'descriptor' node tree (CoreMedia/React-style) into plain text."""
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(render_descriptor_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("content", "")
    if node.get("type") == "embed":
        return ""  # skip images/embeds inside rich text
    return "".join(render_descriptor_text(c) for c in node.get("children") or [])


def extract_list_items(node, tag_names=("li",)):
    """Find all <li>-equivalent nodes within a descriptor tree, returning their flattened text."""
    items = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        if n.get("key") in tag_names:
            text = render_descriptor_text(n).strip()
            if text:
                items.append(text)
            return  # don't descend further to avoid duplicate/nested text
        for c in n.get("children") or []:
            walk(c)

    walk(node)
    return items


def extract_paragraphs(node):
    """Fallback: flatten <p>/standfirst text blocks in a descriptor tree, one string per block."""
    paras = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        if n.get("key") in ("p", "@@standfirst"):
            text = render_descriptor_text(n).strip()
            if text:
                paras.append(text)
            return
        for c in n.get("children") or []:
            walk(c)

    walk(node)
    return paras


def pick_best_media_image(featured_media):
    """Given a featuredMedia-style list with nested picture/cropInfo data, pick one representative image URL."""
    if not featured_media:
        return None
    for media in featured_media:
        picture = media.get("picture") or media
        for group in picture.get("cropInfo") or []:
            if group.get("key") == "large":
                ratios = group.get("value") or []
                for want in ("16x9", "4x3", "3x2", "1x1"):
                    for r in ratios:
                        if r.get("ratio") == want and r.get("url"):
                            return r["url"]
                if ratios and ratios[0].get("url"):
                    return ratios[0]["url"]
    return None


def build_recipe_from_app_state(node, url=None):
    """Map a recipe-like dict found inside an embedded JSON app-state blob onto schema.org Recipe."""
    name = node.get("name")
    canonical = node.get("canonicalURL") or url

    author = None
    authors = (node.get("contributors") or {}).get("author") or []
    if isinstance(authors, list) and authors:
        author = authors[0].get("name")

    ingredients = []
    for group in (node.get("recipeIngredientsPrepared") or {}).get("ingredients") or []:
        heading = group.get("heading")
        group_ingredients = group.get("ingredients") or []
        if heading:
            # schema.org's Recipe type has no real construct for grouped
            # ingredients (e.g. "For the topping" / "To serve"), and tools
            # like Nextcloud Cookbook just store a flat list of strings --
            # so a bare heading line ends up rendered as if it were an
            # actual (unquantified) ingredient. Fold the heading into each
            # ingredient in that group instead of emitting it as its own
            # fake ingredient line.
            heading_clean = heading.rstrip(":").strip()
            group_ingredients = [f"{ing} ({heading_clean.lower()})" for ing in group_ingredients]
        ingredients.extend(group_ingredients)

    instructions = []
    instr_node = ((node.get("recipeInstructionsPrepared") or {}).get("instructions") or {}).get("descriptor")
    if instr_node:
        steps = extract_list_items(instr_node, tag_names=("li",))
        if not steps:
            steps = extract_paragraphs(instr_node)
        instructions = [{"@type": "HowToStep", "text": clean_step_text(s)} for s in steps]

    description = None
    text_node = (node.get("text") or {}).get("descriptor")
    if text_node:
        paras = extract_paragraphs(text_node)
        if paras:
            description = " ".join(paras[:2])  # keep it to the intro, not the full body

    image_url = pick_best_media_image(node.get("featuredMedia"))

    keywords = node.get("keywords")
    if isinstance(keywords, list):
        keywords = ", ".join(keywords)

    category = node.get("recipeCategory")
    if isinstance(category, list):
        category = ", ".join(category)

    recipe = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": name,
        "description": description,
        "author": {"@type": "Person", "name": author} if author else None,
        "image": [image_url] if image_url else None,
        "recipeYield": node.get("recipeYield"),
        "prepTime": iso8601_duration(node.get("preparationTime")),
        "cookTime": iso8601_duration(node.get("cookingTime")),
        "totalTime": iso8601_duration(node.get("totalTime")),
        "recipeCategory": category,
        "keywords": keywords,
        "recipeIngredient": ingredients,
        "recipeInstructions": instructions,
        "url": canonical,
    }
    return {k: v for k, v in recipe.items() if v not in (None, "", [], {})}


def try_load_existing_jsonld(file_content):
    """
    If a --file argument points at a file this script already produced
    (our <script type="application/ld+json">...</script> wrapper), parse
    and return that JSON directly instead of re-scraping it as if it were
    a fresh recipe page. Re-scraping it would lose information -- notably,
    recipe-scrapers' generic parser can technically read our own embedded
    JSON-LD back in, but its canonical_url() only ever returns whatever
    --url was passed (or the 'https://example.com' placeholder if none
    was), throwing away the real URL that's already sitting right there
    in the file. Returns None if this doesn't look like one of our own
    output files.
    """
    m = re.search(
        r'<script\s+type=["\']application/ld\+json["\']\s*>(.*?)</script>',
        file_content, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("@type") == "Recipe":
        return data
    return None


def jsonld_description_scrape(html, url=None):
    """
    Some lightweight 'article' recipe pages don't use a proper schema.org
    Recipe type at all -- their only JSON-LD is a WebPage/Article block,
    and the entire recipe (ingredients + method) is embedded as one long
    plain-text string in the 'description' field, with 'Ingredients' and
    'Method' as plain-text section markers (e.g. "...Ingredients\n1 cup
    cream\n1 tsp vinegar\nMethod\nPour the cream into a jar..."). This
    looks for that specific pattern and extracts a usable recipe from it.
    Returns None if no such pattern is found.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict) or obj.get("@type") not in (
                "WebPage", "Article", "NewsArticle", "BlogPosting"
            ):
                continue
            description = obj.get("description")
            if not isinstance(description, str):
                continue
            m = re.search(
                r"Ingredients\s*\n+(.*?)\n+Method\s*\n+(.*)$",
                description, re.S | re.IGNORECASE,
            )
            if not m:
                continue

            ingredients = [line.strip() for line in m.group(1).split("\n") if line.strip()]

            steps = []
            for line in m.group(2).split("\n"):
                line = line.strip()
                if not line:
                    continue
                steps.extend(
                    s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line) if s.strip()
                )
            instructions = [{"@type": "HowToStep", "text": clean_step_text(s)} for s in steps]

            intro = description.split("Ingredients")[0].strip()
            intro_first_para = intro.split("\n")[0].strip() if intro else None

            image_field = obj.get("image")
            image_url = None
            if isinstance(image_field, list) and image_field:
                first_img = image_field[0]
                image_url = first_img.get("url") if isinstance(first_img, dict) else first_img
            elif isinstance(image_field, dict):
                image_url = image_field.get("url")
            elif isinstance(image_field, str):
                image_url = image_field

            author_field = obj.get("author")
            author = author_field.get("name") if isinstance(author_field, dict) else None

            recipe = {
                "@context": "https://schema.org",
                "@type": "Recipe",
                "name": obj.get("name"),
                "description": intro_first_para,
                "author": {"@type": "Person", "name": author} if author else None,
                "image": [image_url] if image_url else None,
                "recipeIngredient": ingredients,
                "recipeInstructions": instructions,
                "url": url or obj.get("url"),
            }
            result = {k: v for k, v in recipe.items() if v not in (None, "", [], {})}
            if result.get("recipeIngredient") or result.get("recipeInstructions"):
                return result
    return None


def app_state_scrape(html, url=None):
    """
    Look for an embedded JSON app-state blob (e.g. Next.js's
    <script id="__NEXT_DATA__" type="application/json">) containing a
    structured recipe object, and convert it if found. Returns None if no
    such blob/recipe is present so callers can fall through to the next
    strategy.
    """
    soup = BeautifulSoup(html, "html.parser")

    scripts = []
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        scripts.append(next_data.string)
    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        if tag is next_data or not tag.string:
            continue
        scripts.append(tag.string)

    for raw in scripts:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        recipe_node = find_recipe_node(data)
        if recipe_node:
            result = build_recipe_from_app_state(recipe_node, url=url)
            if result.get("recipeIngredient") or result.get("recipeInstructions"):
                return result
    return None


# Filler words stripped when building a short ingredient descriptor out of
# the prose immediately preceding an "(I used ...)" aside -- see
# extract_iused_ingredients() below. Includes ordinary stopwords plus a few
# generic kitchen-prep nouns (bowl, station, pan, ...) that tend to sit right
# next to the actual ingredient name but aren't themselves ingredients.
IUSED_STOPWORDS = {
    "a", "an", "the", "of", "with", "each", "this", "that", "some", "and",
    "my", "for", "in", "on", "to", "into", "was", "used", "bowl", "station",
    "set", "up", "pan", "plate", "dish", "own",
}
IUSED_PATTERN = re.compile(r"([A-Za-z0-9,\-\s]{0,60}?)\(I used ([^)]+)\)", re.IGNORECASE)
PLUS_SPLIT_PATTERN = re.compile(r"\s+plus\s+", re.IGNORECASE)


# Recognizable ingredient nouns, reused from the liquid/dry-weight keyword
# tables already in this script, plus a few other very common basics. Used
# by extract_iused_ingredients() to spot the actual ingredient name even
# when it's buried inside a longer descriptive clause.
KNOWN_INGREDIENT_NOUNS = (
    set(LIQUID_KEYWORDS)
    | {kw for group, _ in CUP_WEIGHT_TABLE for kw in group}
    | {"salt", "pepper", "egg", "eggs", "cheese", "sugar"}
)


# Rough, generic placeholder amounts for ingredients that typically show up
# as a bare type/brand with no quantity at all (e.g. "I used canola" for
# frying oil). These are NOT derived from the specific recipe being
# parsed -- there's no way to know the pan size or oil depth this article
# actually used -- they're just a plausible generic amount for that kind of
# ingredient. Rendered as a plain leading number (not "~250ml" or similar)
# so it still parses as a valid quantity for tools like Nextcloud Cookbook
# that require one for serving-size scaling; the trailing "(amount not
# stated in source)" note is what flags it as a guess rather than something
# extracted from the text. Deliberately small and conservative: expand only
# for ingredient types where a genuinely typical amount exists (e.g.
# shallow-frying oil in a pan), not for anything where the "right" amount
# varies too much to guess at all (there's no sensible generic default for
# "cheese" or "nuts").
DEFAULT_QUANTITY_GUESSES = (
    (("oil",), "250ml"),  # roughly enough to shallow-fry in a pan/skillet
)


def guess_default_quantity(line):
    """Look up a rough placeholder amount for a type-only ingredient line
    (e.g. 'canola oil'), based on keywords in the line. Returns None if no
    sensible generic default exists for this kind of ingredient."""
    lower = line.lower()
    for keywords, amount in DEFAULT_QUANTITY_GUESSES:
        if any(kw in lower for kw in keywords):
            return amount
    return None


def extract_iused_ingredients(text):
    """
    Best-effort extraction of ingredient quantities from personal-blog-style
    "how I made it" prose, which -- lacking any real ingredients list --
    often still names the exact amount used in a parenthetical aside right
    after the ingredient, e.g. "a bowl of flour (I used 3/4 cup)" or "an oil
    with a high smoking point (I used canola)".

    For each match, builds one ingredient line by combining the "(I used
    ...)" content with a short cleaned-up descriptor taken from the words
    immediately before it -- unless that content already names the
    ingredient itself (e.g. "beaten eggs (I used 2 eggs plus 2 tablespoons
    of water)"), in which case the descriptor would just be redundant noise
    and is dropped.

    This is inherently a rough approximation of a real ingredients list, so
    callers should tell the user to review/complete it -- prose rarely
    marks every ingredient this way (a plain "eggplant" or "salt" mentioned
    without an "(I used ...)" aside won't be picked up at all).
    """

    def clean_descriptor(text_before, max_words=6, max_descriptor_words=2):
        words = [w.strip(",.") for w in text_before.strip().split()[-max_words:]]
        words = [w for w in words if w.lower() not in IUSED_STOPWORDS]
        if not words:
            return ""
        # If a recognizable ingredient noun turns up among the leftover
        # words, that's a much stronger signal of the actual ingredient
        # name than raw position is -- e.g. "oil" inside "oil high smoking
        # point" (from "...oil with a high smoking point (I used
        # canola)"). Prefer that single word over the whole descriptive
        # clause it's embedded in.
        for w in words:
            key = w.lower().rstrip("s")
            if key in KNOWN_INGREDIENT_NOUNS or w.lower() in KNOWN_INGREDIENT_NOUNS:
                return w.lower()
        # Otherwise, a short leftover span (after stripping filler) is
        # usually just the ingredient name itself (e.g. "flour", "bread
        # crumbs"). A longer one is more likely a descriptive clause with
        # no recognizable noun in it -- gluing that onto the amount risks
        # nonsense like "canola oil high smoking point", so discard it
        # rather than guess wrong. The caller falls back to the
        # parenthetical alone, short of a name but at least not misleading.
        if len(words) > max_descriptor_words:
            return ""
        return " ".join(words).strip(" ,.")

    def word_set(s):
        return {re.sub(r"[^a-z]", "", w.lower()).rstrip("s") for w in s.split()}

    ingredients = []
    for m in IUSED_PATTERN.finditer(text or ""):
        descriptor = clean_descriptor(m.group(1))
        # A parenthetical can itself list more than one thing joined by
        # "plus" (e.g. "I used 2 eggs plus 2 tablespoons of water" for an
        # egg wash) -- each part is a distinct ingredient and should be its
        # own line, not one run-on ingredient. Only the first part can
        # plausibly need the preceding descriptor; later parts already name
        # themselves (e.g. "water" doesn't need "beaten eggs" tacked on).
        parts = [p.strip() for p in PLUS_SPLIT_PATTERN.split(m.group(2).strip()) if p.strip()]
        for i, part in enumerate(parts):
            if i == 0 and descriptor and not (word_set(descriptor) & word_set(part)):
                line = f"{part} {descriptor}".strip()
            else:
                line = part
            if not line:
                continue
            # Some asides name a type/brand rather than an amount at all
            # (e.g. "an oil with a high smoking point (I used canola)" only
            # tells us it was canola, never how much). For those, use a
            # clearly-labeled rough guess if one exists for this kind of
            # ingredient (see DEFAULT_QUANTITY_GUESSES), otherwise just
            # flag it "as needed" -- either way, never let it fall through
            # to ensure_leading_quantity()'s fallback, which would
            # otherwise fabricate a meaningless bare "1" in front of it.
            if not NUMBER_START_RE.match(part):
                guess = guess_default_quantity(line)
                if guess:
                    line = f"{guess} {line}, as needed (amount not stated in source)"
                else:
                    line = f"{line}, as needed"
            ingredients.append(line)
    return ingredients


def mntl_structured_content_scrape(html, url=None):
    """
    Handles Dotdash Meredith 'Mantle' CMS how-to articles (Treehugger, The
    Spruce Eats, MNN-migrated content, etc.) that walk through a process
    step by step under a series of <h2> headings, with no schema.org Recipe
    markup and no ingredients list at all -- just narrative paragraphs (e.g.
    "Breading the Eggplant" / "Time to Fry" / "Put Them Into the Freezer").

    Detected via the page's 'structured-content' block (class
    'mntl-sc-block-heading' for headings, 'mntl-sc-block-html' for body
    paragraphs). Each heading + the paragraph(s) that follow it, up to the
    next heading, becomes one HowToStep. Ingredients are inferred from
    "(I used ...)" asides via extract_iused_ingredients() -- see that
    function's docstring for why this is necessarily incomplete.

    Returns None if no 'structured-content' block is found, so callers can
    fall through to the generic heuristic parser.
    """
    soup = BeautifulSoup(html, "html.parser")

    content = soup.find(class_=lambda c: c and "structured-content" in c.split())
    if not content:
        return None

    intro_paragraphs = []
    steps = []
    current_heading = None
    current_paragraphs = []

    def flush():
        if current_heading or current_paragraphs:
            text = clean_step_text(" ".join(current_paragraphs))
            if text:
                steps.append({"@type": "HowToStep", "name": current_heading, "text": text}
                             if current_heading else {"@type": "HowToStep", "text": text})

    for tag in content.find_all(["h2", "h3", "p"]):
        classes = tag.get("class") or []
        is_heading = any("mntl-sc-block-heading" in c for c in classes)
        is_body = any("mntl-sc-block-html" in c for c in classes)
        if is_heading:
            flush()
            current_heading = tag.get_text(strip=True)
            current_paragraphs = []
        elif is_body:
            text = tag.get_text(" ", strip=True)
            if not text:
                continue
            if current_heading is None:
                intro_paragraphs.append(text)
            else:
                current_paragraphs.append(text)
    flush()

    if not steps:
        return None

    all_body_text = " ".join(intro_paragraphs + [s.get("text", "") for s in steps])
    ingredients = extract_iused_ingredients(all_body_text)

    title = extract_title(soup)
    image_url = find_best_image(soup, url=url)
    description = " ".join(intro_paragraphs[:2]) if intro_paragraphs else None

    recipe = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": title,
        "description": description,
        "image": [image_url] if image_url else None,
        "recipeIngredient": ingredients,
        "recipeInstructions": steps,
        "url": url,
    }
    return {k: v for k, v in recipe.items() if v not in (None, "", [], {})}


def extract_title(soup):
    """
    Pick the recipe's title, robust to pages that have more than one
    heading-like element (e.g. a sitewide banner heading plus the actual
    recipe title) -- naively taking the first <h1> can grab the wrong one.

    Strategy: the <title> tag reliably contains the real page title (often
    with a site-name prefix/suffix, e.g. "Recipes - Microwave Fruit Cake
    Recipe"). If any <h1> on the page has text that also appears in the
    <title> tag, that's almost certainly the actual recipe title (a generic
    banner heading like "Baking Recipes" usually won't appear in <title> at
    all). Falls back to the first <h1>, then to <title> itself with a
    likely site-name segment stripped off.
    """
    title_tag = soup.find("title")
    title_text = title_tag.get_text(strip=True) if title_tag else None

    h1_texts = [h.get_text(strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)]

    if title_text:
        # Prefer the longest matching h1 (most specific) that's actually
        # contained in the <title> text.
        matches = [h for h in h1_texts if h in title_text]
        if matches:
            return max(matches, key=len)
        # No h1 matched the <title> at all (e.g. the only h1 is an
        # unrelated sitewide banner) -- try to strip a likely site-name
        # segment off the <title> itself rather than trusting that h1.
        for sep in (" - ", " | ", ": "):
            if sep in title_text:
                parts = [p.strip() for p in title_text.split(sep) if p.strip()]
                if parts:
                    return max(parts, key=len)  # longer segment is usually the real title, not the site name
        return title_text

    if h1_texts:
        return h1_texts[0]

    return None


def heuristic_scrape(html, url=None):
    """
    Best-effort extractor for pages with no schema.org/JSON-LD/microdata at
    all (common on older, hand-coded recipe sites). This looks for a heading
    containing 'ingredient' and a heading containing 'method'/'instructions'/
    'directions', then grabs the text that follows each, up to the next
    heading. It won't be as reliable as real structured data, so review the
    output before publishing it.
    """
    soup = BeautifulSoup(html, "html.parser")

    # BeautifulSoup's get_text() includes text inside <script>/<style> tags
    # by default. Some site builders (Squarespace notably) inject per-block
    # <style> tags directly inside content containers, right alongside the
    # actual text, so without this those CSS rules get swept into
    # ingredients/instructions verbatim. Nothing in this heuristic parser
    # ever wants script/style content, so strip it up front.
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    title = extract_title(soup)

    # Keywords for every section type this parser recognizes, used so a
    # scan for one section stops when it bumps into the start of another.
    SECTION_KEYWORD_GROUPS = {
        "ingredients": ["ingredient"],
        "instructions": ["method", "instructions", "directions"],
    }

    def get_row_container(tag):
        """The block-level container to treat as one 'row' of content: the
        enclosing table row if there is one, otherwise the nearest
        td/li/p/div ancestor."""
        return tag.find_parent("tr") or tag.find_parent(["td", "li", "p", "div"]) or tag

    def find_section(keywords, other_keyword_groups):
        other_keywords = [kw for group in other_keyword_groups for kw in group]
        for tag in soup.find_all(["b", "strong", "h2", "h3", "h4", "p", "td"]):
            label = tag.get_text(strip=True).lower()
            if not (any(kw in label for kw in keywords) and len(label) < 40):
                continue

            row = get_row_container(tag)
            collected = []

            # The row's own text covers the common inline case, e.g.
            # "<b>Ingredients:</b> 175g butter...". Strip the label prefix.
            own_text = row.get_text("\n", strip=True)
            own_text = re.sub(
                r"^\s*(ingredients|method|directions|instructions)\s*:?\s*",
                "",
                own_text,
                flags=re.IGNORECASE,
            )
            if own_text:
                collected.append(own_text)

            # Some sites put the label in its own row/cell and the actual
            # content in the row(s) that follow -- walk forward to catch
            # that layout too, stopping when we hit the next section's own
            # heading (detected via an actual heading-like tag, not by how
            # long the sibling's merged text happens to be).
            def sibling_starts_next_section(sib):
                tags_to_check = [sib] if sib.name in ("b", "strong", "h2", "h3", "h4") else []
                tags_to_check += sib.find_all(["b", "strong", "h2", "h3", "h4"])
                for t in tags_to_check:
                    lbl = t.get_text(strip=True).lower()
                    if any(kw in lbl for kw in other_keywords) and len(lbl) < 40:
                        return True
                return False

            for sib in row.find_next_siblings(limit=6):
                if sibling_starts_next_section(sib):
                    break
                sib_text = sib.get_text("\n", strip=True)
                if sib_text:
                    collected.append(sib_text)
                    break  # old-style sites usually put all content in one following row

            text = "\n".join(c for c in collected if c)
            if text:
                return text
        return None

    image_url = find_best_image(soup, url=url)

    ingredients_text = find_section(
        SECTION_KEYWORD_GROUPS["ingredients"], [SECTION_KEYWORD_GROUPS["instructions"]]
    )
    method_text = find_section(
        SECTION_KEYWORD_GROUPS["instructions"], [SECTION_KEYWORD_GROUPS["ingredients"]]
    )

    ingredients = split_ingredient_block(ingredients_text) if ingredients_text else []

    # A leading line like "Makes one 18 cm cake" or "Serves 4" is a yield
    # statement, not an ingredient -- pull it out if present.
    recipe_yield = None
    if ingredients and re.match(r"^(makes|serves|yields?)\b", ingredients[0], re.IGNORECASE):
        recipe_yield = ingredients.pop(0)

    instructions = []
    if method_text:
        # Split on sentence boundaries as a rough step approximation
        steps = re.split(r"(?<=[.!?])\s+(?=[A-Z])", method_text)
        instructions = [
            {"@type": "HowToStep", "text": clean_step_text(s)} for s in steps if s.strip()
        ]

    recipe = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": title,
        "image": [image_url] if image_url else None,
        "recipeYield": recipe_yield,
        "recipeIngredient": ingredients,
        "recipeInstructions": instructions,
        "url": url,
    }
    return {k: v for k, v in recipe.items() if v not in (None, "", [], {})}


def scrape_from_url(url):
    html = fetch_html(url)
    scraper = scrape_html(html=html, org_url=url, wild_mode=True)
    return build_recipe_jsonld(scraper, canonical_url=url)


def scrape_from_file(path, url=None):
    with open(path, encoding="utf-8", errors="replace") as f:
        html = f.read()
    scraper = scrape_html(html=html, org_url=url or "https://example.com", wild_mode=True)
    return build_recipe_jsonld(scraper, canonical_url=url)


def upload_to_nextcloud(recipe_json, nextcloud_url, username, password):
    """
    Upload a recipe to Nextcloud Cookbook via the API.

    Args:
        recipe_json: The schema.org Recipe dict produced by this script
        nextcloud_url: Base Nextcloud URL (e.g., "https://nextcloud.example.com")
        username: Nextcloud username
        password: Nextcloud password (or app password)

    Returns:
        The ID of the newly created recipe (str).

    Raises:
        HTTPError or URLError on network issues.
    """
    # create_recipe() expects an actual nextcloud_cookbook_api Recipe model,
    # not a plain dict -- it calls recipe.model_dump(...) internally, which
    # a dict doesn't have. The Cookbook API's own field names/types also
    # differ from schema.org's, so we need to translate rather than pass
    # our JSON-LD straight through:
    #   - recipeInstructions here is a list of {"@type": "HowToStep", "text": ...}
    #     dicts; Cookbook wants a plain list[str].
    #   - recipeYield here is free text (e.g. "4 servings" or a yield note);
    #     Cookbook wants an int number of servings.
    #   - id/date_created/date_modified are required by the model but are
    #     assigned by the server on creation, so we fill in placeholders.
    #   - nutrition is a required field on the model; we send an empty one
    #     if we don't have real nutrition data.
    instructions = [
        step.get("text", "") if isinstance(step, dict) else str(step)
        for step in recipe_json.get("recipeInstructions", [])
    ]

    yield_match = re.search(r"\d+", str(recipe_json.get("recipeYield", "")))
    servings = int(yield_match.group()) if yield_match else 1

    image = recipe_json.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""

    # model_construct() bypasses the model's normal validators (including
    # the one that parses a comma-separated keywords string into a list),
    # so do that conversion ourselves rather than passing the raw string
    # through -- otherwise it gets serialized character-by-character.
    keywords_raw = recipe_json.get("keywords")
    if isinstance(keywords_raw, str):
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    elif isinstance(keywords_raw, list):
        keywords = keywords_raw
    else:
        keywords = None

    now = datetime.now(timezone.utc)

    recipe = Recipe.model_construct(
        id="",  # server assigns the real ID on creation
        name=recipe_json.get("name", ""),
        keywords=keywords,
        date_created=now,
        date_modified=now,
        image_url=image,
        image_placeholder_url="",
        type="Recipe",
        prep_time=recipe_json.get("prepTime"),
        cook_time=recipe_json.get("cookTime"),
        total_time=recipe_json.get("totalTime"),
        description=recipe_json.get("description", ""),
        url=recipe_json.get("url", ""),
        image=image,
        servings=servings,
        category=recipe_json.get("recipeCategory", ""),
        tools=[],
        ingredients=recipe_json.get("recipeIngredient", []),
        instructions=instructions,
        nutrition=Nutrition.model_construct(type="NutritionInformation"),
    )

    client = CookbookClient(username=username, password=password, base_url=nextcloud_url)
    new_recipe_id = client.create_recipe(recipe)  # returns the new recipe's ID (str)
    print(f"Created recipe '{recipe.name}' with ID: {new_recipe_id}")
    return new_recipe_id


def main():
    parser = argparse.ArgumentParser(
        description="Convert a recipe page to schema.org Recipe JSON-LD and optionally upload to Nextcloud"
    )
    parser.add_argument("--url", help="Recipe URL (fetched live, or used as canonical URL with --file)")
    parser.add_argument("--file", help="Path to a locally saved HTML file (avoids live fetching)")
    parser.add_argument("--out", help="Output file path (defaults to stdout). Skips Nextcloud upload if provided.")
    parser.add_argument("--nextcloud-url", help="Nextcloud instance URL (e.g., https://nextcloud.example.com)")
    parser.add_argument("--nextcloud-user", help="Nextcloud username")
    parser.add_argument("--nextcloud-pass", help="Nextcloud password or app password")
    args = parser.parse_args()

    if not args.url and not args.file:
        parser.error("Provide --url (to fetch live) or --file (+ optional --url for canonical URL)")

    recipe_json = None
    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as f:
            file_content = f.read()
        recipe_json = try_load_existing_jsonld(file_content)
        if recipe_json:
            print("Detected an existing recipe JSON-LD file; reusing it directly "
                  "instead of re-scraping it.", file=sys.stderr)

    def run_fallback_chain():
        """Try each remaining strategy in turn: an embedded app-state JSON
        blob, a WebPage/Article JSON-LD with the recipe embedded as plain
        text, then the raw-HTML heuristic parser. Exits the program if none
        of them find anything."""
        html = None
        try:
            html = open(args.file, encoding="utf-8", errors="replace").read() if args.file else fetch_html(args.url)
        except Exception as e_html:
            print(f"Could not read/fetch HTML: {e_html}", file=sys.stderr)
            sys.exit(1)

        result = app_state_scrape(html, url=args.url)
        if result:
            print("Found recipe data in an embedded JSON blob (e.g. __NEXT_DATA__).", file=sys.stderr)
            return result

        result = jsonld_description_scrape(html, url=args.url)
        if result:
            print("Found recipe data embedded in a WebPage/Article JSON-LD description.", file=sys.stderr)
            return result

        result = mntl_structured_content_scrape(html, url=args.url)
        if result:
            print("Found a Dotdash Meredith style step-by-step how-to article (no ingredients "
                  "list on the page at all). Ingredients were inferred from '(I used ...)' asides "
                  "in the prose -- this list is very likely incomplete, so please review and "
                  "complete it by hand before using it.", file=sys.stderr)
            return result

        print("No embedded app-state recipe found; falling back to heuristic HTML parsing...",
              file=sys.stderr)
        try:
            result = heuristic_scrape(html, url=args.url)
            if not result.get("recipeIngredient") and not result.get("recipeInstructions"):
                print("Heuristic parsing also failed to find ingredients/instructions. "
                      "This page's markup may need a custom parser.", file=sys.stderr)
                sys.exit(1)
            return result
        except Exception as e2:
            print(f"Heuristic fallback also failed: {e2}", file=sys.stderr)
            sys.exit(1)

    if recipe_json is None:
        try:
            if args.file:
                recipe_json = scrape_from_file(args.file, url=args.url)
            else:
                recipe_json = scrape_from_url(args.url)
        except RecipeScrapersExceptions as e:
            print(f"recipe-scrapers found no schema markup ({e}); "
                  f"checking for an embedded app-state recipe blob...", file=sys.stderr)
            recipe_json = run_fallback_chain()
        else:
            # recipe-scrapers can "succeed" by finding a validly-typed
            # Recipe schema that's nonetheless incomplete -- e.g. a stub
            # object with just name/description/image and no actual
            # ingredients or steps, while the real recipe content sits in a
            # sibling JSON-LD block. Treat that the same as a hard failure.
            if not recipe_json.get("recipeIngredient") and not recipe_json.get("recipeInstructions"):
                print("recipe-scrapers found an incomplete Recipe schema (no ingredients/instructions); "
                      "checking other strategies...", file=sys.stderr)
                recipe_json = run_fallback_chain()

    # An explicitly passed --url always takes precedence over whatever URL
    # ended up in recipe_json, whether that came from scraping a fresh page
    # or from reusing an existing JSON-LD file.
    if args.url:
        recipe_json["url"] = args.url

    recipe_json = normalize_fractions_deep(recipe_json)
    recipe_json = normalize_measurements_deep(recipe_json)
    if "recipeIngredient" in recipe_json:
        recipe_json["recipeIngredient"] = normalize_ingredient_phrasing(recipe_json["recipeIngredient"])
        recipe_json["recipeIngredient"] = ensure_leading_quantity(recipe_json["recipeIngredient"])

    if args.nextcloud_url:
        upload_to_nextcloud(recipe_json, args.nextcloud_url, args.nextcloud_user, args.nextcloud_pass)
    else:

        output = f'<script type="application/ld+json">\n{json.dumps(recipe_json, indent=2, ensure_ascii=False)}\n</script>'

        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Written to {args.out}")
        else:
            print(output)


if __name__ == "__main__":
    main()
