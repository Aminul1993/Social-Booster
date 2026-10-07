"""Framework-agnostic service layer.

Everything in this package is independent of FastAPI: services receive plain
configuration dataclasses and (where needed) a shared ``httpx.AsyncClient`` so
they can be unit-tested in isolation and reused from CLIs or workers.

Modules
-------
vision      Image description with an online vision model (Ollama Cloud).
ollama      Ollama Cloud chat client: retries, parsing, error recovery.
buffer      Buffer OAuth 2 + profile listing + post scheduling.
publishing  Provider-neutral contracts so other networks can be added later.
storage     Upload storage abstraction (local disk implementation).
images      Upload validation and sanitising (magic bytes, Pillow, EXIF strip).
content     Caption / hashtag / keyword normalisation helpers.
prompts     Prompt templates for image description and copy generation.
retry       Exponential back-off retry policy shared by HTTP clients.
errors      Exception hierarchy shared by all adapters.
"""
