"""Bundled LSP protocol, source sessions and GTK editor integration."""
def start(api):
    from .ui import CodeNavigation
    api.register_feature('navigation_factory', CodeNavigation)
