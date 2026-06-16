#!/usr/bin/env python3
"""
chc-pipeline — локальный полный цикл: Linear → контракт → Claude Code (-p) → валидация → Review.

Проектные параметры (репо, Linear team, фильтры) — из .agents/chc-pipeline/projects.json.
Зависимости: только стандартная библиотека Python 3.10+.
Секреты: LINEAR_API_KEY из окружения или ~/.dev-env/credentials/linear.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LINEAR_URL = "https://api.linear.app/graphql"
BACKLOG_STATE_NAME = "Backlog"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECTS_JSON = SCRIPT_DIR / "projects.json"

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}", re.I),
    re.compile(r"sk-[a-zA-Z0-9]{20,}", re.I),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[a-zA-Z0-9_\-]{20,}", re.I),
    re.compile(r"password\s*[:=]\s*['\"][^'\"]{8,}['\"]", re.I),
]

CONTENT_BEARING_RE = re.compile(
    r"\b("
    r"privacy policy|support page|landing page|app store metadata|public page|"
    r"copy|page content|legal text|privacy text|"
    r"политик[аи] конфиденциальности|страниц[ауы]|лендинг|текст страницы|"
    r"контент страницы|юридическ(?:ий|ого) текст|метаданн"
    r")\b",
    re.I,
)

SOURCE_OF_TRUTH_RE = re.compile(
    r"^##\s+("
    r"Готовый контент страницы|Финальный текст|Текст страницы|"
    r"Правила|Сценарии|Контракт интеграции|"
    r"Page content|Final copy|Source of truth"
    r")\s*$",
    re.I | re.M,
)

META_DELEGATION_RE = re.compile(
    r"(агент должен|agent should|implementation agent should|"
    r"написать консервативн|сформулировать текст|придумать текст|"
    r"write conservative copy|write .* copy)",
    re.I,
)


@dataclass
class IssueSnapshot:
    id: str
    identifier: str
    title: str
    description: str
    priority: int
    created_at: str
    state_name: str
    project_name: str | None


@dataclass
class ProjectPreset:
    name: str
    repo: Path
    linear_team_id: str
    todo_state_id: str
    in_progress_state_id: str
    review_state_id: str
    blocked_state_id: str
    linear_project_substring: str | None
    base_branch: str
    memory_project: str
    prompt_label: str
    remote_only_issues: set[str] = field(default_factory=set)
    remote_blocker_template: str | None = None
    backlog_filter: dict[str, Any] | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_projects_registry() -> dict[str, Any]:
    if not PROJECTS_JSON.is_file():
        raise RuntimeError(f"Не найден реестр проектов: {PROJECTS_JSON}")
    try:
        data = json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Ошибка чтения {PROJECTS_JSON}: {e}") from e
    if not isinstance(data, dict) or "projects" not in data:
        raise RuntimeError(f"Некорректный формат {PROJECTS_JSON}: нужны default_project и projects")
    return data


def list_project_names() -> list[str]:
    reg = load_projects_registry()
    projects = reg.get("projects") or {}
    return sorted(str(k) for k in projects.keys())


def resolve_preset(
    project_name: str,
    *,
    repo_override: str | None = None,
    linear_team_override: str | None = None,
    linear_project_override: str | None = None,
    base_branch_override: str | None = None,
) -> ProjectPreset:
    reg = load_projects_registry()
    projects = reg.get("projects") or {}
    if project_name not in projects:
        available = ", ".join(sorted(projects.keys()))
        raise RuntimeError(f"Проект «{project_name}» не найден в {PROJECTS_JSON}. Доступны: {available}")

    cfg = projects[project_name]
    if not isinstance(cfg, dict):
        raise RuntimeError(f"Пресет «{project_name}» должен быть объектом")

    repo_path: Path | None = None
    if repo_override:
        repo_path = Path(repo_override).expanduser().resolve()
    else:
        repo_env = str(cfg.get("repo_env") or "").strip()
        if repo_env:
            env_val = os.environ.get(repo_env, "").strip()
            if env_val:
                repo_path = Path(env_val).expanduser().resolve()
    if repo_path is None:
        raise RuntimeError(
            f"Путь к репозиторию не задан для проекта «{project_name}». "
            f"Передай --repo или задай env {cfg.get('repo_env', '(repo_env не указан в пресете)')}."
        )

    state_ids = cfg.get("linear_state_ids") or {}
    if not isinstance(state_ids, dict):
        raise RuntimeError(f"linear_state_ids в пресете «{project_name}» должен быть объектом")

    for key in ("todo", "in_progress", "review", "blocked"):
        if not state_ids.get(key):
            raise RuntimeError(f"В пресете «{project_name}» не задан linear_state_ids.{key}")

    remote_list = cfg.get("remote_only_issues") or []
    remote_set = {str(x).upper() for x in remote_list} if isinstance(remote_list, list) else set()

    backlog_filter = cfg.get("backlog_filter")
    if backlog_filter is not None and not isinstance(backlog_filter, dict):
        backlog_filter = None

    team_id = linear_team_override or str(cfg.get("linear_team_id") or "").strip()
    if not team_id:
        team_id = os.environ.get("CHC_LINEAR_TEAM_ID", "").strip()
    if not team_id:
        raise RuntimeError(f"linear_team_id не задан для проекта «{project_name}»")

    project_sub = linear_project_override
    if project_sub is None:
        project_sub = cfg.get("linear_project_substring")
        if project_sub is not None:
            project_sub = str(project_sub).strip() or None
        if project_sub is None:
            env_sub = os.environ.get("CHC_LINEAR_PROJECT_SUBSTRING", "").strip()
            project_sub = env_sub or None

    base_branch = base_branch_override or str(cfg.get("base_branch") or "main").strip()
    if not base_branch:
        base_branch = os.environ.get("CHC_PIPELINE_BASE_BRANCH", "main").strip() or "main"

    return ProjectPreset(
        name=project_name,
        repo=repo_path,
        linear_team_id=team_id,
        todo_state_id=str(state_ids["todo"]),
        in_progress_state_id=str(state_ids["in_progress"]),
        review_state_id=str(state_ids["review"]),
        blocked_state_id=str(state_ids["blocked"]),
        linear_project_substring=project_sub,
        base_branch=base_branch,
        memory_project=str(cfg.get("memory_project") or project_name),
        prompt_label=str(cfg.get("prompt_label") or project_name),
        remote_only_issues=remote_set,
        remote_blocker_template=str(cfg["remote_blocker_template"]).strip()
        if cfg.get("remote_blocker_template")
        else None,
        backlog_filter=backlog_filter,
    )


def _load_linear_api_key() -> str:
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if key:
        return key
    cred = Path.home() / ".dev-env" / "credentials" / "linear.json"
    if cred.is_file():
        try:
            data = json.loads(cred.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if isinstance(data, dict):
            return str(data.get("LINEAR_API_KEY") or data.get("api_key") or "").strip()
    return ""


def linear_request(api_key: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        LINEAR_URL,
        data=body,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"Linear HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Linear сеть: {e}") from e
    payload = json.loads(raw)
    if payload.get("errors"):
        raise RuntimeError("Linear GraphQL errors: " + json.dumps(payload["errors"], ensure_ascii=False)[:4000])
    return payload.get("data") or {}


def fetch_workflow_states(api_key: str, team_id: str) -> dict[str, str]:
    q = """
    query WorkflowStates($teamId: ID!) {
      workflowStates(filter: { team: { id: { eq: $teamId } } }, first: 100) {
        nodes {
          id
          name
        }
      }
    }
    """
    data = linear_request(api_key, q, {"teamId": team_id})
    nodes = (data.get("workflowStates") or {}).get("nodes") or []
    return {str(n.get("name") or ""): str(n.get("id") or "") for n in nodes if n.get("name") and n.get("id")}


def fetch_issues_by_state_id(
    api_key: str,
    team_id: str,
    state_id: str,
    project_substring: str | None,
) -> list[dict[str, Any]]:
    q = """
    query IssuesByState($teamId: ID!, $stateId: ID!) {
      issues(
        filter: { team: { id: { eq: $teamId } }, state: { id: { eq: $stateId } } }
        first: 250
      ) {
        nodes {
          id
          identifier
          title
          priority
          createdAt
          description
          state { id name type }
          project { id name }
        }
      }
    }
    """
    data = linear_request(api_key, q, {"teamId": team_id, "stateId": state_id})
    nodes = (data.get("issues") or {}).get("nodes") or []
    out = []
    for n in nodes:
        proj = (n.get("project") or {}) or {}
        pname = proj.get("name")
        if project_substring and pname and project_substring.lower() not in str(pname).lower():
            continue
        if project_substring and not pname:
            continue
        out.append(n)
    return out


def fetch_todo_issues(
    api_key: str,
    preset: ProjectPreset,
    project_substring: str | None,
) -> list[dict[str, Any]]:
    return fetch_issues_by_state_id(
        api_key, preset.linear_team_id, preset.todo_state_id, project_substring
    )


def _parse_issue_identifier(identifier: str) -> tuple[str, int] | None:
    m = re.match(r"^([A-Za-z]+)-(\d+)$", identifier.strip())
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2))


def get_issue_by_identifier(api_key: str, identifier: str, team_id: str) -> dict[str, Any] | None:
    parsed = _parse_issue_identifier(identifier)
    if parsed:
        num = parsed[1]
        q = """
        query ByNumber($teamId: ID!, $num: Float!) {
          issues(
            filter: { team: { id: { eq: $teamId } }, number: { eq: $num } }
            first: 5
          ) {
            nodes {
              id
              identifier
              title
              priority
              createdAt
              description
              state { id name type }
              project { id name }
            }
          }
        }
        """
        data = linear_request(api_key, q, {"teamId": team_id, "num": float(num)})
        nodes = (data.get("issues") or {}).get("nodes") or []
        ident_norm = identifier.strip().upper()
        for n in nodes:
            if str(n.get("identifier") or "").upper() == ident_norm:
                return n
        if len(nodes) == 1:
            return nodes[0]
    q_uuid = """
    query OneIssue($id: String!) {
      issue(id: $id) {
        id
        identifier
        title
        priority
        createdAt
        description
        state { id name type }
        project { id name }
      }
    }
    """
    data = linear_request(api_key, q_uuid, {"id": identifier.strip()})
    return data.get("issue")


def list_issue_comments(api_key: str, issue_id: str) -> list[dict[str, str]]:
    q = """
    query Comments($issueId: String!) {
      issue(id: $issueId) {
        comments(first: 50) {
          nodes {
            body
            createdAt
            user { name }
          }
        }
      }
    }
    """
    data = linear_request(api_key, q, {"issueId": issue_id})
    issue = data.get("issue") or {}
    nodes = ((issue.get("comments") or {}).get("nodes")) or []
    comments = []
    for n in nodes:
        comments.append(
            {
                "createdAt": str(n.get("createdAt") or ""),
                "user": ((n.get("user") or {}) or {}).get("name") or "",
                "body": str(n.get("body") or ""),
            }
        )
    comments.sort(key=lambda c: c["createdAt"])
    return comments


def issue_update_state(api_key: str, issue_uuid: str, state_id: str) -> None:
    m = """
    mutation Update($id: String!, $stateId: String!) {
      issueUpdate(id: $id, input: { stateId: $stateId }) {
        success
        issue { identifier state { name } }
      }
    }
    """
    data = linear_request(api_key, m, {"id": issue_uuid, "stateId": state_id})
    upd = data.get("issueUpdate") or {}
    if not upd.get("success"):
        raise RuntimeError("issueUpdate не применился: " + json.dumps(upd, ensure_ascii=False))


def issue_add_comment(api_key: str, issue_uuid: str, body: str) -> None:
    m = """
    mutation Comment($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
        comment { id }
      }
    }
    """
    data = linear_request(api_key, m, {"issueId": issue_uuid, "body": body})
    cr = data.get("commentCreate") or {}
    if not cr.get("success"):
        raise RuntimeError("commentCreate не применился: " + json.dumps(cr, ensure_ascii=False))


def pick_next_issue(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not nodes:
        return None

    def sort_key(n: dict[str, Any]) -> tuple[int, float, str]:
        pr = int(n.get("priority") or 0)
        if pr <= 0:
            pr = 999
        created = str(n.get("createdAt") or "")
        return (pr, 0.0, created)

    ranked = sorted(nodes, key=sort_key)
    return ranked[0]


def _milestone_step_from_title(title: str, title_regex: str) -> tuple[int, int] | None:
    m = re.match(title_regex, title.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def make_backlog_picker(filter_cfg: dict[str, Any] | None) -> Callable[[list[dict[str, Any]]], dict[str, Any] | None]:
    if not filter_cfg:
        return lambda nodes: None

    title_regex = str(filter_cfg.get("title_regex") or r"^M(\d+)\.(\d+)\b")
    phase_lt = int(filter_cfg.get("phase_lt", 9))
    exclude_ids = {str(x).upper() for x in (filter_cfg.get("exclude_ids") or [])}
    exclude_markers = tuple(str(x).lower() for x in (filter_cfg.get("exclude_title_markers") or []))

    def is_candidate(n: dict[str, Any]) -> bool:
        identifier = str(n.get("identifier") or "").upper()
        title = str(n.get("title") or "")
        title_lower = title.lower()
        if identifier in exclude_ids:
            return False
        if exclude_markers and any(marker in title_lower for marker in exclude_markers):
            return False
        milestone = _milestone_step_from_title(title, title_regex)
        if not milestone:
            return False
        phase, _step = milestone
        return phase < phase_lt

    def pick(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [n for n in nodes if is_candidate(n)]
        if not candidates:
            return None

        def sort_key(n: dict[str, Any]) -> tuple[int, int, int, str]:
            phase, step = _milestone_step_from_title(str(n.get("title") or ""), title_regex) or (999, 999)
            pr = int(n.get("priority") or 0)
            if pr <= 0:
                pr = 999
            return (phase, step, pr, str(n.get("createdAt") or ""))

        return sorted(candidates, key=sort_key)[0]

    return pick


def issue_to_snapshot(n: dict[str, Any]) -> IssueSnapshot:
    st = (n.get("state") or {}) or {}
    proj = (n.get("project") or {}) or {}
    return IssueSnapshot(
        id=str(n["id"]),
        identifier=str(n["identifier"]),
        title=str(n.get("title") or ""),
        description=str(n.get("description") or ""),
        priority=int(n.get("priority") or 0),
        created_at=str(n.get("createdAt") or ""),
        state_name=str(st.get("name") or ""),
        project_name=proj.get("name"),
    )


def readiness_blockers(issue: IssueSnapshot) -> list[str]:
    text = f"{issue.title}\n\n{issue.description}".strip()
    blockers: list[str] = []
    if not issue.description.strip():
        blockers.append("Описание тикета пустое.")

    is_content_bearing = bool(CONTENT_BEARING_RE.search(text))
    has_source_of_truth = bool(SOURCE_OF_TRUTH_RE.search(issue.description))
    has_meta_delegation = bool(META_DELEGATION_RE.search(issue.description))

    if is_content_bearing and not has_source_of_truth:
        blockers.append(
            "Content-bearing тикет без source-of-truth секции. Добавь `## Готовый контент страницы`, "
            "`## Финальный текст`, `## Правила`, `## Сценарии` или аналогичный раздел с самим содержанием."
        )
    if has_meta_delegation and not has_source_of_truth:
        blockers.append(
            "Тикет делегирует смысл implementation-агенту (`агент должен написать/сформулировать`), "
            "но не содержит готового контента или точных правил."
        )

    placeholder_re = re.compile(r"\b(TBD|TODO|to be defined|подробности уточним)\b", re.I)
    if placeholder_re.search(issue.description):
        blockers.append("В описании есть placeholder (`TBD`/`TODO`/`подробности уточним`).")

    return blockers


def build_sprint_contract(issue: IssueSnapshot, comments: list[dict[str, str]]) -> str:
    comments_md = "\n\n".join(
        f"**{c['createdAt']}** — {c['user'] or 'unknown'}\n\n{c['body']}" for c in comments if c["body"].strip()
    )
    if not comments_md.strip():
        comments_md = "_Комментариев нет._"
    return f"""SPRINT CONTRACT — {issue.identifier}

## Тикет
- **Заголовок:** {issue.title}
- **Проект:** {issue.project_name or '—'}
- **Приоритет Linear:** {issue.priority}

## Описание (из Linear)
{issue.description or '_Пусто_'}

## Комментарии Linear (контекст / развилки)
{comments_md}

## Scope (заполни перед первым запуском или оставь как вывод из описания)
1. Конкретные изменения по файлам/модулям — по описанию тикета выше.

## Out of scope
- Всё, что не следует из описания и комментариев.

## Testable criteria
| # | Критерий | Как проверить | Ожидаемый результат |
|---|----------|---------------|---------------------|
| 1 | Из описания / чеклистов | ревью diff / команды из репо | по тикету |

## Технический подход
- Следовать существующим паттернам репозитория; не расширять scope.

## Риски
- Зафиксировать в комментарии Linear при блокере.
"""


def pipeline_dir(repo: Path) -> Path:
    return repo / ".chc-pipeline"


def worktrees_root(repo: Path) -> Path:
    return repo.parent / ".chc-pipeline-worktrees"


def save_state(repo: Path, data: dict[str, Any]) -> None:
    d = pipeline_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state(repo: Path) -> dict[str, Any] | None:
    p = pipeline_dir(repo) / "state.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        capture_output=True,
    )


def ensure_clean_main(repo: Path, base_branch: str) -> None:
    st = run_git(repo, "status", "--porcelain")
    if st.stdout.strip():
        raise RuntimeError("Репозиторий не чистый. Закоммить или спрячь изменения перед pipeline.")
    run_git(repo, "rev-parse", "--verify", base_branch)


def create_worktree(repo: Path, issue_key: str, base_branch: str) -> tuple[Path, str]:
    root = worktrees_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    branch = f"pipeline/{issue_key}"
    path = root / issue_key
    exists = run_git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
    if exists.returncode == 0:
        branch = f"pipeline/{issue_key}-{int(time.time())}"
    if path.exists():
        raise RuntimeError(f"Путь worktree уже существует: {path}")
    cp = run_git(
        repo,
        "worktree",
        "add",
        "-b",
        branch,
        str(path),
        base_branch,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"git worktree add: {cp.stderr or cp.stdout}")
    return path, branch


def remove_worktree(main_repo: Path, wt_path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "remove", "--force", str(wt_path)],
        check=False,
        text=True,
        capture_output=True,
    )


def build_executor_prompt(
    *,
    issue: IssueSnapshot,
    prompt_label: str,
    contract: str,
    round_idx: int,
    max_rounds: int,
    feedback: str | None,
) -> str:
    fb = ""
    if feedback:
        fb = f"\n\n## Обратная связь с прошлой попытки (исправить всё)\n{feedback}\n"
    return f"""Ты — исполнитель по тикету Linear {issue.identifier} ({prompt_label}). Рабочая копия — текущий каталог (git worktree).

## Sprint contract (истина для scope)
{contract}
{fb}

## Инструкции
1. Прочитай контракт и сделай минимальные изменения под scope.
2. Можно коммитить в текущую ветку worktree осмысленными коммитами (рекомендуется один коммит с телом сообщения с ID тикета).
3. Не трогай несвязанные модули.
4. В конце ответа ОБЯЗАТЕЛЬНО блоки в точных заголовках ниже (для оркестратора).

## Формат итога (строго)
### FILES_CHANGED
- путь/к/файлу (кратко что)

### WHAT_WAS_DONE
Кратко по пунктам.

### ACCEPTANCE_CRITERIA_STATUS
- [ ] или [x] каждый критерий из контракта — пояснение.

### POTENTIAL_ISSUES
Риски или пусто.

Раунд реализации: {round_idx}/{max_rounds}.
"""


def run_claude_print(
    cwd: Path,
    prompt: str,
    *,
    model: str,
    effort: str,
    output_format: str,
) -> str:
    inner = [
        "claude",
        "-p",
        prompt,
        "--print",
        "--output-format",
        output_format,
        "--model",
        model,
        "--effort",
        effort,
        "--permission-mode",
        "bypassPermissions",
    ]
    cmd = "exec " + " ".join(shlex.quote(x) for x in inner)
    proc = subprocess.run(
        ["/bin/bash", "-lc", cmd],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=3600,
    )
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        raise RuntimeError(f"claude завершился с кодом {proc.returncode}:\n{out[-8000:]}")
    return proc.stdout or ""


def extract_text_from_claude_output(raw: str, output_format: str) -> str:
    raw = raw.strip()
    if output_format == "json":
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(j, dict):
            for k in ("result", "output", "text", "message"):
                v = j.get(k)
                if isinstance(v, str) and v.strip():
                    return v
            return json.dumps(j, ensure_ascii=False)
    return raw


def git_diff_stat(repo: Path, base_ref: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), "diff", "--stat", base_ref],
        text=True,
        capture_output=True,
        check=False,
    )
    return (p.stdout or p.stderr or "").strip()


def git_diff_patch(repo: Path, base_ref: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), "diff", base_ref],
        text=True,
        capture_output=True,
        check=False,
    )
    return p.stdout or ""


def scan_secrets(patch: str) -> list[str]:
    hits: list[str] = []
    for i, line in enumerate(patch.splitlines(), 1):
        for pat in SECRET_PATTERNS:
            if pat.search(line):
                hits.append(f"строка {i}: {line[:200]}")
                break
    return hits


def run_optional_tests(repo: Path, cmd: str | None) -> tuple[bool, str]:
    if not cmd:
        return True, "тесты не заданы (CHC_PIPELINE_TEST_CMD пусто)"
    proc = subprocess.run(
        ["/bin/bash", "-lc", cmd],
        cwd=str(repo),
        text=True,
        capture_output=True,
        timeout=3600,
    )
    ok = proc.returncode == 0
    tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-12000:]
    return ok, tail


def remote_blocker_message(issue: IssueSnapshot, preset: ProjectPreset) -> str:
    if preset.remote_blocker_template:
        return preset.remote_blocker_template.format(identifier=issue.identifier)
    return (
        f"{issue.identifier} — remote-only задача. "
        "Локальный orchestrate.py не должен запускать её в обычном worktree. "
        "Настрой CHC_REMOTE_EXECUTOR_CMD или запусти на remote build host."
    )


def validate_round(
    wt: Path,
    base_ref: str,
    claude_text: str,
    test_cmd: str | None,
) -> tuple[bool, str]:
    reasons: list[str] = []
    patch = git_diff_patch(wt, base_ref)
    stat = git_diff_stat(wt, base_ref)
    if not patch.strip():
        reasons.append("Пустой diff относительно базы — нет изменений.")
    hits = scan_secrets(patch)
    if hits:
        reasons.append("Возможные секреты в diff:\n" + "\n".join(hits[:20]))
    if "### FILES_CHANGED" not in claude_text:
        reasons.append("В ответе Claude нет секции ### FILES_CHANGED.")
    ok_test, test_log = run_optional_tests(wt, test_cmd)
    if not ok_test:
        reasons.append("Тесты не прошли (CHC_PIPELINE_TEST_CMD):\n" + test_log)
    ok = not reasons
    report = f"## diff --stat\n{stat or '(пусто)'}\n\n## проверки\n"
    if ok:
        report += "OK: есть diff, формат ответа, секреты не найдены"
        if test_cmd:
            report += ", тесты прошли"
        report += ".\n"
    else:
        report += "\n".join(reasons)
    report += f"\n\n## лог тестов (хвост)\n{test_log if test_cmd else '(нет)'}\n"
    return ok, report


def write_memory_stub(repo: Path, issue: IssueSnapshot, memory_project: str, summary: str) -> None:
    d = pipeline_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    suggestion = {
        "hint": "Вызови memory_save через MCPProxy в Cursor (см. .cursorrules), если нужно зафиксировать итог.",
        "memory_type": "Decision",
        "importance": 0.75,
        "project": memory_project,
        "content": f"Pipeline: {issue.identifier} — {issue.title}. {summary[:1500]}",
    }
    path = d / "last-memory-suggestion.json"
    path.write_text(
        json.dumps(suggestion, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    hook = os.environ.get("CHC_MEMORY_SAVE_CMD", "").strip()
    if hook:
        env = {**os.environ, "CHC_MEMORY_JSON": str(path)}
        proc = subprocess.run(
            ["/bin/bash", "-lc", hook],
            cwd=str(repo),
            text=True,
            capture_output=True,
            env=env,
            timeout=120,
        )
        if proc.returncode != 0:
            print(
                f"CHC_MEMORY_SAVE_CMD завершился с кодом {proc.returncode}:\n"
                f"{(proc.stdout or '') + (proc.stderr or '')}"[-4000:],
                file=sys.stderr,
            )


def cmd_run(args: argparse.Namespace) -> int:
    api_key = _load_linear_api_key()
    if not api_key:
        print("Нет LINEAR_API_KEY (env или ~/.dev-env/credentials/linear.json).", file=sys.stderr)
        return 2

    reg = load_projects_registry()
    project_name = args.project or str(reg.get("default_project") or "").strip()
    if not project_name:
        print("Укажи --project или задай default_project в projects.json.", file=sys.stderr)
        return 2

    try:
        preset = resolve_preset(
            project_name,
            repo_override=args.repo or None,
            linear_team_override=args.linear_team or None,
            linear_project_override=args.linear_project if args.linear_project is not None else None,
            base_branch_override=args.base_branch or None,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    repo = preset.repo
    git_marker = repo / ".git"
    if not (git_marker.is_dir() or git_marker.is_file()):
        print(f"Не git-репозиторий: {repo}", file=sys.stderr)
        return 2

    pd = pipeline_dir(repo)
    pd.mkdir(parents=True, exist_ok=True)

    project_sub: str | None = preset.linear_project_substring
    if args.all_projects:
        project_sub = None

    base_branch = preset.base_branch
    model = os.environ.get("CHC_CLAUDE_MODEL", "opus")
    effort = os.environ.get("CHC_CLAUDE_EFFORT", "xhigh")
    output_format = os.environ.get("CHC_CLAUDE_OUTPUT_FORMAT", "text")
    max_rounds = int(os.environ.get("CHC_PIPELINE_MAX_ROUNDS", "3"))
    test_cmd = os.environ.get("CHC_PIPELINE_TEST_CMD", "").strip() or None

    pick_backlog = make_backlog_picker(preset.backlog_filter)

    issue_raw: dict[str, Any] | None = None
    if args.issue:
        issue_raw = get_issue_by_identifier(api_key, args.issue, preset.linear_team_id)
        if not issue_raw:
            print(f"Тикет не найден: {args.issue}", file=sys.stderr)
            return 1
        if args.dry_run:
            print(json.dumps(asdict(issue_to_snapshot(issue_raw)), indent=2, ensure_ascii=False))
            return 0
    else:
        todos = fetch_todo_issues(api_key, preset, project_sub)
        issue_raw = pick_next_issue(todos)
        picked_from_backlog = False
        if not issue_raw and preset.backlog_filter:
            states = fetch_workflow_states(api_key, preset.linear_team_id)
            backlog_id = states.get(BACKLOG_STATE_NAME)
            backlog: list[dict[str, Any]] = []
            if backlog_id:
                backlog = fetch_issues_by_state_id(
                    api_key, preset.linear_team_id, backlog_id, project_sub
                )
            issue_raw = pick_backlog(backlog)
            picked_from_backlog = bool(issue_raw)
        if not issue_raw:
            msg = "Нет подходящих тикетов в Todo"
            if preset.backlog_filter:
                msg += " или agent-ready Backlog"
            if project_sub and not args.all_projects:
                msg += f" (фильтр проекта: подстрока «{project_sub}»; сброс: --all-projects)"
            print(msg + ".", file=sys.stderr)
            return 1
        if picked_from_backlog:
            print(
                f"Todo пуст; выбран следующий agent-ready Backlog тикет: "
                f"{issue_raw.get('identifier')} — {issue_raw.get('title')}",
                file=sys.stderr,
            )
        if args.dry_run:
            snap = issue_to_snapshot(issue_raw)
            print(json.dumps(asdict(snap), indent=2, ensure_ascii=False))
            return 0

    issue = issue_to_snapshot(issue_raw)
    if not args.skip_linear_mutations and issue.state_name not in (
        "Todo",
        BACKLOG_STATE_NAME,
        "In Progress",
        "Blocked",
    ):
        print(
            f"Тикет в статусе «{issue.state_name}» — не запускаем мутации Linear. "
            f"Используй --skip-linear-mutations или возьми задачу из Todo/Backlog.",
            file=sys.stderr,
        )
        return 1

    art = pd / "artifacts" / issue.identifier
    art.mkdir(parents=True, exist_ok=True)
    blockers = readiness_blockers(issue)
    if blockers and not args.allow_unready_ticket:
        report = "\n".join(f"- {b}" for b in blockers)
        readiness = f"""# Readiness failed — {issue.identifier}

Pipeline stopped before sprint contract / implementation.

## Blockers
{report}

## Required action
Enrich the Linear ticket first. For content/spec tasks, write the actual source-of-truth content into the ticket instead of asking the implementation agent to invent it.

Bypass only for exceptional cases with `--allow-unready-ticket`.
"""
        (art / "readiness-report.md").write_text(readiness, encoding="utf-8")
        print(readiness, file=sys.stderr)
        return 1

    comments = list_issue_comments(api_key, issue.id)
    contract = build_sprint_contract(issue, comments)
    (art / "sprint-contract.md").write_text(contract, encoding="utf-8")

    if args.stop_after_contract:
        print(f"Контракт записан: {art / 'sprint-contract.md'}")
        save_state(
            repo,
            {
                "updated_at": _utc_now_iso(),
                "phase": "contract_only",
                "project": preset.name,
                "issue": asdict(issue),
            },
        )
        return 0

    ident_upper = issue.identifier.upper()
    if ident_upper in preset.remote_only_issues and not os.environ.get("CHC_REMOTE_EXECUTOR_CMD", "").strip():
        msg = remote_blocker_message(issue, preset)
        if not args.skip_linear_mutations:
            issue_update_state(api_key, issue.id, preset.in_progress_state_id)
            issue_update_state(api_key, issue.id, preset.blocked_state_id)
            issue_add_comment(
                api_key,
                issue.id,
                f"**chc-pipeline** STOP {_utc_now_iso()}\n\n{msg}",
            )
        save_state(
            repo,
            {
                "updated_at": _utc_now_iso(),
                "phase": "blocked_remote_only",
                "project": preset.name,
                "issue": asdict(issue),
                "blocker": msg,
            },
        )
        print(msg, file=sys.stderr)
        return 4

    if not args.skip_linear_mutations:
        issue_update_state(api_key, issue.id, preset.in_progress_state_id)
        issue_add_comment(
            api_key,
            issue.id,
            f"**chc-pipeline** старт {_utc_now_iso()} [{preset.name}]\n\n"
            "Локальный оркестратор взял тикет в работу.",
        )

    ensure_clean_main(repo, base_branch)
    wt_path, branch = create_worktree(repo, issue.identifier, base_branch)
    merge_base = run_git(wt_path, "merge-base", "HEAD", base_branch).stdout.strip()

    save_state(
        repo,
        {
            "updated_at": _utc_now_iso(),
            "phase": "implementing",
            "project": preset.name,
            "issue": asdict(issue),
            "worktree": str(wt_path),
            "branch": branch,
            "merge_base": merge_base,
        },
    )

    feedback: str | None = None
    last_claude_text = ""
    for rnd in range(1, max_rounds + 1):
        prompt = build_executor_prompt(
            issue=issue,
            prompt_label=preset.prompt_label,
            contract=contract,
            round_idx=rnd,
            max_rounds=max_rounds,
            feedback=feedback,
        )
        (art / f"round-{rnd}-prompt.txt").write_text(prompt, encoding="utf-8")
        print(f"=== Раунд {rnd}/{max_rounds}: запуск claude в {wt_path} ===", flush=True)
        raw_out = run_claude_print(
            wt_path,
            prompt,
            model=model,
            effort=effort,
            output_format=output_format,
        )
        last_claude_text = extract_text_from_claude_output(raw_out, output_format)
        (art / f"round-{rnd}-claude-output.txt").write_text(last_claude_text, encoding="utf-8")

        ok, vreport = validate_round(wt_path, merge_base, last_claude_text, test_cmd)
        (art / f"round-{rnd}-validation.md").write_text(vreport, encoding="utf-8")
        if ok:
            break
        feedback = vreport + "\n\nИсправь замечания и снова заполни секции FILES_CHANGED / ACCEPTANCE_CRITERIA_STATUS."
        if rnd == max_rounds and not args.skip_linear_mutations:
            issue_update_state(api_key, issue.id, preset.blocked_state_id)
            issue_add_comment(
                api_key,
                issue.id,
                f"**chc-pipeline** блокировка после {max_rounds} раундов\n\n{vreport[:12000]}",
            )
            save_state(
                repo,
                {
                    "updated_at": _utc_now_iso(),
                    "phase": "blocked",
                    "project": preset.name,
                    "issue": asdict(issue),
                    "worktree": str(wt_path),
                    "branch": branch,
                },
            )
            print(vreport)
            return 3

    if not args.skip_linear_mutations:
        summary = git_diff_stat(wt_path, merge_base) or "(нет stat)"
        issue_update_state(api_key, issue.id, preset.review_state_id)
        issue_add_comment(
            api_key,
            issue.id,
            f"**chc-pipeline** готово к ревью {_utc_now_iso()}\n\n```\n{summary}\n```\n\n"
            f"Worktree: `{wt_path}` ветка `{branch}`.\n\n### Ответ исполнителя (хвост)\n\n"
            f"{last_claude_text[-8000:]}",
        )

    write_memory_stub(
        repo,
        issue,
        preset.memory_project,
        summary=git_diff_stat(wt_path, merge_base) or last_claude_text[:500],
    )

    save_state(
        repo,
        {
            "updated_at": _utc_now_iso(),
            "phase": "review",
            "project": preset.name,
            "issue": asdict(issue),
            "worktree": str(wt_path),
            "branch": branch,
            "merge_base": merge_base,
        },
    )

    print("Готово: Linear → Review, артефакты в", art)
    print("Подсказка для memory:", pd / "last-memory-suggestion.json")
    if args.cleanup_worktree:
        remove_worktree(repo, wt_path)
        run_git(repo, "branch", "-D", branch, check=False)
        print("Worktree удалён (--cleanup-worktree).")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    reg = load_projects_registry()
    project_name = args.project or str(reg.get("default_project") or "").strip()
    try:
        preset = resolve_preset(project_name, repo_override=args.repo or None)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    repo = preset.repo
    st = load_state(repo) or {}
    wt = st.get("worktree")
    br = st.get("branch")
    if args.remove_worktree and wt:
        remove_worktree(repo, Path(wt))
    if args.delete_branch and br:
        run_git(repo, "branch", "-D", str(br), check=False)
    print("cleanup: ok")
    return 0


def cmd_list_projects(_args: argparse.Namespace) -> int:
    reg = load_projects_registry()
    default = reg.get("default_project", "")
    print(f"default_project: {default}\n")
    for name in list_project_names():
        cfg = (reg.get("projects") or {}).get(name) or {}
        repo_env = cfg.get("repo_env") or "—"
        team = cfg.get("linear_team_id") or "—"
        sub = cfg.get("linear_project_substring")
        sub_s = f", linear_project_substring={sub!r}" if sub else ""
        print(f"  {name}: repo_env={repo_env}, team={team}{sub_s}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--list-projects":
        return cmd_list_projects(argparse.Namespace())

    p = argparse.ArgumentParser(description="chc-pipeline orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="Полный цикл для следующего Todo или --issue")
    pr.add_argument(
        "--project",
        default="",
        help="Ключ проекта из projects.json (по умолчанию default_project)",
    )
    pr.add_argument("--repo", default="", help="Путь к git-клону (перебивает repo_env пресета)")
    pr.add_argument("--linear-team", default="", help="Linear team ID (перебивает пресет)")
    pr.add_argument(
        "--linear-project",
        default=None,
        help="Подстрока в имени Linear-проекта (перебивает пресет; пустая строка = без фильтра)",
    )
    pr.add_argument("--base-branch", default="", help="Базовая ветка для worktree")
    pr.add_argument("--issue", default="", help="Конкретный ключ, например CHC-694")
    pr.add_argument("--dry-run", action="store_true", help="Только показать следующий тикет, без мутаций")
    pr.add_argument("--all-projects", action="store_true", help="Не фильтровать по имени проекта Linear")
    pr.add_argument("--stop-after-contract", action="store_true", help="Остановиться после sprint-contract")
    pr.add_argument("--skip-linear-mutations", action="store_true", help="Не трогать Linear")
    pr.add_argument("--allow-unready-ticket", action="store_true", help="Пропустить agent-ready preflight")
    pr.add_argument("--cleanup-worktree", action="store_true", help="После успеха удалить worktree и ветку")
    pr.set_defaults(func=cmd_run)

    pc = sub.add_parser("cleanup", help="Убрать worktree/ветку по state.json")
    pc.add_argument("--project", default="", help="Ключ проекта из projects.json")
    pc.add_argument("--repo", default="", help="Путь к git-клону")
    pc.add_argument("--remove-worktree", action="store_true")
    pc.add_argument("--delete-branch", action="store_true")
    pc.set_defaults(func=cmd_cleanup)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
