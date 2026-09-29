"""Publish through the real entrypoint to disposable local bare repositories.

Run with Python 3 on Linux with Bash, Git and rsync. Set PUBLISH_TEST_IMAGE
to run the same cases through the built Docker image (network disabled).
Only the fetch-failure case injects a failing Git command; all publication,
copying, commits and other failure cases use real Git and rsync.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ACTION_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-never-log"
PAT = "test-user:test-pat-never-log"


def outputs(path):
    """Read both single-line and multiline GitHub environment-file values."""
    values = {}
    lines = iter(path.read_text().splitlines())
    for line in lines:
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            value = []
            for part in lines:
                if part == delimiter:
                    break
                value.append(part)
            else:
                raise AssertionError("Unterminated output value")
            values[key] = "\n".join(value)
        else:
            key, value = line.split("=", 1)
            values[key] = value
    return values


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="publish-action-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.source = self.workspace / "source folder"
        self.source.mkdir(parents=True)
        (self.source / "artifact.txt").write_text("new artifact\n")
        self.home = self.root / "home"
        self.home.mkdir()
        self.remote = self.root / "destination.git"
        self.seed = self.root / "seed"
        self.env = os.environ.copy()
        # Ignore host credentials, repository overrides and Git tracing settings.
        for name in list(self.env):
            if name.startswith(("GIT_", "GITHUB_", "INPUT_")):
                self.env.pop(name)
        self.env.update(
            HOME=str(self.home),
            GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GITHUB_ACTOR="publisher",
            GITHUB_REPOSITORY="fixture/artifacts",
            GITHUB_WORKFLOW="Local publication test",
            GITHUB_REF="refs/heads/build",
        )
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "releases", str(self.seed))
        self.git("-C", str(self.seed), "config", "user.name", "Fixture")
        self.git("-C", str(self.seed), "config", "user.email", "fixture@example.test")
        self.invocations = 0

    def git(self, *args, env=None):
        return subprocess.run(
            ["git", *args], env=env or self.env, check=True,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()

    def seed_branch(self, files=None):
        for name, value in (files or {"artifact.txt": "old artifact\n", "stale.txt": "old\n"}).items():
            path = self.seed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        self.git("-C", str(self.seed), "add", ".")
        self.git("-C", str(self.seed), "commit", "-m", "Seed")
        self.git("-C", str(self.seed), "push", str(self.remote), "releases")
        return self.remote_hash()

    def remote_hash(self):
        return self.git("--git-dir", str(self.remote), "rev-parse", "refs/heads/releases")

    def remote_files(self):
        return set(self.git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "releases").splitlines())

    def remote_text(self, path):
        return self.git("--git-dir", str(self.remote), "show", f"releases:{path}")

    def run_action(self, *, expected=0, env_changes=None, **overrides):
        self.invocations += 1
        output = self.root / f"outputs-{self.invocations}"
        output.write_text("previous_output=preserved\n")
        work = self.root / f"checkout {self.invocations}"
        inputs = dict(
            repository="", branch="releases", host="", github_token=TOKEN,
            github_pat="", source_folder="source folder", target_folder="",
            commit_author="", commit_message="", dry_run="",
            working_directory=str(work), initial_source_folder="",
            initial_commit_message="Initial commit", no_delete="",
        )
        inputs.update(overrides)
        env = self.env | {"INPUT_REMOTE": str(self.remote), "GITHUB_OUTPUT": str(output)}
        env.update(env_changes or {})
        env = {key: value for key, value in env.items() if value is not None}
        image = os.environ.get("PUBLISH_TEST_IMAGE")
        if image:
            command = [
                "docker", "run", "--rm", "--network", "none",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--mount", f"type=bind,source={self.root},target={self.root}",
                "--workdir", str(self.workspace),
            ]
            for key in sorted(env):
                if key.startswith(("GIT_", "GITHUB_", "INPUT_", "PUBLISH_")) or key in ("HOME", "PATH"):
                    # Use the image's normal PATH unless a fault-injection shim is needed.
                    value = env[key]
                    if key == "PATH":
                        if not env_changes or "PATH" not in env_changes:
                            continue
                        value = f"{self.root}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                    command.extend(["--env", f"{key}={value}"])
            command.extend([image, *inputs.values()])
        else:
            command = ["bash", str(ACTION_ROOT / "entrypoint.sh"), *inputs.values()]
        result = subprocess.run(
            command, cwd=self.workspace, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
        )
        self.assertNotIn(TOKEN, result.stdout)
        self.assertNotIn(PAT, result.stdout)
        self.assertNotIn(PAT.split(":", 1)[1], result.stdout)
        self.assertNotIn("::set-output", result.stdout)
        if expected == 0:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        values = outputs(output)
        self.assertEqual(values.pop("previous_output"), "preserved")
        if expected == 0:
            self.assertEqual(set(values), {"commit_hash", "working_directory"})
            work = Path(values["working_directory"])
            self.assertTrue(work.is_absolute())
            self.assertEqual(values["commit_hash"], self.git("-C", str(work), "rev-parse", "HEAD"))
        else:
            self.assertEqual(values, {}, result.stdout)
        return result.stdout, values

    def test_existing_branch_updates_and_deletes(self):
        previous = self.seed_branch()
        _, result = self.run_action()
        self.assertNotEqual(self.remote_hash(), previous)
        self.assertEqual(result["commit_hash"], self.remote_hash())
        self.assertEqual(self.remote_files(), {"artifact.txt"})
        self.assertEqual(self.remote_text("artifact.txt"), "new artifact")

    def test_same_size_and_timestamp_changed_content_is_copied(self):
        self.seed_branch()
        hooks = self.root / "timestamp-fixture"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        # Real Git runs this hook after checkout, giving both files exactly the
        # same timestamp. Their sizes match but their contents differ.
        hook.write_text('#!/bin/sh\ntouch -r artifact.txt "$PUBLISH_TEST_SOURCE"\n')
        hook.chmod(0o755)
        _, result = self.run_action(env_changes={
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks),
            "PUBLISH_TEST_SOURCE": str(self.source / "artifact.txt"),
        })
        published = Path(result["working_directory"]) / "artifact.txt"
        self.assertEqual(published.stat().st_size, (self.source / "artifact.txt").stat().st_size)
        self.assertEqual(published.stat().st_mtime_ns, (self.source / "artifact.txt").stat().st_mtime_ns)
        self.assertEqual(self.remote_text("artifact.txt"), "new artifact")

    def test_no_delete_preserves_unrelated_maven_artifacts(self):
        self.seed_branch({"other/group/old.jar": "keep", "artifact.txt": "old"})
        self.run_action(no_delete="true")
        self.assertEqual(self.remote_files(), {"artifact.txt", "other/group/old.jar"})
        self.assertEqual(self.remote_text("other/group/old.jar"), "keep")
        self.assertEqual(self.remote_text("artifact.txt"), "new artifact")

    def test_nested_paths_spaces_relative_working_directory_and_no_delete(self):
        for no_delete in ("", "true"):
            with self.subTest(no_delete=no_delete):
                # Each subcase has a separate branch checkout; seed only once.
                if not no_delete:
                    self.seed_branch({"outside.txt": "keep", "packages with spaces/nested/stale.txt": "old"})
                else:
                    self.git("-C", str(self.seed), "push", "--force", str(self.remote), "releases")
                nested = self.source / "inner directory"
                nested.mkdir(exist_ok=True)
                (nested / "file with spaces.txt").write_text("nested data")
                relative = f"../relative checkout {self.invocations}"
                _, result = self.run_action(
                    target_folder="packages with spaces/nested", working_directory=relative, no_delete=no_delete,
                )
                self.assertEqual(self.remote_text("outside.txt"), "keep")
                self.assertEqual(self.remote_text("packages with spaces/nested/inner directory/file with spaces.txt"), "nested data")
                self.assertEqual("packages with spaces/nested/stale.txt" in self.remote_files(), bool(no_delete))
                self.assertEqual(Path(result["working_directory"]), (self.workspace / relative).resolve())

    def test_missing_branch_default_initialization_has_outputs_and_one_commit(self):
        _, result = self.run_action()
        self.assertEqual(result["commit_hash"], self.remote_hash())
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-list", "--count", "releases"), "1")
        self.assertEqual(self.git("--git-dir", str(self.remote), "log", "-1", "--format=%s", "releases"), "Initial commit")

    def test_missing_branch_separate_initial_source_and_messages(self):
        initial = self.workspace / "initial folder"
        initial.mkdir()
        (initial / "initial.txt").write_text("initial data")
        _, result = self.run_action(
            initial_source_folder="initial folder", initial_commit_message="Bootstrap artifacts",
            commit_message="Publish artifacts", commit_author="Release Author <release@example.test>",
        )
        self.assertEqual(self.remote_files(), {"artifact.txt"})
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", "releases~1:initial.txt"), "initial data")
        self.assertEqual(self.git("--git-dir", str(self.remote), "log", "--format=%s", "releases").splitlines(), ["Publish artifacts", "Bootstrap artifacts"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "log", "--format=%an <%ae>", "releases").splitlines(), ["Release Author <release@example.test>"] * 2)
        self.assertEqual(result["commit_hash"], self.remote_hash())

    def test_missing_branch_initial_files_preserved_with_no_delete(self):
        initial = self.workspace / "initial folder"
        initial.mkdir()
        (initial / "initial.txt").write_text("initial data")
        self.run_action(initial_source_folder="initial folder", no_delete="true")
        self.assertEqual(self.remote_files(), {"artifact.txt", "initial.txt"})

    def test_empty_missing_branch_can_be_initialized(self):
        (self.source / "artifact.txt").unlink()
        self.run_action()
        self.assertEqual(self.remote_files(), set())
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-list", "--count", "releases"), "1")

    def test_unchanged_does_not_commit_or_push_and_returns_outputs(self):
        previous = self.seed_branch({"artifact.txt": "new artifact\n"})
        self.reject_pushes()
        log, result = self.run_action()
        self.assertIn("No changes", log)
        self.assertEqual(result["commit_hash"], previous)
        self.assertEqual(self.remote_hash(), previous)

    def reject_pushes(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'fixture rejects push' >&2\nexit 1\n")
        hook.chmod(0o755)

    def test_dry_run_existing_branch_creates_local_commit_without_push(self):
        previous = self.seed_branch()
        self.reject_pushes()
        log, result = self.run_action(dry_run="true")
        self.assertIn("[DRY-RUN]", log)
        self.assertEqual(self.remote_hash(), previous)
        self.assertNotEqual(result["commit_hash"], previous)
        self.assertEqual((Path(result["working_directory"]) / "artifact.txt").read_text(), "new artifact\n")

    def test_dry_run_missing_branch_does_not_push_initial_or_final_commit(self):
        self.reject_pushes()
        initial = self.workspace / "initial"
        initial.mkdir()
        (initial / "base.txt").write_text("initial")
        _, result = self.run_action(dry_run="false", initial_source_folder="initial", no_delete="false")
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "refs/heads"), "")
        self.assertEqual(self.git("-C", result["working_directory"], "rev-list", "--count", "HEAD"), "2")
        self.assertTrue((Path(result["working_directory"]) / "base.txt").exists())

    def test_custom_author_and_default_committer(self):
        self.seed_branch()
        _, result = self.run_action(commit_author="Custom Author <custom@example.test>", commit_message="Custom publication")
        self.assertEqual(self.git("-C", result["working_directory"], "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s"),
                         "Custom Author <custom@example.test>|publisher <publisher@users.noreply.github.com>|Custom publication")

    def test_token_pat_precedence_host_and_repository(self):
        for token, pat, host, repository in [
            (TOKEN, "", "", ""),
            (TOKEN, PAT, "git.example.test", "other/repository"),
            ("", PAT, "git.example.test", ""),
            ("", "test-user@example.test:test-pat-never-log", "git.example.test", ""),
        ]:
            with self.subTest(pat=bool(pat), host=host):
                credentials = pat or token
                if ":" not in credentials:
                    credentials = f"x-access-token:{credentials}"
                url = f"https://{credentials}@{host or 'github.com'}/{repository or 'fixture/artifacts'}.git"
                self.git("config", "--global", f"url.file://{self.remote}.insteadOf", url)
                log, result = self.run_action(
                    github_token=token, github_pat=pat, host=host, repository=repository,
                    env_changes={"INPUT_REMOTE": None},
                )
                self.assertEqual(self.git("-C", result["working_directory"], "config", "--get", "remote.origin.url"), url)
                self.assertEqual(self.remote_text("artifact.txt"), "new artifact")
                if pat:
                    self.assertNotIn(pat, log)
                    self.assertNotIn("test-user", log)

    def test_action_metadata_preserves_the_positional_contract(self):
        metadata = (ACTION_ROOT / "action.yml").read_text()
        arguments = re.findall(r"- \$\{\{ inputs\.(\w+) \}\}", metadata)
        self.assertEqual(arguments, [
            "repository", "branch", "host", "github_token", "github_pat", "source_folder", "target_folder",
            "commit_author", "commit_message", "dry_run", "working_directory", "initial_source_folder",
            "initial_commit_message", "no_delete",
        ])
        self.assertIn("using: 'docker'", metadata)

    def test_source_default_workspace_root_excludes_git_metadata(self):
        hidden = self.workspace / ".git"
        hidden.mkdir()
        (hidden / "config").write_text("source metadata must not be copied")
        self.run_action(source_folder="", working_directory="")
        self.assertEqual(self.remote_files(), {"source folder/artifact.txt"})

    def test_default_working_directory_and_author_and_ref(self):
        self.seed_branch()
        _, result = self.run_action(working_directory="", env_changes={"GITHUB_BASE_REF": "feature/target"})
        self.assertEqual(Path(result["working_directory"]).parent, self.home)
        self.assertEqual(self.git("-C", result["working_directory"], "log", "-1", "--format=%an <%ae>|%s"),
                         "publisher <publisher@users.noreply.github.com>|[Local publication test] Publish from fixture/artifacts:target/source folder")

    def test_newline_in_working_directory_is_encoded_in_output(self):
        path = self.root / "checkout\nwith newline"
        _, result = self.run_action(working_directory=str(path))
        self.assertEqual(result["working_directory"], str(path))

    def test_remote_unavailable_is_failure_not_missing_branch(self):
        log, _ = self.run_action(expected=1, env_changes={"INPUT_REMOTE": str(self.root / "missing.git")})
        self.assertNotIn("Creating initial commit", log)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "refs/heads"), "")

    def test_fetch_failure_propagates_and_redacts_git_diagnostics(self):
        self.seed_branch()
        url = f"https://{PAT}@git.example.test/fixture/artifacts.git"
        self.git("config", "--global", f"url.file://{self.remote}.insteadOf", url)
        shim = self.root / "bin"
        shim.mkdir()
        real_git = "/usr/bin/git" if os.environ.get("PUBLISH_TEST_IMAGE") else shutil.which("git")
        wrapper = shim / "git"
        wrapper.write_text(
            '#!/bin/bash\nif [ "$1" = fetch ]; then\n'
            '  printf "fatal: failed fetching %s using %s\\n" "$INPUT_REMOTE" "$INPUT_GITHUB_PAT" >&2\n'
            '  printf "fatal: rejected password %s\\n" "${INPUT_GITHUB_PAT#*:}" >&2\n'
            '  printf "fatal: could not read Password for https://test-user@git.example.test\\n" >&2\n'
            '  exit 71\nfi\nexec "$PUBLISH_REAL_GIT" "$@"\n'
        )
        wrapper.chmod(0o755)
        log, _ = self.run_action(
            expected=1, github_pat=PAT,
            env_changes={"INPUT_REMOTE": url, "INPUT_GITHUB_PAT": PAT, "PUBLISH_REAL_GIT": real_git, "PATH": f"{shim}:{self.env['PATH']}"},
        )
        self.assertIn("https://git.example.test/fixture/artifacts.git", log)
        self.assertIn("[REDACTED]", log)
        self.assertNotIn("test-user", log)
        self.assertNotIn("Creating commit", log)

    def test_rejected_push_fails_without_success_outputs(self):
        previous = self.seed_branch()
        self.reject_pushes()
        self.run_action(expected=1)
        self.assertEqual(self.remote_hash(), previous)

    def test_rejected_initial_push_fails_without_outputs(self):
        self.reject_pushes()
        self.run_action(expected=1)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "refs/heads"), "")

    def test_commit_failure_propagates_without_push(self):
        previous = self.seed_branch()
        hooks = self.root / "reject-commit"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.run_action(expected=1, env_changes={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks)})
        self.assertEqual(self.remote_hash(), previous)

    def test_initial_commit_failure_does_not_create_remote_branch(self):
        hooks = self.root / "reject-initial-commit"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.run_action(expected=1, env_changes={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks)})
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "refs/heads"), "")

    def test_checkout_failure_does_not_publish(self):
        previous = self.seed_branch()
        hooks = self.root / "reject-checkout"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.run_action(expected=1, env_changes={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks)})
        self.assertEqual(self.remote_hash(), previous)

    def test_git_status_failure_is_not_reported_as_unchanged(self):
        previous = self.seed_branch()
        hooks = self.root / "corrupt-index"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\nprintf invalid > .git/index\n")
        hook.chmod(0o755)
        log, _ = self.run_action(expected=1, env_changes={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks)})
        self.assertNotIn("No changes", log)
        self.assertEqual(self.remote_hash(), previous)

    def test_required_inputs_invalid_branch_and_missing_sources(self):
        for changes in [dict(branch=""), dict(github_token="", github_pat=""), dict(branch="bad branch"),
                        dict(source_folder="missing"), dict(source_folder="source folder/artifact.txt"),
                        dict(initial_source_folder="missing")]:
            with self.subTest(changes=changes):
                self.run_action(expected=1, **changes)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "refs/heads"), "")

    def test_missing_output_file_fails_before_publication(self):
        previous = self.seed_branch()
        self.run_action(expected=1, env_changes={"GITHUB_OUTPUT": None})
        self.assertEqual(self.remote_hash(), previous)

    def test_unsafe_target_or_working_directory_preserves_files(self):
        unrelated = self.root / "user directory"
        unrelated.mkdir()
        marker = unrelated / "keep.txt"
        marker.write_text("user data")
        for changes in [dict(target_folder="../user directory"), dict(target_folder=".git"),
                        dict(working_directory=str(unrelated)), dict(working_directory=str(self.source / "checkout"))]:
            with self.subTest(changes=changes):
                self.run_action(expected=1, **changes)
        self.assertEqual(marker.read_text(), "user data")
        self.assertEqual((self.source / "artifact.txt").read_text(), "new artifact\n")
        self.assertFalse((self.source / "checkout").exists())

    def test_target_symlink_cannot_escape_checkout(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("user data")
        (self.seed / "linked").symlink_to(outside, target_is_directory=True)
        previous = self.seed_branch()
        self.run_action(expected=1, target_folder="linked")
        self.assertEqual(marker.read_text(), "user data")
        self.assertEqual(self.remote_hash(), previous)


if __name__ == "__main__":
    unittest.main(verbosity=2)
