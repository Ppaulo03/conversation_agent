"""A tiny agent whose one protected capability attaches a file the contact sent to a ticket."""

from __future__ import annotations

from typing import Any

from conversation_agent.core.compiler import CompiledAgent, compile_manifest

CONNECTION = "tickets_api"


def manifest(*, content: bool = False) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "min_framework": "0.1.0",
        "agent_id": "files-demo",
        "version": "0.1.0",
        "timezone": "America/Sao_Paulo",
        "persona": "Você ajuda a anexar arquivos a chamados. Use a ferramenta de anexo.",
        "allowed_capabilities": ["files.attach"],
        "confirmation": {
            "prompt": "Posso confirmar? {summary}. Responda SIM ou NÃO.",
            "executed_fallback": "Pronto ({status}).",
        },
        "capabilities": [
            {
                "name": "files.attach",
                "description": "Attaches a file the contact sent to one of their tickets.",
                "risk": "irreversible",
                "confirmation_required": True,
                "summary_template": "Anexar {file} ao chamado {ticket}",
                "executed_template": "Anexei {file} ao chamado {ticket} (anexo {attachment_id}).",
                "input": {
                    "ticket": {"type": "string", "description": "The ticket number"},
                    "file": {"type": "media", "description": "The file to attach"},
                },
                "output": {"attachment_id": {"type": "string"}},
            }
        ],
        "tools": [
            {
                "name": "tickets_attach",
                "description": "POST /tickets/{ticket}/files",
                "risk": "irreversible",
                "confirmation_required": True,
                "timeout_seconds": 5.0,
                "provider": "http",
                "connection": CONNECTION,
                "http": {
                    "method": "POST",
                    "path": "/tickets/{ticket}/files",
                    "body": ["file"],
                    **({"media_content": ["file"]} if content else {}),
                },
                "idempotency_supported": True,
                "recovery": {"strategy": "human_handoff"},
                "input": {"ticket": {"type": "string"}, "file": {"type": "media"}},
                "output": {"id": {"type": "string"}},
            }
        ],
        "bindings": [
            {
                "capability": "files.attach",
                "tool": "tickets_attach",
                "input_map": {"ticket": "$.ticket", "file": "$.file"},
                "output_map": {"attachment_id": "$.id"},
                "error_map": {
                    "default_5xx": {"write": {"type": "unknown"}},
                    "timeout": {"write": {"type": "unknown"}},
                },
            }
        ],
    }


def compiled_media_agent(*, content: bool = False) -> CompiledAgent:
    return compile_manifest(manifest(content=content))
