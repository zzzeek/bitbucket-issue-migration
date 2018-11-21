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

import contextlib
import getpass
import os
import pprint
import random
import requests
import subprocess
import tempfile
import time

from .base import Client
from .base import keyring


class GitHub(Client):
    # GitHub's Import API currently requires a special header
    headers = {'Accept': 'application/vnd.github.golden-comet-preview+json'}

    def __init__(self, config, options):
        self.config = config
        self.options = options
        self._login()
        self.repo = options.github_repo
        self._load_milestones()
        self._load_labels()

    def _login(self):
        options = self.options
        self.url = gh_repo_url = (
            'https://api.github.com/repos/{}'.format(options.github_repo)
        )

        # Always need the GH pass so format_user() can verify links to GitHub
        # user profiles don't 404. Auth'ing necessary to get higher GH rate
        # limits.
        kr_pass_gh = keyring.get_password('Github', options.github_username)
        github_password = kr_pass_gh or getpass.getpass(
            "Please enter your GitHub password.\n"
            "Note: If your GitHub account has authentication enabled, "
            "you must use a personal access token from "
            "https://github.com/settings/tokens in place of a password for "
            "this script.\n"
        )
        options.gh_auth = (options.github_username, github_password)
        # Verify GH creds work
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
            raise RuntimeError(
                "Could not find a GitHub repo at: " + gh_repo_url)
        self.session = requests.Session()
        self.session.auth = options.gh_auth
        self.session.headers.update(self.headers)

        response = self._expect_200(self.session.get(gh_repo_url), gh_repo_url)
        full_name = response.json()['full_name']
        if full_name != options.github_repo:
            raise Exception(
                "Repo name does not match that one "
                "sent: {} != {}.  Was this repo renamed?".
                format(options.github_repo, full_name))

    def _load_milestones(self):
        self.milestones = {}
        self._milestone_url = url = \
            'https://api.github.com/repos/{repo}/milestones?state=all'.\
            format(repo=self.repo)
        while url:
            respo = self._expect_200(self.session.get(url), url)
            for m in respo.json():
                self.milestones[m['title']] = m['number']
            if "next" in respo.links:
                url = respo.links['next']['url']
            else:
                url = None

    def _load_labels(self):
        self.labels = set()
        self.label_translations = self.config['label_translations']
        self._label_url = url = \
            'https://api.github.com/repos/{repo}/labels?state=all'.\
            format(repo=self.repo)
        while url:
            respo = self._expect_200(self.session.get(url), url)
            for m in respo.json():
                self.labels.add(m['name'])
            if "next" in respo.links:
                url = respo.links['next']['url']
            else:
                url = None

    def translate_label(self, label):
        label = self.label_translations.get(label, label)
        if label in (None, '(none)', "None"):
            return None

        label = label.replace(",", '')[:50]
        return label

    def ensure_labels(self, labels):
        labels = {
            self.translate_label(label) for label in labels}.difference([None])

        for label in labels.difference(self.labels):
            self._create_label(label)
            self.labels.add(label)
        return labels

    def _create_label(self, name):
        if self.options.dry_run:
            return

        respo = self.session.post(
            self._label_url,
            json={"name": name, "color": self._random_web_color()})
        if respo.status_code != 201:
            raise RuntimeError(
                "Failed to create label due to HTTP status code: {}".
                format(respo.status_code))

    def _random_web_color(self):
        r, g, b = [random.randint(0, 15) * 16 for i in range(3)]
        return ('%02X%02X%02X' % (r, g, b))

    def ensure_milestone(self, title):
        number = self.milestones.get(title)
        if number is None:
            number = self._create_milestone(title)
            self.milestones[title] = number
        return number

    def _create_milestone(self, title):
        if self.options.dry_run:
            return random.randint(1000000)

        respo = self.session.post(self._milestone_url, json={"title": title})
        if respo.status_code != 201:
            raise RuntimeError(
                "Failed to get milestones due to HTTP status code: {}".
                format(respo.status_code))
        return respo.json()["number"]

    def push_github_issue(self, issue, comments, verify_issue_id):
        """
        Push a single issue to GitHub.

        Importing via GitHub's normal Issue API quickly triggers anti-abuse
        rate limits. So we use their dedicated Issue Import API instead:
        https://gist.github.com/jonmagic/5282384165e0f86ef105
        https://github.com/nicoddemus/bitbucket_issue_migration/issues/1
        """

        if self.options.dry_run:
            print("\nIssue: ", issue)
            print("\nComments: ", comments)
            return

        issue_data = {'issue': issue, 'comments': comments}
        url = 'https://api.github.com/repos/{repo}/import/issues'.format(
            repo=self.repo)
        push_respo = self.session.post(url, json=issue_data)
        if push_respo.status_code == 422:
            raise RuntimeError(
                "Initial import validation failed for issue '{}' due to the "
                "following errors:\n{}".format(
                    issue['title'], push_respo.json())
            )
        elif push_respo.status_code != 202:
            raise RuntimeError(
                "Failed to POST issue: '{}' "
                "due to unexpected HTTP status code: {}"
                .format(issue['title'], push_respo.status_code)
            )

        # issue POSTed successfully, now verify the import finished before
        # continuing. Otherwise, we risk issue IDs not being sync'd between
        # Bitbucket and GitHub because GitHub processes the data in the
        # background, so IDs can be out of order if two issues are POSTed
        # and the latter finishes before the former. For example, if the
        # former had a bunch more comments to be processed.
        # https://github.com/jeffwidman/bitbucket-issue-migration/issues/45

        # TODO: how this should also work is when we first start out, we
        # *retrieve* the issues FROM github first to see what the highest
        # number is, then we make sure we don't overwrite.   The --offset
        # parameter shouldn't be needed.

        status_url = push_respo.json()['url']
        self._verify_github_issue_import_finished(verify_issue_id, status_url)

    def _verify_github_issue_import_finished(
            self, verify_issue_id, status_url):
        """
        Check the status of a GitHub issue import.

        If the status is 'pending', it sleeps, then rechecks until the status
        is either 'imported' or 'failed'.
        """
        while True:
            respo = self.session.get(status_url)
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
                    "Failed to check GitHub issue import status url: "
                    "{} due to unexpected HTTP status code: {}"
                    .format(status_url, respo.status_code)
                )
            status = respo.json()['status']
            if status != 'pending':
                break
            time.sleep(.5)
        if status == 'imported':
            # Verify GH & BB issue IDs match.
            # If this assertion fails, convert_links() will have incorrect
            # output.  This condition occurs when:
            # - the GH repository has pre-existing issues.
            # - the Bitbucket repository has gaps in the numbering.
            json = respo.json()
            gh_issue_url = json['issue_url']
            gh_issue_id = int(gh_issue_url.split('/')[-1])
            if gh_issue_id != verify_issue_id:
                raise Exception(
                    "Issues are out of sync, got github issue {} but "
                    "bitbucket issue is at {}".
                    format(gh_issue_id, verify_issue_id))
            print("Imported Issue:", json['issue_url'])
        elif status == 'failed':
            raise RuntimeError(
                "Failed to import GitHub issue due to the following "
                "errors:\n{}".format(respo.json())
            )
        else:
            raise RuntimeError(
                "Status check for GitHub issue import returned unexpected "
                "status: '{}'".format(status)
            )

class AttachmentsRepo:
    def __init__(self, repo, options):

        self.git_url = "ssh://git@github.com/{}.wiki.git".format(repo)
        self.dest = tempfile.mkdtemp()

        if options.git_ssh_identity:
            os.environ['GIT_SSH_COMMAND'] = (
                'ssh -o IdentitiesOnly=yes -i {}'.
                format(options.git_ssh_identity)
            )

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
