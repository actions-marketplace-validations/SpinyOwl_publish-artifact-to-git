publish-to-git
==============

[GitHub Action](https://github.com/features/actions) for publishing a directory
and its contents to another git repository.

This can be especially useful for publishing static website, such as with
[GitHub Pages](https://pages.github.com/), from built files in other job
steps, such as [Doxygen](http://www.doxygen.nl/) generated HTML files.

Use `${{ secrets.GITHUB_TOKEN }}` with `contents: write` permission to publish
to the current repository. To publish to another repository, pass a Personal
Access Token with access to the destination through `github_pat`. A non-empty
`github_pat` takes precedence over `github_token`; PAT-only invocation is also
supported. See [GitHub token permissions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication)
and [workflow secrets](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions).

A bare token is sent as the HTTPS password with username `x-access-token`.
Explicit `username:password` credentials supplied through `github_pat` are
preserved. Git does not need an interactive password prompt.

Inputs
------

- `repository`: Destination repository (default: current repository).
- `branch`: Destination branch (required).
- `host`: Destination git host (default: `github.com`).
- `github_token`: GitHub Token (use `secrets.GITHUB_TOKEN`; either this or `github_pat` must be non-empty).
- `github_pat`: Personal Access Token or other https credentials.
- `source_folder`: Source folder in workspace to copy (default: workspace root).
- `target_folder`: Target folder in destination branch to copy to (default: repository root).
- `commit_author`: Override commit author as `Name <email>` (default: `{github.actor} <{github.actor}@users.noreply.github.com>`). The committer remains the workflow actor.
- `commit_message`: Set commit message (default: `[workflow] Publish from [repository]:[branch]/[folder]`).
- `dry_run`: Does not push if non-empty (default: empty).
- `working_directory`: Empty directory for the checkout, outside both source folders (default: random location in `${HOME}`). A missing directory is created; relative paths are resolved from the workspace.
- `initial_source_folder`: Source folder in workspace to copy if branch didn't exist (default: `source_folder` value)
- `initial_commit_message`: Commit message if branch didn't exist (default: `Initial commit`)
- `no_delete`: Do not delete files from the destination repository if they are not exist in source folder (set to non-empty string to skip file removing)

Outputs
-------

- `commit_hash`: SHA hash of the final commit. When files are unchanged, this is
  the existing HEAD; in a dry run, this is the local commit that was not pushed.
- `working_directory`: Absolute working directory of the destination checkout
  inside the Action container.

Both outputs are written on every successful invocation using
[`GITHUB_OUTPUT`](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#setting-an-output-parameter).
They are not emitted on failure.

Publication behavior
--------------------

The Docker Action runs on Linux runners. Source paths are resolved from the
workspace before changing into the working directory. The target folder is
relative to the destination checkout; paths escaping that checkout or targeting
its `.git` metadata are rejected. Spaces and nested directories are supported.

For an existing branch, the Action checks out its fetched HEAD and synchronizes
`source_folder` into `target_folder`. By default, files missing from the source
are deleted **within the target folder**; files elsewhere in the repository
remain. With `no_delete` set to any non-empty string, existing files absent from
the source are preserved. `.git` metadata is excluded from synchronization.
Content checksums detect updates even when file sizes and timestamps match;
this requires reading source and destination file contents.

An absent branch is created with an orphan initial commit populated from
`initial_source_folder` and using `initial_commit_message`. Then the normal
source is synchronized and committed if different. This preserves the original
two-stage behavior: there may be two commits and two pushes. An empty initial
source is allowed. A failure after the initial push can leave that initial commit
on the destination branch.

Unchanged content creates no further commit or push. `dry_run` still fetches,
copies and commits locally, but prevents **all** pushes, including the initial
push for an absent branch. Both `dry_run` and `no_delete` retain their historical
non-empty-string semantics: the string `false` also enables the option.

Remote messages and Git network diagnostics redact credentials. Failures to
inspect the remote, fetch, check out, commit or push exit unsuccessfully; a failed
remote lookup is not treated as an absent branch.

License
-------

MIT License. See [LICENSE](LICENSE) for details.

Usage Example
-------------

```yaml
jobs:
  publish:
    runs-on: ubuntu-latest
    # Checkout and build steps must populate build/publish first.
    steps:
      - uses: SpinyOwl/publish-artifact-to-git@2.0.0 # Pin an immutable commit SHA in production.
        if: success() && github.event_name == 'push' && !contains(github.event.head_commit.message, '[skip publish]')
        with:
          repository: SpinyOwl/repo
          branch: releases
          github_token: '${{ secrets.GITHUB_TOKEN }}'
          github_pat: '${{ secrets.GH_PAT }}'
          source_folder: build/publish
          no_delete: true
```

Functional tests
----------------

On Linux with Python 3, Bash, Git and rsync installed:

```sh
bash -n entrypoint.sh
python3 tests/test_publish.py
python3 tests/test_authentication.py
```

The suite runs the entrypoint against temporary local bare Git repositories,
with isolated Git configuration and no writes to GitHub or other user remotes.
It covers existing/absent branches, initial files/messages, updates and deletions,
`no_delete`, nested paths and spaces, unchanged files, dry runs, author/committer,
token/PAT precedence, custom host/repository, outputs and path validation.
Git URL rewriting maps HTTPS-shaped authentication cases to local file remotes;
these cases check argument selection. The separate authentication suite keeps
the generated credentials and rewrites only HTTPS to HTTP for a loopback Git
smart-HTTP server. It exercises authentication challenges and real publication
with a GitHub-shaped token, a bare PAT, explicit credentials, PAT precedence and
rejected credentials. This does not check TLS or live GitHub authorization.
A Git shim injects
a fetch failure and credential-bearing diagnostics; real Git hooks reject
commits/pushes and establish equal timestamps for the checksum regression.

To run the same publication suite through the actual Docker image:

```sh
docker build -t publish-artifact-test .
PUBLISH_TEST_IMAGE=publish-artifact-test python3 tests/test_publish.py
```

Test containers have networking disabled and only mount their temporary fixture
directory. The image includes GNU coreutils for `realpath -m` when validating
new nested target paths. CI also invokes `uses: ./` twice to check Action argument
mapping, runner outputs and an unchanged repeat publication against a local bare
repository. Bash syntax checks and workflow linting are static checks; they do
not replace these publication tests.
