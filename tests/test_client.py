import git
import os
import pathlib
import sys
import tempfile
import toml

import unittest

from declib.artifacts import (
    FunctionHeader, StackVariable, Comment, Struct
)
from binsync.core.client import Client
from binsync.auto_recover import _is_first_sync


class TestClient(unittest.TestCase):
    FAKE_ADDR = 0x400080

    def test_repo_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Client("user0", tmpdir, "fake_hash", init_repo=True)
            assert os.path.isdir(os.path.join(tmpdir, ".git")) is True

    def test_dirty_master_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = Client("user0", tmpdir, "fake_hash", init_repo=True)
            state = client.master_state
            assert state.user == "user0"
            # after first creation, state is dirty
            assert state.dirty is True

            func_header = FunctionHeader("some_name", self.FAKE_ADDR)
            state.set_function_header(func_header)
            # it should be dirty still (more edits)
            assert state.dirty is True

            # commit changes so we clean it!
            client.master_state = state
            client.commit_master_state()
            state = client.master_state
            assert state.dirty is False

            # ignore cache and grab the master state from the git repo
            state = client.get_state(user="user0", fetch_cache=False)
            assert len(state.functions) == 1
            assert state.functions[self.FAKE_ADDR].header == func_header

            # git is still running at least on windows
            client.shutdown()

    def test_commit_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = Client("user0", tmpdir, "fake_hash", init_repo=True)
            state = client.master_state

            # create changes, and verify the state recorded a message
            fh_0 = FunctionHeader("user0_func", self.FAKE_ADDR)
            state.set_function_header(fh_0)
            assert state.last_commit_msg == f"Updated {fh_0}"
            client.master_state = state

            sv_0 = StackVariable(-0x10, "u0_var", "int", 4, self.FAKE_ADDR)
            state.set_stack_variable(sv_0)
            assert state.last_commit_msg == f"Updated {sv_0}"
            client.master_state = state

            # simulate a merge from another user
            fh_1: FunctionHeader = fh_0.copy()
            fh_1.name = "user1_func"
            # a merge is any setting to the state that does not update the 'last_change' parameter
            state.set_function_header(fh_1, from_user="user1", set_last_change=False)
            assert state.last_commit_msg == f"Merged in {fh_1} from user1"
            client.master_state = state

            # now check those changes really made it into the git repo
            client.commit_master_state()
            commits = list(client.repo.iter_commits())
            assert commits[0].message == f"Merged in {fh_1} from user1\n"
            assert commits[1].message == f"Updated {sv_0}\n"
            assert commits[2].message == f"Updated {fh_0}\n"

    def test_multi_user_branch_loading(self):
        with tempfile.TemporaryDirectory() as tmpdir:

            #
            # First User
            #

            client = Client("user0", tmpdir, "fake_hash", init_repo=True)
            state = client.master_state
            user0_func_header = FunctionHeader("user0_func", self.FAKE_ADDR)
            state.set_function_header(user0_func_header)
            client.master_state = state
            client.commit_master_state()
            client.shutdown()

            #
            # Second User
            #

            client = Client("user1", tmpdir, "fake_hash")
            state = client.master_state
            user1_func_header = FunctionHeader("user1_func", self.FAKE_ADDR)
            state.set_function_header(user1_func_header)
            client.master_state = state
            client.commit_master_state()

            assert client.master_user == "user1"
            user0_state = client.get_state(user="user0")
            assert user0_state.functions[self.FAKE_ADDR].header == user0_func_header
            assert client.master_state.functions[self.FAKE_ADDR].header == user1_func_header

    def test_corrupted_toml_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = Client("user0", tmpdir, "fake_hash", init_repo=True)
            state = client.master_state

            func_header = FunctionHeader("some_name", self.FAKE_ADDR)
            state.set_function_header(func_header)
            client.master_state = state
            client.commit_master_state()
            client.shutdown()

            # do some emulated file corruption making this TOML no longer valid
            with open(pathlib.Path(tmpdir) / "functions" / "00400080.toml", "r+", encoding="utf-8") as file:
                file.truncate(5)

            # force a real git commit for later loading in the client
            repo = git.Repo(tmpdir)
            repo.git.add(all=True)
            repo.index.commit("corrupt")
            
            # on the creation of the client, it will load the master_state, which will result in an
            # exception because the TOML fails to load
            self.assertRaises(toml.decoder.TomlDecodeError, lambda: Client("user0", tmpdir, "fake_hash"))


class TestAutoRecoverFirstSync(unittest.TestCase):
    """
    Regression tests for _is_first_sync (binsync/auto_recover.py).

    The master user's branch is created from binsync/__root__, which already carries
    the "Root commit" that initialized the repo. _is_first_sync must count only the
    commits the user added on top of the root branch, otherwise the first-sync guard
    always returns False and auto_sync_all never fires.
    """

    def _make_client(self, tmpdir):
        return Client("user0", tmpdir, "fake_hash", init_repo=True)

    def test_first_sync_only_root_commit(self):
        """A brand-new user branch carries only the Root commit -> first sync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._make_client(tmpdir)
            try:
                self.assertTrue(_is_first_sync(client))
            finally:
                client.shutdown()

    def test_first_sync_after_user_created_commit(self):
        """After the updater's first 'User created' commit -> still first sync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._make_client(tmpdir)
            try:
                client.commit_and_update_states(commit_msg="User created")
                self.assertTrue(_is_first_sync(client))
            finally:
                client.shutdown()

    def test_not_first_sync_with_artifact_commit(self):
        """Once the user has real artifact commits -> not first sync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            client = self._make_client(tmpdir)
            try:
                state = client.master_state
                state.set_function_header(FunctionHeader("my_func", 0x400080))
                client.master_state = state
                client.commit_and_update_states(commit_msg="Updated my_func")
                self.assertFalse(_is_first_sync(client))
            finally:
                client.shutdown()


if __name__ == "__main__":
    unittest.main(argv=sys.argv)
