import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from convo_cleaner import ConvoCleanerApp

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


if __name__ == "__main__":
    unittest.main()
