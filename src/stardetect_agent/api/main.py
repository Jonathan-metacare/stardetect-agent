from typing import Annotated

from fastapi import Depends, FastAPI

from stardetect_agent.agent.service import AgentService, get_agent_service
from stardetect_agent.api.schemas import ChatRequest, ChatResponse, HealthResponse
from stardetect_agent.config import Settings, get_settings
from stardetect_agent.jetson.provider import JetsonMetricsProvider, get_metrics_provider
from stardetect_agent.jetson.tools import list_tool_metadata

app = FastAPI(
    title="Stardetect Jetson Agent",
    version="0.1.0",
    description="FastAPI service exposing a LangChain agent with Jetson Orin telemetry tools.",
)


@app.get("/health", response_model=HealthResponse)
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(
        status="ok",
        ollama_model=settings.ollama_model,
        ollama_base_url=settings.ollama_base_url,
    )


@app.get("/api/tools")
def tools() -> dict[str, object]:
    return {"tools": list_tool_metadata()}


@app.get("/api/telemetry/snapshot")
def telemetry_snapshot(
    provider: Annotated[JetsonMetricsProvider, Depends(get_metrics_provider)],
) -> dict[str, object]:
    return provider.get_system_snapshot()


@app.post("/api/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> ChatResponse:
    result = service.invoke(request.message)
    return ChatResponse(**result)
