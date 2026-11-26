"""Pure line editing plans: no widget, filesystem, shell or LSP ownership."""
from dataclasses import dataclass

LINE_COMMANDS = frozenset({'toggle_comment', 'indent', 'outdent', 'move_up',
                           'move_down', 'duplicate', 'delete_line'})


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    replacement: str
    anchor: int
    cursor: int

    def apply(self, text): return text[:self.start] + self.replacement + text[self.end:]


def line_span(text, anchor, cursor):
    if not (0 <= anchor <= len(text) and 0 <= cursor <= len(text)):
        raise ValueError('Selection outside document')
    low, high = sorted((anchor, cursor))
    start = text.rfind('\n', 0, low) + 1
    # An endpoint at the next line's column zero does not select that line.
    probe = high - 1 if high > low and text[high - 1:high] == '\n' else high
    end = text.find('\n', probe)
    return start, len(text) if end == -1 else end + 1


def _selection(anchor, cursor, start, end, caret=None):
    if anchor == cursor:
        point = start if caret is None else caret
        return point, point
    return (start, end) if anchor < cursor else (end, start)


def line_edit(command, text, anchor, cursor, *, comment=None, width=4, spaces=True, tab_width=4):
    if command not in LINE_COMMANDS:
        raise ValueError('Unknown editor command: ' + command)
    if not 1 <= width <= 16 or not 1 <= tab_width <= 16:
        raise ValueError('Invalid indentation width')
    start, end = line_span(text, anchor, cursor)
    selected = text[start:end]
    lines = selected.splitlines(keepends=True) or ['']
    caret = cursor
    if command in {'move_up', 'move_down'}:
        if command == 'move_up':
            if start == 0: return None
            before = text.rfind('\n', 0, start - 1) + 1
            other = text[before:start]
            # Transfer the separating newline when the final line lacks one.
            eol = '\r\n' if other.endswith('\r\n') else '\n'
            replacement = selected + other if selected.endswith('\n') else selected + eol + other[:-len(eol)]
            shift = -(start - before)
            a, c = anchor + shift, cursor + shift
            if anchor != cursor and max(anchor, cursor) == end and not selected.endswith('\n'):
                if a > c: a += len(eol)
                else: c += len(eol)
            return Edit(before, end, replacement, a, c)
        if end >= len(text): return None
        following = text.find('\n', end)
        after = len(text) if following == -1 else following + 1
        other = text[end:after]
        eol = '\r\n' if selected.endswith('\r\n') else '\n'
        replacement = other + selected if other.endswith('\n') else other + eol + selected[:-len(eol)]
        shift = len(other) if other.endswith('\n') else len(other) + len(eol)
        a, c = anchor + shift, cursor + shift
        if anchor != cursor and max(anchor, cursor) == end and not other.endswith('\n'):
            if a > c: a -= len(eol)
            else: c -= len(eol)
        return Edit(start, after, replacement, a, c)
    if command == 'duplicate':
        separator = '' if selected.endswith('\n') else ('\r\n' if '\r\n' in text else '\n')
        replacement = selected + separator + selected
        a, c = anchor + len(selected) + len(separator), cursor + len(selected) + len(separator)
        return Edit(start, end, replacement, a, c)
    if command == 'delete_line':
        if end == len(text) and start and not selected.endswith('\n'):
            start -= 2 if text[:start].endswith('\r\n') else 1
        return Edit(start, end, '', start, start)
    if command == 'toggle_comment':
        if not comment or not comment[0]: return None
        prefix, suffix = comment
        nonblank = [line.strip() for line in lines if line.strip()]
        if not nonblank: return None
        uncomment = all(line.startswith(prefix) and (not suffix or line.endswith(suffix)) for line in nonblank)
    transformed = []
    for number, line in enumerate(lines):
        old = line
        leading = len(line) - len(line.lstrip(' \t'))
        if command in {'indent','outdent'}:
            visual = 0
            for char in line[:leading]:
                visual += tab_width - visual % tab_width if char == '\t' else 1
            target = visual + width if command == 'indent' else max(0,visual-width)
            prefix = ' '*target if spaces else '\t'*(target//tab_width) + ' '*(target%tab_width)
            line = prefix + line[leading:]
        elif line.strip():
            eol = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
            body = line[leading:len(line)-len(eol) if eol else len(line)]
            if uncomment:
                body = body[len(prefix):]
                if body.startswith(' '): body = body[1:]
                if suffix:
                    trimmed = body.rstrip(' \t')
                    trailing = body[len(trimmed):]
                    trimmed = trimmed[:-len(suffix)]
                    if trimmed.endswith(' '): trimmed = trimmed[:-1]
                    body = trimmed + trailing
            else: body = prefix + ' ' + body + (' ' + suffix if suffix else '')
            line = line[:leading] + body + eol
        if number == 0 and anchor == cursor:
            caret = max(start, cursor + len(line) - len(old))
            caret = min(caret, start + len(line.rstrip('\r\n')))
        transformed.append(line)
    replacement = ''.join(transformed)
    if replacement == selected: return None
    a, c = _selection(anchor, cursor, start, start + len(replacement), caret)
    return Edit(start, end, replacement, a, c)


def shortcut(key, *, control=False, alt=False, shift=False):
    """Compatibility default lookup; runtime input uses the user's registry."""
    from .keybindings import EDITOR_COMMANDS, normalize
    if key == 'ISO_Left_Tab': key,shift='Tab',True
    try: value=normalize('+'.join([name for name,on in [('Ctrl',control),('Alt',alt),('Shift',shift)] if on]+[key]))
    except ValueError: return None
    return next((action for identity,title,default,action in EDITOR_COMMANDS if default==value),None)
