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

from recipe_scrapers import scrape_html, scrape_me
from recipe_scrapers._exceptions import RecipeScrapersExceptions


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


def scrape_from_url(url):
    scraper = scrape_me(url, wild_mode=True)
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
        print(f"Could not scrape recipe: {e}", file=sys.stderr)
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
