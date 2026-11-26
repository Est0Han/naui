"""GTK key translation and core command adapters, separate from session ownership."""
from gi.repository import Gdk, Gtk, Vte
from .keybindings import Command, EDITOR_COMMANDS


def event_shortcut(key,state):
    modifiers = [label for label,mask in [('Ctrl',Gdk.ModifierType.CONTROL_MASK),('Alt',Gdk.ModifierType.ALT_MASK),
                ('Shift',Gdk.ModifierType.SHIFT_MASK),('Super',Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK)] if state & mask]
    if key == Gdk.KEY_ISO_Left_Tab:
        key = Gdk.KEY_Tab
        if 'Shift' not in modifiers: modifiers.append('Shift')
    # GDK reports shifted punctuation as a distinct keyval. Store its base key.
    char = Gdk.keyval_to_unicode(key)
    char = chr(char) if char else None
    shifted = dict(zip('~!@#$%^&*()_+{}|:"<>?', '`1234567890-=[]\\;\',./'))
    if char in shifted and 'Shift' in modifiers: char = shifted[char]
    aliases = {'space':'Space','Page_Up':'PageUp','Page_Down':'PageDown','KP_Add':'+','KP_Subtract':'-', 'KP_0':'0'}
    name = Gdk.keyval_name(key)
    if name and (name.endswith('_L') or name.endswith('_R')): return None
    from .keybindings import normalize
    try: return normalize('+'.join(modifiers + [char if char and char.isprintable() and char != ' ' else aliases.get(name,name or '')]))
    except ValueError: return None


def focused_context(window):
    focus = window.get_focus()
    right = getattr(window, 'right_panels', None)
    if right and focus and (focus is right or focus.is_ancestor(right)): return 'plugin'
    search = getattr(window, 'search_panel', None)
    if search and focus and (focus is search or focus.is_ancestor(search)):
        return 'search'
    # Text fields in search, path, inline rename etc. retain native editing keys.
    if isinstance(focus,(Gtk.Entry,Gtk.Text)): return 'global'
    editor = window.current_editor()
    if editor and editor.focus_in_view(): return 'editor'
    page = window.notebook.get_nth_page(window.notebook.get_current_page())
    view = getattr(page,'view',None)
    if isinstance(view,Vte.Terminal) and focus and (focus is view or focus.is_ancestor(view)): return 'terminal'
    sidebar = window.workspace_sidebar
    if focus and (focus is sidebar or focus.is_ancestor(sidebar)): return 'file_tree'
    sidebar = getattr(window, 'left_sidebar', None)
    if sidebar and focus and (focus is sidebar or focus.is_ancestor(sidebar)): return 'sidebar'
    if getattr(page,'keybinding_context',None): return page.keybinding_context
    return 'global'


def register_core(window,registry):
    editor = window.current_editor
    def ready():
        p=editor(); return bool(p and p.focus_in_view() and not p.loading)
    def add(identity,title,category,context,default,callback,enabled=lambda:True):
        registry.register(Command(identity,title,category,context,callback,default,enabled,owner='core'))
    for identity,title,default,action in EDITOR_COMMANDS:
        def enabled(action=action):
            if not ready(): return False
            p=editor()
            if action=='toggle_comment': return bool(p.editing.comment)
            return True
        add('editor.'+identity,title,'Editor','editor',default,lambda action=action:editor().command(action),enabled)
    for identity, title, shortcut, action in [
            ('commands', 'Command Palette', 'Ctrl+Shift+P', 'palette'),
            ('run_task', 'Run Project Task', None, 'run_task'),
            ('save_task', 'Save Project Task', None, 'add_task'),
            ('reopen_tab', 'Reopen Closed File', 'Ctrl+Alt+T', 'reopen')]:
        add('development.'+identity, title, 'Development', 'global', shortcut,
            lambda action=action: getattr(window.development, action)())
    add('tabs.recent', 'Switch Recent Tab', 'Tabs', 'global', 'Ctrl+Tab', lambda: window.notebook.recent_tab())
    add('tabs.recent_reverse', 'Switch Recent Tab Backward', 'Tabs', 'global', 'Ctrl+Shift+Tab', lambda: window.notebook.recent_tab(True))
    add('tabs.pin', 'Pin or Unpin Tab', 'Tabs', 'global', None, lambda: window.notebook.toggle_pin())
    add('editor.save','Save','Editor','editor','Ctrl+S',lambda:editor().save(),ready)
    add('editor.undo','Undo','Editor','editor','Ctrl+Z',lambda:editor().buffer.undo(),lambda:ready() and editor().buffer.get_can_undo())
    add('editor.redo','Redo','Editor','editor','Ctrl+Shift+Z',lambda:editor().buffer.redo(),lambda:ready() and editor().buffer.get_can_redo())
    add('editor.dismiss','Dismiss Search','Editor','editor','Escape',lambda:editor().editing.hide_search(),ready)
    for identity,title,default,action in [('definition','Go to Definition','F12','definition'),('references','Find References','Shift+F12','references'),('hover','Show Hover','Ctrl+K','hover')]:
        add('navigation.'+identity,title,'Navigation','editor',default,lambda action=action:window.code_navigation.query(editor(),action),lambda:ready() and not editor().document.large)
    for method, title, shortcut in [('completion','Code Completion','Ctrl+Space'), ('codeAction','Quick Fix','Ctrl+.'), ('formatting','Format Document','Ctrl+Shift+I')]:
        add('navigation.'+method, title, 'Code', 'editor', shortcut,
            lambda method=method: window.code_navigation.assist(editor(), method), ready)
    add('navigation.rename','Rename Symbol','Navigation','editor','F2',lambda:window.code_navigation.rename_dialog(editor()),lambda:ready() and not editor().document.large)
    for identity,title,default,direction in [('back','Navigate Back','Alt+Left',-1),('forward','Navigate Forward','Alt+Right',1)]:
        add('navigation.'+identity,title,'Navigation','editor',default,lambda direction=direction:window.code_navigation.history_move(direction),ready)
    for identity,title,default,method in [('sidebar.toggle','Toggle Sidebar','Ctrl+B','toggle_sidebar'),('terminal.new','New Terminal','Ctrl+Shift+T','new_terminal'),('terminal.close','Close Terminal',None,'close_active_terminal')]:
        add(identity,title,'Terminal' if identity.startswith('terminal') else 'General', 'terminal' if identity=='terminal.close' else 'global',default,lambda method=method:getattr(window,method)())
    for identity, title, shortcut in [('explorer', 'Explorer', 'Ctrl+Shift+E'), ('search', 'Search', 'Ctrl+Shift+F'),
            ('source_control', 'Source Control', 'Ctrl+Shift+G'), ('transfers', 'Transfers', None),
            ('devices', 'Devices', None), ('settings', 'Settings', None)]:
        add('view.' + identity, 'Show ' + title, 'Views', 'global', shortcut,
            lambda identity=identity: window.search_panel.focus_search('content') if identity == 'search' and hasattr(window, 'search_panel') else window.show_sidebar_panel(identity))
    for identity, title, context, shortcut, callback in [
            ('workspace', 'Search Workspace Content', 'global', None, lambda: window.search_panel.focus_search('content')),
            ('files', 'Search Files and Folders', 'global', 'Ctrl+P', lambda: window.search_panel.focus_search('files')),
            ('focus', 'Focus Search Query', 'search', None, lambda: window.search_panel.focus_search()),
            ('cancel', 'Cancel Search', 'search', 'Escape', lambda: window.search_panel.cancel()),
            ('next_result', 'Next Search Result', 'search', None, lambda: window.search_panel.move_result(1)),
            ('previous_result', 'Previous Search Result', 'search', None, lambda: window.search_panel.move_result(-1)),
            ('open_result', 'Open Search Result', 'search', None, lambda: window.search_panel.open_result())]:
        add('search.' + identity, title, 'Search', context, shortcut, callback,
            lambda: hasattr(window, 'search_panel'))
    def replace_search():
        window.search_panel.focus_search('content')
        window.search_panel.replace_expander.set_expanded(True)
        window.search_panel.replace_entry.grab_focus()
    add('search.replace', 'Replace in Workspace', 'Search', 'global', 'Ctrl+Shift+H', replace_search)
    add('view.right_panel', 'Toggle Right Panel', 'Views', 'global', None, lambda: window.right_panels.toggle())
    add('sidebar.focus', 'Focus Sidebar', 'Views', 'global', None,
        lambda: window.show_sidebar_panel(window.left_sidebar.state.active))
    for identity, title, callback in [
            ('split_horizontal', 'Split Side by Side', lambda: window.split_tabs(Gtk.Orientation.HORIZONTAL)),
            ('split_vertical', 'Split Top and Bottom', lambda: window.split_tabs(Gtk.Orientation.VERTICAL)),
            ('move_tab', 'Move Tab to Other Group', lambda: window.move_tab_group()),
            ('single_view', 'Single View', lambda: window.notebook.single_view())]:
        add('view.' + identity, title, 'Views', 'global', None, callback)
    for identity, title, shortcut, direction in [('in', 'Zoom In', 'Ctrl+=', 1), ('in_alt', 'Zoom In (alternate)', 'Ctrl+Shift+=', 1),
            ('out', 'Zoom Out', 'Ctrl+-', -1), ('reset', 'Reset Zoom', 'Ctrl+0', 0)]:
        add('sidebar.zoom_' + identity, 'Sidebar ' + title, 'Views', 'sidebar', shortcut,
            lambda direction=direction: window.left_sidebar.zoom(direction))
    for identity,title,default,direction in [('in_alt','Zoom In (alternate)','Ctrl+Shift+=',1),('in','Zoom In','Ctrl+=',1),('out','Zoom Out','Ctrl+-',-1),('reset','Reset Zoom','Ctrl+0',0)]:
        add('view.zoom_'+identity,title,'General','global',default,lambda direction=direction:window.zoom_active(direction))
    add('terminal.copy','Copy','Terminal','terminal','Ctrl+Shift+C',lambda:window.terminal.view.copy_clipboard_format(Vte.Format.TEXT))
    add('terminal.paste','Paste','Terminal','terminal','Ctrl+Shift+V',lambda:window.terminal.view.paste_clipboard())
    for identity,title,callback in [('refresh','Refresh Files',lambda:window.explorer.refresh()),('new_file','New File',lambda:window.workspace_sidebar.new_item(False)),('new_folder','New Folder',lambda:window.workspace_sidebar.new_item(True))]:
        add('files.'+identity,title,'Files','file_tree',None,callback)

    add('files.focus_path','Focus Path Bar','Files','file_tree',None,lambda:window.explorer.edit_path())
    def selected():
        explorer=window.explorer
        if not explorer.device.connected or explorer.inline is not None: return None
        row=explorer.selection.get_selected_item() if getattr(explorer,'tree_model',None) else None
        node=row.get_item() if row else None
        return node.entry if node and not node.root and not node.inline else None
    def rename():
        import posixpath
        explorer,entry=window.explorer,selected()
        if entry: explorer.name_dialog('Rename',posixpath.dirname(entry.path),explorer.backend,explorer.session,entry)
    def delete():
        explorer,entry=window.explorer,selected()
        if entry: explorer.confirm_delete(entry,explorer.backend,explorer.session)
    add('files.rename','Rename File','Files','file_tree',None,rename,lambda:bool(selected()))
    add('files.delete','Delete File','Files','file_tree',None,delete,lambda:bool(selected()))

    def diff_ready():
        from .diff_view import DiffView
        page = window.notebook.get_nth_page(window.notebook.get_current_page())
        return page if isinstance(page, DiffView) and not page._shutting_down else None
    for identity, title, shortcut, direction in [('next_change', 'Next Diff Change', 'F7', 1), ('previous_change', 'Previous Diff Change', 'Shift+F7', -1)]:
        add('git.' + identity, title, 'Git', 'git', shortcut,
            lambda direction=direction: diff_ready().change(direction), lambda: bool(diff_ready() and diff_ready().hunk_offsets))
