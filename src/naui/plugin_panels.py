"""Core-owned right-hand plugin panels; no native widgets cross the public API."""
from gi.repository import Gtk, Gdk, Gio, Pango
from .conversation_view import ConversationView, validate_messages
from .lifecycle import Signals, Sources
from .plugin_views import SidebarSection

MAX_TEXT = 256 * 1024
MAX_INPUT = 16000


def validate_panel(identity, title, text, placeholder, on_submit, rows, messages=None):
    validate_messages(messages)
    if not isinstance(identity, str) or not 1 <= len(identity) <= 64 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in identity):
        raise ValueError('Panel ID must contain 1–64 ASCII letters, digits, hyphens or underscores')
    for name, value, limit in [('title', title, 200), ('text', text, MAX_TEXT), ('placeholder', placeholder, 200)]:
        if not isinstance(value, str) or '\0' in value or len(value.encode()) > limit:
            raise ValueError('Invalid panel ' + name)
    if not title.strip(): raise ValueError('Panel title is required')
    if on_submit is not None and not callable(on_submit): raise ValueError('Panel submit handler must be callable')
    count, size = 0, 0
    def check(items, depth=0):
        nonlocal count, size
        if not isinstance(items, (list, tuple)) or depth > 4: raise ValueError('Invalid panel rows')
        for row in items:
            count += 1
            if count > 200 or not isinstance(row, dict) or 'text' not in row: raise ValueError('Invalid or excessive panel rows')
            if set(row) - {'text','tooltip','id','header','expanded','children','actions','action_labels','menu','select','input','icon'}: raise ValueError('Unknown panel row field')
            for key in ('text','tooltip','id','icon'):
                if key in row:
                    value = row[key]
                    if not isinstance(value,str) or '\0' in value or len(value.encode()) > 8192: raise ValueError('Invalid panel row text')
                    size += len(value.encode())
            for key in ('header','expanded','action_labels'):
                if key in row and type(row[key]) is not bool: raise ValueError('Invalid panel row flag')
            for key in ('actions','menu'):
                if key in row:
                    if not isinstance(row[key], (list,tuple)) or len(row[key]) > 24: raise ValueError('Invalid panel actions')
                    for action in row[key]:
                        if not isinstance(action,(list,tuple)) or len(action)!=2 or not isinstance(action[0],str) or not 1<=len(action[0])<=120 or '\0' in action[0] or not callable(action[1]): raise ValueError('Invalid panel action')
            if 'select' in row and not callable(row['select']): raise ValueError('Invalid panel selection handler')
            if 'input' in row:
                field = row['input']
                if not isinstance(field,dict) or set(field)-{'value','placeholder','changed'} or not callable(field.get('changed')): raise ValueError('Invalid panel input')
                for key in ('value','placeholder'):
                    value = field.get(key,'')
                    if not isinstance(value,str) or '\0' in value or len(value.encode())>8192: raise ValueError('Invalid panel input text')
                    size += len(value.encode())
            if 'children' in row: check(row['children'],depth+1)
        if size > MAX_TEXT: raise ValueError('Panel rows exceed 256 KiB')
    check(rows)


class PanelToolbar(Gtk.Box):
    """Compact controls; popup contents are built on demand, not on chat updates."""
    def __init__(self, alive):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        self.alive, self.closed = alive, False
        self.controls = {}
        self.sources = Sources()

    def update(self, rows):
        keys = [row.get('id', str(i)) for i,row in enumerate(rows)]
        for key in list(self.controls):
            if key not in keys:
                item=self.controls.pop(key)
                if item.get('section'): item['section'].close()
                self.remove(item['button'])
        for key,row in zip(keys,rows):
            popup = bool(row.get('children') or row.get('menu') or row.get('actions'))
            kind='popup' if popup else 'button' if row.get('select') else 'label'
            item=self.controls.get(key)
            if item and item['kind'] != kind:
                if item.get('section'): item['section'].close()
                self.remove(item['button']); self.controls.pop(key); item=None
            if item is None:
                button=Gtk.MenuButton() if popup else Gtk.Button() if kind=='button' else Gtk.Label()
                item={'button':button,'popup':popup,'kind':kind,'row':row,'section':None}
                self.controls[key]=item
                if popup:
                    button.set_create_popup_func(lambda b, entry=item:self.populate(entry))
                elif kind=='button':
                    def clicked(*_, entry=item):
                        callback=entry['row'].get('select')
                        if callback: self.sources.idle(lambda:callback() if not self.closed and self.alive() else None)
                    button.connect('clicked',clicked)
                self.append(button)
            item['row']=row
            button=item['button']
            if row.get('icon') and kind!='label':
                button.set_icon_name(row['icon'])
                button.update_property([Gtk.AccessibleProperty.LABEL],[row['text']])
            else:
                # MenuButton's internal child is a box, so styling only its
                # immediate child never constrained long model labels.
                label=button if kind=='label' else item.get('label')
                if label is None:
                    label=Gtk.Label()
                    button.set_child(label); item['label']=label
                label.set_text(row['text'][:80])
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_width_chars(1); label.set_max_width_chars(20)
            button.set_hexpand(key=='model')
            button.set_tooltip_text(row.get('tooltip',row['text']))
            button.set_sensitive(True)
        self.set_visible(bool(rows))

    def populate(self, item):
        if self.closed or not self.alive(): return
        if item.get('section'): item['section'].close()
        row=item['row']
        section=SidebarSection(row['text'])
        section.scroll.set_min_content_height(0)
        section.scroll.set_max_content_height(420)
        contents=list(row.get('children',[]))
        contents += [{'text':label,'select':callback} for label,callback in list(row.get('menu',[]))+list(row.get('actions',[]))]
        def wrap(values):
            result=[]
            def action(callback):
                def invoke(*args):
                    popup=item['button'].get_popover()
                    if popup: popup.popdown()
                    callback(*args)
                return invoke
            for value in values:
                value=dict(value)
                for key in ('menu','actions'):
                    if key in value: value[key]=[(label,action(callback)) for label,callback in value[key]]
                if 'select' in value: value['select']=action(value['select'])
                if 'children' in value: value['children']=wrap(value['children'])
                result.append(value)
            return result
        section.update(wrap(contents),lambda:not self.closed and self.alive())
        # Use the generic row body without another expander around it.
        body=section.get_child(); section.set_child(None)
        popover=Gtk.Popover(); popover.set_child(body)
        body.set_size_request(280,-1)
        item['section']=section
        item['button'].set_popover(popover)
        def closed(*_):
            # Recreate from current data next time, preserving open inputs while streaming.
            self.sources.idle(lambda:item['button'].set_popover(None) if not self.closed else None)
        popover.connect('closed',closed)

    def close(self):
        self.closed=True; self.sources.close()
        for item in self.controls.values():
            if item.get('section'): item['section'].close()


class PluginPanel(Gtk.Box):
    def __init__(self, title, alive):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, vexpand=True)
        self.signals, self.sources = Signals(), Sources()
        self.alive, self.closed, self.on_submit = alive, False, None
        self.conversation = ConversationView(lambda: not self.closed and self.alive())
        self.message_state = None
        self.accept_attachments = False
        self.attachments = []
        self.attachment_store = None
        self.attachment_pending = False
        self.chooser = None
        style = Gtk.CssProvider()
        style.load_from_data(b".naui-chat-approval { border: 1px solid #5599cc; border-radius: 8px; padding: 10px; background: #303030; } .naui-chat-user { background: #333333; border-radius: 8px; padding: 10px; } .naui-chat-tool { border-left: 2px solid #666666; padding: 8px; } .naui-chat-notice { opacity: 0.8; } .naui-chat-composer button { min-height: 24px; min-width: 20px; padding: 2px 5px; } .naui-chat-composer flowboxchild { padding: 0; } .naui-chat-composer { border: 1px solid #666666; border-radius: 8px; padding: 6px; margin: 5px; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.style = style
        self.top_toolbar = PanelToolbar(alive)
        self.append(self.top_toolbar)
        self.section = SidebarSection(title)
        self.section.scroll.set_max_content_height(220)
        self.append(self.section)
        self.output = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.output.set_left_margin(8); self.output.set_right_margin(8)
        self.output.add_css_class('naui-code')
        self.output_scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.output_scroll.set_child(self.output); self.append(self.output_scroll)
        self.composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.composer.add_css_class('naui-chat-composer')
        self.hint = Gtk.Label(xalign=0, wrap=True); self.composer.append(self.hint)
        self.input = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.input.update_property([Gtk.AccessibleProperty.LABEL], ['Plugin message'])
        scroll = Gtk.ScrolledWindow(min_content_height=44, max_content_height=120, propagate_natural_height=True)
        scroll.set_child(self.input); self.composer.append(scroll)
        self.attachment_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.composer.append(self.attachment_list)
        self.attach = Gtk.Button(icon_name='list-add-symbolic', tooltip_text='Attach file', halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        self.attach.update_property([Gtk.AccessibleProperty.LABEL], ['Attach file'])
        self.signals.connect(self.attach, 'clicked', self.choose_attachment)
        self.bottom_toolbar = PanelToolbar(alive)
        self.composer_actions = Gtk.Box(spacing=4)
        self.composer_actions.append(self.attach)
        self.bottom_toolbar.set_hexpand(True)
        self.bottom_toolbar.set_valign(Gtk.Align.CENTER)
        self.composer_actions.append(self.bottom_toolbar)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.signals.connect(keys, 'key-pressed', self.paste_image)
        self.input.add_controller(keys)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        self.signals.connect(drop, 'drop', self.drop_files)
        self.composer.add_controller(drop)
        self.error = Gtk.Label(xalign=0, wrap=True); self.composer.append(self.error)
        self.error.set_visible(False)
        self.signals.connect(self.error, 'notify::label', lambda label, *_: label.set_visible(bool(label.get_text())))
        send = self.send = Gtk.Button(icon_name='go-up-symbolic', tooltip_text='Send message', halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        send.update_property([Gtk.AccessibleProperty.LABEL], ['Send message'])
        self.signals.connect(send, 'clicked', lambda *_: self.submit())
        self.composer_actions.append(send)
        self.composer.append(self.composer_actions); self.append(self.composer)
        self.signals.connect(self.input.get_buffer(), 'changed', self.limit_input)

    def store(self):
        if self.attachment_store is None:
            from .chat_attachments import Attachments
            self.attachment_store = Attachments()
        return self.attachment_store

    def show_attachments(self):
        self.attachment_list.set_visible(bool(self.attachments))
        while self.attachment_list.get_first_child():
            self.attachment_list.remove(self.attachment_list.get_first_child())
        for item in self.attachments:
            row = Gtk.Box(spacing=4)
            label = Gtk.Label(label=item['name'], xalign=0, hexpand=True, wrap=True)
            label.set_tooltip_text(str(item['size'])+' bytes · local attachment')
            row.append(label)
            remove = Gtk.Button(icon_name='window-close-symbolic', tooltip_text='Remove attachment')
            def removed(*_, entry=item):
                self.sources.idle(lambda: self.remove_attachment(entry))
            remove.connect('clicked', removed)
            row.append(remove); self.attachment_list.append(row)

    def remove_attachment(self, item):
        if self.closed: return
        if item in self.attachments: self.attachments.remove(item)
        self.show_attachments()

    def add_attachment(self, path):
        if self.closed or not self.accept_attachments: return
        try:
            if len(self.attachments) >= 8: raise ValueError('Attach up to 8 files per message.')
            self.attachments.append(self.store().add_file(path))
            self.error.set_text(''); self.show_attachments()
        except (OSError, ValueError) as error:
            self.error.set_text(str(error) if isinstance(error, ValueError) else 'Could not read this local file.')

    def choose_attachment(self, *_):
        if not self.accept_attachments or self.chooser: return
        chooser = Gtk.FileChooserNative(title='Attach files to your message', transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN, accept_label='Attach', cancel_label='Cancel')
        self.chooser = chooser
        chooser.set_select_multiple(True)
        def selected(dialog, response):
            if not self.closed and response == Gtk.ResponseType.ACCEPT:
                files = dialog.get_files()
                for i in range(files.get_n_items()):
                    path = files.get_item(i).get_path()
                    if path: self.add_attachment(path)
            dialog.destroy(); self.chooser = None
        chooser.connect('response', selected); chooser.show()

    def drop_files(self, target, files, x, y):
        if not self.accept_attachments: return False
        for file in files.get_files():
            path = file.get_path()
            if path: self.add_attachment(path)
        return True

    def paste_image(self, controller, key, keycode, state):
        if not self.accept_attachments or key not in (Gdk.KEY_v, Gdk.KEY_V) or not state & Gdk.ModifierType.CONTROL_MASK:
            return False
        clipboard = self.input.get_clipboard()
        formats = clipboard.get_formats()
        if (formats.contain_gtype(Gdk.Texture) or
                any(t.is_a(Gdk.Texture.__gtype__) for t in formats.get_gtypes()) or
                any(m in formats.get_mime_types() for m in ('image/png', 'image/jpeg', 'image/tiff', 'image/bmp', 'image/webp'))):
            if self.attachment_pending: return True
            self.attachment_pending = True
            def received(source, result):
                self.attachment_pending = False
                if self.closed: return
                try:
                    texture = source.read_texture_finish(result)
                    if texture.get_width()*texture.get_height() > 24000000:
                        raise ValueError('Image is too large. Resize it below 24 megapixels.')
                    if len(self.attachments) >= 8: raise ValueError('Attach up to 8 files per message.')
                    data = texture.save_to_png_bytes().get_data()
                    self.attachments.append(self.store().add_bytes('pasted-image.png', data))
                    self.show_attachments(); self.error.set_text('')
                except Exception as error:
                    self.error.set_text(str(error) if isinstance(error, ValueError) else 'Could not paste the image. Use Attach file instead.')
            clipboard.read_texture_async(None, received)
            return True
        return False

    def limit_input(self, buffer):
        if self.closed: return
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        if len(text.encode()) <= MAX_INPUT:
            self.error.set_text(''); return
        self.error.set_text('Long text will be attached as a text file when you send.' if self.accept_attachments else
                            'This provider accepts up to 16 KB of text. Your draft has been kept.')

    def update(self, title, rows, text, on_submit, placeholder, messages=None, accept_attachments=False, header_rows=(), composer_rows=()):
        self.top_toolbar.update(header_rows)
        self.bottom_toolbar.update(composer_rows)
        self.accept_attachments = accept_attachments
        self.attach.set_visible(accept_attachments)
        self.attachment_list.set_visible(bool(self.attachments))
        self.section.heading.set_text(title)
        self.section.update(rows, self.alive); self.section.set_visible(bool(rows))
        buffer = self.output.get_buffer()
        old = buffer.get_text(buffer.get_start_iter(),buffer.get_end_iter(),True)
        changed_messages = self.message_state != messages
        if changed_messages:
            self.message_state = [dict(m) for m in messages] if messages is not None else None
        if old != text or changed_messages:
            adjustment = self.output_scroll.get_vadjustment()
            position = adjustment.get_value()
            follow = position >= adjustment.get_upper() - adjustment.get_page_size() - 24
            buffer.set_text(text)
            if messages is not None:
                if self.conversation.get_parent() is None:
                    self.output_scroll.set_child(self.conversation)
                self.conversation.update(messages)
            else:
                if self.output.get_parent() is None:
                    self.output_scroll.set_child(self.output)
            self.sources.remove(getattr(self, 'scroll_source', None))
            def restore_scroll():
                if not self.closed:
                    adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size())
                                         if follow else position)
            self.scroll_source = self.sources.timeout(30, restore_scroll)
        self.on_submit = on_submit
        self.hint.set_text(placeholder); self.composer.set_visible(on_submit is not None or messages is not None)
        self.send.set_sensitive(on_submit is not None)
        # Updating a transcript never replaces the user's unsent draft or focus.

    def submit(self):
        if self.closed or not self.alive() or self.on_submit is None: return
        if self.attachment_pending:
            self.error.set_text('Wait for the pasted image to finish loading.'); return
        buffer = self.input.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(),buffer.get_end_iter(),True)
        if not text.strip() and not self.attachments: return
        if '\0' in text:
            self.error.set_text('This paste contains binary data. Attach it as a file instead. Your draft has been kept.'); return
        if self.attachments and not self.accept_attachments:
            self.error.set_text('The current panel does not accept attachments. Switch back to an attachment-enabled view to send them.'); return
        if len(text.encode()) > (12000 if self.attachments else MAX_INPUT):
            if not self.accept_attachments:
                self.limit_input(buffer); return
            try:
                if len(self.attachments) >= 8: raise ValueError('Remove an attachment before sending long text.')
                self.attachments.append(self.store().add_bytes('pasted-text.txt', text.encode()))
                self.show_attachments()
            except ValueError as error:
                self.error.set_text(str(error)); return
            text = 'Please read and respond to the attached pasted-text.txt.'
        callback = self.on_submit
        payload = {'text':text, 'attachments':[dict(a) for a in self.attachments]} if self.attachments else text
        sent = list(self.attachments)
        original = buffer.get_text(buffer.get_start_iter(),buffer.get_end_iter(),True)
        self.error.set_text('')
        buffer.set_text(''); self.attachments = []; self.show_attachments()
        def deliver():
            if self.closed or not self.alive(): return
            accepted = callback(payload)
            if accepted is False and not buffer.get_char_count():
                buffer.set_text(original); self.attachments = sent + self.attachments; self.show_attachments()
        self.sources.idle(deliver)

    def close(self):
        if self.closed: return
        self.closed = True
        self.top_toolbar.close(); self.bottom_toolbar.close()
        self.sources.close(); self.signals.close(); self.section.close(); self.conversation.close()
        if self.chooser: self.chooser.destroy(); self.chooser = None
        if self.attachment_store: self.attachment_store.close()
        self.on_submit = None
        Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), self.style)


class RightPanelHost(Gtk.Box):
    def __init__(self, window, pane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=False, vexpand=True)
        self.window, self.pane = window, pane
        self.signals, self.sources = Signals(), Sources()
        self.panels, self.order = {}, []
        self.closed, self.applying, self.extent = False, False, 0
        self.choosing = False
        self.titles = {}
        header = Gtk.Box(spacing=4)
        self.choice = Gtk.ComboBoxText(hexpand=True)
        self.choice.set_tooltip_text('Right-side plugin panels')
        header.append(self.choice)
        hide = Gtk.Button(icon_name='window-close-symbolic', tooltip_text='Hide right panel')
        self.signals.connect(hide,'clicked',lambda *_: self.hide_panel())
        header.append(hide); self.append(header)
        self.stack = Gtk.Stack(hexpand=True,vexpand=True,hhomogeneous=False,vhomogeneous=False)
        self.append(self.stack)
        self.signals.connect(self.choice,'changed',self.selected)
        self.signals.connect(pane,'notify::max-position',self.restore_geometry)
        self.signals.connect(pane,'notify::position',self.resized)
        self.set_visible(False)

    @staticmethod
    def key(plugin_id, identity): return plugin_id + ':' + identity

    def publish(self, plugin_id, identity, title, rows, text, on_submit, placeholder, alive, messages=None, accept_attachments=False, header_rows=(), composer_rows=()):
        key = self.key(plugin_id,identity)
        if key not in self.panels:
            if len(self.panels)>=32 or sum(k.startswith(plugin_id+':') for k in self.panels)>=8:
                raise ValueError('Right panel limit reached (8 per plugin, 32 total)')
            panel = PluginPanel(title,alive)
            self.panels[key] = panel
            self.stack.add_named(panel,key)
            self.order.append(key)
            self.choice.append(key, title + ' · ' + plugin_id)
            self.titles[key] = title
            preferred = self.window.ui_settings.values['right_panel_selected']
            if len(self.order)==1 or preferred==key:
                self.choosing = bool(preferred and preferred != key)
                try: self.choice.set_active_id(key)
                finally: self.choosing = False
        if self.titles.get(key) != title:
            active = self.choice.get_active_id()
            self.choosing = True
            try:
                index = self.order.index(key)
                self.choice.remove(index); self.choice.insert(index, key, title + ' · ' + plugin_id)
                self.choice.set_active_id(active)
                self.titles[key] = title
            finally: self.choosing = False
        panel = self.panels[key]
        panel.update(title,rows,text,on_submit,placeholder,messages,accept_attachments,header_rows,composer_rows)
        self.set_visible(not self.window.ui_settings.values['right_panel_collapsed'])
        self.restore_geometry()

    def selected(self, *_):
        key = self.choice.get_active_id()
        if key not in self.panels: return
        self.stack.set_visible_child_name(key)
        if not self.choosing:
            self.window.ui_settings.values['right_panel_selected'] = key
            self.persist()

    def focus_panel(self, plugin_id, identity):
        key = self.key(plugin_id,identity)
        if key not in self.panels: raise ValueError('Panel has not been created')
        self.window.ui_settings.values['right_panel_collapsed'] = False
        self.choice.set_active_id(key); self.set_visible(True); self.restore_geometry()
        panel = self.panels[key]
        (panel.input if panel.on_submit else panel.output).grab_focus()
        self.persist()

    def hide_panel(self):
        self.window.ui_settings.values['right_panel_collapsed'] = True
        self.set_visible(False); self.persist()

    def toggle(self):
        if not self.panels:
            self.window.set_status('No plugin has created a right-side panel yet.'); return
        if self.get_visible(): self.hide_panel()
        else:
            key = self.choice.get_active_id() or self.order[0]
            self.focus_panel(*key.split(':',1))

    def restore_geometry(self, *_):
        if self.closed or self.applying: return
        self.applying = True
        try:
            self.set_visible(bool(self.panels) and not self.window.ui_settings.values['right_panel_collapsed'])
            self.extent = self.pane.get_width()
            if self.get_visible() and self.extent > 0:
                width = min(self.window.ui_settings.values['right_panel_width'], max(180,self.extent-240))
                self.pane.set_position(max(0,self.extent-width))
        finally: self.applying = False

    def resized(self, *_):
        if self.closed or self.applying or not self.get_visible() or self.extent != self.pane.get_width(): return
        width = self.extent - self.pane.get_position()
        if width>0:
            self.window.ui_settings.values['right_panel_width'] = max(180,min(800,width))
            self.persist()

    def persist(self):
        self.sources.remove(getattr(self,'save_source',None))
        self.save_source = self.sources.timeout(400,self.window.save_ui)

    def remove(self, plugin_id, identity):
        key = self.key(plugin_id,identity)
        panel = self.panels.pop(key,None)
        if panel is None: return
        panel.close()
        self.titles.pop(key, None)
        # Clear native focus before detaching a widget that may own it.
        focus = self.window.get_focus()
        if focus and (focus is panel or focus.is_ancestor(panel)): self.window.set_focus(None)
        index = self.order.index(key); self.order.remove(key)
        self.stack.remove(panel); self.choice.remove(index)
        if self.order and self.choice.get_active_id() not in self.panels: self.choice.set_active(0)
        if not self.panels: self.set_visible(False)

    def remove_plugin(self, plugin_id):
        for key in list(self.panels):
            owner, identity = key.split(':',1)
            if owner == plugin_id: self.remove(owner,identity)

    def apply_layout(self):
        key = self.window.ui_settings.values['right_panel_selected']
        if key in self.panels: self.choice.set_active_id(key)
        self.restore_geometry()

    def close(self):
        if self.closed: return
        self.closed = True
        self.sources.close(); self.signals.close()
        for panel in self.panels.values(): panel.close()
