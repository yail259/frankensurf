"""Explicit local adapter plugin registration. No installation during operations."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AdapterManifest:
    id: str
    version: str


@dataclass(frozen=True)
class AdapterRequest:
    content: str
    content_type: str
    url: str
    navigation_data: dict | None = None
    navigation: dict | None = None
    policy: object | None = None
    requested_url: str | None = None
    # Core-owned acquisition metadata. Runtime supplies this only after the
    # acquisition boundary has validated and detached it from provider output.
    acquisition_attestation: object | None = None


class AdapterPlugin(Protocol):
    manifest: AdapterManifest
    def extract(self, request: AdapterRequest) -> dict: ...


class AdapterRegistry:
    def __init__(self):
        self._plugins={}
        self._disabled=set()
        self._frozen=False

    def register(self, plugin: AdapterPlugin):
        if self._frozen: raise RuntimeError("Adapter registry is frozen")
        manifest=plugin.manifest
        if not isinstance(manifest,AdapterManifest) or not manifest.id or not manifest.version or not callable(getattr(plugin,"extract",None)):
            raise ValueError("Invalid adapter plugin contract")
        if manifest.id in self._plugins: raise ValueError("Adapter already registered")
        self._plugins[manifest.id]=plugin
        return manifest

    def clone(self):
        registry = type(self)()
        registry._plugins = self._plugins.copy()
        registry._disabled = self._disabled.copy()
        return registry

    def freeze(self):
        self._frozen = True
        return self

    def contains(self, identifier): return identifier in self._plugins

    def enable(self, identifier, enabled=True):
        if self._frozen: raise RuntimeError("Adapter registry is frozen")
        if identifier not in self._plugins: raise ValueError("Unknown adapter")
        if enabled: self._disabled.discard(identifier)
        else: self._disabled.add(identifier)

    def inspect(self):
        return [{"id": name, "version": plugin.manifest.version,
                 **({"binding_id": plugin.binding_id}
                    if (isinstance(getattr(plugin, "binding_id", None), str)
                        and len(plugin.binding_id) == 64
                        and all(char in "0123456789abcdef"
                                for char in plugin.binding_id))
                    else {}),
                 "enabled": name not in self._disabled}
                for name, plugin in sorted(self._plugins.items())]

    def binding_id(self, identifier):
        plugin = self._plugins.get(identifier)
        value = getattr(plugin, "binding_id", None)
        return (value if isinstance(value, str) and len(value) == 64
                and all(char in "0123456789abcdef" for char in value)
                else None)

    def semantic_capabilities(self):
        """Return a detached primitive snapshot of contract capabilities."""
        rows = []
        for identifier, plugin in sorted(self._plugins.items()):
            capabilities = getattr(plugin, "semantic_capabilities", ())
            if type(capabilities) is not tuple:
                raise ValueError("Invalid adapter semantic capabilities")
            for capability in capabilities:
                rows.append((
                    identifier, plugin.manifest.version,
                    self.binding_id(identifier),
                    identifier not in self._disabled, capability))
        return tuple(rows)

    def require_enabled(self, identifier):
        from .runtime import WebFailure
        if identifier not in self._plugins: raise WebFailure("PLUGIN_DISABLED","Adapter is not registered")
        if identifier in self._disabled: raise WebFailure("PLUGIN_DISABLED","Adapter is disabled")
        return self._plugins[identifier].manifest

    def require_sequence_enabled(self, identifier):
        from .runtime import WebFailure
        manifest = self.require_enabled(identifier)
        if not callable(getattr(self._plugins[identifier], "sequence_pages", None)):
            raise WebFailure("SEQUENCE_UNSUPPORTED", "Adapter does not expose native raw page captures")
        return manifest

    def sequence_pages(self, identifier, request):
        from .runtime import WebFailure
        from .pagination import validate_sequence_pages
        self.require_sequence_enabled(identifier)
        try:
            captures = self._plugins[identifier].sequence_pages(request)
            return validate_sequence_pages(request, captures)
        except WebFailure:
            raise
        except Exception:
            raise WebFailure("SCHEMA_CHANGED", "Invalid native raw page capture protocol") from None

    def project(self, identifier, request):
        from .runtime import WebFailure
        self.require_enabled(identifier)
        try:
            result=self._plugins[identifier].extract(request)
        except WebFailure: raise
        except Exception:
            raise WebFailure("SCHEMA_CHANGED","Adapter plugin failed; inspect locally without exporting exception data") from None
        if not isinstance(result,dict) or not isinstance(result.get("text"),str) or not isinstance(result.get("image_urls"),list) or any(not isinstance(url,str) for url in result["image_urls"]) or "structured" not in result:
            raise WebFailure("SCHEMA_CHANGED","Adapter plugin returned an invalid projection")
        return result


class LegacyAdapter:
    """Generic built-in projections: raw text, JSON-LD, embedded JSON, images."""

    def __init__(self, identifier):
        self.manifest = AdapterManifest(identifier, "legacy")

    def extract(self, request):
        from .runtime import _parse_builtin_content
        return _parse_builtin_content(request.content, request.content_type, request.url, self.manifest.id,
                                      request.navigation_data, request.navigation, policy=request.policy)


DEFAULT_ADAPTERS = AdapterRegistry()
for _identifier in ("html", "json", "rss"):
    DEFAULT_ADAPTERS.register(LegacyAdapter(_identifier))
