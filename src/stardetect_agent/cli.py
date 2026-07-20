import uvicorn


def main() -> None:
    uvicorn.run(
        "stardetect_agent.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()

