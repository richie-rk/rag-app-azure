"""`POST /openai/v1/embeddings` (84-testing.md section 2.3).

One unit-length vector per input, of length `dimensions` (3,072 when absent), drawn from a
generator seeded with the SHA-256 of the input text, so equal texts give equal vectors.
"""

from __future__ import annotations

import base64
import hashlib
import math
import struct
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

DEFAULT_DIMENSIONS = 3072
MAX_DIMENSIONS = 3072
MAX_INPUTS = 2048
_CHARACTERS_PER_TOKEN = 4
_WORD_BYTES = 4
_WORD_RANGE = 2**32

type Inputs = StrictStr | Annotated[list[StrictStr], Field(min_length=1, max_length=MAX_INPUTS)]


class EmbeddingRequest(BaseModel):
    """The fields the stub reads; any other OpenAI parameter is accepted and ignored."""

    model_config = ConfigDict(extra="ignore", strict=True)

    model: StrictStr
    input: Inputs
    dimensions: StrictInt = Field(default=DEFAULT_DIMENSIONS, ge=1, le=MAX_DIMENSIONS)
    # The openai package asks for base64 unless the caller names a format.
    encoding_format: Literal["float", "base64"] = "float"

    def texts(self) -> list[str]:
        return [self.input] if isinstance(self.input, str) else list(self.input)


def vector(text: str, dimensions: int) -> list[float]:
    """The unit-length vector of `text`.

    SHAKE-256 keyed with the text's SHA-256 is the generator: its output is a pure function of
    the text, and it fills thousands of components at C speed on the event loop.
    """
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    stream = hashlib.shake_256(seed).digest(dimensions * _WORD_BYTES)
    words = struct.unpack(f"<{dimensions}I", stream)
    components = [word / _WORD_RANGE * 2.0 - 1.0 for word in words]
    norm = math.sqrt(math.fsum(component * component for component in components))
    return [component / norm for component in components]


def _encoded(values: list[float], encoding_format: str) -> list[float] | str:
    if encoding_format == "base64":
        return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")
    return values


def estimated_tokens(texts: list[str]) -> int:
    """The usage estimate: one token per four characters, rounded up."""
    return math.ceil(sum(len(text) for text in texts) / _CHARACTERS_PER_TOKEN)


def response_body(request: EmbeddingRequest) -> dict[str, Any]:
    texts = request.texts()
    tokens = estimated_tokens(texts)
    return {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "index": index,
                "embedding": _encoded(vector(text, request.dimensions), request.encoding_format),
            }
            for index, text in enumerate(texts)
        ],
        "model": request.model,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
