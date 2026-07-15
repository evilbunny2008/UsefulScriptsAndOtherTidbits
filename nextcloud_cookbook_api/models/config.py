from pydantic import BaseModel, Field


class VisibleInfoBlocks(BaseModel):
    """Describing the configuration of the visible information blocks in the web app."""

    preparation_time: bool | None = Field(
        None,
        description="Show the preparation time in UI",
        examples=[True, False],
        serialization_alias="preparation-time",
        validation_alias="preparation-time",
    )
    cooking_time: bool | None = Field(
        None,
        description="Show the time required for cooking in the UI",
        examples=[True, False],
        serialization_alias="cooking-time",
        validation_alias="cooking-time",
    )
    total_time: bool | None = Field(
        None,
        description="Show the total time required to carry out the complee recipe",
        examples=[True, False],
        serialization_alias="total-time",
        validation_alias="total-time",
    )
    nutrition_information: bool | None = Field(
        None,
        description="Show the nutrition information in the UI",
        examples=[True, False],
        serialization_alias="nutrition-information",
        validation_alias="nutrition-information",
    )
    tools: bool | None = Field(None, description="Show the list of tools in the UI")


class Config(BaseModel):
    """Describing the configuration of the web app."""

    folder: str | None = Field(
        None,
        description="The folder in the user's files that contains the recipes",
        examples=["/Recipes"],
    )
    update_interval: int | None = Field(
        None,
        description="The interval between automatic rescans to rebuild the database cache in minutes",
        examples=[10],
    )

    print_image: bool | None = Field(
        None,
        description="True, if the user wished to print the recipe images with the rest of the recipes",
    )
    visible_info_blocks: VisibleInfoBlocks | None = Field(
        None,
        description="Which information blocks should be visible in the UI",
        validation_alias="visibleInfoBlocks",
        serialization_alias="visibleInfoBlocks",
    )
