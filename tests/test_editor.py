"""Editor transformations, real headless GtkSource buffers and public dispatch."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import gi
for name,version in (('Gtk','4.0'),('Gdk','4.0'),('GtkSource','5')): gi.require_version(name,version)
from gi.repository import GLib, Gio, GtkSource

from naui.backends import FileSnapshot, LocalFileBackend
from naui.editor_commands import LINE_COMMANDS, line_edit, line_span, shortcut
from naui.editor_model import EditorDocument, large_file, save_text, shebang_language
from naui.editor_view import EditorController, GtkBufferAdapter
from naui.plugin_api import EditorAPI
from naui.ui_settings import UISettings


class LineEditingTests(unittest.TestCase):
    def edit(self,command,text,anchor=0,cursor=None,**settings):
        plan=line_edit(command,text,anchor,len(text) if cursor is None else cursor,**settings)
        return plan.apply(text),plan

    def test_language_comments_round_trip_and_reverse_selection(self):
        for syntax in (('#',''),('//',''),(';',''),('--',''),('/*','*/'),('<!--','-->')):
            with self.subTest(syntax=syntax):
                original='    one()\n        two()\n\nthree()\n'
                commented,plan=self.edit('toggle_comment',original,len(original),0,comment=syntax)
                self.assertGreater(plan.anchor,plan.cursor)
                self.assertIn(syntax[0]+' one()',commented)
                restored,second=self.edit('toggle_comment',commented,plan.anchor,plan.cursor,comment=syntax)
                self.assertEqual(restored,original)
                self.assertEqual(second.cursor,0)

    def test_unknown_language_and_blank_selection_noop(self):
        self.assertIsNone(line_edit('toggle_comment','a()',0,3))
        self.assertIsNone(line_edit('toggle_comment',' \n\t\n',0,4,comment=('#','')))

    def test_endpoint_at_next_line_excludes_that_line(self):
        text='one()\ntwo()\nthree()\n'
        result,plan=self.edit('toggle_comment',text,0,12,comment=('#',''))
        self.assertEqual(result,'# one()\n# two()\nthree()\n')
        self.assertEqual(line_span(result,plan.anchor,plan.cursor),(0,16))

    def test_mixed_comments_comment_all_then_restore(self):
        text='# one()\ntwo()\n'
        result,plan=self.edit('toggle_comment',text,comment=('#',''))
        self.assertEqual(result,'# # one()\n# two()\n')
        self.assertEqual(self.edit('toggle_comment',result,plan.anchor,plan.cursor,comment=('#',''))[0],text)
        self.assertEqual(self.edit('toggle_comment','  /* one() */  \n',comment=('/*','*/'))[0],'  one()  \n')

    def test_indent_spaces_tabs_and_outdent(self):
        for spaces,width in ((True,2),(True,4),(False,4)):
            original='one()\n'+('    ' if spaces else '\t')+'two()\n'
            result,plan=self.edit('indent',original,spaces=spaces,width=width)
            self.assertEqual(result,(' '*width if spaces else '\t')+'one()\n'+(' '*(width+4) if spaces else '\t\t')+'two()\n')
            self.assertEqual(self.edit('outdent',result,plan.anchor,plan.cursor,spaces=spaces,width=width)[0],original)

    def test_outdent_partial_tab_respects_display_width(self):
        result,_=self.edit('outdent','\tone()\n  two()\n',width=2,tab_width=4)
        self.assertEqual(result,'  one()\ntwo()\n')
        result,plan=self.edit('indent','\tone()\n',width=2,tab_width=8,spaces=False)
        self.assertEqual(result,'\t  one()\n')
        self.assertEqual(self.edit('outdent',result,plan.anchor,plan.cursor,width=2,tab_width=8,spaces=False)[0],'\tone()\n')

    def test_outdent_at_zero_is_noop(self):
        self.assertIsNone(line_edit('outdent','one()',0,5))

    def test_move_selected_block_both_directions_preserves_orientation(self):
        original='top\n  one\n  two\nbottom\n'
        result,plan=self.edit('move_up',original,16,4)
        self.assertEqual(result,'  one\n  two\ntop\nbottom\n')
        self.assertGreater(plan.anchor,plan.cursor)
        restored,reverse=self.edit('move_down',result,plan.anchor,plan.cursor)
        self.assertEqual(restored,original)
        self.assertEqual((reverse.anchor,reverse.cursor),(16,4))

    def test_move_unterminated_last_line_and_crlf(self):
        for original in ('one\ntwo','one\r\ntwo'):
            start=original.index('two')
            result,plan=self.edit('move_up',original,start,len(original))
            ending='\r\n' if '\r\n' in original else '\n'
            self.assertEqual(result,'two'+ending+'one')
            self.assertEqual(result[min(plan.anchor,plan.cursor):max(plan.anchor,plan.cursor)],'two'+ending)
            self.assertEqual(self.edit('move_down',result,plan.anchor,plan.cursor)[0],original)

    def test_move_boundaries_noop(self):
        self.assertIsNone(line_edit('move_up','one\ntwo',0,0))
        self.assertIsNone(line_edit('move_down','one\ntwo',5,5))

    def test_duplicate_selects_copy_and_preserves_final_newline_state(self):
        for original in ('one\n','one','one\r\n'):
            result,plan=self.edit('duplicate',original)
            separator='' if original.endswith('\n') else '\n'
            self.assertEqual(result,original+separator+original)
            self.assertEqual(result[plan.anchor:plan.cursor],original)

    def test_delete_line_and_final_line(self):
        self.assertEqual(self.edit('delete_line','one\ntwo\nthree',5,5)[0],'one\nthree')
        self.assertEqual(self.edit('delete_line','one\ntwo',6,6)[0],'one')
        self.assertEqual(self.edit('delete_line','one\r\ntwo',7,7)[0],'one')
        self.assertEqual(self.edit('delete_line','one\n',0,0)[0],'')

    def test_unicode_comments_no_conversion(self):
        original='    sõna("世界")\n'
        self.assertEqual(self.edit('toggle_comment',original,comment=('#',''))[0],'    # sõna("世界")\n')

    def test_invalid_command_and_offsets_fail(self):
        with self.assertRaises(ValueError): line_edit('run','x',0,0)
        with self.assertRaises(ValueError): line_edit('indent','x',0,3)

    def test_shortcut_dispatch_is_context_ready(self):
        self.assertEqual(shortcut('/',control=True),'toggle_comment')
        self.assertEqual(shortcut('d',control=True),'next_occurrence')
        self.assertEqual(shortcut('D',control=True,shift=True),'duplicate')
        self.assertEqual(shortcut('ISO_Left_Tab'),'outdent')
        self.assertEqual(shortcut('Down',alt=True),'move_down')
        self.assertIsNone(shortcut('c',control=True))
        self.assertIsNone(shortcut('Down',alt=True,control=True))


class DocumentTests(unittest.TestCase):
    def test_device_backend_revision_and_conflicts_stay_owned(self):
        backend=object()
        document=EditorDocument('example-device','/project/main.py',backend)
        document.loaded(FileSnapshot('/project/main.py','a()','original-revision'))
        document.buffer=SimpleNamespace(text=lambda:'edited()',dirty=True)
        self.assertEqual((document.device_id,document.path,document.backend),('example-device','/project/main.py',backend))
        self.assertTrue(document.dirty)
        self.assertEqual(document.encoding,'utf-8')
        document.external.observe(document.revision,FileSnapshot(document.path,'external','new-revision'))
        self.assertEqual(document.text(),'edited()')
        self.assertEqual(document.revision,'original-revision')

    def test_large_mode_thresholds(self):
        self.assertFalse(large_file('normal\n'*100))
        self.assertTrue(large_file('x'*100000))
        self.assertTrue(large_file('x\n'*20000))
        self.assertTrue(large_file('x'*80+'\n'*1024*1024))

    def test_shebang_only_returns_language_never_executes(self):
        for text,expected in [('#!/usr/bin/env python3 -u\n','python3'),('#!/bin/bash\n','sh'),
                              ('#!/usr/bin/env -S node --test\n','js'),('python3 main.py','nothing')]:
            self.assertEqual(shebang_language(text),None if expected=='nothing' else expected)

    def test_save_policy_default_preserves_bytes_and_opt_in_undo_content(self):
        text='one  \r\n\ttwo\t'
        self.assertEqual(save_text(text),text)
        self.assertEqual(save_text(text,trim=True,final_newline=True),'one\r\n\ttwo\r\n')
        self.assertEqual(save_text(''), '')
        self.assertEqual(save_text('x\n',final_newline=True),'x\n')

    def test_settings_round_trip_and_layout_reset_preserves_editing(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=UISettings(Path(directory)/'ui.json')
            settings.save(editor_tab_width=8,editor_indent_width=2,editor_spaces=False,editor_wrap=True,
                          editor_trim=True,editor_final_newline=True,editor_whitespace=True)
            restored=UISettings(settings.path)
            self.assertEqual(restored.values,settings.values)
            restored.reset()
            self.assertEqual(restored.values['editor_indent_width'],2)
            self.assertTrue(restored.values['editor_trim'])
            restored.save(editor_indent_width=0)
            self.assertEqual(restored.values['editor_indent_width'],2)

    def test_plugin_command_permissions_and_device_preservation(self):
        host=Mock()
        api=Mock(_host=host)
        api.current_document.return_value=SimpleNamespace(location=SimpleNamespace(device_id='example-device'))
        public=EditorAPI(api)
        self.assertTrue(public.invoke('indent'))
        api.require.assert_called_with('commands','editor_read','filesystem_write')
        host.current_editor.return_value.command.assert_called_once_with('indent')
        with self.assertRaises(ValueError): public.invoke('run')
        api.require.side_effect=PermissionError('missing filesystem_write')
        with self.assertRaises(PermissionError): public.invoke('indent')


class NativeBufferTests(unittest.TestCase):
    """GtkSource buffers work without a display; rendering is a separate smoke test."""
    def setUp(self):
        self.buffer=GtkSource.Buffer()
        self.buffer.set_enable_undo(True)
        self.buffer.set_text('one()\ntwo()\nthree()\n')
        self.buffer.set_modified(False)
        self.pane=SimpleNamespace(buffer=self.buffer,view=Mock(),window=SimpleNamespace(_shutting_down=False),
                                  _shutting_down=False)
        self.controller=EditorController.__new__(EditorController)
        self.controller.pane=self.pane
        self.controller.closed=False
        self.adapter=GtkBufferAdapter(self.buffer)

    def test_real_buffer_multiline_comment_selection_and_one_undo(self):
        original=self.adapter.text()
        edit=line_edit('toggle_comment',original,0,12,comment=('#',''))
        self.controller.apply(edit)
        self.assertEqual(self.adapter.text(),'# one()\n# two()\nthree()\n')
        self.assertTrue(self.buffer.get_has_selection())
        self.buffer.undo()
        self.assertEqual(self.adapter.text(),original)
        self.buffer.redo()
        self.assertEqual(self.adapter.text(),edit.apply(original))

    def test_native_language_metadata_comment_syntax(self):
        manager=GtkSource.LanguageManager.get_default()
        for language,prefix in [('python3','#'),('sh','#'),('c','//'),('cpp','//'),('js','//'),('typescript','//'),
                                ('yaml','#'),('toml','#'),('ini',';'),('sql','--')]:
            with self.subTest(language=language):
                self.assertEqual(manager.get_language(language).get_metadata('line-comment-start'),prefix)
        for language in ('json','markdown','html','css'):
            self.assertIsNotNone(manager.get_language(language))

    def test_native_search_case_whole_word_regex_replace_and_undo(self):
        self.buffer.set_text('name Name names\nname\n')
        settings=GtkSource.SearchSettings()
        settings.set_search_text('name')
        settings.set_case_sensitive(True)
        settings.set_at_word_boundaries(True)
        context=GtkSource.SearchContext.new(self.buffer,settings)
        found,start,end,wrapped=context.forward(self.buffer.get_start_iter())
        self.assertTrue(found)
        self.assertEqual(self.buffer.get_text(start,end,True),'name')
        self.assertEqual(context.replace_all('renamed',-1),2)
        self.assertEqual(self.adapter.text(),'renamed Name names\nrenamed\n')
        self.buffer.undo()
        self.assertEqual(self.adapter.text(),'name Name names\nname\n')
        settings.set_regex_enabled(True)
        settings.set_at_word_boundaries(False)
        settings.set_search_text(r'([Nn])ame(s?)')
        self.assertEqual(context.replace_all(r'\1oun\2',-1),4)
        self.assertEqual(self.adapter.text(),'noun Noun nouns\nnoun\n')

    def test_native_async_search_can_be_cancelled(self):
        settings=GtkSource.SearchSettings()
        settings.set_search_text('two')
        context=GtkSource.SearchContext.new(self.buffer,settings)
        result=[]
        context.forward_async(self.buffer.get_start_iter(),None,lambda owner, value: result.append(owner.forward_finish(value)))
        main=GLib.MainContext.default()
        import time
        deadline=time.monotonic()+2
        while not result and time.monotonic()<deadline:
            main.iteration(False)
        self.assertTrue(result[0][0])
        cancelled = Gio.Cancellable()
        cancelled.cancel()
        errors = []
        def finish(owner,result):
            try: owner.forward_finish(result)
            except GLib.Error as error:
                # Keep plain results, not an exception traceback retaining the
                # native GTask, callback frame and buffer past finalization.
                errors.append(error.matches(Gio.io_error_quark(),Gio.IOErrorEnum.CANCELLED))
        context.forward_async(self.buffer.get_start_iter(),cancelled,finish)
        deadline=time.monotonic()+2
        while not errors and time.monotonic()<deadline: main.iteration(False)
        self.assertTrue(errors[0])

    def test_view_cleanup_cancels_search_and_is_idempotent(self):
        control=self.controller
        control.search_generation=0
        pending=control.pending_search=Gio.Cancellable()
        control.search_settings=GtkSource.SearchSettings()
        control.search_context=GtkSource.SearchContext.new(self.buffer,control.search_settings)
        control.dialogs=set()
        control.close()
        self.assertTrue(pending.is_cancelled())
        self.assertTrue(control.closed)
        control.close()
        self.pane.view.insert_action_group.assert_called_once_with('editor',None)
        self.pane.window=None
        self.assertFalse(control.execute('duplicate'))

    def test_fifty_search_cancellations_after_close_and_gc(self):
        import gc
        main=GLib.MainContext.default()
        for index in range(50):
            buffer=GtkSource.Buffer()
            buffer.set_text('one\ntwo\n')
            pane=SimpleNamespace(buffer=buffer,view=Mock(),document=SimpleNamespace(large=False),
                                 _shutting_down=False,window=SimpleNamespace(_shutting_down=False))
            control=EditorController.__new__(EditorController)
            control.pane,control.closed=pane,False
            control.dialogs=set()
            control.pending_search=None
            control.search_generation=0
            control.search_settings=GtkSource.SearchSettings()
            control.search_context=GtkSource.SearchContext.new(buffer,control.search_settings)
            control.entry=SimpleNamespace(get_text=lambda:'two')
            control.case=control.word=control.regex=SimpleNamespace(get_active=lambda:False)
            control.count=Mock()
            control.find_next()
            pending=control.pending_search
            control.close()
            pane.window=None
            self.assertTrue(pending.is_cancelled())
            gc.collect()
            while main.pending(): main.iteration(False)
            control.close()
            del control,pane,buffer,pending
            gc.collect()


if __name__=='__main__': unittest.main()
