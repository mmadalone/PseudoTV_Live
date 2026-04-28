# Test-only stub for script.module.infotagger's ListItemInfoTag.
# Our addon code only instantiates this class (`ListItemInfoTag(liz, 'video')`);
# the __getattr__ no-op fallback handles any method calls without breaking imports.
class ListItemInfoTag:
    def __init__(self, listitem, media_type='video'):
        self.listitem = listitem
        self.media_type = media_type

    def __getattr__(self, name):
        # Any other method becomes a no-op so module/class import doesn't break.
        return lambda *a, **kw: None
