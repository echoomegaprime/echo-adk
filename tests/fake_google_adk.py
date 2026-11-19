"""A minimal fake ``google.adk`` / ``google.genai`` installed into ``sys.modules`` for adapter tests.

Mirrors only the surface the adapter touches (google-adk 2.13.0 module paths and signatures). The
real package is never required by the test suite; ``pip install echo-adk[adk]`` brings the real one.
"""

from __future__ import annotations

import sys
import types as _types
from typing import Any, Optional

MODULES = (
    "google",
    "google.genai",
    "google.genai.types",
    "google.adk",
    "google.adk.models",
    "google.adk.models.base_llm",
    "google.adk.models.llm_request",
    "google.adk.models.llm_response",
    "google.adk.tools",
    "google.adk.tools.base_tool",
    "google.adk.tools.base_toolset",
    "google.adk.tools.tool_context",
    "google.adk.plugins",
    "google.adk.plugins.base_plugin",
    "google.adk.apps",
    "google.adk.apps.app",
    "google.adk.agents",
    "google.adk.agents.llm_agent",
    "google.adk.agents.readonly_context",
)


class _Record:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        out = {}
        for key, value in vars(self).items():
            if exclude_none and value is None:
                continue
            out[key] = value.model_dump(exclude_none) if isinstance(value, _Record) else value
        return out


class Part(_Record):
    def __init__(self, text: Optional[str] = None, function_call: Any = None, function_response: Any = None) -> None:
        super().__init__(text=text, function_call=function_call, function_response=function_response)


class Content(_Record):
    def __init__(self, role: str = "user", parts: Optional[list[Any]] = None) -> None:
        super().__init__(role=role, parts=list(parts or []))


class FunctionCall(_Record):
    def __init__(self, name: str, args: Optional[dict[str, Any]] = None) -> None:
        super().__init__(name=name, args=dict(args or {}))


class FunctionResponse(_Record):
    def __init__(self, name: str, response: Any = None) -> None:
        super().__init__(name=name, response=response)


class Schema(_Record):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class FunctionDeclaration(_Record):
    def __init__(self, name: str, description: str = "", parameters: Any = None) -> None:
        super().__init__(name=name, description=description, parameters=parameters)


class Tool(_Record):
    def __init__(self, function_declarations: Optional[list[Any]] = None) -> None:
        super().__init__(function_declarations=list(function_declarations or []))


class GenerateContentConfig(_Record):
    def __init__(self, system_instruction: Any = None, tools: Optional[list[Any]] = None) -> None:
        super().__init__(system_instruction=system_instruction, tools=list(tools or []))


class BaseLlm:
    def __init__(self, model: str, **kwargs: Any) -> None:
        self.model = model

    @classmethod
    def supported_models(cls) -> list[str]:
        return []

    async def generate_content_async(self, llm_request: Any, stream: bool = False):  # pragma: no cover
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator


class LlmRequest(_Record):
    def __init__(
        self, model: Optional[str] = None, contents: Optional[list[Any]] = None, config: Any = None, tools_dict: Optional[dict] = None
    ) -> None:
        super().__init__(
            model=model, contents=list(contents or []), config=config or GenerateContentConfig(), tools_dict=dict(tools_dict or {})
        )


class LlmResponse(_Record):
    def __init__(self, **kwargs: Any) -> None:
        defaults = {
            "content": None,
            "model_version": None,
            "partial": None,
            "turn_complete": None,
            "custom_metadata": None,
            "error_code": None,
        }
        defaults.update(kwargs)
        super().__init__(**defaults)


class BaseTool:
    def __init__(self, *, name: str, description: str, is_long_running: bool = False, custom_metadata: Any = None, **kwargs: Any) -> None:
        self.name = name
        self.description = description
        self.is_long_running = is_long_running
        self.custom_metadata = custom_metadata

    def _get_declaration(self) -> Any:
        return None

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class BaseToolset:
    def __init__(self, *, tool_filter: Any = None, tool_name_prefix: Optional[str] = None) -> None:
        self.tool_filter = tool_filter
        self.tool_name_prefix = tool_name_prefix

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None


class ToolContext(_Record):
    pass


class ReadonlyContext(_Record):
    pass


class BasePlugin:
    def __init__(self, name: str) -> None:
        self.name = name


class LlmAgent(_Record):
    def __init__(
        self, *, name: str, model: Any = "", instruction: str = "", tools: Optional[list[Any]] = None, description: str = "", **kwargs: Any
    ) -> None:
        super().__init__(name=name, model=model, instruction=instruction, tools=list(tools or []), description=description)


class App(_Record):
    def __init__(self, *, name: str, root_agent: Any = None, plugins: Optional[list[Any]] = None, **kwargs: Any) -> None:
        super().__init__(name=name, root_agent=root_agent, plugins=list(plugins or []))


def install() -> dict[str, _types.ModuleType]:
    """Register the fake modules. Returns the previous entries so ``uninstall`` can restore them."""
    previous = {name: sys.modules.get(name) for name in MODULES}
    mods = {name: _types.ModuleType(name) for name in MODULES}
    for mod in mods.values():
        mod.__path__ = []  # type: ignore[attr-defined] - mark as package
    mods["google.genai.types"].__dict__.update(
        Part=Part,
        Content=Content,
        FunctionCall=FunctionCall,
        FunctionResponse=FunctionResponse,
        Schema=Schema,
        FunctionDeclaration=FunctionDeclaration,
        Tool=Tool,
        GenerateContentConfig=GenerateContentConfig,
    )
    mods["google.genai"].types = mods["google.genai.types"]  # type: ignore[attr-defined]
    mods["google.adk.models.base_llm"].BaseLlm = BaseLlm  # type: ignore[attr-defined]
    mods["google.adk.models.llm_request"].LlmRequest = LlmRequest  # type: ignore[attr-defined]
    mods["google.adk.models.llm_response"].LlmResponse = LlmResponse  # type: ignore[attr-defined]
    mods["google.adk.tools.base_tool"].BaseTool = BaseTool  # type: ignore[attr-defined]
    mods["google.adk.tools.base_toolset"].BaseToolset = BaseToolset  # type: ignore[attr-defined]
    mods["google.adk.tools.tool_context"].ToolContext = ToolContext  # type: ignore[attr-defined]
    mods["google.adk.plugins.base_plugin"].BasePlugin = BasePlugin  # type: ignore[attr-defined]
    mods["google.adk.apps.app"].App = App  # type: ignore[attr-defined]
    mods["google.adk.agents.llm_agent"].LlmAgent = LlmAgent  # type: ignore[attr-defined]
    mods["google.adk.agents.readonly_context"].ReadonlyContext = ReadonlyContext  # type: ignore[attr-defined]
    mods["google.adk"].__version__ = "2.13.0-fake"  # type: ignore[attr-defined]
    for name, mod in mods.items():
        sys.modules[name] = mod
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, mod)
    return previous  # type: ignore[return-value]


def uninstall(previous: dict[str, Any]) -> None:
    for name in MODULES:
        old = previous.get(name)
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old
    for name in [m for m in list(sys.modules) if m == "echo_adk.adk" or m.startswith("echo_adk.adk.")]:
        sys.modules.pop(name, None)
