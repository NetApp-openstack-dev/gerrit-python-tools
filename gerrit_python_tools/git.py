"""
Simple, quick module that wraps subprocess around several common git cli
command. Should look into swapping out subprocess for one of the already
existing python/git libraries.

"""
import subprocess
import log

logger = log.get_logger()


class Ref(object):
    """
    Models a reference returned by git ls-remotes.
    Should have a hash and a name

    """
    def __init__(self, hash_, name):
        self.hash = hash_
        self.name = name

    def __str__(self):
        return "\t".join([self.hash, self.name])


def git_cmd(args):
    """
    Convenience method to bundle logged git commands with execution of said
    igt commands.

    @param args - List or String reprsenting command to send to subprocess

    """
    msg = " ". join(args)
    print("Issuing git command %s" % msg)
    logger.debug(msg)
    subprocess.check_call(args)


def listify(thing):
    """
    Convenience method to turn something into a list if it isn't
    already a list.

    @param thing - [List|Other] thing to turn into a list if not a list
    @returns List

    """
    if not isinstance(thing, list):
        return [thing]
    return thing


def init():
    """
    Equivalent to calling git init. Affects current working directory.

    """
    args = ['git', 'init']
    git_cmd(args)


def add_remote(name, url):
    """
    git remote add
    Adds a remote to the git repo that should be the current working
    directory.

    Equivalent to git remote add <name> <url>

    @param name = String name of the remote to add
    @param url = String url of the remote repo

    """
    args = ['git', 'remote', 'add', name, url]
    git_cmd(args)
    logger.debug("Added remote %s: %s" % (name, url))


def fetch(remote, refspecs):
    """
    git fetch
    Fetches a list respecs from the specified remote.

    Equivalent to:
        git fetch <remote> <refspec[0]> <refspec[1]> ... <refspec[n]>

    @param remote - String name of the remote
    @param refspecs - List of strings that are refspecs

    """
    refspecs = listify(refspecs)
    args = ['git', 'fetch', remote]
    args = args + refspecs
    git_cmd(args)


def checkout_branch(name, new=False):
    """
    git checkout
    Checks out a branch. Optionally creates a new branch

    Equivalent to:
        git checkout [-b] <name>

    @param name - String name of branch
    @param new - Boolean create a new branch

    """
    args = ['git', 'checkout', name]
    if new:
        args.insert(2, '-b')
    git_cmd(args)


def set_config(name, value):
    """
    git config
    Sets a git configuration key value pair for the current directory repo

    Equivalent to:
        git config <name> <value>

    @param name - String name of value to set
    @param value - String value

    """
    args = ['git', 'config', name, value]
    git_cmd(args)


def add(things):
    """
    git add
    Adds multiple things to staging

    Equivalent to
        git add <things[0]> <things[1]> ... <things[2]>

    @param things - List of paths to add

    """
    things = listify(things)
    if isinstance(things, str):
        things = listify(things)
    args = ['git', 'add'] + things
    git_cmd(args)


def commit(message=''):
    """
    git commit
    Commits the staged changes on the current repo

    Equivalent to:
        git commit -m message

    """
    args = ['git', 'commit', '-m', message]
    git_cmd(args)


def push(remote, all_=False, tags=False, force=False, refspecs=None):
    """
    git push

    Equivalent to:
        git push <remote> [--all] [--tags] \
        [<refspecs[0]> <refspecs[1]> ... <refspecs[n]>]

    @param remote - String name of remote to push to
    @param all_ - Boolean push all HEAD branches
    @param tags - Boolean push all tags
    @param refspecs - List of refspecs to push

    """
    args = ['git', 'push', remote]
    if all_:
        args.append('--all')
    if tags:
        args.append('--tags')
    if force:
        args.append('--force')
    if refspecs:
        refspecs = listify(refspecs)
        args = args + refspecs
    git_cmd(args)


def clone(source, name=None, bare=False):
    """
    git clone
    Clones a repo

    Equivalent to:
        git clone [--bare] <source> [<name>]

    @param source - Url to source repo
    @param name - String name of directory to clone into
    @param bare - Boolean clone with the --bare option

    """
    args = ['git', 'clone', source]
    if name:
        args.append(name)
    if bare:
        args.insert(2, '--bare')
    git_cmd(args)


def remote_refs(remote, heads=False, tags=False):
    """
    git ls-remote
    Parses the output of git ls-remote

    Equivalent to
        git ls-remote [--heads] [--tags] <remote>

    @param remote - String remote name
    @param heads - Boolean look at all heads
    @param tags - Boolean look at all tags
    @returns - Set of all refs

    """
    args = ['git', 'ls-remote', remote]
    if heads:
        args.insert(2, '--heads')
    if tags:
        args.insert(2, '--tags')
    cmd = subprocess.Popen(args, stdout=subprocess.PIPE)
    s = lambda line: line.rstrip().split("\t")[1]
    return set(map(s, cmd.stdout))
