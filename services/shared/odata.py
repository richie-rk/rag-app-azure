"""Escaping for values interpolated into OData $filter expressions.

Azure Table Storage and Azure AI Search both take OData filters as strings,
so any user-controlled value spliced into one must have its string delimiter
escaped or it can break out and rewrite the query (e.g. `' or PartitionKey ne '`).
"""


def odata_escape(value: str) -> str:
    """Escape a value for use inside a single-quoted OData string literal.

    Per the OData spec a literal single quote is doubled.
    """
    return value.replace("'", "''")
