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

try:
    import keyring
    assert keyring.get_keyring().priority
except (ImportError, AssertionError):
    # no suitable keyring is available, so mock the interface
    # to simulate no pw
    class keyring:
        get_password = staticmethod(lambda system, username: None)


class Client:
    def _expect_200(self, response, url, warn=None):
        if response.status_code != 200:
            if warn and response.status_code in warn:
                return response
            raise RuntimeError(
                "Failed to call API URL {} due to unexpected HTTP "
                "status code: {}"
                .format(url, response.status_code)
            )
        return response


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
