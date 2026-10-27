import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from convo_cleaner import CODEX_CLI_MISSING, ConvoCleanerApp

GONE = "11111111-1111-1111-1111-111111111111"
KEPT = "22222222-2222-2222-2222-222222222222"


def history_line(session_id, paste_hash=None):
    pastes = {"1": {"id": 1, "type": "text", "contentHash": paste_hash}} if paste_hash else {}
    entry = {"display": "prompt", "pastedContents": pastes, "timestamp": 1, "sessionId": session_id}
    return json.dumps(entry).encode("utf-8") + b"\r\n"


class SessionTraceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        # Skip __init__ so no Tk window opens; only the paths these methods read are set.
        self.app = ConvoCleanerApp.__new__(ConvoCleanerApp)
        self.app.session_trace_paths = [self.home / n for n in ("file-history", "session-env", "uploads")]
        self.app.prompt_history_path = self.home / "history.jsonl"
        self.app.paste_cache_path = self.home / "paste-cache"
        self.app.paste_cache_path.mkdir()

    def test_removes_only_the_deleted_sessions_trace_folders(self):
        for parent in self.app.session_trace_paths:
            (parent / GONE).mkdir(parents=True)
            (parent / GONE / "data").write_text("x")
            (parent / KEPT).mkdir()
        errors = []

        removed = self.app._remove_session_traces(GONE, errors)

        self.assertEqual((removed, errors), (3, []))
        for parent in self.app.session_trace_paths:
            self.assertFalse((parent / GONE).exists())
            self.assertTrue((parent / KEPT).is_dir())

    def test_non_uuid_name_never_removes_the_trace_folder_itself(self):
        for parent in self.app.session_trace_paths:
            parent.mkdir()

        self.assertEqual(self.app._remove_session_traces(".", []), 0)
        for parent in self.app.session_trace_paths:
            self.assertTrue(parent.is_dir())

    def test_purge_drops_session_prompts_and_unshared_pastes(self):
        kept_lines = [history_line(KEPT, "bbbb"), b"not json\r\n", history_line(KEPT)]
        self.app.prompt_history_path.write_bytes(
            history_line(GONE, "aaaa") + kept_lines[0] + history_line(GONE, "bbbb") + b"".join(kept_lines[1:])
        )
        (self.app.paste_cache_path / "aaaa.txt").write_text("only the deleted session pasted this")
        (self.app.paste_cache_path / "bbbb.txt").write_text("both sessions pasted this")
        errors = []

        removed = self.app._purge_prompt_history({GONE}, errors)

        self.assertEqual((removed, errors), (2, []))
        self.assertEqual(self.app.prompt_history_path.read_bytes(), b"".join(kept_lines))
        self.assertFalse((self.app.paste_cache_path / "aaaa.txt").exists())
        self.assertTrue((self.app.paste_cache_path / "bbbb.txt").exists())
        self.assertFalse((self.home / "history.jsonl.tmp").exists())


class OpenSessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = ConvoCleanerApp.__new__(ConvoCleanerApp)
        self.app.open_sessions_path = Path(tmp.name)

    def write_window(self, pid, session_id):
        entry = {"pid": pid, "sessionId": session_id}
        (self.app.open_sessions_path / f"{pid}.json").write_text(json.dumps(entry), encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "reads Windows process state")
    def test_lists_only_sessions_whose_window_is_still_running(self):
        # Popen keeps the exited process's handle open, so its PID cannot be reused mid-test.
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        self.write_window(os.getpid(), KEPT)
        self.write_window(exited.pid, GONE)

        # Probing this test's own PID also proves the check does not terminate what it probes.
        self.assertEqual(self.app._open_session_ids(), {KEPT})

    def test_unreadable_window_file_means_unknown(self):
        self.write_window(os.getpid(), KEPT)
        (self.app.open_sessions_path / "1.json").write_text("{half written", encoding="utf-8")

        self.assertIsNone(self.app._open_session_ids())

    def test_missing_folder_means_unknown(self):
        self.app.open_sessions_path = self.app.open_sessions_path / "absent"

        self.assertIsNone(self.app._open_session_ids())


class CodexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = ConvoCleanerApp.__new__(ConvoCleanerApp)
        self.app.codex_home = Path(tmp.name)
        self.app.codex_sessions_path = self.app.codex_home / "sessions"
        self.app.codex_images_path = self.app.codex_home / "generated_images"
        self.day = self.app.codex_sessions_path / "2026" / "10" / "14"
        self.day.mkdir(parents=True)
        self.cli_calls = []

    def rollout(self, session_id, compressed=False):
        path = self.day / f"rollout-2026-10-14T10-00-00-{session_id}.jsonl{'.zst' if compressed else ''}"
        path.write_text("{}")
        return path

    def pictures(self, session_id):
        folder = self.app.codex_images_path / session_id
        folder.mkdir(parents=True)
        (folder / "exec-1.png").write_bytes(b"png")
        return folder

    def fake_cli(self, result):
        # Stands in for `codex delete`, which removes the log itself when it succeeds.
        def run(session_id):
            self.cli_calls.append(session_id)
            if result:
                for path in self.day.glob(f"*{session_id}*"):
                    path.unlink()
            return result
        self.app._codex_cli_delete = run

    def info(self, path, session_id):
        return {"jsonl_path": path, "session_id": session_id, "root_path": self.app.codex_sessions_path}

    def test_lists_compressed_and_plain_sessions_with_their_ids(self):
        self.rollout(GONE, compressed=True)
        self.rollout(KEPT)
        (self.day / "notes.txt").write_text("not a session")

        entries = self.app._collect_codex_entries(self.app.codex_sessions_path, "active", "sessions")

        self.assertEqual(sorted(e["session_id"] for e in entries), [GONE, KEPT])

    def test_delete_runs_codex_and_removes_only_that_sessions_pictures(self):
        path = self.rollout(GONE, compressed=True)
        self.pictures(GONE)
        kept_pictures = self.pictures(KEPT)
        self.fake_cli(True)
        errors = []

        removed = self.app._delete_codex_session(self.info(path, GONE), errors)

        self.assertEqual((removed, errors, self.cli_calls), (True, [], [GONE]))
        self.assertFalse(path.exists())
        self.assertFalse((self.app.codex_images_path / GONE).exists())
        self.assertTrue(kept_pictures.is_dir())

    def test_without_codex_cli_files_still_go_with_one_note(self):
        paths = [self.rollout(GONE), self.rollout(KEPT)]
        self.pictures(GONE)
        self.fake_cli(None)
        errors = []

        for path, session_id in zip(paths, (GONE, KEPT)):
            self.app._delete_codex_session(self.info(path, session_id), errors)

        self.assertEqual(errors, [CODEX_CLI_MISSING])
        self.assertFalse(any(p.exists() for p in paths))
        self.assertFalse((self.app.codex_images_path / GONE).exists())

    def test_failed_codex_delete_is_reported_and_the_file_still_goes(self):
        path = self.rollout(GONE)
        self.fake_cli(False)
        errors = []

        self.app._delete_codex_session(self.info(path, GONE), errors)

        self.assertFalse(path.exists())
        self.assertEqual(len(errors), 1)
        self.assertIn("codex delete failed", errors[0])

    def test_log_without_session_id_never_touches_codex_or_the_pictures_root(self):
        path = self.day / "odd.jsonl"
        path.write_text("{}")
        self.pictures(KEPT)
        self.fake_cli(True)

        removed = self.app._delete_codex_session(self.info(path, None), [])

        self.assertEqual((removed, self.cli_calls), (False, []))
        self.assertFalse(path.exists())
        self.assertTrue(self.app.codex_images_path.is_dir())

    def test_reads_titles_and_dates_from_the_newest_state_database(self):
        for version, title in ((4, "old database"), (5, "Make one image\nSave it as a.png")):
            con = sqlite3.connect(self.app.codex_home / f"state_{version}.sqlite")
            con.execute("CREATE TABLE threads (id TEXT, created_at INTEGER, updated_at INTEGER, source TEXT, cwd TEXT, title TEXT)")
            con.execute("INSERT INTO threads VALUES (?, 1791840544, 1791879487, 'exec', 'C:/Work', ?)", (GONE, title))
            con.commit()
            con.close()

        thread = self.app._codex_threads()[GONE]

        self.assertEqual(thread["title"], "Make one image\nSave it as a.png")
        self.assertEqual((thread["source"], thread["created_ms"]), ("exec", 1791840544000))

    def test_unreadable_state_database_means_no_titles(self):
        (self.app.codex_home / "state_5.sqlite").write_text("not a database")

        self.assertEqual(self.app._codex_threads(), {})


if __name__ == "__main__":
    unittest.main()
