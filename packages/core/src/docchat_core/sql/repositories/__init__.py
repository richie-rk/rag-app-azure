"""Repository functions: the statements each area runs through the data-access layer
(`01-repository.md` section 6.2 rule 10; `80-data-model.md` section 2.1 item 8).

Every function on a request path carries `@request_path(example_params=…)`, takes the
`SqlEngine` as its first argument and its parameters as keywords, so the query-plan audit
(`docchat_testing.data_audit`) can import every module here and run each function once.
"""
