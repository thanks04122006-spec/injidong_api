"""Stateless connectivity probe; deliberately independent of archive storage."""
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Injidong Connection Test", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


class EchoRequest(BaseModel):
    client: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)


@app.get("/")
def index():
    return {"service": "injidong-connection-test", "mode": "connectivity-only",
            "docs": "/docs", "health": "/health", "echo": "/api/v1/echo"}


@app.get("/health")
def health():
    return {"status": "ok", "mode": "connectivity-only"}


@app.post("/api/v1/echo")
def echo(body: EchoRequest):
    return {"status": "ok", "client": body.client, "message": body.message,
            "received_at": datetime.now(timezone.utc).isoformat()}
