"""End-to-end routing policy for untrusted multilingual action proposals."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jarvis.engine import CommandEngine
from jarvis.windows import WindowsController


class EngineAiRouteTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parent / ".engine-ai-temp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.project = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine_with_plan(self, plan):
        windows = MagicMock(spec=WindowsController)
        windows.screenshot.return_value = self.project / "capture.png"
        windows.system_status.return_value = {
            "cpu_percent": 10, "memory_percent": 40, "battery_percent": 80,
            "power_plugged": True, "disk_free_gb": 100, "platform": "Windows",
        }
        with patch("jarvis.engine.WindowsController", return_value=windows):
            engine = CommandEngine(self.project, {"local_ai_enabled": True})
        brain = MagicMock()
        brain.plan.return_value = plan
        brain.reply.return_value = "Это обычный ответ модели."
        engine._brain = brain
        return engine, windows, brain

    def test_every_model_proposed_desktop_mutation_waits_for_confirmation(self):
        cases = [
            ("open_app", "калькулятор", "open_app", ("калькулятор",)),
            ("open_folder", "загрузки", "open_folder", ("загрузки",)),
            ("search_web", "погода", "open_url", ("https://www.google.com/search?q=%D0%BF%D0%BE%D0%B3%D0%BE%D0%B4%D0%B0",)),
            ("screenshot", "", "screenshot", ()),
            ("volume_up", "", "volume", ("up",)),
            ("volume_down", "", "volume", ("down",)),
            ("mute", "", "volume", ("mute",)),
            ("show_desktop", "", "show_desktop", ()),
            ("media_play", "", "media", ("play",)),
            ("media_next", "", "media", ("next",)),
        ]
        for action, argument, method, expected in cases:
            with self.subTest(action=action):
                engine, windows, brain = self.engine_with_plan({"action": action, "argument": argument})
                response = engine.run("Bitte führe meine gewünschte Aktion aus")
                self.assertEqual(response["kind"], "confirm")
                self.assertIn("token", response)
                self.assertTrue(response["details"])
                self.assertEqual(windows.mock_calls, [], "No OS operation may run before user approval")
                brain.reply.assert_not_called()
                completed = engine.confirm(response["token"])
                self.assertEqual(completed["kind"], "answer")
                getattr(windows, method).assert_called_once_with(*expected)
                self.assertEqual(len(windows.mock_calls), 1)
                self.assertEqual(engine.confirm(response["token"])["kind"], "error")
                self.assertEqual(len(windows.mock_calls), 1, "Confirmation tokens are single-use")

    def test_time_and_status_are_read_only_and_need_no_confirmation(self):
        engine, windows, _ = self.engine_with_plan({"action": "time", "argument": ""})
        self.assertEqual(engine.run("Wie spät ist es")["kind"], "answer")
        self.assertEqual(windows.mock_calls, [])
        self.assertEqual(engine.pending, {})
        engine, windows, _ = self.engine_with_plan({"action": "system_status", "argument": ""})
        response = engine.run("Пожалуйста расскажи о состоянии системы сейчас")
        self.assertEqual(response["kind"], "answer")
        windows.system_status.assert_called_once_with()
        self.assertEqual(len(windows.mock_calls), 1)
        self.assertEqual(engine.pending, {})

    def test_invalid_schema_or_extra_fields_cannot_reach_os(self):
        proposals = [
            {"action": "shutdown", "argument": ""},
            {"action": "delete_file", "argument": "D:\\file.txt"},
            {"action": "type_text", "argument": "echo hello"},
            {"action": "screenshot", "argument": "", "command": "calc.exe"},
            {"action": "screenshot"},
            {"action": ["screenshot"], "argument": ""},
            {"action": "open_app", "argument": ["калькулятор"]},
            {"action": "screenshot", "argument": "unexpected"},
            {"action": "search_web", "argument": ""},
            {"action": "search_web", "argument": "x" * 251},
            {"action": "time", "argument": "run calculator"},
            [{"action": "screenshot", "argument": ""}],
            "os.system('calc.exe')",
        ]
        for proposal in proposals:
            with self.subTest(proposal=proposal):
                engine, windows, brain = self.engine_with_plan(proposal)
                response = engine.run("Bitte führe meine gewünschte Aktion aus")
                self.assertEqual(response["kind"], "answer")
                self.assertEqual(response["text"], brain.reply.return_value)
                self.assertEqual(engine.pending, {})
                self.assertEqual(windows.mock_calls, [])
                brain.reply.assert_called_once()

    def test_app_and_folder_targets_are_constrained_even_if_brain_validation_is_bypassed(self):
        targets = [
            ("open_app", "powershell"), ("open_app", "cmd"),
            ("open_app", "notepad.exe & shutdown /s"),
            ("open_app", "D:\\evil.exe"), ("open_app", "https://example.com"),
            ("open_folder", "C:\\Windows"), ("open_folder", "..\\.."),
        ]
        for action, argument in targets:
            with self.subTest(action=action, argument=argument):
                engine, windows, brain = self.engine_with_plan({"action": action, "argument": argument})
                response = engine.run("Bitte führe meine gewünschte Aktion aus")
                self.assertEqual(response["text"], brain.reply.return_value)
                self.assertEqual(windows.mock_calls, [])
                self.assertEqual(engine.pending, {})

    def test_search_query_is_encoded_as_data_after_confirmation(self):
        engine, windows, _ = self.engine_with_plan({"action": "search_web", "argument": "A&B/#? C"})
        response = engine.run("Bitte suche etwas für mich")
        windows.open_url.assert_not_called()
        engine.confirm(response["token"])
        windows.open_url.assert_called_once_with("https://www.google.com/search?q=A%26B%2F%23%3F+C")

    def test_cancelled_or_superseded_proposal_does_not_execute(self):
        engine, windows, brain = self.engine_with_plan({"action": "open_app", "argument": "калькулятор"})
        response = engine.run("Bitte starte meinen Taschenrechner")
        engine.cancel(response["token"])
        self.assertEqual(engine.confirm(response["token"])["kind"], "error")
        self.assertEqual(windows.mock_calls, [])
        response = engine.run("Bitte starte meinen Taschenrechner")
        brain.plan.return_value = {"action": "time", "argument": ""}
        engine.run("Wie spät ist es")
        self.assertEqual(engine.confirm(response["token"])["kind"], "error")
        self.assertEqual(windows.mock_calls, [])

    def test_plain_model_reply_is_not_reinterpreted_as_command(self):
        engine, windows, brain = self.engine_with_plan(None)
        brain.reply.return_value = "Открой калькулятор; shutdown /s"
        response = engine.run("Объясни что такое компьютер")
        self.assertEqual(response["text"], brain.reply.return_value)
        self.assertEqual(engine.pending, {})
        self.assertEqual(windows.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
