"""OpenAPI importer (optional): an allowlisted operation -> an HTTP ToolManifest.

Same stance as the MCP importer (see `tool_import`): the document is untrusted data, only the
operations the operator names are imported, risk is the operator's decision (a method that
changes state cannot be allowed as `read` without saying so), and what cannot be represented
exactly is refused:

  - the document's `servers` and `securitySchemes` are IGNORED on purpose: where requests go and
    how they authenticate is the connection's business, never the document's (DESIGN §30);
  - path and query parameters and a JSON object body are supported; header/cookie parameters and
    other body media types are not (they could not be sent, and a required input that is never
    sent is the failure the compiler exists to prevent);
  - a name that is both a parameter and a body field is ambiguous and refused.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from conversation_agent.core.definitions.capability import Risk
from conversation_agent.core.definitions.json_schema import (
    UnsupportedSchemaError,
    clean_text,
    model_spec_from_json_schema,
)
from conversation_agent.core.definitions.manifest import ToolManifest
from conversation_agent.core.definitions.schema_spec import ModelSpec
from conversation_agent.core.definitions.tool import (
    HTTPRequestSpec,
    RecoverySpec,
    RetryPolicy,
)
from conversation_agent.core.definitions.tool_import import (
    ImportDiagnostic,
    ImportReport,
    local_name,
)

_METHODS = ("get", "post", "put", "patch", "delete")
_READ_METHODS = frozenset({"get"})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AllowedOperation(_Frozen):
    operation_id: str
    risk: Risk  # REQUIRED
    name: str | None = None
    description: str | None = None
    confirmation_required: bool = False
    idempotency_supported: bool = False
    timeout_seconds: float = 5.0
    retry: RetryPolicy = RetryPolicy()
    recovery: RecoverySpec | None = None
    accept_method_risk_conflict: bool = False  # `read` on a state-changing method


class OpenAPIAllowlist(_Frozen):
    connection: str
    prefix: str = "api"
    operations: tuple[AllowedOperation, ...]


def _find(document: dict[str, Any], operation_id: str) -> tuple[str, str, dict[str, Any]] | None:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return None
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in _METHODS:
            operation = item.get(method)
            if isinstance(operation, dict) and operation.get("operationId") == operation_id:
                shared = item.get("parameters", [])
                merged = {**operation, "parameters": [*shared, *operation.get("parameters", [])]}
                return str(path), method, merged
    return None


def _ref(document: dict[str, Any], node: Any) -> Any:
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        target: Any = document
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        return target
    return node


def _success_schema(document: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any] | None:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    for status in sorted(responses):
        if str(status).startswith("2"):
            response = _ref(document, responses[status])
            content = response.get("content") if isinstance(response, dict) else None
            media = content.get("application/json") if isinstance(content, dict) else None
            schema = media.get("schema") if isinstance(media, dict) else None
            if isinstance(schema, dict):
                return schema
    return None


def import_openapi_tools(document: dict[str, Any], allowlist: OpenAPIAllowlist) -> ImportReport:
    imported: list[ToolManifest] = []
    problems: list[ImportDiagnostic] = []
    allowed_ids = {a.operation_id for a in allowlist.operations}

    def bad(tool: str, code: str, message: str) -> None:
        problems.append(ImportDiagnostic(code=code, tool=tool, message=message))

    for allowed in allowlist.operations:
        found = _find(document, allowed.operation_id)
        if found is None:
            bad(allowed.operation_id, "ALLOWED_OPERATION_NOT_FOUND", "no such operationId")
            continue
        path, method, operation = found
        if (
            allowed.risk == "read"
            and method not in _READ_METHODS
            and not allowed.accept_method_risk_conflict
        ):
            bad(
                allowed.operation_id,
                "RISK_CONFLICTS_WITH_METHOD",
                f"{method.upper()} changes state but the allowlist says `read`",
            )
            continue
        built = _build(document, allowed, path, method, operation, bad)
        if built is None:
            continue
        spec, http, output = built
        imported.append(
            ToolManifest(
                name=allowed.name or local_name(allowlist.prefix, allowed.operation_id),
                description=allowed.description
                or clean_text(operation.get("summary") or operation.get("description"))
                or allowed.operation_id,
                input=spec,
                output=output,
                risk=allowed.risk,
                confirmation_required=allowed.confirmation_required,
                timeout_seconds=allowed.timeout_seconds,
                provider="http",
                connection=allowlist.connection,
                http=http,
                idempotency_supported=allowed.idempotency_supported,
                retry=allowed.retry,
                recovery=allowed.recovery,
            )
        )
    operations = [
        str(op["operationId"])
        for item in (document.get("paths") or {}).values()
        if isinstance(item, dict)
        for m in _METHODS
        if isinstance(op := item.get(m), dict) and "operationId" in op
    ]
    return ImportReport(
        tools=tuple(imported),
        diagnostics=tuple(problems),
        not_exposed=tuple(sorted(o for o in operations if o not in allowed_ids)),
    )


def _build(
    document: dict[str, Any],
    allowed: AllowedOperation,
    path: str,
    method: str,
    operation: dict[str, Any],
    bad: Any,
) -> tuple[ModelSpec, HTTPRequestSpec, ModelSpec | None] | None:
    tool = allowed.operation_id
    spec: ModelSpec = {}
    query: list[str] = []
    body: list[str] = []
    failed = False
    for raw in operation["parameters"]:
        param = _ref(document, raw)
        if not isinstance(param, dict) or not isinstance(param.get("name"), str):
            bad(tool, "UNSUPPORTED_PARAMETER", "a parameter could not be read")
            failed = True
            continue
        name, where = param["name"], param.get("in")
        if where not in ("path", "query"):
            bad(tool, "UNSUPPORTED_PARAMETER_LOCATION", f"parameter {name!r} is in {where!r}")
            failed = True
            continue
        schema = param.get("schema")
        try:
            wrapper = model_spec_from_json_schema(
                {
                    "type": "object",
                    "properties": {name: schema or {}},
                    "required": [name] if param.get("required") or where == "path" else [],
                },
                root=document,
            )
        except UnsupportedSchemaError as exc:
            for message in exc.problems:
                bad(tool, "UNSUPPORTED_SCHEMA", message)
            failed = True
            continue
        spec.update(wrapper)
        (query if where == "query" else []).append(name)
    request_body = _ref(document, operation.get("requestBody"))
    if request_body is not None:
        content = request_body.get("content") if isinstance(request_body, dict) else None
        media = content.get("application/json") if isinstance(content, dict) else None
        if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
            bad(
                tool, "UNSUPPORTED_BODY", "only an application/json body with a schema is supported"
            )
            failed = True
        else:
            try:
                fields = model_spec_from_json_schema(media["schema"], root=document)
            except UnsupportedSchemaError as exc:
                for message in exc.problems:
                    bad(tool, "UNSUPPORTED_SCHEMA", message)
                failed = True
            else:
                for name, field in fields.items():
                    if name in spec:
                        bad(
                            tool, "AMBIGUOUS_NAME", f"{name!r} is both a parameter and a body field"
                        )
                        failed = True
                    optional_body = not request_body.get("required", True)
                    spec[name] = (
                        field.model_copy(update={"required": False}) if optional_body else field
                    )
                    body.append(name)
    if failed:
        return None
    output_schema = _success_schema(document, operation)
    output: ModelSpec | None = None
    if output_schema is not None:
        try:
            output = model_spec_from_json_schema(output_schema, root=document)
        except UnsupportedSchemaError:
            output = None  # a convenience, never approximated
    http = HTTPRequestSpec(method=method.upper(), path=path, query=tuple(query), body=tuple(body))
    return spec, http, output
