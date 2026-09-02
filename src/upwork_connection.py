from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

logger = logging.getLogger("upwork_connection")

UPWORK_MCP_URL = "https://mcp.upwork.com/mcp"

TOKEN_CACHE_PATH = Path(
    os.getenv(
        "UPWORK_TOKEN_CACHE_PATH",
        Path(__file__).resolve().parent.parent / ".secrets" / "upwork_mcp_tokens.json",
    )
)

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

OAUTH_CALLBACK_TIMEOUT_SECONDS = 300


class FileTokenStorage(TokenStorage):
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._data.get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._data.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save()


@dataclass
class _OAuthCallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None


def _wait_for_oauth_callback() -> _OAuthCallbackResult:
    import http.server

    result = _OAuthCallbackResult()
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            result.code = qs.get("code", [None])[0]
            result.state = qs.get("state", [None])[0]
            result.error = qs.get("error", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if result.error:
                body = f"<h1>Authorization failed</h1><p>{result.error}</p>"
            else:
                body = "<h1>Upwork authorized.</h1><p>You can close this tab and return to the terminal.</p>"
            self.wfile.write(body.encode())
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    done.wait(timeout=OAUTH_CALLBACK_TIMEOUT_SECONDS)
    server.server_close()
    return result


async def _redirect_handler(auth_url: str) -> None:
    logger.info("Opening browser for Upwork login/consent")
    logger.info("If it does not open automatically, visit: %s", auth_url)
    webbrowser.open(auth_url)


async def _callback_handler() -> AuthorizationCodeResult:
    result = await asyncio.to_thread(_wait_for_oauth_callback)
    if result.error or not result.code:
        raise RuntimeError(f"OAuth callback failed: {result.error or 'no code received'}")
    return AuthorizationCodeResult(code=result.code, state=result.state)


def build_oauth_provider() -> OAuthClientProvider:
    storage = FileTokenStorage(TOKEN_CACHE_PATH)
    client_metadata = OAuthClientMetadata(
        client_name="Upwork Bid Agent (local dev)",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    return OAuthClientProvider(
        server_url=UPWORK_MCP_URL,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
    )


@asynccontextmanager
async def upwork_session():
    auth = build_oauth_provider()
    http_client = create_mcp_http_client(auth=auth)

    async with http_client:
        async with streamable_http_client(UPWORK_MCP_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def run_with_session(operation, attempts: int = 4, initial_delay: float = 3.0):
    delay = initial_delay
    for attempt in range(1, attempts + 1):
        try:
            async with upwork_session() as session:
                return await operation(session)
        except Exception:
            if attempt == attempts:
                logger.error("MCP session failed after %d attempt(s)", attempts)
                raise
            logger.warning(
                "MCP session attempt %d/%d failed; retrying in %.0fs",
                attempt,
                attempts,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)
            delay *= 2


async def check_connection() -> None:
    async with upwork_session() as session:
        logger.info("Connected and initialized MCP session with Upwork")

        tools = await session.list_tools()
        logger.info("Available tools: %d", len(tools.tools))
        for t in tools.tools:
            logger.debug("  %s: %s", t.name, t.description or "")

        tool_names = [t.name for t in tools.tools]
        smoke_test_tool = next((n for n in tool_names if "list_accounts" in n.lower()), None)
        if smoke_test_tool:
            logger.info("Calling read-only smoke-test tool: %s", smoke_test_tool)
            result = await session.call_tool(smoke_test_tool, {})
            logger.info("Result: %s", result)
        else:
            logger.warning("No list_accounts tool found; inspect the tool list and call one manually")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(check_connection())
