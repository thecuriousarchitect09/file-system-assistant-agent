# File System Assistant

A local, command-line assistant that lets you manage files by typing plain-English requests. It runs on a local [Ollama](https://ollama.com) model, so your files and their contents never leave your computer.

```
you › find all PDF files and list the ones bigger than 1 MB
assistant › I found 14 PDF files. Three are larger than 1 MB: ...

you › create an Archive folder and move the .log files into it
assistant › Created Archive/ and moved 6 log files into it.
```

## Features

- **Natural-language file management**: list, search, read, create, move, copy, and delete files by describing what you want.
- **Multi-step tasks**: the assistant chains operations, such as creating a folder and then moving files into it.
- **Content search**: find text inside files, with file names and line numbers.
- **Sandboxed**: the assistant can only access the folder you choose. Paths that escape it, including `../` and absolute paths, are blocked.
- **Confirmation prompts**: deleting files and overwriting existing files require your approval (unless you use `--yes`).
- **Fully local**: no cloud APIs or accounts required.

## Requirements

- Python 3.10 or newer
- [Ollama](https://ollama.com/download) installed and running
- A model with tool-calling support, such as `llama3.1`, `qwen2.5`, `mistral-nemo`, or `llama3.2`

## Installation

1. Clone or download this project and move into its folder:

   ```bash
   cd file-system-assistant
   ```

2. Create and activate a virtual environment (recommended):

   ```bash
   python -m venv .venv
   source .venv/bin/activate        # macOS / Linux
   .venv\Scripts\activate           # Windows
   ```

3. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Start the Ollama server (skip if it already runs in the background) and download a model:

   ```bash
   ollama serve                     # in a separate terminal, if needed
   ollama pull llama3.1
   ```

## Usage

```bash
python file_system_assistant.py --root ~/Documents --model llama3.1
```

### Command-line options

| Option | Default | Description |
| --- | --- | --- |
| `--root` | `.` (current folder) | The sandbox directory the assistant may access |
| `--model` | `llama3.1` | Ollama model to use (must support tool calling) |
| `--host` | `http://localhost:11434` | URL of the Ollama server |
| `--yes` | off | Skip confirmation prompts for deletes and overwrites |
| `--quiet` | off | Hide the tool-call trace lines |

### Chat commands

| Command | Action |
| --- | --- |
| `/help` | Show help and example requests |
| `/cwd` | Show the sandbox root directory |
| `/reset` | Clear the conversation memory |
| `/exit` | Quit (Ctrl+C and Ctrl+D also work) |

### Example requests

- `what's in this folder?`
- `find all files named invoice*`
- `search for "budget" inside text files`
- `read notes/todo.txt and summarize it`
- `make a file called ideas.md with three startup ideas`
- `copy report.docx to backups/report_copy.docx`

## How it works

1. Your message is sent to the Ollama model along with descriptions of the available file tools.
2. If the model decides a tool is needed, the script runs it inside the sandbox and returns the result to the model.
3. The model repeats this until it can answer you.

Available tools: `list_directory`, `file_info`, `read_text_file`, `write_text_file`, `create_directory`, `move_path`, `copy_path`, `delete_path`, `search_files`, and `search_content`.

## Project structure

```
file-system-assistant/
├── file_system_assistant.py   # main script: tools, sandbox, agent loop, CLI
├── requirements.txt           # Python dependencies
└── README.md
```

## Safety notes

- Destructive operations (`delete_path`, and `write_text_file` when overwriting) ask for confirmation unless `--yes` is set. Deletions are permanent; the script does not use a trash folder.
- Files larger than about 1 MB are not read, to keep the model's context manageable.
- Binary files are skipped by content search.
- Start with a test folder, not your entire home directory, until you are comfortable with how the model behaves.

## Limitations

- Output quality depends on the model. Smaller models may skip tool calls, pass incorrect arguments, or misread results. Larger tool-capable models generally work better.
- Only text files can be read or searched. PDFs, Word documents, and images are listed and can be moved or copied, but their contents are not extracted.
- Each request is processed in the context of the current session. Use `/reset` to start fresh.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `Cannot reach the Ollama server` | Run `ollama serve`, or check `--host` |
| `Model 'x' is not installed` | Run `ollama pull x` |
| `ModuleNotFoundError: ollama` | Run `pip install -r requirements.txt` |
| Assistant ignores files or makes up results | Try a larger tool-capable model, or make the request more specific |

## License

Add your preferred license here.