"""The API's operations outside the chat pipeline and the conversation store, one module per area
(`11-api.md` section 3; `01-repository.md` section 1.2). Each `/v1` area module defines one
`APIRouter(route_class=DocChatRoute)` named `router`; `docchat_api.app` mounts it on the `/v1`
router."""
