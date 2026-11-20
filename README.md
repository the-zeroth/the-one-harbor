# The One in Harbor

Run [The One](https://the-zeroth.com)'s software team on [Harbor](https://github.com/harbor-framework/harbor)
tasks, such as Terminal-Bench and SWE-Atlas-QnA, as a Harbor installed agent.

In each task container the agent installs the published Linux server, `the-one-server`, from
updates.the-zeroth.com (x86_64 or arm64, glibc or musl/Alpine images), then hands the task to The
One's software team with `the-one-server run`. The team works in the task's working directory and
decides by itself: nobody answers its questions. When the run ends, the trial has:

- `agent/trajectory.json`: the run's ATIF trajectory, with every agent's messages, tool calls and
  results, where Harbor's viewer and `harbor analyze` read it;
- `agent/the-one/`: `run.json` (outcome, usage, the files changed), `delivery.md` (the team's
  report), the progress lines and the server's logs;
- the agent result's input, cached and output tokens, and their cost at the provider's API list
  price when the server knows the model's price.

## Requirements

- Harbor 0.26.0 or 0.24.0 (Python 3.12) and Docker.
- Task containers that can reach updates.the-zeroth.com and the model provider.
- A model key where Harbor runs (see [Models](#models)).

## Run

```sh
git clone https://github.com/the-zeroth/the-one-harbor
export OPENAI_API_KEY=...
PYTHONPATH=the-one-harbor/scripts/harbor uvx --python 3.12 --from harbor==0.26.0 harbor run \
  -d <dataset> -k 3 \
  -a the_one_server_agent:TheOneServer -m openai/gpt-6.2-sol --effort max \
  --ak version=0.5.2 --ak run_timeout=8h
```

`PYTHONPATH` makes `the_one_server_agent` importable; an installed `harbor` works the same way.
Harbor 0.24 has no `--effort`: pass `--ak reasoning_effort=max` instead.

- `--ak version` pins the server release (default: the latest). Pin it for a benchmark.
- `--ak run_timeout` is how long the team may work. The server's own default is 3 hours, which
  cuts long tasks short, so set it at least as long as your longest task timeout. Harbor's
  per-task agent timeout still applies: when it fires, the agent stops the team, which saves its
  results before the verifier runs.

## Models

We tuned The One on GPT-6.2 Sol at max effort, but it runs on any model. Set the provider's key
where Harbor runs and name the model as `provider/model`:

| Provider | Key | `-m` |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | `openai/gpt-6.2-sol` |
| Anthropic | `ANTHROPIC_API_KEY` | `anthropic/claude-opus-5-5` |
| Google | `GEMINI_API_KEY` | `gemini/<model id>` |
| OpenRouter | `OPENROUTER_API_KEY` | the model's OpenRouter id, slash included |

Each key can come with a `*_BASE_URL` (for example `OPENAI_BASE_URL` for a proxy). When several keys
are set, `--ak connection=<provider>` chooses one. The reasoning effort is `--effort` with `none`,
`minimal`, `low`, `medium`, `high`, `xhigh` or `max`.

For another OpenAI-compatible server (vLLM, a gateway), write a credentials file, `chmod 600` it,
and pass `--ak credentials_file=creds.json --ak connection=custom -m <model id>`:

```json
{
  "connections": [
    {"id": "custom", "provider": "custom", "api_family": "openai-chat-completions",
     "base_url": "https://example.com/v1", "api_key": "..."}
  ]
}
```

`api_family` can also be `openai-responses` or `anthropic-messages`. Keys never go on a command line:
the agent copies them into a mode-600 credentials file inside the container.

## Options

`--ak name=value`:

| Option | What it does |
|---|---|
| `version` | The server release to install (default: the latest). |
| `run_timeout` | How long the team may work, for example `8h` (default `3h`). |
| `credentials_file` | A `the-one-server` credentials file (mode 600) with model connections. |
| `connection` | Which connection runs the model, when there are several. |
| `team` | The team (default `polished_software_dev`). |
| `assistant` | `true` gives the task to the main assistant, which chooses who works on it, as in the desktop app. |
| `on_question` | `answer` (default): the team decides by itself; `stop`: the run ends at a question. |
| `idle` | How long the team rests after delivering before the run ends (default `60s`). |
| `workdir` | The folder the team works in (default: the task's). |
| `archive` | A local `the-one-server-<version>-linux-<arch>.tar.gz` to install instead of downloading one; the `.sha256` beside it is checked, and `scripts/packaging/install_the_one_server.sh` installs it. |
| `release_url` | Where server releases are downloaded from (default `https://updates.the-zeroth.com/server/linux`). |

A task's skills (`environment.skills_dir`) and MCP servers (`[[environment.mcp_servers]]`) go to
the team too.

## Without Harbor

The same server takes a task from the command line, for example for a benchmark that has no Harbor
adapter:

```sh
curl -fsSL https://updates.the-zeroth.com/server/linux/install.sh | sh -s -- --version 0.5.2
export OPENAI_API_KEY=...
cd <the repository> && the-one-server run - --model openai/gpt-6.2-sol --effort max \
  --on-question answer --timeout 8h < task.md
```

`run -` reads the task from standard input. The team's report goes to standard output and to
`.the-one/delivery.md`, with `run.json` (outcome, usage and cost, changed files) and the ATIF
`trajectory.json` beside it. Exit codes: 0 delivered, 1 failed, 2 stopped at a question, 3 timed
out. The [Linux server documentation](https://the-zeroth.com/en/docs/the-one-app/settings/linux-server)
covers the rest.

## License

[Apache-2.0](LICENSE).
