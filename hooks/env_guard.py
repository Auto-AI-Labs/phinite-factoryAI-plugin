"""Block tool calls that would print a `.env` file's values (Python 3.8+, standard library only).

The Phinite plugin's rules say never to open or print a whole `.env`, but rules only apply once the assistant knows
it is doing Phinite work. This hook runs before every file read and shell command, Phinite or not, and refuses:

- reading a `.env` / `.env.<name>` file (`.env.example`, `.env.sample`, `.env.template`, `.env.dist` are fine);
- shell commands that print one: `cat .env`, `head`, `grep KEY .env`, `sed -n p .env`, `python -c "open('.env')"`,
  `source .env && printenv` and the like.

Names-only commands stay allowed (`cut -d= -f1 .env`, `awk -F= '{print $1}' .env`, `sed 's/=.*//' .env`,
`grep -c/-l/-q`), so is writing to a `.env` (`echo X=1 >> .env`) and the project script's `env-check`.

    python3 env_guard.py --client claude < event.json

The event (JSON on stdin) is the client's PreToolUse payload. Field names differ between clients, so the tool name
and its arguments are looked up under every known name. --client picks how a block is reported:

    claude, codex, factory, devin, kiro, opencode   exit 2, reason on stderr
    cursor                                          {"permission": ...} on stdout (always answers: empty = blocked)
    copilot                                         {"permissionDecision": "deny", ...} on stdout, exit 2
    antigravity                                     {"decision": "deny", "reason": ...} on stdout

Anything unexpected (bad JSON, unknown shape) allows the call: the guard must never break normal work.
Set PHINITE_ALLOW_ENV_READ=1 in the environment that starts the client to turn it off.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

SAFE_SUFFIXES = {"example", "sample", "template", "dist", "defaults.example"}
PATH_KEYS = ("file_path", "filePath", "path", "AbsolutePath", "SearchPath", "notebook_path", "target_file",
             "file", "filename", "absolute_path")
COMMAND_KEYS = ("command", "CommandLine", "cmd", "script")
WRITE_TOOLS = {"write", "edit", "multiedit", "create", "apply_patch", "applypatch", "write_to_file",
               "replace_file_content", "fs_write", "notebookedit", "str_replace", "insert"}
# Programs that print what they read.
READERS = {"cat", "tac", "less", "more", "most", "head", "tail", "bat", "batcat", "nl", "xxd", "od", "hexdump",
           "strings", "type", "get-content", "gc", "sed", "awk", "gawk", "mawk", "grep", "egrep", "fgrep", "rg",
           "ag", "cut", "sort", "uniq", "paste", "column", "jq", "yq", "base64", "diff", "cmp", "comm", "fold",
           "fmt", "pr", "expand", "rev", "tr", "dotenv", "select-string", "sls", "findstr", "openssl", "gpg",
           "envsubst", "wc"}
PRINTS = re.compile(r"print|read|cat\b|echo|console\.log|puts|write|dump|Get-Content|\bgc\b", re.I)
INTERPRETERS = {"python", "python3", "node", "deno", "bun", "ruby", "perl", "php", "pwsh", "powershell", "bash",
                "sh", "zsh"}
PRINTS_ENV = re.compile(r"(^|[\s;&|(])(printenv|env|set|export\s+-p|declare\s+-x?p)\s*($|[;&|)])"
                        r"|\becho\b[^;&|]*\$|\bprintf\b[^;&|]*\$", re.I)
WRAPPERS = {"sudo", "command", "builtin", "exec", "time", "nice", "nohup", "stdbuf", "timeout"}

REASON = ("Blocked by the Phinite plugin: this would show the values in {file}, and secrets must stay out of the "
          "chat, logs and tool calls. To see which variables it has, list the names only: `cut -d= -f1 {file}`. "
          "To use a value, let the program load the file itself (never print it). To send values to Phinite, run "
          "the phinite-project script's `env-check` and pass `names=[...]` to `set_env_vars`. If the user wants to "
          "see a value, ask them to open the file themselves.")


def secret_env(token: str) -> str | None:
    """The `.env` file a word names (a path, `.env*` glob, or `--env-file=x`), unless it's an example file."""
    token = token.strip("\"'`()<>{}[],;")
    if "=" in token and token.split("=", 1)[0].startswith("-"):
        token = token.split("=", 1)[1]
    base = re.split(r"[\\/]", token)[-1]
    if base == ".env" or base == ".env*" or base == ".env.*":
        return token
    m = re.fullmatch(r"\.env\.([\w.*-]+)", base) or re.fullmatch(r"([\w-]+)\.env", base)
    if m and m.group(1).lower() not in SAFE_SUFFIXES and not m.group(1).lower().endswith(("example", "sample")):
        return token
    return None


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _names_only(prog: str, words: list[str]) -> bool:
    """True for the usual ways to list a .env's names without its values."""
    rest = " ".join(words[1:])
    if prog == "cut":
        return bool(re.search(r"(-f\s*1|--fields[= ]1)(\s|$)", rest)) and not re.search(r"-f\s*1[-,]", rest)
    if prog in ("awk", "gawk", "mawk"):
        split_on_eq = any(w in ("-F=", "-F") and (w == "-F=" or words[words.index(w) + 1:][:1] == ["="])
                          for w in words[1:])
        prog_text = next((w for w in words[1:] if "print" in w), "")
        return split_on_eq and re.fullmatch(r"\s*\{\s*print \$1;?\s*\}\s*", prog_text) is not None
    if prog == "sed":
        return bool(re.search(r"s/=\.\*//|s/=.*\$//", rest)) and " -n" not in f" {rest}"
    if prog in ("grep", "egrep", "fgrep", "rg"):
        flags = "".join(w[1:] for w in words[1:] if w.startswith("-") and not w.startswith("--"))
        long = [w for w in words[1:] if w.startswith("--")]
        if set(flags) & set("clqL") or any(w in ("--count", "--files-with-matches", "--quiet", "--silent")
                                          for w in long):
            return True
        # grep -o '^[A-Za-z_][A-Za-z0-9_]*' .env  (the name before "=")
        pattern = next((w for w in words[1:] if not w.startswith("-") and not secret_env(w)), "")
        core = pattern.replace("[^=]", "")
        return "o" in flags and pattern.startswith("^") and "=" not in core.rstrip("=") and "." not in core
    if prog == "wc":
        return True
    return False


def _program(words: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(words) and (re.fullmatch(r"\w+=.*", words[i]) or words[i] in WRAPPERS):
        i += 1
    if i < len(words) and words[i] == "env":       # env VAR=1 cat .env
        i += 1
        while i < len(words) and (re.fullmatch(r"\w+=.*", words[i]) or words[i].startswith("-")):
            i += 1
    words = words[i:]
    prog = re.split(r"[\\/]", words[0])[-1].lower() if words else ""
    prog = re.sub(r"\.exe$", "", prog)
    return prog, words


def check_command(command: str) -> str | None:
    """The .env file a shell command would print, or None."""
    if not command or ".env" not in command:
        return None
    if "phinite_project.py" in command:           # the plugin's own script reads .env names on purpose
        return None
    # `source .env && printenv`, `. ./.env; echo $KEY`
    sourced = re.search(r"(^|[\s;&|(])(source|\.)\s+(\S*\.env[\w.-]*)", command)
    if sourced and secret_env(sourced.group(3)) and PRINTS_ENV.search(command[sourced.end():]):
        return secret_env(sourced.group(3))
    # Remove redirect targets (`> .env`, `>> .env`, `2>.env`): writing a .env is fine. `< .env` is a read.
    without_writes = re.sub(r"\d?>>?\s*\S+", " ", command)
    for segment in re.split(r"\|\||&&|[|;\n&]|\$\(|`", without_writes):
        words = _words(segment)
        if not words:
            continue
        prog, words = _program(words)
        fed = [h for h in (secret_env(m) for m in re.findall(r"<\s*(\S+)", segment)) if h]
        if fed and prog not in ("cut", "awk", "gawk", "mawk", "sed", "grep", "egrep", "fgrep", "rg", "wc"):
            return fed[0]                              # `... done < .env`: the loop body gets every line
        hits = [h for h in (secret_env(w) for w in words[1:]) if h] + fed
        inline = re.findall(r"[\w./\\-]*\.env[\w.*-]*", segment)
        if prog in INTERPRETERS:
            # python -c "print(open('.env').read())", node -e ..., bash -c 'cat .env'
            inline_code = any(w in ("-c", "-e", "-Command", "-command", "--eval", "-r", "-p") for w in words[1:])
            if inline_code and PRINTS.search(segment):
                hits += [secret_env(w) for w in inline if secret_env(w)]
            elif not re.search(r"<\s*\S*\.env", segment):
                continue                               # `python app.py --env-file .env`: the program loads it
        if not hits:
            continue
        if prog in INTERPRETERS or prog in READERS or prog == "":
            if prog in READERS and _names_only(prog, words):
                continue
            return hits[0]
        if prog in ("cp", "mv", "ln", "rm", "touch", "chmod", "chown", "stat", "ls", "test", "[", "git", "wc",
                    "file", "du", "find", "docker", "docker-compose", "npm", "npx", "pnpm", "yarn", "uv",
                    "make", "pytest", "uvicorn", "flask", "dotenvx"):
            continue
    return None


def _event_parts(event: dict) -> tuple[str, dict]:
    """(tool name, arguments) from any client's payload."""
    call = event.get("toolCall") if isinstance(event.get("toolCall"), dict) else {}
    name = event.get("tool_name") or event.get("toolName") or call.get("name") or event.get("tool") or ""
    args = event.get("tool_input", event.get("toolArgs", call.get("args", event.get("args"))))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"command": args}
    if not isinstance(args, dict):
        args = {}
    for key in PATH_KEYS + COMMAND_KEYS:          # Cursor's beforeReadFile / beforeShellExecution: top level
        if key in event and key not in args and isinstance(event[key], str):
            args[key] = event[key]
    return str(name), args


def decide(event: dict) -> str | None:
    """The reason to block this tool call, or None to allow it."""
    name, args = _event_parts(event)
    for key in COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, list):
            value = " ".join(map(str, value))
        if isinstance(value, str):
            hit = check_command(value)
            if hit:
                return REASON.format(file=hit)
    if re.sub(r"^.*__", "", name).lower().replace("-", "_") in WRITE_TOOLS:
        return None
    for key in PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and secret_env(value):
            return REASON.format(file=value)
    return None


def respond(client: str, reason: str | None) -> int:
    if client == "cursor":
        out = {"permission": "allow"} if reason is None else {
            "permission": "deny", "user_message": "Phinite plugin: blocked reading a .env file's values.",
            "agent_message": reason}
        print(json.dumps(out))
        return 0
    if reason is None:
        return 0
    if client == "copilot":
        print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}))
        return 2
    if client == "antigravity":
        print(json.dumps({"decision": "deny", "reason": reason}))
        return 0
    print(reason, file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    client = argv[argv.index("--client") + 1] if "--client" in argv[:-1] else "claude"
    if os.environ.get("PHINITE_ALLOW_ENV_READ") == "1":
        return respond(client, None)
    try:
        event = json.loads(sys.stdin.read() or "{}")
        reason = decide(event) if isinstance(event, dict) else None
    except Exception:                               # never break the client on an odd payload
        reason = None
    return respond(client, reason)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
