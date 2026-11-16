"""The ingestion app's orchestrators (`14-ingestion.md` sections 5, 6.1 and 8.12): the singleton
`ingestion_dispatcher`, the two item sub-orchestrations and `call_with_retry`, each module with its
own Durable blueprint `bp` that `docchat_ingestion.function_app` registers."""
