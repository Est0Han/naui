"""Bundled explorer; device/session/transfer/authentication services remain core."""
def start(api):
    from .workspace_sidebar import WorkspaceSidebar
    api.register_feature('explorer_factory', WorkspaceSidebar)
    api.register_feature('explorer_transfers', api.transfers)
    api.register_feature('explorer_files', api.files)
