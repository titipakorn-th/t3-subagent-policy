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
    check("unavailable Orchestrator model omitted",
          "gemini-pro-orchestrator-g1-pro" not in models, True)
    for model, expected in (
        ("gemini-4-argon", {"low", "medium", "high"}),
        ("gemini-4-hydrogen", {"low", "medium", "high", "xhigh"}),
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


def main():
    test_effort_routing()
    test_new_antigravity_models_and_efforts()
    test_hydrogen_xhigh_variant_maps_its_effort()
    print("\nall checks passed")


if __name__ == "__main__":
    main()
