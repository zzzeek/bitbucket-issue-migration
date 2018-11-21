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

import re
import requests

from . import base

SEP = "-" * 40


def process_wiki_attachments(
        issue_num, bitbucket, options, attachments_repo):
    attachment_links = []

    bb_attachments = bitbucket.get_attachments(issue_num)

    for val in bb_attachments:
        filename = val['name']
        content = bitbucket.get_attachment(issue_num, filename)

        link = attachments_repo.add_attachment(
            issue_num, filename, content)
        attachment_links.append(
            {
                "name": filename,
                "link": link
            }
        )
    if bb_attachments:
        if not options.dry_run:
            attachments_repo.commit(issue_num)
            attachments_repo.push()

    return attachment_links


def get_attachment_names(issue_num, bitbucket):
    """Get the names of attachments on this issue."""

    bb_attachments = bitbucket.get_attachments()
    return [{"name": val['name'], "link": None} for val in bb_attachments]


def convert_issue(
        issue, comments, changes, options, attachment_links, gh, config):
    """
    Convert an issue schema from Bitbucket to GitHub's Issue Import API
    """
    # Bitbucket issues have an 'is_spam' field that Akismet sets true/false.
    # they still need to be imported so that issue IDs stay sync'd

    if isinstance(issue, base.DummyIssue):
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
            labels.add(v)

    if issue['state'] in config['states_as_labels']:
        labels.add(issue['state'])

    labels = gh.ensure_labels(labels)

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
        out['milestone'] = gh.ensure_milestone(milestone['name'])

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


def convert_change(change, options, config, gh):
    """
    Convert an issue comment from Bitbucket schema to GitHub's Issue Import API
    schema.
    """
    body = format_change_body(change, options, config, gh)
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


def format_change_body(change, options, config, gh):
    author = change['user']

    # bb sneaked in an "assignee_account_id" that's not in their spec...
    # "attachment" is from our zipfile export, not sure if this is in
    # BB api 2.0
    include_changes = {
        "assignee", "state", "title", "kind", "milestone",
        "component", "priority", "version", "content", "attachment"}
    added_labels = set()
    removed_labels = set()
    field_changes = set()
    status_changes = set()
    misc_changes = set()

    for change_element in change['changes']:
        if change_element not in include_changes:
            continue

        if change_element == "attachment":
            misc_changes.add(
                "attached file {}".format(
                    change['changes'][change_element]['new']
                )
            )
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

        old = gh.translate_label(old)
        new = gh.translate_label(new)

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
            if new in ('', 'open', 'new', 'on hold') and \
                    old in ('resolved', 'duplicate', 'wontfix', 'closed'):
                status_changes.add("reopened")

            if old in ('', 'open', 'new', 'on hold') and \
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
    fix up changeset symbols
    """

    changeset_re = re.compile(r"<<(?:cset|changeset) (.+?)>>")

    return changeset_re.sub(
        lambda m: m.group(1), content
    )


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


def _zzzeeks_specific_milestone_fixer(gh, gh_issue, gh_comments):
    """undo a mistake I made in bitbucket years ago, where I removed the
    milestones from thousands of issues.  Read it in from the comment that
    says "removed milestone XYZ" and put it back.

    This function has nothing to do with what anyone would want this tool
    to do, unless it was generically customizable.

    """

    # NOTE: an assumption here is that the "change" comment will follow
    # after the "comment" comment that matches the regexp

    removed_milestone = also_remove = None
    new_comments = []
    for comment in gh_comments:
        match = re.match(
            r".*Removing milestone: (.+?) \(automated comment\)",
            comment['body'], re.S)
        if match:
            removed_milestone = match.group(1)
            also_remove = 'removed **milestone** (was: "{}")'.format(
                removed_milestone)
        else:
            if also_remove is None or also_remove not in comment['body']:
                new_comments.append(comment)

    if removed_milestone:
        gh_issue['milestone'] = gh.ensure_milestone(removed_milestone)

        gh_comments[:] = new_comments
