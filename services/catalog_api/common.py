from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Camel(BaseModel):
    """API models use camelCase on the wire and snake_case in Python."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
