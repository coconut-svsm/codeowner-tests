#!/usr/bin/env python3
import os
import sys
import json
import re
from pathlib import Path
from fnmatch import fnmatch

try:
    from github import Github, GithubException
except ImportError:
    print("Error: PyGithub is not installed")
    print("Install with: pip install PyGithub")
    sys.exit(1)


def parse_reviewers_file(file_path):
    """Parse CODEOWNERS-style file format.

    Supports:
    - @username or plain username
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


def email_to_username(email, gh, repo_name):
    """Convert email to GitHub username using multiple strategies.

    1. Search users by email (works for public emails)
    2. Search commits in repo (works for private emails with commits)
    """
    # Strategy 1: Search users by email
    try:
        users = gh.search_users(f"{email} in:email")
        for user in users:
            print(f"  ✓ Resolved {email} -> @{user.login}")
            return user.login
    except GithubException:
        pass

    # Strategy 2: Search commits by email in repo
    try:
        commits = gh.search_commits(f"author-email:{email} repo:{repo_name}")
        for commit in commits:
            if commit.author:
                username = commit.author.login
                print(f"  ✓ Resolved {email} -> @{username} (via commits)")
                return username
    except GithubException:
        pass

    print(f"  ⚠ Could not resolve email: {email}")
    return None


def resolve_reviewers(reviewers, gh, repo_name):
    """Resolve reviewer identifiers to GitHub usernames.

    Handles:
    - @username -> username
    - username -> username
    - email@domain.com -> username (via API lookup)
    - @org/team -> org/team
    """
    email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

    resolved = []
    teams = []

    for reviewer in reviewers:
        # Team: @org/team-name
        if '/' in reviewer:
            teams.append(reviewer.lstrip('@'))
        # Email: convert to username
        elif email_pattern.match(reviewer):
            username = email_to_username(reviewer, gh, repo_name)
            if username:
                resolved.append(username)
        # Username: strip @ prefix if present
        else:
            resolved.append(reviewer.lstrip('@'))

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
            # Normalize pattern: remove leading slash if present
            normalized_pattern = pattern.lstrip('/')

            # Handle directory patterns with proper boundary matching
            if normalized_pattern.endswith('/'):
                dir_pattern = normalized_pattern.rstrip('/')
                # Check if file is in this directory
                if file_path.startswith(dir_pattern + '/') or file_path == dir_pattern:
                    matched_reviewers = pattern_reviewers
            # Handle glob patterns
            elif fnmatch(file_path, normalized_pattern):
                matched_reviewers = pattern_reviewers

        all_reviewers.extend(matched_reviewers)

    # Remove duplicates
    return list(set(all_reviewers))


def main():
    # Get inputs from environment variables
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
        repo_name = event['repository']['full_name']
        pr_author = event['pull_request']['user']['login']
    except KeyError as e:
        print(f"Error: Missing required field in GitHub event: {e}")
        sys.exit(1)

    print(f"Processing PR #{pr_number} in {repo_name}")
    print(f"PR Author: {pr_author}")

    # Initialize GitHub client
    try:
        gh = Github(github_token)
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(pr_number)
    except GithubException as e:
        print(f"Error accessing GitHub API: {e}")
        sys.exit(1)

    # Parse reviewers file
    if not Path(config_path).exists():
        print(f"Error: Config file '{config_path}' not found")
        sys.exit(1)

    rules = parse_reviewers_file(config_path)
    print(f"Loaded {len(rules)} reviewer rule(s)")

    # Get changed files
    changed_files = [f.filename for f in pr.get_files()]
    print(f"Changed files: {', '.join(changed_files)}")

    # Match files to reviewers
    matched_reviewers = match_files_to_reviewers(changed_files, rules)

    if not matched_reviewers:
        print("No reviewers matched for changed files")
        return

    print(f"Matched reviewers: {', '.join(matched_reviewers)}")

    # Resolve reviewers (emails, usernames, teams)
    reviewers, teams = resolve_reviewers(matched_reviewers, gh, repo_name)

    # Remove PR author from reviewers list
    filtered_reviewers = [r for r in reviewers if r != pr_author]

    if pr_author in reviewers:
        print(f"  ℹ Skipping PR author: @{pr_author}")

    if not filtered_reviewers and not teams:
        print("No reviewers to assign")
        return

    reviewers = filtered_reviewers

    # Assign reviewers
    try:
        if reviewers:
            pr.create_review_request(reviewers=reviewers)
            print(f"✓ Assigned reviewers: {', '.join(reviewers)}")
        if teams:
            pr.create_review_request(team_reviewers=teams)
            print(f"✓ Assigned teams: {', '.join(teams)}")
    except GithubException as e:
        print(f"Error assigning reviewers: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
