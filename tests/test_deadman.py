"""Тесты внешнего сторожа (.github/deadman.py). Запуск: python3 -m unittest discover -s tests -v

Сеть и GitHub подменены: ничего не отправляется, state-файл и GITHUB_OUTPUT — во временном каталоге.
"""

import importlib.util
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deadman", ROOT / ".github" / "deadman.py")
deadman = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deadman)

NOW = datetime(2026, 10, 5, 7, 17, tzinfo=timezone.utc).timestamp()  # слот cron :17
HOUR = 3600


def pulse(path: Path, age_hours: float, ok: bool = True, text: str = "📋 Сборка постов: отработала.") -> None:
    ts = datetime.fromtimestamp(NOW - age_hours * HOUR, timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps({"ts": ts, "ok": ok, "text": text}, ensure_ascii=False), encoding="utf-8")


class StaleWhy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "heartbeat.json"

    def test_свежий_и_ok_молчим(self):
        pulse(self.file, 3)
        self.assertIsNone(deadman.stale_why(self.file, NOW))

    def test_ровно_26_часов_ещё_не_тревога_а_27_уже_да(self):
        pulse(self.file, 26)
        self.assertIsNone(deadman.stale_why(self.file, NOW))
        pulse(self.file, 27)
        self.assertIn("27 ч", deadman.stale_why(self.file, NOW))

    def test_свежий_но_ok_false_это_тревога_с_текстом_пульса(self):
        pulse(self.file, 1, ok=False, text="🚨 Сборка постов сегодня не запускалась\nСмотреть: systemctl")
        why = deadman.stale_why(self.file, NOW)
        self.assertIn("ok=False", why)
        self.assertIn("Сборка постов сегодня не запускалась / Смотреть: systemctl", why)

    def test_файла_нет_или_он_битый_это_тревога(self):
        self.assertIn("ещё не появился", deadman.stale_why(self.file, NOW))
        self.file.write_text("не json", encoding="utf-8")
        self.assertIn("не читается", deadman.stale_why(self.file, NOW))
        self.file.write_text(json.dumps({"ok": True}), encoding="utf-8")
        self.assertIn("не читается", deadman.stale_why(self.file, NOW))

    def test_время_без_пояса_считается_UTC(self):
        naive = datetime.fromtimestamp(NOW - 30 * HOUR, timezone.utc).replace(tzinfo=None).isoformat()
        self.file.write_text(json.dumps({"ts": naive, "ok": True}), encoding="utf-8")
        self.assertIn("30 ч", deadman.stale_why(self.file, NOW))

    def test_длинный_текст_пульса_режется(self):
        pulse(self.file, 30, text="я" * 1000)
        self.assertLess(len(deadman.stale_why(self.file, NOW)), 300)


class Repeat(unittest.TestCase):
    def test_раз_в_12_часов_с_допуском_на_дрожание_cron(self):
        self.assertTrue(deadman.due(None, NOW), "тревоги ещё не было")
        self.assertFalse(deadman.due(NOW - 1 * HOUR, NOW), "через час после тревоги молчим")
        self.assertFalse(deadman.due(NOW - 11 * HOUR, NOW))
        self.assertTrue(deadman.due(NOW - 11.5 * HOUR, NOW), "слот :17 мог прийти на минуту раньше 12 часов")
        self.assertTrue(deadman.due(NOW - 13 * HOUR, NOW))

    def test_время_в_будущем_не_глушит_навсегда(self):
        self.assertTrue(deadman.due(NOW + 5 * HOUR, NOW), "часы сбились — лучше лишняя тревога, чем тишина")


class Send(unittest.TestCase):
    ENV = {"HUB_BOT_TOKEN": "h", "HUB_CHAT_ID": "-100", "HUB_TOPIC_SMM": "9",
           "ZLATOUM_SMM_BOT_TOKEN": "d", "ZLATOUM_SMM_EDITOR_ID": "42"}

    def run_send(self, env, fail=()):
        calls = []

        def fake_post(token, chat, thread, text):
            calls.append((token, chat, thread))
            return token not in fail

        original, deadman.post = deadman.post, fake_post
        try:
            return deadman.send("текст", env), calls
        finally:
            deadman.post = original

    def test_сначала_штаб_в_тему_smm_и_на_этом_всё(self):
        via, calls = self.run_send(self.ENV)
        self.assertEqual((via, calls), ("штаб", [("h", "-100", "9")]))

    def test_штаб_не_принял_запасной_путь_личка(self):
        via, calls = self.run_send(self.ENV, fail=("h",))
        self.assertEqual((via, calls), ("личка", [("h", "-100", "9"), ("d", "42", "")]))

    def test_секретов_штаба_ещё_нет_идём_в_личку_как_раньше(self):
        env = {k: v for k, v in self.ENV.items() if not k.startswith("HUB_")}
        via, calls = self.run_send(env)
        self.assertEqual((via, calls), ("личка", [("d", "42", "")]))

    def test_тема_не_задана_штаб_всё_равно_работает_без_темы(self):
        env = {**self.ENV, "HUB_TOPIC_SMM": ""}
        via, calls = self.run_send(env)
        self.assertEqual((via, calls), ("штаб", [("h", "-100", "")]))

    def test_не_доставлено_никуда(self):
        via, calls = self.run_send(self.ENV, fail=("h", "d"))
        self.assertIsNone(via)
        self.assertEqual(len(calls), 2)
        via, calls = self.run_send({})
        self.assertIsNone(via)
        self.assertEqual(calls, [])


class Main(unittest.TestCase):
    """Весь проход: пульс → решение → отправка → state-файл и GITHUB_OUTPUT."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pulse = self.tmp / "heartbeat.json"
        self.state = self.tmp / ".deadman" / "alert.json"
        self.out = self.tmp / "out"
        self.sent = []
        self.deliver = True
        self.env = {"HUB_BOT_TOKEN": "h", "HUB_CHAT_ID": "-100", "HUB_TOPIC_SMM": "9", "GITHUB_OUTPUT": str(self.out)}
        self._orig_post = deadman.post
        deadman.post = lambda token, chat, thread, text: self.sent.append(text) or self.deliver

    def tearDown(self):
        deadman.post = self._orig_post

    def run_main(self, now=NOW):
        self.out.write_text("", encoding="utf-8")
        deadman.main(self.env, now=now, pulse=self.pulse, state=self.state)
        return dict(line.split("=", 1) for line in self.out.read_text(encoding="utf-8").splitlines())

    def test_свежий_пульс_ничего_не_шлёт_и_state_не_трогает(self):
        pulse(self.pulse, 2)
        self.assertEqual(self.run_main()["state"], "ok")
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state.exists())

    def test_ровно_одно_сообщение_а_повторный_запуск_в_тот_же_час_молчит(self):
        pulse(self.pulse, 30)
        self.assertEqual(self.run_main()["state"], "alerted")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Контент-завод молчит", self.sent[0])
        self.assertIn("30 ч", self.sent[0])
        self.assertEqual(self.run_main(NOW + 60)["state"], "silent")
        self.assertEqual(self.run_main(NOW + 3 * HOUR)["state"], "silent")
        self.assertEqual(len(self.sent), 1)

    def test_через_12_часов_напоминает_снова(self):
        pulse(self.pulse, 30)
        self.run_main()
        self.assertEqual(self.run_main(NOW + 12 * HOUR)["state"], "alerted")
        self.assertEqual(len(self.sent), 2)

    def test_не_доставили_state_не_пишем_и_через_час_пробуем_снова(self):
        pulse(self.pulse, 30)
        self.deliver = False
        self.assertEqual(self.run_main()["state"], "failed")
        self.assertFalse(self.state.exists())
        self.deliver = True
        self.assertEqual(self.run_main(NOW + HOUR)["state"], "alerted")
        self.assertEqual(len(self.sent), 2)

    def test_пульс_ожил_и_снова_замолчал_тревога_сразу(self):
        pulse(self.pulse, 30)
        self.run_main()
        pulse(self.pulse, 1)
        self.assertEqual(self.run_main(NOW + 2 * HOUR)["state"], "ok")
        pulse(self.pulse, 40)  # через двое суток: прошлая тревога давно старше 12 часов
        self.assertEqual(self.run_main(NOW + 48 * HOUR)["state"], "alerted")

    def test_битый_state_это_нет_state(self):
        pulse(self.pulse, 30)
        self.state.parent.mkdir()
        self.state.write_text("мусор", encoding="utf-8")
        self.assertEqual(self.run_main()["state"], "alerted")

    def test_без_GITHUB_OUTPUT_не_падает(self):
        pulse(self.pulse, 2)
        env = {k: v for k, v in self.env.items() if k != "GITHUB_OUTPUT"}
        self.assertEqual(deadman.main(env, now=NOW, pulse=self.pulse, state=self.state), 0)


class Files(unittest.TestCase):
    def test_в_репозитории_нет_секретов(self):
        import re
        for path in [ROOT / ".github" / "deadman.py", ROOT / ".github" / "workflows" / "deadman.yml", ROOT / "README.md"]:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\d{8,10}:[A-Za-z0-9_-]{30,}", text), f"токен бота в {path.name}")
            self.assertNotIn("-100", text.replace("${{", ""), f"id чата в {path.name}")

    def test_воркфлоу_берёт_секреты_из_secrets_и_запоминает_тревогу_кэшем(self):
        text = (ROOT / ".github" / "workflows" / "deadman.yml").read_text(encoding="utf-8")
        for name in ("HUB_BOT_TOKEN", "HUB_CHAT_ID", "HUB_TOPIC_SMM", "ZLATOUM_SMM_BOT_TOKEN", "ZLATOUM_SMM_EDITOR_ID"):
            self.assertIn(f"secrets.{name}", text)
        self.assertIn("actions/cache/restore", text)
        self.assertIn("actions/cache/save", text)
        self.assertIn("python3 .github/deadman.py", text)


if __name__ == "__main__":
    unittest.main()
