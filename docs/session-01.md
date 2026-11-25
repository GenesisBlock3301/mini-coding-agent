# Session 1 — One Request to the Model

**Goal (from the plan):** send one DeepSeek request. Display the response,
duration, and available token usage.
**Done when:** a CLI prompt produces a response; API failures are understandable.

This session has no tools, no loop and no files. It is the smallest thing that
talks to a model, and every later session is built on what it returns.

## What a request is

A chat model is a function: a list of messages in, one message out.

```text
messages = [{"role": "user", "content": "Say hello"}]
-> POST {base_url}/chat/completions
<- {"choices": [{"message": {...}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": ..., "completion_tokens": ...}}
```

Three facts to carry forward:

- **The model keeps no state.** Every request resends the whole conversation.
  The "memory" of a chat is the list your code keeps and resends.
- **Tokens are the unit of cost and of context.** `usage` reports prompt and
  completion tokens. Cost is tokens times a configured price, and the context
  limit is a token count too.
- **`finish_reason` says why the model stopped.** `stop` is a finished answer.
  `length` is a cut-off. Later, `tool_calls` means the model wants an action.

## Where it lives

- `agent/model.py` is the only module that talks to the provider.
  `ModelConfig` holds base URL, model, timeouts and pricing. `call_model` builds
  the payload, posts it and returns a `ModelResponse` (text, `finish_reason`,
  prompt/completion/total tokens, `duration_ms`, `tool_calls`).
- Credentials come from `~/.config/mini-coding-agent/.env`, never from the
  workspace. `PROVIDER` selects DeepSeek (default) or a local Ollama server.
- `agent/cli.py` is the entry point that takes the prompt and prints the result.

## Failures must be understandable

No `httpx` exception escapes `model.py`. Every failure leaves it as a
`ModelError` with a `kind` (for example a bad key, a rate limit, a timeout or a
bad request). The CLI prints one actionable line and maps the `kind` to an
exit code. A traceback is never the user interface.

## Try it

1. Run a plain prompt and read the text, duration and token usage.
2. Break the API key on purpose. Expect one readable error line, not a traceback.
3. Set a tiny read timeout. Expect a timeout error.
4. Send the same prompt twice. Compare the token counts.

## Self-test before Session 2

- Why must a chat resend the whole history on each request?
- What does `finish_reason` tell you that the text does not?
- Why is credentials-outside-the-workspace a safety rule and not a convenience?

Next: [Session 2](session-02.md), where the first tool and the first boundary
appear.
