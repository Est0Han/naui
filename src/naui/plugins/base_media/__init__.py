"""Bundled media MIME specs and preview widgets; transport/cache remain core."""
def start(api):
    from ...viewer_registry import DEFAULT_VIEWERS
    from .viewers import AudioViewer, ImageViewer, VideoViewer, UnsupportedViewer
    from .executable_viewer import ExecutableViewer
    from .rpf_viewer import RpfViewer
    from .binary_viewer import BinaryViewer
    api.register_feature('viewer_specs', DEFAULT_VIEWERS)
    api.register_feature('viewer_factories', {'binary': BinaryViewer, 'executable': ExecutableViewer, 'rpf': RpfViewer, 'image': ImageViewer, 'audio': AudioViewer,
                         'video': VideoViewer, 'unsupported': UnsupportedViewer})
