"""Behavior and boundary tests. No real programs, keys or power calls execute."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from jarvis.engine import CommandEngine, calculate
from jarvis.windows import WindowsController


TEST_ROOT = Path(__file__).resolve().parent
SCRATCH = TEST_ROOT / '.engine-temp'


class EngineTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=SCRATCH)
        self.base = Path(self.temp.name).resolve()
        self.assertTrue(self.base.is_relative_to(SCRATCH.resolve()))
        self.project = self.base / 'jarvis-project'
        self.project.mkdir()
        self.documents = self.base / 'documents'
        self.documents.mkdir()
        self.windows = Mock(spec=WindowsController)
        self.windows.system_status.return_value = {
            'memory_percent': 50, 'battery_percent': 80, 'disk_free_gb': 300,
            'cpu_percent': 20, 'power_plugged': True, 'platform': 'Windows',
        }
        self.windows.list_windows.return_value = [{'hwnd': 0x123456789, 'title': 'Блокнот — Проверка'}]
        self.constructor = patch('jarvis.engine.WindowsController', return_value=self.windows)
        self.constructor.start()
        self.engine = CommandEngine(self.project, {
            'local_ai_enabled': False, 'ollama_enabled': False,
            'search_roots': [str(self.documents)],
        })

    def tearDown(self):
        self.engine.close()
        self.constructor.stop()
        self.assertTrue(self.base.is_relative_to(SCRATCH.resolve()))
        self.temp.cleanup()

    def file(self, name='Отчёт.txt', content='исходные данные'):
        path = self.documents / name
        path.write_text(content, encoding='utf-8')
        return path

    def trash(self, path):
        planned = self.engine.run('Удали файл ' + str(path))
        self.assertEqual(planned['kind'], 'confirm', planned)
        self.assertTrue(path.exists(), 'A file must stay in place until confirmation')
        done = self.engine.confirm(planned['token'])
        self.assertEqual(done['kind'], 'answer', done)
        self.assertFalse(path.exists())
        return done

    def test_power_is_confirmed_and_token_is_single_use(self):
        for command, action in [('Выключи компьютер', 'shutdown'), ('Перезагрузи компьютер', 'restart')]:
            with self.subTest(command=command):
                self.windows.reset_mock()
                planned = self.engine.run(command)
                self.assertEqual(planned['kind'], 'confirm')
                self.windows.power.assert_not_called()
                self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'answer')
                self.windows.power.assert_called_once_with(action)
                self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'error')
                self.windows.power.assert_called_once_with(action)

    def test_cancel_expiry_and_new_command_prevent_execution(self):
        planned = self.engine.run('Выключи компьютер')
        self.engine.cancel(planned['token'])
        self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'error')
        planned = self.engine.run('Выключи компьютер')
        self.engine.pending[planned['token']].expires = time.monotonic() - 1
        self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'error')
        planned = self.engine.run('Выключи компьютер')
        self.engine.run('Привет')
        self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'error')
        self.windows.power.assert_not_called()

    def test_failed_callback_also_consumes_confirmation(self):
        self.windows.power.side_effect = OSError('Windows denied action')
        planned = self.engine.run('Выключи компьютер')
        self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'error')
        self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'error')
        self.windows.power.assert_called_once_with('shutdown')

    def test_negative_phrases_and_questions_never_trigger_power(self):
        for command in [
            'Не выключай компьютер', 'не перезагружай ноутбук',
            'Как выключить компьютер?', 'Ты можешь выключить компьютер?',
            'Что будет, если выключить компьютер?', 'Do not shutdown computer',
            'How can I restart my computer?', 'Компьютерді өшірме',
        ]:
            with self.subTest(command=command):
                outcome = self.engine.run(command)
                self.assertNotEqual(outcome['kind'], 'confirm', outcome)
                self.windows.power.assert_not_called()

    def test_exact_russian_english_kazakh_commands_ignore_case(self):
        for command in ['ОТКРОЙ КАЛЬКУЛЯТОР', 'Open Calculator', 'КАЛЬКУЛЯТОРДЫ АШ']:
            with self.subTest(command=command):
                self.windows.reset_mock()
                outcome = self.engine.run(command)
                self.assertEqual(outcome['kind'], 'answer', outcome)
                self.assertEqual(self.windows.open_app.call_args.args[0].casefold(), 'калькулятор')
        for command in ['ГРОМЧЕ', 'Volume Up', 'ДЫБЫСТЫ КӨТЕР']:
            with self.subTest(command=command):
                self.windows.reset_mock()
                self.assertEqual(self.engine.run(command)['kind'], 'answer')
                self.windows.volume.assert_called_once_with('up')

    def test_wake_word_is_only_a_prefix(self):
        self.assertEqual(self.engine.run('Джарвис, открой калькулятор')['kind'], 'answer')
        self.windows.open_app.assert_called_once()

    def test_shell_like_app_names_are_not_executed(self):
        # Exercise the actual Windows app resolver with an empty app index, while
        # intercepting every possible launch so this test cannot open programs.
        self.engine.windows = WindowsController(self.project)
        with patch.object(self.engine.windows, '_apps', return_value={}), \
             patch('jarvis.windows.subprocess.Popen') as popen, \
             patch('jarvis.windows.os.startfile') as startfile, \
             patch('jarvis.windows.webbrowser.open') as browser:
            for name in ['блокнот & calc.exe', 'cmd /c whoami', 'powershell -Command Remove-Item D:\\*', '$(echo test)']:
                with self.subTest(name=name):
                    self.assertEqual(self.engine.run('Открой ' + name)['kind'], 'error')
            popen.assert_not_called()
            startfile.assert_not_called()
            browser.assert_not_called()

    def test_arithmetic_is_useful_and_rejects_code_or_huge_work(self):
        self.assertEqual(calculate('(12 + 8) * 5'), 100)
        self.assertEqual(calculate('2,5 × 4'), 10)
        self.assertEqual(calculate('2^8'), 256)
        for expression in [
            '__import__("os").system("whoami")', 'open("secret")',
            '[1, 2][0]', 'True + 1', '2**1000000000', '1e309',
            '2 +', '1; 2', '1 / 0', '9' * 161,
        ]:
            with self.subTest(expression=expression):
                outcome = self.engine.run('Посчитай ' + expression)
                self.assertEqual(outcome['kind'], 'error', outcome)
        self.windows.assert_not_called()
        self.assertEqual(self.windows.mock_calls, [])

    def test_notes_preserve_text_without_sending_or_logging_contents(self):
        content = 'Секрет_из_заметки\nВторая строка; $(echo anything)'
        outcome = self.engine.run('Создай заметку: ' + content)
        self.assertEqual(outcome['kind'], 'answer', outcome)
        saved = Path(outcome['path'])
        self.assertTrue(saved.is_relative_to(self.project / 'data' / 'notes'))
        self.assertEqual(saved.read_text(encoding='utf-8'), content)
        audit = self.project / 'data' / 'logs' / 'actions.jsonl'
        self.assertNotIn('Секрет_из_заметки', audit.read_text(encoding='utf-8'))
        self.assertEqual(self.windows.mock_calls, [])

    def test_typed_text_requires_confirmation_and_remembers_window(self):
        outcome = self.engine.run('Напечатай в окно Блокнот: Привет 😀')
        self.assertEqual(outcome['kind'], 'confirm', outcome)
        self.windows.type_text.assert_not_called()
        self.assertEqual(self.engine.confirm(outcome['token'])['kind'], 'answer')
        self.windows.type_text.assert_called_once_with('Привет 😀', 0x123456789)

    def test_ambiguous_window_does_not_type_or_switch(self):
        self.windows.list_windows.return_value.append({'hwnd': 456, 'title': 'Блокнот — Другой'})
        outcome = self.engine.run('Напечатай в окно Блокнот: Текст')
        self.assertEqual(outcome['kind'], 'error')
        self.windows.type_text.assert_not_called()
        self.windows.focus_window.assert_not_called()

    def test_trash_cancel_keeps_file_and_never_calls_windows_recycle(self):
        path = self.file()
        planned = self.engine.run('Удали файл ' + str(path))
        self.assertEqual(planned['kind'], 'confirm')
        self.assertTrue(path.exists())
        self.engine.cancel(planned['token'])
        self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'error')
        self.assertEqual(path.read_text(encoding='utf-8'), 'исходные данные')
        self.windows.recycle_file.assert_not_called()

    def test_trash_and_restore_are_both_explicit_and_preserve_contents(self):
        path = self.file()
        self.trash(path)
        restore = self.engine.run('Восстанови последний файл')
        self.assertEqual(restore['kind'], 'confirm', restore)
        self.assertFalse(path.exists())
        self.assertEqual(self.engine.confirm(restore['token'])['kind'], 'answer')
        self.assertEqual(path.read_text(encoding='utf-8'), 'исходные данные')
        self.windows.recycle_file.assert_not_called()

    def test_restoration_cannot_overwrite_a_new_file(self):
        path = self.file()
        self.trash(path)
        restore = self.engine.run('Восстанови последний файл')
        self.assertEqual(restore['kind'], 'confirm')
        path.write_text('новый файл', encoding='utf-8')
        self.assertEqual(self.engine.confirm(restore['token'])['kind'], 'error')
        self.assertEqual(path.read_text(encoding='utf-8'), 'новый файл')
        path.unlink()
        restore = self.engine.run('Восстанови последний файл')
        self.assertEqual(self.engine.confirm(restore['token'])['kind'], 'answer')
        self.assertEqual(path.read_text(encoding='utf-8'), 'исходные данные')

    def test_changed_file_invalidates_trash_confirmation(self):
        path = self.file()
        planned = self.engine.run('Удали файл ' + str(path))
        self.assertEqual(planned['kind'], 'confirm')
        path.write_text('изменённый файл после подтверждения', encoding='utf-8')
        self.assertEqual(self.engine.confirm(planned['token'])['kind'], 'error')
        self.assertTrue(path.exists())

    def test_metadata_filename_cannot_collide_with_recycled_file(self):
        path = self.file('restore.json', '{"user": "data"}')
        self.trash(path)
        restore = self.engine.run('Восстанови последний файл')
        self.assertEqual(restore['kind'], 'confirm', restore)
        self.assertEqual(self.engine.confirm(restore['token'])['kind'], 'answer')
        self.assertEqual(path.read_text(encoding='utf-8'), '{"user": "data"}')

    def test_directories_relative_paths_and_project_files_are_protected(self):
        protected = self.project / 'main.py'
        protected.write_text('protected', encoding='utf-8')
        for target in [str(self.documents), 'relative.txt', str(protected)]:
            with self.subTest(target=target):
                outcome = self.engine.run('Удали файл ' + target)
                self.assertEqual(outcome['kind'], 'error', outcome)
        self.assertTrue(protected.exists())

    def test_symlink_file_is_rejected(self):
        target = self.file()
        link = self.documents / 'link.txt'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Windows does not permit creating symlinks: ' + str(exc))
        outcome = self.engine.run('Удали файл ' + str(link))
        self.assertEqual(outcome['kind'], 'error', outcome)
        self.assertTrue(target.exists())
        self.assertTrue(link.is_symlink())

    def test_link_guard_runs_before_a_file_is_moved(self):
        # Cover the refusal even on Windows accounts without symlink privileges.
        target = self.file()
        with patch.object(Path, 'is_symlink', return_value=True):
            outcome = self.engine.run('Удали файл ' + str(target))
        self.assertEqual(outcome['kind'], 'error', outcome)
        self.assertTrue(target.exists())
        self.windows.recycle_file.assert_not_called()

    def test_file_search_finds_names_without_reading_contents(self):
        path = self.file('Мой Диплом.txt', 'НЕ_ПОКАЗЫВАТЬ_СОДЕРЖИМОЕ')
        outcome = self.engine.run('Найди файл диплом')
        self.assertEqual(outcome['kind'], 'answer', outcome)
        self.assertIn(str(path), outcome['text'])
        self.assertNotIn('НЕ_ПОКАЗЫВАТЬ_СОДЕРЖИМОЕ', outcome['text'])


if __name__ == '__main__':
    unittest.main()
