"""
MSD Congress Activity Dashboard — Secure Backend
FastAPI server with:
  - SQLite data store (SQL Server-ready — swap connection string)
  - JWT authentication middleware (all /api/data/* endpoints)
  - AI chat endpoint (/api/chat)
  - Static file serving

JWT Integration for Event Cadence:
  1. Both apps share JWT_SECRET (set as environment variable)
  2. Event Cadence generates a short-lived token when the user opens the dashboard
  3. Token is passed as ?token=<jwt> in the iframe src URL
  4. This server validates the token on every data request

Token payload shape:
  { "sub": "user@email.com", "role": "admin|viewer", "exp": <unix timestamp> }
"""
import os
import sqlite3
import json
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from anthropic import Anthropic

# ── Config ─────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
DB_PATH     = BASE / "dashboard.db"
LLM_CONTEXT = (BASE / "llm_context.txt").read_text()

# JWT secret — set JWT_SECRET env var in production.
# Default shown here is for local dev only; NEVER use this default in production.
JWT_SECRET    = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"

# If True, ALL /api/data/* endpoints require a valid JWT.
# Set REQUIRE_AUTH=false to run without auth during local development.
REQUIRE_AUTH  = os.environ.get("REQUIRE_AUTH", "true").lower() != "false"

# ── App setup ──────────────────────────────────────────────────────────────
# root_path makes FastAPI aware it is mounted under a subpath when behind a
# reverse-proxy or when the domain serves the app at /msd/activityDashboard.
ROOT_PATH = os.environ.get("ROOT_PATH", "/msd/activityDashboard")
app = FastAPI(title="MSD Congress Activity Dashboard API", root_path=ROOT_PATH)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Tighten to your Event Cadence domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database helpers ───────────────────────────────────────────────────────
@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()

# ── JWT auth ───────────────────────────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)

def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    """
    Validates the Bearer JWT.  Returns the decoded payload.
    If REQUIRE_AUTH is False (local dev), always passes.
    """
    if not REQUIRE_AUTH:
        return {"sub": "dev", "role": "admin"}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

# ── Token verification endpoint (for client bootstrap) ────────────────────
@app.get("/api/auth/verify")
async def auth_verify(token_payload: dict = Depends(verify_token)):
    """
    Client calls this first to confirm the token is valid.
    Returns the user info from the token payload.
    """
    return {"ok": True, "user": token_payload.get("sub"), "role": token_payload.get("role", "viewer")}

# ── Data endpoints ─────────────────────────────────────────────────────────

@app.get("/api/data/events")
async def get_events(token_payload: dict = Depends(verify_token)):
    """All events with aggregate stats."""
    with get_db() as con:
        rows = con.execute("""
            SELECT
                e.name                          AS event_name,
                COUNT(DISTINCT t.id)            AS title_count,
                COALESCE(SUM(t.unique_users),0) AS unique_users,
                COALESCE(SUM(t.total_actions),0)AS total_actions
            FROM events e
            LEFT JOIN titles t ON t.event_id = e.id
            GROUP BY e.id, e.name
            ORDER BY e.name
        """).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/data/events/{event_name}/titles")
async def get_titles(event_name: str, token_payload: dict = Depends(verify_token)):
    """All job titles for a given event."""
    with get_db() as con:
        rows = con.execute("""
            SELECT
                t.name                                          AS title_name,
                t.unique_users,
                t.total_actions,
                COUNT(DISTINCT u.id)                            AS user_count
            FROM titles t
            JOIN events e ON e.id = t.event_id
            LEFT JOIN users u ON u.title_id = t.id
            WHERE e.name = ?
            GROUP BY t.id
            ORDER BY t.total_actions DESC
        """, (event_name,)).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Event not found")
    return [dict(r) for r in rows]


@app.get("/api/data/events/{event_name}/titles/{title_name}/users")
async def get_users(event_name: str, title_name: str, token_payload: dict = Depends(verify_token)):
    """All users for a given event + title."""
    with get_db() as con:
        rows = con.execute("""
            SELECT
                u.email,
                u.total_actions,
                COUNT(DISTINCT a.action) AS action_types
            FROM users u
            JOIN titles t  ON t.id = u.title_id
            JOIN events e  ON e.id = t.event_id
            LEFT JOIN actions a ON a.user_id = u.id
            WHERE e.name = ? AND t.name = ?
            GROUP BY u.id
            ORDER BY u.total_actions DESC
        """, (event_name, title_name)).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Event or title not found")
    return [dict(r) for r in rows]


@app.get("/api/data/events/{event_name}/titles/{title_name}/users/{email}/actions")
async def get_user_actions(
    event_name: str, title_name: str, email: str,
    token_payload: dict = Depends(verify_token)
):
    """Action breakdown for a specific user."""
    with get_db() as con:
        rows = con.execute("""
            SELECT a.action, a.count
            FROM actions a
            JOIN users u   ON u.id = a.user_id
            JOIN titles t  ON t.id = u.title_id
            JOIN events e  ON e.id = t.event_id
            WHERE e.name = ? AND t.name = ? AND u.email = ?
            ORDER BY a.count DESC
        """, (event_name, title_name, email)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/data/titles")
async def get_all_titles(token_payload: dict = Depends(verify_token)):
    """All job titles rolled up across all events."""
    with get_db() as con:
        rows = con.execute("""
            SELECT
                t.name                                  AS title_name,
                COUNT(DISTINCT t.event_id)              AS event_count,
                COALESCE(SUM(t.unique_users),0)         AS unique_users,
                COALESCE(SUM(t.total_actions),0)        AS total_actions
            FROM titles t
            GROUP BY t.name
            ORDER BY total_actions DESC
        """).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/data/titles/{title_name}/events")
async def get_title_events(title_name: str, token_payload: dict = Depends(verify_token)):
    """All events a given title appears in."""
    with get_db() as con:
        rows = con.execute("""
            SELECT
                e.name          AS event_name,
                t.unique_users,
                t.total_actions
            FROM titles t
            JOIN events e ON e.id = t.event_id
            WHERE t.name = ?
            ORDER BY t.total_actions DESC
        """, (title_name,)).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Title not found")
    return [dict(r) for r in rows]


@app.get("/api/data/actions")
async def get_action_types(token_payload: dict = Depends(verify_token)):
    """All distinct action types in the dataset."""
    with get_db() as con:
        rows = con.execute("SELECT DISTINCT action FROM actions ORDER BY action").fetchall()
    return [r["action"] for r in rows]


@app.get("/api/data/events/{event_name}/action-counts")
async def get_event_action_counts(event_name: str, token_payload: dict = Depends(verify_token)):
    """Per-action totals for a given event (for Show Action Details)."""
    with get_db() as con:
        rows = con.execute("""
            SELECT a.action, SUM(a.count) AS total
            FROM actions a
            JOIN users u  ON u.id = a.user_id
            JOIN titles t ON t.id = u.title_id
            JOIN events e ON e.id = t.event_id
            WHERE e.name = ?
            GROUP BY a.action
            ORDER BY a.action
        """, (event_name,)).fetchall()
    return {r["action"]: r["total"] for r in rows}


@app.get("/api/data/events/{event_name}/titles/{title_name}/action-counts")
async def get_title_action_counts(
    event_name: str, title_name: str,
    token_payload: dict = Depends(verify_token)
):
    """Per-action totals for a given event+title."""
    with get_db() as con:
        rows = con.execute("""
            SELECT a.action, SUM(a.count) AS total
            FROM actions a
            JOIN users u  ON u.id = a.user_id
            JOIN titles t ON t.id = u.title_id
            JOIN events e ON e.id = t.event_id
            WHERE e.name = ? AND t.name = ?
            GROUP BY a.action
            ORDER BY a.action
        """, (event_name, title_name)).fetchall()
    return {r["action"]: r["total"] for r in rows}


@app.get("/api/data/titles/{title_name}/action-counts")
async def get_all_title_action_counts(
    title_name: str,
    token_payload: dict = Depends(verify_token)
):
    """Per-action totals for a title across ALL events."""
    with get_db() as con:
        rows = con.execute("""
            SELECT a.action, SUM(a.count) AS total
            FROM actions a
            JOIN users u  ON u.id = a.user_id
            JOIN titles t ON t.id = u.title_id
            WHERE t.name = ?
            GROUP BY a.action
            ORDER BY a.action
        """, (title_name,)).fetchall()
    return {r["action"]: r["total"] for r in rows}


@app.get("/api/data/chart")
async def get_chart_data(token_payload: dict = Depends(verify_token)):
    """Top-50 titles × all action types for the Charts page."""
    with get_db() as con:
        # Get top 50 titles by total actions
        top_titles = con.execute("""
            SELECT name FROM (
                SELECT name, SUM(total_actions) AS ta
                FROM titles GROUP BY name ORDER BY ta DESC LIMIT 50
            )
        """).fetchall()
        top_title_names = [r["name"] for r in top_titles]

        action_types = con.execute(
            "SELECT DISTINCT action FROM actions ORDER BY action"
        ).fetchall()
        action_names = [r["action"] for r in action_types]

        # Build matrix
        result = {}
        for title in top_title_names:
            rows = con.execute("""
                SELECT a.action, SUM(a.count) AS total
                FROM actions a
                JOIN users u  ON u.id = a.user_id
                JOIN titles t ON t.id = u.title_id
                WHERE t.name = ?
                GROUP BY a.action
            """, (title,)).fetchall()
            result[title] = {r["action"]: r["total"] for r in rows}

    return {"titles": top_title_names, "actions": action_names, "data": result}


# ── Token generation helper (for Event Cadence integration / testing) ──────
@app.post("/api/auth/generate-token")
async def generate_token(request: Request):
    """
    DEV ONLY — generates a test JWT. Remove or restrict in production.
    Event Cadence should generate tokens server-side using the shared secret.

    POST body: { "sub": "user@email.com", "role": "admin", "expires_in": 3600 }
    """
    if os.environ.get("ALLOW_TOKEN_GENERATION", "true").lower() != "true":
        raise HTTPException(status_code=403, detail="Token generation disabled in production")

    body = await request.json()
    import time
    payload = {
        "sub":  body.get("sub", "test@example.com"),
        "role": body.get("role", "viewer"),
        "exp":  int(time.time()) + body.get("expires_in", 3600),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"token": token, "expires_in": body.get("expires_in", 3600)}


# ── AI Chat ────────────────────────────────────────────────────────────────
anthropic_client = Anthropic()

SYSTEM_PROMPT = f"""You are an expert analyst assistant for the MSD Congress Activity Dashboard.
You help users understand how MSD employees use the Event Cadence mobile app at medical congresses.

The dashboard tracks menu/feature usage (called "actions") by job title across 91 medical congress events in 2025.
Actions include: All Appointments, Appointments, Attendees, Attending/Not Attending, Customer Lists, Customers,
Engagements, Events, Explore, Full Schedule, Groups, Home, Live Feed, Location Availability, Maps, Master List,
My Appointments, My Schedule, People, Profile, Schedule, Speakers, Support and Settings, Travel.

Below is a full data summary of the dashboard:

{LLM_CONTEXT}

Answer questions concisely and specifically. Use numbers from the data. When listing items, keep it to the top 5-7 unless asked for more.
Format your answers clearly — use bullet points or short paragraphs as appropriate.
If asked about something not in the data, say so clearly rather than guessing.
Do not make up data. All numbers must come from the summary above."""

@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse({"error": "No messages provided"}, status_code=400)
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages
        )
        return JSONResponse({"answer": response.content[0].text})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Static files (must be last) ────────────────────────────────────────────
# Serve static files at root.  FastAPI's root_path prefix is applied
# automatically by the ASGI server for the OpenAPI docs; the static file
# mount stays at "/" so Railway can serve index.html from the domain root
# while nginx on docs.incadence.com proxies /msd/activityDashboard/ → here.
app.mount("/", StaticFiles(directory=str(BASE), html=True), name="static")
