#!/usr/bin/env python3
"""
File System Assistant
---------------------
A local, privacy-friendly command-line assistant powered by Ollama.
You describe what you want in plain English ("find all invoices from 2024",
"move the screenshots into a folder called Archive", "what's in notes.txt?")
and a local model decides which file operations to run.

All operations are sandboxed to a root directory you choose. Deleting or
overwriting files requires explicit confirmation.

Requirements
    pip install ollama
    ollama pull llama3.1          # or any model with tool-calling support,
                                  # e.g. qwen2.5, mistral-nemo, llama3.2

Usage
    python file_system_assistant.py --root ~/Documents --model llama3.1
    python file_system_assistant.py --root . --model qwen2.5 --yes   # skip confirmations
"""

import argparse
import fnmatch
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import ollama
except ImportError:
    sys.exit("The 'ollama' package is missing. Install it with: pip install ollama")


MAX_READ_BYTES = 1_000_000      # refuse to load files larger than ~1 MB into context
MAX_LIST_ENTRIES = 200
MAX_SEARCH_RESULTS = 100
MAX_AGENT_STEPS = 12            # safety cap on tool-call rounds per user request

# ANSI colors for readable terminal output
DIM, BOLD, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"


# ---------------------------------------------------------------------------
# Tool schemas (what the model sees)
# ---------------------------------------------------------------------------
def _tool(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
            },
        },
    }


TOOL_SCHEMAS = [
    _tool(
        "list_directory",
        "List the contents of a directory with names, types, sizes and modification times.",
        {"path": {"type": "string", "description": "Directory path relative to the root. Use '.' for the root."}},
        required=[],
    ),
    _tool(
        "file_info",
        "Get metadata (size, type, timestamps) for a file or directory.",
        {"path": {"type": "string", "description": "Path relative to the root."}},
        required=["path"],
    ),
    _tool(
        "read_text_file",
        "Read the text content of a file. Use this to answer questions about what a file contains.",
        {
            "path": {"type": "string", "description": "File path relative to the root."},
            "max_chars": {"type": "integer", "description": "Maximum characters to return (default 4000)."},
        },
        required=["path"],
    ),
    _tool(
        "write_text_file",
        "Create a new text file, or overwrite an existing one when overwrite is true. Overwrites need user confirmation.",
        {
            "path": {"type": "string", "description": "File path relative to the root."},
            "content": {"type": "string", "description": "Full text content to write."},
            "overwrite": {"type": "boolean", "description": "Allow replacing an existing file."},
        },
        required=["path", "content"],
    ),
    _tool(
        "create_directory",
        "Create a directory (including any missing parents).",
        {"path": {"type": "string", "description": "Directory path relative to the root."}},
        required=["path"],
    ),
    _tool(
        "move_path",
        "Move or rename a file or directory. If destination is an existing directory, the source is moved into it.",
        {
            "source": {"type": "string", "description": "Existing path relative to the root."},
            "destination": {"type": "string", "description": "New path or target directory, relative to the root."},
        },
        required=["source", "destination"],
    ),
    _tool(
        "copy_path",
        "Copy a file or directory.",
        {
            "source": {"type": "string", "description": "Existing path relative to the root."},
            "destination": {"type": "string", "description": "Destination path relative to the root."},
        },
        required=["source", "destination"],
    ),
    _tool(
        "delete_path",
        "Permanently delete a file or directory (directories are deleted recursively). Requires user confirmation.",
        {"path": {"type": "string", "description": "Path relative to the root."}},
        required=["path"],
    ),
    _tool(
        "search_files",
        "Find files or directories by name using a glob pattern such as '*.pdf', 'report*', or 'invoice_2024*.xlsx'. Searches recursively.",
        {
            "pattern": {"type": "string", "description": "Glob pattern matched against file names."},
            "path": {"type": "string", "description": "Directory to search in, relative to the root (default '.')."},
        },
        required=["pattern"],
    ),
    _tool(
        "search_content",
        "Search inside text files for a case-insensitive phrase and return matching lines with their locations.",
        {
            "query": {"type": "string", "description": "Text to look for."},
            "path": {"type": "string", "description": "Directory to search in, relative to the root (default '.')."},
        },
        required=["query"],
    ),
]

TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}


# ---------------------------------------------------------------------------
# Sandboxed file operations
# ---------------------------------------------------------------------------
class FileSystemTools:
    def __init__(self, root: Path, auto_confirm: bool = False):
        self.root = root.expanduser().resolve()
        self.auto_confirm = auto_confirm

    # -- helpers -----------------------------------------------------------
    def _resolve(self, path: str) -> Path:
        """Resolve a user/model supplied path and make sure it stays inside the root."""
        target = (self.root / (path or ".")).resolve()   # absolute paths replace root in pathlib
        if target != self.root and self.root not in target.parents:
            raise PermissionError(f"'{path}' is outside the allowed root ({self.root})")
        return target

    def _rel(self, p: Path) -> str:
        return "." if p == self.root else str(p.relative_to(self.root))

    def _confirm(self, question: str) -> bool:
        if self.auto_confirm:
            return True
        print(f"\n{YELLOW}⚠  {question}{RESET}")
        answer = input(f"{YELLOW}   Proceed? [y/N]: {RESET}").strip().lower()
        return answer in ("y", "yes")

    # -- read-only tools ---------------------------------------------------
    def list_directory(self, path: str = ".") -> dict:
        target = self._resolve(path)
        if not target.is_dir():
            return {"error": f"'{path}' is not a directory"}
        entries = []
        for child in sorted(target.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if len(entries) >= MAX_LIST_ENTRIES:
                break
            stat = child.stat()
            entries.append({
                "name": child.name,
                "type": "directory" if child.is_dir() else "file",
                "size_bytes": None if child.is_dir() else stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        return {"path": self._rel(target), "count": len(entries), "entries": entries}

    def file_info(self, path: str) -> dict:
        target = self._resolve(path)
        if not target.exists():
            return {"error": f"'{path}' does not exist"}
        stat = target.stat()
        return {
            "path": self._rel(target),
            "type": "directory" if target.is_dir() else "file",
            "size_bytes": stat.st_size,
            "extension": target.suffix or None,
            "created": datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds"),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        }

    def read_text_file(self, path: str, max_chars: int = 4000) -> dict:
        target = self._resolve(path)
        if not target.is_file():
            return {"error": f"'{path}' is not a file"}
        if target.stat().st_size > MAX_READ_BYTES:
            return {"error": f"'{path}' is larger than {MAX_READ_BYTES // 1000} KB"}
        text = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(text) > max_chars
        return {"path": self._rel(target), "content": text[:max_chars], "truncated": truncated}

    def search_files(self, pattern: str, path: str = ".") -> dict:
        base = self._resolve(path)
        matches = []
        for item in base.rglob("*"):
            if fnmatch.fnmatch(item.name.lower(), pattern.lower()):
                matches.append(self._rel(item) + ("/" if item.is_dir() else ""))
                if len(matches) >= MAX_SEARCH_RESULTS:
                    break
        return {"pattern": pattern, "count": len(matches), "matches": matches}

    def search_content(self, query: str, path: str = ".") -> dict:
        base = self._resolve(path)
        needle = query.lower()
        hits = []
        for item in base.rglob("*"):
            if not item.is_file() or item.stat().st_size > MAX_READ_BYTES:
                continue
            try:
                lines = item.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue  # skip binary files
            for number, line in enumerate(lines, start=1):
                if needle in line.lower():
                    hits.append({"file": self._rel(item), "line": number, "text": line.strip()[:200]})
                    if len(hits) >= MAX_SEARCH_RESULTS:
                        return {"query": query, "count": len(hits), "matches": hits, "truncated": True}
        return {"query": query, "count": len(hits), "matches": hits, "truncated": False}

    # -- write / destructive tools ------------------------------------------
    def write_text_file(self, path: str, content: str, overwrite: bool = False) -> dict:
        target = self._resolve(path)
        if target.is_dir():
            return {"error": f"'{path}' is a directory"}
        if target.exists():
            if not overwrite:
                return {"error": f"'{path}' already exists. Set overwrite=true to replace it."}
            if not self._confirm(f"Overwrite existing file '{self._rel(target)}'?"):
                return {"error": "User declined the overwrite."}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"success": True, "path": self._rel(target), "bytes_written": len(content.encode("utf-8"))}

    def create_directory(self, path: str) -> dict:
        target = self._resolve(path)
        target.mkdir(parents=True, exist_ok=True)
        return {"success": True, "path": self._rel(target)}

    def move_path(self, source: str, destination: str) -> dict:
        src, dst = self._resolve(source), self._resolve(destination)
        if not src.exists():
            return {"error": f"'{source}' does not exist"}
        if src == self.root:
            return {"error": "Cannot move the root directory"}
        final = shutil.move(str(src), str(dst))
        return {"success": True, "moved_to": self._rel(Path(final))}

    def copy_path(self, source: str, destination: str) -> dict:
        src, dst = self._resolve(source), self._resolve(destination)
        if not src.exists():
            return {"error": f"'{source}' does not exist"}
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=False)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return {"success": True, "copied_to": self._rel(dst)}

    def delete_path(self, path: str) -> dict:
        target = self._resolve(path)
        if not target.exists():
            return {"error": f"'{path}' does not exist"}
        if target == self.root:
            return {"error": "Refusing to delete the root directory"}
        kind = "directory and everything inside it" if target.is_dir() else "file"
        if not self._confirm(f"Permanently delete {kind}: '{self._rel(target)}'?"):
            return {"error": "User declined the deletion."}
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"success": True, "deleted": self._rel(target)}

    # -- dispatcher ---------------------------------------------------------
    def execute(self, name: str, arguments: dict) -> dict:
        if name not in TOOL_NAMES:
            return {"error": f"Unknown tool '{name}'"}
        handler = getattr(self, name)
        try:
            return handler(**(arguments or {}))
        except TypeError as exc:
            return {"error": f"Invalid arguments for {name}: {exc}"}
        except Exception as exc:  # keep the agent loop alive; the model can recover
            return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Assistant (Ollama agent loop)
# ---------------------------------------------------------------------------
class FileSystemAssistant:
    def __init__(self, model: str, tools: FileSystemTools, host: str | None = None, verbose: bool = True):
        self.model = model
        self.tools = tools
        self.client = ollama.Client(host=host) if host else ollama.Client()
        self.verbose = verbose
        self.messages = [{"role": "system", "content": self._system_prompt()}]

    def _system_prompt(self) -> str:
        today = datetime.now().strftime("%A, %Y-%m-%d")
        return (
            "You are a File System Assistant running locally on the user's computer. "
            f"Today is {today}. You can only access files inside the root directory: {self.tools.root}. "
            "All tool paths are relative to that root ('.' means the root itself). "
            "Use the provided tools to inspect, search, read, and manage files; never invent file names "
            "or contents. When the user asks what a file contains, read it first. When asked to find files, "
            "search first. Chain several tool calls when a task needs them. Deletions and overwrites require "
            "user confirmation, which the tools handle; do not claim something was done unless a tool confirmed it. "
            "After acting, reply with a short, clear summary of what you did or found."
        )

    def reset(self):
        self.messages = [{"role": "system", "content": self._system_prompt()}]

    def ask(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_AGENT_STEPS):
            response = self.client.chat(model=self.model, messages=self.messages, tools=TOOL_SCHEMAS)
            message = response.message
            self.messages.append(message)

            if not message.tool_calls:
                return (message.content or "").strip()

            for call in message.tool_calls:
                name = call.function.name
                args = dict(call.function.arguments or {})
                if self.verbose:
                    print(f"{DIM}  ↳ {name}({', '.join(f'{k}={v!r}' for k, v in args.items())}){RESET}")
                result = self.tools.execute(name, args)
                self.messages.append({"role": "tool", "name": name, "content": json.dumps(result, default=str)})

        return "I stopped after too many steps. Try breaking the request into smaller parts."


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------
HELP_TEXT = f"""
{BOLD}Commands{RESET}
  /help     show this help
  /cwd      show the sandbox root directory
  /reset    clear the conversation memory
  /exit     quit (or press Ctrl+C)

{BOLD}Examples{RESET}
  what's in this folder?
  find all PDF files
  search for "invoice" inside text files
  read notes/todo.txt and summarize it
  create a folder called Archive and move every .log file into it
  make a file called ideas.md with three startup ideas
"""


def check_model(client: ollama.Client, model: str) -> None:
    try:
        client.show(model)
    except ollama.ResponseError as exc:
        if exc.status_code == 404:
            sys.exit(f"Model '{model}' is not installed. Run: ollama pull {model}")
        raise
    except Exception as exc:
        sys.exit(f"Cannot reach the Ollama server ({exc}). Is 'ollama serve' running?")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with your files using a local Ollama model.")
    parser.add_argument("--root", default=".", help="Sandbox directory the assistant may access (default: current folder)")
    parser.add_argument("--model", default="llama3.1", help="Ollama model with tool-calling support (default: llama3.1)")
    parser.add_argument("--host", default=None, help="Ollama server URL (default: http://localhost:11434)")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts for deletes and overwrites")
    parser.add_argument("--quiet", action="store_true", help="Hide tool-call traces")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"Root directory does not exist: {root}")

    tools = FileSystemTools(root, auto_confirm=args.yes)
    assistant = FileSystemAssistant(args.model, tools, host=args.host, verbose=not args.quiet)
    check_model(assistant.client, args.model)

    print(f"{BOLD}File System Assistant{RESET} · model: {CYAN}{args.model}{RESET} · root: {CYAN}{tools.root}{RESET}")
    print("Type /help for commands. Ask me anything about your files.\n")

    while True:
        try:
            user_input = input(f"{BOLD}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            break
        if user_input == "/help":
            print(HELP_TEXT)
            continue
        if user_input == "/cwd":
            print(tools.root)
            continue
        if user_input == "/reset":
            assistant.reset()
            print(f"{DIM}Conversation cleared.{RESET}")
            continue

        try:
            reply = assistant.ask(user_input)
            print(f"\n{BOLD}assistant ›{RESET} {reply}\n")
        except ollama.ResponseError as exc:
            print(f"{RED}Ollama error: {exc}{RESET}\n")
        except Exception as exc:
            print(f"{RED}Unexpected error: {exc}{RESET}\n")

    print("Goodbye.")


if __name__ == "__main__":
    main()