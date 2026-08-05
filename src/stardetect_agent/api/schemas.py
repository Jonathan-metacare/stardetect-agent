from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    status: str
    model_backend: str
    model: str
    base_url: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    image_url: str | None = Field(default=None, min_length=1, max_length=14_000_000)

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str | None) -> str | None:
        if value is None:
            return None

        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value
        if value.startswith("data:image/") and ";base64," in value:
            return value
        raise ValueError("image_url must be an http(s) URL or a base64 data:image URL")


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, object]] = Field(default_factory=list)
