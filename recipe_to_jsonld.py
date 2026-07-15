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

from bs4 import BeautifulSoup

from nextcloud_cookbook_api.client import CookbookClient
from nextcloud_cookbook_api.models import Recipe

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
    r"(?=\d+[\d\s/.,]*\s?(?:g|kg|ml|l|tsp|tbsp|cup|cups|oz|lb|lbs)\b)", re.IGNORECASE
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

    candidates = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src:
            continue
        src_clean = src.strip()
        if not src_clean.lower().split("?")[0].endswith(IMAGE_EXTENSIONS):
            continue
        if IMAGE_SKIP_PATTERN.search(src_clean):
            continue

        # Use width/height attributes as a rough size signal when available;
        # otherwise fall back to a neutral score so the image isn't excluded.
        try:
            width = int(img.get("width", 0))
            height = int(img.get("height", 0))
        except ValueError:
            width = height = 0
        area = width * height

        # Tiny declared dimensions (icons, tracking pixels) are disqualifying.
        if width and width < 100:
            continue
        if height and height < 100:
            continue

        alt = (img.get("alt") or "").lower()
        title_bonus = 1 if any(w in alt for w in ("recipe", "cake", "dish", "food")) else 0

        candidates.append((area, title_bonus, absolutize(src_clean)))

    if not candidates:
        return None

    # Prefer images explicitly tagged as food-related, then by declared area.
    candidates.sort(key=lambda c: (c[1], c[0]), reverse=True)
    return candidates[0][2]


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
        if heading:
            ingredients.append(heading.rstrip(":"))
        ingredients.extend(group.get("ingredients") or [])

    instructions = []
    instr_node = ((node.get("recipeInstructionsPrepared") or {}).get("instructions") or {}).get("descriptor")
    if instr_node:
        steps = extract_list_items(instr_node, tag_names=("li",))
        if not steps:
            steps = extract_paragraphs(instr_node)
        instructions = [{"@type": "HowToStep", "text": s} for s in steps]

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

    image_url = find_best_image(soup, url=url)

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
        "image": [image_url] if image_url else None,
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


def upload_to_nextcloud(recipe_json, nextcloud_url, username, password):
    """
    Upload a recipe to Nextcloud Cookbook via the API.

    Args:
        recipe_json: The recipe dict (will be JSON-encoded)
        nextcloud_url: Base Nextcloud URL (e.g., "https://nextcloud.example.com")
        username: Nextcloud username
        password: Nextcloud password (or app password)

    Returns:
        True if successful, False otherwise.

    Raises:
        HTTPError or URLError on network issues.
    """
    # Ensure URL doesn't have trailing slash
    #nextcloud_url = nextcloud_url.rstrip("/")

    #api_endpoint = f"{nextcloud_url}/ocs/v2.php/apps/cookbook/api/v1/recipes"

    #print(f"api_endpoint: {api_endpoint}")

    # Create Basic Auth header
    #credentials = base64.b64encode(f"{username}:{password}".encode()).decode()

    # Prepare the request
    #payload = json.dumps(recipe_json).encode("utf-8")

    print(f"nextcloud_url: {nextcloud_url}")

    client = CookbookClient(
        username=username,
        password=password,
        base_url=nextcloud_url,
    )

    new_recipe = client.create_recipe(recipe_json)

    print(new_recipe.id)
    print(new_recipe.name)


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

    try:
        if args.file:
            recipe_json = scrape_from_file(args.file, url=args.url)
        else:
            recipe_json = scrape_from_url(args.url)
    except RecipeScrapersExceptions as e:
        print(f"recipe-scrapers found no schema markup ({e}); "
              f"checking for an embedded app-state recipe blob...", file=sys.stderr)
        html = None
        try:
            html = open(args.file, encoding="utf-8").read() if args.file else fetch_html(args.url)
        except Exception as e_html:
            print(f"Could not read/fetch HTML: {e_html}", file=sys.stderr)
            sys.exit(1)

        recipe_json = app_state_scrape(html, url=args.url)
        if recipe_json:
            print("Found recipe data in an embedded JSON blob (e.g. __NEXT_DATA__).", file=sys.stderr)
        else:
            print("No embedded app-state recipe found; falling back to heuristic HTML parsing...",
                  file=sys.stderr)
            try:
                recipe_json = heuristic_scrape(html, url=args.url)
                if not recipe_json.get("recipeIngredient") and not recipe_json.get("recipeInstructions"):
                    print("Heuristic parsing also failed to find ingredients/instructions. "
                          "This page's markup may need a custom parser.", file=sys.stderr)
                    sys.exit(1)
            except Exception as e2:
                print(f"Heuristic fallback also failed: {e2}", file=sys.stderr)
                sys.exit(1)

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
