#!/usr/bin/env bash
# dash and other POSIX shells lack `set -o pipefail`; re-exec under bash when started by one.
if [ -z "${BASH_VERSION:-}" ]; then
  command -v bash >/dev/null 2>&1 || { echo "This script requires bash." >&2; exit 1; }
  exec bash "$0" "$@"
fi
set -euo pipefail

T3_USER_HOME="${HOME:?HOME is not set}"
CLIPROXY_CONFIG="${CLIPROXY_CONFIG:-${T3_USER_HOME}/cliproxyapi/config.yaml}"
CLIPROXY_BASE_URL="${CLIPROXY_BASE_URL:-}"
CODEX_BIN="${CODEX_BIN:-$(command -v codex || true)}"
CODEX_HOME_DIR="${CODEX_HOME:-${T3_USER_HOME}/.codex}"
T3_HOME_DIR="${T3_HOME:-${T3_USER_HOME}/.t3}"
AGY_BIN_DIR="${AGY_BIN_DIR:-${T3_USER_HOME}/.local/bin}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUNDLED_SHIM="${SCRIPT_DIR}/patches/acp_clip_proxy_shim.py"
SETUP_MODE=""

for arg in "$@"; do
  case "$arg" in
    --both) SETUP_MODE="both" ;;
    --codex-only|--codex) SETUP_MODE="codex" ;;
    --antigravity-only|--antigravity) SETUP_MODE="antigravity" ;;
    --help|-h)
      echo "Usage: $0 [--both|--codex-only|--antigravity-only]"
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ -z "$SETUP_MODE" ]]; then
  echo "What would you like to set up?"
  echo "  1) Both Codex and Antigravity ACP (default)"
  echo "  2) Codex only"
  echo "  3) Antigravity ACP only"
  printf "Choose [1-3]: " > /dev/tty
  read -r choice < /dev/tty
  case "${choice:-1}" in
    1) SETUP_MODE="both" ;;
    2) SETUP_MODE="codex" ;;
    3) SETUP_MODE="antigravity" ;;
    *) echo "Choose 1, 2, or 3." >&2; exit 2 ;;
  esac
fi

INSTALL_CODEX=0
INSTALL_ANTIGRAVITY=0
case "$SETUP_MODE" in
  both) INSTALL_CODEX=1; INSTALL_ANTIGRAVITY=1 ;;
  codex) INSTALL_CODEX=1 ;;
  antigravity) INSTALL_ANTIGRAVITY=1 ;;
esac

if [[ "$INSTALL_CODEX" = "1" && -z "$CODEX_BIN" ]]; then
  if [[ -x "${T3_USER_HOME}/.local/bin/codex" ]]; then
    CODEX_BIN="${T3_USER_HOME}/.local/bin/codex"
  else
    echo "Codex CLI was not found. Set CODEX_BIN to its executable path." >&2
    exit 1
  fi
fi

python3 - "$CLIPROXY_CONFIG" "$CLIPROXY_BASE_URL" "$CODEX_HOME_DIR" "$T3_HOME_DIR" "$AGY_BIN_DIR" "$CODEX_BIN" "$BUNDLED_SHIM" "$INSTALL_CODEX" "$INSTALL_ANTIGRAVITY" <<'PY'
import datetime
import filecmp
import glob
import getpass
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

config_path, base_override, codex_home, t3_home, agy_bin_dir, codex_bin, bundled_shim, install_codex, install_antigravity = sys.argv[1:]
install_codex = install_codex == "1"
install_antigravity = install_antigravity == "1"
config_path = os.path.abspath(os.path.expanduser(config_path))
codex_home = os.path.abspath(os.path.expanduser(codex_home))
t3_home = os.path.abspath(os.path.expanduser(t3_home))
agy_bin_dir = os.path.abspath(os.path.expanduser(agy_bin_dir))
bundled_shim = os.path.abspath(os.path.expanduser(bundled_shim))

if install_antigravity:
    # Preflight the T3 Antigravity ACP install before changing Codex credentials.
    acp_tools_root = os.path.join(t3_home, "tools", "antigravity-acp")
    active_candidates = glob.glob(os.path.join(acp_tools_root, "*", "active.json"))
    if not active_candidates:
        raise SystemExit("T3 Antigravity ACP is not installed on this machine.")
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_aliases = {"linux": ("linux",), "darwin": ("darwin", "macos"), "windows": ("windows",)}
    arch_aliases = {
        "x86_64": ("x64", "x86_64", "amd64"),
        "amd64": ("x64", "x86_64", "amd64"),
        "aarch64": ("arm64", "aarch64"),
        "arm64": ("arm64", "aarch64"),
    }
    os_prefixes = os_aliases.get(system, (system,))
    arch_names = arch_aliases.get(machine, (machine,))
    matching_active = [
        path for path in active_candidates
        if any(os.path.basename(os.path.dirname(path)).lower().startswith(os_name + "-") for os_name in os_prefixes)
        and any(arch in os.path.basename(os.path.dirname(path)).lower() for arch in arch_names)
    ]
    if len(matching_active) == 1:
        active_path = matching_active[0]
    elif len(active_candidates) == 1:
        active_path = active_candidates[0]
    else:
        raise SystemExit("Could not uniquely locate this machine's T3 Antigravity ACP installation.")
    linux_tools = os.path.dirname(active_path)
    shim_path = os.path.join(t3_home, "tools", "antigravity-cliproxy", "acp_clip_proxy_shim.py")
    with open(active_path, encoding="utf-8") as f:
        release_id = json.load(f).get("releaseId")
    version_dir = os.path.join(linux_tools, "versions", str(release_id))
    server_binary = os.path.join(version_dir, "agy_acp_server.par")
    native_server = False
    if not os.path.isfile(server_binary):
        server_binary = os.path.join(version_dir, "antigravity")
        native_server = os.path.isfile(server_binary)
    if not os.path.isfile(server_binary):
        installed_servers = (
            glob.glob(os.path.join(linux_tools, "versions", "*", "agy_acp_server.par"))
            + glob.glob(os.path.join(linux_tools, "versions", "*", "antigravity"))
        )
        if not installed_servers:
            raise SystemExit("No installed T3 Antigravity ACP server binary was found.")
        server_binary = max(installed_servers, key=os.path.getmtime)
        version_dir = os.path.dirname(server_binary)
        native_server = os.path.basename(server_binary) == "antigravity"
    if not os.path.isfile(shim_path) and not os.path.isfile(bundled_shim):
        raise SystemExit("The bundled patches/acp_clip_proxy_shim.py file is missing.")
    # The legacy .par server needs --uid= on Linux; the native CLI uses `acp` instead.
    server_args = ["--uid="] if system == "linux" and not native_server else []
try:
    import yaml
except ImportError:
    yaml = None

proxy_config = {}
if os.path.isfile(config_path):
    if yaml is None:
        raise SystemExit("PyYAML is required to read CLIProxyAPI config.yaml (pip install pyyaml).")
    with open(config_path, encoding="utf-8") as f:
        proxy_config = yaml.safe_load(f) or {}

api_key = os.environ.get("CLIPROXY_API_KEY", "")
if not api_key:
    api_keys = (proxy_config.get("access") or {}).get("api-keys") or []
    api_key = api_keys[0] if api_keys and isinstance(api_keys[0], str) else ""
if not api_key:
    # A previous run already stored the key; reuse it so the prompt can be skipped without a TTY.
    stored_key_path = os.path.join(t3_home, "userdata", "secrets", "cliproxyapi-api-key")
    if os.path.isfile(stored_key_path):
        with open(stored_key_path, encoding="utf-8") as f:
            api_key = f.read().strip()
if not api_key:
    api_key = getpass.getpass("CLIProxyAPI client API key: ").strip()
if not api_key:
    raise SystemExit("No CLIProxyAPI API key supplied.")

root_url = base_override.strip().rstrip("/") if base_override else input(
    "CLIProxyAPI base URL (without /v1, e.g. https://proxy.example:8317): "
).strip().rstrip("/")
if root_url.endswith("/v1"):
    root_url = root_url[:-3]
parsed_url = urllib.parse.urlsplit(root_url)
if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
    raise SystemExit("CLIPROXY_BASE_URL must be an absolute http(s) URL.")

# Verify the API key without printing it or the response body.
request = urllib.request.Request(
    root_url + "/v1/models",
    headers={"X-Api-Key": api_key},
)
try:
    with urllib.request.urlopen(request, timeout=8) as response:
        model_data = json.load(response)
        model_count = len(model_data.get("data", []))
except urllib.error.HTTPError as e:
    raise SystemExit("CLIProxyAPI rejected the key or models request (HTTP {}).".format(e.code))
except Exception as e:
    raise SystemExit("Could not reach CLIProxyAPI at {} ({}). Check CLIPROXY_BASE_URL and network access.".format(root_url, type(e).__name__))

stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
def backup(path):
    if os.path.isfile(path):
        shutil.copy2(path, path + ".bak-" + stamp)

def atomic_json(path, value, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".setup-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

if install_codex:
    # Configure Codex without changing unrelated settings.
    os.makedirs(codex_home, exist_ok=True)
    codex_config_path = os.path.join(codex_home, "config.toml")
    backup(codex_config_path)
    try:
        with open(codex_config_path, encoding="utf-8") as f:
            toml_text = f.read()
    except FileNotFoundError:
        toml_text = ""

    section = "\n".join([
        "[model_providers.cliproxy]",
        'name = "CLIProxyAPI"',
        "base_url = " + json.dumps(root_url + "/v1"),
        'wire_api = "responses"',
        'http_headers = { "X-Api-Key" = ' + json.dumps(api_key) + " }",
        "requires_openai_auth = true",
    ]) + "\n"
    section_pattern = re.compile(r"(?ms)^\[model_providers\.cliproxy\][ \t]*\n.*?(?=^\[|\Z)")
    if section_pattern.search(toml_text):
        toml_text = section_pattern.sub(section, toml_text, count=1)
    else:
        toml_text = toml_text.rstrip() + ("\n\n" if toml_text.strip() else "") + section

    parts = re.split(r"(?m)(?=^\[)", toml_text, maxsplit=1)
    top = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    model_provider_line = 'model_provider = "cliproxy"'
    if re.search(r"(?m)^model_provider\s*=", top):
        top = re.sub(r"(?m)^model_provider\s*=.*$", model_provider_line, top, count=1)
    else:
        top = top.rstrip() + ("\n" if top.strip() else "") + model_provider_line + "\n"
    with open(codex_config_path, "w", encoding="utf-8") as f:
        f.write((top + rest).rstrip() + "\n")
    os.chmod(codex_config_path, 0o600)

    auth_path = os.path.join(codex_home, "auth.json")
    backup(auth_path)
    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home
    login = subprocess.run(
        [codex_bin, "login", "--with-api-key"],
        input=api_key + "\n",
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    if login.returncode != 0:
        raise SystemExit("Codex CLIProxy login failed; restore the auth.json backup if needed.")
    if os.path.isfile(auth_path):
        os.chmod(auth_path, 0o600)
if install_antigravity:
    # The ACP shim reads this private file and passes the key to its Antigravity child.
    secret_dir = os.path.join(t3_home, "userdata", "secrets")
    os.makedirs(secret_dir, mode=0o700, exist_ok=True)
    os.chmod(secret_dir, 0o700)
    secret_path = os.path.join(secret_dir, "cliproxyapi-api-key")
    fd, tmp = tempfile.mkstemp(prefix=".proxy-key-", dir=secret_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(api_key + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, secret_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    # Reuse the T3 Antigravity ACP binary and refresh the shim when its bundled copy changed.
    if not os.path.isfile(shim_path) or not filecmp.cmp(shim_path, bundled_shim, shallow=False):
        backup(shim_path)
        os.makedirs(os.path.dirname(shim_path), exist_ok=True)
        shutil.copy2(bundled_shim, shim_path)
        os.chmod(shim_path, 0o755)
    wrapper_path = os.path.join(t3_home, "tools", "antigravity-cliproxy", "antigravity-cliproxy-acp.sh")
    os.makedirs(os.path.dirname(wrapper_path), exist_ok=True)
    backup(wrapper_path)
    # T3's ACP probe never calls authenticate, so the agent must pick its auth method up from settings.
    gemini_home = os.path.join(t3_home, "tools", "antigravity-cliproxy", "gemini-home")
    wrapper = """#!/usr/bin/env bash
    set -euo pipefail
    export PATH={agy_bin_dir}:"$PATH"
    export CLIPROXY_API_KEY="$(cat {secret_path})"
    export CLIPROXY_CONFIG_PATH={config_path}
    export CLIPROXY_HUB_URL={root_url}
    GEMINI_HOME_DIR={gemini_home}
    unset GEMINI_API_KEY
    mkdir -p "$GEMINI_HOME_DIR/antigravity-acp"
    printf '%s' {auth_settings} > "$GEMINI_HOME_DIR/antigravity-acp/settings.json"
    T3_TOOL_ROOT={linux_tools}
    RELEASE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["releaseId"])' "$T3_TOOL_ROOT/active.json")"
    VERSION_DIR="$T3_TOOL_ROOT/versions/$RELEASE_ID"
    SERVER_BINARY="$VERSION_DIR/agy_acp_server.par"
    SERVER_SUBCOMMAND=()
    if [ ! -x "$SERVER_BINARY" ]; then
      SERVER_BINARY="$VERSION_DIR/antigravity"
      SERVER_SUBCOMMAND=(acp --app-data-dir "$GEMINI_HOME_DIR" --cli_dir_name antigravity-acp)
    fi
    if [ ! -x "$SERVER_BINARY" ]; then
      SERVER_BINARY=""
      for candidate in "$T3_TOOL_ROOT"/versions/*/agy_acp_server.par "$T3_TOOL_ROOT"/versions/*/antigravity; do
        [ -x "$candidate" ] || continue
        if [ -z "$SERVER_BINARY" ] || [ "$candidate" -nt "$SERVER_BINARY" ]; then
          SERVER_BINARY="$candidate"
          if [ "${{candidate##*/}}" = "antigravity" ]; then SERVER_SUBCOMMAND=(acp --app-data-dir "$GEMINI_HOME_DIR" --cli_dir_name antigravity-acp); else SERVER_SUBCOMMAND=(); fi
        fi
      done
      if [ -n "$SERVER_BINARY" ]; then VERSION_DIR="$(dirname "$SERVER_BINARY")"; fi
    fi
    if [ ! -x "$SERVER_BINARY" ]; then
      echo "no installed Antigravity ACP server found under $T3_TOOL_ROOT/versions" >&2
      exit 127
    fi
    if [ "${{SERVER_BINARY##*/}}" = "antigravity" ]; then
      unset GEMINI_HOME
    else
      export GEMINI_HOME="$GEMINI_HOME_DIR"
      SERVER_SUBCOMMAND=()
    fi
    KEEP_UID={keep_uid}
    if [ "${{SERVER_BINARY##*/}}" = "antigravity" ]; then KEEP_UID=0; fi
    ARGS=()
    for arg in "$@"; do
      case "$arg" in
        --uid=*) if [ "$KEEP_UID" = "1" ]; then ARGS+=("$arg"); fi ;;
        *) ARGS+=("$arg") ;;
      esac
    done
    exec python3 {shim_path} "$SERVER_BINARY" "${{SERVER_SUBCOMMAND[@]+"${{SERVER_SUBCOMMAND[@]}}"}}" "${{ARGS[@]+"${{ARGS[@]}}"}}"
    """.format(
        agy_bin_dir=shlex.quote(agy_bin_dir),
        secret_path=shlex.quote(secret_path),
        config_path=shlex.quote(config_path),
        root_url=shlex.quote(root_url),
        gemini_home=shlex.quote(gemini_home),
        auth_settings=shlex.quote(json.dumps({"auth": {"type": "gemini-api-key"}})),
        keep_uid="1" if system == "linux" else "0",
        linux_tools=shlex.quote(linux_tools),
        shim_path=shlex.quote(shim_path),
    )
    with open(wrapper_path, "w", encoding="utf-8") as f:
        f.write(wrapper)
    os.chmod(wrapper_path, 0o755)

    # Keep the known-outdated Gemini choices out of local catalogs; the shim filters future ACP refreshes.
    def obsolete_model(value):
        if not isinstance(value, str):
            return False
        value = value.lower()
        return value.startswith("gemini-3.1-pro") or (
            value.startswith("gemini-") and "flash" in value and not value.startswith("gemini-3.8-flash")
        )
    manifest_path = os.path.join(t3_home, "userdata", "model-manifest.json")
    if os.path.isfile(manifest_path):
        backup(manifest_path)
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        root = manifest.get("manifest", {})
        current = root.get("currentModels", {}).get("antigravity", [])
        root.get("currentModels", {}).get("antigravity", [])[:] = [x for x in current if not obsolete_model(x)]
        provider = root.get("providers", {}).get("antigravity", {})
        models = provider.get("models", [])
        provider["models"] = [x for x in models if not obsolete_model(x.get("slug"))]
        manifest["fetchedAtMs"] = int(datetime.datetime.now().timestamp() * 1000)
        atomic_json(manifest_path, manifest)
    for cache_name in ("antigravity.json", "acpRegistry_antigravity_clip.json"):
        cache_path = os.path.join(t3_home, "caches", cache_name)
        if not os.path.isfile(cache_path):
            continue
        backup(cache_path)
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        cache["models"] = [m for m in cache.get("models", []) if not obsolete_model(m.get("slug"))]
        if cache_name == "acpRegistry_antigravity_clip.json":
            base_names = {
                "gemini-4-argon": ("Gemini 4 Argon", ("low", "medium", "high")),
                "gemini-4-hydrogen": ("Gemini 4 Hydrogen", ("low", "medium", "high", "xhigh")),
                "claude-opus-5-5": ("Claude Opus 5.5", ("low", "medium", "high")),
                "claude-sonnet-5-5": ("Claude Sonnet 5.5", ("low", "medium", "high")),
            }
            models_by_slug = {}
            for model in cache["models"]:
                slug = model.get("slug")
                base = next((name for name, (_display, levels) in base_names.items()
                             if slug in {name + "-" + level for level in levels}), None)
                if base:
                    model = dict(model)
                    model["slug"] = base
                    model["name"] = base_names[base][0]
                existing = models_by_slug.setdefault(model.get("slug"), model)
                existing["isDefault"] = existing.get("isDefault", False) or model.get("isDefault", False)
            cache["models"] = list(models_by_slug.values())
        cache["checkedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        atomic_json(cache_path, cache)
if install_codex or install_antigravity:
    settings_path = os.path.join(t3_home, "userdata", "settings.json")
    if os.path.isfile(settings_path):
        backup(settings_path)
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
        instances = settings.setdefault("providerInstances", {})
        if install_codex:
            codex_instance = instances.get("codex")
            if isinstance(codex_instance, dict):
                codex_instance.setdefault("config", {})["homePath"] = codex_home
        if install_antigravity:
            provider_id = "acpRegistry_antigravity_clip"
            instance = instances.setdefault(provider_id, {})
            instance.update({
                "driver": "acpRegistry",
                "displayName": "Antigravity via CLIProxyAPI",
                "accentColor": "#4285f4",
                "enabled": True,
            })
            instance.setdefault("config", {}).update({
                "source": "local",
                "commandPath": wrapper_path,
                "commandArgs": server_args,
            })
        atomic_json(settings_path, settings, 0o600)

if install_codex:
    print("Codex configured for CLIProxyAPI with X-Api-Key and CLI login.")
if install_antigravity:
    print("Antigravity via CLIProxyAPI ACP provider configured in T3.")
print("CLIProxyAPI endpoint verified; model count: {}.".format(model_count))
print("Backups use suffix .bak-{}; API keys were not printed.".format(stamp))
print("Restart T3 to reload provider settings.")
PY
