"""Self-check for the effort routing in acp_clip_proxy_shim.py.

Run: python3 test_acp_clip_proxy_shim.py [path-to-acp_clip_proxy_shim.py]
Defaults to the deployed T3 antigravity clip proxy shim.
"""
import importlib.util
import json
import os
import sys

DEFAULT = os.path.expanduser("~/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py")


def target_from_argv():
    """The shim to test.

    Under pytest sys.argv holds the test files, so only a file that does not
    look like a test is taken as the shim.
    """
    for arg in sys.argv[1:]:
        name = os.path.basename(arg)
        if name.endswith(".py") and not name.startswith("test_") and os.path.exists(arg):
            return arg
    return DEFAULT


target = target_from_argv()
if not os.path.exists(target):
    sys.exit(f"shim not found: {target}")

spec = importlib.util.spec_from_file_location("shim", target)
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)


def check(label, got, want):
    assert got == want, f"{label}: got {got!r}, want {want!r}"
    print(f"ok  {label}: {got!r}")


def test_effort_routing():
    check("shim under test", target, target)

    # Effort must be derived for every model whose name states a level.
    check("flash-low", shim.thinking_level_for("gemini-3.8-flash-low"), "low")
    check("flash-medium", shim.thinking_level_for("gemini-3.8-flash-medium"), "medium")
    check("flash-high", shim.thinking_level_for("gemini-3.8-flash-high"), "high")
    check("argon-low", shim.thinking_level_for("gemini-4-argon-low"), "low")
    check("claude-opus-low", shim.thinking_level_for("claude-opus-5-5-low"), "low")

    # A routed model keeps exactly the level its own table carries. Shim revisions
    # differ on whether claude-*-high carries one, and the patch must not change
    # what the table says.
    routed = shim.PROXY_MODELS.get("claude-opus-5-5-high")
    check("claude-opus-high follows the routed table",
          shim.thinking_level_for("claude-opus-5-5-high"),
          routed[1] if routed else "high")
    check("unknown", shim.thinking_level_for("some-model"), None)
    check("none", shim.thinking_level_for(None), None)

    # The level must reach the request payload.
    body = json.dumps({"contents": [{"role": "user"}]}).encode()
    out = json.loads(shim.apply_thinking_level(body, "low"))
    check("payload level", out["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")
    check("payload contents kept", out["contents"], [{"role": "user"}])
    check("no level -> unchanged", shim.apply_thinking_level(body, None), body)
    check("bad json -> unchanged", shim.apply_thinking_level(b"not json", "low"), b"not json")


def test_new_antigravity_models_and_efforts():
    option = {"id": "model", "category": "model", "currentValue": "gemini-3.8-flash", "options": []}
    response = {"result": {"configOptions": [option]}}

    shim.state.select(None)
    shim.add_proxy_choices(response)
    models = {choice["value"]: choice for choice in option["options"]}
    check("Argon model advertised", "gemini-4-argon" in models, True)
    check("Hydrogen model advertised", "gemini-4-hydrogen" in models, True)
    check("Opus model advertised", "claude-opus-5-5" in models, True)
    check("Sonnet model advertised", "claude-sonnet-5-5" in models, True)
    check("Orchestrator model advertised",
          "gemini-pro-orchestrator-g1-pro" in models, True)
    for model, expected in (
        ("gemini-4-argon", {"low", "medium", "high"}),
        ("gemini-4-hydrogen", {"low", "medium", "high", "xhigh"}),
        ("gemini-pro-orchestrator-g1-pro", {"low", "medium", "high", "xhigh"}),
        ("claude-opus-5-5", {"low", "medium", "high"}),
        ("claude-sonnet-5-5", {"low", "medium", "high"}),
    ):
        shim.state.select(model)
        response = {"result": {"configOptions": [{
            "id": "model", "category": "model", "options": []
        }]}}
        shim.add_proxy_choices(response)
        effort = next(
            item for item in response["result"]["configOptions"]
            if item.get("id") in ("thinking_level", "reasoning_effort")
        )
        check(f"{model} exposes its effort levels",
              {item["value"] for item in effort["options"]} == expected, True)
        check(f"{model} effort option is shared across models in T3",
              effort["category"] != "thought_level", True)

    shim.state.select(None)


def test_hydrogen_xhigh_variant_maps_its_effort():
    shim.state.select("gemini-4-hydrogen-xhigh")
    check("xhigh variant maps to xhigh", shim.PROXY_MODELS["gemini-4-hydrogen-xhigh"][1], "xhigh")
    check("xhigh variant uses Hydrogen effort levels",
          shim.effort_levels_for("gemini-4-hydrogen-xhigh"),
          ("low", "medium", "high", "xhigh"))
    shim.state.select(None)


def test_subagent_tool_filtering_and_policy():
    raw_body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "systemInstruction": {
            "parts": [
                {"text": "Base instructions.\n<subagents>\n## Invoking Subagents\nUse invoke_subagent\n</subagents>\nEnd base."}
            ]
        },
        "tools": [
            {
                "functionDeclarations": [
                    {"name": "invoke_subagent", "description": "old subagent"},
                    {"name": "define_subagent", "description": "old define"},
                    {"name": "manage_subagents", "description": "old manage"},
                    {"name": "call_mcp_tool", "description": "mcp tool"},
                    {"name": "run_command", "description": "run command"}
                ]
            }
        ]
    }).encode("utf-8")

    transformed = json.loads(shim.transform_request_payload(raw_body, "medium"))
    decls = transformed["tools"][0]["functionDeclarations"]
    tool_names = [d["name"] for d in decls]
    check("invoke_subagent removed from tools", "invoke_subagent" in tool_names, False)
    check("define_subagent removed from tools", "define_subagent" in tool_names, False)
    check("manage_subagents removed from tools", "manage_subagents" in tool_names, False)
    check("call_mcp_tool kept in tools", "call_mcp_tool" in tool_names, True)
    check("run_command kept in tools", "run_command" in tool_names, True)

    sys_text = "\n".join(p.get("text", "") for p in transformed["systemInstruction"]["parts"])
    check("<subagents> block stripped", "<subagents>" in sys_text, False)
    check("[MANDATORY SUBAGENT DIRECTIVE] injected", "[MANDATORY SUBAGENT DIRECTIVE]" in sys_text, True)
    check("directive mentions delegate_task", "delegate_task" in sys_text, True)
    check("directive mentions call_mcp_tool", "call_mcp_tool" in sys_text, True)


def test_subagent_function_call_rewriting():
    fc_data = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "invoke_subagent",
                        "args": {
                            "Subagents": [{
                                "Prompt": "Search repo for tests",
                                "Role": "Codebase Researcher",
                                "TypeName": "research"
                            }]
                        }
                    }
                }]
            }
        }]
    }
    check("rewrite invoke_subagent triggers", shim.rewrite_subagent_fc(fc_data), True)
    rewritten_fc = fc_data["candidates"][0]["content"]["parts"][0]["functionCall"]
    check("fc name rewritten to call_mcp_tool", rewritten_fc["name"], "call_mcp_tool")
    check("ServerName is t3-code", rewritten_fc["args"]["ServerName"], "t3-code")
    check("ToolName is delegate_task", rewritten_fc["args"]["ToolName"], "delegate_task")
    check("Arguments task", rewritten_fc["args"]["Arguments"]["task"], "Search repo for tests")
    check("Arguments role", rewritten_fc["args"]["Arguments"]["role"], "research")

    # Also check direct delegate_task function call
    dt_data = {
        "candidates": [{
            "content": {
                "parts": [{
                    "functionCall": {
                        "name": "delegate_task",
                        "args": {
                            "task": "Direct delegate",
                            "role": "implementation"
                        }
                    }
                }]
            }
        }]
    }
    check("rewrite delegate_task triggers", shim.rewrite_subagent_fc(dt_data), True)
    rewritten_dt = dt_data["candidates"][0]["content"]["parts"][0]["functionCall"]
    check("dt name rewritten to call_mcp_tool", rewritten_dt["name"], "call_mcp_tool")
    check("dt Arguments task", rewritten_dt["args"]["Arguments"]["task"], "Direct delegate")


def test_task_continuation_policy_and_cleaning():
    raw_body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "Base system prompt.\n"
                        "Commands that do not finish within the specified wait time continue running in the background as tasks; "
                        "the system will notify you when they finish, so do not poll their status or set timers to check on them.\n"
                        "## Reactive Wakeup (No Polling Needed)\n"
                        "This means you do NOT need to poll in a loop while waiting for messages or updates. "
                        "After launching anything that performs work asynchronously, you may continue other work or "
                        "simply stop by calling no more tools. The system will notify you when there is something to process.\n"
                    )
                }
            ]
        }
    }).encode("utf-8")

    transformed = json.loads(shim.transform_request_payload(raw_body, "medium"))
    sys_text = "\n".join(p.get("text", "") for p in transformed["systemInstruction"]["parts"])
    check("directive injected", "[MANDATORY COMMAND & TASK CONTINUATION DIRECTIVE]" in sys_text, True)
    check("do not poll neutralized", "do not poll their status" not in sys_text, True)
    check("simply stop neutralized", "simply stop by calling no more tools" not in sys_text, True)
    check("manage_task mentioned in directive", "manage_task" in sys_text, True)


def test_background_task_response_rewriting():
    raw_body = json.dumps({
        "contents": [
            {
                "role": "model",
                "parts": [{
                    "functionCall": {
                        "name": "run_command",
                        "args": {"CommandLine": "cargo test"}
                    }
                }]
            },
            {
                "role": "function",
                "parts": [{
                    "functionResponse": {
                        "name": "run_command",
                        "response": {
                            "output": (
                                "Tool is running as a background task with task id: test-task-42\n"
                                "Task logs are available at: file:///path/to/task-42.log\n"
                                "YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS: A) either proceed to other relevant work (if any) or, "
                                "B) simply update the user with a short message (that you have launched the command and will wait for it to finish) and end the turn.\n"
                                " DO NOTHING ELSE."
                            )
                        }
                    }
                }]
            }
        ],
        "systemInstruction": {"parts": [{"text": "Base instructions."}]}
    }).encode("utf-8")

    transformed = json.loads(shim.transform_request_payload(raw_body, "medium"))
    resp_output = transformed["contents"][1]["parts"][0]["functionResponse"]["response"]["output"]
    check("task id preserved in rewritten text", "test-task-42" in resp_output, True)
    check("DO NOTHING ELSE removed", "DO NOTHING ELSE" not in resp_output, True)
    check("TWO ACTIONS removed", "YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS" not in resp_output, True)
    check("T3 critical instruction added", "[T3 CODE CRITICAL INSTRUCTION]" in resp_output, True)
    check("manage_task poll mandated", "manage_task" in resp_output, True)


def main():
    test_effort_routing()
    test_new_antigravity_models_and_efforts()
    test_hydrogen_xhigh_variant_maps_its_effort()
    test_subagent_tool_filtering_and_policy()
    test_subagent_function_call_rewriting()
    test_task_continuation_policy_and_cleaning()
    test_background_task_response_rewriting()
    print("\nall checks passed")


if __name__ == "__main__":
    main()
