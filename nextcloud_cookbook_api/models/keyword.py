from pydantic import BaseModel, Field


class Keyword(BaseModel):
    """Represents a recipe keyword."""

    name: str = Field(..., description="The name of the keyword")
    recipe_count: int = Field(..., description="The number of recipes with the keyword")
