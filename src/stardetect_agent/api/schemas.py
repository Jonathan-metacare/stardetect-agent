from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    ollama_model: str
    ollama_base_url: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, object]] = Field(default_factory=list)

