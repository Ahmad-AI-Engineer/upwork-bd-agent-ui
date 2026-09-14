from __future__ import annotations
import asyncio
import json
import logging
import os
import threading
import time
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
    OAuthMetadata,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

logger = logging.getLogger("upwork_connection_handler")

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

HEADLESS = os.getenv("UPWORK_HEADLESS", "").strip().lower() in ("1", "true", "yes")

TOKEN_REFRESH_MARGIN_SECONDS = 300


class PermanentUpworkError(RuntimeError):
    """An error retrying cannot fix. run_with_session re-raises these immediately
    instead of spending its backoff budget on them."""


class AuthorizationRequiredError(PermanentUpworkError):
    pass


_HEADLESS_MESSAGE = (
    "Upwork authorization is required but no browser is available "
    f"(UPWORK_HEADLESS is set). The cached token at {{path}} is missing, expired, "
    "or could not be refreshed. Re-authorize on the host with: "
    "python src/upwork_connection.py"
)


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
        self._data = self._load()
        raw = self._data.get("tokens")
        if not raw:
            return None

        token = OAuthToken.model_validate(raw)
        expires_at = self._data.get("expires_at")

        if expires_at and token.refresh_token:
            remaining = float(expires_at) - time.time()
            if remaining <= TOKEN_REFRESH_MARGIN_SECONDS:
                logger.info(
                    "Cached access token expires in %.0fs; forcing refresh-token exchange",
                    remaining,
                )
                token.access_token = ""
        return token

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        if tokens.expires_in:
            self._data["expires_at"] = time.time() + float(tokens.expires_in)
        self._save()
        logger.info(
            "Stored Upwork token (expires_in=%s, refresh_token=%s)",
            tokens.expires_in,
            "yes" if tokens.refresh_token else "no",
        )

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
        def do_GET(self):
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
    if HEADLESS:
        raise AuthorizationRequiredError(_HEADLESS_MESSAGE.format(path=TOKEN_CACHE_PATH))
    logger.info("Opening browser for Upwork login/consent")
    logger.info("If it does not open automatically, visit: %s", auth_url)
    webbrowser.open(auth_url)


async def _callback_handler() -> AuthorizationCodeResult:
    if HEADLESS:
        raise AuthorizationRequiredError(_HEADLESS_MESSAGE.format(path=TOKEN_CACHE_PATH))
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


AS_METADATA_URL = "https://mcp.upwork.com/.well-known/oauth-authorization-server"


async def prime_oauth_metadata(provider: OAuthClientProvider) -> None:
    if provider.context.oauth_metadata is not None:
        return
    try:
        async with create_mcp_http_client() as client:
            response = await client.get(AS_METADATA_URL)
            response.raise_for_status()
            metadata = OAuthMetadata.model_validate_json(await response.aread())
        provider.context.oauth_metadata = metadata
        logger.debug("Primed OAuth metadata; token endpoint: %s", metadata.token_endpoint)
    except Exception:
        logger.warning("Could not prime OAuth metadata from %s", AS_METADATA_URL, exc_info=True)


@asynccontextmanager
async def upwork_session():
    auth = build_oauth_provider()
    await prime_oauth_metadata(auth)
    http_client = create_mcp_http_client(auth=auth)

    async with http_client:
        async with streamable_http_client(UPWORK_MCP_URL, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _find_permanent_error(exc: BaseException) -> PermanentUpworkError | None:
    if isinstance(exc, PermanentUpworkError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_permanent_error(sub)
            if found is not None:
                return found
    if exc.__cause__ is not None:
        found = _find_permanent_error(exc.__cause__)
        if found is not None:
            return found
    return None


async def run_with_session(operation, attempts: int = 4, initial_delay: float = 3.0):
    delay = initial_delay
    for attempt in range(1, attempts + 1):
        try:
            async with upwork_session() as session:
                return await operation(session)
        except Exception as exc:
            permanent = _find_permanent_error(exc)
            if permanent is not None:
                # Unwrapped from any ExceptionGroup, but deliberately not logged here:
                # the caller reports it, so it appears exactly once.
                raise permanent from None
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


def token_seconds_remaining() -> float | None:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not (data.get("tokens") or {}).get("access_token"):
        return None
    expires_at = data.get("expires_at")
    if not expires_at:
        return None
    return float(expires_at) - time.time()


async def ensure_access_token() -> bool:
    remaining = token_seconds_remaining()

    if remaining is None:
        logger.info("No cached access token; authorizing")
    elif remaining > TOKEN_REFRESH_MARGIN_SECONDS:
        logger.info("Access token valid for %.1f hour(s); no refresh needed", remaining / 3600)
        return True
    else:
        logger.info("Access token expired or expiring (%.0fs left); refreshing", remaining)

    transport_logger = logging.getLogger("mcp.client.streamable_http")
    previous_level = transport_logger.level
    transport_logger.setLevel(logging.CRITICAL)
    try:
        async with upwork_session():
            pass
    except AuthorizationRequiredError:
        logger.error(
            "Could not refresh the access token and no browser is available. "
            "Re-authorize on the host with: python orchestration/upwork_connection_handler.py"
        )
        return False
    except Exception:
        logger.exception("Access token refresh failed")
        return False

    finally:
        transport_logger.setLevel(previous_level)

    refreshed = token_seconds_remaining()
    if refreshed is None:
        logger.warning("Token refresh completed but no expiry was stored")
    else:
        logger.info("Access token refreshed; valid for %.1f hour(s)", refreshed / 3600)
    return True


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
