# chc-pipeline — локальный оркестратор реализации

Оркестратор убирает ручную пересылку контекста между Cursor и Claude Code:
**Linear → agent-ready preflight → sprint contract → `claude -p` в git worktree → проверки → статус Review и комментарий**.

Проектно-агностичный: параметры целевого репо, Linear-команды и фильтров — в [`projects.json`](projects.json).

Скрипт: [`orchestrate.py`](orchestrate.py) — только стандартная библиотека Python, без внешних зависимостей.

## Что нужно на машине

- Python 3.9+
- Установленный и залогиненный [Claude Code](https://claude.com/claude-code) CLI (`claude`)
- git
- Свой `LINEAR_API_KEY` (см. ниже)

## Быстрый старт

```bash
git clone <этот-репо> chc-pipeline
cd chc-pipeline

# 1. Свой ключ Linear
export LINEAR_API_KEY="lin_api_..."   # или положи в ~/.dev-env/credentials/linear.json

# 2. Путь к git-клону целевого проекта (имя env берётся из projects.json → repo_env)
export CHC_BROWSER_REPO="/путь/к/твоему/репо"

# 3. Список доступных пресетов
python3 orchestrate.py --list-projects

# 4. Сухой прогон — посмотреть следующий Todo без мутаций
python3 orchestrate.py run --project chc-browser --dry-run

# 5. Полный цикл
./chc-pipeline run --project chc-browser

# Конкретный тикет
./chc-pipeline run --project chc-browser --issue CHC-694
```

## Настрой под себя

`projects.json` содержит пресеты под конкретный Linear-воркспейс. **Подставь свои значения:**

| Поле | Где взять |
|------|-----------|
| `linear_team_id` | UUID команды Linear |
| `linear_state_ids` | UUID статусов: `todo`, `in_progress`, `review`, `blocked` |
| `linear_project_substring` | подстрока в имени Linear-проекта для отбора тикетов |
| `repo_env` | имя env-переменной с путём к git-клону |
| `base_branch` | базовая ветка worktree (по умолчанию `main`) |

UUID команды/статусов можно вытащить через Linear API или GraphQL-эксплорер. Если работаешь в том же воркспейсе — значения из примеров подойдут как есть.

## Что появляется в целевом git-репо

| Путь | Назначение |
|------|------------|
| `.chc-pipeline/state.json` | Текущая фаза, проект, тикет, worktree, ветка |
| `.chc-pipeline/artifacts/<ISSUE-KEY>/` | Контракт, промпты, ответы Claude, отчёты валидации |
| `.chc-pipeline/last-memory-suggestion.json` | Черновик для memory_save |
| `../.chc-pipeline-worktrees/<ISSUE-KEY>` | Отдельный worktree на тикет (рядом с репо) |

Добавь в `.gitignore` целевого репо строку `.chc-pipeline/`.

## Переменные окружения

| Переменная | По умолчанию | Смысл |
|------------|--------------|--------|
| `LINEAR_API_KEY` | — | Ключ Linear API (или `~/.dev-env/credentials/linear.json`) |
| `CHC_CLAUDE_MODEL` | `opus` | Модель для `claude -p` |
| `CHC_CLAUDE_EFFORT` | `xhigh` | `--effort` |
| `CHC_CLAUDE_OUTPUT_FORMAT` | `text` | `text` или `json` |
| `CHC_PIPELINE_MAX_ROUNDS` | `3` | Попыток Claude при FAIL валидации |
| `CHC_PIPELINE_TEST_CMD` | пусто | Shell-команда в корне worktree |
| `CHC_MEMORY_SAVE_CMD` | пусто | Hook после успеха; env `CHC_MEMORY_JSON` |
| `CHC_REMOTE_EXECUTOR_CMD` | пусто | Для remote-only тикетов из пресета |

Проектные env (`CHC_BROWSER_REPO`, `DUALIPAY_LEDGER_REPO` и т.п.) задаются в поле `repo_env` пресета.

## Команды

```bash
# Список проектов
python3 orchestrate.py --list-projects

# Сухой прогон без мутаций
python3 orchestrate.py run --project chc-browser --dry-run

# Полный цикл
./chc-pipeline run --project chc-browser

# Конкретный тикет
./chc-pipeline run --project chc-browser --issue CHC-694

# Только контракт, без записи в Linear
./chc-pipeline run --project chc-browser --issue CHC-694 \
  --stop-after-contract --skip-linear-mutations

# Локальная отладка без Linear
./chc-pipeline run --project chc-browser --issue CHC-694 --skip-linear-mutations

# Cleanup worktree
./chc-pipeline cleanup --project chc-browser --remove-worktree --delete-branch
```

### CLI-флаги, перебивающие пресет

| Флаг | Перебивает |
|------|------------|
| `--repo` | `repo_env` |
| `--linear-team` | `linear_team_id` |
| `--linear-project` | `linear_project_substring` |
| `--base-branch` | `base_branch` |
| `--all-projects` | отключает фильтр по имени Linear-проекта |

## Как это работает

1. Берёт следующий Todo из Linear (или указанный `--issue`).
2. Прогоняет agent-ready preflight, собирает sprint contract.
3. Создаёт отдельный git worktree под тикет.
4. Запускает `claude -p` с контрактом, до `CHC_PIPELINE_MAX_ROUNDS` попыток при FAIL.
5. При успехе — переводит тикет в Review и пишет комментарий; при FAIL после всех раундов — Blocked + комментарий.
6. Diff-ревью делается отдельно (например, в Cursor).

## Ограничения

- Один тикет за запуск.
- При FAIL после всех раундов — Blocked + комментарий в Linear.
- Нет встроенного MCP memory_save (только JSON-заготовка в `.chc-pipeline/last-memory-suggestion.json`).
