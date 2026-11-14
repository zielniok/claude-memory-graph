# Installation and use

## Terms

- **MCP server:** a small helper program that Claude Code starts on your own computer to get extra tools. It is not a machine on the internet.
- **Memory server:** the MCP server used here. It manages the graph file and gives Claude tools to search and change it. Claude does not open the file itself, it asks the memory server.
- **`@modelcontextprotocol/server-memory`:** the name of the memory server in npm, the package directory of Node.js. `@modelcontextprotocol` is the publisher, `server-memory` the package.
- **Frontmatter:** the header block at the top of a Markdown file, between two lines of `---`. It holds details about the file as `key: value`. Claude Code writes it into every memory file.

A memory file with frontmatter:

```
---
name: backup-rotation
description: Nightly backups, seven daily and four weekly copies on the second disk.
metadata:
  type: project
  modified: 2026-04-10T09:00:00.000Z
---

Backups run nightly at 02:30. Seven daily and four weekly copies are kept on the second disk, older ones are deleted.
```

## Requirements

| | Version | Needed for |
|---|---|---|
| Claude Code | current | Writes the memory files. Searches the graph with the tools of the memory server. |
| Node.js | 24 or later | Runs the memory server. |
| Python | 3.9 or later | The import script. No extra packages. |
| Browser | current | The viewer. |

Tested on Windows 11 with Node.js 24.22, Python 3.10 and `@modelcontextprotocol/server-memory` 2.0.0. The import script and the commands in this guide are untested on macOS and Linux.

The viewer runs in the browser and does not depend on the operating system. It was tested in Chromium and Firefox.

## Setup

The examples assume the repository is in `C:\tools\claude-memory-graph`. Use your own folder instead.

### 1. Get the files

Clone the repository into the folder where it should stay, for example `C:\tools`:

```
git clone https://github.com/zielniok/claude-memory-graph.git
```

This creates the folder `claude-memory-graph` with all files. Without Git, choose "Code" and then "Download ZIP" on the GitHub page of the repository and unpack the file.

### 2. Install the memory server

```
npm install -g @modelcontextprotocol/server-memory
```

### 3. Register the server in Claude Code

The server stores the graph in the file named by `MEMORY_FILE_PATH`. Point it to `knowledge-graph.jsonl` in the repository folder. With `--scope user` the server is available in every project.

Windows, PowerShell:

```powershell
claude mcp add memory-mcp --scope user -e "MEMORY_FILE_PATH=C:\tools\claude-memory-graph\knowledge-graph.jsonl" -- node "$(npm root -g)\@modelcontextprotocol\server-memory\dist\index.js"
```

macOS and Linux, with the repository in `~/tools/claude-memory-graph` (untested):

```bash
claude mcp add memory-mcp --scope user -e "MEMORY_FILE_PATH=$HOME/tools/claude-memory-graph/knowledge-graph.jsonl" -- node "$(npm root -g)/@modelcontextprotocol/server-memory/dist/index.js"
```

Restart Claude Code afterwards. The tools of the server appear as `mcp__memory-mcp__search_nodes` and so on.

### 4. Configure the import

Copy `config-example.json` to `config.json` in the same folder and edit it. `config.json` is ignored by Git, so private paths stay on your machine.

| Key | Meaning | Default |
|---|---|---|
| `graph` | The graph file. Must be the file from `MEMORY_FILE_PATH`. A relative path is relative to `config.json`. | `knowledge-graph.jsonl` |
| `roots` | The folders to search, each with a short name. The name becomes the first part of `source:` in the graph. Allowed are lowercase letters, digits, `-` and `_`. `~` is your home folder. | required |
| `skip_dirs` | Folder names that are not searched. `*` and `?` are wildcards, case is ignored. `node_modules`, `.git`, `.venv`, `venv` and `__pycache__` are always skipped. | none |
| `skip_files` | Memory files to leave out, written as the import reports them, for example `projects/acme-shop/.claude/memory/old-note.md`. | none |
| `strip_prefix` | Characters removed from the start of project folder names, for example `#`. | none |
| `max_depth` | How many folder levels below a root are searched. | `8` |

Which root to name depends on where your memory files are:

- **Default of Claude Code:** the memory of each project is in `~/.claude/projects/<project>/memory/`. Use `~/.claude/projects` as root, as in `config-example.json`. The project name in the graph is the folder name Claude Code gave the project.
- **Memory inside the projects:** if `autoMemoryDirectory` in the settings of your projects points to `<project>/.claude/memory`, use the folder that contains your projects as root. The project name in the graph is the name of the project folder.

Both layouts can be mixed, and you can name several roots.

A memory file is expected to have frontmatter with `name`, `description` and, below `metadata`, `type` and `modified`, as in the example under Terms. A file without frontmatter is imported with its file name as name and the type `memory`. `MEMORY.md`, the index of a memory folder, is not imported.

### 5. Run the import

Check first what would happen. Nothing is written:

```
python memory-graph-import.py --dry-run
```

Then write the graph:

```
python memory-graph-import.py
```

Example output:

```
Memory folders:
    5  projects/acme-shop/.claude/memory  -> acme-shop
    2  projects/acme-shop/apps/newsletter/.claude/memory  -> newsletter
    4  projects/garden-blog/.claude/memory  -> garden-blog
    3  projects/home-server/.claude/memory  -> home-server
Import: 18 entities (feedback 6, project 4, project_folder 4, reference 3, user 1), 27 relations (belongs_to 14, links_to 12, part_of 1)
Kept from the graph: 0 entities, 0 relations, 0 unreadable lines
Written: C:\tools\claude-memory-graph\knowledge-graph.jsonl (9851 bytes, 18 entities, 27 relations)
```

Other lines the import can print:

| Line | Meaning |
|---|---|
| `Skipped` | The file is listed in `skip_files`. |
| `Not readable` | The file could not be read and was left out. |
| `Link without target` | A `[[name]]` points to a name that is not in the graph. |
| `Root "x" is not available here` | The folder of this root does not exist on this machine. Its entries stay as they are. |
| `Removed, source is gone` | The memory file was deleted or renamed, its entity was removed. |
| `Changed` | Observations of this entity were replaced, because the file changed or because Claude had added observations to it. |
| `Relation without entity` | A relation that Claude created points to an entity that no longer exists. |
| `Already up to date, not written` | The graph already matches the memory files. |
| `Aborted, nothing written` | An error. The graph file is unchanged. |

Run the import again whenever the memory files have changed. There is no automatic run.

## Using the output

### In Claude Code

The graph is never loaded into a session by itself. Claude reads it only when it calls a tool of the memory server, so ask for it:

- "Search the knowledge graph for backup."
- "Open the node backup-rotation in the knowledge graph and show what links to it."
- "Which memory entries belong to the project garden-blog?"

| Tool | Use |
|---|---|
| `search_nodes` | Finds entities whose name, type or observations contain the search text. Case is ignored. |
| `open_nodes` | Returns entities by their exact names, with their relations. |
| `read_graph` | Returns the whole graph. A large graph fills much of the context, prefer the two tools above. |

The search is a plain substring search. `seo audit` finds exactly this text, not "audit for SEO".

Claude can also write to the graph with `create_entities`, `create_relations` and `add_observations`. The import keeps entities and relations that Claude created. It replaces observations that Claude added to an imported entity, so lasting facts belong into the memory files.

### In the viewer

Open `knowledge-graph-viewer.html` in a browser, then choose "Open file" or drop the graph file onto the page. `example-graph.jsonl` shows what to expect. The file is read in the browser only. `vis-network.min.js` must be in the same folder as the viewer.

- **Nodes:** one colour per type, project folders are diamonds. The size grows with the number of relations.
- **Edges:** `links_to` is a solid arrow, `belongs_to` a dotted line, `part_of` a thick arrow. Other relation types are labelled.
- **Search:** dims everything that does not match. Enter jumps to the next match, exact names first.
- **Filter:** the project list shows one project, the type buttons hide and show types.
- **Details:** a click on a node shows its observations, its relations and its source. Escape closes the panel.

### The file itself

`knowledge-graph.jsonl` has one JSON object per line, first the entities, then the relations:

```json
{"type":"entity","name":"owner-prefers-short-answers","entityType":"user","observations":["The shop owner wants short answers and decisions with one recommendation.","Answers should be short. When there is a choice, give one recommendation and the reason in a sentence.","source: projects/acme-shop/.claude/memory/owner-prefers-short-answers.md","updated: 2026-01-08"]}
{"type":"relation","from":"owner-prefers-short-answers","to":"acme-shop","relationType":"belongs_to"}
```

The file contains everything that is in your memory files. `.gitignore` excludes every `.jsonl` file except `example-graph.jsonl`, so it is not committed by accident.

## Several machines

- Give each root the same name in the `config.json` of every machine. The folder behind the name may differ.
- A root whose folder is missing on a machine is left alone there. Its entries stay in the graph.
- If the graph file is synchronised between machines, write from one machine at a time. The lock file of the memory server does not work across machines.

## Removing

```
claude mcp remove memory-mcp --scope user
```

```
npm uninstall -g @modelcontextprotocol/server-memory
```

Then delete the repository folder. The memory files are not touched by any of this.
