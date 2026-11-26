"""Pure editor conflict state. Never replaces an editor buffer automatically."""
from dataclasses import dataclass


@dataclass
class ExternalChange:
    snapshot: object = None
    deleted: bool = False

    def observe(self, revision, snapshot):
        if snapshot.revision == revision:
            self.snapshot, self.deleted = None, False
            return False
        self.snapshot, self.deleted = snapshot, snapshot.revision == 'missing'
        return True

    def message(self, dirty):
        if self.deleted: return 'File deleted externally. Editor contents preserved; use Save a copy or Close.'
        if dirty: return 'File changed externally while you have unsaved changes. Compare before Reload or Keep My Version.'
        return 'File changed externally. Reload or View Changes; current contents are preserved.'

    def clear(self):
        self.snapshot, self.deleted = None, False
