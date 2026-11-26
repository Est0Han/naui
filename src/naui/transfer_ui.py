"""GTK transfer queue, native DND tokens and core-owned askpass prompts.

A 200 ms main-loop poll consumes only plain core snapshots. Worker threads
never retain widgets or register callbacks themselves. DND carries a short-lived
opaque application token, never executable paths or a remote credential.
"""
import secrets
import time

from gi.repository import Gdk, Gtk, Pango

from .lifecycle import Signals
from .transfers import FileLocation, FINAL_STATES


def human_bytes(value):
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.1f} {unit}'
        size /= 1024


class DragBroker:
    def __init__(self):
        self.tokens = {}

    def create(self, location):
        now = time.monotonic()
        self.tokens = {key: value for key, value in self.tokens.items() if value[1] > now}
        if len(self.tokens) >= 256:
            self.tokens.pop(next(iter(self.tokens)))
        token = 'naui-file:' + secrets.token_hex(24)
        self.tokens[token] = location, now + 600
        return token

    def consume(self, token):
        if not isinstance(token, str) or len(token) > 128:
            return None
        value = self.tokens.pop(token, None)
        return value[0] if value and value[1] > time.monotonic() else None

    def discard(self, token):
        self.tokens.pop(token, None)

    def close(self):
        self.tokens.clear()


class TransferPanel(Gtk.Expander):
    def __init__(self, window):
        super().__init__()
        from .plugin_views import compact_label
        self.heading = compact_label('TRANSFERS')
        self.set_label_widget(self.heading)
        self.set_vexpand(True)
        self.window, self.closed = window, False
        self.signals = Signals()
        self.rows, self.last_states = {}, {}
        self.auth_dialog = self.conflict_dialog = None
        self.drag_broker = DragBroker()
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        clear = Gtk.Button(label='Clear finished transfers')
        self.signals.connect(clear, 'clicked', lambda *_: window.transfers.clear_finished() if not self.closed else None)
        clear.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        clear.get_child().set_width_chars(1); clear.get_child().set_max_width_chars(18)
        clear.set_tooltip_text('Clear finished transfers')
        self.box.append(clear)
        self.empty = Gtk.Label(label='No active transfers', xalign=0)
        self.box.append(self.empty)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(70)
        scroll.set_vexpand(True)
        scroll.set_child(self.box)
        self.set_child(scroll)
        self.timer = window.sources.timeout(200, self.poll)

    def attach_row(self, widget, item, explorer, signals):
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        source._token = None
        def prepare(controller, x, y):
            if self.closed or explorer._shutting_down or not explorer.device.connected:
                return None
            row = item.get_item()
            if row is None:
                return None
            node = row.get_item()
            if node.inline or node.entry.kind not in {'f', 'd'}:
                return None
            token = self.drag_broker.create(FileLocation(explorer.device.id, node.entry.path))
            controller._token = token
            return Gdk.ContentProvider.new_for_value(token)
        signals.connect(source, 'prepare', prepare)
        signals.connect(source, 'drag-end', lambda controller, *_: self.drag_broker.discard(controller._token))
        widget.add_controller(source)
        target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY)
        def drop(controller, token, x, y):
            if self.closed or explorer._shutting_down or not explorer.device.connected:
                return False
            row = item.get_item()
            if row is None:
                return False
            node = row.get_item()
            if node.inline or node.entry.kind != 'd':
                return False
            location = self.drag_broker.consume(token)
            if not location:
                return False
            import posixpath
            destination = FileLocation(explorer.device.id, posixpath.join(node.entry.path, posixpath.basename(location.path)))
            try:
                # The bundled explorer receives the same permission-scoped API
                # third-party plugins use, never a low-level stream/backend.
                self.window.plugins.features['explorer_transfers'].copy(location, destination)
                self.set_expanded(True)
                return True
            except (ValueError, OSError, RuntimeError, PermissionError) as error:
                self.window.set_status(str(error))
                return False
        signals.connect(target, 'drop', drop)
        widget.add_controller(target)

    def poll(self):
        self.timer = None
        if self.closed or self.window._shutting_down:
            return
        if self.auth_dialog and self.auth_dialog[1] not in self.window.authentication.pending():
            self.auth_dialog[0].response(Gtk.ResponseType.CANCEL)
        if self.conflict_dialog and self.conflict_dialog[1] not in self.window.transfers.conflicts():
            self.conflict_dialog[0].response(Gtk.ResponseType.CANCEL)
        snapshots = self.window.transfers.status()
        current = {item['id'] for item in snapshots}
        for identity in set(self.rows) - current:
            text, bar, buttons = self.rows.pop(identity)
            for button in buttons.values():
                self.signals.disconnect(button)
            self.box.remove(text.get_parent())
            self.last_states.pop(identity, None)
        active = sum(item['state'] not in FINAL_STATES for item in snapshots)
        self.heading.set_text(f'TRANSFERS · {active} active')
        self.empty.set_visible(not snapshots)
        from .sidebar_layout import transfer_badge
        if hasattr(self.window, 'left_sidebar'):
            count, failed = transfer_badge(snapshots)
            self.window.left_sidebar.set_badge('transfers', count, failed)
        for item in snapshots:
            identity, state = item['id'], item['state']
            if identity in self.rows and state in FINAL_STATES and self.last_states.get(identity) == state:
                continue  # Finished rows do not need repeated GTK text/progress updates.
            if identity not in self.rows:
                row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                text = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True, width_chars=1, max_width_chars=20)
                bar = Gtk.ProgressBar()
                controls = Gtk.Box(spacing=4)
                buttons = {}
                for name, action in [('Pause', self.window.transfers.pause), ('Resume', self.window.transfers.resume),
                                     ('Cancel', self.window.transfers.cancel), ('Retry', self.window.transfers.retry)]:
                    button = Gtk.Button(label=name)
                    self.signals.connect(button, 'clicked', lambda *_, action=action, identity=identity: self.control(action, identity))
                    controls.append(button)
                    buttons[name] = button
                for widget in (text, bar, controls):
                    row.append(widget)
                self.box.append(row)
                self.rows[identity] = (text, bar, buttons)
            text, bar, buttons = self.rows[identity]
            source, destination = item['source'], item['destination']
            devices = self.window.workspace.devices
            description = (f"{devices[source['device_id']].display_name}:{source['path']} → "
                           f"{devices[destination['device_id']].display_name}:{destination['path']}\n"
                           f"{state} · {human_bytes(item['bytes_transferred'])} / "
                           + (human_bytes(item['size']) if item['total_known'] else 'enumerating')
                           + f" · {human_bytes(item['speed'])}/s · {item['files_completed']} file(s)")
            if item['eta'] is not None and state == 'running':
                description += f" · ETA {max(0, int(item['eta']))}s"
            if item['skipped']:
                description += f" · skipped {item['skipped']} symlink/special item(s)"
            if item['error']:
                description += '\n' + item['error']
            text.set_text(description)
            if item['total_known']:
                bar.set_fraction(min(1, item['bytes_transferred'] / max(1, item['size'])) if state != 'completed' else 1)
            elif state not in FINAL_STATES:
                bar.pulse()
            buttons['Pause'].set_sensitive(state == 'running')
            buttons['Resume'].set_sensitive(item['paused'])
            buttons['Cancel'].set_sensitive(state not in FINAL_STATES)
            buttons['Retry'].set_sensitive(state in {'failed', 'cancelled'})
            for name, visible in [('Pause', state == 'running' and not item['paused']), ('Resume', item['paused']),
                                  ('Cancel', state not in FINAL_STATES), ('Retry', state in {'failed', 'cancelled'})]:
                buttons[name].set_visible(visible)
            if state == 'completed' and self.last_states.get(identity) != state:
                explorer = self.window.workspace_sidebar.explorers.get(destination['device_id'])
                if explorer:
                    explorer.refresh()
            self.last_states[identity] = state
        if self.window.dialog is None:
            pending = self.window.authentication.pending()
            conflicts = self.window.transfers.conflicts()
            if pending:
                self.show_auth(pending[0])
            elif conflicts:
                self.show_conflict(conflicts[0])
        self.timer = self.window.sources.timeout(200, self.poll)

    def control(self, action, identity):
        if self.closed:
            return
        try:
            action(identity)
        except (ValueError, RuntimeError, KeyError) as error:
            self.window.set_status(str(error))

    def _dialog(self, title):
        dialog = Gtk.Dialog(title=title, transient_for=self.window, modal=True)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        area = dialog.get_content_area()
        area.set_spacing(8)
        for edge in ('top', 'bottom', 'start', 'end'):
            getattr(area, 'set_margin_' + edge)(12)
        self.window.dialog = dialog
        return dialog, area

    def _release_dialog(self, dialog):
        if self.window.dialog is dialog:
            self.window.dialog = None
        self.window.signals.disconnect(dialog)
        dialog.destroy()

    def show_auth(self, request):
        device = self.window.workspace.devices.get(request.device_id)
        if not device:
            self.window.authentication.answer(request)
            return
        dialog, area = self._dialog('OpenSSH host confirmation' if request.confirmation else 'Authentication required')
        self.auth_dialog = (dialog, request)
        area.append(Gtk.Label(label=f'Device: {device.display_name}\nHost: {device.hostname}\nUser: {device.username or "OpenSSH config"}', xalign=0, wrap=True))
        area.append(Gtk.Label(label=request.prompt, xalign=0, wrap=True, selectable=True))
        password = Gtk.PasswordEntry()
        password.set_show_peek_icon(True)
        remember = Gtk.CheckButton(label='Remember for this Naui session')
        if not request.confirmation:
            area.append(password)
            area.append(remember)
        dialog.add_button('Trust this host' if request.confirmation else 'Continue', Gtk.ResponseType.ACCEPT)
        def response(dialog, answer):
            if self.closed:
                return
            value = ('yes' if request.confirmation else password.get_text()) if answer == Gtk.ResponseType.ACCEPT else None
            self.window.authentication.answer(request, value, remember.get_active())
            password.set_text('')
            self.auth_dialog = None
            self._release_dialog(dialog)
        self.window.signals.connect(dialog, 'response', response)
        dialog.present()
        if not request.confirmation:
            password.grab_focus()

    def show_conflict(self, conflict):
        dialog, area = self._dialog('Transfer destination already exists')
        self.conflict_dialog = (dialog, conflict)
        devices = self.window.workspace.devices
        area.append(Gtk.Label(label=f'Source: {devices[conflict.source.device_id].display_name}:{conflict.source.path}\n'
                              f'Size: {human_bytes(conflict.source_size)}\n'
                              f'Destination: {devices[conflict.destination.device_id].display_name}:{conflict.destination.path}\n'
                              f'Size: {human_bytes(conflict.destination_size)}', xalign=0, wrap=True, selectable=True))
        responses = {Gtk.ResponseType.CANCEL: 'cancel'}
        for number, choice in enumerate(conflict.choices[1:], 1):
            dialog.add_button({'keep_both': 'Keep Both', 'replace': 'Replace File', 'merge': 'Merge Folders'}[choice], number)
            responses[number] = choice
        def response(dialog, answer):
            if self.closed:
                return
            try:
                self.window.transfers.resolve_conflict(conflict.job_id, responses.get(answer, 'cancel'))
            except ValueError:
                pass
            self.conflict_dialog = None
            self._release_dialog(dialog)
        self.window.signals.connect(dialog, 'response', response)
        dialog.present()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.window.sources.remove(self.timer)
        self.signals.close()
        self.drag_broker.close()
        self.rows.clear()
        self.last_states.clear()
        self.auth_dialog = self.conflict_dialog = None
        self.window = None
