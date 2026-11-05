# Mini Coding Agent: Applied AI and AI Engineering Practice Plan

## Goal

Build a small coding agent from scratch using Python and the DeepSeek API. Understand how an agent chooses actions, where it fails, and whether a change actually improves its performance.

This is a learning project. Prioritize understanding over adding features.

**Time:** 14 sessions of approximately 1.5–2 hours each. Continue unfinished milestones in the next session rather than rushing.

**Stack:** Python, DeepSeek API, pytest; FastAPI and Docker in the final stage. Start without an agent framework so the loop and message handling remain visible.

## 1. Define the Scope

The agent will operate inside one dedicated workspace and can:

- List files and directories.
- Read text files.
- Replace an exact piece of text.
- Explain its changes.
- Stop at configured step or spending limits.

Do not add terminal execution, browsing, package installation, or multiple agents in the first version. These features expand the debugging and security requirements substantially.

**Example task:**

> Read calculator.py, fix the subtraction bug, and explain the change.

The agent should locate the file, read it, edit it, and report the result. Later, an external evaluator will verify the result using checks or tests.

## 2. Project Structure

```text
mini-coding-agent/
├── agent/
│   ├── cli.py           # Accept the user's task
│   ├── model.py         # DeepSeek API adapter
│   ├── loop.py          # Agent execution loop
│   ├── tools.py         # Tool schemas and implementations
│   ├── policy.py        # Workspace boundaries and operation limits
│   └── tracing.py       # Events, usage, and duration
├── evals/
│   ├── cases/           # Task fixtures and expected outcomes
│   ├── run.py           # Evaluation runner
│   └── results/         # Evaluation reports
├── playground/          # The agent's permitted workspace
├── tests/
├── .env.example
└── README.md
```

Keep the API key in an environment variable. Keep secrets, real projects, and the actual `.env` file outside the agent's permitted workspace. The example environment file must contain placeholders only.

## 3. Implementation Sessions

| Session | Work                                                                                  | Completion criteria                                                                 |
|---------|---------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------|
| 1       | Send one DeepSeek request. Display the response, duration, and available token usage. | A CLI prompt produces a response; API failures are understandable.                  |
| 2       | Implement `list_files` and call it directly without the model.                        | Valid directories work; paths outside the workspace are rejected.                   |
| 3       | Add `read_file` with file-size and output limits.                                     | Missing, oversized, and invalid files produce controlled results.                   |
| 4       | Implement model tool call → execution → tool result → model.                          | The agent independently reads a file to answer a task.                              |
| 5       | Handle multiple tool calls per response and stopping rules.                           | Every result matches its tool-call ID; the agent stops at its step limit.           |
| 6       | Add `replace_text`; require exactly one match.                                        | Zero or multiple matches return an error without changing the file.                 |
| 7       | Run the read → edit workflow against a small fixture project.                         | A simple bug is fixed and the diff is easy to inspect.                              |
| 8       | Deliberately introduce failures.                                                      | Timeouts, malformed arguments, and unknown tools do not crash the process.          |
| 9       | Handle repeated actions and context growth.                                           | Repeated failures trigger a stop; large outputs are bounded.                        |
| 10      | Add JSONL event traces.                                                               | You can inspect requests, tool calls, results, usage, and stop reasons after a run. |
| 11      | Create 10 evaluation tasks with fresh fixtures.                                       | A script verifies the expected outcomes.                                            |
| 12      | Expand to 20 tasks and compare one improvement against a baseline.                    | Produce a report covering success, latency, usage, and unexpected changes.          |
| 13      | Expose the loop through FastAPI with a separate workspace per run.                    | Concurrent runs do not share files or conversation history.                         |
| 14      | Add Docker, documentation, and a demo.                                                | The documented setup works in a fresh environment.                                  |

### Limits to Implement

- Start with a maximum of five model steps per run.
- Also limit tool calls, output size, request duration, and total run duration.
- Record token usage and estimate spending from explicitly configured pricing.
- Check the remaining budget before requesting another response. Leave headroom: the next response may consume tokens before its actual usage is known.
- Stop explicitly when a limit is reached, and report changes already made.

## 4. Understand the Agent Loop

```python
def run_agent(system_message, user_task, max_steps=5):
    messages = [system_message, user_task]

    for step in range(max_steps):
        response = call_model(messages)
        messages.append(response)

        if not response.tool_calls:
            return final_result(response)

        for tool_call in response.tool_calls:
            result = validate_and_execute(tool_call)
            messages.append(tool_result(tool_call.id, result))

    return stopped_result("step_limit")
```

This is conceptual pseudocode. Implement the provider's actual message serialization in the API adapter. Preserve assistant tool-call messages and associate each tool result with the correct call ID. Initially execute tools sequentially so edits have a predictable order.

Keep three responsibilities separate:

| Component | Responsibility |
| --- | --- |
| Model | Proposes the next action. |
| Policy and tool executor | Validate arguments and enforce allowed actions. |
| Evaluator | Independently checks whether the task was solved. |

A final model response means the model stopped requesting tools. It does not prove that the task succeeded. Traces record observable actions and outputs, not access to the model's hidden internal reasoning.

## 5. Failure Practice

| Failure | Expected behavior |
| --- | --- |
| Missing file | Return a structured error so the agent can try another path. |
| Invalid tool arguments | Return a validation error as the tool result. |
| Access outside the workspace | Reject the request, including symlink escapes. |
| API timeout or rate limit | Use bounded retries; return an explicit failure if exhausted. |
| Repeated identical tool call | Stop at the configured repetition threshold. |
| Multiple replacement matches | Do not edit; let the agent request more context. |
| File contains “ignore instructions” | Treat file content as untrusted data; enforce policy independently of the prompt. |
| Step limit reached | Report that the task is incomplete and list edits already performed. |

### Retry and Edit Safety

An API retry and a file-edit retry have different consequences. An edit may have succeeded even if its result was lost. Before applying an edit, verify expected content or a file hash. Reject stale edits rather than applying them blindly.

Resolve paths against the workspace boundary and test symlink escapes. For the learning version, reject symlinks and use disposable fixtures. Path checks alone are not a complete sandbox for hostile concurrent filesystem changes; Docker alone is not a complete security boundary either.

### Context Growth

First measure conversation size and limit tool outputs. Stop cleanly before exceeding the available context. Treat summarization or compaction as a later experiment: summaries can lose file details, instructions, and tool-call relationships.

## 6. Build a 20-Task Evaluation Set

| Category | Count | Example |
| --- | --- | --- |
| Read/search | 4 | Find the function that calculates a discount. |
| Simple edits | 6 | Fix arithmetic, a default value, or a typo. |
| Multi-file tasks | 4 | Rename a function and update its imports. |
| Failure/boundary | 4 | Missing file, ambiguous replacement, outside path, repeated failure. |
| Already correct or clarification needed | 2 | Avoid an unnecessary edit or request clarification for an ambiguous task. |
| **Total** | **20** | |

Each case should contain:

```text
Task instruction
Initial workspace fixture
Expected final state or behavior
Allowed files to change
Verification function
```

Restore a fresh fixture before every run. Keep evaluator checks outside the agent's editable workspace. Check both intended results and unexpected file changes. For clarification cases, record an explicit `needs_clarification` outcome rather than pretending that an edit completed the task.

### Metrics

- Task success rate based on independent checks.
- Unexpected file changes.
- Tool calls and model requests.
- Total duration.
- Token usage and estimated cost when pricing is configured.
- Stop reason: model finished, failed, step limit, budget limit, policy blocked, or clarification needed.

Run each case once to debug the runner. Then repeat cases three times for a more useful comparison. Small samples remain noisy.

Record the model identifier, prompt version, configuration, and code version. Change one variable at a time when comparing improvements. Reserve some cases from prompt tuning so improvements are not merely tailored to familiar tasks.

## 7. Daily Practice Routine

1. **15 minutes:** Read the relevant concept in the Hugging Face Agents Course.
2. **60 minutes:** Implement the current milestone yourself.
3. **20 minutes:** Deliberately trigger at least one failure.
4. **10 minutes:** Record what happened and why.

Course: https://huggingface.co/learn/agents-course/unit0/introduction

### Prompt for Your Editor's AI Assistant

> I am learning to build a coding agent. Help me with the current milestone only. Explain the design and failure cases first. Let me implement it, then review my code. Do not generate the entire project.

Generating everything at once can produce a working demo while leaving gaps in your understanding of the loop, conversation history, and tool-result handling.

### Session Notes Template

```markdown
## Session N
- Goal:
- What I implemented:
- Failure I triggered:
- Expected versus observed behavior:
- What I learned:
- Remaining questions:
- Next step:
```

## 8. First Milestone

Accept a CLI task → let the model choose `list_files` and `read_file` → return an answer grounded in the file → stop within five model steps.

Before adding editing, verify that you can explain:

- Why the tool result must be sent back to the model.
- Why tool-call IDs must match their results.
- How conversation history grows on each iteration.
- Which rules are enforced by code rather than by the prompt.
- Why an agent saying “done” is different from verified success.

## 9. Final Deliverables

- A working CLI agent with bounded execution and restricted file tools.
- A 20-task evaluation suite and baseline comparison report.
- Observable run traces with secrets excluded.
- A FastAPI interface with isolated run state.
- A Docker setup and reproducible README.
- A short demo showing a successful task, a controlled failure, and the evaluation results.

This project develops agentic application engineering skills. Broader AI engineering topics such as model training, fine-tuning, data pipelines, and production model serving require additional projects after this foundation.
