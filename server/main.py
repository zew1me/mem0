import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi_mcp import FastApiMCP
from pydantic import BaseModel, Field
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select

from auth import ADMIN_API_KEY, AUTH_DISABLED, JWT_SECRET, verify_auth
from errors import (
    UpstreamError,
    install_request_id_logging,
    new_request_id,
    request_id_var,
    upstream_error,
    upstream_error_handler,
)
from rate_limit import limiter
from db import SessionLocal
from models import RequestLog, User
import telemetry
from routers import auth as auth_router
from routers import api_keys as api_keys_router
from routers import entities as entities_router
from routers import requests as requests_router
from routers.entities import TYPE_TO_FIELD, EntityType, list_entities_impl
from schemas import MessageResponse
from server_state import get_current_config, get_memory_instance, initialize_state, set_session_factory, update_config

load_dotenv()

install_request_id_logging()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [%(request_id)s] %(message)s")

MIN_KEY_LENGTH = 16
SENSITIVE_CONFIG_KEYS = {
    "admin_api_key",
    "api_key",
    "authorization",
    "jwt_secret",
    "password",
    "password_hash",
    "secret",
    "token",
}
SKIPPED_REQUEST_LOG_PATHS = {"/api/health", "/docs", "/redoc", "/openapi.json"}
SKIPPED_REQUEST_LOG_PREFIXES = ("/requests",)

BUNDLED_LLM_PROVIDERS = ("openai", "anthropic", "gemini")
BUNDLED_EMBEDDER_PROVIDERS = ("openai", "gemini")
MCP_OPERATION_IDS = [
    "add_memory",
    "search_memories",
    "get_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
    "delete_all_memories",
    "memory_history",
    "list_entities",
    "delete_entity",
]

_MCP_SERVER_DESCRIPTION = (
    "Persistent memory layer for AI agents and assistants. "
    "Stores, retrieves, and manages memories scoped by user, agent, or session.\n\n"
    "## Authentication\n"
    "Every tool call requires authentication. Pass the X-API-Key header set to your API key, "
    "or an Authorization: Bearer <jwt> header. Calls without a valid credential return 401.\n\n"
    "## Identity Model\n"
    "All memory operations are scoped to one identity field. Choose exactly one per call:\n"
    "- user_id: person-scoped, persists across all sessions and agents\n"
    "- agent_id: agent-role-scoped, shared across all users of that agent\n"
    "- run_id: session-scoped, ephemeral per conversation turn\n\n"
    "## Typical Workflow\n"
    "1. Call search_memories at the start of each turn to retrieve relevant past context.\n"
    "2. Call add_memory when the user shares facts, preferences, goals, or corrections.\n"
    "3. Use get_memories to list what is stored; delete_memory or delete_all_memories to forget.\n"
    "4. memory_history shows the evolution of a specific memory over time.\n"
    "5. list_entities / delete_entity manage identity-level records.\n"
)


def _warn_if_unconfigured() -> None:
    """Pre-auth deployments upgrading into this build will 401 everywhere until
    an admin key or admin user exists. Surface the fix before the support tickets."""
    try:
        with SessionLocal() as session:
            if session.scalar(select(func.count(User.id))) > 0:
                return
    except Exception:
        return

    logging.warning(
        "\n%s\n"
        "  Auth is enabled by default and this server has no admin configured.\n"
        "  Protected endpoints will return 401 until you either:\n"
        "    1. Set ADMIN_API_KEY=<long-random-value>  (fastest, no client changes)\n"
        "    2. Register an admin at http://<host>:3000/setup\n"
        "    3. Set AUTH_DISABLED=true                 (local development only)\n"
        "  Docs: https://docs.mem0.ai/open-source/features/rest-api#authentication\n"
        "%s",
        "=" * 72,
        "=" * 72,
    )


if not AUTH_DISABLED and not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is required. Set it in .env (generate with `openssl rand -base64 48`) "
        "or set AUTH_DISABLED=true for local development only."
    )

if AUTH_DISABLED:
    logging.warning("AUTH_DISABLED is enabled. Protected endpoints are open for local development only.")
elif ADMIN_API_KEY and len(ADMIN_API_KEY) < MIN_KEY_LENGTH:
    logging.warning(
        "ADMIN_API_KEY is shorter than %d characters - consider using a longer key for production.",
        MIN_KEY_LENGTH,
    )
elif not ADMIN_API_KEY:
    _warn_if_unconfigured()

telemetry.log_status()

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres")
POSTGRES_COLLECTION_NAME = os.environ.get("POSTGRES_COLLECTION_NAME", "memories")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
HISTORY_DB_PATH = os.environ.get("HISTORY_DB_PATH", "/app/history/history.db")
DEFAULT_LLM_MODEL = os.environ.get("MEM0_DEFAULT_LLM_MODEL", "gpt-4.1-nano-2025-04-14")
DEFAULT_EMBEDDER_MODEL = os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL", "text-embedding-3-small")

DEFAULT_CONFIG = {
    "version": "v1.1",
    "vector_store": {
        "provider": "pgvector",
        "config": {
            "host": POSTGRES_HOST,
            "port": int(POSTGRES_PORT),
            "dbname": POSTGRES_DB,
            "user": POSTGRES_USER,
            "password": POSTGRES_PASSWORD,
            "collection_name": POSTGRES_COLLECTION_NAME,
        },
    },
    "llm": {
        "provider": "openai",
        "config": {"api_key": OPENAI_API_KEY, "temperature": 0.2, "model": DEFAULT_LLM_MODEL},
    },
    "embedder": {"provider": "openai", "config": {"api_key": OPENAI_API_KEY, "model": DEFAULT_EMBEDDER_MODEL}},
    "history_db_path": HISTORY_DB_PATH,
}


set_session_factory(SessionLocal)
initialize_state(DEFAULT_CONFIG)


app = FastAPI(
    title="Mem0 REST APIs",
    description=(
        "A REST API for managing and searching memories for your AI Agents and Apps.\n\n"
        "## Authentication\n"
        "Supports Bearer JWT tokens, per-user API keys via `X-API-Key` header, "
        "or the legacy `ADMIN_API_KEY` environment variable. Set `AUTH_DISABLED=true` for local development only."
    ),
    version="1.0.0",
    redirect_slashes=False,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_exception_handler(UpstreamError, upstream_error_handler)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[DASHBOARD_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(api_keys_router.router)
app.include_router(entities_router.router)
app.include_router(requests_router.router)


class Message(BaseModel):
    role: str = Field(..., description="Role of the message (user or assistant).")
    content: str = Field(..., description="Message content.")


_IDENTITY_FIELDS_DESC = (
    "Provide exactly one: user_id (person-scoped, persists across sessions), "
    "agent_id (agent-role-scoped), or run_id (session-scoped, ephemeral)."
)


class MemoryCreate(BaseModel):
    messages: List[Message] = Field(
        ...,
        description="Conversation messages to extract memories from. Include recent turns for best extraction quality.",
    )
    user_id: Optional[str] = Field(None, description="Scopes memory to a specific person. Persists across all sessions.")
    agent_id: Optional[str] = Field(None, description="Scopes memory to an agent role. Shared across all users of that agent.")
    run_id: Optional[str] = Field(None, description="Scopes memory to a single conversation session. Ephemeral.")
    metadata: Optional[Dict[str, Any]] = None
    infer: Optional[bool] = Field(
        None,
        description="When True (default), the server extracts discrete facts from messages. Set False to store verbatim.",
    )
    memory_type: Optional[str] = Field(
        None,
        description="Type of memory to store. Use 'core' for default memories or 'procedural_memory'.",
    )
    prompt: Optional[str] = Field(None, description="Custom prompt to use for fact extraction.")


class MemoryUpdate(BaseModel):
    text: str = Field(..., description="New content to update the memory with.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata to update.")


class SearchRequest(BaseModel):
    query: str = Field(..., description="Natural language search query.")
    user_id: Optional[str] = Field(None, description="Scopes search to a specific person.")
    run_id: Optional[str] = Field(None, description="Scopes search to a specific conversation session.")
    agent_id: Optional[str] = Field(None, description="Scopes search to an agent role.")
    filters: Optional[Dict[str, Any]] = None
    top_k: Optional[int] = Field(None, description="Maximum number of results to return.")
    threshold: Optional[float] = Field(None, description="Minimum similarity score for results (0.0–1.0).")


# MCP shim request models — POST body wrappers for operations the REST API exposes via GET/PUT/DELETE


class MemoryListRequest(BaseModel):
    user_id: Optional[str] = Field(None, description="Scopes listing to a specific person.")
    agent_id: Optional[str] = Field(None, description="Scopes listing to an agent role.")
    run_id: Optional[str] = Field(None, description="Scopes listing to a conversation session.")


class MemoryFetchRequest(BaseModel):
    memory_id: str = Field(..., description="ID of the memory to retrieve. Obtain from add_memory, search_memories, or get_memories.")


class MemoryHistoryRequest(BaseModel):
    memory_id: str = Field(..., description="ID of the memory whose change history to retrieve.")


class MemoryUpdateMCPRequest(BaseModel):
    memory_id: str = Field(..., description="ID of the memory to update. Obtain from search_memories or get_memories.")
    text: str = Field(..., description="New text content for the memory.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Optional metadata to update.")


class MemoryDeleteRequest(BaseModel):
    memory_id: str = Field(..., description="ID of the memory to delete. Obtain from search_memories or get_memories.")


class MemoryDeleteAllRequest(BaseModel):
    user_id: Optional[str] = Field(None, description="Delete all memories for this person.")
    agent_id: Optional[str] = Field(None, description="Delete all memories for this agent role.")
    run_id: Optional[str] = Field(None, description="Delete all memories for this conversation session.")


class EntityDeleteRequest(BaseModel):
    entity_type: EntityType = Field(..., description="Type of entity: 'user', 'agent', or 'run'.")
    entity_id: str = Field(..., description="ID of the entity whose memories should all be deleted.")


class GenerateInstructionsRequest(BaseModel):
    use_case: str = Field(..., description="Description of what the user will use Mem0 for.")


def _redact_config(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {item_key: _redact_config(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_config(item_value, key) for item_value in value]
    if key is not None and key.lower() in SENSITIVE_CONFIG_KEYS:
        return "[redacted]" if value else value
    return value


def _validate_bundled_providers(config: Dict[str, Any]) -> None:
    llm = config.get("llm")
    if isinstance(llm, dict) and (provider := llm.get("provider")) and provider not in BUNDLED_LLM_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"LLM provider '{provider}' is not bundled in this image. "
                f"Bundled providers: {', '.join(BUNDLED_LLM_PROVIDERS)}. "
                "To use another provider, install its Python package, rebuild the container, "
                "and extend BUNDLED_LLM_PROVIDERS in server/main.py."
            ),
        )

    embedder = config.get("embedder")
    if (
        isinstance(embedder, dict)
        and (provider := embedder.get("provider"))
        and provider not in BUNDLED_EMBEDDER_PROVIDERS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Embedder provider '{provider}' is not bundled in this image. "
                f"Bundled providers: {', '.join(BUNDLED_EMBEDDER_PROVIDERS)}. "
                "To use another provider, install its Python package, rebuild the container, "
                "and extend BUNDLED_EMBEDDER_PROVIDERS in server/main.py."
            ),
        )


def _should_log_request(request: Request) -> bool:
    if request.method == "OPTIONS":
        return False
    path = request.url.path
    if path in SKIPPED_REQUEST_LOG_PATHS:
        return False
    return not path.startswith(SKIPPED_REQUEST_LOG_PREFIXES)


def _persist_request_log(method: str, path: str, status_code: int, latency_ms: float, auth_type: str) -> None:
    session = SessionLocal()

    try:
        session.add(
            RequestLog(
                method=method,
                path=path,
                status_code=status_code,
                latency_ms=latency_ms,
                auth_type=auth_type,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        logging.exception("Failed to persist request log")
    finally:
        session.close()


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request.state.auth_type = getattr(request.state, "auth_type", "none")
    rid = new_request_id()
    token = request_id_var.set(rid)
    start = time.perf_counter()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        request_id_var.reset(token)
        if _should_log_request(request):
            asyncio.get_running_loop().run_in_executor(
                None,
                _persist_request_log,
                request.method,
                request.url.path,
                status_code,
                round((time.perf_counter() - start) * 1000, 2),
                getattr(request.state, "auth_type", "none"),
            )


@app.get("/api/health", summary="Health check", include_in_schema=False)
def health_check():
    return {"status": "ok"}


@app.get("/configure", summary="Get current Mem0 configuration")
def get_config(_auth=Depends(verify_auth)):
    return _redact_config(get_current_config())


@app.get("/configure/providers", summary="List bundled LLM and embedder providers")
def list_bundled_providers(_auth=Depends(verify_auth)):
    return {"llm": list(BUNDLED_LLM_PROVIDERS), "embedder": list(BUNDLED_EMBEDDER_PROVIDERS)}


@app.post("/configure", summary="Configure Mem0")
def set_config(config: Dict[str, Any], _auth=Depends(verify_auth)):
    """Set memory configuration."""
    _validate_bundled_providers(config)
    update_config(config)
    return {"message": "Configuration set successfully"}


@app.post("/generate-instructions", summary="Generate custom instructions from a use case")
def generate_instructions(req: GenerateInstructionsRequest, _auth=Depends(verify_auth)):
    """Generate custom instructions and a contextual test message tailored to a use case."""
    try:
        llm = get_memory_instance().llm
        prompt = (
            "You are configuring a memory system. Given the use case below, produce two things:\n"
            "1. INSTRUCTIONS: A short paragraph of custom instructions telling the memory extraction system "
            "what kinds of facts, preferences, and context to prioritize. Be specific to the use case.\n"
            "2. TEST_MESSAGE: A single realistic sentence a user in this use case would say, suitable for "
            "testing that the memory system works.\n\n"
            "Respond in exactly this format (no markdown, no extra text):\n"
            "INSTRUCTIONS: <your instructions>\n"
            f"TEST_MESSAGE: <your test message>\n\nUse case: {req.use_case}"
        )
        response = llm.generate_response([{"role": "user", "content": prompt}])
        instructions = response
        test_message = "I like to hike on weekends."
        if "INSTRUCTIONS:" in response and "TEST_MESSAGE:" in response:
            parts = response.split("TEST_MESSAGE:")
            instructions = parts[0].replace("INSTRUCTIONS:", "").strip()
            test_message = parts[1].strip()
        return {"custom_instructions": instructions, "test_message": test_message}
    except Exception:
        raise upstream_error()


def _memory_add_params(memory_create: MemoryCreate) -> Dict[str, Any]:
    params = {k: v for k, v in memory_create.model_dump().items() if v is not None and k != "messages"}
    if params.get("memory_type") == "core":
        params.pop("memory_type")
    return params


@app.post("/memories", summary="Create memories", operation_id="add_memory")
def add_memory(memory_create: MemoryCreate, _auth=Depends(verify_auth)):
    """
    Call this tool whenever the user shares personal preferences, facts, goals, or any context
    worth retaining across future sessions. Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    Identity: provide exactly one of user_id (person-scoped), agent_id (agent-scoped),
    or run_id (session-scoped). Use user_id for persistent personal memory; run_id for
    ephemeral within-session context.

    The server infers discrete memories from the messages list by default (infer=True).
    Set infer=False to store message content verbatim without extraction.
    Returns a list of created/updated memory records including their IDs.
    """
    if not any([memory_create.user_id, memory_create.agent_id, memory_create.run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier (user_id, agent_id, run_id) is required.")

    params = _memory_add_params(memory_create)
    try:
        response = get_memory_instance().add(messages=[m.model_dump() for m in memory_create.messages], **params)
        return JSONResponse(content=response)
    except Exception:
        raise upstream_error()


ALL_MEMORIES_LIMIT = 1000
_RESERVED_PAYLOAD_KEYS = {"data", "user_id", "agent_id", "run_id", "hash", "created_at", "updated_at"}


def _serialize_memory(row: Any) -> Dict[str, Any]:
    payload = getattr(row, "payload", None) or {}
    return {
        "id": getattr(row, "id", None),
        "memory": payload.get("data"),
        "user_id": payload.get("user_id"),
        "agent_id": payload.get("agent_id"),
        "run_id": payload.get("run_id"),
        "hash": payload.get("hash"),
        "metadata": {k: v for k, v in payload.items() if k not in _RESERVED_PAYLOAD_KEYS},
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


def _list_all_memories(limit: int = ALL_MEMORIES_LIMIT) -> Dict[str, Any]:
    results = get_memory_instance().vector_store.list(top_k=limit)
    rows = results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []
    return {"results": [_serialize_memory(row) for row in rows]}


@app.get("/memories", summary="Get memories", operation_id="get_memories_rest")
def get_all_memories(
    user_id: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    _auth=Depends(verify_auth),
):
    """Retrieve stored memories. Lists all memories when no identifier is provided."""
    try:
        if not any([user_id, run_id, agent_id]):
            return _list_all_memories()
        filters = {
            k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v is not None
        }
        return get_memory_instance().get_all(filters=filters)
    except Exception:
        raise upstream_error()


@app.get("/memories/{memory_id}", summary="Get a memory", operation_id="get_memory_rest")
def get_memory(memory_id: str, _auth=Depends(verify_auth)):
    """Retrieve a specific memory by ID."""
    try:
        return get_memory_instance().get(memory_id)
    except Exception:
        raise upstream_error()


@app.post("/search", summary="Search memories", operation_id="search_memories")
def search_memories(search_req: SearchRequest, _auth=Depends(verify_auth)):
    """
    Call this tool BEFORE generating any response when the user's query may benefit from past context.
    Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    Identity: provide the same identifier used when storing memories (user_id, agent_id, or run_id).
    Returns semantically ranked results; use the 'memory' field from each result as context to inject
    into your response. At least one identity field is required.
    """
    if not any([search_req.user_id, search_req.agent_id, search_req.run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier (user_id, agent_id, run_id) is required.")
    try:
        entity_keys = {"user_id", "agent_id", "run_id"}
        filters = {k: v for k, v in search_req.model_dump().items() if k in entity_keys and v is not None}
        if search_req.filters:
            filters.update(search_req.filters)
        params = {k: v for k, v in search_req.model_dump().items() if v is not None and k not in entity_keys | {"query", "filters"}}
        return get_memory_instance().search(query=search_req.query, filters=filters, **params)
    except Exception:
        raise upstream_error()


@app.put("/memories/{memory_id}", summary="Update a memory", operation_id="update_memory_rest")
def update_memory(memory_id: str, updated_memory: MemoryUpdate, _auth=Depends(verify_auth)):
    """Update an existing memory."""
    try:
        return get_memory_instance().update(
            memory_id=memory_id, data=updated_memory.text, metadata=updated_memory.metadata
        )
    except Exception:
        raise upstream_error()


@app.get("/memories/{memory_id}/history", summary="Get memory history", operation_id="memory_history_rest")
def memory_history(memory_id: str, _auth=Depends(verify_auth)):
    """Retrieve memory history."""
    try:
        return get_memory_instance().history(memory_id=memory_id)
    except Exception:
        raise upstream_error()


@app.delete(
    "/memories/{memory_id}",
    summary="Delete a memory",
    response_model=MessageResponse,
    operation_id="delete_memory_rest",
)
def delete_memory(memory_id: str, _auth=Depends(verify_auth)):
    """Delete a specific memory by ID."""
    try:
        get_memory_instance().delete(memory_id=memory_id)
        return MessageResponse(message="Memory deleted successfully")
    except Exception:
        raise upstream_error()


@app.delete(
    "/memories",
    summary="Delete all memories",
    response_model=MessageResponse,
    operation_id="delete_all_memories_rest",
)
def delete_all_memories(
    user_id: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    _auth=Depends(verify_auth),
):
    """Delete all memories for a given identifier."""
    if not any([user_id, run_id, agent_id]):
        raise HTTPException(status_code=400, detail="At least one identifier is required.")
    try:
        params = {
            k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v is not None
        }
        get_memory_instance().delete_all(**params)
        return MessageResponse(message="All relevant memories deleted")
    except Exception:
        raise upstream_error()


@app.post("/reset", summary="Reset all memories")
def reset_memory(_auth=Depends(verify_auth)):
    """Completely reset stored memories."""
    try:
        get_memory_instance().reset()
        return {"message": "All memories reset"}
    except Exception:
        raise upstream_error()


@app.get("/", summary="Redirect to the OpenAPI documentation", include_in_schema=False)
def home():
    """Redirect to the OpenAPI documentation."""
    return RedirectResponse(url="/docs")


# ---------------------------------------------------------------------------
# MCP tool shims — POST wrappers for REST operations that use GET/PUT/DELETE.
# fastapi-mcp only maps POST routes to MCP tools; these shims give every
# operation a JSON-body POST surface while the original REST routes remain
# unchanged for HTTP API clients.
# ---------------------------------------------------------------------------


@app.post("/mcp-tools/memories/list", summary="List memories (MCP)", operation_id="get_memories", tags=["mcp-tools"])
def mcp_get_memories(req: MemoryListRequest, _auth=Depends(verify_auth)):
    """
    List all stored memories for a given identity. Use this to show the user what is remembered,
    or to collect memory IDs before updating or deleting specific entries.
    Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    Provide at least one identity field: user_id (person), agent_id (agent role), or run_id (session).
    Omit all three to list every memory regardless of identity.
    Returns a 'results' array; each item has 'id', 'memory', and identity fields.
    """
    try:
        if not any([req.user_id, req.agent_id, req.run_id]):
            return _list_all_memories()
        filters = {k: v for k, v in {"user_id": req.user_id, "agent_id": req.agent_id, "run_id": req.run_id}.items() if v is not None}
        return get_memory_instance().get_all(filters=filters)
    except Exception:
        raise upstream_error()


@app.post("/mcp-tools/memories/fetch", summary="Get a memory (MCP)", operation_id="get_memory", tags=["mcp-tools"])
def mcp_get_memory(req: MemoryFetchRequest, _auth=Depends(verify_auth)):
    """
    Retrieve a single memory by its ID. Use when you need the full details of a specific memory
    whose ID you obtained from add_memory, search_memories, or get_memories.
    Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    Returns the memory object including its text, metadata, and timestamps.
    """
    try:
        return get_memory_instance().get(req.memory_id)
    except Exception:
        raise upstream_error()


@app.post("/mcp-tools/memories/update", summary="Update a memory (MCP)", operation_id="update_memory", tags=["mcp-tools"])
def mcp_update_memory(req: MemoryUpdateMCPRequest, _auth=Depends(verify_auth)):
    """
    Update the text content of an existing memory. Use when the user corrects or refines
    a previously stored fact. Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    memory_id must be obtained from a prior search_memories or get_memories call.
    Returns the updated memory record.
    """
    try:
        return get_memory_instance().update(memory_id=req.memory_id, data=req.text, metadata=req.metadata)
    except Exception:
        raise upstream_error()


@app.post("/mcp-tools/memories/delete", summary="Delete a memory (MCP)", operation_id="delete_memory", response_model=MessageResponse, tags=["mcp-tools"])
def mcp_delete_memory(req: MemoryDeleteRequest, _auth=Depends(verify_auth)):
    """
    Delete a specific memory by ID. Use when the user explicitly asks to forget a particular fact.
    Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    memory_id must be obtained from a prior search_memories or get_memories call.
    This action is irreversible.
    """
    try:
        get_memory_instance().delete(memory_id=req.memory_id)
        return MessageResponse(message="Memory deleted successfully")
    except Exception:
        raise upstream_error()


@app.post("/mcp-tools/memories/delete-all", summary="Delete all memories (MCP)", operation_id="delete_all_memories", response_model=MessageResponse, tags=["mcp-tools"])
def mcp_delete_all_memories(req: MemoryDeleteAllRequest, _auth=Depends(verify_auth)):
    """
    Delete ALL memories for a given identity. Use only when the user explicitly asks to wipe
    everything they have stored. This is irreversible.
    Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    Provide at least one identity field: user_id, agent_id, or run_id.
    """
    if not any([req.user_id, req.agent_id, req.run_id]):
        raise HTTPException(status_code=400, detail="At least one identity field is required.")
    try:
        params = {k: v for k, v in {"user_id": req.user_id, "agent_id": req.agent_id, "run_id": req.run_id}.items() if v is not None}
        get_memory_instance().delete_all(**params)
        return MessageResponse(message="All relevant memories deleted")
    except Exception:
        raise upstream_error()


@app.post("/mcp-tools/memories/history", summary="Get memory history (MCP)", operation_id="memory_history", tags=["mcp-tools"])
def mcp_memory_history(req: MemoryHistoryRequest, _auth=Depends(verify_auth)):
    """
    Retrieve the full change history of a specific memory: when it was created, how its text
    evolved, and when it was deleted (if applicable). Use to audit or explain to the user how
    a memory has changed over time.
    Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    memory_id must be obtained from a prior search_memories or get_memories call.
    """
    try:
        return get_memory_instance().history(memory_id=req.memory_id)
    except Exception:
        raise upstream_error()


@app.post("/mcp-tools/entities/list", summary="List entities (MCP)", operation_id="list_entities", tags=["mcp-tools"])
def mcp_list_entities(_auth=Depends(verify_auth)):
    """
    List all known entities (users, agents, sessions) that have stored memories, along with
    their memory counts and timestamps. Use to discover what identity IDs exist before
    querying or deleting memories for a specific entity.
    Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    Returns an array of entity objects with 'id', 'type' ('user'|'agent'|'run'),
    'total_memories', 'created_at', and 'updated_at'.
    """
    return list_entities_impl()


@app.post("/mcp-tools/entities/delete", summary="Delete entity (MCP)", operation_id="delete_entity", response_model=MessageResponse, tags=["mcp-tools"])
def mcp_delete_entity(req: EntityDeleteRequest, _auth=Depends(verify_auth)):
    """
    Delete ALL memories belonging to a specific entity. This removes every memory stored
    under that user_id, agent_id, or run_id. Use when the user asks to be fully forgotten,
    or to clean up a stale agent or session.
    Requires auth: X-API-Key header (or Authorization: Bearer <jwt>).

    entity_type must be 'user', 'agent', or 'run'. entity_id is the identity value.
    Obtain valid entity IDs from list_entities. This action is irreversible.
    """
    try:
        get_memory_instance().delete_all(**{TYPE_TO_FIELD[req.entity_type]: req.entity_id})
    except Exception:
        raise upstream_error()
    return MessageResponse(message="Entity deleted")


mcp = FastApiMCP(
    app,
    name="Mem0 Local MCP",
    description=_MCP_SERVER_DESCRIPTION,
    include_operations=MCP_OPERATION_IDS,
    headers=["authorization", "x-api-key"],
)
mcp.mount_http()
