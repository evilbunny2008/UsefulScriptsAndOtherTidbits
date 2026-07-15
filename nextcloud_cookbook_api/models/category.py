from pydantic import BaseModel, Field


class Category(BaseModel):
    """Represents a recipe category."""

    name: str = Field(..., description="The name of the category")
    recipe_count: int = Field(..., description="The number of recipes in the category")
