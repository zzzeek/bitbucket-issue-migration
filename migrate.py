#!/usr/bin/env python

# This file is part of the Bitbucket issue migration script.
#
# The script is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# The script is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with the Bitbucket issue migration script.
# If not, see <http://www.gnu.org/licenses/>.

import argparse
import contextlib
import os
import pprint
import queue
import random
import re
import subprocess
import sys
import tempfile
import time
import threading
import warnings

import getpass
import yaml
import requests

try:
    import keyring
    assert keyring.get_keyring().priority
except (ImportError, AssertionError):
    # no suitable keyring is available, so mock the interface
    # to simulate no pw
    class keyring:
        get_password = staticmethod(lambda system, username: None)

SEP = "-" * 40


def read_arguments():
    parser = argparse.ArgumentParser(
        description="A tool to migrate issues from Bitbucket to GitHub."
    )

    parser.add_argument(
        "bitbucket_repo",
        help=(
            "Bitbucket repository to pull issues from.\n"
            "Format: <user or organization name>/<repo name>\n"
            "Example: jeffwidman/bitbucket-issue-migration"
        )
    )

    parser.add_argument(
        "github_repo",
        help=(
            "GitHub repository to add issues to.\n"
            "Format: <user or organization name>/<repo name>\n"
            "Example: jeffwidman/bitbucket-issue-migration"
        )
    )

    parser.add_argument(
        "github_username",
        help=(
            "Your GitHub username. This is used only for authentication, not "
            "for the repository location."
        )
    )

    parser.add_argument(
        "-bu", "--bb-user", dest="bitbucket_username",
        help=(
            "Your Bitbucket username. This is only necessary when migrating "
            "private Bitbucket repositories."
        )
    )

    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help=(
            "Simulate issue migration to confirm issues can be extracted from "
            "Bitbucket and converted by this script. Nothing will be copied "
            "to GitHub."
        )
    )

    parser.add_argument(
        "-f", "--skip", type=int, default=0,
        help=(
            "The number of Bitbucket issues to skip. Note that if Bitbucket "
            "issues were deleted, they are already automatically skipped."
        )
    )

    parser.add_argument(
        "-m", "--map-user", action="append", dest="_map_users", default=[],
        help=(
            "Override user mapping for usernames, for example "
            "`--map-user fk=fkrull`.  Can be specified multiple times."
        ),
    )

    parser.add_argument(
        "--skip-attribution-for", dest="bb_skip",
        help=(
            "BitBucket user who doesn't need comments re-attributed. Useful "
            "to skip your own comments, because you are running this script, "
            "and the GitHub comments will be already under your name."
        ),
    )

    parser.add_argument(
        "--link-changesets", action="store_true",
        help="Link changeset references back to BitBucket.",
    )

    parser.add_argument(
        "--mention-attachments", action="store_true",
        help="Mention the names of attachments.",
    )

    parser.add_argument(
        "--attachments-wiki", action="store_true",
        help=(
            "Download attachments and commit them to a local clone of the "
            "the project's github wiki repo.   Comments will be added linking "
            "to this repo. "
        )
    )

    parser.add_argument(
        "--git-ssh-identity", type=str,
        help=(
            "When using the --attachments-wiki option, specify a path "
            "to an alternate identity file."
        )
    )

    parser.add_argument(
        "--mention-changes", action="store_true",
        help="Mention changes in status as comments.",
    )

    parser.add_argument(
        "--use-config", type=str,
        default="config.yml",
        help=(
            "config.yml file to use.  defaults to config.yml."
        )
    )
    return parser.parse_args()


def main(options):
    """Main entry point for the script."""
    bb_url = "https://api.bitbucket.org/2.0/repositories/{repo}/issues".format(
        repo=options.bitbucket_repo)
    options.bb_auth = None
    options.users = dict(user.split('=') for user in options._map_users)
    bb_repo_status = requests.head(bb_url).status_code
    if bb_repo_status == 404:
        raise RuntimeError(
            "Could not find a Bitbucket Issue Tracker at: {}\n"
            "Hint: the Bitbucket repository name is case-sensitive."
            .format(bb_url)
        )
    elif bb_repo_status == 403:  # Only need BB auth creds for private BB repos
        if not options.bitbucket_username:
            raise RuntimeError(
                """
                Trying to access a private Bitbucket repository, but no
                Bitbucket username was entered. Please rerun the script using
                the argument `--bb-user <username>` to pass in your Bitbucket
                username.
                """
            )
        kr_pass_bb = keyring.get_password(
            'Bitbucket', options.bitbucket_username)
        bitbucket_password = kr_pass_bb or getpass.getpass(
            "Please enter your Bitbucket password.\n"
            "Note: If your Bitbucket account has two-factor authentication "
            "enabled, you must temporarily disable it until "
            "https://bitbucket.org/site/master/issues/11774/ is resolved.\n"
        )
        options.bb_auth = (options.bitbucket_username, bitbucket_password)
        # Verify BB creds work
        bb_creds_status = requests.head(
            bb_url, auth=options.bb_auth).status_code
        if bb_creds_status == 401:
            raise RuntimeError("Failed to login to Bitbucket.")
        elif bb_creds_status == 403:
            raise RuntimeError(
                "Bitbucket login succeeded, but user '{}' doesn't have "
                "permission to access the url: {}"
                .format(options.bitbucket_username, bb_url)
            )

    # Always need the GH pass so format_user() can verify links to GitHub user
    # profiles don't 404. Auth'ing necessary to get higher GH rate limits.
    kr_pass_gh = keyring.get_password('Github', options.github_username)
    github_password = kr_pass_gh or getpass.getpass(
        "Please enter your GitHub password.\n"
        "Note: If your GitHub account has authentication enabled, "
        "you must use a personal access token from "
        "https://github.com/settings/tokens in place of a password for this "
        "script.\n"
    )
    options.gh_auth = (options.github_username, github_password)
    # Verify GH creds work
    gh_repo_url = 'https://api.github.com/repos/' + options.github_repo
    gh_repo_status = requests.head(
        gh_repo_url, auth=options.gh_auth).status_code
    if gh_repo_status == 401:
        raise RuntimeError("Failed to login to GitHub.")
    elif gh_repo_status == 403:
        raise RuntimeError(
            "GitHub login succeeded, but user '{}' either doesn't have "
            "permission to access the repo at: {}\n"
            "or is over their GitHub API rate limit.\n"
            "You can read more about GitHub's API rate limiting policies "
            "here: https://developer.github.com/v3/#rate-limiting"
            .format(options.github_username, gh_repo_url)
        )
    elif gh_repo_status == 404:
        raise RuntimeError("Could not find a GitHub repo at: " + gh_repo_url)

    with open(options.use_config, "r") as file_:
        config = yaml.load(file_)

    # GitHub's Import API currently requires a special header
    headers = {'Accept': 'application/vnd.github.golden-comet-preview+json'}
    gh_milestones = GithubMilestones(
        options.github_repo, options.gh_auth, headers)
    gh_labels = GithubLabels(
        config['label_translations'],
        options.github_repo, options.gh_auth, headers)

    if options.attachments_wiki:
        if options.mention_attachments:
            raise TypeError(
                "Options --mention-attachments and --attachments-wiki are "
                "mutually exclusive")
        attachments_repo = AttachmentsRepo(options.github_repo, options)

    print("getting issues from bitbucket")
    issues_iterator = fill_gaps(
        get_issues(bb_url, options.skip, options.bb_auth),
        options.skip
    )

    abort_event = threading.Event()

    work_queue = queue.Queue()
    worker_thread = threading.Thread(
        target=push_issues,
        args=(abort_event, work_queue, options.github_repo,
              options.gh_auth, headers, options.dry_run)
    )
    worker_thread.daemon = True
    worker_thread.start()

    for index, issue in enumerate(issues_iterator):
        if abort_event.is_set():
            break

        if isinstance(issue, DummyIssue):
            comments = []
            changes = []
        else:
            comments = get_issue_comments(issue['id'], bb_url, options.bb_auth)
            changes = get_issue_changes(issue['id'], bb_url, options.bb_auth)

        if options.attachments_wiki:
            attachment_links = process_wiki_attachments(
                issue['id'], bb_url, options.bb_auth,
                options, attachments_repo
            )
        elif options.mention_attachments:
            attachment_links = get_attachment_names(
                issue['id'], bb_url, options.bb_auth)
        else:
            attachment_links = []

        gh_issue = convert_issue(
            issue, comments, changes,
            options, attachment_links, gh_milestones, gh_labels, config
        )
        gh_comments = [
            convert_comment(c, options, config) for c in comments
            if c['content']['raw'] is not None
        ]

        if options.mention_changes:
            last_change = changes[-1]
            gh_comments += [
                converted_change for converted_change in
                [convert_change(c, options, config, gh_labels,
                                c is last_change)
                 for c in changes]
                if converted_change
            ]

        print("Queuing bitbucket issue {} for export".format(issue['id']))
        work_queue.put((issue['id'], gh_issue, gh_comments))

    work_queue.join()


def push_issues(abort, work_queue, github_repo, gh_auth, headers, dry_run):
    num_issues = 0

    while not abort.is_set():
        try:
            issue_id, gh_issue, gh_comments = work_queue.get(timeout=3)
        except queue.Empty:
            if abort.is_set():
                break
            else:
                continue

        num_issues += 1

        # keep one second between API requests per githubs rate limiting
        # advice
        time.sleep(1)

        if dry_run:
            print("\nIssue: ", gh_issue)
            print("\nComments: ", gh_comments)
            continue

        push_respo = push_github_issue(
            gh_issue, gh_comments, options.github_repo,
            options.gh_auth, headers
        )

        # issue POSTed successfully, now verify the import finished before
        # continuing. Otherwise, we risk issue IDs not being sync'd between
        # Bitbucket and GitHub because GitHub processes the data in the
        # background, so IDs can be out of order if two issues are POSTed
        # and the latter finishes before the former. For example, if the
        # former had a bunch more comments to be processed.
        # https://github.com/jeffwidman/bitbucket-issue-migration/issues/45
        status_url = push_respo.json()['url']
        resp = verify_github_issue_import_finished(
            status_url, options.gh_auth, headers)

        # Verify GH & BB issue IDs match.
        # If this assertion fails, convert_links() will have incorrect
        # output.  This condition occurs when:
        # - the GH repository has pre-existing issues.
        # - the Bitbucket repository has gaps in the numbering.
        if resp:
            gh_issue_url = resp.json()['issue_url']
            gh_issue_id = int(gh_issue_url.split('/')[-1])
            if gh_issue_id != issue_id:
                abort.set()
                raise Exception(
                    "Issues are out of sync, got github issue {} but "
                    "bitbucket issue is at {}".format(gh_issue_id, issue_id))
        work_queue.task_done()

        print("Completed {} issues".format(num_issues))


class DummyIssue(dict):
    def __init__(self, num):
        self.update(
            id=num,
            # ...
        )


def fill_gaps(issues_iterator, offset):
    """
    Fill gaps in the issues, assuming an initial offset.

    >>> issues = [
    ...     dict(id=2),
    ...     dict(id=4),
    ...     dict(id=7),
    ... ]
    >>> fill_gaps(issues, 0)
    >>> [issue['id'] for issue in issues]
    [1, 2, 3, 4, 5, 6, 7]

    >>> issues = [
    ...     dict(id=52),
    ...     dict(id=54),
    ... ]
    >>> fill_gaps(issues, 50)
    >>> [issue['id'] for issue in issues]
    [51, 52, 53, 54]
    """

    current_id = offset
    for issue in issues_iterator:
        issue_id = issue['id']
        for dummy_id in range(current_id + 1, issue_id):
            yield DummyIssue(dummy_id)
        current_id = issue_id
        yield issue


def process_wiki_attachments(
        issue_num, bb_url, bb_auth, options, attachments_repo):
    respo = requests.get(
        "{}/{}/attachments".format(bb_url, issue_num),
        auth=bb_auth,
    )
    attachment_links = []

    if respo.status_code != 200:
        raise RuntimeError(
            "Failed to get issue attachments for issue {} due to "
            "unexpected HTTP status code: {}"
            .format(issue_num, respo.status_code)
        )

    result = respo.json()
    for val in result['values']:
        filename = val['name']
        # this seems to be in val['links']['self']['href'][0] also
        content_url = "{}/{}/attachments/{}".format(
            bb_url, issue_num, filename)
        content = requests.get(content_url, auth=bb_auth)
        if content.status_code != 200:
            raise RuntimeError(
                "Failed to download attachment: {}  due to "
                "unexpected HTTP status code: {}"
                .format(content_url, respo.status_code)
            )

        link = attachments_repo.add_attachment(
            issue_num, filename, content.content)
        attachment_links.append(
            {
                "name": filename,
                "link": link
            }
        )
    if result['values']:
        if not options.dry_run:
            attachments_repo.commit(issue_num)
            attachments_repo.push()

    return attachment_links


def get_attachment_names(issue_num, bb_url, bb_auth):
    """Get the names of attachments on this issue."""

    respo = requests.get(
        "{}/{}/attachments".format(bb_url, issue_num),
        auth=bb_auth,
    )
    if respo.status_code == 200:
        result = respo.json()
        return [
            {"name": val['name'], "link": None} for val in result['values']]
    else:
        return []


def get_issues(bb_url, offset, bb_auth):
    """Fetch the issues from Bitbucket."""

    next_url = bb_url

    params = {"sort": "id"}
    if offset:
        params['q'] = "id > {}".format(offset)

    while next_url is not None:
        respo = requests.get(
            next_url, auth=bb_auth,
            params=params
        )
        if respo.status_code == 200:
            result = respo.json()
            # check to see if there are issues to process, if not break out.
            if result['size'] == 0:
                break

            print(
                "Retrieving issues in batches of {}, total number "
                "of issues {}, receiving {} to {}".format(
                    result['pagelen'], result['size'],
                    (result['page'] - 1) * result['pagelen'] + 1,
                    result['page'] * result['pagelen'],
                ))
            # https://developer.atlassian.com/bitbucket/api/2/reference/meta/pagination
            next_url = result.get('next', None)

            for issue in result['values']:
                yield issue

        else:
            raise RuntimeError(
                "Bitbucket returned an unexpected HTTP status code: {}"
                .format(respo.status_code)
            )


def get_issue_comments(issue_id, bb_url, bb_auth):
    """Fetch the comments for the specified Bitbucket issue."""
    next_url = "{bb_url}/{issue_id}/comments/".format(**locals())

    comments = []

    while next_url is not None:
        respo = requests.get(next_url, auth=bb_auth, params={"sort": "id"})
        if respo.status_code != 200:
            raise RuntimeError(
                "Failed to get issue comments from: {} due to unexpected HTTP "
                "status code: {}"
                .format(next_url, respo.status_code)
            )
        rec = respo.json()
        next_url = rec.get('next')
        comments.extend(rec['values'])
    return comments


def get_issue_changes(issue_id, bb_url, bb_auth):
    """Fetch the changes for the specified Bitbucket issue."""
    next_url = "{bb_url}/{issue_id}/changes/".format(**locals())

    changes = []

    while next_url is not None:
        respo = requests.get(next_url, auth=bb_auth, params={"sort": "id"})
        # unfortunately, BB's v 2.0 API seems to be 500'ing on some of these
        # but it does not seem to suggest the whole system isn't working
        if respo.status_code == 500:
            warnings.warn(
                "Failed to get issue changes from {} due to "
                "semi-expected HTTP status code: {}".format(
                    next_url, respo.status_code)
            )
            return []
        elif respo.status_code != 200:
            raise RuntimeError(
                "Failed to get issue changes from: {} due to unexpected HTTP "
                "status code: {}"
                .format(next_url, respo.status_code)
            )
        rec = respo.json()
        next_url = rec.get('next')
        changes.extend(rec['values'])
    return changes


def convert_issue(
        issue, comments, changes, options, attachment_links, gh_milestones,
        gh_labels, config):
    """
    Convert an issue schema from Bitbucket to GitHub's Issue Import API
    """
    # Bitbucket issues have an 'is_spam' field that Akismet sets true/false.
    # they still need to be imported so that issue IDs stay sync'd

    if isinstance(issue, DummyIssue):
        return dict(
            title="dummy issue",
            body="filler issue created by bitbucket_issue_migration",
            closed=True,
        )

    labels = {issue['priority']}

    for key in ['component', 'kind', 'version']:
        v = issue[key]
        if v is not None:
            if key == 'component':
                v = v['name']

    if issue['state'] in config['states_as_labels']:
        labels.add(issue['state'])

    labels = gh_labels.ensure(labels)

    is_closed = issue['state'] not in ('open', 'new')

    out = {
        'title': issue['title'],
        'body': format_issue_body(
            issue, attachment_links, options, config),
        'closed': is_closed,
        'created_at': convert_date(issue['created_on']),
        'updated_at': convert_date(issue['updated_on']),
        'labels': list(labels),
        ####
        # GitHub Import API supports assignee, but we can't use it because
        # our mapping of BB users to GH users isn't 100% accurate
        # 'assignee': "jonmagic",
    }

    if is_closed:
        closed_status = [
            convert_date(change['created_on'])
            for change in changes
            if 'state' in change['changes'] and
            change['changes']['state']['old'] in
            ('', 'open', 'new') and
            change['changes']['state']['new'] not in
            ('', 'open', 'new')
        ]
        if closed_status:
            out['closed_at'] = sorted(closed_status)[-1]
        else:
            out['closed_at'] = issue['updated_on']

    # If there's a milestone for the issue, convert it to a Github
    # milestone number (creating it if necessary).
    milestone = issue['milestone']
    if milestone and milestone['name']:
        out['milestone'] = gh_milestones.ensure(milestone['name'])

    return out


def convert_comment(comment, options, config):
    """
    Convert an issue comment from Bitbucket schema to GitHub's Issue Import API
    schema.
    """
    return {
        'created_at': convert_date(comment['created_on']),
        'body': format_comment_body(comment, options, config),
    }


def convert_change(change, options, config, gh_labels, is_last):
    """
    Convert an issue comment from Bitbucket schema to GitHub's Issue Import API
    schema.
    """
    body = format_change_body(change, options, config, gh_labels, is_last)
    if not body:
        return None
    return {
        'created_at': convert_date(change['created_on']),
        'body': body
    }


def format_issue_body(issue, attachment_links, options, config):
    content = issue['content']['raw']
    content = convert_changesets(content, options)
    content = convert_creole_braces(content)
    content = convert_links(content, options)
    content = convert_users(content, options)
    reporter = issue.get('reporter')

    if options.attachments_wiki and attachment_links:
        attachments = config['linked_attachments_template'].format(
            attachment_links=" | ".join(
                "[{}]({})".format(link['name'], link['link'])
                for link in attachment_links),
            sep=SEP
        )
    elif options.mention_attachments and attachment_links:
        attachments = config['names_only_attachments_template'].format(
            attachment_names=", ".join(
                "{}".format(link['name'])
                for link in attachment_links),
            sep=SEP
        )
    else:
        attachments = ''

    data = dict(
        # anonymous issues are missing 'reported_by' key
        reporter=format_user(reporter, options, config),
        sep=SEP,
        repo=options.bitbucket_repo,
        id=issue['id'],
        content=content,
        attachments=attachments
    )
    skip_user = reporter and reporter['username'] == options.bb_skip
    template = config['issue_template_skip_user'] \
        if skip_user else config['issue_template']
    return template.format(**data)


def format_comment_body(comment, options, config):
    content = comment['content']['raw']
    content = convert_changesets(content, options)
    content = convert_creole_braces(content)
    content = convert_links(content, options)
    content = convert_users(content, options)
    author = comment['user']
    data = dict(
        author=format_user(author, options, config),
        sep=SEP,
        content=content,
    )
    skip_user = author and author['username'] == options.bb_skip
    template = config['comment_template_skip_user'] if skip_user \
        else config['comment_template']
    return template.format(**data)


def format_change_body(change, options, config, gh_labels, is_last):
    author = change['user']

    # bb sneaked in an "assignee_account_id" that's not in their spec...
    include_changes = {
        "assignee", "state", "title", "kind", "milestone",
        "component", "priority", "version", "content"}
    added_labels = set()
    removed_labels = set()
    field_changes = set()
    status_changes = set()
    misc_changes = set()

    for change_element in change['changes']:
        if change_element not in include_changes:
            continue

        old = change['changes'][change_element]['old']
        new = change['changes'][change_element]['new']

        new_is_label = change_element in (
            'priority', 'component', 'kind', 'version') or (
            change_element == 'state' and new in config['states_as_labels']
        )

        old_is_label = change_element in (
            'priority', 'component', 'kind', 'version') or (
            change_element == 'state' and old in config['states_as_labels']
        )

        old = gh_labels.translate(old)
        new = gh_labels.translate(new)

        oldnewchange = [change_element, None, None]

        if old_is_label:
            if old:
                removed_labels.add(old)
        else:
            oldnewchange[1] = old

        if new_is_label:
            if new:
                added_labels.add(new)
        else:
            oldnewchange[2] = new

        if change_element == 'state':
            if new in ('open', 'new', 'on hold') and \
                    old in ('resolved', 'duplicate', 'wontfix', 'closed'):
                status_changes.add("reopened")

            if not is_last and old in ('open', 'new', 'on hold') and \
                    new in ('resolved', 'duplicate', 'wontfix', 'closed'):
                status_changes.add("closed")
        elif change_element == "content":
            misc_changes.add("edited description")
        elif not old_is_label or not new_is_label:
            field_changes.add(tuple(oldnewchange))

    def format_change_element(field, old, new):
        if old and new:
            return 'changed **{}** from "{}" to "{}"'.format(field, old, new)
        elif old:
            return 'removed **{}** (was: "{}")'.format(field, old)
        elif new:
            return 'set **{}** to "{}"'.format(field, new)
        else:
            assert False

    changes = []
    if removed_labels:
        changes.append(
            "* removed labels: {}".format(
                ", ".join("**{}**".format(label) for label in removed_labels)
            )
        )
    if added_labels:
        changes.append(
            "* added labels: {}".format(
                ", ".join("**{}**".format(label) for label in added_labels)
            )
        )
    if field_changes:
        for field, old, new in field_changes:
            changes.append(
                "* {}".format(format_change_element(field, old, new)))
    if status_changes:
        for verb in status_changes:
            changes.append(
                "* changed **status** to {}".format(verb)
            )
    if misc_changes:
        for misc in misc_changes:
            changes.append("* {}".format(misc))

    if not changes:
        return None

    data = dict(
        author=format_user(author, options, config),
        sep=SEP,
        changes="\n".join(changes)
    )
    template = config['change_template']
    return template.format(**data)


def _gh_username(username, users, gh_auth):
    try:
        return users[username]
    except KeyError:
        pass

    # Verify GH user link doesn't 404. Unfortunately can't use
    # https://github.com/<name> because it might be an organization
    gh_user_url = 'https://api.github.com/users/' + username
    status_code = requests.head(gh_user_url, auth=gh_auth).status_code
    if status_code == 200:
        users[username] = username
        return username
    elif status_code == 404:
        users[username] = None
        return None
    elif status_code == 403:
        raise RuntimeError(
            "GitHub returned HTTP Status Code 403 Forbidden when "
            "accessing: {}.  This may be due to rate limiting. "
            "You can read more about GitHub's API rate limiting "
            "policies here: https://developer.github.com/v3/#rate-limiting"
            .format(gh_user_url)
        )
    else:
        raise RuntimeError(
            "Failed to check GitHub User url: {} due to "
            "unexpected HTTP status code: {}"
            .format(gh_user_url, status_code)
        )


def format_user(user, options, config):
    """
    Format a Bitbucket user's info into a string containing either 'Anonymous'
    or their name and links to their Bitbucket and GitHub profiles.

    The GitHub profile link may be incorrect because it assumes they reused
    their Bitbucket username on GitHub.
    """
    # anonymous comments have null 'author_info', anonymous issues don't have
    # 'reported_by' key, so just be sure to pass in None
    if user is None:
        return "Anonymous"
    bb_user = config['bitbucket_username_template'].strip().format(
        **{"bb_user": user['username']})
    gh_username = _gh_username(
        user['username'], options.users, options.gh_auth)
    if gh_username is not None:
        gh_user = config['github_username_template'].strip().format(
            **{"gh_user": gh_username})
    else:
        gh_user = ""

    data = {
        "bb_username": user['username'],
        "gh_username": gh_username or "",
        "bb_user_badge": bb_user,
        "gh_user_badge": gh_user,
        "display_name": user['display_name']
    }
    return config['user_template'].strip().format(**data)


def convert_date(bb_date):
    """Convert the date from Bitbucket format to GitHub format."""
    # '2012-11-26T09:59:39+00:00'
    m = re.search(r'(\d\d\d\d-\d\d-\d\d)T(\d\d:\d\d:\d\d)', bb_date)
    if m:
        return '{}T{}Z'.format(m.group(1), m.group(2))

    raise RuntimeError("Could not parse date: {}".format(bb_date))


def convert_changesets(content, options):
    """
    Remove changeset references like:

        → <<cset 22f3981d50c8>>'

    Since they point to mercurial changesets and there's no easy way to map
    them to git hashes, better to remove them altogether.
    """
    if options.link_changesets:
        # Look for things that look like sha's. If they are short, they must
        # have a digit
        def replace_changeset(match):
            sha = match.group(1)
            if len(sha) >= 8 or re.search(r"[0-9]", sha):
                return (
                    ' [{sha} (bb)]'
                    '(https://bitbucket.org/{repo}/commits/{sha})'.format(
                        repo=options.bitbucket_repo, sha=sha,
                    )
                )
        content = re.sub(r" ([a-f0-9]{6,40})\b", replace_changeset, content)
    else:
        lines = content.splitlines()
        filtered_lines = [l for l in lines if not l.startswith("→ <<cset")]
        content = "\n".join(filtered_lines)
    return content


def convert_creole_braces(content):
    """
    Convert Creole code blocks to Markdown formatting.

    Convert text wrapped in "{{{" and "}}}" to "`" for inline code and
    four-space indentation for multi-line code blocks.
    """
    lines = []
    in_block = False
    for line in content.splitlines():
        if line.startswith("{{{") or line.startswith("}}}"):
            if "{{{" in line:
                _, _, after = line.partition("{{{")
                lines.append('    ' + after)
                in_block = True
            if "}}}" in line:
                before, _, _ = line.partition("}}}")
                lines.append('    ' + before)
                in_block = False
        else:
            if in_block:
                lines.append("    " + line)
            else:
                lines.append(line.replace("{{{", "`").replace("}}}", "`"))
    return "\n".join(lines)


def convert_links(content, options):
    """
    Convert absolute links to other issues related to this repository to
    relative links ("#<id>").
    """
    pattern = r'https://bitbucket.org/{repo}/issue/(\d+)'.format(
        repo=options.bitbucket_repo)
    return re.sub(pattern, r'#\1', content)


MENTION_RE = re.compile(r'(?:^|(?<=[^\w]))@[a-zA-Z0-9_-]+\b')


def convert_users(content, options):
    """
    Replace @mentions with users specified on the cli.
    """
    def replace_user(match):
        matched = match.group()[1:]
        return '@' + (options.users.get(matched) or matched)

    return MENTION_RE.sub(replace_user, content)


class AttachmentsRepo:
    def __init__(self, repo, options):

        self.git_url = "ssh://git@github.com/{}.wiki.git".format(repo)
        self.dest = tempfile.mkdtemp()

        if options.git_ssh_identity:
            os.environ['GIT_SSH_COMMAND'] = 'ssh -i {}'.format(
                options.git_ssh_identity)

        print("Cloning {} into {}...".format(self.git_url, self.dest))
        with self._chdir_as(self.dest):
            self._run_cmd("git", "clone", self.git_url, "wiki_checkout")
            self.repo_path = os.path.join(self.dest, "wiki_checkout")
            if not os.path.exists(
                    os.path.join(
                        self.repo_path, "imported_issue_attachments")):
                os.makedirs(
                    os.path.join(self.repo_path, "imported_issue_attachments"))

    def add_attachment(self, issue_num, filename, content):
        with self._chdir_as(self.repo_path, "imported_issue_attachments"):
            if not os.path.exists(str(issue_num)):
                os.makedirs(str(issue_num))
            path = os.path.join(str(issue_num), filename)
            with open(path, "wb") as out_:
                out_.write(content)
            self._run_cmd("git", "add", path)
        return "../wiki/imported_issue_attachments/{}/{}".format(
            issue_num, filename
        )

    def commit(self, issue_num):
        with self._chdir_as(self.repo_path):
            self._run_cmd(
                "git", "commit", "-m",
                "Imported attachments for issue {}".format(issue_num))

    def push(self):
        with self._chdir_as(self.repo_path):
            self._run_cmd("git", "push")

    @contextlib.contextmanager
    def _chdir_as(self, *path_tokens):
        currdir = os.getcwd()
        path = os.path.join(*path_tokens)
        os.chdir(path)
        yield
        os.chdir(currdir)

    def _run_cmd(self, *args):
        subprocess.check_call(args)


class GithubMilestones:
    """
    This class handles creation of Github milestones for a given
    repository.

    When instantiated, it loads any milestones that exist for the
    respository. Calling ensure() will cause a milestone with
    a given title to be created if it doesn't already exist. The
    Github number for the milestone is returned.
    """

    def __init__(self, repo, auth, headers):
        self.url = 'https://api.github.com/repos/{repo}/milestones'.\
            format(repo=repo)
        self.session = requests.Session()
        self.session.auth = auth
        self.session.headers.update(headers)
        self.refresh()

    def refresh(self):
        self.title_to_number = self.load()

    def load(self):
        milestones = {}
        url = self.url + "?state=all"
        while url:
            respo = self.session.get(url)
            if respo.status_code != 200:
                raise RuntimeError(
                    "Failed to get milestones due to HTTP status code: {}".
                    format(respo.status_code))
            for m in respo.json():
                milestones[m['title']] = m['number']
            if "next" in respo.links:
                url = respo.links['next']['url']
            else:
                url = None
        return milestones

    def ensure(self, title):
        number = self.title_to_number.get(title)
        if number is None:
            number = self.create(title)
            self.title_to_number[title] = number
        return number

    def create(self, title):
        respo = self.session.post(self.url, json={"title": title})
        if respo.status_code != 201:
            raise RuntimeError(
                "Failed to get milestones due to HTTP status code: {}".
                format(respo.status_code))
        return respo.json()["number"]


class GithubLabels:
    def __init__(self, label_translations, repo, auth, headers):
        self.label_translations = label_translations
        self.url = 'https://api.github.com/repos/{repo}/labels'.format(
            repo=repo)
        self.session = requests.Session()
        self.session.auth = auth
        self.session.headers.update(headers)
        self.refresh()

    def refresh(self):
        self.labels = set()
        url = self.url + "?state=all"
        while url:
            respo = self.session.get(url)
            if respo.status_code != 200:
                raise RuntimeError(
                    "Failed to get labels due to HTTP status code: {}".
                    format(respo.status_code))
            for m in respo.json():
                self.labels.add(m['name'])
            if "next" in respo.links:
                url = respo.links['next']['url']
            else:
                url = None

    def translate(self, label):
        label = self.label_translations.get(label, label)
        if label in (None, '(none)', "None"):
            return None

        label = label.replace(",", '')[:50]
        return label

    def ensure(self, labels):
        labels = {self.translate(label) for label in labels}.difference([None])

        for label in labels.difference(self.labels):
            self.create(label)
            self.labels.add(label)
        return labels

    def create(self, name):
        respo = self.session.post(
            self.url, json={"name": name, "color": self._random_web_color()})
        if respo.status_code != 201:
            raise RuntimeError(
                "Failed to create label due to HTTP status code: {}".
                format(respo.status_code))

    def _random_web_color(self):
        r, g, b = [random.randint(0, 15) * 16 for i in range(3)]
        return ('%02X%02X%02X' % (r, g, b))


def push_github_issue(issue, comments, github_repo, auth, headers):
    """
    Push a single issue to GitHub.

    Importing via GitHub's normal Issue API quickly triggers anti-abuse rate
    limits. So we use their dedicated Issue Import API instead:
    https://gist.github.com/jonmagic/5282384165e0f86ef105
    https://github.com/nicoddemus/bitbucket_issue_migration/issues/1
    """
    issue_data = {'issue': issue, 'comments': comments}
    url = 'https://api.github.com/repos/{repo}/import/issues'.format(
        repo=github_repo)
    respo = requests.post(url, json=issue_data, auth=auth, headers=headers)
    if respo.status_code == 202:
        return respo
    elif respo.status_code == 422:
        raise RuntimeError(
            "Initial import validation failed for issue '{}' due to the "
            "following errors:\n{}".format(issue['title'], respo.json())
        )
    else:
        raise RuntimeError(
            "Failed to POST issue: '{}' due to unexpected HTTP status code: {}"
            .format(issue['title'], respo.status_code)
        )


def verify_github_issue_import_finished(status_url, auth, headers):
    """
    Check the status of a GitHub issue import.

    If the status is 'pending', it sleeps, then rechecks until the status is
    either 'imported' or 'failed'.
    """
    while True:  # keep checking until status is something other than 'pending'
        respo = requests.get(status_url, auth=auth, headers=headers)
        if respo.status_code in (403, 404):
            print(respo.status_code, "retrieving status URL", status_url)
            respo.status_code == 404 and print(
                "GitHub sometimes inexplicably returns a 404 for the "
                "check url for a single issue even when the issue "
                "imports successfully. For details, see #77."
            )
            pprint.pprint(respo.headers)
            return
        if respo.status_code != 200:
            raise RuntimeError(
                "Failed to check GitHub issue import status url: {} due to "
                "unexpected HTTP status code: {}"
                .format(status_url, respo.status_code)
            )
        status = respo.json()['status']
        if status != 'pending':
            break
        time.sleep(.5)
    if status == 'imported':
        print("Imported Issue:", respo.json()['issue_url'])
    elif status == 'failed':
        raise RuntimeError(
            "Failed to import GitHub issue due to the following errors:\n{}"
            .format(respo.json())
        )
    else:
        raise RuntimeError(
            "Status check for GitHub issue import returned unexpected status: "
            "'{}'"
            .format(status)
        )
    return respo


if __name__ == "__main__":
    options = read_arguments()
    sys.exit(main(options))
