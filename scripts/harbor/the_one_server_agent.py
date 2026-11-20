"""A Harbor installed agent that runs The One's headless server on a task (roadmap F8).

    PYTHONPATH=scripts/harbor uvx --python 3.12 --from harbor==0.24.0 harbor run \\
        -p <task or dataset> -a the_one_server_agent:TheOneServer -m anthropic/claude-sonnet-4-5 \\
        --ak archive=dist/the-one-server/the-one-server-<version>-linux-x86_64.tar.gz

Terminal-Bench 2 runs through Harbor (``-d terminal-bench@2.0``), so this is also how it calls The
One. In the task's container the agent installs the Linux server bundle with its ``install.sh``
(the ``archive`` given, checked against the ``.sha256`` beside it, or the published version; on a
container of another architecture or C library, the bundle beside ``archive`` built for it, such
as ``...-linux-x86_64-musl.tar.gz`` on Alpine), and
hands the task to a team with ``the-one-server run``, the way a person on a server would: the
team works in the task's working directory, nobody answers its questions (``--on-question
answer`` tells it to decide by itself), and ``run.json``, ``delivery.md``, the progress lines and
the server's logs land in the trial's ``agent/the-one/``. The tokens and cost in ``run.json`` fill
the trial's agent result, and the run's ATIF trajectory (every Agent's conversation, tool calls
and results) is the trial's ``agent/trajectory.json``. With ``--ak assistant=true`` the task goes
to the Space's main assistant instead (``run --assistant``), which chooses who does the work, as
it does for a person in the app.

Model credentials reach the server only in its credentials file, mode 600 and owned by the user
the agent runs as: the ``credentials_file`` given (it must be mode 600 itself), then a connection
for each of ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``OPENROUTER_API_KEY`` and
``GEMINI_API_KEY`` (with its ``*_BASE_URL``) set where Harbor runs, which replaces the file's
connection of the same id, as the server's own environment variables would. The file is copied
into the container, so no key is on a command line or in a command's environment. ``--ae``
variables are Harbor's to pass, on the ``docker exec`` command line; keys do not belong there.

When Harbor stops waiting (the task's agent timeout, or the job is cancelled), the agent sends
``run`` a SIGTERM and waits for it: the team is frozen and the results are written before the
verifier looks at the work. Set ``run_timeout`` below the task's agent timeout to let ``run`` end
on its own first.

A task can give the agent skills (``environment.skills_dir``) and MCP servers
(``[[environment.mcp_servers]]``). The skills are listed at the end of the task text, each with
its SKILL.md. Each MCP server is added before the run, through the Gateway as a person would add
one, to a copy of the Default Space made active (the Default refuses changes), so its tools are
discovered then, and every Agent there gets it as an ``mcp:<name>`` tool. The One's client speaks stdio and streamable HTTP; an SSE server's
URL ending in ``/sse`` is used at ``/mcp`` instead, where servers that speak both (such as
@playwright/mcp) serve streamable HTTP. The end of the task text also names each server, its
address and whether its tools were found.

Harbor needs Python 3.12; this module imports only Harbor, Pydantic and the standard library. It
runs under Harbor 0.24.0 and 0.26.0. Where server versions are downloaded from is ``release_url``.
Harbor 0.26's ``--base-url`` (the inference API's address, through its model connections) is not
taken yet: the model's address comes from ``*_BASE_URL`` where Harbor runs or the credentials file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, override

from pydantic import Field

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.options import Cli, InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import MCPServerConfig

INSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "install_the_one_server.sh"
DEFAULT_RELEASE_URL = "https://updates.the-zeroth.com/server/linux"
# install.sh, run as root, links the command here.
COMMAND = "/usr/local/bin/the-one-server"
# The server's data and configuration, the task text and the pid of the running ``run``.
STATE = PurePosixPath("/installed-agent/the-one")
# Each provider the server reads from its environment: its key variable and base URL variable
# (src/desktop_runtime/headless/credentials.py, ENV_KEYS).
PROVIDER_ENVS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL"),
    "gemini": ("GEMINI_API_KEY", "GEMINI_BASE_URL"),
}
# How long a SIGTERM'd ``run`` may take to freeze the team, write its results and stop the server.
STOP_GRACE_SECONDS = 240
# A bundle's name ends in the machine it is built for, as install.sh names them.
ARCHIVE_NAME = re.compile(
    r"(?P<stem>the-one-server-[0-9A-Za-z.+-]+-linux-)(?P<platform>(?:x86_64|arm64)(?:-musl)?)"
    r"\.tar\.gz"
)
# The task container's machine as install.sh tells it: the architecture, with -musl where the
# C library is musl rather than glibc (Alpine).
PLATFORM_COMMAND = (
    "case $(uname -m) in x86_64|amd64) arch=x86_64 ;; aarch64|arm64) arch=arm64 ;; "
    "*) arch=$(uname -m) ;; esac; "
    "if getconf GNU_LIBC_VERSION >/dev/null 2>&1; then echo $arch; "
    "elif [ -e /lib/ld-musl-x86_64.so.1 ] || [ -e /lib/ld-musl-aarch64.so.1 ]; "
    "then echo $arch-musl; else echo $arch; fi"
)
# The start of each skill's SKILL.md in a task's skills directory, after a line naming the file.
SKILLS_COMMAND = (
    'for f in {folder}/*/SKILL.md; do [ -f "$f" ] || continue; '
    'printf "@@SKILL %s\\n" "$f"; sed -n "1,60p" "$f"; done; true'
)
SKILL_MARKER = "@@SKILL "
# The characters an MCP Server ID may have (capability_registry.domain.McpServerConfig).
MCP_ID_OTHER = re.compile(r"[^A-Za-z0-9_.-]+")


class TheOneServerOptions(InstalledAgentOptions):
    """``--ak`` options. ``version`` is the published version to download without ``archive``."""

    archive: str | None = Field(
        default=None,
        description=(
            "A the-one-server-<version>-linux-<arch>.tar.gz to install; the .sha256 beside it is "
            "checked. A task container of another architecture or with musl (Alpine) gets the "
            "bundle beside it built for that machine, such as ...-linux-x86_64-musl.tar.gz. Omit "
            "to download the published one."
        ),
    )
    release_url: str | None = Field(
        default=None,
        description=f"Where server versions are published (default {DEFAULT_RELEASE_URL}).",
    )
    credentials_file: str | None = Field(
        default=None,
        description="A the-one-server credentials.json (mode 600) with the model connections.",
    )
    team: Annotated[str | None, Cli("--team")] = Field(
        default=None, description="The team (default: the server's, polished_software_dev)."
    )
    assistant: Annotated[bool, Cli("--assistant")] = Field(
        default=False,
        description=(
            "Give the task to the Space's main assistant as the person's message instead of to a "
            "team; it chooses who does the work, as in the app."
        ),
    )
    connection: Annotated[str | None, Cli("--connection")] = Field(
        default=None, description="Which configured connection runs the model."
    )
    reasoning_effort: Annotated[str | None, Cli("--effort")] = Field(
        default=None, description="Reasoning effort (harbor run --effort)."
    )
    run_timeout: Annotated[str | None, Cli("--timeout")] = Field(
        default=None,
        description="How long the team may take, for example 45m (default 3h).",
    )
    idle: Annotated[str | None, Cli("--idle")] = Field(
        default=None,
        description="How long the team must rest after delivering before the run ends (60s).",
    )
    on_question: Annotated[Literal["answer", "stop"], Cli("--on-question")] = Field(
        default="answer",
        description="answer: the team decides by itself; stop: the run ends at a question.",
    )
    workdir: Annotated[str | None, Cli("--workdir")] = Field(
        default=None, description="The folder the team works in (default: the task's)."
    )


def _capabilities() -> AgentCapabilities:
    """What the agent applies, said in the fields the installed Harbor has.

    It takes a task's skills and MCP servers, and Harbor's ``--effort`` (``run --effort``). Harbor
    0.26 refuses ``--effort`` for an agent that does not declare ``reasoning_effort``; 0.24 has no
    such field and refuses fields it does not know, so it is declared only where Harbor has it.
    ``base_url`` stays undeclared: Harbor 0.26 takes ``--base-url`` only through a model
    connection (``MODEL_CONNECTION``), which this agent does not declare yet.
    """

    declared = {"skills": True, "mcp_servers": True}
    if "reasoning_effort" in AgentCapabilities.model_fields:
        declared["reasoning_effort"] = True
    return AgentCapabilities(**declared)


class TheOneServer(BaseInstalledAgent):
    """The One's team on a Harbor task, through ``the-one-server run``."""

    options_model = TheOneServerOptions
    capabilities = _capabilities()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        options = self._options
        if options.archive is not None and not Path(options.archive).is_file():
            raise ValueError(f"archive {options.archive} is not a file")
        # Read now, so a file nobody else may read is checked before any trial starts.
        self._connections, self._default = _file_connections(options.credentials_file)
        self._connections.update(self._environment_connections())
        self._owner: str | None = None
        # How many tools each task MCP server offered when it was added, by id (None: none found).
        self._mcp_tools: dict[str, int | None] = {}
        if not self._connections:
            raise ValueError(
                "No model credentials: set ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY "
                "or GEMINI_API_KEY where Harbor runs, or pass credentials_file (mode 600)."
            )

    @property
    def _options(self) -> TheOneServerOptions:
        assert isinstance(self.options, TheOneServerOptions)
        return self.options

    @staticmethod
    @override
    def name() -> str:
        return "the-one-server"

    @override
    def get_version_command(self) -> str | None:
        return f"{COMMAND} --version"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().removeprefix("the-one-server").strip()

    @property
    def _out(self) -> PurePosixPath:
        """Where ``run`` writes, inside the trial's agent logs."""

        return self.environment_logs_dir / "the-one"

    def _server(self, *args: str) -> str:
        """A the-one-server command line on this agent's data and configuration."""

        return shlex.join(
            [
                COMMAND,
                "--data-dir", str(STATE / "data"),
                "--config-dir", str(STATE / "config"),
                *args,
            ]
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        options = self._options
        if options.archive is not None:
            await self.ensure_system_dependencies(environment, ("tar", "coreutils"))
            archive = await self._archive_for(environment, Path(options.archive))
            target = f"/installed-agent/{archive.name}"
            await environment.upload_file(archive, target)
            checksum = archive.with_name(archive.name + ".sha256")
            if checksum.is_file():
                await environment.upload_file(checksum, target + ".sha256")
            await environment.upload_file(INSTALL_SCRIPT, "/installed-agent/install.sh")
            command = f"sh /installed-agent/install.sh --archive {shlex.quote(target)}"
        else:
            await self.ensure_system_dependencies(
                environment, ("curl", "ca_certificates", "tar", "coreutils")
            )
            release_url = (options.release_url or DEFAULT_RELEASE_URL).rstrip("/")
            arguments = ["--base-url", release_url]  # install.sh's own name for it
            if options.version:
                arguments += ["--version", options.version]
            command = (
                f"curl -fsSL {shlex.quote(release_url + '/install.sh')} | sh -s -- "
                + shlex.join(arguments)
            )
        await self.exec_as_root(environment, command)
        await self.exec_as_root(
            environment,
            f"mkdir -p {STATE / 'config'} && chmod 700 {STATE} {STATE / 'config'}",
        )
        credentials: dict[str, Any] = {"connections": list(self._connections.values())}
        if self._default:
            credentials["default"] = self._default
        await self._upload_private(
            environment, json.dumps(credentials), STATE / "config" / "credentials.json"
        )
        owner = await self._agent_owner(environment)
        await self.exec_as_root(environment, f"chown -R {owner} {STATE}")
        if self.mcp_servers:
            await self._add_mcp_servers(environment)

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        note = await self._task_setup_note(environment)
        if note:
            instruction = f"{instruction.rstrip()}\n\n{note}\n"
        task = STATE / "task.md"
        await self._upload_private(environment, instruction, task)
        arguments = ["run", "-", "--out", str(self._out)]
        if self.model_name:
            arguments += ["--model", self.model_name]
        flags = self.build_cli_flags()
        out = shlex.quote(str(self._out))
        command = "\n".join(
            [
                f"mkdir -p {out}",
                f"{self._server(*arguments)} {flags} < {task}"
                f" > {out}/stdout.txt 2> {out}/progress.txt &",
                # The pid lets a stop from Harbor reach run itself, which then freezes the team.
                f"echo $! > {STATE / 'run.pid'}",
                "wait $!",
                "code=$?",
                f"tail -n 40 {out}/progress.txt >&2",
                "exit $code",
            ]
        )
        try:
            await self.exec_as_agent(environment, command)
        except asyncio.CancelledError:
            await self._stop_run(environment)
            raise
        finally:
            await self._collect_server_logs(environment)

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        self._place_trajectory()
        try:
            record = json.loads((self.logs_dir / "the-one" / "run.json").read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(record, dict):
            return
        usage = record.get("usage") or {}
        context.n_input_tokens = usage.get("prompt_tokens")
        context.n_cache_tokens = usage.get("cache_read_tokens")
        context.n_output_tokens = usage.get("completion_tokens")
        context.cost_usd = usage.get("cost_usd")
        context.metadata = {
            **(context.metadata or {}),
            "the_one": {
                key: record.get(key)
                for key in ("outcome", "exit_code", "reason", "team", "entry_node",
                            "workflow_id", "execution_id", "cleanup", "elapsed_seconds",
                            "workdir_changes")
                if key in record
            }
            | {key: usage.get(key)
               for key in ("llm_calls", "cache_write_tokens", "reasoning_tokens")}
            | {"delivery": record.get("delivery")},
        }

    def _place_trajectory(self) -> None:
        """The run's ATIF trajectory where Harbor's viewer and ``harbor analyze`` read an
        agent's: the trial's ``agent/trajectory.json``, naming the model the job ran."""

        source = self.logs_dir / "the-one" / "trajectory.json"
        try:
            document = json.loads(source.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(document, dict):
            return
        agent = document.get("agent")
        if isinstance(agent, dict) and self.model_name and not agent.get("model_name"):
            agent["model_name"] = self.model_name
        (self.logs_dir / "trajectory.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    async def _archive_for(self, environment: BaseEnvironment, archive: Path) -> Path:
        """``archive``, or the bundle beside it built for the task container's machine.

        One job can mix task images: SWE-Atlas QnA has Alpine ones among its Debian and Ubuntu
        ones, and install.sh refuses a glibc bundle on musl.
        """

        name = ARCHIVE_NAME.fullmatch(archive.name)
        if name is None:
            return archive
        result = await self.exec_as_root(environment, PLATFORM_COMMAND)
        platform = str(result.stdout or "").strip()
        if platform in ("", name["platform"]):
            return archive
        sibling = archive.with_name(f"{name['stem']}{platform}.tar.gz")
        if not sibling.is_file():
            raise RuntimeError(
                f"{archive.name} is for {name['platform']}, and this task's container is "
                f"{platform}: put {sibling.name} (with its .sha256) beside it"
            )
        return sibling

    async def _add_mcp_servers(self, environment: BaseEnvironment) -> None:
        """Each task MCP server in the server's Space, its tools found, for every Agent there.

        The server starts once; the Gateway copies the Default Space, makes the copy active and
        saves each server in it, which lists the server's tools as when a person adds one; with
        the server stopped again, each Agent's tools gain ``mcp:<id>``, and ``run`` starts the
        server on that Space.
        """

        await self.ensure_system_dependencies(environment, ("curl",))
        folder = STATE / "mcp"
        await self.exec_as_agent(environment, f"mkdir -p {folder} && chmod 700 {folder}")
        definitions = {d["id"]: d for d in map(_mcp_definition, self.mcp_servers)}
        for server_id, definition in definitions.items():
            await self._upload_private(
                environment, json.dumps(definition), folder / f"{server_id}.json"
            )
        start = ["start"]
        if self.model_name:
            start += ["--model", self.model_name]
        if self._options.connection:
            start += ["--connection", self._options.connection]
        await self.exec_as_agent(
            environment,
            _mcp_setup_script(self._server(*start), self._server("stop"), list(definitions)),
        )
        for server_id in definitions:
            answer = await self.exec_as_agent(
                environment, f"cat {folder / (server_id + '.out')} 2>/dev/null; true"
            )
            count, reason = _discovered_tools(str(answer.stdout or ""))
            self._mcp_tools[server_id] = count
            if count is None:
                self.logger.warning("MCP server %s: no tools found (%s)", server_id, reason)
            else:
                self.logger.info("MCP server %s: %d tools", server_id, count)

    async def _task_setup_note(self, environment: BaseEnvironment) -> str:
        """What the end of the task text says about the task's skills and MCP servers."""

        parts = []
        if self.skills_dir:
            listing = await self.exec_as_agent(
                environment, SKILLS_COMMAND.format(folder=shlex.quote(self.skills_dir))
            )
            skills = _skills(str(listing.stdout or ""))
            if skills:
                parts.append("\n".join([
                    f"This task comes with skills in {self.skills_dir}. Before work a skill "
                    "covers, read its SKILL.md and follow it:",
                    *(f"- {name} ({path})" + (f": {description}" if description else "")
                      for name, description, path in skills),
                ]))
        if self.mcp_servers:
            lines = ["This task comes with MCP servers:"]
            for server in self.mcp_servers:
                server_id = _mcp_definition(server)["id"]
                where = server.url or shlex.join([server.command or "", *server.args])
                count = self._mcp_tools.get(server_id)
                lines.append(
                    f"- {server.name} ({server.transport} at {where}): "
                    + (f"its {count} tools are yours as mcp:{server_id}." if count
                       else "its tools could not be listed before the run started.")
                )
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def _environment_connections(self) -> dict[str, dict[str, Any]]:
        connections = {}
        for provider, (key_name, base_url_name) in PROVIDER_ENVS.items():
            key = self._get_env(key_name)
            if not key:
                continue
            connection = {"id": provider, "provider": provider, "api_key": key}
            base_url = self._get_env(base_url_name)
            if base_url:
                connection["base_url"] = base_url
            connections[provider] = connection
        return connections

    async def _upload_private(
        self, environment: BaseEnvironment, content: str, target: PurePosixPath
    ) -> None:
        """A file only the agent's user can read, copied in (never on a command line)."""

        with tempfile.TemporaryDirectory(prefix="the-one-harbor-") as directory:
            source = Path(directory) / target.name
            descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
            await environment.upload_file(source, str(target))
        owner = await self._agent_owner(environment)
        await self.exec_as_root(environment, f"chown {owner} {target} && chmod 600 {target}")

    async def _agent_owner(self, environment: BaseEnvironment) -> str:
        """``uid:gid`` of the user Harbor runs the agent's commands as.

        A copied-in file keeps the uid it has where Harbor runs (``docker cp``), and the server
        refuses a private file another user owns. So each one goes to the agent's user even when
        the task names none: when Harbor runs as a user other than the container's (uid 1001 on
        a GitHub runner, root in the container), the server would not start.
        """

        if self._owner is None:
            result = await self.exec_as_agent(environment, "id -u && id -g")
            ids = str(result.stdout or "").split()
            if len(ids) != 2 or not all(re.fullmatch(r"\d+", part) for part in ids):
                raise RuntimeError(f"could not tell which user runs the agent ({ids})")
            self._owner = ":".join(ids)
        return self._owner

    async def _stop_run(self, environment: BaseEnvironment) -> None:
        """Harbor stopped waiting: SIGTERM ``run`` and wait while it freezes the team."""

        command = (
            f"pid=$(cat {STATE / 'run.pid'} 2>/dev/null) || exit 0\n"
            'kill -TERM "$pid" 2>/dev/null || exit 0\n'
            "waited=0\n"
            f'while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt {STOP_GRACE_SECONDS} ]; do\n'
            "    sleep 1; waited=$((waited + 1))\n"
            "done"
        )
        try:
            await environment.exec(command, timeout_sec=STOP_GRACE_SECONDS + 30)
        except Exception as error:  # noqa: BLE001 - the trial is ending anyway; say why.
            self.logger.warning("Stopping the-one-server run failed: %s", error)

    async def _collect_server_logs(self, environment: BaseEnvironment) -> None:
        logs = shlex.quote(str(self._out / "server-logs"))
        try:
            await environment.exec(
                f"mkdir -p {logs} && cp -R {STATE / 'data' / 'logs'}/. {logs}/ 2>/dev/null; true",
                timeout_sec=120,
            )
        except Exception as error:  # noqa: BLE001 - logs only help reading a trial; go on without.
            self.logger.warning("Copying the server's logs failed: %s", error)


def _file_connections(path: str | None) -> tuple[dict[str, dict[str, Any]], str]:
    """A credentials file's connections, by id, and its default; refuses one others may read."""

    if path is None:
        return {}, ""
    source = Path(path).expanduser()
    try:
        status = source.lstat()
    except OSError as error:
        raise ValueError(f"credentials_file {source} cannot be read ({error.strerror})") from None
    if not stat.S_ISREG(status.st_mode):
        raise ValueError(f"credentials_file {source} is not a regular file")
    if status.st_mode & 0o077:
        raise ValueError(
            f"credentials_file {source} may be read by others; run: chmod 600 {source}"
        )
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        reason = type(error).__name__
        raise ValueError(f"credentials_file {source} is not JSON ({reason})") from None
    connections = data.get("connections") if isinstance(data, dict) else None
    if not isinstance(connections, list) or not all(isinstance(c, dict) for c in connections):
        raise ValueError(f"credentials_file {source} has no list of connections")
    by_id = {str(c.get("id") or c.get("provider") or ""): c for c in connections}
    default = data.get("default")
    return by_id, default if isinstance(default, str) else ""


def _mcp_definition(server: MCPServerConfig) -> dict[str, Any]:
    """A task's MCP server as The One's MCP server definition (PUT /api/presets/mcp-servers)."""

    server_id = MCP_ID_OTHER.sub("-", server.name).strip("-.") or "task-mcp"
    definition: dict[str, Any] = {"id": server_id, "display_name": server.name, "enabled": True}
    if server.transport == "stdio":
        return definition | {
            "transport": "stdio", "command": server.command or "", "args": list(server.args)
        }
    url = server.url or ""
    if server.transport == "sse":
        # The One's client speaks streamable HTTP, not the older SSE transport; servers that speak
        # both serve it at /mcp beside /sse.
        url = re.sub(r"/sse/?$", "/mcp", url)
    return definition | {"transport": "streamable_http", "url": url}


def _mcp_setup_script(start: str, stop: str, server_ids: list[str]) -> str:
    """The shell that adds the servers uploaded as ``mcp/<id>.json`` (``_add_mcp_servers``);
    the Gateway's answer for each lands in ``mcp/<id>.out``.

    The built-in Default Space refuses changes and is mirrored afresh from the bundle at each
    start, so the servers go into a copy of it, made active as a person would make one. If any
    step fails, the run goes on in the Default Space without them.
    """

    data = STATE / "data"
    value = 's/.*"{key}": *"\\([^"]*\\)".*/\\1/p'
    gateway_url = value.format(key="gateway_url")
    active_space = value.format(key="activeSpaceId")
    # awk turns each \n into a line break: one tools entry per server, under "tools:".
    entries = "".join(f"- name: mcp:{server_id}\\n" for server_id in server_ids)
    lines = [
        f"cd {STATE / 'mcp'} || exit 1",
        f"{start} > start.log 2>&1 || exit 1",
        f"gateway=$(sed -n '{gateway_url}' {data / 'headless' / 'instance.json'})",
        "umask 077",
        # The token goes to curl in a file, not on its command line.
        "printf 'X-Desktop-Local-Token: %s\\n' "
        f"\"$(cat {data / 'headless' / 'local-api-token'})\" > header",
        "call() { curl -sS -H @header -H 'Content-Type: application/json' \"$@\"; }",
        "call -X POST --data '{\"name\": \"Task\"}' \"$gateway/api/spaces/__default__/duplicate\""
        " > space.out 2>&1",
        # The answer is compact JSON whose first key is the copy's id.
        "space=$(sed -n 's/^{\"id\":\"\\([^\"]*\\)\".*/\\1/p' space.out)",
        "if [ -n \"$space\" ]; then",
        "  call -X POST \"$gateway/api/spaces/$space/activate\" > activate.out 2>&1",
    ]
    lines += [
        f"  call -X PUT --data @{server_id}.json "
        f"\"$gateway/api/presets/mcp-servers/{server_id}\" > {server_id}.out 2>&1"
        for server_id in server_ids
    ]
    lines += [
        "fi",
        "rm -f header",
        f"{stop} >> start.log 2>&1",
        f"if [ -n \"$space\" ] && [ \"$(sed -n '{active_space}' {data / 'spaces.json'})\" = "
        "\"$space\" ]; then",
        f"  find {data / 'spaces'}/\"$space\"/agents -name '*.yaml' | while read -r f; do",
        f"    awk -v add='{entries}' '{{ print }} $0 == \"tools:\" {{ printf \"%s\", add }}' "
        "\"$f\" > \"$f.new\" && mv \"$f.new\" \"$f\"",
        "  done",
        "fi",
        "true",
    ]
    return "\n".join(lines)


def _discovered_tools(answer: str) -> tuple[int | None, str]:
    """How many tools the Gateway found when it saved an MCP server, or None and why not."""

    try:
        reply = json.loads(answer)
    except ValueError:
        return None, answer.strip()[:200] or "no answer"
    discovery = reply.get("discovery") if isinstance(reply, dict) else None
    if not isinstance(discovery, dict) or not discovery.get("ok"):
        reason = discovery.get("error") if isinstance(discovery, dict) else None
        return None, str(reason or reply)[:200]
    return int(discovery.get("tool_count") or 0), ""


def _skills(listing: str) -> list[tuple[str, str, str]]:
    """(name, description, SKILL.md path) of each skill in ``SKILLS_COMMAND``'s output."""

    skills = []
    for block in listing.split(SKILL_MARKER)[1:]:
        path, _, text = block.partition("\n")
        path = path.strip()
        front = _front_matter(text)
        name = front.get("name") or PurePosixPath(path).parent.name
        skills.append((name, front.get("description", ""), path))
    return skills


def _front_matter(text: str) -> dict[str, str]:
    """The ``key: value`` lines of a SKILL.md's front matter, a folded value's lines joined."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    key = ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        pair = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
        if pair:
            key, value = pair[1], pair[2].strip()
            values[key] = "" if value in (">", "|", ">-", "|-") else value.strip("'\"")
        elif key and line[:1] in (" ", "\t"):
            values[key] = f"{values[key]} {line.strip()}".strip()
    return values
