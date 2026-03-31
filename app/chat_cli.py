from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.core.runtime_env import load_project_env
from app.core.session_store import (
    close_session,
    create_session,
    get_active_session,
    get_active_session_id,
    list_sessions,
    load_session,
    set_active_session_id,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = PROJECT_ROOT / ".runs"
CLI_INPUT_PATH = RUNS_DIR / "cli_input.json"


def parse_slash_command(text: str) -> Tuple[str, str]:
    raw = str(text or "").strip()
    if not raw.startswith("/"):
        return "", raw
    parts = raw.split(maxsplit=1)
    command = parts[0][1:].strip().lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    return command, argument


def write_json_payload(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def agent_cmd(extra_args: List[str]) -> Dict[str, Any]:
    cmd = [sys.executable, "-m", "app.agent", *extra_args, "--json-stdout"]
    env = dict(os.environ)
    env.setdefault("LANGSMITH_TRACING", "false")
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "agent execution failed")
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(proc.stderr.strip() or "agent returned empty output")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(proc.stderr.strip() or stdout) from exc


def build_runtime_agent_args(session_id: str, turn_type: str, user_request: str) -> List[str]:
    lmstudio_base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    model = os.environ.get("MODEL_NAME", "qwen/qwen3.5-9b")
    args = [
        "--user-request",
        user_request,
        "--session-id",
        session_id,
        "--turn-type",
        turn_type,
        "--lmstudio-base-url",
        lmstudio_base_url,
        "--model",
        model,
        "--disable-thinking",
        "--disable-llm-strategy",
        "--disable-llm-judge",
        "--retry-max-attempts",
        "2",
        "--llm-timeout-sec",
        "30",
        "--llm-max-retries",
        "1",
        "--planner-timeout-sec",
        "120",
        "--planner-max-retries",
        "2",
    ]
    return args


def run_initial_request(user_request: str, *, force_new_session: bool) -> Dict[str, Any]:
    title = user_request[:60]
    if force_new_session or not get_active_session_id(PROJECT_ROOT):
        session = create_session(PROJECT_ROOT, title=title, initial_intent={})
    else:
        session = create_session(PROJECT_ROOT, title=title, initial_intent={})
    return agent_cmd(build_runtime_agent_args(session["session_id"], "request", user_request))


def run_feedback(reason: str, *, session_id: str) -> Dict[str, Any]:
    write_json_payload(CLI_INPUT_PATH, {"reason": reason})
    return agent_cmd(
        build_runtime_agent_args(session_id, "feedback", reason)
        + ["--feedback-json-path", str(CLI_INPUT_PATH)]
    )


def run_selection(target: str, *, session_id: str) -> Dict[str, Any]:
    write_json_payload(
        CLI_INPUT_PATH,
        {
            "type": "option_selection",
            "target_option": target,
            "signals": [{"kind": "selection", "value": "select_option", "strength": "hard"}],
            "reason": "用户确认采用该方案。",
        },
    )
    return agent_cmd(
        build_runtime_agent_args(session_id, "selection", "选择方案")
        + [
            "--feedback-json-path",
            str(CLI_INPUT_PATH),
            "--selected-option-ref",
            target,
        ]
    )


def ensure_active_session(explicit_session_id: str = "") -> str:
    session_id = explicit_session_id or get_active_session_id(PROJECT_ROOT)
    if not session_id:
        raise RuntimeError("当前没有 active session，请先发起一次初始规划，或使用 /use <session_id>。")
    return session_id


def print_status() -> None:
    session = get_active_session(PROJECT_ROOT)
    if not session:
        print("当前没有 active session。")
        return
    selected = session.get("selected_option") or {}
    current = session.get("current_response") or {}
    recommended = current.get("recommended_option") or {}
    print(
        json.dumps(
            {
                "session_id": session.get("session_id"),
                "status": session.get("status"),
                "title": session.get("title"),
                "turn_count": session.get("turn_count"),
                "selected_option": selected.get("pickup_point"),
                "recommended_option": recommended.get("pickup_point"),
                "updated_at": session.get("updated_at"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def print_sessions() -> None:
    print(
        json.dumps(
            {
                "active_session_id": get_active_session_id(PROJECT_ROOT),
                "sessions": list_sessions(PROJECT_ROOT, limit=20),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def show_payload(payload: Dict[str, Any]) -> int:
    text = str(payload.get("natural_language_output", "") or "").strip()
    if not text and str(payload.get("status", "")) == "error":
        error_text = str(payload.get("error", "") or "")
        text = f"本次规划失败：{error_text}" if error_text else "本次规划失败。"
    if text:
        print(text)
    interrupt_flag = bool(payload.get("awaiting_feedback"))
    if interrupt_flag:
        print("可继续输入反馈，或使用 /select 2 选方案。")
    return 1 if str(payload.get("status", "")) == "error" else 0


def resolve_select_ref(raw: str) -> str:
    text = str(raw or "").strip()
    if text.isdigit():
        idx = int(text)
        if idx <= 0:
            return text
        if idx == 1:
            return "recommended"
        return f"alternative_{idx - 1}"
    return text


def handle_slash(command: str, argument: str) -> int:
    if command == "help":
        print(
            "\n".join(
                [
                    "/new <需求>  新建会话并规划",
                    "/status      查看当前会话状态",
                    "/sessions    查看最近会话",
                    "/use <id>    切换 active session",
                    "/close       关闭当前会话",
                    "/select <n|recommended|名称>  选定方案",
                    "/exit        退出交互模式",
                    "/quit        退出交互模式",
                    "/help        查看帮助",
                ]
            )
        )
        return 0
    if command in {"exit", "quit"}:
        raise SystemExit(0)
    if command == "status":
        print_status()
        return 0
    if command == "sessions":
        print_sessions()
        return 0
    if command == "use":
        if not argument:
            raise RuntimeError("请提供 session_id。")
        if not load_session(PROJECT_ROOT, argument):
            raise RuntimeError(f"找不到 session: {argument}")
        set_active_session_id(PROJECT_ROOT, argument)
        print(f"已切换到 session: {argument}")
        return 0
    if command == "close":
        session_id = ensure_active_session()
        close_session(PROJECT_ROOT, session_id)
        print(f"已关闭 session: {session_id}")
        return 0
    if command == "new":
        if not argument:
            raise RuntimeError("请在 /new 后提供新的规划需求。")
        return show_payload(run_initial_request(argument, force_new_session=True))
    if command == "select":
        session_id = ensure_active_session()
        return show_payload(run_selection(resolve_select_ref(argument), session_id=session_id))
    raise RuntimeError(f"未知命令: /{command}")


def interactive_loop() -> int:
    print("进入结伴规划交互模式。输入需求开始，输入 /help 查看命令。")
    while True:
        try:
            line = input("jieban> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            return 0
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return 0
        command, argument = parse_slash_command(line)
        try:
            if command:
                handle_slash(command, argument)
                continue
            active = get_active_session(PROJECT_ROOT)
            if active and int(active.get("turn_count", 0) or 0) > 0:
                show_payload(run_feedback(line, session_id=active["session_id"]))
            else:
                show_payload(run_initial_request(line, force_new_session=True))
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="结伴规划交互式 CLI")
    parser.add_argument("request", nargs="*")
    parser.add_argument("--new-session")
    parser.add_argument("--feedback")
    parser.add_argument("--select")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--sessions", action="store_true")
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--use-session")
    return parser


def main() -> int:
    load_project_env(PROJECT_ROOT, override=False)
    parser = build_parser()
    args = parser.parse_args()

    if args.use_session:
        if not load_session(PROJECT_ROOT, args.use_session):
            raise SystemExit(f"Error: unknown session {args.use_session}")
        set_active_session_id(PROJECT_ROOT, args.use_session)

    try:
        if args.status:
            print_status()
            return 0
        if args.sessions:
            print_sessions()
            return 0
        if args.close:
            session_id = ensure_active_session()
            close_session(PROJECT_ROOT, session_id)
            print(f"已关闭 session: {session_id}")
            return 0
        if args.new_session:
            return show_payload(run_initial_request(args.new_session, force_new_session=True))
        if args.feedback:
            return show_payload(run_feedback(args.feedback, session_id=ensure_active_session(args.use_session or "")))
        if args.select:
            return show_payload(run_selection(resolve_select_ref(args.select), session_id=ensure_active_session(args.use_session or "")))
        if args.request:
            return show_payload(run_initial_request(" ".join(args.request).strip(), force_new_session=True))
        return interactive_loop()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
