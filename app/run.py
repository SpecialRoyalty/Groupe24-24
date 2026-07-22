import os

import uvicorn


def get_port() -> int:
    raw_port = os.getenv("PORT", "8000").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError(f"PORT doit être un entier, valeur reçue: {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"PORT hors limites: {port}")
    return port


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=get_port(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
