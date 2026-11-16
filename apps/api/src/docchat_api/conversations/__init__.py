"""The conversation store and the routes over it (`13-conversations-and-memory.md` sections 1 to
5, 7 and 8).

`store.ConversationStore` is the one object through which the API reads and writes the
`conversations` and `memories` containers; the chat pipeline calls only its turn-path methods
(`begin_turn`, `read_recent_turns`, `get_prompt_memory`, `finalize_turn`, `attach_followups` and
`get_turn`), and the routes its reads and conditioned writes. The routes: `session_routes`
(`listSessions`, `getSession`, `listSessionTurns`), `feedback_routes` (`putTurnFeedback`) and
`memory_routes` (`getMemory` and the four memory writes). The rolling summary and memory
extraction join this package with the after-response work.
"""
