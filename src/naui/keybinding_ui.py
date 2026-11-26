"""Core-owned keybinding settings tab, capture and reviewed profile import."""
from gi.repository import Gtk, Gdk, Pango
from .lifecycle import Signals, Sources
from .keybindings import Conflict, RESERVED_TERMINAL
from .keybinding_host import event_shortcut


class KeybindingsView(Gtk.Box):
    keybinding_context = 'global'
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.window, self.registry = window, window.command_registry
        self.signals, self.rows_signals, self.sources = Signals(), Signals(), Sources()
        self.dialogs, self._shutting_down = set(), False
        self.dialog_signals = {}
        self.path, self.backend, self.session, self.device_id = 'Keybindings', None, None, 'local'
        self.label = Gtk.Label(label='Keybindings')
        self.search = Gtk.SearchEntry(placeholder_text='Search name, command ID, shortcut or category')
        self.append(self.search)
        self.signals.connect(self.search,'search-changed',lambda *_:self.refresh())
        actions=Gtk.Box(spacing=6)
        for title, callback in [('Import…',lambda:self.choose_file(False)),('Export…',lambda:self.choose_file(True)),('Reset All…',self.reset_all)]:
            button=Gtk.Button(label=title);actions.append(button)
            self.signals.connect(button,'clicked',lambda *_,callback=callback:callback())
        self.append(actions)
        self.status = Gtk.Label(xalign=0,wrap=True)
        self.append(self.status)
        self.status.set_text('; '.join(self.registry.warnings) or 'Focused context takes precedence over Global. Terminal Ctrl+C/D/Z always reach the shell.')
        self.rows=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4)
        scroll=Gtk.ScrolledWindow(vexpand=True);scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.rows);self.append(scroll)
        self.refresh_source = None
        self.listener = self.schedule_refresh
        self.registry.listeners.append(self.listener)
        self.refresh()

    def schedule_refresh(self):
        if self._shutting_down or self.refresh_source is not None: return
        def refresh():
            self.refresh_source=None
            self.refresh()
        self.refresh_source=self.sources.idle(refresh)

    def refresh(self):
        if self._shutting_down:return
        self.rows_signals.close(); self.rows_signals=Signals()
        while self.rows.get_first_child():self.rows.remove(self.rows.get_first_child())
        query=self.search.get_text().lower();category=None
        for command in sorted(self.registry.commands.values(),key=lambda c:(c.category,c.title,c.id)):
            binding=self.registry.binding(command.id)
            requested=self.registry.requested(command.id)
            if query not in ' '.join([command.title,command.id,command.category,requested or '',command.context]).lower():continue
            if command.category!=category:
                category=command.category;self.rows.append(Gtk.Label(label=category,xalign=0))
            row=Gtk.Box(spacing=6)
            label=Gtk.Label(label=command.title,xalign=0,hexpand=True,ellipsize=Pango.EllipsizeMode.END)
            label.set_tooltip_text(command.id+' · '+command.context)
            row.append(label)
            context_label=Gtk.Label(label=command.context)
            context_label.add_css_class('dim-label');row.append(context_label)
            text=binding or ('Conflict — unbound' if requested else 'Unassigned')
            if command.id in self.registry.overrides:text+=' · Modified'
            button=Gtk.Button(label=text);row.append(button)
            self.rows_signals.connect(button,'clicked',lambda *_,identity=command.id:self.capture(identity))
            reset=Gtk.Button.new_from_icon_name('edit-undo-symbolic');reset.set_tooltip_text('Reset to Default')
            row.append(reset);self.rows_signals.connect(reset,'clicked',lambda *_,identity=command.id:self.reset(identity))
            self.rows.append(row)

    def reset(self,identity):
        try:self.registry.reset(identity)
        except Conflict as error:
            default=self.registry.commands[identity].default
            self.window.confirm_action('Shortcut conflict',str(error),'Replace Existing',
                lambda:self.perform(lambda:self.registry.set(identity,default,replace=True)))
        except (ValueError,OSError) as error:self.status.set_text(str(error))

    def reset_all(self):
        self.window.confirm_action('Reset All Keybindings?', 'Restore shipped defaults and remove all saved overrides?', 'Reset All',
                                   lambda:self.perform(self.registry.reset_all))

    def perform(self,callback):
        if self._shutting_down:return
        try:callback()
        except (ValueError,OSError) as error:self.status.set_text(str(error))

    def capture(self,identity):
        command=self.registry.commands.get(identity)
        if not command:return
        dialog=Gtk.Window(title=command.title,transient_for=self.window,modal=True,default_width=440)
        self.dialogs.add(dialog)
        owned = self.dialog_signals[dialog] = Signals()
        box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10)
        for setter in (box.set_margin_start,box.set_margin_end,box.set_margin_top,box.set_margin_bottom):setter(16)
        box.append(Gtk.Label(label=command.context.title()+' · Current: '+(self.registry.binding(identity) or 'Unassigned')))
        display=Gtk.Label(label='Press a shortcut…',wrap=True);box.append(display)
        selected=[None];captured=[False]
        def keys(_,key,keycode,state):
            if self._shutting_down or dialog not in self.dialogs: return False
            value=event_shortcut(key,state)
            if value:
                selected[0]=value;captured[0]=True;display.set_text(value)
                if value in RESERVED_TERMINAL:display.set_text(value+' — protected in terminal context; it always reaches the PTY.')
                overlaps=self.registry.conflicts(identity,value,overlap=True)
                if overlaps:display.set_text(display.get_text()+'\nOverlaps: '+', '.join(c.title+' ('+c.context+')' for c in overlaps))
            return True
        controller=Gtk.EventControllerKey();controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        owned.connect(controller,'key-pressed',keys);dialog.add_controller(controller)
        buttons=Gtk.Box(spacing=6)
        def dispose(*_):
            self.dialogs.discard(dialog)
            signals=self.dialog_signals.pop(dialog,None)
            if signals:signals.close()
            return False
        def close():dispose();dialog.close()
        def save(replace=False):
            if self._shutting_down or dialog not in self.dialogs: return
            if not captured[0]:return
            if identity not in self.registry.commands:display.set_text('Command is no longer available');return
            try:self.registry.set(identity,selected[0],replace=replace)
            except Conflict as error:
                # Show the exact commands and contexts before replacement.
                self.window.confirm_action('Shortcut conflict',str(error)+'\nContext: '+command.context,'Replace Existing',lambda:save(True));return
            except (ValueError,OSError) as error:display.set_text(str(error));return
            close()
        for title,callback in [('Cancel',close),('Clear Shortcut',lambda:(selected.__setitem__(0,None),captured.__setitem__(0,True),display.set_text('Unassigned'))),('Save',save)]:
            button=Gtk.Button(label=title);buttons.append(button)
            owned.connect(button,'clicked',lambda *_,callback=callback:callback())
        box.append(buttons);dialog.set_child(box)
        owned.connect(dialog,'close-request',dispose)
        dialog.present()

    def choose_file(self,export):
        chooser=Gtk.FileChooserNative(title='Export Keybindings' if export else 'Import Keybindings',transient_for=self.window,
            action=Gtk.FileChooserAction.SAVE if export else Gtk.FileChooserAction.OPEN,
            accept_label='Export' if export else 'Import',cancel_label='Cancel')
        if export:chooser.set_current_name('keybindings.json')
        self.dialogs.add(chooser)
        owned = self.dialog_signals[chooser] = Signals()
        def done(_,response):
            self.dialogs.discard(chooser)
            if response==Gtk.ResponseType.ACCEPT and not self._shutting_down:
                file=chooser.get_file();path=file.get_path() if file else None
                if path:self.perform(lambda:self.registry.export(path) if export else self.import_file(path))
            self.dialog_signals.pop(chooser,None)
            owned.close();chooser.destroy()
        owned.connect(chooser,'response',done);chooser.show()

    def import_file(self,path):
        values,conflicts,unknown=self.registry.preview_import(path)
        summary='Unavailable commands retained: '+(', '.join(unknown) or 'none')
        def apply():
            if not self._shutting_down:self.registry.commit(values);self.status.set_text(summary)
        if conflicts:self.window.confirm_action('Import shortcut conflicts', 'These commands will remain unbound until resolved: '+', '.join(conflicts), 'Import Profile',apply)
        else:apply()

    def update_label(self):pass
    def mark_disconnected(self,*_):pass
    def close(self):
        if self._shutting_down:return
        self._shutting_down=True
        self.registry.listeners.remove(self.listener)
        self.sources.close();self.rows_signals.close();self.signals.close()
        for owned in self.dialog_signals.values():owned.close()
        self.dialog_signals.clear()
        for dialog in tuple(self.dialogs):
            if isinstance(dialog,Gtk.NativeDialog):dialog.destroy()
            else:dialog.close()
        self.dialogs.clear();self.window=None
