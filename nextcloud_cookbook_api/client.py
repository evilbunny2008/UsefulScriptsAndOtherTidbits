from typing import Literal
from urllib.parse import urljoin

import requests
from requests.auth import HTTPBasicAuth

from nextcloud_cookbook_api.models import Category, Config, Keyword, Recipe, RecipeStub


class CookbookClient:
    """API client for the Nextcloud Cookbook app."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        """Create a new CookbookClient instance.

        :param base_url: The base URL of the Nextcloud instance.
        :param username: The username for authentication.
        :param password: The password for authentication.
        """
        self.base_url = base_url
        self.username = username
        self.password = password

    def _make_request(
        self,
        method: Literal["GET", "POST", "PUT", "DELETE"],
        path: str,
        **kwargs,
    ):
        """Handle all requests to the Cookbook API with authentication, error handling, and response parsing.

        :param method: The HTTP method to use for the request (GET, POST, PUT, DELETE).
        :param path: The API endpoint path to request.
        :param kwargs: Additional keyword arguments to pass to the requests library.
        :return: The response object from the API request.
        """
        auth = HTTPBasicAuth(self.username, self.password)

        url = urljoin(self.base_url, path)

        return requests.request(method, url, auth=auth, **kwargs)

    def get_keywords(self) -> list[Keyword]:
        """Retrieve all available keywords.

        :return: A list of keyword strings.
        """
        response = self._make_request("GET", "/index.php/apps/cookbook/api/v1/keywords")
        response.raise_for_status()
        return [Keyword.model_validate(k) for k in response.json()]

    def search_recipes_by_keywords(self, keywords: list[str]) -> list[RecipeStub]:
        """Search recipes by keyword(s).

        :param keywords: The keywords to search for.
        :return: A list of RecipeStub objects matching the search query.
        """
        keyword_string = ",".join(keywords)

        response = self._make_request(
            "GET",
            f"/index.php/apps/cookbook/api/v1/tags/{keyword_string}",
        )
        response.raise_for_status()
        return [RecipeStub.model_validate(r) for r in response.json()]

    def get_categories(self) -> list[Category]:
        """Retrieve all available categories.

        :return: A list of Category objects.
        """
        response = self._make_request("GET", "/index.php/apps/cookbook/api/v1/categories")
        response.raise_for_status()
        return [Category.model_validate(c) for c in response.json()]

    def get_recipes_by_category(self, category: str | None) -> list[RecipeStub]:
        """Retrieve recipes belonging to a specific category.

        :param category: The name of the category. If None, all recipes without a category are returned.
        :return: A list of RecipeStub objects belonging to the specified category.
        """
        if category is None:
            category = "_"

        response = self._make_request(
            "GET",
            f"/index.php/apps/cookbook/api/v1/category/{category}",
        )
        response.raise_for_status()
        return [RecipeStub.model_validate(r) for r in response.json()]

    def rename_category(self, old_name: str, new_name: str) -> None:
        """Rename a category.

        :param old_name: The current name of the category.
        :param new_name: The new name for the category.
        :raises ValueError: If the category with the old name does not exist.
        """
        if old_name == new_name:
            return

        # check if the category with the old name exists, there is no server-side validation for this
        categories = self.get_categories()
        if not any(c.name == old_name for c in categories):
            msg = f"Category '{old_name}' does not exist."
            raise ValueError(msg)

        response = self._make_request(
            "PUT",
            f"/index.php/apps/cookbook/api/v1/category/{old_name}",
            json={"name": new_name},
        )
        response.raise_for_status()

    def import_recipe(self, url: str) -> Recipe:
        """Import a recipe from a URL.

        :param url: The URL of the recipe to import.
        :return: The imported Recipe object.
        """
        response = self._make_request(
            "POST",
            "/index.php/apps/cookbook/api/v1/import",
            json={"url": url},
        )
        response.raise_for_status()
        return Recipe.model_validate(response.json())

    def get_recipe_main_image(
        self,
        recipe_id: str,
        size: Literal["full", "thumb", "thumb16"] = "full",
    ) -> bytes:
        """Get the main image of a recipe.

        :return: The image bytes.
        """
        response = self._make_request(
            "GET",
            f"/index.php/apps/cookbook/api/v1/recipes/{recipe_id}/image",
            params={"size": size},
        )
        response.raise_for_status()
        return response.content

    def search_recipes(self, query: str) -> list[RecipeStub]:
        """Search for recipes with categories, keywords, or names matching the search query.

        :param query: The search query, separated with spaces and/or commas.
        :return: A list of RecipeStub objects matching the search query.
        """
        response = self._make_request("GET", f"/index.php/apps/cookbook/api/v1/search/{query}")
        response.raise_for_status()
        return [RecipeStub.model_validate(item) for item in response.json()]

    def get_recipes(self) -> list[RecipeStub]:
        """Retrieve all recipes from the cookbook.

        :return: A list of RecipeStub objects representing all recipes.
        """
        response = self._make_request("GET", "/index.php/apps/cookbook/api/v1/recipes")
        response.raise_for_status()
        return [RecipeStub.model_validate(item) for item in response.json()]

    def create_recipe(self, recipe: Recipe) -> str:
        """Create a new recipe in the cookbook.

        :param recipe: The Recipe object to create.
        :return: The ID of the newly created recipe.
        """
        response = self._make_request(
            "POST",
            "/index.php/apps/cookbook/api/v1/recipes",
            json=recipe.model_dump(mode="json", by_alias=True),
        )
        response.raise_for_status()
        return response.text

    def get_recipe(self, id: str) -> Recipe:
        """Retrieve a recipe by its ID.

        :param id: The ID of the recipe to retrieve.
        :return: The Recipe object representing the retrieved recipe.
        """
        response = self._make_request("GET", f"/index.php/apps/cookbook/api/v1/recipes/{id}")
        response.raise_for_status()
        return Recipe.model_validate(response.json())

    def update_recipe(self, id, recipe: Recipe) -> None:
        """Update an existing recipe.

        :param id: The ID of the recipe to update.
        :param recipe: The updated Recipe object.
        """
        response = self._make_request(
            "PUT",
            f"/index.php/apps/cookbook/api/v1/recipes/{id}",
            json=recipe.model_dump(mode="json", by_alias=True),
        )
        response.raise_for_status()

    def delete_recipe(self, id: str) -> None:
        """Delete a recipe by its ID.

        :param id: The ID of the recipe to delete.
        """
        response = self._make_request("DELETE", f"/index.php/apps/cookbook/api/v1/recipes/{id}")
        response.raise_for_status()

    def get_ocr_capabilities(self) -> dict:
        """Get the capabilities of the Nextcloud instance.

        :return: A dictionary containing the capabilities.
        """
        response = self._make_request(
            "GET",
            "/ocs/v2.php/cloud/capabilities",
            headers={"OCS-APIRequest": "true"},
            params={"format": "json"},
        )
        response.raise_for_status()
        return response.json()

    def trigger_reindex(self) -> None:
        """Trigger a rescan of all recipes into the caching database."""
        response = self._make_request(
            "POST",
            "/index.php/apps/cookbook/api/v1/reindex",
            headers={"OCS-APIRequest": "true"},
        )
        response.raise_for_status()

    def get_config(self) -> Config:
        """Get the current configuration of the cookbook app.

        :return: The Config object.
        """
        response = self._make_request("GET", "/index.php/apps/cookbook/api/v1/config")
        response.raise_for_status()
        return Config.model_validate(response.json())

    def set_config(self, config: Config) -> None:
        """Set the current configuration of the cookbook app for the current user.

        :param config: The Config object to set.
        """
        response = self._make_request(
            "POST",
            "/index.php/apps/cookbook/api/v1/config",
            json=config.model_dump(mode="json", by_alias=True),
        )
        response.raise_for_status()
