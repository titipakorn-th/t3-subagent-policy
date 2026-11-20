#!/usr/bin/env python3
"""Expose CLIProxyAPI Antigravity models through T3's ACP process.

The bundled ACP server validates selected model IDs against its built-in list.
This shim advertises Argon, Hydrogen and Claude choices, translates their ACP selection
to a supported placeholder model, and rewrites only the selected model's
Gemini API request path before forwarding it to CLIProxyAPI.
"""
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

ARGON_MODELS = ("gemini-4-argon",)
HYDROGEN_MODELS = ("gemini-4-hydrogen",)
ORCHESTRATOR_MODELS = (
    "gemini-pro-orchestrator-g1-pro",
    "gemini-pro-orchestrator-argon",
    "gemini-pro-orchestrator",
)
FLASH_MODELS = ("gemini-3.8-flash",)
CLAUDE_MODELS = {
    "claude-opus-5-5": ("claude-opus-5-5-high", None),
    "claude-sonnet-5-5": ("claude-sonnet-5-5-high", None),
}
GPT_OSS_MODELS = ("gpt-oss-120b-medium",)

ALL_MANAGED_MODELS = (
    *ARGON_MODELS,
    *HYDROGEN_MODELS,
    *ORCHESTRATOR_MODELS,
    *FLASH_MODELS,
    *CLAUDE_MODELS.keys(),
    *GPT_OSS_MODELS,
)

PROXY_ARGON_MODEL = "gemini-4-argon-high"
PROXY_HYDROGEN_MODEL = "gemini-4-hydrogen-xhigh"
PROXY_ORCHESTRATOR_MODEL = "gemini-4-argon-high"
PROXY_FLASH_MODEL = "gemini-3.8-flash"
PROXY_GPT_OSS_MODEL = "gpt-oss-120b-medium"

PLACEHOLDER_MODEL = "gemini-3.8-flash-high"
CONFIG_PLACEHOLDER_MODEL = "gemini-3.8-flash"
THINKING_LEVELS = ("low", "medium", "high")
HYDROGEN_THINKING_LEVELS = (*THINKING_LEVELS, "xhigh")

T3_MCP_ENDPOINT_ENV = "T3_ACP_MCP_ENDPOINT"
T3_MCP_AUTHORIZATION_ENV = "T3_ACP_MCP_AUTHORIZATION"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

MODEL_EFFORT_LEVELS = {
    **dict.fromkeys(ARGON_MODELS, THINKING_LEVELS),
    **dict.fromkeys(HYDROGEN_MODELS, HYDROGEN_THINKING_LEVELS),
    **dict.fromkeys(ORCHESTRATOR_MODELS, HYDROGEN_THINKING_LEVELS),
    **dict.fromkeys(FLASH_MODELS, THINKING_LEVELS),
    **dict.fromkeys(CLAUDE_MODELS, THINKING_LEVELS),
    **dict.fromkeys(GPT_OSS_MODELS, THINKING_LEVELS),
}
MODEL_LEVEL_CHOICES = {
    f"{model}-{level}"
    for model, levels in MODEL_EFFORT_LEVELS.items()
    for level in levels
}
PROXY_MODELS = {
    "gemini-4-argon": (PROXY_ARGON_MODEL, None),
    "gemini-4-hydrogen": (PROXY_HYDROGEN_MODEL, None),
    "gemini-pro-orchestrator-g1-pro": (PROXY_ORCHESTRATOR_MODEL, None),
    "gemini-pro-orchestrator-argon": (PROXY_ORCHESTRATOR_MODEL, None),
    "gemini-pro-orchestrator": (PROXY_ORCHESTRATOR_MODEL, None),
    "gemini-3.8-flash": (PROXY_FLASH_MODEL, None),
    "gpt-oss-120b-medium": (PROXY_GPT_OSS_MODEL, None),
    **CLAUDE_MODELS,
    **{
        f"gemini-4-argon-{level}": (PROXY_ARGON_MODEL, level)
        for level in THINKING_LEVELS
    },
    **{
        f"gemini-4-hydrogen-{level}": (f"gemini-4-hydrogen-{level}", level)
        for level in HYDROGEN_THINKING_LEVELS
    },
    **{
        f"{model}-{level}": (PROXY_ORCHESTRATOR_MODEL, level)
        for model in ORCHESTRATOR_MODELS
        for level in HYDROGEN_THINKING_LEVELS
    },
    **{
        f"gemini-3.8-flash-{level}": (f"gemini-3.8-flash-{level}" if level == "high" else PROXY_FLASH_MODEL, level)
        for level in THINKING_LEVELS
    },
    **{
        f"gpt-oss-120b-medium-{level}": (PROXY_GPT_OSS_MODEL, level)
        for level in THINKING_LEVELS
    },
    **{
        f"{model}-{level}": (proxy_model, level)
        for model, (proxy_model, _default_level) in CLAUDE_MODELS.items()
        for level in THINKING_LEVELS
    },
}

# T3's ACP registry driver gives a "thought_level" option only to the model that was current
# when it probed (Flash), so other models lose it. A custom category makes it shared by all.
SHARED_THINKING_CATEGORY = "_thinking_level"
GEMINI_38_TIER_MODELS = {f"gemini-3.8-flash-{level}" for level in THINKING_LEVELS}
MODEL_NAMES = {
    "gemini-4-argon": "Gemini 4 Argon",
    "gemini-4-hydrogen": "Gemini 4 Hydrogen",
    "gemini-pro-orchestrator-g1-pro": "Orchestrator (Argon)",
    "gemini-pro-orchestrator-argon": "Orchestrator (Argon)",
    "gemini-pro-orchestrator": "Orchestrator (Argon)",
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
}
PRIMARY_MANAGED_MODELS = (
    "gemini-4-argon",
    "gemini-4-hydrogen",
    "gemini-pro-orchestrator-g1-pro",
    "gemini-3.8-flash",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "gpt-oss-120b-medium",
)
MODEL_CHOICES = {
    model: (
        MODEL_NAMES[model],
        f"{MODEL_NAMES[model]} routed through CLIProxyAPI.",
    )
    for model in PRIMARY_MANAGED_MODELS
}


def effort_levels_for(model):
    """Thinking levels exposed for this model."""
    if isinstance(model, str):
        for family, levels in MODEL_EFFORT_LEVELS.items():
            if model == family or model.startswith(family + "-"):
                return levels
    return THINKING_LEVELS


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.proxy_model = "gemini-4-argon"
        self.thinking_level = "medium"
        self.active_goal = None

    def select(self, model):
        with self.lock:
            if model:
                self.proxy_model = model
                if self.thinking_level not in effort_levels_for(model):
                    self.thinking_level = None

    def selected_proxy_model(self):
        with self.lock:
            return self.proxy_model

    def set_thinking_level(self, level):
        with self.lock:
            self.thinking_level = level if level in effort_levels_for(self.proxy_model) else None

    def selected_thinking_level(self):
        with self.lock:
            return self.thinking_level

    def set_goal(self, goal):
        with self.lock:
            self.active_goal = goal

    def get_goal(self):
        with self.lock:
            return self.active_goal

    def clear_goal(self):
        with self.lock:
            self.active_goal = None


def thinking_level_for(model):
    """Effort to send upstream for the model the client selected.

    Routed models (argon, claude) carry their level in PROXY_MODELS. Anything else is
    forwarded as-is, so the requested suffix is the only effort signal available:
    without it 'gemini-3.8-flash-low' silently ran at the upstream default.
    """
    if not isinstance(model, str):
        return None
    if model in PROXY_MODELS:
        return PROXY_MODELS[model][1]
    suffix = model.rsplit("-", 1)[-1]
    return suffix if suffix in THINKING_LEVELS else None


def apply_thinking_level(body, level):
    if body is None or not level:
        return body
    try:
        payload = json.loads(body)
        generation = payload.setdefault("generationConfig", {})
        thinking = generation.setdefault("thinkingConfig", {})
        thinking["thinkingLevel"] = level
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


SUBAGENT_DELEGATION_POLICY = (
    "\n\n[MANDATORY SUBAGENT DIRECTIVE]\n"
    "When asked to spawn or run a subagent, delegate, or child agent, DO NOT use internal "
    "subagent tools (they are disabled). ALWAYS use T3 Code's orchestrator tool `delegate_task` "
    "via `call_mcp_tool` with:\n"
    "- ServerName: \"t3-code\"\n"
    "- ToolName: \"delegate_task\"\n"
    "- Arguments: {\"task\": \"<detailed task prompt>\", \"title\": \"<short task title>\", \"role\": \"<implementation|research|review|test|general>\", \"mode\": \"wait\"}\n"
    "- toolAction: \"Delegating subagent\"\n"
    "- toolSummary: \"Delegate task to T3\"\n"
    "This ensures the child thread and task appear in the T3 Code UI and sidebar."
)

INTERNAL_SUBAGENT_TOOLS = {
    "invoke_subagent",
    "define_subagent",
    "manage_subagents",
    "default_api:invoke_subagent",
    "default_api:define_subagent",
    "default_api:manage_subagents",
}


def remove_internal_subagents(payload):
    """Remove internal subagent tools from tools.functionDeclarations so the model cannot spawn un-tracked subagents."""
    if not isinstance(payload, dict):
        return
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return
    for tool_group in tools:
        if not isinstance(tool_group, dict):
            continue
        decls = tool_group.get("functionDeclarations")
        if isinstance(decls, list):
            tool_group["functionDeclarations"] = [
                d for d in decls
                if not isinstance(d, dict) or d.get("name") not in INTERNAL_SUBAGENT_TOOLS
            ]


def clean_subagents_from_system_instruction(payload):
    """Remove <subagents>...</subagents> instructions that promote invoke_subagent."""
    if not isinstance(payload, dict):
        return
    sys_inst = payload.get("systemInstruction")
    if not isinstance(sys_inst, dict):
        return
    parts = sys_inst.get("parts")
    if not isinstance(parts, list):
        return
    for part in parts:
        if isinstance(part, dict) and "text" in part:
            text = part["text"]
            if "<subagents>" in text and "</subagents>" in text:
                part["text"] = re.sub(r"<subagents>.*?</subagents>", "", text, flags=re.DOTALL)


def build_goal_policy(goal_text):
    return (
        f"\n\n[MANDATORY ACTIVE GOAL DIRECTIVE]\n"
        f"The user has established an active session goal:\n"
        f"\"{goal_text}\"\n\n"
        f"Execution Requirements:\n"
        f"1. Break this goal down and execute each necessary step systematically.\n"
        f"2. Maintain task progress and verify your work.\n"
        f"3. Do NOT stop prematurely or hand back control with incomplete work unless blocked.\n"
        f"4. If and only when the goal has been fully accomplished, explicitly state the final outcome."
    )


def inject_goal_policy(body, goal_text):
    """Ensure active session goal is preserved across turns in system instructions."""
    if body is None or not goal_text:
        return body
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict) or "contents" not in payload:
            return body

        sys_inst = payload.setdefault("systemInstruction", {})
        if not isinstance(sys_inst, dict):
            return body
        parts = sys_inst.setdefault("parts", [])
        if not isinstance(parts, list):
            return body

        goal_directive = build_goal_policy(goal_text)
        # Avoid duplicate injection
        for part in parts:
            if isinstance(part, dict) and "[MANDATORY ACTIVE GOAL DIRECTIVE]" in part.get("text", ""):
                part["text"] = goal_directive
                return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        parts.append({"text": goal_directive})
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


def inject_subagent_policy(body):
    """Ensure subagent instruction favors T3 Code's delegate_task over invoke_subagent."""
    if body is None:
        return body
    try:
        is_dict = isinstance(body, dict)
        payload = body if is_dict else json.loads(body)
        if not isinstance(payload, dict) or "contents" not in payload:
            return body

        clean_subagents_from_system_instruction(payload)
        remove_internal_subagents(payload)

        sys_inst = payload.setdefault("systemInstruction", {})
        if not isinstance(sys_inst, dict):
            return body
        parts = sys_inst.setdefault("parts", [])
        if not isinstance(parts, list):
            return body

        for part in parts:
            if isinstance(part, dict) and "[MANDATORY SUBAGENT DIRECTIVE]" in part.get("text", ""):
                part["text"] = SUBAGENT_DELEGATION_POLICY
                return payload if is_dict else json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        parts.append({"text": SUBAGENT_DELEGATION_POLICY})
        return payload if is_dict else json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


COMMAND_TASK_EXECUTION_POLICY = (
    "\n\n[MANDATORY COMMAND & TASK CONTINUATION DIRECTIVE]\n"
    "When running commands under T3 Code:\n"
    "1. Never end a turn saying you are waiting for a command or background task to finish. Background task completion will NOT automatically start a new turn in T3 Code.\n"
    "2. If `run_command` backgrounds a command (returns a TaskId), you MUST immediately call `manage_task` with Action='status' and TaskId to monitor it until Status is DONE or ERROR.\n"
    "3. Once the task finishes, review the output, and immediately execute all subsequent delivery steps within the same turn (e.g., verifying tests, committing git changes, pushing branches, and calling T3 Code tools such as `pull_request_create`, `link_pull_request`, and `comment_on_issue`).\n"
    "4. Do NOT leave tasks half-done or yield control to the user prematurely when tests/builds finish."
)

TASK_ID_RE = re.compile(r"task id:\s*([^\s\r\n]+)", re.IGNORECASE)
LOG_URL_RE = re.compile(r"(Task logs are available at:\s*[^\r\n]+)", re.IGNORECASE)


def rewrite_background_task_string(text):
    if not isinstance(text, str):
        return text
    if "Tool is running as a background task with task id" not in text:
        return text
    if "YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS" not in text and "DO NOTHING ELSE" not in text:
        return text

    m = TASK_ID_RE.search(text)
    task_id = m.group(1) if m else "<task_id>"
    log_match = LOG_URL_RE.search(text)
    log_info = f"\n{log_match.group(1)}" if log_match else ""

    return (
        f"Tool is running as a background task with task id: {task_id}{log_info}\n\n"
        f"[T3 CODE CRITICAL INSTRUCTION]: You are running under T3 Code ACP. Background task "
        f"completions DO NOT automatically wake up this thread after ending a turn. "
        f"You MUST NOT end the turn waiting.\n"
        f"You must immediately poll `manage_task` with Action='status' and TaskId='{task_id}' "
        f"until the task status is DONE or ERROR. Once finished, inspect the command output "
        f"and proceed immediately to complete all remaining mandate tasks (verifying results, "
        f"committing changes, pushing branches, and calling T3 Code tools such as "
        f"`pull_request_create`, `link_pull_request`, and `comment_on_issue`)."
    )


def rewrite_response_obj(obj):
    if isinstance(obj, str):
        return rewrite_background_task_string(obj)
    if isinstance(obj, dict):
        return {k: rewrite_response_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rewrite_response_obj(x) for x in obj]
    return obj


def rewrite_background_tasks_in_contents(payload):
    """Scan contents for run_command background task responses and rewrite the instruction to poll instead of stopping."""
    if not isinstance(payload, dict):
        return
    contents = payload.get("contents")
    if not isinstance(contents, list):
        return
    for item in contents:
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "text" in part and isinstance(part["text"], str):
                part["text"] = rewrite_background_task_string(part["text"])
            fc_resp = part.get("functionResponse")
            if isinstance(fc_resp, dict):
                if "response" in fc_resp:
                    fc_resp["response"] = rewrite_response_obj(fc_resp["response"])


def clean_reactive_wakeups_from_system_instruction(payload):
    """Neutralize Antigravity instructions telling the model not to poll and to end turn on background tasks."""
    if not isinstance(payload, dict):
        return
    sys_inst = payload.get("systemInstruction")
    if not isinstance(sys_inst, dict):
        return
    parts = sys_inst.get("parts")
    if not isinstance(parts, list):
        return
    for part in parts:
        if isinstance(part, dict) and "text" in part:
            text = part["text"]
            text = re.sub(
                r"Commands that do not finish within [^\.]*?so do not poll their status[^\.]*\.",
                "Commands that do not finish within the wait time run as background tasks. Under T3 Code ACP, you must poll manage_task(Action='status', TaskId=...) until completion; do NOT end the turn waiting.",
                text,
            )
            text = re.sub(
                r"## Reactive Wakeup \(No Polling Needed\).*?The system will notify you when there is something to process\.",
                "## Task & Command Polling (Required under T3 Code ACP)\n\nUnder T3 Code ACP, background tasks DO NOT automatically wake up or resume this thread after ending a turn. You MUST NOT end the turn waiting or stop calling tools while a background task is running. You must poll `manage_task(Action='status', TaskId=...)` until the task is DONE or ERROR.",
                text,
                flags=re.DOTALL,
            )
            text = re.sub(
                r"This means you do NOT need to poll in a loop while waiting for messages or updates\..*?simply stop by calling no more tools\. The system will notify you when there is something to process\.",
                "Under T3 Code ACP, background tasks DO NOT automatically wake up this thread after ending a turn. You MUST NOT end the turn waiting. Poll manage_task(Action='status', TaskId=...) until completion.",
                text,
                flags=re.DOTALL,
            )
            part["text"] = text


def inject_task_continuation_policy(body):
    """Ensure background tasks are polled and completed within turn, avoiding premature turn exit."""
    if body is None:
        return body
    try:
        is_dict = isinstance(body, dict)
        payload = body if is_dict else json.loads(body)
        if not isinstance(payload, dict) or "contents" not in payload:
            return body

        clean_reactive_wakeups_from_system_instruction(payload)
        rewrite_background_tasks_in_contents(payload)

        sys_inst = payload.setdefault("systemInstruction", {})
        if not isinstance(sys_inst, dict):
            return body
        parts = sys_inst.setdefault("parts", [])
        if not isinstance(parts, list):
            return body

        for part in parts:
            if isinstance(part, dict) and "[MANDATORY COMMAND & TASK CONTINUATION DIRECTIVE]" in part.get("text", ""):
                part["text"] = COMMAND_TASK_EXECUTION_POLICY
                return payload if is_dict else json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        parts.append({"text": COMMAND_TASK_EXECUTION_POLICY})
        return payload if is_dict else json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


def rewrite_subagent_fc(data):
    """Rewrite any invoke_subagent or delegate_task function call into call_mcp_tool."""
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return False
    changed = False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            fc = part.get("functionCall")
            if not isinstance(fc, dict):
                continue
            name = fc.get("name")
            if name in ("invoke_subagent", "default_api:invoke_subagent"):
                args = fc.get("args") or {}
                subagents = args.get("Subagents") or []
                sub = subagents[0] if isinstance(subagents, list) and subagents else {}
                prompt = sub.get("Prompt", "") if isinstance(sub, dict) else ""
                role_str = (sub.get("Role") or "general").lower() if isinstance(sub, dict) else "general"
                role = "research" if "research" in role_str else ("review" if "review" in role_str else "implementation")
                fc["name"] = "call_mcp_tool"
                fc["args"] = {
                    "ServerName": "t3-code",
                    "ToolName": "delegate_task",
                    "Arguments": {
                        "task": prompt,
                        "title": (sub.get("Role") or "Subagent Task") if isinstance(sub, dict) else "Subagent Task",
                        "role": role,
                        "mode": "wait",
                    },
                    "toolAction": "Delegating subagent",
                    "toolSummary": "Delegate task to T3",
                }
                changed = True
            elif name in ("delegate_task", "default_api:delegate_task"):
                args = fc.get("args") or {}
                fc["name"] = "call_mcp_tool"
                fc["args"] = {
                    "ServerName": "t3-code",
                    "ToolName": "delegate_task",
                    "Arguments": args,
                    "toolAction": "Delegating subagent",
                    "toolSummary": "Delegate task to T3",
                }
                changed = True
    return changed


def rewrite_sse_chunk(line_text):
    if not isinstance(line_text, str):
        return line_text
    stripped = line_text.strip()
    if not stripped:
        return ""
    if not line_text.startswith("data:"):
        return line_text + "\n\n" if not line_text.endswith("\n\n") else line_text
    data_str = line_text[5:].strip()
    if not data_str or data_str == "[DONE]":
        return line_text + "\n\n" if not line_text.endswith("\n\n") else line_text
    try:
        data = json.loads(data_str)
        if rewrite_subagent_fc(data):
            return "data: " + json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n\n"
    except Exception:
        pass
    return line_text + "\n\n" if not line_text.endswith("\n\n") else line_text


def transform_request_payload(body, level):
    body = apply_thinking_level(body, level)
    body = inject_subagent_policy(body)
    body = inject_task_continuation_policy(body)
    active_goal = state.get_goal()
    if active_goal:
        body = inject_goal_policy(body, active_goal)
    return body


state = State()


def route_effort_option(params):
    value = params.get("value")
    state.set_thinking_level(value)
    if value == "xhigh":
        params["value"] = "high"
        return True
    return False


def configured_proxy_api_key():
    for name in ("CLIPROXY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    default_config_path = os.path.join(os.path.expanduser("~"), "cliproxyapi", "config.yaml")
    config_path = os.environ.get("CLIPROXY_CONFIG_PATH", default_config_path)
    try:
        import yaml

        with open(config_path, "r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
        keys = (config.get("access") or {}).get("api-keys") or []
        return keys[0] if keys and isinstance(keys[0], str) else None
    except Exception:
        return None


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = "http://127.0.0.1:8317"

    def log_message(self, *_args):
        return

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_PUT(self):
        self._forward()

    def do_DELETE(self):
        self._forward()

    def do_PATCH(self):
        self._forward()

    def _forward(self):
        upstream = urlsplit(self.upstream)
        path = self.path
        selected_model = state.selected_proxy_model() or "gemini-4-argon"
        body_level = None
        proxy_model = None
        if selected_model in PROXY_MODELS:
            proxy_model, default_level = PROXY_MODELS[selected_model]
            selected_level = state.selected_thinking_level()
            body_level = selected_level if selected_level in effort_levels_for(selected_model) else default_level
            if selected_model.startswith("gemini-4-hydrogen") and body_level in HYDROGEN_THINKING_LEVELS:
                proxy_model = f"gemini-4-hydrogen-{body_level}"
            elif selected_model.startswith("gemini-3.8-flash") and body_level == "high":
                proxy_model = "gemini-3.8-flash-high"
        else:
            body_level = state.selected_thinking_level() or thinking_level_for(selected_model)
        if proxy_model:
            new = f"/v1beta/models/{proxy_model}:"
            replaced = False
            for placeholder in (PLACEHOLDER_MODEL, CONFIG_PLACEHOLDER_MODEL):
                old = f"/v1beta/models/{placeholder}:"
                if old in path:
                    path = path.replace(old, new, 1)
                    replaced = True
                    break
            if not replaced and "/v1beta/models/" in path and ":" in path:
                prefix, sep, rest = path.partition("/v1beta/models/")
                model_in_path, sep2, after = rest.partition(":")
                path = f"{prefix}/v1beta/models/{proxy_model}:{after}"
        for orch_model in (*ORCHESTRATOR_MODELS, "gemini-pro-orchestrator-neon"):
            old = f"/v1beta/models/{orch_model}:"
            if old in path:
                target_orch = proxy_model or PROXY_ORCHESTRATOR_MODEL
                path = path.replace(old, f"/v1beta/models/{target_orch}:", 1)
                break
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else None
        body = transform_request_payload(body, body_level)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "transfer-encoding", "content-length"}
        }
        upstream_timeout = int(os.environ.get("CLIPROXY_TIMEOUT", "1800"))
        conn = (
            http.client.HTTPSConnection(upstream.hostname, upstream.port or 443, timeout=upstream_timeout)
            if upstream.scheme == "https"
            else http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=upstream_timeout)
        )
        headers_sent = False
        try:
            conn.request(self.command, path, body=body, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status, response.reason)
            response_headers = {k.lower(): (k, v) for k, v in response.getheaders()}
            is_chunked = response_headers.get("transfer-encoding", ("", ""))[1].lower() == "chunked"
            content_length = response_headers.get("content-length")
            for lower, (key, value) in response_headers.items():
                if lower in {"connection", "transfer-encoding", "keep-alive", "proxy-authenticate", "proxy-authorization", "trailer", "upgrade"}:
                    continue
                self.send_header(key, value)
            if content_length is None and self.command != "HEAD":
                self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            if self.command != "HEAD":
                sse_buffer = ""
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    chunk_text = chunk.decode("utf-8", errors="replace")
                    if "data:" in chunk_text or sse_buffer:
                        combined = sse_buffer + chunk_text
                        lines = combined.split("\n\n")
                        sse_buffer = lines.pop()
                        out_chunks = []
                        for block in lines:
                            r = rewrite_sse_chunk(block)
                            if r:
                                out_chunks.append(r)
                        out_bytes = "".join(out_chunks).encode("utf-8")
                        if out_bytes:
                            if content_length is None:
                                self.wfile.write(f"{len(out_bytes):X}\r\n".encode("ascii"))
                                self.wfile.write(out_bytes + b"\r\n")
                            else:
                                self.wfile.write(out_bytes)
                            self.wfile.flush()
                    else:
                        if content_length is None:
                            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                            self.wfile.write(chunk + b"\r\n")
                        else:
                            self.wfile.write(chunk)
                        self.wfile.flush()
                if sse_buffer and sse_buffer.strip():
                    trailing = rewrite_sse_chunk(sse_buffer).encode("utf-8")
                    if trailing:
                        if content_length is None:
                            self.wfile.write(f"{len(trailing):X}\r\n".encode("ascii"))
                            self.wfile.write(trailing + b"\r\n")
                        else:
                            self.wfile.write(trailing)
                        self.wfile.flush()
                if content_length is None:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
        except Exception as exc:
            try:
                if not headers_sent:
                    payload = ("CLIProxyAPI adaptor error: " + str(exc)).encode("utf-8", "replace")
                    self.send_response(502)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
            except Exception:
                pass
        finally:
            conn.close()
            self.close_connection = True


def is_outdated_model(model):
    if not isinstance(model, str):
        return False
    model = model.lower()
    return (
        model.startswith("gemini-3.1-pro")
        or "neon" in model
        or (
            model.startswith("gemini-")
            and "flash" in model
            and not model.startswith("gemini-3.8-flash")
        )
    )


def unsuffixed_model(model):
    if model in MODEL_LEVEL_CHOICES or model in GEMINI_38_TIER_MODELS:
        return model.rsplit("-", 1)[0]
    return model


def filter_model_choices(choices):
    filtered = []
    for choice in choices:
        if not isinstance(choice, dict):
            filtered.append(choice)
            continue
        if is_outdated_model(choice.get("value")) or choice.get("value") in GEMINI_38_TIER_MODELS or choice.get("value") in MODEL_LEVEL_CHOICES:
            continue
        nested = choice.get("options")
        if isinstance(nested, list):
            choice = dict(choice)
            choice["options"] = filter_model_choices(nested)
        filtered.append(choice)
    return filtered


def add_proxy_choices(message):
    result = message.get("result")
    if not isinstance(result, dict):
        return
    for option in result.get("configOptions", []):
        if option.get("id") != "model" and option.get("category") != "model":
            continue
        choices = option.get("options")
        if not isinstance(choices, list):
            continue
        choices[:] = filter_model_choices(choices)
        option["currentValue"] = unsuffixed_model(option.get("currentValue"))
        flat = []
        for choice in choices:
            flat.extend([choice] if isinstance(choice, dict) and "value" in choice else (choice.get("options", []) if isinstance(choice, dict) else []))
        for model, (name, description) in MODEL_CHOICES.items():
            if not any(choice.get("value") == model for choice in flat if isinstance(choice, dict)):
                choices.append({
                    "value": model,
                    "name": name,
                    "description": description,
                })
        # The child echoes the placeholder model back; report the model the user actually picked.
        selected = state.selected_proxy_model()
        if selected:
            for choice in choices:
                if isinstance(choice, dict):
                    cval = choice.get("value")
                    if cval == selected or (selected in ORCHESTRATOR_MODELS and cval in ORCHESTRATOR_MODELS):
                        option["currentValue"] = selected
                        break

    config_options = result.setdefault("configOptions", [])
    if isinstance(config_options, list):
        selected_model = state.selected_proxy_model()
        levels = effort_levels_for(selected_model)
        effort_options = [
            option for option in config_options
            if isinstance(option, dict)
            and option.get("id") in ("thinking_level", "reasoning_effort", "thought_level")
        ]
        if not levels:
            config_options[:] = [
                option for option in config_options
                if not isinstance(option, dict)
                or option.get("id") not in ("thinking_level", "reasoning_effort", "thought_level")
            ]
        elif effort_options:
            options = [{"value": level, "name": "Extra" if level == "xhigh" else level.title()} for level in levels]
            selected_level = state.selected_thinking_level()
            for option in effort_options:
                option["category"] = SHARED_THINKING_CATEGORY
                option["options"] = options
                if selected_level in levels:
                    option["currentValue"] = selected_level
                elif option.get("currentValue") not in levels:
                    option["currentValue"] = "medium" if "medium" in levels else ("high" if "high" in levels else levels[0])
        else:
            selected_level = state.selected_thinking_level()
            config_options.append({
                "id": "thinking_level",
                "name": "Thinking Level",
                "category": SHARED_THINKING_CATEGORY,
                "type": "select",
                "currentValue": selected_level if selected_level in levels else (
                    "medium" if "medium" in levels else ("high" if "high" in levels else levels[0])
                ),
                "options": [{"value": level, "name": "Extra" if level == "xhigh" else level.title()} for level in levels],
            })

    # Advertise /goal command to host
    slash_commands = result.setdefault("slashCommands", [])
    if isinstance(slash_commands, list):
        if not any(isinstance(c, dict) and c.get("name") == "goal" for c in slash_commands):
            slash_commands.append({
                "name": "goal",
                "description": "Set an autonomous goal loop (works systematically until goal is achieved)."
            })

    models = result.get("models")
    if isinstance(models, dict):
        available = models.get("availableModels")
        if isinstance(available, list):
            available[:] = [
                item for item in available
                if not isinstance(item, dict) or (
                    not is_outdated_model(item.get("modelId"))
                    and item.get("modelId") not in GEMINI_38_TIER_MODELS
                    and item.get("modelId") not in MODEL_LEVEL_CHOICES
                )
            ]
            if "currentModelId" in models:
                models["currentModelId"] = unsuffixed_model(models["currentModelId"])
            for model, (name, _description) in MODEL_CHOICES.items():
                if not any(isinstance(item, dict) and item.get("modelId") == model for item in available):
                    available.append({
                        "modelId": model,
                        "name": name,
                    })
            selected = state.selected_proxy_model()
            if selected:
                for item in available:
                    if isinstance(item, dict):
                        m_id = item.get("modelId")
                        if m_id == selected or (selected in ORCHESTRATOR_MODELS and m_id in ORCHESTRATOR_MODELS):
                            models["currentModelId"] = selected
                            break


def patch_notification(message):
    params = message.get("params")
    if not isinstance(params, dict):
        return
    update = params.get("update")
    if not isinstance(update, dict):
        return

    # If the server is broadcasting available slash commands to the client, inject 'goal'
    session_update_type = update.get("sessionUpdate") or update.get("session_update")
    if session_update_type == "available_commands_update" or "availableCommands" in update or "available_commands" in update:
        for field in ("availableCommands", "available_commands"):
            cmds = update.get(field)
            if isinstance(cmds, list):
                if not any(isinstance(c, dict) and c.get("name") == "goal" for c in cmds):
                    cmds.append({
                        "name": "goal",
                        "description": "Set an autonomous goal loop (works systematically until goal is achieved)."
                    })

    config_options = update.get("configOptions") or update.get("config_options")
    models = update.get("models")
    available_models = update.get("availableModels") or update.get("available_models")
    if not isinstance(config_options, list) and not isinstance(models, (dict, list)) and not isinstance(available_models, list):
        return
    result = {}
    if isinstance(config_options, list):
        result["configOptions"] = config_options
    if isinstance(models, dict):
        result["models"] = models
    elif isinstance(models, list):
        result["models"] = {"availableModels": models}
    elif isinstance(available_models, list):
        result["models"] = {"availableModels": available_models}
    add_proxy_choices({"result": result})


def local_addresses():
    """This machine's non-loopback IPv4 addresses.

    T3 Code publishes its MCP endpoint as the loopback address of its own HTTP
    server, but a second T3 Code server (the VS Code extension host) can hold
    that same loopback port. Loopback traffic then reaches the wrong server
    and the credential is refused with 401. The server that issued the
    credential listens on every interface, so its other addresses reach it.
    """
    addresses = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            host = info[4][0]
            if not host.startswith("127.") and host not in addresses:
                addresses.append(host)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            host = probe.getsockname()[0]
            if not host.startswith("127.") and host not in addresses:
                addresses.append(host)
        finally:
            probe.close()
    except OSError:
        pass
    return addresses


def endpoint_candidates(endpoint):
    """The endpoint itself, then the same path on this machine's other addresses."""
    parts = urlsplit(endpoint)
    candidates = [endpoint]
    if parts.hostname not in LOOPBACK_HOSTS:
        return candidates
    port = parts.port or (443 if parts.scheme == "https" else 80)
    for host in local_addresses():
        candidate = urlunsplit(
            (parts.scheme, f"{host}:{port}", parts.path, parts.query, parts.fragment))
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def accepts_credential(endpoint, authorization, timeout=2.0):
    """True when the endpoint accepts the credential.

    A GET on the MCP endpoint answers 405 when the credential is accepted and
    401 when it is refused, so this needs no MCP session.
    """
    parts = urlsplit(endpoint)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    try:
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
        try:
            conn.request("GET", path, headers={
                "Authorization": authorization,
                "Accept": "application/json, text/event-stream",
            })
            status = conn.getresponse().status
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return False
    return status in (200, 405)


_mcp_endpoints = {}


def resolve_mcp_endpoint(endpoint, authorization):
    """An endpoint that accepts `authorization`, or the given one unchanged.

    Without this the agent loads the 't3-code' MCP server against whichever
    server holds the loopback port, and the turn dies with
    'The MCP server t3-code failed to initialize'.
    """
    if not endpoint or not authorization:
        return endpoint
    key = (endpoint, authorization)
    cached = _mcp_endpoints.get(key)
    if cached is not None:
        return cached
    for candidate in endpoint_candidates(endpoint):
        if accepts_credential(candidate, authorization):
            _mcp_endpoints[key] = candidate
            return candidate
    return endpoint


def rewrite_mcp_endpoint(params):
    """Point every T3 MCP server entry at an endpoint the credential works on."""
    servers = params.get("mcpServers")
    if not isinstance(servers, list):
        return False
    changed = False
    for server in servers:
        if not isinstance(server, dict):
            continue
        if server.get("type") == "http":
            url = server.get("url")
            headers = server.get("headers")
            if not isinstance(url, str) or not isinstance(headers, list):
                continue
            values = {h.get("name"): h.get("value") for h in headers if isinstance(h, dict)}
            authorization = values.get("Authorization")
            resolved = resolve_mcp_endpoint(url, authorization)
            if resolved != url:
                server["url"] = resolved
                changed = True
            continue
        env = server.get("env")
        if not isinstance(env, list):
            continue
        values = {e.get("name"): e.get("value") for e in env if isinstance(e, dict)}
        endpoint = values.get(T3_MCP_ENDPOINT_ENV)
        authorization = values.get(T3_MCP_AUTHORIZATION_ENV)
        if not endpoint or not authorization:
            continue
        resolved = resolve_mcp_endpoint(endpoint, authorization)
        if resolved == endpoint:
            continue
        for entry in env:
            if isinstance(entry, dict) and entry.get("name") == T3_MCP_ENDPOINT_ENV:
                entry["value"] = resolved
        changed = True
    return changed


def configured_mcp_servers():
    path = os.environ.get(
        "T3_ACP_MCP_SERVERS_CONFIG",
        os.path.expanduser("~/.t3/userdata/secrets/antigravity-mcp-servers.json"),
    )
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        configured = json.load(f).get("mcpServers", {})
    if not isinstance(configured, dict):
        raise ValueError("mcpServers config must be an object")
    servers = []
    for name, server in configured.items():
        if not isinstance(server, dict) or not isinstance(server.get("command"), str):
            raise ValueError("each MCP server needs a command")
        env = server.get("env", {})
        args = server.get("args", [])
        if not isinstance(env, dict) or not isinstance(args, list):
            raise ValueError("MCP server env must be an object and args must be an array")
        servers.append({
            "type": "stdio",
            "name": name,
            "command": server["command"],
            "args": args,
            "env": [{"name": key, "value": value} for key, value in env.items()],
        })
    return servers


def main():
    if len(sys.argv) < 2:
        print("usage: acp_argon_shim.py <agy_acp_server.par> [args...]", file=sys.stderr)
        return 2

    target = os.environ.get("CLIPROXY_HUB_URL", "http://127.0.0.1:8317").rstrip("/")
    mcp_servers = configured_mcp_servers()
    ProxyHandler.upstream = target
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=httpd.serve_forever, name="cliproxy-adaptor", daemon=True)
    thread.start()

    child_env = os.environ.copy()
    child_env["GOOGLE_GEMINI_BASE_URL"] = f"http://127.0.0.1:{httpd.server_port}"
    inherited_endpoint = child_env.get(T3_MCP_ENDPOINT_ENV)
    inherited_authorization = child_env.get(T3_MCP_AUTHORIZATION_ENV)
    if inherited_endpoint and inherited_authorization:
        resolved = resolve_mcp_endpoint(inherited_endpoint, inherited_authorization)
        if resolved != inherited_endpoint:
            child_env[T3_MCP_ENDPOINT_ENV] = resolved
    if not child_env.get("GEMINI_API_KEY"):
        proxy_key = configured_proxy_api_key()
        if proxy_key:
            child_env["GEMINI_API_KEY"] = proxy_key
    child = subprocess.Popen(
        sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=None, text=True, bufsize=1, env=child_env,
    )

    def stop(_signum, _frame):
        try:
            child.terminate()
        except Exception:
            pass
        httpd.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def feed_child():
        try:
            for line in sys.stdin:
                forwarded = line
                try:
                    request = json.loads(line)
                    method = request.get("method")
                    if method in ("session/new", "session/resume", "session/load"):
                        params = request.get("params")
                        if isinstance(params, dict):
                            changed = False
                            if mcp_servers and method in ("session/new", "session/resume"):
                                existing = params.get("mcpServers", [])
                                if not isinstance(existing, list):
                                    existing = []
                                names = {s.get("name") for s in existing if isinstance(s, dict)}
                                params["mcpServers"] = existing + [s for s in mcp_servers if s["name"] not in names]
                                changed = True
                            if rewrite_mcp_endpoint(params):
                                changed = True
                            if changed:
                                forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                        if method == "session/new":
                            state.select(None)
                            state.set_thinking_level(None)
                            state.clear_goal()
                    elif request.get("method") == "session/prompt":
                        params = request.get("params")
                        if isinstance(params, dict):
                            prompt_val = params.get("prompt")
                            prompt_text = ""
                            if isinstance(prompt_val, list):
                                for part in prompt_val:
                                    if isinstance(part, dict) and part.get("type") == "text":
                                        prompt_text = part.get("text", "")
                                        break
                            elif isinstance(prompt_val, str):
                                prompt_text = prompt_val

                            if prompt_text.strip().startswith("/goal"):
                                goal_content = prompt_text.strip()[5:].strip()
                                if goal_content:
                                    state.set_goal(goal_content)
                                    transformed_text = (
                                        f"[GOAL INITIATED: {goal_content}]\n\n"
                                        f"Please formulate an implementation plan and execute the necessary steps "
                                        f"towards this goal: {goal_content}"
                                    )
                                    if isinstance(prompt_val, list):
                                        for part in prompt_val:
                                            if isinstance(part, dict) and part.get("type") == "text":
                                                part["text"] = transformed_text
                                                break
                                    elif isinstance(prompt_val, str):
                                        params["prompt"] = transformed_text
                                    forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                    elif request.get("method") == "session/set_model":
                        params = request.get("params")
                        if isinstance(params, dict):
                            model_id = params.get("modelId")
                            if model_id in PROXY_MODELS or model_id in ALL_MANAGED_MODELS:
                                state.select(model_id)
                                params["modelId"] = PLACEHOLDER_MODEL
                                forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                            elif thinking_level_for(model_id):
                                state.select(model_id)
                                params["modelId"] = PLACEHOLDER_MODEL
                                forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                            else:
                                state.select(model_id)
                    elif request.get("method") == "session/set_config_option":
                        params = request.get("params")
                        if isinstance(params, dict):
                            config_id = params.get("configId", params.get("config_id"))
                            model_id = params.get("value")
                            if config_id in ("thinking_level", "reasoning_effort", "thought_level"):
                                if route_effort_option(params):
                                    forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                            elif config_id == "model":
                                if model_id in PROXY_MODELS or model_id in ALL_MANAGED_MODELS:
                                    state.select(model_id)
                                    params["value"] = CONFIG_PLACEHOLDER_MODEL
                                    forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                                elif thinking_level_for(model_id):
                                    state.select(model_id)
                                    params["value"] = CONFIG_PLACEHOLDER_MODEL
                                    forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                                else:
                                    state.select(model_id)
                except Exception:
                    pass
                child.stdin.write(forwarded)
                child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                child.stdin.close()
            except Exception:
                pass

    feeder = threading.Thread(target=feed_child, name="acp-stdin", daemon=True)
    feeder.start()
    try:
        for line in child.stdout:
            try:
                message = json.loads(line)
                result = message.get("result")
                if isinstance(result, dict) and ("sessionId" in result or "configOptions" in result or "models" in result):
                    # session/new and session/set_config_option both carry the model list; the client
                    # takes the last one it sees, so both have to carry the proxy choices.
                    add_proxy_choices(message)
                elif message.get("method") == "session/update":
                    patch_notification(message)
                output = json.dumps(message, separators=(",", ":"))
            except Exception:
                output = line.rstrip("\r\n")
            sys.stdout.write(output + "\n")
            sys.stdout.flush()
    finally:
        httpd.shutdown()
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
    return child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())

