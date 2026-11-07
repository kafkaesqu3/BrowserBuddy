"""Measure exact-vector retrieval mechanics without downloading an embedding model."""

import json
import platform
import struct
import tempfile
import time
from pathlib import Path

from browserbuddy import db
from browserbuddy.config import Settings
from browserbuddy.memory import content_version
from browserbuddy.memory_models import SearchRequest
from browserbuddy.search import HybridSearch
from browserbuddy.worker import embedding_identity


class QueryEncoder:
    def query(self, text):
        return [1.0] + [0.0] * 767


def main():
    with tempfile.TemporaryDirectory(prefix="buddy-benchmark-") as directory:
        settings = Settings(data_dir=Path(directory), semantic_enabled=True)
        conn = db.connect(settings.db_path)
        config = embedding_identity(settings)
        with conn:
            conn.execute(
                "INSERT INTO sessions(id,client_id,name,started_at) VALUES(1,'b','bench',0)"
            )
            for page in range(1, 10001):
                url, text = f"https://example.test/{page}", "Stored research document"
                version = content_version("Research", url, text)
                conn.execute(
                    "INSERT INTO page_visits(id,session_id,url,title,visited_at,text)"
                    " VALUES(?,1,?,'Research',?,?)",
                    (page, url, page, text),
                )
                vector = [0.0] * 768
                vector[page % 768] = 1.0
                packed = struct.pack("<768f", *vector)
                conn.executemany(
                    "INSERT INTO memory_chunks(source_kind,source_id,source_version,config_id,"
                    "ordinal,kind,text,start,end,vector) VALUES('page',?,?,?,?,'body',?,0,?,?)",
                    [
                        (page, version, config, chunk, text, len(text), packed)
                        for chunk in range(10)
                    ],
                )
                conn.execute(
                    "INSERT INTO indexed_documents(source_kind,source_id,source_version,"
                    "config_id,chunk_count) VALUES('page',?,?,?,10)",
                    (page, version, config),
                )
        conn.close()
        service = HybridSearch(settings, QueryEncoder())
        timings = []
        for _ in range(3):
            start = time.perf_counter()
            result = service.search(SearchRequest(query="semantic", limit=20), source_kind="page")
            timings.append(time.perf_counter() - start)
            assert result.mode == "hybrid" and len(result.hits) == 20
        output = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "documents": 10000,
            "chunks": 100000,
            "dimensions": 768,
            "retrieval_seconds": timings,
            "gate_passed": max(timings) <= 2,
            "model_inference": "excluded; deterministic vectors",
        }
        try:
            import resource

            output["peak_rss_platform_units"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            output["peak_rss_platform_units"] = None
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
