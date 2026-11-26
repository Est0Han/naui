"""Bounded, selectable chat cards. Markdown is escaped before Pango rendering."""
import html
import re
from gi.repository import Gtk, Pango
from .lifecycle import Sources


def markup(text):
    parts = re.split(r'(```[^\n]*\n.*?```|`[^`\n]+`)', text, flags=re.S)
    rendered = []
    for part in parts:
        if part.startswith('```') and part.endswith('```'):
            body = part.split('\n', 1)[1][:-3]
            rendered.append('<span font_family="monospace" background="#292929">'+html.escape(body)+'</span>')
        elif part.startswith('`') and part.endswith('`'):
            rendered.append('<span font_family="monospace" background="#292929">'+html.escape(part[1:-1])+'</span>')
        else:
            part = html.escape(part)
            part = re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', part)
            part = re.sub(r'(?m)^#{1,4} (.+)$', r'<b>\1</b>', part)
            part = re.sub(r'(?m)^[-*] ', '• ', part)
            rendered.append(part)
    return ''.join(rendered)


def validate_messages(messages):
    if messages is None: return
    if not isinstance(messages, (list, tuple)) or len(messages) > 150: raise ValueError('Invalid conversation messages')
    size, identities = 0, set()
    for message in messages:
        if not isinstance(message, dict) or set(message)-{'id','role','text','title','time','actions'}: raise ValueError('Invalid conversation message')
        if message.get('role') not in ('user','assistant','tool','notice'): raise ValueError('Invalid message role')
        for key, limit in [('id',128),('text',64000),('title',200),('time',40)]:
            value = message.get(key, '')
            if not isinstance(value,str) or '\0' in value or len(value.encode())>limit: raise ValueError('Invalid message field')
            size += len(value.encode())
        actions = message.get('actions', [])
        if not isinstance(actions, (list, tuple)) or len(actions) > 4: raise ValueError('Invalid conversation actions')
        for action in actions:
            if not isinstance(action, (list, tuple)) or len(action) != 2 or not isinstance(action[0], str) or not action[0] or len(action[0].encode()) > 120 or '\0' in action[0] or not callable(action[1]):
                raise ValueError('Invalid conversation action')
            size += len(action[0].encode())
        identity = message.get('id')
        if not identity or identity in identities: raise ValueError('Message IDs must be unique')
        identities.add(identity)
    if size > 256*1024: raise ValueError('Conversation too large')


class ConversationView(Gtk.Box):
    def __init__(self, alive=lambda: True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_margin_top(8); self.set_margin_bottom(8)
        self.set_margin_start(10); self.set_margin_end(10)
        self.cards = {}
        self.actions = {}
        self.action_boxes = {}
        self.sources = Sources()
        self.alive = alive

    def update(self, messages):
        wanted = {m['id'] for m in messages}
        for identity in list(self.cards):
            if identity not in wanted:
                self.remove(self.cards.pop(identity)[0])
                self.actions.pop(identity, None)
                self.action_boxes.pop(identity, None)
        previous = None
        for message in messages:
            identity, role = message['id'], message['role']
            if identity not in self.cards:
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
                box.add_css_class('naui-chat-card')
                box.add_css_class('naui-chat-'+role)
                heading = Gtk.Label(xalign=0, wrap=True)
                heading.add_css_class('dim-label')
                body = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True, hexpand=True)
                body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                body.set_natural_wrap_mode(Gtk.NaturalWrapMode.NONE)
                if role == 'tool':
                    expander = Gtk.Expander()
                    expander.set_label_widget(heading); expander.set_child(body)
                    box.append(expander)
                else:
                    box.append(heading); box.append(body)
                self.append(box)
                self.cards[identity] = (box, heading, body, None)
            box, heading, body, old = self.cards[identity]
            self.reorder_child_after(box, previous); previous = box
            actions = message.get('actions', [])
            self.actions[identity] = actions
            labels = [a[0] for a in actions]
            if labels != [a[0] for a in (old or {}).get('actions', [])]:
                if identity in self.action_boxes: box.remove(self.action_boxes.pop(identity))
                if actions:
                    box.add_css_class('naui-chat-approval')
                    buttons = Gtk.Box(spacing=6)
                    box.append(buttons); self.action_boxes[identity] = buttons
                    for index, (label, _) in enumerate(actions):
                        button = Gtk.Button(label=label)
                        button.connect('clicked', lambda b, i=identity, n=index: self.activate(i, n))
                        buttons.append(button)
            if old == message: continue
            title = message.get('title') or {'user':'You','assistant':'Assistant','tool':'Tool activity','notice':'Status'}[role]
            heading.set_text(title + ('  ·  '+message['time'] if message.get('time') else ''))
            if role == 'assistant': body.set_markup(markup(message['text']))
            else: body.set_text(message['text'])
            if role == 'tool': body.add_css_class('monospace')
            self.cards[identity] = (box, heading, body, dict(message))

    def activate(self, identity, index):
        actions = self.actions.get(identity, [])
        if not self.alive() or index >= len(actions): return
        callback = actions[index][1]
        buttons = self.action_boxes.get(identity)
        if buttons is None or not buttons.get_sensitive(): return
        buttons.set_sensitive(False)
        def deliver():
            current = self.actions.get(identity, [])
            if self.alive() and self.action_boxes.get(identity) is buttons and index < len(current) and current[index][1] is callback:
                callback()
            if self.action_boxes.get(identity) is buttons: buttons.set_sensitive(True)
        self.sources.idle(deliver)

    def close(self):
        self.sources.close(); self.actions.clear()
