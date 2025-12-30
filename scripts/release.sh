#!/bin/bash
# Release script for jellyfin-db-sync
# Updates version in all files, commits, creates tag, and pushes
#
# Usage:
#   ./scripts/release.sh <version>
#   ./scripts/release.sh 0.0.2
#   ./scripts/release.sh 1.0.0
#   ./scripts/release.sh 0.0.8 -m "Fix progress sync to always use last action"
#
# Options:
#   -m, --message  Custom message for the tag (default: "Release <version>")
#   --force        Delete existing tag and recreate it
#   --dry-run      Show what would be done without making changes
#   --no-push      Create commit and tag but don't push
#   --help         Show this help message

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Flags
DRY_RUN=false
NO_PUSH=false
FORCE=false
TAG_MESSAGE=""

# Files to update with their sed patterns
declare -A VERSION_FILES=(
    ["pyproject.toml"]='s/^version = "[0-9]+\.[0-9]+\.[0-9]+"/version = "VERSION"/'
    ["charts/jellyfin-db-sync/Chart.yaml:version"]='s/^version: [0-9]+\.[0-9]+\.[0-9]+/version: VERSION/'
    ["charts/jellyfin-db-sync/Chart.yaml:appVersion"]='s/^appVersion: "[0-9]+\.[0-9]+\.[0-9]+"/appVersion: "VERSION"/'
    ["src/jellyfin_db_sync/__init__.py"]='s/__version__ = "[0-9]+\.[0-9]+\.[0-9]+"/__version__ = "VERSION"/'
    ["src/jellyfin_db_sync/main.py"]='s/version="[0-9]+\.[0-9]+\.[0-9]+"/version="VERSION"/'
    ["src/jellyfin_db_sync/api/status.py"]='s/version="[0-9]+\.[0-9]+\.[0-9]+"/version="VERSION"/'
    ["README.md"]='s|/v[0-9]+\.[0-9]+\.[0-9]+/jellyfin-db-sync-[0-9]+\.[0-9]+\.[0-9]+\.tgz|/vVERSION/jellyfin-db-sync-VERSION.tgz|g'
)

usage() {
    echo "Usage: $0 [OPTIONS] <version>"
    echo ""
    echo "Release script for jellyfin-db-sync"
    echo "Updates version in all files, commits, creates tag, and pushes"
    echo ""
    echo "Arguments:"
    echo "  version         New version number (e.g., 0.0.2, 1.0.0)"
    echo ""
    echo "Options:"
    echo "  -m, --message   Custom message for the tag (default: 'Release <version>')"
    echo "  --force         Delete existing tag and recreate it"
    echo "  --dry-run       Show what would be done without making changes"
    echo "  --no-push       Create commit and tag but don't push"
    echo "  --help          Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 0.0.2                           # Release version 0.0.2"
    echo "  $0 0.0.8 -m 'Fix progress sync'    # Release with custom message"
    echo "  $0 --force 0.0.8                   # Recreate existing release"
    echo "  $0 --dry-run 1.0.0                 # Preview release 1.0.0"
    echo "  $0 --no-push 0.1.0                 # Create release locally only"
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_dry() {
    echo -e "${YELLOW}[DRY-RUN]${NC} $1"
}

validate_version() {
    local version=$1
    if [[ ! $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        log_error "Invalid version format: $version"
        log_error "Version must be in format X.Y.Z (e.g., 0.0.1, 1.2.3)"
        exit 1
    fi
}

check_git_status() {
    cd "$PROJECT_ROOT"

    # Check if we're in a git repository
    if ! git rev-parse --git-dir > /dev/null 2>&1; then
        log_error "Not a git repository"
        exit 1
    fi

    # Check for uncommitted changes (excluding the files we're about to modify)
    if ! git diff --quiet HEAD -- . ':!pyproject.toml' ':!charts/' ':!src/jellyfin_db_sync/__init__.py' ':!src/jellyfin_db_sync/main.py' ':!src/jellyfin_db_sync/api/status.py' ':!README.md' 2>/dev/null; then
        log_warn "You have uncommitted changes in other files"
        log_warn "Consider committing or stashing them first"
        read -p "Continue anyway? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi

    # Check if on main/master branch
    local branch
    branch=$(git rev-parse --abbrev-ref HEAD)
    if [[ "$branch" != "main" && "$branch" != "master" ]]; then
        log_warn "You are on branch '$branch', not main/master"
        read -p "Continue anyway? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

check_tag_exists() {
    local version=$1
    local tag="v$version"

    if git rev-parse "$tag" >/dev/null 2>&1; then
        if $FORCE; then
            log_warn "Tag $tag exists, will be deleted (--force)"
        else
            log_error "Tag $tag already exists"
            log_error "Use --force to delete and recreate, or delete manually:"
            log_error "  git tag -d $tag && git push origin :refs/tags/$tag"
            exit 1
        fi
    fi
}

delete_existing_tag() {
    local version=$1
    local tag="v$version"

    if git rev-parse "$tag" >/dev/null 2>&1; then
        if $DRY_RUN; then
            log_dry "Would delete local tag $tag"
            log_dry "Would delete remote tag $tag"
        else
            log_info "Deleting local tag $tag..."
            git tag -d "$tag"
            log_success "Deleted local tag"

            if git ls-remote --tags origin | grep -q "refs/tags/$tag"; then
                log_info "Deleting remote tag $tag..."
                git push origin :refs/tags/"$tag" 2>/dev/null || true
                log_success "Deleted remote tag"
            fi
        fi
    fi
}

get_current_version() {
    grep -E '^version = "[0-9]+\.[0-9]+\.[0-9]+"' "$PROJECT_ROOT/pyproject.toml" | sed 's/version = "\(.*\)"/\1/'
}

update_file() {
    local file=$1
    local pattern=$2
    local version=$3
    local full_path="$PROJECT_ROOT/$file"

    # Handle special case where file has :suffix for multiple patterns
    local actual_file="${file%%:*}"
    full_path="$PROJECT_ROOT/$actual_file"

    if [[ ! -f "$full_path" ]]; then
        log_warn "File not found: $actual_file"
        return 1
    fi

    # Replace VERSION placeholder with actual version
    local sed_pattern="${pattern//VERSION/$version}"

    if $DRY_RUN; then
        log_dry "Would update $actual_file"
        return 0
    fi

    # Use sed with extended regex
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' -E "$sed_pattern" "$full_path"
    else
        sed -i -E "$sed_pattern" "$full_path"
    fi

    log_success "Updated $actual_file"
}

update_all_versions() {
    local version=$1

    log_info "Updating version to $version in all files..."
    echo ""

    for file in "${!VERSION_FILES[@]}"; do
        update_file "$file" "${VERSION_FILES[$file]}" "$version"
    done

    echo ""
}

create_commit_and_tag() {
    local version=$1
    local tag="v$version"
    local message="${TAG_MESSAGE:-Release $version}"

    cd "$PROJECT_ROOT"

    if $DRY_RUN; then
        log_dry "Would stage all version files"
        log_dry "Would commit with message: 'chore: release $version'"
        log_dry "Would create tag: $tag with message: '$message'"
        return 0
    fi

    log_info "Staging version files..."
    git add pyproject.toml \
            charts/jellyfin-db-sync/Chart.yaml \
            src/jellyfin_db_sync/__init__.py \
            src/jellyfin_db_sync/main.py \
            src/jellyfin_db_sync/api/status.py \
            README.md

    log_info "Creating commit..."
    git commit -m "chore: release $version"
    log_success "Created commit"

    log_info "Creating tag $tag..."
    git tag -a "$tag" -m "$message"
    log_success "Created tag $tag with message: '$message'"
}

push_changes() {
    local version=$1
    local tag="v$version"

    if $DRY_RUN; then
        log_dry "Would push commit to origin"
        log_dry "Would push tag $tag to origin"
        return 0
    fi

    if $NO_PUSH; then
        log_warn "Skipping push (--no-push specified)"
        log_info "To push manually:"
        echo "  git push origin"
        echo "  git push origin $tag"
        return 0
    fi

    log_info "Pushing to origin..."
    git push origin
    log_success "Pushed commit"

    log_info "Pushing tag $tag..."
    git push origin "$tag"
    log_success "Pushed tag $tag"
}

main() {
    local version=""

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --no-push)
                NO_PUSH=true
                shift
                ;;
            --force|-f)
                FORCE=true
                shift
                ;;
            -m|--message)
                if [[ -z "${2:-}" ]]; then
                    log_error "Option $1 requires a message argument"
                    exit 1
                fi
                TAG_MESSAGE="$2"
                shift 2
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            -*)
                log_error "Unknown option: $1"
                usage
                exit 1
                ;;
            *)
                if [[ -z "$version" ]]; then
                    version=$1
                else
                    log_error "Multiple versions specified: $version and $1"
                    exit 1
                fi
                shift
                ;;
        esac
    done

    # Check if version was provided
    if [[ -z "$version" ]]; then
        log_error "Version not specified"
        echo ""
        usage
        exit 1
    fi

    # Validate and prepare
    validate_version "$version"

    local current_version
    current_version=$(get_current_version)

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  jellyfin-db-sync Release Script"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    log_info "Current version: $current_version"
    log_info "New version:     $version"
    $DRY_RUN && log_warn "DRY RUN MODE - no changes will be made"
    $NO_PUSH && log_warn "NO PUSH MODE - changes won't be pushed"
    $FORCE && log_warn "FORCE MODE - existing tag will be deleted"
    echo ""

    # Pre-flight checks
    check_git_status
    check_tag_exists "$version"

    # Delete existing tag if --force
    if $FORCE; then
        delete_existing_tag "$version"
    fi

    # Execute release steps
    update_all_versions "$version"
    create_commit_and_tag "$version"
    push_changes "$version"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    if $DRY_RUN; then
        log_warn "DRY RUN complete - no changes were made"
    else
        log_success "Release $version complete!"
        echo ""
        log_info "GitHub Actions will now:"
        echo "  • Build and push Docker image to GHCR"
        echo "  • Create GitHub Release with changelog"
        echo "  • Package and publish Helm chart"
    fi
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
}

main "$@"
