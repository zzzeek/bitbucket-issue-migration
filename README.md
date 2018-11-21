# Bitbucket Issues Migration

This is an application that migrates Bitbucket issues to a GitHub project.
In theory it also provides the basis for migrating Bitbucket to any sort of
label-oriented issue tracker, e.g. Gogs, Gitea, however currently the only
target is GitHub.

This is a fork by Mike Bayer which highly modifies the project at
https://github.com/jeffwidman/bitbucket-issue-migration to include a lot
more features.    Pull requests have been made back to the upstream project
however the architecture here has changed significantly.

The source of issues is the Bitbucket 2.0 API, or preferably, the zipfile
that you get from exporting your issues from your Bitbucket project, which
is a lot more reliable and accurate, not to mention very fast.

The destination is GitHubs not-official not-publicized not-really-maintained-but-
barely-good-enough issue import API illustrated at this one Gist, and nowhere
else:  https://gist.github.com/jonmagic/5282384165e0f86ef105.    Given that
GitHub seems to have a very tenuous committment to having a real issue import
API, it is not known if this API will remain available or be changed or what.

## Usage:

Here's how I'm importing issues into a test GitHub repo from a SQLAlchemy
export:

    bbmigrate --use-config mikes_config.yml \
      /home/classic/sqla_bb_issue_export.d3.zip \
      sqlalchemy-bot/test_sqlalchemy sqlalchemy-bot \
      --mention-changes \
      --git-ssh-identity /home/classic/.ssh/sqlalchemy_bot_rsa \
      --attachments-wiki

Users of the original Bitbucket migration script will note this looks completely
different.

The configuration allows one to customize how issues, comments, attachment
messages, etc. are formatted, as well as a translation map of Bitbucket
"label" names to GitHub labels.   For example, Bitbucket forces every
issue to have a "priority" which defaults to "major".  It's kind of tedious
then to have thousands of issues that all say "major" on them, so the
mapping allows you to translate "major" to nothing.    It also allows you to
correct for strange Bitbucket decisions like "priority=trivial" vs.
"priority=blocker", one is about how difficult the issue is and the other is
about how important it is, so I map "priority=trivial" to the label "easy".

The script works around GitHub's egregious and admitted lack of any way of
automating the attachment of files to issues by adding the attachments
to your project's wiki.   This feature is enabled by adding --attachments-wiki
to the command line, and then making sure your GitHub project has a wiki
enabled and created.   It pulls down the wiki via git and pushes files back
up to the project, which are then linked from the issues.   The links themselves
are relative links so that the name of the repository isn't hardcoded in them.

For authentication, you're going to want to use the "keyring" application
which is installed by the requirements here.   Add your GitHub password to
it like this:

    /path/to/virtualenv/bin/keyring set GitHub <your username>

It will ask for your password, which it then stores in some kind of we would
assume non-plaintext way (or who knows).   The script here then uses that
keyring to set up an authenticated session with GitHub.  You definitely need
to do this because GitHub has very low rate limits if you are not logged in.

After the script uses your login to have an authenticated session, it's doing
all the best practices of looking at GitHub's rate limit headers in the
requests and adjusting how many API calls it makes per second.   The speed of
import is already limited by the  fact that we need to push one issue at a
time, so that they come out numerically in the same order as those of your
Bitbucket issue tracker.

As many other things as possible "just work", such as, milestones being
transferred, usernames are looked up in GitHub to provide a link, comments
and issue content are rewritten as best as possible to follow GitHubs formatting
and issue linking conventions.

The script also **is** idempotent - if it crashes, due to hitting the rate limit
(which it shouldn't) or because of some other issue accessing APIs, the first
thing it does when you run it again is it looks up the highest issue number
in the GitHub repo and starts there again.

When importing issues, you will want the repo to have the git source of
your application already available, as it seems that GitHub's hyperlinking
of changesets doesn't occur after the fact (or at least it didn't seem to).


This is a personal fork of the original tool, developed by
[Mike Bayer](https://github.com/zzzeek).

The "public" version of the application is maintained by [Jeff Widman](http://www.jeffwidman.com/).
Originally written and open-sourced by [Vitaly Babiy](https://github.com/vbabiy).
