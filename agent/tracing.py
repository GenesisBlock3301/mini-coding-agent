"""JSONL event traces: requests, tool calls, results, usage, stop reasons. (Session 10)

Secrets never enter a trace record. The API key lives in a request header, so
no request object is ever logged wholesale.
"""
