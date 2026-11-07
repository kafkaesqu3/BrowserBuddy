"""Optional CPU embeddings with explicit setup and exact source offsets."""

import hashlib
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from browserbuddy.config import Settings
from browserbuddy.memory_models import SetupResult

INFERENCE = ThreadPoolExecutor(max_workers=1, thread_name_prefix="buddy-inference")
PREPARE_LOCK = threading.Lock()


@dataclass
class Chunk:
    text: str
    ordinal: int
    kind: str
    start: int | None = None
    end: int | None = None


def validate_vector(vector: list[float], *, dimensions: int) -> list[float]:
    if len(vector) != dimensions or not all(math.isfinite(x) for x in vector):
        raise ValueError("invalid vector dimensions or nonfinite values")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0 or not math.isfinite(norm):
        raise ValueError("invalid vector norm")
    return [x / norm for x in vector]


def chunk_page(text: str, *, title: str, tokenizer, max_length: int) -> list[Chunk]:
    """Split stored text without silently truncating the encoder input.

    Args:
        text: Original stored text.
        title: Prefix used during embedding.
        tokenizer: Encoder tokenizer with character offset mappings.
        max_length: Model's actual input capacity, including special tokens.
    """
    title_tokens = tokenizer(title, add_special_tokens=False)["input_ids"][:32]
    capacity = min(512, max_length - len(title_tokens) - tokenizer.num_special_tokens_to_add())
    if capacity <= 64:
        raise ValueError("model capacity must leave more than 64 body tokens")
    offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)[
        "offset_mapping"
    ]
    chunks = []
    for start in range(0, len(offsets), capacity - 64):
        selected = offsets[start : start + capacity]
        begin, end = selected[0][0], selected[-1][1]
        chunks.append(Chunk(text[begin:end], len(chunks), "body", begin, end))
        if start + capacity >= len(offsets):
            break
    return chunks


def prepared_config(settings: Settings) -> dict | None:
    path = settings.data_dir / "embedding.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if (value["model"], value["requested_revision"]) != (
        settings.embedding_model,
        settings.embedding_revision,
    ):
        return None
    return value


def config_id(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


class LocalEmbedder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None
        self.config = None

    def prepare(self) -> SetupResult:
        """Download the explicitly selected model and record immutable identity."""
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer

        with PREPARE_LOCK:
            path = snapshot_download(
                self.settings.embedding_model,
                revision=self.settings.embedding_revision,
                cache_dir=str(self.settings.data_dir / "models"),
            )
            model = SentenceTransformer(
                path, device="cpu", trust_remote_code=False, local_files_only=True
            )
            data = {
                "model": self.settings.embedding_model,
                "requested_revision": self.settings.embedding_revision,
                "revision": path.rsplit("/", 1)[-1],
                "path": path,
                "dimensions": model.get_sentence_embedding_dimension(),
                "max_length": model.max_seq_length,
                "target": 512,
                "overlap": 64,
                "prefix": 32,
                "normalized": True,
                "format": 1,
            }
            dest = self.settings.data_dir / "embedding.json"
            temp = dest.with_suffix(".tmp")
            temp.write_text(json.dumps(data, sort_keys=True))
            temp.replace(dest)
            self.config, self.model = data, model
            return SetupResult(config_id=config_id(data), ready=True)

    def load(self) -> None:
        from browserbuddy.worker import CapabilityUnavailable

        config = prepared_config(self.settings)
        if config is None:
            raise CapabilityUnavailable("Run buddy index prepare first")
        if self.model is not None and self.config == config:
            return
        try:
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(
                config["path"], device="cpu", trust_remote_code=False, local_files_only=True
            )
            self.config = config
        except (ImportError, OSError) as exc:
            raise CapabilityUnavailable("Local embedding model unavailable") from exc

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.load()
        vectors = self.model.encode(
            texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False
        )
        return [validate_vector(v.tolist(), dimensions=self.config["dimensions"]) for v in vectors]

    def query(self, text: str) -> list[float]:
        future = INFERENCE.submit(self.encode, [text])
        try:
            return future.result(timeout=5)[0]
        finally:
            future.cancel()

    def document(self, *, title: str, url: str, text: str) -> tuple[list[Chunk], list]:
        self.load()
        tokenizer = self.model.tokenizer
        prefix_ids = tokenizer(title, add_special_tokens=False)["input_ids"][:32]
        prefix = tokenizer.decode(prefix_ids)
        chunks = chunk_page(
            text, title=prefix, tokenizer=tokenizer, max_length=self.model.max_seq_length
        )
        metadata_ids = tokenizer(f"{title}\n{url}", add_special_tokens=False)["input_ids"]
        metadata_capacity = self.model.max_seq_length - tokenizer.num_special_tokens_to_add()
        metadata = tokenizer.decode(metadata_ids[:metadata_capacity])
        chunks.append(Chunk(metadata, len(chunks), "metadata"))
        inputs = [prefix + chunk.text if chunk.kind == "body" else chunk.text for chunk in chunks]
        return chunks, self.encode(inputs)
