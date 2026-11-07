import math

import pytest


def test_chunking_preserves_unicode_offsets_and_capacity():
    from browserbuddy.embeddings import chunk_page

    class Tokenizer:
        def __call__(self, text, **kwargs):
            return {
                "input_ids": list(range(len(text))),
                "offset_mapping": [(i, i + 1) for i in range(len(text))],
            }

        def num_special_tokens_to_add(self):
            return 2

    text = "αβγδε" * 100
    chunks = chunk_page(text, title="T", tokenizer=Tokenizer(), max_length=130)
    assert chunks[0].start == 0
    assert chunks[0].end == 127
    assert chunks[1].start == 63
    assert chunks[-1].end == len(text)
    for chunk in chunks:
        assert chunk.text == text[chunk.start : chunk.end]


@pytest.mark.parametrize("vector", [[0, 0], [math.nan, 1], [math.inf, 1], [1]])
def test_invalid_vectors_rejected(vector):
    from browserbuddy.embeddings import validate_vector

    with pytest.raises(ValueError):
        validate_vector(vector, dimensions=2)


def test_vector_is_normalized():
    from browserbuddy.embeddings import validate_vector

    assert validate_vector([3, 4], dimensions=2) == pytest.approx([0.6, 0.8])
