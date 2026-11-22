# Exact plugin SDK contract

This records the signatures from the authoritative specification (§7), not a
new interface proposal. Runtime, examples, tests and documentation must use them.

## Mutable vs frozen dataclasses

- `Tool` and `PluginContext`: frozen.
- `ConnectResult`, `ToolError`, `ToolResult`: mutable. The core may assign an
  OAuth redirect to `ConnectResult.redirect_url` after `connect()` returns.
- Each default Tool schema is a completely fresh object:
  `{"type": "object", "properties": {}}`; nested properties must not be shared.

## Storage protocol

```python
def get(self, key: str, default: Any = None) -> Any: ...
def set(self, key: str, value: Any, ttl: int | None = None) -> None: ...
def delete(self, key: str) -> bool: ...
def list_keys(self, prefix: str = "") -> list[str]: ...
```

The keyword is `ttl`, not `ttl_seconds`; reads support a caller-supplied default,
deletes report whether a key existed, and listing supports prefix filtering.

## Plugin base

```python
def __init__(self, ctx: PluginContext) -> None:
    self.ctx = ctx

# Required synchronous methods
def auth_methods(self) -> list[AuthMode]: ...
def list_tools(self) -> list[Tool]: ...
def connect(self) -> ConnectResult: ...
def ping(self) -> Status: ...
def invoke_tool(self, name: str, args: dict[str, Any]) -> ToolResult: ...

# Optional synchronous hooks
def on_api_key_set(self, api_key: str) -> None: ...
def on_oauth_credentials_set(
    self, client_id: str, client_secret: str | None
) -> None: ...
def get_oauth_sign_in_url(self, state: str) -> str:
    raise NotImplementedError
def on_oauth_result(self, code: str, state: str) -> ConnectResult:
    raise NotImplementedError
def refresh(self) -> Status:
    return Status.AUTH_EXPIRED
def on_restore(self) -> None: ...
def on_disconnect(self) -> None: ...
def shutdown(self) -> None: ...
```

Credential hooks **do receive their arguments** after the core stores the same
credentials in reserved scoped keys. Reading stored credentials is convenient,
not a reason to remove hook parameters. OAuth state and code are explicit
arguments. `refresh()` returns `Status`, not `ConnectResult`.

## Result constructors

```python
ToolResult.success(result: dict[str, Any]) -> ToolResult
ToolResult.failure(
    code: str, message: str, retryable: bool = False
) -> ToolResult
```

`success` requires a dictionary; it does not default to `None`. Host validation
still handles malformed plugin return values rather than trusting annotations.

## Public exports

`MCPPlugin`, `PluginContext`, `Storage`, `AuthMode`, `Status`, `Tool`,
`ConnectResult`, `ToolError`, `ToolResult` — with no non-stdlib SDK dependencies.

## Regression tests

Check named parameters/defaults with `inspect.signature`, mutable redirect/result
fields, default `refresh()` status, unsupported OAuth hooks raising, presence of
`instance.ctx`, independent nested schemas, callable hook arguments and all
Storage protocol signatures. Do not merely test that imports succeed.
