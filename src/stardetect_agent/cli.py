import uvicorn

from stardetect_agent.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "stardetect_agent.api.main:app",
        host="0.0.0.0",
        port=settings.agent_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
