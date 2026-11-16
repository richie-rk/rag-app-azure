# ADR 0013: Committed OpenAPI contract and generated client

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 11-api.md, 10-web-client.md, 01-repository.md, 84-testing.md
- **Related ADRs:** 0011, 0012, 0023, 0055, 0083, 0085, 0124, 0129, 0133

## Context

The web client and the API deploy as separate artifacts, and a page stays open on the bundle it loaded while the API behind it is redeployed, yet a renamed field, a removed enum value or a changed stream event must never reach users as a silent failure. The API's OpenAPI document is generated in CI, the client's TypeScript types come from it, streamed events are validated at run time against the same schemas, and the authorisation matrix test enumerates every route from it. The chat stream is NDJSON with eight event types (`meta`, `status`, `delta`, `citations`, `followups`, `heartbeat`, `error`, `done`, ADR 0055), which plain OpenAPI response schemas don't describe line by line. The client must accept only the fields the API declares, read every field under the name the API sends, and receive structured values as typed JSON.

Vendor facts (checked 2026-10-14):
- FastAPI 0.143 streams JSON Lines from an `AsyncIterable` return with the content type `application/jsonl` hard-coded, and documents the body as `content['application/jsonl'].itemSchema`, an OpenAPI 3.2 keyword inside a document that reports `openapi: 3.1.0`; sending `application/x-ndjson` needs `StreamingResponse` with an explicit media type and manually documented events.
- `@hey-api/openapi-ts` 0.99.0 (still 0.x) with the plugins `@hey-api/typescript` and `{ name: 'zod', definitions: true }` generates `types.gen.ts` and `zod.gen.ts` (Zod 4); with TypeScript 6.0.3 it generated per-event schemas from `components` but typed an `itemSchema` stream response as `unknown`.
- openapi-typescript 7.13.0 generates types only, no run-time schemas, and its required peer `typescript ^5.x` failed to install beside TypeScript 6.0.3 (ERESOLVE).
- Zod 4.6.7 probes `new Function('')` unless `jitless` is set; `z.config({ jitless: true })` gave zero CSP violations under the site's policy. ajv needs `'unsafe-eval'` unless schemas are compiled at build time.
- TypeScript 7.0.2 exports only `./lib/version.cjs` at the package root, with the compiler API under `./unstable/*`; typescript-eslint 8.71.2 requires `typescript >=4.8.4 <6.1.0`. The generator and the linter load the classic compiler API, so TypeScript stays at 6.0.3.

## Decision

1. **Export.** The FastAPI app is built with `openapi_url=None`, `docs_url=None`, `redoc_url=None` and `redirect_slashes=False`: no environment serves the document or a docs page, and a trailing slash is never redirected. `python -m docchat_api.openapi --output <file>` writes the OpenAPI 3.1 document from `app.openapi()`; `create_app()` builds without settings or network (settings load in the lifespan), so the export needs no environment.
2. **What every operation carries.** Its operationId (11-api.md); one tag per area (`health`, `me`, `projects`, `chat`, `sessions`, `memory`, `admin-projects`, `admin-users`, `admin-indexes`, `admin-documents`, `admin-uploads`, `admin-runs`); the security requirement `bearerAuth` (HTTP bearer, JWT), except `getHealth` (`security: []`); every error status it can return, each referencing `components.schemas.Problem` (ADR 0012); and three extensions that the authorisation matrix test reads (ADR 0133): `x-docchat-access` (`public` for `getHealth` only, `user` for a caller holding `User` or `Admin`, `admin`); `x-docchat-scoped-params` (every parameter that identifies a resource subject to a reachability or existence check: a path or query parameter name, or `body:<JSON pointer>` for a body field: `body:/projectId` and `body:/sessionId` on `createChatTurn`, `body:/projectId` on `adminReserveUpload`, `body:/initialMemberIds` on `adminCreateProject`, `body:/versionIds` on `adminSubmitUploads`; the matrix substitutes unreachable IDs there, into every element of an array; an empty list when none); and `x-docchat-error-codes` (the problem codes the operation can return). An operation whose valid body the matrix can't generate from the schema alone carries an OpenAPI request example that the API accepts. Schema names are the Pydantic model names; JSON names are the camelCase aliases.
3. **The documented stream.** The chat route returns `StreamingResponse` with `application/x-ndjson`. `document_streams()` in `docchat_api/openapi.py` post-processes the document: every member of the `ChatStreamEvent` union goes into `components.schemas` from `pydantic.TypeAdapter(ChatStreamEvent).json_schema(mode="serialization", ref_template="#/components/schemas/{model}")`; a `ChatStreamEvent` schema with `oneOf` and `discriminator.propertyName = "type"`; and `createChatTurn`'s `200` response as `content: {"application/x-ndjson": {"schema": {"$ref": "#/components/schemas/ChatStreamEvent"}}}` with `x-docchat-stream: "ndjson-one-event-per-line"`.
4. **Deterministic, committed and checked.** The file is written with sorted keys, two-space indentation, `ensure_ascii=False` and a trailing newline, so regenerating an unchanged API produces identical bytes. `apps/api/openapi.json` is committed. CI job `contract` (10 minutes, a required check) regenerates it with `uv run --package docchat-api python -m docchat_api.openapi --output apps/api/openapi.json`, regenerates the web client's files with `npm run generate:api`, fails on any difference (`git diff --exit-code`), then runs `scripts/ci/check_contract_enums.py`.
5. **Client generation.** `@hey-api/openapi-ts` 0.99.0 exact, configured in `apps/web/openapi-ts.config.ts` with `input: "../api/openapi.json"`, `output: "src/api/generated"` and `plugins: ["@hey-api/typescript", { name: "zod", definitions: true }]`, writes `src/api/generated/types.gen.ts` and `src/api/generated/zod.gen.ts` (Zod 4.6.7). `apps/web/scripts/gen-limits.mjs` reads the same document and writes `src/api/generated/limits.gen.ts`: one exported constant per request field the forms use, holding its `maxLength`, `maxItems`, `minimum` or `maximum`. `npm run generate:api` runs both, then `prettier --write src/api/generated`. The generated files are committed and excluded from ESLint and coverage. The web client sets `z.config({ jitless: true })` before any schema runs, because the CSP forbids `new Function` (ADR 0083).
6. **No generated client runtime.** Calls go through the hand-written table `src/api/operations.ts`, one entry per operationId the client uses (method, path template, request type, response schema); a unit test reads `apps/api/openapi.json` and fails when an entry's method or path differs from the document's. `src/api/http.ts` parses every `2xx` JSON body with the operation's generated Zod schema, which strips unknown members; the NDJSON reader validates each line against the generated per-event schema and skips an unknown event type (ADR 0085).
7. **Response enums evolve client first.** A new value in a response enum ships in this order: the API model adds it and the committed document carries it; the web release that understands it deploys; only then may the API start returning it. Until then the server code path that would return it stays unreachable through its owning area's configuration. `scripts/ci/check_contract_enums.py` fails a pull request that adds a response-enum value and, in the same change, a code path returning it, unless the pull request carries the label `enum-already-released`.

## Alternatives considered

- **FastAPI's JSON Lines streaming.** Lost: it sends `application/jsonl`, not the stream's `application/x-ndjson`, and documents events through the OpenAPI 3.2 `itemSchema`, which both generators type as `unknown`, so the reader would get no per-event schemas.
- **Serving `/openapi.json` and a docs page from the API.** Lost: it adds routes outside the one public route (ADR 0023), or puts the document behind a token that CI doesn't have; exporting from `create_app()` needs neither.
- **Generating the document in CI without committing it.** Lost: a reviewer wouldn't see the contract change in the pull request, and the web build would depend on an API build artifact.
- **openapi-typescript for the types.** Lost: types only, no run-time schemas for the stream reader, and its peer dependency refuses TypeScript 6.0.3.
- **ajv validation against the document's JSON Schemas.** Lost: run-time compilation needs `'unsafe-eval'`, which the CSP forbids; Zod in jitless mode needs nothing extra.
- **The generator's own client runtime.** Lost: the generator is still 0.x; depending only on its type and schema output limits what an upgrade can break, and the operation table is small and checked against the document.
- **Server-first enum changes.** Lost: a page still open with the previous bundle would fail schema validation on a value it doesn't know and show an error for a valid response.

## Consequences

- Every API change is visible in a pull request as a diff of `openapi.json` and the generated files, and any drift fails CI. A misnamed or missing field fails the TypeScript build in strict mode; unknown response members never reach screen code; structured values have typed schemas end to end.
- The authorisation matrix, the route-option tests and the typed-boundary test (every input model reachable from the document has `extra="forbid"`) all read the same document (ADR 0133).
- Obligations: run `npm run generate:api` after changing any API model and commit the result; add a request example for an operation whose valid body the schema can't express; apply the label `enum-already-released` only after the web release that understands the value is deployed.
- Tests: `check_contract_enums.py` fails a change that adds a response-enum value with a code path returning it, unless the label is present; regenerating reproduces the committed bytes, and `createChatTurn`'s `200` response is `application/x-ndjson` with the eight-member `ChatStreamEvent` union in `components.schemas`; the operation-table test.
- Risk: the generator is 0.x and pinned exact; an upgrade arrives as a Dependabot pull request that must regenerate and pass `contract`, the web tests and the browser tests. Risk: `document_streams()` depends on Pydantic's JSON Schema output; the regeneration test fails if that output changes shape.

## References

- FastAPI, "Stream JSON Lines": https://fastapi.tiangolo.com/tutorial/stream-json-lines/ (checked 2026-10-14)
- Hey API, Zod plugin: https://heyapi.dev/docs/openapi/typescript/plugins/zod/v4 (checked 2026-10-14)
- openapi-typescript 7.13.0: https://www.npmjs.com/package/openapi-typescript/v/7.13.0 (checked 2026-10-14)
- Zod, `jitless` configuration: https://zod.dev/compile (checked 2026-10-14)
- ajv and Content Security Policy: https://ajv.js.org/security.html (checked 2026-10-14)
- TypeScript 7.0.2 package (no classic compiler API at the root): https://www.npmjs.com/package/typescript/v/7.0.2 (checked 2026-10-14)
- typescript-eslint 8.71.2 peer range: https://www.npmjs.com/package/typescript-eslint/v/8.71.2 (checked 2026-10-14)
