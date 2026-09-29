#!/bin/bash

# Name the Docker inputs.
#
INPUT_REPOSITORY="$1"
INPUT_BRANCH="$2"
INPUT_HOST="$3"
INPUT_GITHUB_TOKEN="$4"
INPUT_GITHUB_PAT="$5"
INPUT_SOURCE_FOLDER="$6"
INPUT_TARGET_FOLDER="$7"
INPUT_COMMIT_AUTHOR="$8"
INPUT_COMMIT_MESSAGE="$9"
INPUT_DRYRUN="${10}"
INPUT_WORKDIR="${11}"
INPUT_INITIAL_SOURCE_FOLDER="${12}"
INPUT_INITIAL_COMMIT_MESSAGE="${13}"
INPUT_NO_DELETE="${14}"

# Check for required inputs.
#
[ -z "$INPUT_BRANCH" ] && echo >&2 "::error::'branch' is required" && exit 1
[ -z "$INPUT_GITHUB_TOKEN" -a -z "$INPUT_GITHUB_PAT" ] && echo >&2 "::error::'github_token' or 'github_pat' is required" && exit 1
[ -n "${GITHUB_OUTPUT}" ] || { echo >&2 "::error::GITHUB_OUTPUT is required"; exit 1; }
touch -- "${GITHUB_OUTPUT}" || exit 1
GITHUB_OUTPUT="$(realpath "${GITHUB_OUTPUT}")" || exit 1

# Set state from inputs or defaults.
#
REPOSITORY="${INPUT_REPOSITORY:-${GITHUB_REPOSITORY}}"
BRANCH="${INPUT_BRANCH}"
HOST="${INPUT_HOST:-github.com}"
TOKEN="${INPUT_GITHUB_PAT:-${INPUT_GITHUB_TOKEN}}"
# A bare token is an HTTPS password; explicit username:password credentials
# retain their existing meaning. Supplying both avoids interactive Git prompts.
HTTPS_CREDENTIALS="${TOKEN}"
CREDENTIAL_PASSWORD="${TOKEN#*:}"
if [[ "${TOKEN}" != *:* ]]; then
    HTTPS_CREDENTIALS="x-access-token:${TOKEN}"
fi
REMOTE="${INPUT_REMOTE:-https://${HTTPS_CREDENTIALS}@${HOST}/${REPOSITORY}.git}"
# Never print credentials, including in diagnostics returned by Git.
REMOTE_DISPLAY="${REMOTE}"
if [[ "${REMOTE}" =~ ^(https?://)[^/]*@(.*)$ ]]; then
    REMOTE_DISPLAY="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
fi
redact_remote() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line//"${REMOTE}"/"${REMOTE_DISPLAY}"}
        # Git may omit the password or rewrite the URL in its diagnostics.
        while [[ "${line}" =~ (https?://)[^/[:space:]]*@ ]]; do
            line=${line//"${BASH_REMATCH[0]}"/"${BASH_REMATCH[1]}"}
        done
        line=${line//"${TOKEN}"/"[REDACTED]"}
        if [ -n "${CREDENTIAL_PASSWORD}" ]; then
            line=${line//"${CREDENTIAL_PASSWORD}"/"[REDACTED]"}
        fi
        printf '%s\n' "$line"
    done
}
remote_git() {
    git "$@" 2> >(redact_remote >&2) | redact_remote
    return "${PIPESTATUS[0]}"
}

SOURCE_FOLDER="${INPUT_SOURCE_FOLDER:-.}"
INITIAL_SOURCE_FOLDER="${INPUT_INITIAL_SOURCE_FOLDER:-${SOURCE_FOLDER}}"
TARGET_FOLDER="${INPUT_TARGET_FOLDER}"

REF="${GITHUB_BASE_REF:-${GITHUB_REF}}"
REF_BRANCH=$(echo "${REF}" | rev | cut -d/ -f1 | rev)
[ -z "$REF_BRANCH" ] && echo 2>&1 "No ref branch" && exit 1

COMMIT_AUTHOR="${INPUT_COMMIT_AUTHOR:-${GITHUB_ACTOR} <${GITHUB_ACTOR}@users.noreply.github.com>}"
COMMIT_MESSAGE="${INPUT_COMMIT_MESSAGE:-[${GITHUB_WORKFLOW}] Publish from ${GITHUB_REPOSITORY}:${REF_BRANCH}/${SOURCE_FOLDER}}"
INITIAL_COMMIT_MESSAGE="${INPUT_INITIAL_COMMIT_MESSAGE}"

# Calculate the real source path.
#
SOURCE_PATH="$(realpath "${SOURCE_FOLDER}")" || exit 1
INITIAL_SOURCE_PATH="$(realpath "${INITIAL_SOURCE_FOLDER}")" || exit 1
[ -d "${SOURCE_PATH}" ] || { echo >&2 "::error::Source folder must be a directory"; exit 1; }
[ -d "${INITIAL_SOURCE_PATH}" ] || { echo >&2 "::error::Initial source folder must be a directory"; exit 1; }
echo "::debug::SOURCE_PATH=${SOURCE_PATH}"
echo "::debug::INITIAL_SOURCE_PATH=${INITIAL_SOURCE_PATH}"

# Let's start doing stuff.
echo "Publishing ${SOURCE_FOLDER} to ${REMOTE_DISPLAY}:${BRANCH}/${TARGET_FOLDER}"

# Create a working directory; the workspace may be filled with other important
# files.
#
WORK_DIR="${INPUT_WORKDIR:-$(mktemp -d "${HOME}/gitrepo.XXXXXX")}" || exit 1
[ -z "${WORK_DIR}" ] && echo >&2 "::error::Failed to create temporary working directory" && exit 1
WORK_DIR="$(realpath -m "${WORK_DIR}")" || exit 1
for source in "${SOURCE_PATH}" "${INITIAL_SOURCE_PATH}"; do
    case "${WORK_DIR}/" in
        "${source}/"*) echo >&2 "::error::Working directory must be outside the source folders"; exit 1 ;;
    esac
done
mkdir -p "${WORK_DIR}" || exit 1
contents=$(ls -A "${WORK_DIR}") || exit 1
[ -z "$contents" ] || { echo >&2 "::error::Working directory must be empty"; exit 1; }
cd "${WORK_DIR}" || exit 1

# Resolve after checkout as well, to reject target symlinks outside this clone.
target_path() {
    TARGET_PATH="$(realpath -m "${WORK_DIR}/${TARGET_FOLDER}")" || return 1
    case "${TARGET_PATH}" in
        "${WORK_DIR}/.git"|"${WORK_DIR}/.git/"*) echo >&2 "::error::Target folder cannot be .git"; return 1 ;;
        "${WORK_DIR}"|"${WORK_DIR}/"*) ;;
        *) echo >&2 "::error::Target folder must be inside the working directory"; return 1 ;;
    esac
}
publish_outputs() {
    local delimiter
    COMMIT_HASH="$(git rev-parse HEAD)" || return 1
    delimiter="publish_workdir_${COMMIT_HASH}_$$"
    while [[ $'\n'"${WORK_DIR}"$'\n' == *$'\n'"${delimiter}"$'\n'* ]]; do delimiter="${delimiter}_"; done
    printf 'commit_hash=%s\nworking_directory<<%s\n%s\n%s\n' "${COMMIT_HASH}" "$delimiter" "${WORK_DIR}" "$delimiter" >> "${GITHUB_OUTPUT}"
}
target_path || exit 1
git check-ref-format --branch "${BRANCH}" >/dev/null || exit 1

# Initialize git repo and configure for remote access.
#
echo "Initializing repository with remote ${REMOTE_DISPLAY}"
git init || exit 1
git config --local user.email "${GITHUB_ACTOR}@users.noreply.github.com" || exit 1
git config --local user.name  "${GITHUB_ACTOR}" || exit 1
git config --global --add safe.directory "${WORK_DIR}" || exit 1
remote_git remote add origin "${REMOTE}" || exit 1

# Fetch initial (current contents).
#
echo "Fetching ${REMOTE_DISPLAY}:${BRANCH}"
REMOTE_HEAD=$(remote_git ls-remote --heads origin "refs/heads/${BRANCH}") || { echo >&2 "$REMOTE_HEAD"; exit 1; }
if [ -z "${REMOTE_HEAD}" ] ; then
    echo "Initialising ${BRANCH} branch"
    git checkout --orphan "${BRANCH}" || exit 1
    target_path || exit 1
    echo "Populating ${TARGET_PATH}"
    mkdir -p "${TARGET_PATH}" || exit 1
    if [ -z "$INPUT_NO_DELETE" ] ; then
        rsync -a --checksum --quiet --delete --exclude ".git" "${INITIAL_SOURCE_PATH}/" "${TARGET_PATH}" || exit 1
    else
        rsync -a --checksum --quiet --exclude ".git" "${INITIAL_SOURCE_PATH}/" "${TARGET_PATH}" || exit 1
    fi

    echo "Creating initial commit"
    git add -- "${TARGET_PATH}" || exit 1
    git commit --allow-empty -m "${INITIAL_COMMIT_MESSAGE}" --author "${COMMIT_AUTHOR}" || exit 1
    COMMIT_HASH="$(git rev-parse HEAD)" || exit 1
    echo "Created commit ${COMMIT_HASH}"

    if [ -z "${INPUT_DRYRUN}" ] ; then
        echo "Pushing to ${REMOTE_DISPLAY}:${BRANCH}"
        remote_git push origin "${BRANCH}" || exit 1
    else
        echo "[DRY-RUN] Not pushing to ${REMOTE_DISPLAY}:${BRANCH}"
    fi
else
    remote_git fetch --depth 1 origin "refs/heads/${BRANCH}" || exit 1
    git checkout -b "${BRANCH}" FETCH_HEAD || exit 1
fi

# Create the target directory (if necessary) and copy files from source.
# Compare content even when build tools preserve file sizes and timestamps.
#
target_path || exit 1
echo "Populating ${TARGET_PATH}"
mkdir -p "${TARGET_PATH}" || exit 1

if [ -z "$INPUT_NO_DELETE" ] ; then
    rsync -a --checksum --quiet --delete --exclude ".git" "${SOURCE_PATH}/" "${TARGET_PATH}" || exit 1
else
    rsync -a --checksum --quiet --exclude ".git" "${SOURCE_PATH}/" "${TARGET_PATH}" || exit 1
fi

# Check changes
#
STATUS="$(git status --porcelain)" || exit 1
if [ -z "${STATUS}" ] ; then
    echo "No changes, script exited"
    publish_outputs || exit 1
    exit 0
fi

# Create commit with changes.
#
echo "Creating commit"
git add -- "${TARGET_PATH}" || exit 1
git commit -m "${COMMIT_MESSAGE}" --author "${COMMIT_AUTHOR}" || exit 1
COMMIT_HASH="$(git rev-parse HEAD)" || exit 1
echo "Created commit ${COMMIT_HASH}"

# Push if not a dry-run.
#
if [ -z "${INPUT_DRYRUN}" ] ; then
    echo "Pushing to ${REMOTE_DISPLAY}:${BRANCH}"
    remote_git push origin "${BRANCH}" || exit 1
else
    echo "[DRY-RUN] Not pushing to ${REMOTE_DISPLAY}:${BRANCH}"
fi

publish_outputs || exit 1
