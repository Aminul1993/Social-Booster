"""Local stand-ins for Ollama Cloud and Buffer - demo and UI-test the full flow offline.

Buffer needs an account with an API key and Ollama Cloud needs a paid key, so
this tiny server mimics Buffer's GraphQL endpoint and the chat completions
endpoint the app uses (image descriptions and copy). Nothing is posted anywhere.

    python scripts/mock_upstreams.py --port 8020

Then start the app with (e.g. in .env):

    OLLAMA_ENDPOINT=http://127.0.0.1:8020/v1/chat/completions
    OLLAMA_API_KEY=mock
    BUFFER_ACCESS_TOKEN=mock
    BUFFER_API_URL=http://127.0.0.1:8020/graphql

Received posts are listed at http://127.0.0.1:8020/posts.
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Ollama + Buffer")
POSTS: list[dict[str, Any]] = []
CHANNELS = [
    {"id": "mock-instagram", "service": "instagram", "name": "acme.studio", "avatar": ""},
    {"id": "mock-linkedin", "service": "linkedin", "name": "Acme Studio", "avatar": ""},
    {"id": "mock-x", "service": "twitter", "name": "acme", "avatar": ""},
]


@app.post("/v1/chat/completions")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    prompt = body["messages"][-1]["content"]
    if isinstance(prompt, list):  # an image to describe (OpenAI content parts)
        content = json.dumps(
            {
                "description": "A cheerful product photo in soft natural light.",
                "keywords": ["product", "lifestyle", "natural light", "minimal"],
            }
        )
    else:
        match = re.search(r"(\[.*?\])", prompt)
        keywords: list[str] = json.loads(match.group(1)) if match else ["photo"]
        subject = keywords[0] if keywords else "this moment"
        tags = [f"#{re.sub(r'[^0-9A-Za-z]', '', k).lower()}" for k in keywords][:4]
        tags += ["#instagood", "#photooftheday", "#weekendvibes"]
        content = json.dumps(
            {"caption": f"Say hello to {subject} - pure good vibes, zero filter.", "hashtags": tags}
        )
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
    )


@app.post("/graphql", response_model=None)
async def graphql(request: Request) -> dict[str, Any] | JSONResponse:
    if not request.headers.get("authorization", "").startswith("Bearer "):
        return JSONResponse({"errors": [{"message": "Unauthorized"}]}, status_code=401)
    body = await request.json()
    query: str = body["query"]
    variables: dict[str, Any] = body.get("variables") or {}
    if "createPost" in query:
        POSTS.append(variables["input"])
        post = {"id": uuid.uuid4().hex[:12]}
        return {"data": {"createPost": {"__typename": "PostActionSuccess", "post": post}}}
    if "channels(" in query:
        return {"data": {"channels": CHANNELS}}
    return {"data": {"account": {"organizations": [{"id": "mock-org"}]}}}


@app.get("/posts")
async def posts() -> list[dict[str, Any]]:
    return POSTS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
