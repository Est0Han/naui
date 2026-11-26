"""Extract one reviewed change block without applying unrelated file metadata."""

def select_hunk(patch, index):
    lines = patch.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith('@@ ')]
    if not starts or type(index) is not int or not 0 <= index < len(starts):
        raise ValueError('Choose a change with Previous Change or Next Change first.')
    header = lines[:starts[0]]
    if sum(line.startswith('diff --git ') for line in lines) != 1 or any(
            line.startswith(('new file ', 'deleted file ', 'rename ', 'copy ', 'old mode ', 'new mode ', 'GIT binary')) for line in header):
        raise ValueError('Use Stage File for new, deleted, renamed, binary, or mode-changing files.')
    if not any(line.startswith('--- a/') for line in header) or not any(line.startswith('+++ b/') for line in header):
        raise ValueError('This file requires whole-file staging.')
    end = starts[index+1] if index+1 < len(starts) else len(lines)
    return ''.join(header + lines[starts[index]:end])
