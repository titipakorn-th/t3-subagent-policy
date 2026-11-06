#!/usr/bin/env python3
"""Expose CLIProxyAPI Antigravity models through T3's ACP process.

The bundled ACP server validates selected model IDs against its built-in list.
This shim advertises Argon and Claude choices, translates their ACP selection
to a supported placeholder model, and rewrites only the selected model's
Gemini API request path before forwarding it to CLIProxyAPI.
"""
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ARGON_MODELS = (
    "gemini-4-argon",
)
PROXY_ARGON_MODEL = "gemini-4-argon-high"
PLACEHOLDER_MODEL = "gemini-3.8-flash-high"
CONFIG_PLACEHOLDER_MODEL = "gemini-3.8-flash"
THINKING_LEVELS = ("low", "medium", "high")

CLAUDE_MODELS = {
    "claude-opus-5-5": ("claude-opus-5-5-high", None),
    "claude-sonnet-5-5": ("claude-sonnet-5-5-high", None),
}
PROXY_MODELS = {
    **{model: (PROXY_ARGON_MODEL, None) for model in ARGON_MODELS},
    **CLAUDE_MODELS,
    **{
        f"gemini-4-argon-{level}": (PROXY_ARGON_MODEL, level)
        for level in THINKING_LEVELS
    },
    **{
        f"{model}-{level}": (proxy_model, level)
        for model, (proxy_model, _default_level) in CLAUDE_MODELS.items()
        for level in THINKING_LEVELS
    },
}
LEGACY_PROXY_MODELS = set(PROXY_MODELS) - set(ARGON_MODELS) - set(CLAUDE_MODELS)
GEMINI_38_TIER_MODELS = {f"gemini-3.8-flash-{level}" for level in THINKING_LEVELS}
MODEL_CHOICES = {
    **{
        model: ("Gemini 4 Argon", "Gemini 4 Argon routed through CLIProxyAPI.")
        for model in ARGON_MODELS
    },
    **{
        model: (
            f"Claude {'Opus' if '-opus-' in model else 'Sonnet'} 5.5",
            "Claude routed through your Antigravity quota via CLIProxyAPI.",
        )
        for model in CLAUDE_MODELS
    },
}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.proxy_model = None
        self.thinking_level = None
        self.active_goal = None

    def select(self, model):
        with self.lock:
            self.proxy_model = model

    def selected_proxy_model(self):
        with self.lock:
            return self.proxy_model

    def set_thinking_level(self, level):
        with self.lock:
            self.thinking_level = level if level in THINKING_LEVELS else None

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
    "`invoke_subagent`. ALWAYS use T3 Code's orchestrator tool `delegate_task` so that the "
    "child thread and task appear in the T3 Code UI and sidebar."
)


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
        payload = json.loads(body)
        if not isinstance(payload, dict) or "contents" not in payload:
            return body

        sys_inst = payload.setdefault("systemInstruction", {})
        if not isinstance(sys_inst, dict):
            return body
        parts = sys_inst.setdefault("parts", [])
        if not isinstance(parts, list):
            return body

        # Check if policy already present to prevent duplicate injections
        for part in parts:
            if isinstance(part, dict) and "[MANDATORY SUBAGENT DIRECTIVE]" in part.get("text", ""):
                return body

        parts.append({"text": SUBAGENT_DELEGATION_POLICY})
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


def transform_request_payload(body, level):
    body = apply_thinking_level(body, level)
    body = inject_subagent_policy(body)
    active_goal = state.get_goal()
    if active_goal:
        body = inject_goal_policy(body, active_goal)
    return body


state = State()


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
        selected_model = state.selected_proxy_model()
        body_level = None
        if selected_model in PROXY_MODELS:
            proxy_model, default_level = PROXY_MODELS[selected_model]
            body_level = state.selected_thinking_level() or default_level
            new = f"/v1beta/models/{proxy_model}:"
            for placeholder in (PLACEHOLDER_MODEL, CONFIG_PLACEHOLDER_MODEL):
                old = f"/v1beta/models/{placeholder}:"
                if old in path:
                    path = path.replace(old, new, 1)
                    break
        else:
            body_level = state.selected_thinking_level() or thinking_level_for(selected_model)
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else None
        body = transform_request_payload(body, body_level)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "transfer-encoding", "content-length"}
        }
        conn = http.client.HTTPSConnection(upstream.hostname, upstream.port or 443, timeout=300) if upstream.scheme == "https" else http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=300)
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
            if self.command != "HEAD":
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    if content_length is None:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk + b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                if content_length is None:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
        except Exception as exc:
            try:
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
        or (
            model.startswith("gemini-")
            and "flash" in model
            and not model.startswith("gemini-3.8-flash")
        )
    )


def unsuffixed_model(model):
    if model in LEGACY_PROXY_MODELS or model in GEMINI_38_TIER_MODELS:
        return model.rsplit("-", 1)[0]
    return model


def filter_model_choices(choices):
    filtered = []
    for choice in choices:
        if not isinstance(choice, dict):
            filtered.append(choice)
            continue
        if is_outdated_model(choice.get("value")) or choice.get("value") in LEGACY_PROXY_MODELS or choice.get("value") in GEMINI_38_TIER_MODELS:
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
        if (selected in PROXY_MODELS or thinking_level_for(selected)) and any(
            choice.get("value") == selected for choice in choices if isinstance(choice, dict)
        ):
            option["currentValue"] = selected

    config_options = result.setdefault("configOptions", [])
    if isinstance(config_options, list) and not any(
        option.get("id") in ("thinking_level", "reasoning_effort")
        for option in config_options if isinstance(option, dict)
    ):
        config_options.append({
            "id": "thinking_level",
            "name": "Thinking Level",
            "category": "thought_level",
            "type": "select",
            "currentValue": "high",
            "options": [{"value": level, "name": level.title()} for level in THINKING_LEVELS],
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
                    and item.get("modelId") not in LEGACY_PROXY_MODELS
                    and item.get("modelId") not in GEMINI_38_TIER_MODELS
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
                    if method in ("session/new", "session/resume"):
                        params = request.get("params")
                        if isinstance(params, dict) and mcp_servers:
                            existing = params.get("mcpServers", [])
                            if not isinstance(existing, list):
                                existing = []
                            names = {s.get("name") for s in existing if isinstance(s, dict)}
                            params["mcpServers"] = existing + [s for s in mcp_servers if s["name"] not in names]
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
                            if model_id in PROXY_MODELS:
                                state.select(model_id)
                                params["modelId"] = PLACEHOLDER_MODEL
                                forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                            elif thinking_level_for(model_id):
                                state.select(model_id)
                            else:
                                state.select(None)
                    elif request.get("method") == "session/set_config_option":
                        params = request.get("params")
                        if isinstance(params, dict):
                            config_id = params.get("configId", params.get("config_id"))
                            model_id = params.get("value")
                            if config_id in ("thinking_level", "reasoning_effort"):
                                state.set_thinking_level(model_id)
                            elif config_id == "model" and model_id in PROXY_MODELS:
                                state.select(model_id)
                                params["value"] = CONFIG_PLACEHOLDER_MODEL
                                forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                            elif config_id == "model" and thinking_level_for(model_id):
                                state.select(model_id)
                                params["value"] = CONFIG_PLACEHOLDER_MODEL
                                forwarded = json.dumps(request, separators=(",", ":")) + "\n"
                            elif config_id == "model":
                                state.select(None)
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

