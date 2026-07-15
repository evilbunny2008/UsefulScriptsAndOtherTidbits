from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, field_validator


class Nutrition(BaseModel):
    """Nutrition information for a recipe."""

    type: Literal["NutritionInformation"] = Field(
        ...,
        description="The type of the nutrition information",
        validation_alias="@type",
        serialization_alias="@type",
    )
    calories: str | None = Field(
        None,
        description="The number of calories for the given amount",
        examples=["650 kcal"],
    )
    carbohydrate_content: str | None = Field(
        None,
        description="The number of grams of carbohydrates",
        examples=["300 g"],
        validation_alias="carbohydrateContent",
        serialization_alias="carbohydrateContent",
    )
    cholesterol_content: str | None = Field(
        None,
        description="The number of milligrams of cholesterol",
        examples=["10 g"],
        validation_alias="cholesterolContent",
        serialization_alias="cholesterolContent",
    )
    fat_content: str | None = Field(
        None,
        description="The number of grams of fat",
        examples=["45 g"],
        validation_alias="fatContent",
        serialization_alias="fatContent",
    )
    fiber_content: str | None = Field(
        None,
        description="The number of grams of fiber",
        examples=["50 g"],
        validation_alias="fiberContent",
        serialization_alias="fiberContent",
    )
    protein_content: str | None = Field(
        None,
        description="The number of grams of protein",
        examples=["80 g"],
        validation_alias="proteinContent",
        serialization_alias="proteinContent",
    )
    saturated_fat_content: str | None = Field(
        None,
        description="The number of grams of saturated fat",
        examples=["5 g"],
        validation_alias="saturatedFatContent",
        serialization_alias="saturatedFatContent",
    )
    serving_size: str | None = Field(
        None,
        description="The serving size, in terms of the number of volume or mass",
        examples=["One plate, sufficient for one person as dessert"],
        validation_alias="servingSize",
        serialization_alias="servingSize",
    )
    sodium_content: str | None = Field(
        None,
        description="The number of milligrams of sodium",
        examples=["10 mg"],
        validation_alias="sodiumContent",
        serialization_alias="sodiumContent",
    )
    sugar_content: str | None = Field(
        None,
        description="The number of grams of sugar",
        examples=["5 g"],
        validation_alias="sugarContent",
        serialization_alias="sugarContent",
    )
    trans_fat_content: str | None = Field(
        None,
        description="The number of grams of trans fat",
        examples=["10 g"],
        validation_alias="transFatContent",
        serialization_alias="transFatContent",
    )
    unsaturated_fat_content: str | None = Field(
        None,
        description="The number of grams of unsaturated fat",
        examples=["40 g"],
        validation_alias="unsaturatedFatContent",
        serialization_alias="unsaturatedFatContent",
    )


class RecipeStub(BaseModel):
    """A stub of a recipe with some basic information present."""

    id: str = Field(..., description="The identifier of the recipe", example="123")
    name: str = Field(
        ...,
        description="The name of the recipe",
        example="Baked bananas",
    )
    keywords: list[str] | None = Field(
        None,
        description="A comma-separated list of recipe keywords, can be empty string",
        examples=[["sweets,fruit"]],
    )
    date_created: datetime = Field(
        ...,
        description="The date the recipe was created in the app",
        validation_alias="dateCreated",
        serialization_alias="dateCreated",
    )
    date_modified: datetime = Field(
        ...,
        description="The date the recipe was modified lastly in the app",
        validation_alias="dateModified",
        serialization_alias="dateModified",
    )
    image_url: str = Field(
        "",
        description="The URL of the recipe image",
        validation_alias="imageUrl",
        serialization_alias="imageUrl",
    )
    image_placeholder_url: str = Field(
        "",
        description="The URL of the placeholder of the recipe image",
        validation_alias="imagePlaceholderUrl",
        serialization_alias="imagePlaceholderUrl",
    )

    @field_validator("keywords", mode="before")
    @classmethod
    def parse_keywords(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_serializer("keywords", mode="plain")
    def serialize_keywords(self, value: list[str] | None) -> str:
        if value is None:
            return ""
        return ",".join(value)


class Recipe(RecipeStub):
    """A complete recipe with all the information present."""

    type: Literal["Recipe"] = Field(
        ...,
        description="The type of the recipe",
        validation_alias="@type",
        serialization_alias="@type",
    )
    prep_time: str | None = Field(
        None,
        description="The time required for preparation in ISO8601 format",
        validation_alias="prepTime",
        serialization_alias="prepTime",
    )
    cook_time: str | None = Field(
        None,
        description="The time required for cooking in ISO8601 format",
        validation_alias="cookTime",
        serialization_alias="cookTime",
    )
    total_time: str | None = Field(
        None,
        description="The time required for the complete processing in ISO8601 format",
        validation_alias="totalTime",
        serialization_alias="totalTime",
    )
    description: str = Field(
        "",
        description="A description of the recipe or the empty string",
    )
    url: str = Field(
        "",
        description="The URL the recipe was found at or the empty string",
    )
    image: str = Field("", description="The URL of the original recipe")
    servings: int = Field(
        1,
        description="The number of servings in the recipe",
        validation_alias="recipeYield",
        serialization_alias="recipeYield",
    )
    category: str = Field(
        "",
        description="The category of the recipe",
        examples=["Dessert"],
        validation_alias="recipeCategory",
        serialization_alias="recipeCategory",
    )
    tools: list[str] = Field(
        [],
        description="A list of tools used in the recipe",
        examples=["Flat and fire-resistent bowl"],
    )
    ingredients: list[str] = Field(
        [],
        description="A list of ingredients used in the recipe",
        examples=[["100g ripe Bananas"]],
        validation_alias="recipeIngredient",
        serialization_alias="recipeIngredient",
    )
    instructions: list[str] = Field(
        [],
        description="A list of instructions for processing the recipe",
        examples=["Peel the bananas"],
        validation_alias="recipeInstructions",
        serialization_alias="recipeInstructions",
    )
    nutrition: Nutrition = Field(
        description="Nutrition information for the recipe",
        examples=[
            {
                "@type": "NutritionInformation",
                "calories": "650 kcal",
                "carbohydrate_content": "300 g",
            },
        ],
    )
