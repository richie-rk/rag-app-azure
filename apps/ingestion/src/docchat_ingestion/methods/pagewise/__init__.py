"""The page-wise method, `pagewise` version 1 (`14-ingestion.md` section 10; ADR 0047): one chunk
per page of the version's PDF, parsed by `unstructured` inside the parsing child, each table as
its summary followed by its sanitised HTML.

`IMPLEMENTATION` is what `docchat_ingestion.function_app` registers. It is loaded on first access,
because the parsing child imports `docchat_ingestion.methods.pagewise.partition`, and with it this
package, and must not load the activity-side modules and their clients.
"""

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "IMPLEMENTATION":
        from docchat_ingestion.methods.pagewise.method import IMPLEMENTATION  # noqa: PLC0415

        return IMPLEMENTATION
    raise AttributeError(name)
