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

import requests
import warnings
import time

from .base import Client
from .base import keyring


class Bitbucket(Client):
    def __init__(self, config, options):
        self.config = config
        self.options = options
        self._login()
        self.auth = options.bb_auth

    def _login(self):
        options = self.options
        self.url = bb_url = (
            "https://api.bitbucket.org/2.0/repositories/{repo}/issues".
            format(repo=options.bitbucket_repo)
        )
        options.bb_auth = None
        options.users = dict(user.split('=') for user in options._map_users)

        bb_repo_status = requests.head(bb_url).status_code
        if bb_repo_status == 404:
            raise RuntimeError(
                "Could not find a Bitbucket Issue Tracker at: {}\n"
                "Hint: the Bitbucket repository name is case-sensitive."
                .format(bb_url)
            )
        # Only need BB auth creds for private BB repos
        elif bb_repo_status == 403:
            if not options.bitbucket_username:
                raise RuntimeError(
                    """
                    Trying to access a private Bitbucket repository, but no
                    Bitbucket username was entered. Please rerun the script
                    using the argument `--bb-user <username>` to pass in your
                    Bitbucket username.
                    """
                )
            kr_pass_bb = keyring.get_password(
                'Bitbucket', options.bitbucket_username)
            bitbucket_password = kr_pass_bb or getpass.getpass(
                "Please enter your Bitbucket password.\n"
                "Note: If your Bitbucket account has two-factor "
                "authentication enabled, you must temporarily disable it "
                "until https://bitbucket.org/site/master/issues/11774/ is "
                "resolved.\n"
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

    def get_issues(self, offset):
        """Fetch the issues from Bitbucket."""

        next_url = self.url

        params = {"sort": "id"}
        if offset:
            params['q'] = "id > {}".format(offset)

        while next_url is not None:
            respo = self._expect_200(
                requests.get(next_url, auth=self.auth, params=params),
                next_url
            )
            result = respo.json()

            if result['size'] == 0:
                break

            print(
                "Retrieving issues in batches of {}, total number "
                "of issues {}, receiving {} to {}".format(
                    result['pagelen'], result['size'],
                    (result['page'] - 1) * result['pagelen'] + 1,
                    result['page'] * result['pagelen'],
                ))

            next_url = result.get('next', None)

            for issue in result['values']:
                yield issue

    def get_issue_comments(self, issue_id):
        """Fetch the comments for the specified Bitbucket issue."""

        next_url = "{bb_url}/{issue_id}/comments/".format(
            bb_url=self.url,
            issue_id=issue_id
        )

        comments = []

        while next_url is not None:
            respo = self._expect_200(
                requests.get(next_url, auth=self.auth, params={"sort": "id"}),
                next_url
            )
            rec = respo.json()
            next_url = rec.get('next')
            comments.extend(rec['values'])
        return comments

    def get_issue_changes(self, issue_id):
        """Fetch the changes for the specified Bitbucket issue."""

        next_url = "{bb_url}/{issue_id}/changes/".format(
            bb_url=self.url,
            issue_id=issue_id
        )

        changes = []

        while next_url is not None:
            respo = self._expect_200(
                requests.get(next_url, auth=self.auth, params={"sort": "id"}),
                next_url, warn=(500,)
            )
            # unfortunately, BB's v 2.0 API seems to be 500'ing on some of
            # these but it does not seem to suggest the whole system isn't
            # working
            if respo.status_code == 500:
                warnings.warn(
                    "Failed to get issue changes from {} due to "
                    "semi-expected HTTP status code: {}".format(
                        next_url, respo.status_code)
                )
                return []
            rec = respo.json()
            next_url = rec.get('next')
            changes.extend(rec['values'])
        return changes

    def get_attachments(self, issue_num):
        url = "{}/{}/attachments".format(self.url, issue_num)
        respo = self._expect_200(
            requests.get(url, auth=self.auth), url
        )
        result = respo.json()
        return result['values']

    def get_attachment(self, issue_num, filename):
        # this seems to be in val['links']['self']['href'][0] also
        content_url = "{}/{}/attachments/{}".format(
            self.url, issue_num, filename)
        for retry in range(5):
            content = self._expect_200(
                requests.get(content_url, auth=self.auth),
                content_url, warn=(403,)
            )
            if content.status_code == 403:
                warnings.warn(
                    "Got a 403 from %s, waiting a few seconds then "
                    "trying again" % content_url)
                time.sleep(5)
                continue
            else:
                break
        else:
            return "Couldn't download attachment: %s" % content_url

        return content.content
