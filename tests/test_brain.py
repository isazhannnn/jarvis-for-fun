"""Local brain tests with fake processes and HTTP; no model download needed."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jarvis.brain import LocalBrain, SYSTEM_PROMPT, loopback_url


class LoopbackTests(unittest.TestCase):
    def test_localhost_normalized_without_dns(self):
        self.assertEqual(loopback_url("http://localhost:11434/"), "http://127.0.0.1:11434")

    def test_ipv6_loopback(self):
        self.assertEqual(loopback_url("http://[::1]:11434"), "http://[::1]:11434")

    def test_remote_credentials_and_redirect_like_values_rejected(self):
        for url in ("https://example.com", "http://192.168.1.2:11434", "http://127.0.0.1@example.com", "http://127.0.0.1/x", "http://localhost:11434?next=https://example.com"):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                loopback_url(url)


class BrainTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parent / ".brain-temp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.project = Path(self.temp.name)
        self.brain = LocalBrain(self.project, {})

    def tearDown(self):
        self.brain.close()
        self.temp.cleanup()

    def _fake_artifacts(self):
        self.brain.server_path.parent.mkdir(parents=True)
        self.brain.model_path.parent.mkdir(parents=True)
        self.brain.server_path.write_bytes(b"test-only")
        self.brain.model_path.write_bytes(b"test-only")

    def test_constructing_does_not_start_model(self):
        self.assertIsNone(self.brain._process)
        self.assertFalse((self.project / "data").exists())

    def test_missing_artifacts_have_actionable_setup_message(self):
        with self.assertRaisesRegex(RuntimeError, "setup_runtime.py"):
            self.brain.reply("Привет")

    def test_inference_has_no_tools_and_does_not_execute_model_text(self):
        with patch.object(self.brain, "_ensure_server", return_value=("http://127.0.0.1:18765", "private")), patch.object(
            self.brain, "_json_request", return_value={"choices": [{"message": {"content": "os.system('shutdown /s')"}}]}
        ) as request:
            answer = self.brain.reply("Скажи привет")
        self.assertEqual(answer, "os.system('shutdown /s')")
        payload = request.call_args.args[1]
        self.assertNotIn("tools", payload)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["max_tokens"], 220)
        self.assertEqual(request.call_args.kwargs["token"], "private")

    def test_owned_server_uses_loopback_auth_and_hidden_subprocess(self):
        self._fake_artifacts()
        process = MagicMock()
        process.poll.return_value = None
        with patch("jarvis.brain.subprocess.Popen", return_value=process) as spawn, patch.object(
            self.brain, "_choose_port", return_value=19001
        ), patch.object(self.brain, "_json_request", side_effect=[{"status": "ok"}, {"data": []}]) as request:
            url, token = self.brain._ensure_server()
            self.assertEqual(url, "http://127.0.0.1:19001")
            self.assertGreater(len(token), 20)
            args = spawn.call_args.args[0]
            self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
            self.assertEqual(args[args.index("--api-key") + 1], token)
            self.assertFalse(spawn.call_args.kwargs["shell"])
            self.assertIn("/v1/models", request.call_args.args[0])
            self.brain.close()
        process.terminate.assert_called_once()

    def test_existing_unowned_service_is_not_adopted(self):
        self._fake_artifacts()
        process = MagicMock()
        process.poll.return_value = 1
        with patch("jarvis.brain.subprocess.Popen", return_value=process), patch.object(
            self.brain, "_json_request"
        ) as request:
            with self.assertRaisesRegex(RuntimeError, "Не удалось запустить"):
                self.brain._ensure_server()
        request.assert_not_called()
        process.terminate.assert_not_called()

    def test_fallback_to_local_ollama_when_bundled_artifacts_absent(self):
        self.brain.settings.update(ollama_enabled=True)
        with patch.object(self.brain, "_json_request", return_value={"message": {"content": "Привет."}}) as request:
            self.assertEqual(self.brain.reply("Привет"), "Привет.")
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:11434/api/chat")

    def test_disabled_models_do_not_open_network(self):
        self.brain.settings.update(local_ai_enabled=False, ollama_enabled=False)
        with patch.object(self.brain, "_json_request") as request:
            with self.assertRaisesRegex(RuntimeError, "выключен"):
                self.brain.reply("Привет")
        request.assert_not_called()

    def test_history_retains_six_messages_with_bounded_prompt(self):
        with patch.object(self.brain, "_llama_reply", return_value="ответ " * 100):
            for _ in range(6):
                self.brain.reply("вопрос " * 100)
        self.assertEqual(len(self.brain._history), 6)
        messages = self.brain._messages("текущий вопрос " * 200)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertLessEqual(sum(len(item["content"]) for item in messages), 3400)

    def test_empty_or_invalid_model_response_does_not_enter_history(self):
        with patch.object(self.brain, "_ensure_server", return_value=("http://127.0.0.1:18765", "private")), patch.object(
            self.brain, "_json_request", return_value={"choices": []}
        ):
            with self.assertRaises(RuntimeError):
                self.brain.reply("Привет")
        self.assertEqual(self.brain._history, [])

    def test_close_kills_only_owned_process_if_graceful_stop_times_out(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("llama", 3), 0]
        self.brain._process = process
        self.brain.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertIsNone(self.brain._process)

    def test_closed_brain_cannot_restart(self):
        self.brain.close()
        with patch("jarvis.brain.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "закрывается"):
                self.brain.reply("Привет")
        spawn.assert_not_called()

    def test_json_request_attaches_token_and_limits_response(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"status": "ok"}).encode()
        self.brain._opener = MagicMock()
        self.brain._opener.open.return_value = response
        value = self.brain._json_request("http://127.0.0.1:18765/health", token="secret", timeout=1)
        self.assertEqual(value, {"status": "ok"})
        request = self.brain._opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        response.read.assert_called_once_with(1024 * 1024 + 1)

    def _plan_output(self, user, model_content):
        with patch.object(self.brain, "_ensure_server", return_value=("http://127.0.0.1:18765", "private")), patch.object(
            self.brain, "_json_request", return_value={"choices": [{"message": {"content": model_content}}]}
        ) as request:
            return self.brain.plan(user), request

    def test_multilingual_plan_is_inert_and_does_not_change_history(self):
        value, request = self._plan_output("Öffne den Taschenrechner", '{"action":"open_app","argument":"калькулятор"}')
        self.assertEqual(value, {"action": "open_app", "argument": "калькулятор"})
        self.assertEqual(self.brain._history, [])
        payload = request.call_args.args[1]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", payload)

    def test_fixed_english_identifiers_become_only_allowlisted_canonical_targets(self):
        # Actual multilingual model outputs can use English app identifiers.
        # This normalization is a finite dictionary, never a path or shell parser.
        for action, argument, canonical in [
            ("open_app", "calculator", "калькулятор"),
            ("open_app", "Notepad", "блокнот"),
            ("open_app", "file_explorer", "проводник"),
            ("open_folder", "Downloads", "загрузки"),
        ]:
            with self.subTest(argument=argument):
                content = json.dumps({"action": action, "argument": argument})
                plan, _ = self._plan_output("Ouvre la calculatrice", content)
                self.assertEqual(plan, {"action": action, "argument": canonical})
        self.assertEqual(self.brain._history, [])

    def test_negation_and_questions_rejected_even_if_model_would_propose_action(self):
        for text in ("Don't open calculator", "Не открывай блокнот", "Как открыть блокнот", "How do I open calculator", "Калькуляторды ашпа", "不要打开计算器", "Öffne den Rechner nicht", "Open calculator?"):
            with self.subTest(text=text):
                value, request = self._plan_output(text, '{"action":"open_app","argument":"калькулятор"}')
                self.assertIsNone(value)
                request.assert_not_called()

    def test_unsupported_actions_code_paths_and_extra_fields_rejected(self):
        bad_outputs = [
            '{"action":"shutdown","argument":""}',
            '{"action":"open_app","argument":"powershell.exe -Command exit"}',
            '{"action":"open_folder","argument":"C:\\\\Windows"}',
            '{"action":"screenshot","argument":"" , "command":"calc.exe"}',
            '{"action":"open_app","argument":["калькулятор"]}',
            '[{"action":"screenshot","argument":""}]',
            '```json\n{"action":"screenshot","argument":""}\n```',
            '{"action":"screenshot","argument":"unexpected"}',
            '{"action":"search_web","argument":""}',
            '{"action":"none","argument":""}',
        ]
        for content in bad_outputs:
            with self.subTest(content=content):
                value, _ = self._plan_output("Open an app", content)
                self.assertIsNone(value)

    def test_model_question_classification_can_reject_nonobvious_question(self):
        value, _ = self._plan_output("Пожалуйста расскажи об открытии браузера", '{"action":"none","argument":""}')
        self.assertIsNone(value)

    def test_plan_failure_returns_none_and_leaves_history_untouched(self):
        with patch.object(self.brain, "_ensure_server", side_effect=RuntimeError("no model")):
            self.assertIsNone(self.brain.plan("Open browser"))
        self.assertEqual(self.brain._history, [])

    def test_ollama_plans_request_json_on_loopback_only(self):
        self.brain.settings.update(local_ai_enabled=False, ollama_enabled=True)
        with patch.object(self.brain, "_json_request", return_value={"message": {"content": '{"action":"screenshot","argument":""}'}}) as request:
            self.assertEqual(self.brain.plan("Сфотографируй экран"), {"action": "screenshot", "argument": ""})
        self.assertEqual(request.call_args.args[1]["format"], "json")
        self.brain.settings["ollama_url"] = "http://example.com"
        with patch.object(self.brain, "_json_request") as request:
            self.assertIsNone(self.brain.plan("Open browser"))
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
