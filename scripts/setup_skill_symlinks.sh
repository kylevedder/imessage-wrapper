#!/bin/sh
set -eu

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd -P)
repo_root=$(CDPATH= cd "$script_dir/.." && pwd -P)
source_root="$repo_root/skills"
codex_home=${CODEX_HOME:-"$HOME/.codex"}
target_root="$codex_home/skills"
backup_root="$codex_home/skill-backups"
check_only=false

skills="
search-imessages
search-imessage-contacts
add-imessage-contact
send-imessage
"

usage() {
    printf '%s\n' "Usage: $0 [--check]"
    printf '%s\n' "Link the repository-managed iMessage skills into \${CODEX_HOME:-\$HOME/.codex}/skills."
    printf '%s\n' "Existing paths are moved into a timestamped backup before replacement."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            check_only=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

for skill in $skills; do
    if [ ! -f "$source_root/$skill/SKILL.md" ]; then
        printf 'Missing repository skill: %s\n' "$source_root/$skill" >&2
        exit 1
    fi
done

if [ "$check_only" = true ]; then
    status=0
    for skill in $skills; do
        source_path="$source_root/$skill"
        target_path="$target_root/$skill"
        if [ -L "$target_path" ] && [ "$(readlink "$target_path")" = "$source_path" ]; then
            printf 'ok: %s -> %s\n' "$target_path" "$source_path"
        else
            printf 'not linked: %s -> %s\n' "$target_path" "$source_path" >&2
            status=1
        fi
    done
    exit "$status"
fi

mkdir -p "$target_root"
backup_dir=""

for skill in $skills; do
    source_path="$source_root/$skill"
    target_path="$target_root/$skill"

    if [ -L "$target_path" ] && [ "$(readlink "$target_path")" = "$source_path" ]; then
        printf 'already linked: %s -> %s\n' "$target_path" "$source_path"
        continue
    fi

    if [ -e "$target_path" ] || [ -L "$target_path" ]; then
        if [ -z "$backup_dir" ]; then
            backup_dir="$backup_root/$(date -u '+%Y%m%dT%H%M%SZ')-$$"
            mkdir -p "$backup_dir"
        fi
        mv "$target_path" "$backup_dir/$skill"
        printf 'backed up: %s -> %s\n' "$target_path" "$backup_dir/$skill"
    fi

    ln -s "$source_path" "$target_path"
    printf 'linked: %s -> %s\n' "$target_path" "$source_path"
done

if [ -n "$backup_dir" ]; then
    printf 'backup: %s\n' "$backup_dir"
fi
