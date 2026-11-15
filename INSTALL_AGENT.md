# Установка НАРЕЗ: инструкция для ИИ-агента

Ты — агент (Claude Code, Codex или похожий), которого пользователь попросил установить НАРЕЗ на свой
компьютер. Работай по шагам ниже, по-русски объясняй пользователю, что делаешь.

## Правила

- **Перед установкой любой программы спроси разрешения** и назови, что ставишь и откуда (только официальные
  источники: `winget`, сайты Docker, Mozilla, Astral).
- Никогда не читай, не печатай и не отправляй содержимое `yt_cookies.txt`, `.env` и
  `docker-compose.override.yml` после того, как пользователь вписал туда свои данные. Это доступ к его аккаунтам.
- Ключи API не нужны. Не проси у пользователя ключи Gemini, Upload-Post, ElevenLabs или fal.
- Шаги, где нужен клик или перезагрузка, проси сделать пользователя и жди подтверждения.
- Если шаг не получился — покажи пользователю ошибку и предложи решение, не обходи проверку.

## Шаг 1. Где мы

Убедись, что терминал открыт в корне репозитория (там лежат `docker-compose.yml`, `start.ps1`, этот файл).
Определи систему: Windows, macOS или Linux. Ниже команды для Windows (PowerShell); на macOS и Linux
используй аналоги и `docker compose` напрямую.

## Шаг 2. Виртуализация и Docker

1. `docker version` — если команда не найдена или нет раздела Server:
   - Проверь виртуализацию: `(Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled`.
     Если `False`, попроси пользователя включить Intel VT-x / AMD SVM в BIOS.
   - С разрешения пользователя: `winget install -e --id Docker.DockerDesktop`.
   - Попроси перезагрузить ПК, запустить Docker Desktop, принять условия и дождаться «Engine running».
2. Повтори `docker version` и `docker compose version`, пока оба не ответят.
3. Если Docker Desktop просит обновить WSL: `wsl --update`.

## Шаг 3. Firefox и uv (для куки YouTube)

1. Firefox: проверь `Test-Path "$env:ProgramFiles\Mozilla Firefox\firefox.exe"`.
   Если нет — с разрешения: `winget install -e --id Mozilla.Firefox`.
2. uv: `uv --version`. Если нет — с разрешения: `winget install -e --id astral-sh.uv`, затем открой новый терминал.
3. Объясни пользователю: нужен **отдельный** аккаунт Google только для скачивания (YouTube может ограничить
   аккаунт за частые скачивания), и войти им в YouTube нужно **именно в Firefox** — Chrome, Edge и Яндекс
   шифруют куки так, что их не прочитать. Подожди, пока пользователь войдёт.

## Шаг 4. Запуск

```powershell
.\start.ps1
```

Скрипт создаст `docker-compose.override.yml` с режимом агента (если его нет), выгрузит куки из Firefox,
соберёт и запустит `backend` и `frontend`. Первая сборка — 20–40 минут и несколько гигабайт, предупреди
пользователя. Если PowerShell запрещает скрипты: `powershell -ExecutionPolicy Bypass -File .\start.ps1`.

Если куки не выгрузились (`FAIL: no YouTube cookies in Firefox`) — пользователь не вошёл в YouTube в Firefox.
Система всё равно работает, просто видео придётся загружать файлом.

## Шаг 5. Проверка

1. `docker compose ps` — `openshorts-backend` и `openshorts-frontend` в состоянии running.
2. `Invoke-RestMethod http://localhost:8000/api/config` — в ответе `agentClips: true`.
   Если `false` — нет `docker-compose.override.yml` с `AGENT_CLIPS=1`; создай его из
   `docker-compose.override.example.yml` и выполни `docker compose up -d backend frontend`.
3. Открой пользователю http://localhost:5175 (`Start-Process http://localhost:5175`).

## Шаг 6. Первая нарезка

Объясни пользователю: в интерфейсе вставить ссылку или загрузить файл, выбрать 9:16 и запустить. Когда
задача дойдёт до ожидания агента, в папке `output/<задача>/` появится `transcript.json`.

Тогда сделай нарезку сам:

1. Найди в `output/` самую новую папку, где есть `transcript.json` и нет `clips.json`.
2. Прочитай `transcript.json`: в `segments` есть `start`, `end`, `text` и `words` с секундами.
3. Выбери 6–10 моментов по 15–60 секунд: законченная мысль, эмоция, шутка или поворот; первые 2 секунды
   цепляют без разгона; начало и конец по границам слов.
4. Для каждого напиши заголовок до 100 символов и хук до 10 слов на языке видео.
5. Запиши `clips.json` в ту же папку одним действием, формат — в `README.md`, раздел «Как резать».
   `start` < `end`, оба внутри длины видео.

Клипы появятся на экране задачи в интерфейсе и в той же папке. Готово: расскажи пользователю, что дальше он может
просто писать тебе «нарежь последний стрим».
