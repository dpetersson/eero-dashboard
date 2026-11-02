from pydantic import BaseModel


class LabelRequest(BaseModel):
    name: str | None = None
    owner: str | None = None
    notes: str | None = None
    trusted: bool | None = None
