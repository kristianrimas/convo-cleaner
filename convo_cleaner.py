"""
Conversation Manager (Claude + Codex)
A GUI tool to view and delete conversations from Claude Code and Codex.
"""

import hashlib
import json
import os
import re
import shutil
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

# Scratchpad folders are named after the session UUID. Matching on this shape keeps the
# sweep from touching anything in Temp\claude that is not a session folder.
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class ConvoCleanerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Conversation Manager (Claude + Codex)")
        self.root.geometry("1200x720")
        self.root.minsize(980, 520)

        # Paths
        self.claude_projects_path = Path.home() / ".claude" / "projects"
        self.codex_sessions_path = Path.home() / ".codex" / "sessions"
        self.codex_archived_path = Path.home() / ".codex" / "archived_sessions"
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        self.desktop_sessions_path = Path(appdata) / "Claude" / "claude-code-sessions"
        localappdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        self.scratchpad_path = Path(localappdata) / "Temp" / "claude"

        # Data storage
        self.groups_data = {}
        self.session_lookup = {}
        self._sort_column = None
        self._sort_reverse = False

        self.setup_ui()
        self.load_all()

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Top bar
        top_frame = ttk.Frame(main_frame)
        top_frame.pack(fill=tk.X, pady=(0, 10))

        self.stats_label = ttk.Label(top_frame, text="Loading...", font=("Segoe UI", 10))
        self.stats_label.pack(side=tk.LEFT)

        ttk.Button(top_frame, text="Refresh", command=self.load_all).pack(side=tk.RIGHT)

        # Search
        search_frame = ttk.Frame(main_frame)
        search_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(search_frame, text="Search:").pack(side=tk.LEFT, padx=(0, 5))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.filter_tree())
        ttk.Entry(search_frame, textvariable=self.search_var, width=44).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(search_frame, text="Clear", command=lambda: self.search_var.set("")).pack(side=tk.LEFT)

        # Treeview
        tree_frame = ttk.Frame(main_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("summary", "msgs", "created", "modified", "branch", "cwd", "size")
        self.tree = ttk.Treeview(tree_frame, columns=columns, selectmode="extended")

        self._heading_labels = {
            "#0": "Project / Session",
            "summary": "Summary",
            "msgs": "Msgs",
            "created": "Created",
            "modified": "Modified",
            "branch": "Branch",
            "cwd": "CWD",
            "size": "Size",
        }
        heading_anchors = {"msgs": tk.CENTER, "size": tk.E}
        for col, label in self._heading_labels.items():
            self.tree.heading(
                col, text=label, anchor=heading_anchors.get(col, tk.W),
                command=lambda c=col: self.sort_by(c),
            )

        self.tree.column("#0", width=250, minwidth=180)
        self.tree.column("summary", width=280, minwidth=180)
        self.tree.column("msgs", width=50, minwidth=40)
        self.tree.column("created", width=90, minwidth=80)
        self.tree.column("modified", width=90, minwidth=80)
        self.tree.column("branch", width=80, minwidth=60)
        self.tree.column("cwd", width=200, minwidth=100)
        self.tree.column("size", width=70, minwidth=50)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        # Buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(btn_frame, text="Expand All", command=self.expand_all).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Collapse All", command=self.collapse_all).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(
            btn_frame, text="Sweep Orphan Scratchpads", command=self.sweep_orphan_scratchpads
        ).pack(side=tk.LEFT, padx=(15, 5))

        self.delete_btn = ttk.Button(btn_frame, text="Delete Selected", command=self.delete_selected)
        self.delete_btn.pack(side=tk.RIGHT)

        ttk.Button(btn_frame, text="Select All Sessions", command=self.select_all_sessions).pack(
            side=tk.RIGHT, padx=(0, 10)
        )

        # Agent-only selection, with an age guard so a session you are still working in
        # never has its subagent transcripts swept up. Packed right-to-left, so this
        # reads as: [keep newer than (days)] [3] [Select [agent]].
        ttk.Button(btn_frame, text="Select [agent]", command=self.select_agent_sessions).pack(
            side=tk.RIGHT, padx=(0, 10)
        )
        self.agent_age_var = tk.StringVar(value="3")
        ttk.Spinbox(btn_frame, from_=0, to=365, width=4, textvariable=self.agent_age_var).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        ttk.Label(btn_frame, text="keep newer than (days):").pack(side=tk.RIGHT)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(
            fill=tk.X, pady=(10, 0)
        )

        # Bindings
        self.tree.bind("<ButtonRelease-1>", self.on_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        style = ttk.Style()
        style.configure("Treeview", rowheight=25)

    # ── Data Loading ──────────────────────────────────────────────

    def load_all(self):
        self.tree.delete(*self.tree.get_children())
        self.groups_data = {}
        self.session_lookup = {}
        self._cli_session_ids = set()

        claude_sessions, claude_size = self._load_claude_sessions()
        desktop_sessions, desktop_size = self._load_desktop_sessions()
        codex_sessions, codex_size = self._load_codex_sessions()

        if self._sort_column:
            self._apply_sort()

        total_sessions = claude_sessions + desktop_sessions + codex_sessions
        total_size = claude_size + desktop_size + codex_size
        group_count = len(self.groups_data)

        self.stats_label.config(
            text=f"{group_count} groups | {total_sessions} sessions | {self.format_size(total_size)} total"
        )
        self.status_var.set(f"Loaded {total_sessions} sessions from {group_count} groups")

    def _load_claude_sessions(self):
        if not self.claude_projects_path.exists():
            return 0, 0

        total_sessions = 0
        total_size = 0

        for project_folder in sorted(self.claude_projects_path.iterdir()):
            if not project_folder.is_dir():
                continue

            sessions_index_path = project_folder / "sessions-index.json"
            index_data = {}
            indexed_sessions = {}

            if sessions_index_path.exists():
                try:
                    with open(sessions_index_path, "r", encoding="utf-8") as f:
                        index_data = json.load(f)
                    for entry in index_data.get("entries", []):
                        indexed_sessions[entry.get("sessionId")] = entry
                except (json.JSONDecodeError, IOError):
                    pass

            original_path = index_data.get("originalPath", self._decode_folder_name(project_folder.name))
            jsonl_files = list(project_folder.rglob("*.jsonl"))

            if not jsonl_files:
                continue

            group_iid = self._make_iid("grp", f"claude:{project_folder.name}")
            display = f"[Claude] {self._shorten_path(original_path)} ({len(jsonl_files)})"

            self.groups_data[group_iid] = {
                "source": "claude",
                "project_path": project_folder,
                "index_path": sessions_index_path,
            }

            group_node = self.tree.insert("", tk.END, iid=group_iid, text=display, open=False, tags=("group",))

            for jsonl_path in sorted(jsonl_files, key=lambda p: p.stat().st_mtime, reverse=True):
                session_filename = jsonl_path.stem
                self._cli_session_ids.add(session_filename)
                rel_path = jsonl_path.relative_to(project_folder)
                session_key = str(rel_path).replace("\\", "/").replace(".jsonl", "")
                safe_id = session_key.replace("/", "_").replace("\\", "_")

                if session_filename in indexed_sessions:
                    entry = indexed_sessions[session_filename]
                    summary = entry.get("summary", "No summary")[:80]
                    msg_count = entry.get("messageCount", 0)
                    created = self._format_date(entry.get("created", ""))
                    modified = self._format_date(entry.get("modified", ""))
                    branch = entry.get("gitBranch", "")
                else:
                    si = self._parse_claude_session(jsonl_path)
                    summary = si.get("summary", "")[:80]
                    msg_count = si.get("message_count", 0)
                    created = self._format_date(si.get("created", ""))
                    modified = self._format_date(si.get("modified", ""))
                    branch = si.get("branch", "")

                size = self._get_file_size(jsonl_path)
                total_size += jsonl_path.stat().st_size if jsonl_path.exists() else 0

                display_name = session_filename[:12] + "..."
                if "agent-" in session_filename:
                    display_name = f"[agent] {session_filename[6:18]}..."

                session_iid = self._make_iid("sess", f"claude:{project_folder.name}:{safe_id}")

                self.session_lookup[session_iid] = {
                    "source": "claude",
                    "jsonl_path": jsonl_path,
                    "project_folder_name": project_folder.name,
                    "safe_id": safe_id,
                    "session_filename": session_filename,
                    "group_iid": group_iid,
                }

                self.tree.insert(
                    group_node, tk.END, iid=session_iid, text=display_name,
                    values=(summary, msg_count, created, modified, branch, "", size),
                    tags=("session",),
                )
                total_sessions += 1

        return total_sessions, total_size

    def _load_desktop_sessions(self):
        """Load sessions from the Claude Code desktop app storage."""
        if not self.desktop_sessions_path.exists():
            return 0, 0

        total_sessions = 0
        total_size = 0

        # Collect all session JSON files across account/profile folders
        session_files = list(self.desktop_sessions_path.rglob("*.json"))
        if not session_files:
            return 0, 0

        # Group by CWD (project directory)
        grouped = {}
        for json_path in session_files:
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            # Skip non-session files the app keeps here (e.g. scheduled-tasks.json)
            if not isinstance(data, dict) or not any(
                k in data for k in ("sessionId", "cliSessionId", "cwd", "originCwd")
            ):
                continue

            # Skip sessions that already have CLI .jsonl files
            cli_id = data.get("cliSessionId", "")
            if cli_id and cli_id in self._cli_session_ids:
                continue

            cwd = data.get("cwd", data.get("originCwd", ""))
            group_key = cwd or "Unknown"

            if group_key not in grouped:
                grouped[group_key] = []
            grouped[group_key].append({"path": json_path, "data": data})

        # Create tree nodes for each project group
        for cwd, entries in sorted(grouped.items()):
            entries.sort(key=lambda e: e["data"].get("lastActivityAt", 0), reverse=True)

            group_iid = self._make_iid("grp", f"desktop:{cwd}")
            display = f"[Desktop] {self._shorten_path(cwd)} ({len(entries)})"

            self.groups_data[group_iid] = {
                "source": "desktop",
                "cwd": cwd,
            }

            group_node = self.tree.insert(
                "", tk.END, iid=group_iid, text=display, open=False, tags=("group",)
            )

            for entry in entries:
                json_path = entry["path"]
                data = entry["data"]

                title = data.get("title", "")
                session_id = data.get("sessionId", json_path.stem)
                model = data.get("model", "")
                created = self._format_epoch_ms(data.get("createdAt"))
                modified = self._format_epoch_ms(data.get("lastActivityAt"))

                file_size = json_path.stat().st_size
                total_size += file_size
                size = self.format_size(file_size)

                display_name = session_id
                if len(display_name) > 20:
                    display_name = display_name[:17] + "..."

                session_iid = self._make_iid("sess", f"desktop:{json_path}")

                self.session_lookup[session_iid] = {
                    "source": "desktop",
                    "json_path": json_path,
                    "group_iid": group_iid,
                }

                self.tree.insert(
                    group_node, tk.END, iid=session_iid, text=display_name,
                    values=(title, "", created, modified, model, self._shorten_path(cwd), size),
                    tags=("session",),
                )
                total_sessions += 1

        return total_sessions, total_size

    def _load_codex_sessions(self):
        total_sessions = 0
        total_size = 0

        all_entries = []
        if self.codex_sessions_path.exists():
            all_entries.extend(self._collect_codex_entries(self.codex_sessions_path, "active", "sessions"))
        if self.codex_archived_path.exists():
            all_entries.extend(self._collect_codex_entries(self.codex_archived_path, "archived", "archived_sessions"))

        if not all_entries:
            return 0, 0

        grouped = {}
        for entry in all_entries:
            gk = entry["group_key"]
            if gk not in grouped:
                grouped[gk] = {
                    "group_label": entry["group_label"],
                    "storage": entry["storage"],
                    "root_path": entry["root_path"],
                    "entries": [],
                }
            grouped[gk]["entries"].append(entry)

        sorted_groups = sorted(
            grouped.items(),
            key=lambda item: (0 if item[1]["storage"] == "active" else 1, item[1]["group_label"]),
        )

        for group_key, group_info in sorted_groups:
            entries = sorted(group_info["entries"], key=lambda e: e["path"].stat().st_mtime, reverse=True)
            group_iid = self._make_iid("grp", f"codex:{group_key}")

            self.groups_data[group_iid] = {
                "source": "codex",
                "root_path": group_info["root_path"],
            }

            display = f"[Codex] {group_info['group_label']} ({len(entries)})"
            group_node = self.tree.insert("", tk.END, iid=group_iid, text=display, open=False, tags=("group",))

            for entry in entries:
                jsonl_path = entry["path"]
                session_rel = entry["session_rel"]
                si = self._parse_codex_session(jsonl_path)

                size = self._get_file_size(jsonl_path)
                total_size += jsonl_path.stat().st_size if jsonl_path.exists() else 0

                session_iid = self._make_iid("sess", f"codex:{jsonl_path}")
                display_name = Path(session_rel).stem
                if len(display_name) > 56:
                    display_name = display_name[:53] + "..."

                self.session_lookup[session_iid] = {
                    "source": "codex",
                    "jsonl_path": jsonl_path,
                    "root_path": entry["root_path"],
                    "group_iid": group_iid,
                }

                self.tree.insert(
                    group_node, tk.END, iid=session_iid, text=display_name,
                    values=(
                        si.get("summary", ""),
                        si.get("message_count", 0),
                        self._format_date(si.get("created", "")),
                        self._format_date(si.get("modified", "")),
                        "",
                        self._shorten_path(si.get("cwd", "")),
                        size,
                    ),
                    tags=("session",),
                )
                total_sessions += 1

        return total_sessions, total_size

    def _collect_codex_entries(self, root_path, storage, label_prefix):
        entries = []
        for jsonl_path in root_path.rglob("*.jsonl"):
            rel_path = jsonl_path.relative_to(root_path)
            parent_rel = str(rel_path.parent).replace("\\", "/")
            if parent_rel == ".":
                parent_rel = ""
            group_key = f"{storage}:{parent_rel}"
            group_label = label_prefix if not parent_rel else f"{label_prefix}/{parent_rel}"
            entries.append({
                "group_key": group_key,
                "group_label": group_label,
                "storage": storage,
                "root_path": root_path,
                "session_rel": str(rel_path).replace("\\", "/"),
                "path": jsonl_path,
            })
        return entries

    # ── Claude Session Parsing ────────────────────────────────────

    def _parse_claude_session(self, jsonl_path):
        info = {"summary": "", "message_count": 0, "created": "", "modified": "", "branch": ""}
        try:
            first_user_message = None
            timestamps = []
            message_count = 0
            branch = ""

            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if entry.get("type") == "user":
                        message_count += 1
                        if first_user_message is None:
                            msg = entry.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                first_user_message = content
                            elif isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        first_user_message = item.get("text", "")
                                        break
                                    elif isinstance(item, str):
                                        first_user_message = item
                                        break

                    if not branch and entry.get("gitBranch"):
                        branch = entry.get("gitBranch")

                    if entry.get("timestamp"):
                        timestamps.append(entry.get("timestamp"))

            if first_user_message:
                info["summary"] = first_user_message.split("\n")[0][:100]
            info["message_count"] = message_count
            info["branch"] = branch

            if timestamps:
                timestamps.sort()
                info["created"] = timestamps[0]
                info["modified"] = timestamps[-1]

        except Exception as e:
            print(f"Error parsing {jsonl_path}: {e}")

        return info

    def _decode_folder_name(self, folder_name):
        path = folder_name
        if path.startswith("C--") or path.startswith("c--"):
            path = "C:\\" + path[3:]
        path = path.replace("-", "\\")
        return path

    # ── Codex Session Parsing ─────────────────────────────────────

    def _parse_codex_session(self, jsonl_path):
        info = {"summary": "", "message_count": 0, "created": "", "modified": "", "cwd": ""}
        first_prompt = None
        last_prompt = None
        timestamps = []

        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = entry.get("timestamp")
                    if ts:
                        timestamps.append(ts)

                    entry_type = entry.get("type")

                    if entry_type == "session_meta":
                        payload = entry.get("payload") or {}
                        created = payload.get("timestamp", "")
                        cwd = payload.get("cwd", "")
                        if created and not info["created"]:
                            info["created"] = created
                        if cwd and not info["cwd"]:
                            info["cwd"] = cwd

                    if entry_type == "response_item":
                        payload = entry.get("payload") or {}
                        if payload.get("role") == "user":
                            raw = self._extract_codex_text(payload.get("content"))
                            normalized = self._normalize_codex_text(raw)
                            if normalized:
                                info["message_count"] += 1
                                if first_prompt is None:
                                    first_prompt = normalized
                                last_prompt = normalized

            if first_prompt:
                info["summary"] = first_prompt[:120]
            elif last_prompt:
                info["summary"] = last_prompt[:120]
            else:
                info["summary"] = jsonl_path.stem

            if timestamps:
                timestamps.sort()
                if not info["created"]:
                    info["created"] = timestamps[0]
                info["modified"] = timestamps[-1]

        except Exception as e:
            print(f"Error parsing {jsonl_path}: {e}")
            info["summary"] = jsonl_path.stem

        return info

    def _extract_codex_text(self, content):
        if isinstance(content, str):
            return content
        extracted = []
        items = content if isinstance(content, list) else [content]
        for item in items:
            if isinstance(item, str):
                extracted.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in ("input_text", "text"):
                text = item.get("text", "")
                if isinstance(text, str) and text:
                    extracted.append(text)
            elif item_type in ("input_image", "image", "image_url"):
                extracted.append("[image input]")
            elif isinstance(item.get("text"), str):
                extracted.append(item.get("text"))
        return "\n".join(part for part in extracted if part).strip()

    def _normalize_codex_text(self, text):
        if not text or not text.strip():
            return ""
        text = text.strip()
        lower = text.lower()
        if lower.startswith("# agents.md instructions"):
            return ""
        if lower.startswith("<environment_context>"):
            return ""
        if lower.startswith("<skill>"):
            return ""
        if lower.startswith("<instructions>"):
            return ""
        if lower.startswith('{"type":"input_image"'):
            return "[image input]"
        if "data:image/" in lower:
            return "[image input]"
        for marker in ("## my request for codex:", "## my request for claude:"):
            idx = lower.find(marker)
            if idx != -1:
                extracted = self._first_meaningful_line(text[idx + len(marker):])
                if extracted:
                    return extracted
        return self._first_meaningful_line(text)

    def _first_meaningful_line(self, text):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        skip_prefixes = (
            "# agents.md instructions", "# context from my ide setup",
            "## active file:", "## open tabs:", "## my request for codex:",
            "## my request for claude:", "<environment_context", "</environment_context",
            "<skill", "</skill", "<instructions", "</instructions",
            "---", "## skills", "- skill-",
        )
        for line in lines:
            ll = line.lower()
            if any(ll.startswith(p) for p in skip_prefixes):
                continue
            if line.startswith("<") and line.endswith(">"):
                continue
            return line[:200]
        return lines[0][:200]

    # ── Tree Interaction ──────────────────────────────────────────

    def on_click(self, event):
        item = self.tree.identify_row(event.y)
        if item and item.startswith("grp_"):
            self.tree.after(10, self._select_group_children, item)

    def sort_by(self, column):
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._apply_sort()

    def _apply_sort(self):
        column = self._sort_column
        reverse = self._sort_reverse
        for col, label in self._heading_labels.items():
            suffix = (" ▼" if reverse else " ▲") if col == column else ""
            self.tree.heading(col, text=label + suffix)

        groups = list(self.tree.get_children())
        if column == "#0":
            groups.sort(key=lambda g: self.tree.item(g, "text").lower(), reverse=reverse)
            for index, group_id in enumerate(groups):
                self.tree.move(group_id, "", index)

        for group_id in groups:
            children = list(self.tree.get_children(group_id))
            children.sort(key=lambda c: self._sort_key(c, column), reverse=reverse)
            for index, child in enumerate(children):
                self.tree.move(child, group_id, index)

    def _sort_key(self, item_id, column):
        if column == "#0":
            return self.tree.item(item_id, "text").lower()
        value = self.tree.set(item_id, column)
        if column == "msgs":
            try:
                return int(value)
            except ValueError:
                return -1
        if column == "size":
            return self._parse_size(value)
        return str(value).lower()

    def _parse_size(self, text):
        # Inverse of format_size, so displayed "1.2 MB" sorts by bytes rather than by string.
        units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        try:
            number, unit = text.split()
            return float(number) * units[unit]
        except (ValueError, KeyError):
            return -1

    def _select_group_children(self, group_id):
        children = list(self.tree.get_children(group_id))
        if children:
            self.tree.selection_set(children + [group_id])

    def on_select(self, event):
        selected = self.tree.selection()
        session_count = sum(1 for item in selected if item.startswith("sess_"))
        if session_count > 0:
            self.status_var.set(f"{session_count} session(s) selected")
        else:
            self.status_var.set("Ready")

    def select_all_sessions(self):
        selected = self.tree.selection()
        selected_groups = [item for item in selected if item.startswith("grp_")]

        session_ids = []
        if selected_groups:
            for group_id in selected_groups:
                for session_id in self.tree.get_children(group_id):
                    session_ids.append(session_id)
        else:
            for group_id in self.tree.get_children():
                for session_id in self.tree.get_children(group_id):
                    session_ids.append(session_id)
        self.tree.selection_set(session_ids)

    def select_agent_sessions(self):
        """Select every [agent] subagent transcript older than the age guard.

        These are side-channel records: the parent session stores each agent's returned
        result inline and never references these files by path, so removing them does not
        break resuming the parent. What is lost is the agent's internal working
        transcript, which only matters if you want to audit how it reached a conclusion.

        The guard exists because a session you are still working in may have spawned
        agents you are not finished with. Anything modified inside the window is left
        alone, and the count is reported so a skip is never silent.
        """
        try:
            days = int(self.agent_age_var.get())
            if days < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid value", "Keep-newer-than must be a whole number of days.")
            return

        cutoff = time.time() - days * 86400
        matched, skipped = [], 0

        for group_id in self.tree.get_children():
            for session_id in self.tree.get_children(group_id):
                info = self.session_lookup.get(session_id) or {}
                # Only Claude CLI sessions have subagent transcripts.
                if info.get("source") != "claude":
                    continue
                if "agent-" not in info.get("session_filename", ""):
                    continue
                path = info.get("jsonl_path")
                try:
                    if days and path and path.stat().st_mtime > cutoff:
                        skipped += 1
                        continue
                except OSError:
                    pass  # unreadable mtime: fall through and let it be selectable
                matched.append(session_id)

        self.tree.selection_set(matched)

        if matched:
            # Open the groups holding matches so the selection is visible before deleting.
            for gid in {self.session_lookup[m]["group_iid"] for m in matched}:
                self.tree.item(gid, open=True)
            self.tree.see(matched[0])
            note = f", {skipped} kept (newer than {days}d)" if skipped else ""
            self.status_var.set(f"{len(matched)} [agent] session(s) selected{note}")
        else:
            self.status_var.set(
                f"No [agent] sessions older than {days} day(s)"
                + (f" ({skipped} kept as too recent)" if skipped else "")
            )

    def expand_all(self):
        for item in self.tree.get_children():
            self.tree.item(item, open=True)

    def collapse_all(self):
        for item in self.tree.get_children():
            self.tree.item(item, open=False)

    def filter_tree(self):
        search_text = self.search_var.get().strip().lower()
        for group_id in self.tree.get_children():
            group_has_match = False
            for session_id in self.tree.get_children(group_id):
                values = self.tree.item(session_id, "values")
                text = self.tree.item(session_id, "text")
                haystack = " ".join([str(text)] + [str(v) for v in values]).lower()
                if not search_text or search_text in haystack:
                    group_has_match = True
            self.tree.item(group_id, open=bool(search_text and group_has_match))

    # ── Deletion ──────────────────────────────────────────────────

    def delete_selected(self):
        selected = self.tree.selection()
        sessions_to_delete = [item for item in selected if item.startswith("sess_")]

        if not sessions_to_delete:
            messagebox.showinfo("Info", "Please select sessions to delete (not group folders)")
            return

        confirm = messagebox.askyesno(
            "Confirm Deletion",
            f"Are you sure you want to delete {len(sessions_to_delete)} session(s)?\n\n"
            "This will delete selected conversation files.\n"
            "This action cannot be undone!",
        )
        if not confirm:
            return

        deleted_count = 0
        scratchpads_removed = 0
        errors = []
        removed_dirs = set()

        # Track Claude deletions by project for index updates
        claude_by_project = {}

        for item_id in sessions_to_delete:
            info = self.session_lookup.get(item_id)
            if not info:
                errors.append(f"Session data not found: {item_id}")
                continue

            source = info["source"]

            if source == "desktop":
                json_path = info["json_path"]
                try:
                    if json_path.exists():
                        json_path.unlink()
                    deleted_count += 1
                except Exception as e:
                    errors.append(f"Failed to delete {json_path.name}: {e}")
                continue

            jsonl_path = info["jsonl_path"]

            try:
                if not jsonl_path.exists():
                    if any(jsonl_path.is_relative_to(d) for d in removed_dirs):
                        deleted_count += 1
                    else:
                        errors.append(f"File not found: {jsonl_path.name}")
                    continue

                if source == "claude":
                    jsonl_path.unlink()
                    safe_id = info["safe_id"]
                    if "/" not in safe_id and "_subagents_" not in safe_id:
                        folder = jsonl_path.parent / jsonl_path.stem
                        if folder.exists() and folder.is_dir():
                            removed_dirs.add(folder)
                            shutil.rmtree(folder)
                        # Subagent transcripts share the parent session's scratchpad, so only
                        # a top-level session deletion may remove it.
                        if self._remove_scratchpad(
                            info["project_folder_name"], info["session_filename"], errors
                        ):
                            scratchpads_removed += 1
                    proj_name = info["project_folder_name"]
                    if proj_name not in claude_by_project:
                        claude_by_project[proj_name] = []
                    claude_by_project[proj_name].append(info["session_filename"])

                elif source == "codex":
                    jsonl_path.unlink()
                    root_path = info.get("root_path")
                    if root_path and root_path.exists():
                        self._cleanup_empty_dirs(jsonl_path.parent, root_path)

                deleted_count += 1

            except Exception as e:
                errors.append(f"Failed to delete {jsonl_path.name}: {e}")

        # Update Claude session indexes
        for proj_name, deleted_filenames in claude_by_project.items():
            for gdata in self.groups_data.values():
                if (
                    gdata.get("source") == "claude"
                    and gdata.get("project_path")
                    and gdata["project_path"].name == proj_name
                ):
                    index_path = gdata.get("index_path")
                    if index_path and index_path.exists():
                        try:
                            with open(index_path, "r", encoding="utf-8") as f:
                                idx = json.load(f)
                            if idx.get("entries"):
                                idx["entries"] = [
                                    e for e in idx["entries"] if e.get("sessionId") not in deleted_filenames
                                ]
                                with open(index_path, "w", encoding="utf-8") as f:
                                    json.dump(idx, f, indent=2)
                        except Exception as e:
                            errors.append(f"Failed to update index for {proj_name}: {e}")
                    break

        scratch_note = (
            f"\nAlso removed {scratchpads_removed} temp scratchpad folder(s)."
            if scratchpads_removed
            else ""
        )

        if errors:
            messagebox.showwarning(
                "Partial Success",
                f"Deleted {deleted_count} session(s){scratch_note}\n\nErrors:\n" + "\n".join(errors[:5]),
            )
        else:
            messagebox.showinfo("Success", f"Successfully deleted {deleted_count} session(s){scratch_note}")

        self.load_all()

    def _remove_scratchpad(self, project_folder_name, session_filename, errors):
        """Delete the temp scratchpad (screenshots, scratch scripts) a session left behind.

        Claude Code writes these to Temp\\claude\\<project>\\<session-id>\\ using the same
        project-folder encoding as ~/.claude/projects, so the names map across directly.
        """
        scratch_dir = self.scratchpad_path / project_folder_name / session_filename
        if not scratch_dir.is_dir():
            return False
        try:
            shutil.rmtree(scratch_dir)
        except OSError as e:
            errors.append(f"Failed to delete scratchpad {session_filename[:12]}: {e}")
            return False
        self._cleanup_empty_dirs(scratch_dir.parent, self.scratchpad_path)
        return True

    def _known_session_ids(self):
        """Every session ID that names a transcript file on disk."""
        return {p.stem for p in self.claude_projects_path.rglob("*.jsonl")}

    def _referenced_session_ids(self, candidates):
        """Of `candidates`, the session IDs still mentioned inside some surviving transcript.

        Resuming a conversation rewrites it under a new filename while the replayed messages
        keep their original session_id, and a transcript can also cite an older session's
        scratchpad path directly. Both cases mean the scratchpad still belongs to a
        conversation you have, so matching on filenames alone would wrongly call it orphaned.
        """
        if not candidates:
            return set()
        # Match the JSON field, not a bare UUID: transcripts quote session IDs in ordinary
        # text (command output, pasted paths) and those mentions must not protect a folder.
        alternation = b"|".join(re.escape(cid.encode("utf-8")) for cid in candidates)
        pattern = re.compile(rb'"session_?id"\s*:\s*"(' + alternation + rb')"', re.I)

        found = set()
        for path in self.claude_projects_path.rglob("*.jsonl"):
            if len(found) == len(candidates):
                break
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            found.update(match.decode("utf-8") for match in pattern.findall(blob))
        return found

    def sweep_orphan_scratchpads(self):
        """Delete temp scratchpads whose conversation no longer exists."""
        if not self.scratchpad_path.exists():
            messagebox.showinfo("Sweep Scratchpads", f"No scratchpad folder at:\n{self.scratchpad_path}")
            return

        # Guard: with no transcripts to compare against, every scratchpad looks orphaned
        # and the sweep would wipe the lot, including sessions still in use.
        if not self.claude_projects_path.exists():
            messagebox.showerror(
                "Sweep Scratchpads",
                f"Claude projects folder not found:\n{self.claude_projects_path}\n\nSweep aborted.",
            )
            return

        known = self._known_session_ids()
        if not known:
            messagebox.showerror(
                "Sweep Scratchpads",
                "No session transcripts found. Refusing to sweep in case the folder is unreadable.",
            )
            return

        # A scratchpad is created before its transcript is flushed, so treat anything touched
        # in the last hour as live no matter what the transcript folder says.
        cutoff = time.time() - 3600
        candidates = []
        for project_dir in sorted(self.scratchpad_path.iterdir()):
            if not project_dir.is_dir():
                continue
            for session_dir in sorted(project_dir.iterdir()):
                if not session_dir.is_dir() or not SESSION_ID_RE.match(session_dir.name):
                    continue
                if session_dir.name in known or session_dir.stat().st_mtime > cutoff:
                    continue
                candidates.append(session_dir)

        orphans = []
        if candidates:
            self.status_var.set(f"Checking {len(candidates)} scratchpad(s) against transcript history...")
            self.root.update_idletasks()
            referenced = self._referenced_session_ids({d.name for d in candidates})
            orphans = [d for d in candidates if d.name not in referenced]

        kept = len(candidates) - len(orphans)
        kept_note = f"\n{kept} kept: still referenced by a surviving conversation.\n" if kept else ""

        if not orphans:
            messagebox.showinfo("Sweep Scratchpads", f"No orphaned scratchpads found.\n{kept_note}")
            self.status_var.set(f"No orphaned scratchpads found ({kept} kept as referenced)")
            return

        total = sum(self._dir_size(d) for d in orphans)
        preview = "\n".join(f"   {d.parent.name} / {d.name[:8]}..." for d in orphans[:10])
        if len(orphans) > 10:
            preview += f"\n   ...and {len(orphans) - 10} more"

        confirm = messagebox.askyesno(
            "Sweep Orphan Scratchpads",
            f"Found {len(orphans)} orphaned scratchpad folder(s) using {self.format_size(total)}:\n\n"
            f"{preview}\n{kept_note}\n"
            "No surviving conversation continues these sessions. Delete these temp files?\n"
            "This action cannot be undone!",
        )
        if not confirm:
            self.status_var.set(f"Sweep cancelled ({len(orphans)} orphaned scratchpad(s) found)")
            return

        removed = 0
        errors = []
        for session_dir in orphans:
            try:
                shutil.rmtree(session_dir)
                self._cleanup_empty_dirs(session_dir.parent, self.scratchpad_path)
                removed += 1
            except OSError as e:
                errors.append(f"{session_dir.name[:12]}: {e}")

        if errors:
            messagebox.showwarning(
                "Partial Success",
                f"Removed {removed} of {len(orphans)} scratchpad(s)\n\nErrors:\n" + "\n".join(errors[:5]),
            )
        else:
            messagebox.showinfo(
                "Success", f"Removed {removed} orphaned scratchpad(s), freeing {self.format_size(total)}"
            )

        self.status_var.set(f"Swept {removed} orphaned scratchpad(s)")

    def _cleanup_empty_dirs(self, start_dir, stop_dir):
        current = start_dir
        while current != stop_dir:
            try:
                current.relative_to(stop_dir)
            except ValueError:
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    # ── Utilities ─────────────────────────────────────────────────

    def _make_iid(self, prefix, key):
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}_{digest}"

    def _shorten_path(self, path):
        if not path:
            return ""
        try:
            parts = Path(path).parts
        except Exception:
            return str(path)
        if len(parts) > 4:
            return f".../{'/'.join(parts[-3:])}"
        if len(parts) > 3:
            return f".../{'/'.join(parts[-2:])}"
        return str(path)

    def _format_date(self, date_str):
        if not date_str:
            return ""
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            return date_str[:10] if len(date_str) >= 10 else date_str

    def _format_epoch_ms(self, epoch_ms):
        if not epoch_ms:
            return ""
        try:
            dt = datetime.fromtimestamp(epoch_ms / 1000)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return ""

    def _get_file_size(self, path):
        if not path.exists():
            return "N/A"
        return self.format_size(path.stat().st_size)

    def _dir_size(self, path):
        try:
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except OSError:
            return 0

    def format_size(self, size):
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


def main():
    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except Exception:
        pass
    app = ConvoCleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
