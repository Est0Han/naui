"""Stable public Naui Plugin SDK v1. No native widgets or credentials exposed."""
from naui.plugin_api import PluginAPI, DeviceInfo, DocumentInfo
from naui.plugin_manifest import API_VERSION
from naui.transfers import FileLocation


class Plugin:
    """Subclass as class Plugin; implement activate(api) and idempotent deactivate()."""
    def activate(self, api): raise NotImplementedError('Implement activate(api)')
    def deactivate(self): pass


__all__ = ['Plugin', 'PluginAPI', 'API_VERSION', 'FileLocation', 'DeviceInfo', 'DocumentInfo']
