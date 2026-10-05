"""`python -m conversation_agent.app.serve <manifest.yaml> --http CONNECTION=URL [options]`

Runs the DURABLE runtime (PostgreSQL, ledger, confirmation, outbox, timers, reconciliation) with
the terminal as the channel: you type, the agent answers, a protected action is proposed,
confirmed and executed for real. The same `Runtime` is what a production deployment assembles with
its own channel sender.

Needs: `DATABASE_URL` (a migrated database, see `app.migrate apply`) and the LLM settings of
`app.llm_factory` (`LLM_PROVIDER`, `LLM_API_KEY`, ...). Options:
  --http ID=URL     base URL of the HTTP connection the manifest calls `ID` (local development
                    only: private networks and plain HTTP are allowed). Repeatable.
  --packs DIR       where the manifest's `packs` are installed from.
  --tenant/--contact  identity of the terminal conversation (default: local / terminal).
Type `/quit` (or end the input) to leave; piped input works, replies are delivered before exit.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from typing import NamedTuple

from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.manifest.pack_loader import DirectoryPackLoader
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.senders.console import ConsoleChannel
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.app.llm_factory import LLMConfig, build_llm, close_llm
from conversation_agent.app.runtime import Runtime
from conversation_agent.core.compiler import CompiledAgent, CompileError, compile_manifest
from conversation_agent.core.errors import ConversationAgentError, DefinitionError
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.llm import LLMProvider

USAGE = (
    "usage: python -m conversation_agent.app.serve <manifest.yaml> [--http ID=URL ...] "
    "[--packs DIR] [--tenant T] [--contact C]"
)


class Args(NamedTuple):
    manifest: str
    connections: dict[str, str]
    packs_dir: str | None = None
    tenant: str = "local"
    contact: str = "terminal"


def parse_args(argv: Sequence[str]) -> Args:
    """ValueError on bad usage."""
    args = list(argv)
    connections: dict[str, str] = {}
    options: dict[str, str] = {}
    positional: list[str] = []
    while args:
        arg = args.pop(0)
        if arg in ("--http", "--packs", "--tenant", "--contact"):
            if not args:
                raise ValueError(f"{arg} needs a value")
            value = args.pop(0)
            if arg == "--http":
                name, sep, url = value.partition("=")
                if not sep or not name or not url:
                    raise ValueError("--http expects ID=URL")
                connections[name] = url
            else:
                options[arg] = value
        elif arg.startswith("--"):
            raise ValueError(f"unknown option {arg}")
        else:
            positional.append(arg)
    if len(positional) != 1:
        raise ValueError("give exactly one manifest")
    return Args(
        positional[0],
        connections,
        options.get("--packs"),
        options.get("--tenant", "local"),
        options.get("--contact", "terminal"),
    )


def load_agent(manifest: str, packs_dir: str | None) -> CompiledAgent:
    raw = load_manifest_file(manifest)
    catalog = None
    if packs_dir is not None:
        declared = raw.get("packs")
        catalog = DirectoryPackLoader(packs_dir).catalog_for(
            declared if isinstance(declared, list) else []
        )
    return compile_manifest(raw, catalog)


async def converse(
    runtime: Runtime,
    channel: ConsoleChannel,
    *,
    read_line: Callable[[], str],
    write: Callable[[str], None] = print,
    poll_interval: float = 0.2,
) -> None:
    """The terminal loop: workers run in the background; each typed line is an inbound event."""
    stop = asyncio.Event()
    background = asyncio.create_task(runtime.run(stop, poll_interval=poll_interval))
    try:
        write("Type a message. /quit to leave.")
        while True:
            try:
                line = (await asyncio.to_thread(read_line)).strip()
            except EOFError:
                break
            if line == "/quit":
                break
            if line:
                await runtime.receive(channel.inbound(line))
    finally:
        stop.set()
        await background
    await runtime.drain()  # the answers to what was typed last are produced and shown


async def run(
    args: Args,
    *,
    dsn: str,
    llm: LLMProvider,
    clock: Clock | None = None,
    read_line: Callable[[], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    compiled = load_agent(args.manifest, args.packs_dir)
    clock = clock or SystemClock(compiled.agent.timezone)  # the channel and the agent share "now"
    http = HTTPToolProvider.static(
        {name: local_dev_connection(url, name) for name, url in args.connections.items()}
    )
    identity = ConversationIdentity(
        tenant_id=args.tenant,
        channel_id="console",
        conversation_id=f"console-{args.contact}",
        session_id=f"console-{args.contact}",
        contact_id=args.contact,
    )
    db = await PostgresDatabase.connect(dsn, max_size=4)
    try:
        channel = ConsoleChannel(identity, clock, write=write)
        runtime = Runtime.build(
            db=db,
            compiled=compiled,
            llm=llm,
            providers={"http": http},
            sender=channel,
            clock=clock,
        )
        await converse(runtime, channel, read_line=read_line, write=write)
        return 0
    finally:
        await http.aclose()
        await db.close()


async def _serve(args: Args, dsn: str, llm: LLMProvider) -> int:
    try:
        return await run(args, dsn=dsn, llm=llm)
    finally:
        await close_llm(llm)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        print(f"{exc}\n{USAGE}", file=sys.stderr)
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        llm = build_llm(LLMConfig.from_env(os.environ))
        return asyncio.run(_serve(args, dsn, llm))
    except ConversationAgentError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except CompileError as exc:
        print(f"{args.manifest}: {len(exc.diagnostics)} error(s)", file=sys.stderr)
        for diagnostic in exc.diagnostics:
            print(f"  {diagnostic}", file=sys.stderr)
        return 1
    except (OSError, DefinitionError, UnicodeDecodeError) as exc:
        print(f"cannot start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
