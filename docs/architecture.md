# Architecture

RAG App Azure is made of four services plus a shared library. The three Python services (chat, ingestion, and utils) all import from `services/shared`, which is why they are run from the repository root.

## Repository layout

```
rag-app-azure/
├── services/
│   ├── shared/      # Config, DB models, JWT auth, Azure client factories
│   ├── chat/        # FastAPI RAG chat service
│   ├── ingestion/   # Durable Functions document pipeline
│   └── utils/       # Azure Functions REST API
├── ui/              # React 18 + Fluent UI 9 frontend
├── infra/           # Bicep templates and PowerShell provisioning scripts
└── docs/            # Architecture notes, ADRs, and screenshots
```

## Components

### Chat service

`services/chat/` is a FastAPI app, on port 8000 locally. For each request it validates the JWT, runs a hybrid search (vector, BM25, and a semantic reranker in one Azure AI Search query) against the project's index, builds the prompt from the retrieved sources, and streams the answer back as NDJSON. Follow-up questions are extracted from markers in the model output. In Azure it runs under Gunicorn with Uvicorn workers (`services/chat/startup.sh`).

### Ingestion service

`services/ingestion/` is a containerized Azure Durable Functions app. A `POST /api/ingest` request starts an orchestration that fans out one activity per blob. Each file is downloaded, hashed with SHA-256, and skipped if an identical version was already ingested. Otherwise it is parsed, chunked, embedded in batches, and uploaded to the search index, with one audit row written per document. Activities retry with exponential backoff.

Parsing uses local per-extension parsers for PDF, DOCX, and PPTX, plus a text parser for `.txt`, `.md`, `.py`, `.js`, `.ts`, `.html`, `.json`, and `.csv`. Unknown extensions fall back to the text parser. Page-wise is the only chunking strategy implemented; see the [Roadmap](../README.md#roadmap) for what is planned.

### Utils service

`services/utils/` is an Azure Functions v2 app, on port 7071 locally. It provides the REST API behind the UI: projects, users, sessions, feedback, documents, ingestion audit, the prompt library, and magic-link authentication.

### UI

`ui/` is a React 18 app built with Fluent UI 9, TypeScript, and Vite. It supports MSAL SSO and magic-link guest sign-in. The dev server proxies `/api` to the utils service and `/chat` to the chat service.

### Shared library

`services/shared/` holds the SQLAlchemy models, Azure client factories, JWT and magic-link auth helpers, and the settings loader. It is imported by the chat, ingestion, and utils services.

## API reference

Utils routes are served under the `/api` prefix.

### Chat service

| Route | Method | Purpose |
|-------|--------|---------|
| `/chat` | POST | Run retrieval and stream an NDJSON answer |
| `/health` | GET | Health check |

### Ingestion service

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/ingest` | POST | Start a document ingestion run |

### Utils service

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/projects` | GET | List projects |
| `/api/projects` | POST | Create a project |
| `/api/projects/{project_id}` | PUT | Update a project |
| `/api/users` | GET | List users |
| `/api/users/provision` | POST | Create or update the signed-in user |
| `/api/sessions` | GET | List sessions, or load one session's messages |
| `/api/sessions` | POST | Save a chat session |
| `/api/sessions/{session_id}` | DELETE | Delete a session |
| `/api/feedback` | POST | Save feedback on an answer |
| `/api/documents` | GET | List documents |
| `/api/audit/{project_id}` | GET | Get ingestion audit rows for a project |
| `/api/prompts` | POST | Save a prompt to the prompt library |
| `/api/auth/magic-link` | POST | Send a magic-link email |
| `/api/auth/verify` | GET | Verify a magic-link token and issue a JWT |
