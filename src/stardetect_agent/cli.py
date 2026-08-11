import uvicorn

from stardetect_agent.config import get_settings
from stardetect_agent.observability import configure_structured_logging


def main() -> None:
    configure_structured_logging()
    settings = get_settings()
    uvicorn.run(
        "stardetect_agent.api.main:app",
        host="0.0.0.0",
        port=settings.agent_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
