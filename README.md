# zlatoum-covers
Обложки постов канала @zlatoym_info. Telegram тянет превью по адресу, поэтому картинки длинных постов лежат здесь.

## Сторож контент-завода (deadman)

Сервер раз в сутки кладёт сюда `heartbeat.json` — пульс. `.github/workflows/deadman.yml` раз в час
смотрит его возраст: старше 26 часов или `ok=false` — тревога в Telegram. Первая уходит сразу, дальше
напоминание раз в 12 часов, пока пульс не оживёт; запуск с тревогой краснеет, и GitHub пишет владельцу
письмом (второй канал). Логика — `.github/deadman.py`, тесты — `python3 -m unittest discover -s tests`.

Секреты (Settings → Secrets and variables → Actions), значений в репозитории нет:
`HUB_BOT_TOKEN`, `HUB_CHAT_ID`, `HUB_TOPIC_SMM` — тема «smm» Штаба; `ZLATOUM_SMM_BOT_TOKEN`,
`ZLATOUM_SMM_EDITOR_ID` — запасной путь, личное сообщение, если Штаб не принял или его секретов нет.
