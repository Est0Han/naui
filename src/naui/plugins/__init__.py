"""Default-enabled bundled plugins; imports are lazy for headless core users."""
def bundled_plugins():
    from ..plugin_manager import PluginSpec
    from . import base_file_explorer, base_filepath, base_lsp, python_lsp, base_media, base_runner, base_git
    return (
        (PluginSpec('base-file-explorer', 'File Explorer', frozenset({'filesystem_read', 'filesystem_write', 'filesystem_watch', 'remote_access', 'file_transfer'}), True, True), base_file_explorer.start),
        (PluginSpec('base-filepath', 'Filepath Navigation', frozenset({'filesystem_read', 'editor_navigation', 'remote_access'}), True, True), base_filepath.start),
        (PluginSpec('base-lsp', 'Language Server Protocol', frozenset({'editor_read', 'editor_navigation', 'filesystem_read', 'remote_access'}), True, True), base_lsp.start),
        (PluginSpec('python-lsp', 'Python / Pyright', frozenset({'editor_read', 'filesystem_read', 'remote_access'}), True, True), python_lsp.start),
        (PluginSpec('base-media', 'Media Preview', frozenset({'filesystem_read', 'remote_access'}), True, True), base_media.start),
        (PluginSpec('base-runner', 'File Runner', frozenset({'commands', 'filesystem_read', 'editor_read', 'terminal_execution', 'remote_access'}), True, True), base_runner.start),
        (PluginSpec('base-git', 'Source Control', frozenset({'commands', 'filesystem_write', 'filesystem_read', 'filesystem_watch', 'editor_navigation', 'command_execution', 'views', 'remote_access'}), True, True), base_git.start),
    )
