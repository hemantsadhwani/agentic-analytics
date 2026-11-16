"""Embedders: text → a unit-length vector. One interface, an offline fake and Bedrock.

    e = get_embedder("titan")              # amazon.titan-embed-text-v2:0, in Mumbai
    vecs = e.embed(["Product model Road-150 ..."], kind="document")
    q = e.embed(["lightest road frame"], kind="query")[0]

* ``fake``: a deterministic hashing embedder (word stems and character trigrams hashed into
  256 buckets). No network, no cost. It is good enough to test the plumbing and the fusion;
  it is not a meaning model, so real retrieval quality is measured with Titan or Cohere.
* ``titan``: Amazon Titan Text Embeddings V2, in-region in ap-south-1, $0.024 per million
  input tokens (AWS Price List API, 2026-11-16). 512 dimensions, normalised by the service.
* ``cohere``: Cohere Embed English v3, in-region, $0.10 per million input tokens. It
  distinguishes documents from queries (``input_type``). The corpus and the golden questions
  are English, so the English model is the fair comparison. ``cohere-multilingual`` (same
  price) is for when users ask in Hindi or Hinglish; adopt it only with a golden set in
  those languages to prove it.

Every vector stored records the embedder's ``model_id``: vectors from different models cannot
be compared, so a change of model means a rebuild (the index refuses to mix them).
Unknown price → refused before any call (``pricing.require_price``), like the chat models.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import Any, Literal, Protocol

from agentic_reporting.providers import pricing
from agentic_reporting.providers.base import ProviderUnavailable
from agentic_reporting.schema_graph import stem

Kind = Literal["document", "query"]
FAKE_DIM = 256
_WORD_RE = re.compile(r"[a-z0-9]+")


def normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def cosine(a: list[float], b: list[float]) -> float:
    """Dot product of two unit vectors."""
    return sum(x * y for x, y in zip(a, b, strict=True))


class Embedder(Protocol):
    model_id: str
    dim: int
    tokens: int
    cost_usd: float

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]: ...


class FakeEmbedder:
    """Deterministic, offline: hashed word stems (weight 1) and character trigrams (0.5)."""

    model_id = "fake-hash-256"
    dim = FAKE_DIM

    def __init__(self) -> None:
        self.tokens, self.cost_usd = 0, 0.0

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        words = [stem(w) for w in _WORD_RE.findall(text.lower())]
        feats = [(w, 1.0) for w in words]
        feats += [(w[i:i + 3], 0.5) for w in words if len(w) > 3 for i in range(len(w) - 2)]
        for f, wt in feats:
            h = int(hashlib.md5(f.encode()).hexdigest(), 16)
            v[h % self.dim] += wt if (h >> 8) & 1 else -wt
        self.tokens += len(words)
        return normalise(v)

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        return [self._one(t) for t in texts]


class BedrockEmbedder:
    """Titan Text Embeddings V2 or Cohere Embed v3 through ``bedrock-runtime.invoke_model``."""

    def __init__(self, model_id: str, region: str | None = None, client: Any = None,
                 dim: int = 512) -> None:
        pricing.require_price(model_id)
        self.model_id, self.dim = model_id, dim
        self.region = region or os.environ.get("AWS_REGION") or "ap-south-1"
        self._client = client
        self.tokens, self.cost_usd = 0, 0.0

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as e:   # pragma: no cover
                raise ProviderUnavailable("boto3 is not installed: pip install -e '.[llm]'") from e
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def _invoke(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.invoke_model(modelId=self.model_id, body=json.dumps(body),
                                        contentType="application/json", accept="application/json")
        return json.loads(resp["body"].read())

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        out: list[list[float]] = []
        if self.model_id.startswith("amazon.titan-embed-text"):
            for t in texts:                      # Titan V2 takes one text per call
                r = self._invoke({"inputText": t, "dimensions": self.dim, "normalize": True})
                out.append(normalise(r["embedding"]))
                self._charge(int(r.get("inputTextTokenCount", 0)))
        elif self.model_id.startswith("cohere.embed"):
            for i in range(0, len(texts), 96):  # Cohere v3: up to 96 texts per call
                batch = texts[i:i + 96]
                r = self._invoke({"texts": batch, "truncate": "END",
                                  "input_type": "search_document" if kind == "document"
                                  else "search_query"})
                out += [normalise(v) for v in r["embeddings"]]
                # Cohere v3 returns no token count; estimate 1 token per 4 characters.
                self._charge(sum(len(t) for t in batch) // 4)
            self.dim = len(out[0]) if out else self.dim
        else:
            raise ProviderUnavailable(f"no embedding adapter for {self.model_id!r}")
        return out

    def _charge(self, n: int) -> None:
        self.tokens += n
        self.cost_usd += pricing.estimate_cost(self.model_id, n, 0)


EMBEDDERS = {"fake": None, "titan": "amazon.titan-embed-text-v2:0",
             "cohere": "cohere.embed-english-v3",
             "cohere-multilingual": "cohere.embed-multilingual-v3"}


def get_embedder(name: str | None = None) -> Embedder:
    name = name or os.environ.get("EMBEDDER") or "fake"
    if name == "fake":
        return FakeEmbedder()
    if name in EMBEDDERS:
        return BedrockEmbedder(EMBEDDERS[name])
    return BedrockEmbedder(name)             # a full model id
