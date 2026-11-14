"""Mirror the memory files of Claude Code into the knowledge graph 
of the MCP memory server (@modelcontextprotocol/server-memory).

Usage:  python memory-graph-import.py [--config PATH] [--dry-run]

The Markdown files stay the source of truth. Every run replaces the entries
that came from them (recognised by the observation "source: <root>/<path>")
and leaves everything else in the graph untouched. The file is written the way
the server writes it: lock file, temporary file, rename.

Settings are read from config.json next to this script, see
config-example.json and installation.md.
"""

import argparse
import fnmatch
import json
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = Path(os.path.abspath(__file__)).parent / "config.json"
# Folders that never contain memory and only cost time.
DEFAULT_SKIP_DIRS = ["node_modules", ".git", ".venv", "venv", "__pycache__"]
DEFAULT_MAX_DEPTH = 8
ROOT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
INDEX_FILE = "MEMORY.md"
FOLDER_TYPE = "project_folder"
DEFAULT_TYPE = "memory"
BELONGS_TO, PART_OF, LINKS_TO = "belongs_to", "part_of", "links_to"
OWN_RELATION_TYPES = {BELONGS_TO, PART_OF, LINKS_TO}
SOURCE_PREFIX = "source: "
UPDATED_PREFIX = "updated: "
# Same timing as the server: a lock older than this is considered abandoned.
LOCK_TIMEOUT = 60
LOCK_STALE = 30

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
FRONTMATTER_LINE = re.compile(r"^(\s*)([A-Za-z_]+):(.*)$")
LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
BLOCK_SEPARATOR = re.compile(r"\n[ \t]*\n")


class Abort(Exception):
    pass


def load_config(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise Abort(f"{path} not found. Copy config-example.json to config.json and adjust it.")
    except (OSError, ValueError) as error:
        raise Abort(f"{path} cannot be read: {error}")
    if not isinstance(raw, dict):
        raise Abort(f"{path} must contain a JSON object")

    def resolve(value):
        # Relative paths are relative to the folder of the config file.
        return Path(os.path.abspath(path.parent / Path(str(value)).expanduser()))

    def string_list(key):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise Abort(f'"{key}" in {path} must be a list of strings')
        return value

    roots = raw.get("roots")
    if not isinstance(roots, dict) or not roots:
        raise Abort(f'"roots" in {path} must name at least one folder')
    for name, folder in roots.items():
        if not ROOT_NAME.match(name):
            raise Abort(f'Root name "{name}": use lowercase letters, digits, "-" and "_" only')
        if not isinstance(folder, str) or not folder:
            raise Abort(f'Root "{name}" in {path} needs a folder')
    max_depth = raw.get("max_depth", DEFAULT_MAX_DEPTH)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
        raise Abort(f'"max_depth" in {path} must be a positive number')
    strip_prefix = raw.get("strip_prefix", "")
    if not isinstance(strip_prefix, str):
        raise Abort(f'"strip_prefix" in {path} must be a string')
    return {
        "graph": resolve(raw.get("graph") or "knowledge-graph.jsonl"),
        "roots": {name: resolve(folder) for name, folder in roots.items()},
        "skip_dirs": [pattern.lower() for pattern in DEFAULT_SKIP_DIRS + string_list("skip_dirs")],
        "skip_files": {item.replace("\\", "/").casefold() for item in string_list("skip_files")},
        "strip_prefix": strip_prefix,
        "max_depth": max_depth,
    }


def source_id(root_name, root, path):
    """Machine-independent id of a file or folder: root name plus relative path."""
    relative = path.relative_to(root).as_posix()
    return root_name if relative == "." else f"{root_name}/{relative}"


def find_memory_dirs(config):
    """Return (root name, root, memory folder, project folder) for every memory folder.

    Two layouts are recognised: <project>/.claude/memory (autoMemoryDirectory
    inside the project) and <project>/memory with a MEMORY.md, which is what
    Claude Code creates by default below ~/.claude/projects.
    """
    found = []

    def skipped(name):
        return any(fnmatch.fnmatchcase(name.lower(), pattern) for pattern in config["skip_dirs"])

    def check(root_name, root, directory):
        memory = directory / ".claude" / "memory"
        if memory.is_dir():
            found.append((root_name, root, memory, directory))
        memory = directory / "memory"
        if (memory / INDEX_FILE).is_file():
            found.append((root_name, root, memory, directory))

    def walk(root_name, root, directory, depth):
        check(root_name, root, directory)
        if depth >= config["max_depth"]:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name == ".claude" or skipped(entry.name):
                continue
            if entry.name == "memory" and (Path(entry.path) / INDEX_FILE).is_file():
                continue
            walk(root_name, root, Path(entry.path), depth + 1)

    for root_name, root in config["roots"].items():
        if root.is_dir():
            walk(root_name, root, root, 1)
    return sorted(found, key=lambda item: str(item[2]).lower())


def yaml_value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def parse_memory(path):
    """Read one memory file. Without frontmatter the file name is the name."""
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    meta = {}
    body = text
    match = FRONTMATTER.match(text)
    if match:
        body = match.group(2)
        for line in match.group(1).split("\n"):
            field = FRONTMATTER_LINE.match(line)
            if not field:
                continue
            nested, key, value = bool(field.group(1)), field.group(2), field.group(3)
            if (not nested and key in ("name", "description")) or key in ("type", "modified"):
                meta.setdefault(key, yaml_value(value))
    return {
        "name": meta.get("name") or path.stem,
        "type": meta.get("type") or DEFAULT_TYPE,
        "description": meta.get("description", ""),
        "modified": meta.get("modified", ""),
        "blocks": [b.strip() for b in BLOCK_SEPARATOR.split(body.strip()) if b.strip()],
        "links": [l.strip() for l in LINK.findall(text)],
    }


def project_of(root, owner, strip_prefix):
    """Top-level folder below the root is the project, a deeper owner is a subfolder of it."""
    if owner == root:
        return root.name.lstrip(strip_prefix) or root.name, root, None
    top_dir = root / owner.relative_to(root).parts[0]
    top = top_dir.name.lstrip(strip_prefix) or top_dir.name
    return top, top_dir, (owner.name if owner != top_dir else None)


def unique(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def build(config):
    """Turn the memory files into entities, relations and unresolved [[links]]."""
    entities = {}
    origin = {}
    relations = []
    links = []
    report = {"dirs": [], "skipped": [], "unreadable": [], "collisions": []}
    memories = []

    def add_entity(name, entity_type, observations, source):
        if name in entities:
            report["collisions"].append(f"{name} ({origin[name]} and {source})")
            return
        origin[name] = source
        entities[name] = {
            "name": name,
            "entityType": entity_type,
            "observations": unique(observations + [SOURCE_PREFIX + source]),
        }

    for root_name, root, memory_dir, owner in find_memory_dirs(config):
        parsed = []
        for path in sorted(memory_dir.glob("*.md"), key=lambda p: p.name.lower()):
            if path.name == INDEX_FILE:
                continue
            source = source_id(root_name, root, path)
            if source.casefold() in config["skip_files"]:
                report["skipped"].append(source)
                continue
            try:
                memory = parse_memory(path)
            except (OSError, UnicodeDecodeError) as error:
                report["unreadable"].append(f"{source}: {error}")
                continue
            memory["source"] = source
            parsed.append(memory)
        memory_source = source_id(root_name, root, memory_dir)
        if not parsed:
            report["dirs"].append((memory_source, None, 0))
            continue
        top, top_dir, sub = project_of(root, owner, config["strip_prefix"])
        top_source = source_id(root_name, root, top_dir)
        if origin.get(top) != top_source:
            add_entity(top, FOLDER_TYPE, [], top_source)
        holder = top
        if sub:
            holder = sub
            sub_source = source_id(root_name, root, owner)
            if origin.get(sub) != sub_source:
                add_entity(sub, FOLDER_TYPE, [], sub_source)
                relations.append((sub, top, PART_OF))
        report["dirs"].append((memory_source, holder, len(parsed)))
        for memory in parsed:
            memories.append((memory, holder))

    for memory, holder in memories:
        observations = []
        if memory["description"]:
            observations.append(memory["description"])
        observations.extend(memory["blocks"])
        entity_count = len(entities)
        add_entity(memory["name"], memory["type"], observations, memory["source"])
        if len(entities) == entity_count:
            continue
        if memory["modified"]:
            entities[memory["name"]]["observations"].append(UPDATED_PREFIX + memory["modified"][:10])
        relations.append((memory["name"], holder, BELONGS_TO))
        for target in memory["links"]:
            if target != memory["name"]:
                links.append((memory["name"], target))

    return entities, unique(relations), unique(links), report


def load_graph(path):
    entities, relations, unreadable = [], [], []
    if not path.exists():
        return entities, relations, unreadable
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            unreadable.append(line)
            continue
        if isinstance(item, dict) and item.get("type") == "entity" and isinstance(item.get("name"), str):
            entities.append(item)
        elif isinstance(item, dict) and item.get("type") == "relation":
            relations.append(item)
        else:
            unreadable.append(line)
    return entities, relations, unreadable


def source_root(entity, root_names):
    """Name of the configured root an entity was imported from, else None."""
    for observation in entity.get("observations", []):
        if isinstance(observation, str) and observation.startswith(SOURCE_PREFIX):
            root_name = observation[len(SOURCE_PREFIX):].split("/", 1)[0]
            if root_name in root_names:
                return root_name
    return None


def merge(generated_entities, base_relations, links, entities, relations, config):
    """Replace what the import owns, keep the rest.

    Owned are entities from a root whose folder exists on this machine. Entities
    from a root that is not available here, and entities without a source, stay
    exactly as they are.
    """
    available = {name for name, folder in config["roots"].items() if folder.is_dir()}
    replaced = {}
    kept = []
    unavailable = {}
    for entity in entities:
        root_name = source_root(entity, config["roots"])
        if root_name in available:
            replaced[entity["name"]] = entity
        else:
            kept.append(entity)
            if root_name:
                unavailable[root_name] = unavailable.get(root_name, 0) + 1
    conflicts = [e["name"] for e in kept if e["name"] in generated_entities]
    if conflicts:
        raise Abort("Entries in the graph that the import does not own use names from the import: " + ", ".join(conflicts))

    final_entities = kept + list(generated_entities.values())
    names = {e["name"] for e in final_entities}
    missing = [(source, target) for source, target in links if target not in names]
    generated_relations = unique(
        base_relations + [(source, target, LINKS_TO) for source, target in links if target in names]
    )
    kept_relations = [
        r for r in relations
        if not (r.get("relationType") in OWN_RELATION_TYPES and r.get("from") in replaced)
    ]
    kept_keys = {(r.get("from"), r.get("to"), r.get("relationType")) for r in kept_relations}
    final_relations = kept_relations + [
        {"from": f, "to": t, "relationType": rt}
        for f, t, rt in generated_relations if (f, t, rt) not in kept_keys
    ]

    stats = {
        "generated_relations": generated_relations,
        "kept_entities": len(kept) - sum(unavailable.values()),
        "kept_relations": len(kept_relations),
        "unavailable": unavailable,
        "missing": missing,
        "removed": sorted(n for n in replaced if n not in generated_entities),
        "changed": {},
        "dangling": [r for r in final_relations if r.get("from") not in names or r.get("to") not in names],
    }
    for name, old in replaced.items():
        if name in generated_entities:
            new_observations = set(generated_entities[name]["observations"])
            dropped = [o for o in old.get("observations", []) if o not in new_observations]
            if dropped:
                stats["changed"][name] = len(dropped)
    return final_entities, final_relations, stats


def serialize(entities, relations, unreadable):
    def dump(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    lines = [
        dump({"type": "entity", "name": e["name"], "entityType": e.get("entityType", ""), "observations": e.get("observations", [])})
        for e in entities
    ]
    lines += [
        dump({"type": "relation", "from": r.get("from"), "to": r.get("to"), "relationType": r.get("relationType")})
        for r in relations
    ]
    lines += unreadable
    return ("\n".join(lines) + "\n").encode("utf-8")


def acquire_lock(lock):
    token = json.dumps(
        {"pid": os.getpid(), "hostname": socket.gethostname(), "token": secrets.token_hex(16)},
        separators=(",", ":"),
    )
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > LOCK_STALE:
                raise Abort(f"Lock file {lock} is {age:.0f} s old and is not being refreshed. "
                            "Delete it if no other program is writing the graph.")
        except PermissionError:
            pass  # Windows: the lock file is being removed right now
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
            return token
        if time.monotonic() > deadline:
            raise Abort(f"Lock file {lock} still held after {LOCK_TIMEOUT} s")
        time.sleep(0.05)


def release_lock(lock, token):
    try:
        if lock.read_text(encoding="utf-8") == token:
            lock.unlink()
    except FileNotFoundError:
        pass


def write_graph(graph, data):
    temp = graph.with_name(f".{graph.name}.{os.getpid()}.tmp")
    temp.write_bytes(data)
    for _ in range(50):
        try:
            os.replace(temp, graph)
            return
        except PermissionError:
            time.sleep(0.1)
    temp.unlink()
    raise Abort(f"{graph} is locked by another program")


def print_report(report, generated_entities, stats, unreadable):
    print("Memory folders:")
    for source, holder, count in report["dirs"]:
        print(f"  {count:3}  {source}" + (f"  -> {holder}" if holder else ""))
    for source in report["skipped"]:
        print(f"Skipped: {source}")
    for message in report["unreadable"]:
        print(f"Not readable: {message}")
    for source, target in stats["missing"]:
        print(f"Link without target: {source} -> {target}")

    by_type = {}
    for entity in generated_entities.values():
        by_type[entity["entityType"]] = by_type.get(entity["entityType"], 0) + 1
    rel_types = {}
    for _, _, relation_type in stats["generated_relations"]:
        rel_types[relation_type] = rel_types.get(relation_type, 0) + 1
    print(f"Import: {len(generated_entities)} entities ("
          + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())) + "), "
          + f"{len(stats['generated_relations'])} relations ("
          + ", ".join(f"{k} {v}" for k, v in sorted(rel_types.items())) + ")")
    print(f"Kept from the graph: {stats['kept_entities']} entities, "
          f"{stats['kept_relations']} relations, {len(unreadable)} unreadable lines")
    for root_name, count in sorted(stats["unavailable"].items()):
        print(f'Root "{root_name}" is not available here, its {count} entities stay unchanged')
    for name in stats["removed"]:
        print(f"Removed, source is gone: {name}")
    for name, count in sorted(stats["changed"].items()):
        print(f"Changed: {name}, {count} old observations replaced")
    for relation in stats["dangling"]:
        print(f"Relation without entity: {relation.get('from')} -> {relation.get('to')} ({relation.get('relationType')})")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Mirror Claude Code memory files into the knowledge graph of the MCP memory server"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="settings file (default: config.json next to the script)")
    parser.add_argument("--dry-run", action="store_true", help="only count and report, write nothing")
    args = parser.parse_args()

    try:
        config = load_config(Path(os.path.abspath(args.config.expanduser())))
        graph = config["graph"]
        generated_entities, base_relations, links, report = build(config)
        if report["collisions"]:
            raise Abort("Names used more than once: " + "; ".join(unique(report["collisions"])))

        if args.dry_run:
            entities, relations, unreadable = load_graph(graph)
            _, _, stats = merge(generated_entities, base_relations, links, entities, relations, config)
            print_report(report, generated_entities, stats, unreadable)
            print(f"Dry run, {graph} not written.")
            return 0

        graph.parent.mkdir(parents=True, exist_ok=True)
        lock = graph.with_name(graph.name + ".lock")
        token = acquire_lock(lock)
        try:
            entities, relations, unreadable = load_graph(graph)
            final_entities, final_relations, stats = merge(generated_entities, base_relations, links, entities, relations, config)
            data = serialize(final_entities, final_relations, unreadable)
            changed = not graph.exists() or graph.read_bytes() != data
            if changed:
                write_graph(graph, data)
        finally:
            release_lock(lock, token)
        print_report(report, generated_entities, stats, unreadable)
        summary = f"{graph} ({len(data)} bytes, {len(final_entities)} entities, {len(final_relations)} relations)"
        print(("Written: " if changed else "Already up to date, not written: ") + summary)
        return 0
    except Abort as error:
        print(f"Aborted, nothing written: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
