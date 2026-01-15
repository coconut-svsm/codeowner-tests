#!/usr/bin/env python3
import os
import sys
import json
import re
import time
from pathlib import Path
from fnmatch import fnmatch
import urllib.request
import urllib.error
import urllib.parse


def parse_reviewers_file(file_path):
    """Parse CODEOWNERS-style file format.

    Supports:
    - @username
    - email@domain.com
    - @org/team

    Returns list of (pattern, reviewers) tuples.
    Last matching pattern takes precedence (like CODEOWNERS).
    """
    rules = []

    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue

            # Split pattern and reviewers
            parts = line.split()
            if len(parts) < 2:
                continue

            pattern = parts[0]
            reviewers = parts[1:]

            rules.append((pattern, reviewers))

    return rules


def email_to_username_via_commits(email, repo, token):
    """Convert email to GitHub username by searching commits in the repo."""
    # Search for commits by this email in the repository
    encoded_email = urllib.parse.quote(email)
    url = f"https://api.github.com/search/commits?q=author-email:{encoded_email}+repo:{repo}"
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            if result['total_count'] > 0 and result['items'][0].get('author'):
                username = result['items'][0]['author']['login']
                print(f"  Resolved {email} -> @{username} (via commits)")
                return username
            return None
    except urllib.error.HTTPError as e:
        print(f"  Commit search failed for {email}: {e}")
        return None


def email_to_username(email, token, repo=None, retry_count=0):
    """Convert email to GitHub username using multiple strategies.

    1. First tries user search API (works for public emails)
    2. Falls back to commit search in repo (works for private emails)
    """
    # Strategy 1: Search users by email (only works for public emails)
    encoded_email = urllib.parse.quote(email)
    url = f"https://api.github.com/search/users?q={encoded_email}+in:email"
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            if result['total_count'] > 0:
                username = result['items'][0]['login']
                print(f"  Resolved {email} -> @{username}")
                return username
    except urllib.error.HTTPError as e:
        # Handle rate limiting
        if e.code == 403 and retry_count < 3:
            print(f"  Rate limited, waiting 60s before retry {retry_count + 1}/3")
            time.sleep(60)
            return email_to_username(email, token, repo, retry_count + 1)
        print(f"  User search failed for {email}: {e}")

    # Strategy 2: Search commits by email (works for private emails too)
    if repo:
        username = email_to_username_via_commits(email, repo, token)
        if username:
            return username

    print(f"  Warning: Could not resolve email {email} to GitHub username")
    return None


def resolve_reviewers(reviewers, token, repo):
    """Resolve reviewer identifiers to GitHub usernames.

    Handles:
    - @username -> username
    - email@domain.com -> username (via API lookup)
    - @org/team -> org/team
    """
    # Simple email regex pattern
    email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

    resolved = []
    teams = []

    for reviewer in reviewers:
        # Team: @org/team-name
        if reviewer.startswith('@') and '/' in reviewer:
            teams.append(reviewer[1:])  # Remove @ prefix
        # Username: @username
        elif reviewer.startswith('@'):
            resolved.append(reviewer[1:])  # Remove @ prefix
        # Email: convert to username (proper validation)
        elif email_pattern.match(reviewer):
            username = email_to_username(reviewer, token, repo)
            if username:
                resolved.append(username)
        # Plain username
        else:
            resolved.append(reviewer)

    return resolved, teams


def match_files_to_reviewers(changed_files, rules):
    """Match changed files against rules and return list of reviewers.

    Last matching pattern takes precedence (CODEOWNERS behavior).
    """
    all_reviewers = []

    for file_path in changed_files:
        matched_reviewers = []

        # Find all matching patterns (last one wins)
        for pattern, pattern_reviewers in rules:
            # Normalize pattern: remove leading slash if present (CODEOWNERS uses /path/)
            normalized_pattern = pattern.lstrip('/')

            # Handle directory patterns with proper boundary matching
            if normalized_pattern.endswith('/'):
                # Remove trailing slash for comparison
                dir_pattern = normalized_pattern.rstrip('/')
                # Check if file is in this directory (exact boundary match)
                if file_path.startswith(dir_pattern + '/') or file_path == dir_pattern:
                    matched_reviewers = pattern_reviewers
            # Handle glob patterns
            elif fnmatch(file_path, normalized_pattern):
                matched_reviewers = pattern_reviewers

        all_reviewers.extend(matched_reviewers)

    # Remove duplicates while preserving order
    seen = set()
    unique_reviewers = []
    for reviewer in all_reviewers:
        if reviewer not in seen:
            seen.add(reviewer)
            unique_reviewers.append(reviewer)

    return unique_reviewers


def get_changed_files(repo, pr_number, token):
    """Get list of changed files in a PR."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req) as response:
            files = json.loads(response.read().decode())
            return [f['filename'] for f in files]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if hasattr(e, 'read') else ''
        print(f"Error fetching PR files: {e}")
        print(f"Response: {error_body}")
        sys.exit(1)


def assign_reviewers(repo, pr_number, reviewers, teams, token, pr_author):
    """Assign reviewers to a PR via GitHub API."""
    # Remove PR author from reviewers list
    reviewers = [r for r in reviewers if r != pr_author]

    if not reviewers and not teams:
        print("No reviewers to assign (all matched reviewers are PR author)")
        return

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/requested_reviewers"
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    payload = {}
    if reviewers:
        payload['reviewers'] = reviewers
    if teams:
        payload['team_reviewers'] = teams

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')

    try:
        with urllib.request.urlopen(req) as response:
            if reviewers:
                print(f"Successfully assigned reviewers: {', '.join(reviewers)}")
            if teams:
                print(f"Successfully assigned teams: {', '.join(teams)}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if hasattr(e, 'read') else ''
        print(f"Error assigning reviewers: {e}")
        print(f"Response: {error_body}")
        sys.exit(1)


def main():
    # Get inputs from environment variables with validation
    github_token = os.environ.get('GITHUB_TOKEN')
    if not github_token:
        print("Error: GITHUB_TOKEN environment variable is not set")
        sys.exit(1)

    config_path = os.environ.get('INPUT_CONFIG', 'REVIEWERS')

    # Get PR information from GitHub event
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    if not event_path:
        print("Error: GITHUB_EVENT_PATH environment variable is not set")
        sys.exit(1)

    try:
        with open(event_path, 'r') as f:
            event = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading GitHub event file: {e}")
        sys.exit(1)

    # Validate event structure
    try:
        pr_number = event['pull_request']['number']
        repo = event['repository']['full_name']
        pr_author = event['pull_request']['user']['login']
    except KeyError as e:
        print(f"Error: Missing required field in GitHub event: {e}")
        sys.exit(1)

    print(f"Processing PR #{pr_number} in {repo}")
    print(f"PR Author: {pr_author}")

    # Parse reviewers file
    if not Path(config_path).exists():
        print(f"Config file {config_path} not found")
        sys.exit(1)

    rules = parse_reviewers_file(config_path)
    print(f"Loaded {len(rules)} reviewer rules")

    # Get changed files
    changed_files = get_changed_files(repo, pr_number, github_token)
    print(f"PR has {len(changed_files)} changed file(s): {', '.join(changed_files)}")

    # Match files to reviewers
    matched_reviewers = match_files_to_reviewers(changed_files, rules)
    print(f"Matched reviewer identifiers: {', '.join(matched_reviewers) if matched_reviewers else 'none'}")

    # Resolve reviewers (emails to usernames)
    if matched_reviewers:
        print("Resolving reviewer identifiers...")
        reviewers, teams = resolve_reviewers(matched_reviewers, github_token, repo)

        # Assign reviewers
        if reviewers or teams:
            assign_reviewers(repo, pr_number, reviewers, teams, github_token, pr_author)
        else:
            print("No valid reviewers after resolution")
    else:
        print("No reviewers matched for changed files")


if __name__ == '__main__':
    main()
