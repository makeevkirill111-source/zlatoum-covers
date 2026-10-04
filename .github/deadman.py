#!/usr/bin/env python3
"""Внешний сторож контент-завода: смотрит возраст heartbeat.json и при беде пишет в Telegram.

Запускает .github/workflows/deadman.yml раз в час. Сервер (zlatoum-smm/deploy/deadman.py) раз в сутки
кладёт сюда пульс; здесь решаем, пора ли бить тревогу, и не бьём её каждый час подряд.

  • беда — пульсу больше 26 часов или в нём ok=false;
  • первая тревога уходит в первом же запуске после этого, дальше напоминание раз в 12 часов,
    пока пульс не оживёт (слот cron приходит с дрожанием, поэтому порог 11,5 часа);
  • получатель — тема «smm» Штаба (секреты HUB_BOT_TOKEN, HUB_CHAT_ID, HUB_TOPIC_SMM). Штаб не принял
    или его секретов ещё нет — личка редактору SMM-ботом (ZLATOUM_SMM_BOT_TOKEN, ZLATOUM_SMM_EDITOR_ID);
  • «когда била последняя тревога» хранит кэш Actions: файл .deadman/alert.json. Пишем его только
    после доставленной тревоги — недоставленная повторится в следующем часовом запуске.

Выход для воркфлоу — строка state=… в $GITHUB_OUTPUT:
  ok       пульс свежий;
  silent   пульс плохой, но тревога была недавно — молчим;
  alerted  тревога доставлена (воркфлоу запомнит время и покраснеет: письмо от GitHub — второй канал);
  failed   тревога нужна, но не доставлена никуда (воркфлоу тоже краснеет).

Репозиторий публичный: токенов и id чатов в файлах нет, всё из secrets.
Тесты: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STALE_HOURS = 26  # серверный пульс раз в сутки + запас
REPEAT_HOURS = 11.5  # «раз в 12 ч» с допуском на дрожание часового cron
PULSE = Path("heartbeat.json")
STATE = Path(".deadman/alert.json")


def stale_why(pulse: Path, now: float) -> str | None:
    """None — пульс свежий и ok; иначе причина одной строкой."""
    if not pulse.exists():
        return "heartbeat.json ещё не появился"
    try:
        data = json.loads(pulse.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(data["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return "heartbeat.json не читается"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (now - stamp.timestamp()) / 3600
    ok = bool(data.get("ok", False))
    if age <= STALE_HOURS and ok:
        return None
    text = " / ".join(str(data.get("text", ""))[:200].splitlines())
    return f"пульсу {age:.0f} ч, ok={ok}. {text}".strip()


def due(last: float | None, now: float) -> bool:
    """Пора ли слать: тревоги не было, прошло достаточно времени или часы в state ушли в будущее."""
    return last is None or last > now or now - last >= REPEAT_HOURS * 3600


def post(token: str, chat: str, thread: str, text: str) -> bool:
    """Одно сообщение в Telegram; токен идёт в curl через stdin, не в аргументах."""
    fields = ["--data-urlencode", f"chat_id={chat}", "--data-urlencode", f"text={text}"]
    if thread:
        fields += ["--data-urlencode", f"message_thread_id={thread}"]
    try:
        res = subprocess.run(
            ["curl", "-sS", "-m", "20", "-K", "-", "-o", "/dev/null", "-w", "%{http_code}", *fields],
            input=f'url = "https://api.telegram.org/bot{token}/sendMessage"\n',
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"curl: {exc}")
        return False
    print(f"telegram: HTTP {res.stdout.strip() or '-'}")
    return res.stdout.strip() == "200"


def send(text: str, env: dict[str, str]) -> str | None:
    """Штаб, а если не вышло — личка. Возвращает, куда ушло, или None."""
    if env.get("HUB_BOT_TOKEN") and env.get("HUB_CHAT_ID"):
        if post(env["HUB_BOT_TOKEN"], env["HUB_CHAT_ID"], env.get("HUB_TOPIC_SMM", ""), text):
            return "штаб"
        print("Штаб не принял — пробую личку")
    if env.get("ZLATOUM_SMM_BOT_TOKEN") and env.get("ZLATOUM_SMM_EDITOR_ID"):
        if post(env["ZLATOUM_SMM_BOT_TOKEN"], env["ZLATOUM_SMM_EDITOR_ID"], "", text):
            return "личка"
    return None


def read_last(state: Path) -> float | None:
    try:
        return float(json.loads(state.read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(env: dict[str, str] | None = None, now: float | None = None,
         pulse: Path = PULSE, state: Path = STATE) -> int:
    env = dict(os.environ) if env is None else env
    now = time.time() if now is None else now
    verdict, why = "ok", stale_why(pulse, now)
    if why is None:
        print("пульс свежий")
    elif not due(read_last(state), now):
        verdict = "silent"
        print(f"::notice::пульс плохой ({why}), но тревога уже была недавно — молчу")
    else:
        via = send(f"🚨 Контент-завод молчит: {why}", env)
        if via:
            verdict = "alerted"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"at": now}), encoding="utf-8")
            print(f"::error::Контент-завод молчит: {why} (тревога ушла: {via})")
        else:
            verdict = "failed"
            print(f"::error::Контент-завод молчит: {why} — тревога НЕ доставлена: нет рабочих секретов Штаба и лички")
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"state={verdict}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
