"""The chat pipeline: `createChatTurn` and the turn it streams (`12-chat-pipeline.md`; `11-api.md`
section 3.3).

- `route`: the endpoint and the checks before the stream starts (P1 to P9);
- `turn`: the turn task (T1 to T14), the shielded finaliser and the replay;
- `retrieval`, `sources`, `screening`, `assembly`, `prompts`, `markers`: retrieval and the
  served-chunk-set filter, the sources, Prompt Shields (the only caller of the service:
  `shield_user_prompt`, `ScreeningUnavailable`), prompt assembly with `render_memory_block`, the
  constant texts, and the citation markers and allowed links;
- `calls`, `small`, `generation`, `followups`: the model calls and their usage ledger;
- `stream`: the NDJSON events, the order state machine and the writer;
- `tasks`: the task tracker, `register_background_task` and `begin_drain`;
- `service`: the worker's chat service and the composition root's `ChatPorts`;
- `citation_routes`, `citations`: the citation opening operations (section 16).
"""
