"""Standalone smoke test for POST /ask/stream -- prints each SSE event as
it arrives, with a timestamp, so streaming (not just a single buffered
response) is directly observable. Run with the server already up:
    python -m server.app --path .
then, in a second terminal:
    python test_stream_endpoint.py "your question here"
"""
import json
import sys
import time

import requests

query = sys.argv[1] if len(sys.argv) > 1 else "what is this project?"
start = time.monotonic()

resp = requests.post(
    "http://127.0.0.1:8756/ask/stream",
    json={"query": query},
    stream=True,
)

for line in resp.iter_lines(decode_unicode=True, chunk_size=1):
    if not line or not line.startswith("data: "):
        continue
    elapsed = time.monotonic() - start
    event = json.loads(line[len("data: ") :])
    if event["type"] == "log":
        print(f"[{elapsed:6.2f}s] LOG  {event['logger']}: {event['message']}")
    else:
        print(f"[{elapsed:6.2f}s] DONE")
        if "answer" in event:
            print(f"  answer: {event['answer'][:200]}...")
        else:
            print(f"  error: {event['error']}")
