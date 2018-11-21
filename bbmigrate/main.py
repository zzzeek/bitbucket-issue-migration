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
import queue
import time
import threading

import yaml

from . import base
from . import convert
from .bitbucket import Bitbucket
from .github import AttachmentsRepo
from .github import GitHub


def _read_arguments(argv):
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
    return parser.parse_args(argv)


def main(argv=None):
    """Main entry point for the script."""

    options = _read_arguments(argv)

    with open(options.use_config, "r") as file_:
        config = yaml.load(file_)

    bb = Bitbucket(config, options)

    gh = GitHub(config, options)

    if options.attachments_wiki:
        if options.mention_attachments:
            raise TypeError(
                "Options --mention-attachments and --attachments-wiki are "
                "mutually exclusive")
        attachments_repo = AttachmentsRepo(options.github_repo, options)

    print("getting issues from bitbucket")
    issues_iterator = base.fill_gaps(bb.get_issues(options.skip), options.skip)

    abort_event = threading.Event()

    work_queue = queue.Queue()
    worker_thread = threading.Thread(
        target=push_issues,
        args=(abort_event, work_queue, gh)
    )
    worker_thread.daemon = True
    worker_thread.start()

    for index, issue in enumerate(issues_iterator):
        if abort_event.is_set():
            break

        if isinstance(issue, base.DummyIssue):
            comments = []
            changes = []
        else:
            comments = bb.get_issue_comments(issue['id'])
            changes = bb.get_issue_changes(issue['id'])

        if options.attachments_wiki:
            attachment_links = convert.process_wiki_attachments(
                issue['id'], bb, options, attachments_repo
            )
        elif options.mention_attachments:
            attachment_links = convert.get_attachment_names(issue['id'], bb)
        else:
            attachment_links = []

        gh_issue = convert.convert_issue(
            issue, comments, changes,
            options, attachment_links, gh, config
        )
        gh_comments = [
            convert.convert_comment(c, options, config) for c in comments
            if c['content']['raw'] is not None
        ]

        if options.mention_changes and changes:
            last_change = changes[-1]
            gh_comments += [
                converted_change for converted_change in
                [convert.convert_change(c, options, config, gh,
                 c is last_change) for c in changes]
                if converted_change
            ]

        print("Queuing bitbucket issue {} for export".format(issue['id']))
        work_queue.put((issue['id'], gh_issue, gh_comments))

    if not abort_event.is_set():
        work_queue.join()


def push_issues(abort, work_queue, gh):
    while not abort.is_set():
        try:
            issue_id, gh_issue, gh_comments = work_queue.get(timeout=3)
        except queue.Empty:
            if abort.is_set():
                break
            else:
                continue

        # keep one second between API requests per githubs rate limiting
        # advice
        time.sleep(.5)

        try:
            gh.push_github_issue(gh_issue, gh_comments, issue_id)
        except:
            abort.set()
            raise
        finally:
            work_queue.task_done()

