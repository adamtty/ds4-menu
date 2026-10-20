"""Coverage-guided fuzzing, with Atheris (Linux x86-64; CI runs it), of what the proxy does with raw
bytes from a client before anything can reach the model: parse the request head, decide whether to
refuse it, and pick the model a request names. None of it may raise, and the gate may only answer
with its own status codes.

    pip install --require-hashes -r tests/requirements-atheris.txt
    python tests/fuzz_http.py tests/fuzz-corpus/http -max_total_time=60
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import atheris  # noqa: E402

with atheris.instrument_imports():
    import ds4proxy  # noqa: E402


def gate(api_key):
    p = ds4proxy.Proxy.__new__(ds4proxy.Proxy)
    p.settings = {"api_key": api_key, "model": "DeepSeek-V4-Flash-Q2.gguf"}
    p.models = lambda: {"DeepSeek-V4-Flash-Q2.gguf": 1, "Qwen3.8-Flash-Next-Q4.gguf": 1}
    return p


OPEN, KEYED = gate(None), gate("s3cret")


def TestOneInput(data):
    method, path, hdrs = ds4proxy.parse_head(data + b"\r\n\r\n")
    for p in (OPEN, KEYED):
        refused = p.refuse(method, path, hdrs)
        assert refused is None or refused[0] in (401, 403, 404, 421), refused
        assert p.model_for(data) in (None, "Qwen3.8-Flash-Next-Q4.gguf")


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
