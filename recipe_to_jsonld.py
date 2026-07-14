#!/usr/bin/python3

"""
Convert recipe web pages into schema.org Recipe JSON-LD using the
`recipe-scrapers` library.

Install:
    pip install recipe-scrapers

Usage:
    python recipe_to_jsonld.py --url "https://example.com/some-recipe"
    python recipe_to_jsonld.py --file saved_page.html --url "https://example.com/some-recipe"
"""

import argparse
import json
import re
import sys

from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from recipe_scrapers import scrape_html
from recipe_scrapers._exceptions import RecipeScrapersExceptions

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Rough patterns for splitting an ingredient line into distinct ingredients.
# Old-style recipe pages (no markup) often jam several ingredients into one
# paragraph or table cell, separated by line breaks that get flattened to
# whitespace, or by a quantity+unit pattern repeating inline.
UNIT_PATTERN = re.compile(
    r"(?=\d+[\d\s/.,]*\s?(?:g|kg|ml|l|tsp|tbsp|cup|cups|oz|lb|lbs)\b)", re.IGNORECASE
)


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
            {"@type": "HowToStep", "text": step} for step in instructions_list
        ]
    else:
        raw = safe(scraper.instructions, "")
        instructions = [
            {"@type": "HowToStep", "text": line.strip()}
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
    """Split a chunk of run-together ingredient text into individual lines."""
    # First try actual line breaks (works if <br> was converted to \n upstream)
    lines = [l.strip(" -\u2022\t") for l in text.split("\n") if l.strip()]
    if len(lines) > 1:
        return lines
    # Fall back to splitting right before each quantity+unit occurrence
    pieces = [p.strip(" ,") for p in UNIT_PATTERN.split(text) if p.strip(" ,")]
    return pieces if len(pieces) > 1 else [text.strip()]


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

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    def find_section(keywords):
        for tag in soup.find_all(["b", "strong", "h2", "h3", "h4", "p", "td"]):
            label = tag.get_text(strip=True).lower()
            if any(kw in label for kw in keywords) and len(label) < 40:
                # Prefer the parent cell/container's remaining text if it's a
                # short inline label (e.g. "<b>Ingredients:</b> 175g butter...")
                container = tag.find_parent(["td", "li", "p", "div"]) or tag
                text = container.get_text("\n", strip=True)
                # Strip the label itself off the front
                text = re.sub(
                    r"^\s*(ingredients|method|directions|instructions)\s*:?\s*",
                    "",
                    text,
                    flags=re.IGNORECASE,
                )
                if text:
                    return text
        return None

    ingredients_text = find_section(["ingredient"])
    method_text = find_section(["method", "instructions", "directions"])

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
            {"@type": "HowToStep", "text": s.strip()} for s in steps if s.strip()
        ]

    recipe = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": title,
        "recipeYield": recipe_yield,
        "recipeIngredient": ingredients,
        "recipeInstructions": instructions,
        "url": url,
    }
    return {k: v for k, v in recipe.items() if v not in (None, "", [], {})}


def scrape_from_url(url):
    # scrape_me() in current recipe-scrapers versions doesn't forward extra
    # kwargs like wild_mode, so fetch the HTML ourselves and call
    # scrape_html() directly.
    request = Request(url, headers={"User-Agent": USER_AGENT})
    html = urlopen(request).read().decode("utf-8", errors="replace")
    scraper = scrape_html(html=html, org_url=url, wild_mode=True)
    return build_recipe_jsonld(scraper, canonical_url=url)


def scrape_from_file(path, url=None):
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    scraper = scrape_html(html=html, org_url=url or "https://example.com", wild_mode=True)
    return build_recipe_jsonld(scraper, canonical_url=url)


def main():
    parser = argparse.ArgumentParser(description="Convert a recipe page to schema.org Recipe JSON-LD")
    parser.add_argument("--url", help="Recipe URL (fetched live, or used as canonical URL with --file)")
    parser.add_argument("--file", help="Path to a locally saved HTML file (avoids live fetching)")
    parser.add_argument("--out", help="Output file path (defaults to stdout)")
    args = parser.parse_args()

    if not args.url and not args.file:
        parser.error("Provide --url (to fetch live) or --file (+ optional --url for canonical URL)")

    try:
        if args.file:
            recipe_json = scrape_from_file(args.file, url=args.url)
        else:
            recipe_json = scrape_from_url(args.url)
    except RecipeScrapersExceptions as e:
        print(f"recipe-scrapers found no schema markup ({e}); "
              f"falling back to heuristic HTML parsing...", file=sys.stderr)
        try:
            html = open(args.file, encoding="utf-8").read() if args.file else fetch_html(args.url)
            recipe_json = heuristic_scrape(html, url=args.url)
            if not recipe_json.get("recipeIngredient") and not recipe_json.get("recipeInstructions"):
                print("Heuristic parsing also failed to find ingredients/instructions. "
                      "This page's markup may need a custom parser.", file=sys.stderr)
                sys.exit(1)
        except Exception as e2:
            print(f"Heuristic fallback also failed: {e2}", file=sys.stderr)
            sys.exit(1)

    output = f'<script type="application/ld+json">\n{json.dumps(recipe_json, indent=2, ensure_ascii=False)}\n</script>'

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Written to {args.out}")
    else:
        print(output)


if __name__ == "__main__":
    main()
