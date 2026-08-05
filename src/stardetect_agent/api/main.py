from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException

from stardetect_agent.agent.service import AgentService, AgentUpstreamError, get_agent_service
from stardetect_agent.api.schemas import ChatRequest, ChatResponse, HealthResponse
from stardetect_agent.config import Settings, get_settings
from stardetect_agent.system.provider import SystemMetricsProvider, get_metrics_provider
from stardetect_agent.system.tools import list_tool_metadata


@asynccontextmanager
async def lifespan(_: FastAPI):
    provider = get_metrics_provider()
    provider.start_sampling()
    try:
        yield
    finally:
        provider.stop_sampling()


app = FastAPI(
    title="Stardetect Iluvatar Agent",
    version="0.1.0",
    description="FastAPI service exposing a LangChain agent with Iluvatar GPU telemetry tools.",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(
        status="ok",
        model_backend="openai_compatible",
        model=settings.llm_model,
        base_url=settings.llm_base_url,
    )


@app.get("/api/tools")
def tools() -> dict[str, object]:
    return {"tools": list_tool_metadata()}


@app.get("/api/telemetry/snapshot")
def telemetry_snapshot(
    provider: Annotated[SystemMetricsProvider, Depends(get_metrics_provider)],
) -> dict[str, object]:
    return provider.get_system_snapshot()


@app.post("/api/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> ChatResponse:
    try:
        result = service.invoke(request.message, image_url=request.image_url)
    except AgentUpstreamError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": exc.code,
                "message": exc.message,
            },
        ) from exc
    return ChatResponse(**result)
