import git
import hashlib
import json
import log
import logging
import os
import paramiko
import pipes
import pprint
import Queue
import re
import shutil
import StringIO
import subprocess
import time
import utils
from thread import StoppableThread
from uuid import uuid4
from pipes import quote

# Turn down the logging output of paramiko
log.get_logger('paramiko').setLevel(logging.ERROR)

# Get a logger
logger = log.get_logger()


class SSHStream(StoppableThread):
    """
    Very similar to the gerrit stream at
    https://github.com/atdt/gerrit-stream
    Should connect to a gerrit event stream and add any events
    to the queue.

    """
    def __init__(self, host, port, timeout, username, key_filename, keepalive):
        """
        Class constructor. Cleans numbers and starts a queue.

        """
        super(SSHStream, self).__init__()
        self._queue = Queue.Queue()

        self._ssh_kwargs = {
            'username': username,
            'port': int(port),
            'timeout': int(timeout)
        }

        if key_filename:
            self._ssh_kwargs['key_filename'] = key_filename

        self._host = host
        self._keepalive = int(keepalive)

    def get_event(self):
        """
        Returns an event or None if nothing is in the queue

        @returns - JSON loaded object

        """
        try:
            json_ = self._queue.get_nowait()
            event = json.loads(json_)
            logger.debug("Received event:\n%s" % pprint.pformat(event))
            return event
        except ValueError:
            logger.error("Error loading json:\n%s" % json_)
        except Queue.Empty:
            logger.debug("Nothing in event queue.")
        return None

    def run(self):
        """
        Run method of the thread contains two loops.
        The outer loop reconnects to gerrit after a period of time
            if an error occurs.
        The inner loop reads from the ssh connection.
        Both loops check to see if a stop is requested.

        """
        logger.info("Gerrit event stream started")

        # Outer loop - Manage ssh connection to gerrit
        while True:
            logger.info("Connecting...")
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            try:
                client.connect(self._host, **(self._ssh_kwargs))
                client.get_transport().set_keepalive(self._keepalive)
                _, stdout, _ = client.exec_command('gerrit stream-events')

                # Inner loop - Manage reading from stream
                while not stdout.channel.exit_status_ready():
                    if stdout.channel.recv_ready():
                        self._queue.put(stdout.readline())

                    # Check for break
                    if self._stop.isSet():
                        break

            except Exception:
                logger.exception("Error listening to gerrit event stream.")

            finally:
                client.close()

            if self._stop.isSet():
                logger.info("Event stream stop requested.")
                break

            # Wait 5 seconds before reconnecting. @TODO - Make configurable.
            logger.info("Waiting %s seconds before reconnecting" % 5)
            time.sleep(5)


class SSH(object):
    """
    Class for connecting to a gerrit service via ssh and paramiko.

    """
    def __init__(self, host, port, timeout, username, key_filename):
        """
        Inits the SSH object.

        @param host - String Location of gerrit service
        @param port - String Port of gerrit service (usually 29418)
        @param timeout - Integer Timeout in seconds
        @param username - String username
        @param key_filename - String or None

        """
        self._ssh_kwargs = {
            'username': username,
            'port': int(port),
            'timeout': int(timeout)
        }

        if key_filename:
            self._ssh_kwargs['key_filename'] = key_filename

        self._host = host

    def exec_once(self, cmd):
        """
        Executes a command once

        @param cmd - String command to execute.
        @return Two tuple comprised of the return code and stdout if
            the return code is 0. Returns the stderr if the retcode
            is non zero

        """
        logger.debug("Executing: %s" % cmd)
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        client.connect(self._host, **(self._ssh_kwargs))
        _, stdout, stderr = client.exec_command(cmd)

        retcode = stdout.channel.recv_exit_status()
        stream = stdout if not retcode else stderr
        output = stream.read()
        client.close()
        logger.debug(output)
        return retcode, output


class Remote(object):
    """
    Models a gerrit remote tracking connection info and providing
    methods to create SSH and Event streams

    """
    def __init__(self, _config):
        """
        Inits the remote.

        @param _config - Dictionary containing keys for host, port, timeout,
            username, key_filename, and keepalive

        """
        self.host = _config['host']
        self.port = _config['port']
        self.timeout = _config['timeout']
        self.username = _config['username']
        self.key_filename = _config['key_filename']
        self.keepalive = _config['keepalive']

    def SSHStream(self):
        """
        Returns a gerrit.SSHStream object

        @returns - gerrit.SSHStream

        """
        return SSHStream(
            self.host,
            self.port,
            self.timeout,
            self.username,
            self.key_filename,
            self.keepalive
        )

    def SSH(self):
        """
        Returns a gerrit.SSH object

        @returns - gerrit.SSH
        """
        return SSH(
            self.host,
            self.port,
            self.timeout,
            self.username,
            self.key_filename
        )


class Approval(object):
    """
    Models a gerrit approval.
    Should have fields for type, description, value, granted on and
    granted by

    """
    def __init__(self, data):
        """
        Sets the data for the approval.

        @param data - Dictionary containing approval data

        """
        self._data = data

    @property
    def name(self):
        """
        Returns the 'type' field of the approval dictionary

        @returns string|None

        """
        return self._data.get('type')

    @property
    def value(self):
        """
        Returns the integer value of the approval(+1, +2) etc

        @returns Integer
        """
        return int(self._data.get('value'))


class Label(object):
    """
    Models a gerrit label

    """
    def __init__(self, name, min_, max_):
        """
        Inits the label. Add values and use the pass method to determine
        if this label passes.

        @param name - String label name(Verified, Code-Review, etc)
        @param min_ - Integer lowest possible score
        @param max_ - Integer highest possible score

        """
        self.name = name
        self._min = min_
        self._max = max_
        self._values = []

    def add_approval(self, approval):
        """
        Adds an approval/vote to the label

        @param approval - Approval

        """
        value = approval.value
        logger.debug("Adding value %s to label %s" % (value, self.name))
        self._values.append(value)

    def approved(self):
        """
        Emulates the max with block gerrit function.
        Must have the highest positive value.
        The lowest negative value blocks.
        No values block.

        @return Boolean

        """
        # Fail if no values
        if not self._values:
            return False

        # Fail if we have the lowest possible value
        if min(self._values) <= self._min:
            return False

        # Pass if we have the highest value
        if max(self._values) >= self._max:
            return True

        # Fail otherwise
        return False


class CommentAdded(object):
    """
    Models CommentAdded type gerrit events.
    Provides ability to send upstream if criteria is met.

    """
    def __init__(self, data, conf):
        """
        Inits the object.

        @param data - Dictionary
        @param conf - Dictionary
        """
        self._data = data
        self._conf = conf

    @property
    def comment(self):
        """
        Returns the actual comment. Should return string or empty string.

        @returns - String

        """
        return self._data.get('comment', '')

    @property
    def change_id(self):
        """
        Returns the change id

        @returns - String

        """
        return self._data['change'].get('id')

    @property
    def patchset_id(self):
        """
        Returns the patchset number - @TODO Rename to patchset number

        @returns - Integer

        """
        return int(self._data['patchSet'].get('number'))

    @property
    def project(self):
        """
        Returns the project name

        @returns - String

        """
        return self._data['change']['project']

    @property
    def branch(self):
        """
        Returns the branch name

        @returns - String

        """
        return self._data['change']['branch']

    @property
    def topic(self):
        """
        Returns the topic name

        @returns - String

        """
        return self._data['change']['topic']

    @property
    def revision(self):
        """
        Returns the revision number

        @returns - String

        """
        return self._data['patchSet']['revision']

    @property
    def change_owner_username(self):
        """
        Returns the gerrit username of the author.

        @returns - String | None

        """
        return self._data['change']['owner'].get('username')

    @property
    def change_owner_name(self):
        """
        Returns the full name of the author.

        @returns - String | None

        """
        return self._data['change']['owner'].get('name')

    @property
    def change_owner_email(self):
        """
        Returns the email address of the author

        @returns - String | None

        """
        return self._data['change']['owner'].get('email')

    def is_upstream_project(self):
        """
        Returns whether or not this project is an upstream project.
        An upstream project is a project that exists in conf and is also
        designated as an upstream project.

        @returns Boolean

        """
        project = None

        # Iterate over projects in config looking for one with a matching name
        for p in self._conf.get('projects', []):
            if p.get('name') == self.project:
                project = Project(p)
                break

        # If project not set, then project wasn't found
        if not project:
            logger.debug("Change %s: Project %s not in configuration."
                         % (self.change_id, self.project))
            return False

        # Check upstream designation
        if not project.upstream:
            logger.debug("Change %s: Project %s not designated as upstream."
                         % (self.change_id, self.project))
            return False

        # If the project has been found and if the project is marked as
        # upstream, then return True
        return True

    def is_upstream_indicated(self):
        """
        Breaks the comment up into lines. The first line is examined for
        'Upstream-Ready+1'

        @returns - Boolean True for 'Upstream-Ready+1' inside the comment,
            false otherwise.

        """
        trigger = self._conf['upstream']['trigger']
        logger.debug("Change %s: Trigger '%s'" % (self.change_id, trigger))
        first_line = self.comment.splitlines()[0]
        return trigger in first_line

    def is_upstream_approved(self, approvals):
        """
        Examines approvals on comment added. Must meet label criteria
        before being sent upstream.

        @param approvals - List of approvals
        @returns - Boolean

        """
        labels = get_labels_for_upstream(self._conf, self.project)

        for approval in approvals:
            label = labels.get(approval.name)
            if label:
                label.add_approval(approval)

        # Debugging
        for label in labels.values():
            if label.approved():
                str_ = "approved"
            else:
                str_ = "not approved"
            logger.debug("Change %s: Label %s is %s"
                         % (self.change_id, label.name, str_))

        return all([l.approved() for l in labels.values()])

    def get_approvals(self, ssh):
        """
        Returns a list of approvals or the empty list for the change

        @param ssh - gerrit.SSH object
        @returns - List of approvals
        """
        approvals = []
        cmd = ('gerrit query change:%s branch:%s project:%s'
               ' --all-approvals limit:1 --format JSON')
        cmd = cmd % (self.change_id, self.branch, self.project)
        try:
            retcode, out = ssh.exec_once(cmd)

            # Raise exception if nonzero retcode.
            # @TODO - Clean this exception handling up.
            if retcode:
                raise Exception("Change %s: Error getting approvals"
                                % self.change_id)

            # Grab the first json object. Query returns 1+
            json_ = utils.MultiJSON(out)[0]
            for patchset in json_['patchSets']:
                if int(patchset['number']) == self.patchset_id:
                    for json_approval in patchset['approvals']:
                        logger.debug(pprint.pformat(json_approval))
                        approvals.append(Approval(json_approval))

            logger.debug("Change %s: Approvals returned by gerrit query"
                         % self.change_id)
            logger.debug(pprint.pformat(json_))

        except:
            logger.exception("Change %s: Error getting approvals"
                             % self.change_id)

        # Return approvals or empy list
        return approvals

    def get_upstream_url(self, upstream):
        """
        Attempts to get the upstream url for this change.
        This change should have already been pushed upstream.

        @param upstream - gerrit.Remote
        @returns - None | String. Url for success, None otherwise

        """
        ssh = upstream.SSH()
        cmd = 'gerrit query change:%s branch:%s project:%s --format JSON'
        cmd = cmd % (self.change_id, self.branch, self.project)
        url = None
        try:
            retcode, out = ssh.exec_once(cmd)
            # Check retcode
            if retcode:
                return None

            if not retcode:
                json_ = utils.MultiJSON(out)
                # Check that a match was found. A stats object will always
                # be sent. need length > 1
                if len(json_) < 2:
                    return None
                url = json_[0]['url']
        except Exception:
            logger.exception("Exception getting upstream url")

        # Backup url
        if not url:
            url = ("https://%s/#q,%s,n,z" % (upstream.host, self.change_id))
        return url

    def send_upstream(self, downstream, upstream):
        """
        Sends the change indicated by the comment upstream.

        @param downstream - gerrit.Remote downstream object
        @param upstream - gerrit.Remote upstream object

        """
        # Check if upstream project before doing anything.
        if not self.is_upstream_project():
            return

        ssh = downstream.SSH()

        # Check to see if comment indicates a change is upstream ready
        if not self.is_upstream_indicated():
            logger.debug("Change %s: Upstream not indicated" % self.change_id)
            return

        # Grab all of the approvals
        approvals = self.get_approvals(downstream.SSH())

        # Check to see if comment has necessary approvals.
        if not self.is_upstream_approved(approvals):
            msg = ("Could not send to upstream: One or more labels"
                   " not approved.")
            logger.debug("Change %s: %s" % (self.change_id, msg))
            ssh.exec_once('gerrit review -m %s %s'
                          % (pipes.quote(msg), self.revision))
            return

        # Do some git stuffs to push upstream
        logger.debug("Change %s: Sending to upstream" % self.change_id)

        repo_dir = '~/tmp'
        repo_dir = os.path.expanduser(repo_dir)
        repo_dir = os.path.abspath(repo_dir)

        # Add uuid, want a unique directory here
        uuid_dir = str(uuid4())
        repo_dir = os.path.join(repo_dir, uuid_dir)

        # Make Empty directory - We want this to stop and fail on OSError
        if not os.path.isdir(repo_dir):
            os.makedirs(repo_dir)
            logger.debug(
                "Change %s: Created directory %s" % (self.change_id, repo_dir)
            )

        # Save the current working directory
        old_cwd = os.getcwd()

        try:
            # Change to newly created directory.
            os.chdir(repo_dir)

            # Init the cwd
            git.init()

            # Add the remotes for upstream and downstream
            remote_url = "ssh://%s@%s:%s/%s"
            git.add_remote('downstream', remote_url % (downstream.username,
                                                       downstream.host,
                                                       downstream.port,
                                                       self.project))

            # Figure out what user we will pose as
            # This every upstream user sharing the same key is kinda shady.
            # Default back to the configured user if username doesnt exist.
            # should fail in this case
            username = self.change_owner_username
            name = self.change_owner_name
            email = self.change_owner_email
            if not username:
                logger.debug("Change %s: Unable to use author credentials."
                             " Defaulting to configured credentials."
                             % self.change_id)
                username = upstream.username
                name = self._conf['git-config']['name']
                email = self._conf['git-config']['email']

            git.add_remote('upstream', remote_url % (username,
                                                     upstream.host,
                                                     upstream.port,
                                                     self.project))
            logger.debug('Change %s: Sending upstream as '
                         'username %s, email %s, name %s'
                         % (self.change_id, username, email, name))
            try:
                env = get_review_env()

                # Set committer info
                git.set_config('user.email', email)
                git.set_config('user.name', name)

                # Download  specific change to local
                args = ['git-review', '-r', 'downstream', '-d',
                        '%s,%s' % (self.change_id, self.patchset_id)]
                logger.debug('Change %s: running: %s'
                             % (self.change_id, ' '.join(args)))
                out = subprocess.check_output(args,
                                              stderr=subprocess.STDOUT,
                                              env=env)
                logger.debug("Change %s: %s" % (self.change_id, out))

                # Send downloaded change to upstream
                args = ['git-review', '-R', '-y', '-r', 'upstream',
                        self.branch, '-t', self.topic]
                logger.debug('Change %s: running: %s'
                             % (self.change_id, ' '.join(args)))
                out = subprocess.check_output(args,
                                              stderr=subprocess.STDOUT,
                                              env=env)
                logger.debug("Change %s: %s" % (self.change_id, out))

                upstream_url = self.get_upstream_url(upstream)

                msg = 'Sent to upstream: %s' % (upstream_url)
                # Send comment to downstream gerrit with link to change in
                # upstream gerrit
                ssh.exec_once('gerrit review -m %s %s'
                              % (pipes.quote(msg), self.revision))

            except subprocess.CalledProcessError as e:
                msg = "Could not send to upstream:\n%s" % e.output
                ssh.exec_once('gerrit review -m %s %s'
                              % (pipes.quote(msg), self.revision))
                logger.error("Change %s: Unable to send to upstream"
                             % self.change_id)
                logger.error("Change %s: %s" % (self.change_id, out))

            except Exception:
                msg = 'Could not send to upstream: Error running git-review'
                ssh.exec_once('gerrit review -m %s %s'
                              % (pipes.quote(msg), self.revision))
                logger.exception("Change %s: Unable to send to upstream"
                                 % self.change_id)

        finally:
            # Change to old current working directory
            os.chdir(old_cwd)

            # Attempt to clean up created directory
            shutil.rmtree(repo_dir)


class Group(object):
    """
    Class that provides some simple accessor methods to a dictionary
    representing a group in Gerrit. Also provides some methods to check
    the existence of a group or to create a new group.

    """
    def __init__(self, data):
        """
        Sets the data for the group and checks to ensure a name is present."

        @param data - Dictionary containing group data

        """
        if 'name' not in data:
            raise Exception('Groups must have a name.')
        self._data = data

    @property
    def description(self):
        """
        Returns the group description or None.

        @return String|None

        """
        return self._data.get('description', None)

    @property
    def name(self):
        """
        Returns the name or None.

        @return String|None

        """
        return self._data.get('name', None)

    @property
    def uuid(self):
        """
        Returns the uuid of this group or None.

        @returns String|None

        """
        return self._data.get('uuid', None)

    @property
    def owner(self):
        """
        Returns the owning group or None.

        @return String|None

        """
        return self._data.get('owner', None)

    @property
    def owner_uuid(self):
        """
        Returns the owning group's uuid or None

        @return String|None
        """
        return self._data.get('owner-uuid',  None)

    def get_ls(self):
        """
        Returns the gerrit command to get details of this group.

        @return String

        """
        return 'gerrit ls-groups -q %s --verbose' % quote(self.name)

    def get_create(self):
        """
        Returns the gerrit command to create this group.

        @return String

        """
        # Description is optional
        description = ''
        if self.description:
            description = ' --description %s' % quote(self.description)

        # Owning group is optional(will default to administrators)
        owner = ''
        if self.owner:
            owner = ' --owner %s' % quote(self.owner)

        # Name is not optional
        return 'gerrit create-group %s%s%s' % (
            quote(self.name),
            description,
            owner
        )

    def exists(self, ssh):
        """
        Executes a 'gerrit ls-groups -q <groupname> --verbose' command.

        @param ssh - SSH object
        @return Boolean True for exists, False for does not exist.

        """
        retcode, __ = ssh.exec_once(self.get_ls())
        return True if not retcode else False

    def present(self, remote):
        """
        Makes sure this group is present on gerrit. First checks to see
        if this group exists. If it does not exist already, then this method
        will attempt to create it.

        @param remote - gerrit.Remote object
        @return True if the group exists or was created, False otherwise.

        """
        msg = "Group %s: Ensuring present." % self.name
        logger.info(msg)
        print msg

        ssh = remote.SSH()

        # If the group already exists, do nothing.
        if self.exists(ssh):
            msg = "Group %s: Already exists." % self.name
            logger.info(msg)
            print msg
            return

        # Try to create the group
        retcode, __ = ssh.exec_once(self.get_create())
        if not retcode:
            msg = "Group %s: Created" % self.name
            logger.info(msg)
            print msg
        return True if not retcode else False


class User(object):
    """
    Class that models an internal user. Provides some simple accessor methods
    to access data of a dictionary that represents a Gerrit user.

    """

    def __init__(self, data):
        """
        Inits the model.

        @param data - Dictionary containing gerrit user data.

        """
        if 'username' not in data:
            raise Exception('Users must have a name.')
        self._data = data

    @property
    def username(self):
        """
        Returns the username or None

        @return String|None

        """
        return self._data.get('username', None)

    @property
    def ssh_key(self):
        """
        Returns the ssh key or None

        @returns String|None

        """
        return self._data.get('ssh-key', None)

    @property
    def groups(self):
        """
        Returns a list of groups or the empty list.

        @returns List

        """
        return self._data.get('groups', [])

    @property
    def full_name(self):
        """
        Returns the full name or None
k
        @returns String|None

        """
        return self._data.get('full-name', None)

    @property
    def email(self):
        """
        Returns the email or None

        @return String|None

        """
        return self._data.get('email', None)

    @property
    def http_password(self):
        """
        Returns the http password or None.

        @return String|None
        """
    def get_create(self):
        """
        Returns the gerrit command to create this account.

        @returns String

        """
        ssh_key = ''
        if self.ssh_key:
            ssh_key = ' --ssh-key %s' % quote(self.ssh_key)

        groups = ''
        if self.groups:
            groups = ['--group %s' % quote(g) for g in self.groups]
            groups = ' '.join(groups)
            groups = ' %s' % groups

        full_name = ''
        if self.full_name:
            full_name = ' --full-name %s' % quote(self.full_name)

        email = ''
        if self.email:
            email = ' --email %s' % quote(self.email)

        http_password = ''
        if self.http_password:
            http_password = ' --http-password %s' % quote(self.http_password)

        return "gerrit create-account %s%s%s%s%s %s" % (
            ssh_key,
            groups,
            full_name,
            email,
            http_password,
            quote(self.username)
        )

    def present(self, remote):
        """
        Attempts to create this user. If the return code of that operation
        is 0 then this method returns True. If the return code of that
        operation is 1 and 'already exists' is in the output, then this
        method returns true. Returns false otherwise.

        @param remote = gerrit.Remote object
        @returns Boolean True for created or already exists. False otherwise.

        """
        msg = "User %s: Ensuring present." % self.username
        logger.info(msg)
        print msg

        ssh = remote.SSH()

        retcode, out = ssh.exec_once(self.get_create())
        if not retcode:
            msg = "User %s: Created." % self.username
            logger.info(msg)
            print msg
            return True
        if retcode == 1 and 'already exists' in out:
            msg = "User %s: Already exists." % self.username
            logger.info(msg)
            print msg
            return True

        msg = "User %s: Unable to create - %s" % (self.username, out)
        logger.error(msg)
        print msg
        return False


class Project(object):
    """
    Models a gerrit project

    """

    def __init__(self, data):
        """
        Inits the project from a dictionary

        @param data - Dictionary describing the gerrit project
        """
        if 'name' not in data:
            raise Exception("Projects must have a name")
        self._data = data

    @property
    def name(self):
        """
        Returns the project's name

        @returns String

        """
        return self._data.get('name')

    @property
    def create(self):
        """
        Returns Boolean indicating whether or not to create
        the project.

        @return Boolean

        """
        return self._data.get('create', False)

    @property
    def config(self):
        """
        Returns the location of the project configuration
        file.

        @returns String|None

        """
        return self._data.get('config', None)

    @property
    def source(self):
        """
        Returns the source for this project if creating
        from another repo. Should be a git url.

        @returns String|None

        """
        return self._data.get('source', None)

    @property
    def preserve_prefix(self):
        """
        Returns the prefix for branches that should be preserved and
        not deleted.

        @returns String|None
        """
        return self._data.get('preserve_prefix', None)

    @property
    def heads(self):
        """
        Returns whether or not to include head branches.
        Default is to include heads.

        @returns Boolean - True for include heads, false otherwise

        """
        return self._data.get('heads', True)

    @property
    def tags(self):
        """
        Returns whether or not to include tags.
        Default is to exclude tags

        @returns Boolean - True for include tags, false otherwise

        """
        return self._data.get('tags', False)

    @property
    def force(self):
        """
        Returns whether or not to force commits. This will allow this tool
        to overwrite refs that are not ancestors of a branch from upstream.
        Default is to allow force commits.

        @returns Boolean - True for allow force commits, false otherwise

        """
        return self._data.get('force', True)

    @property
    def upstream(self):
        """
        Returns whether or not this project is designated as an upstream
        project.

        @returns Boolean

        """
        return self._data.get('upstream', False)

    def _create(self, ssh):
        """
        Attempts to create a project through gerrit ssh commands.

        @param ssh - gerrit.SSH object

        """
        if self.create:
            cmd = 'gerrit create-project %s' % quote(self.name)
            retcode, text = ssh.exec_once(cmd)

    def _config(self, remote, conf, groups):
        """
        Builds the groups file and project.config file for a project.

        @param remote - gerrit.Remote object
        @param conf - Dict containing git config information
        @param groups - List of groups

        """
        if not self.config:
            return

        msg = "Project %s: Configuring." % self.name
        logger.info(msg)
        print msg

        repo_dir = '~/tmp'
        repo_dir = os.path.expanduser(repo_dir)
        repo_dir = os.path.abspath(repo_dir)

        uuid_dir = str(uuid4())
        repo_dir = os.path.join(repo_dir, uuid_dir)

        # Make Empty directory - We want this to stop and fail on OSError
        logger.debug(
            "Project %s: Creating directory %s" % (self.name, repo_dir)
        )
        os.makedirs(repo_dir)

        # Save the current working directory
        old_cwd = os.getcwd()

        origin = 'origin'

        try:
            # Change cwd to that repo
            os.chdir(repo_dir)

            # Git init empty directory
            git.init()

            # Add remote origin
            ssh_url = 'ssh://%s@%s:%s/%s' % (
                remote.username,
                remote.host,
                remote.port,
                self.name
            )

            git.add_remote(origin, ssh_url)

            # Fetch refs/meta/config for project
            refspec = 'refs/meta/config:refs/remotes/origin/meta/config'
            git.fetch(origin, refspec)

            # Checkout refs/meta/config
            git.checkout_branch('meta/config')

            # Get md5 of existing config
            _file = os.path.join(repo_dir, 'project.config')
            contents = ''
            try:
                with open(_file, 'r') as f:
                    contents = f.read()
            except IOError:
                pass
            existing_md5 = hashlib.md5(contents).hexdigest()

            # Get md5 of new config
            with open(self.config, 'r') as f:
                contents = f.read()
            new_md5 = hashlib.md5(contents).hexdigest()

            msg = "Project %s: Md5 comparision\n%s\n%s"
            msg = msg % (self.name, existing_md5, new_md5)
            logger.debug(msg)
            print msg

            # Only alter if checksums do not match
            if existing_md5 != new_md5:

                logger.debug(
                    "Project %s: config md5's are different." % self.name
                )

                # Update project.config file
                _file = os.path.join(repo_dir, 'project.config')
                with open(_file, 'w') as f:
                    f.write(contents)

                # Update groups file
                group_contents = groups_file_contents(groups)
                _file = os.path.join(repo_dir, 'groups')
                with open(_file, 'w') as f:
                    f.write(group_contents)

                # Git config user.email
                git.set_config('user.email', conf['git-config']['email'])

                # Git config user.name
                git.set_config('user.name', conf['git-config']['name'])

                # Add groups and project.config
                git.add(['groups', 'project.config'])

                # Git commit
                git.commit(message='Setting up %s' % self.name)

                # Git push
                git.push(origin, refspecs='meta/config:refs/meta/config')
                logger.info("Project %s: pushed configuration." % self.name)

            else:
                msg = "Project %s: config unchanged." % self.name
                logger.info(msg)
                print msg

        finally:
            # Change to old current working directory
            os.chdir(old_cwd)

            # Attempt to clean up created directory
            shutil.rmtree(repo_dir)

    def ref_kwargs(self):
        """
        Returns dictionary of ref keyword arguments

        @returns - Dictionary

        """
        kwargs = {}
        if self.heads:
            kwargs['heads'] = True
        if self.tags:
            kwargs['tags'] = True
        return kwargs

    def _sync(self, remote):
        """
        Pushes all normal branches from a source repo to gerrit.

        @param remote - gerrit.Remote object

        """
        # Only sync if source repo is provided.
        if not self.source:
            return

        # Only sync if heads and/or tags are specified
        if not self.heads and not self.tags:
            return

        msg = "Project %s: syncing with repo %s." % (self.name, self.source)
        logger.info(msg)
        print msg

        repo_dir = '~/tmp'
        repo_dir = os.path.expanduser(repo_dir)
        repo_dir = os.path.abspath(repo_dir)

        # Make Empty directory - We want this to stop and fail on OSError
        if not os.path.isdir(repo_dir):
            os.makedirs(repo_dir)
            logger.debug(
                "Project %s: Created directory %s" % (self.name, repo_dir)
            )

        # Save the current working directory
        old_cwd = os.getcwd()

        try:
            # Change cwd to that repo
            os.chdir(repo_dir)

            uuid_dir = str(uuid4())
            repo_dir = os.path.join(repo_dir, uuid_dir)

            # Do a git clone --bare <source_repo>
            git.clone(self.source, name=uuid_dir, bare=True)

            # Change to bare cloned directory
            os.chdir(uuid_dir)

            # Add remote named gerrit
            ssh_url = 'ssh://%s@%s:%s/%s' % (
                remote.username,
                remote.host,
                remote.port,
                self.name
            )
            git.add_remote('gerrit', ssh_url)

            # Push heads
            if self.heads:
                kwargs = {'all_': True}
                if self.force:
                    kwargs['force'] = True
                git.push('gerrit', **kwargs)

            # Push tags
            if self.tags:
                kwargs = {'tags': True}
                if self.force:
                    kwargs['force'] = True
                git.push('gerrit', **kwargs)

            ref_kwargs = self.ref_kwargs()

            # Grab origin refs
            origin_refset = git.remote_refs('origin', **ref_kwargs)

            # Grab gerrit refs
            gerrit_refset = git.remote_refs('gerrit', **ref_kwargs)

            # Find refs that should be removed.
            prune_refset = gerrit_refset - origin_refset
            if self.preserve_prefix:
                msg = "Project %s: Preserving refs with prefixes of %s" \
                      % (self.name, self.preserve_prefix)
                logger.debug(msg)
                print msg
                heads_prefix = "refs/heads/%s" % self.preserve_prefix
                tags_prefix = "refs/tags/%s" % self.preserve_prefix
                keep = lambda ref: not ref.startswith(heads_prefix) and \
                    not ref.startswith(tags_prefix)
                prune_refset = filter(keep, prune_refset)

            # Prefix each ref in refset with ':' to delete
            colonize = lambda ref: ':%s' % ref
            prune_refset = map(colonize, prune_refset)

            # Remove branches no longer needed
            if prune_refset:
                git.push('gerrit', refspecs=prune_refset)

        finally:
            # Change to old current working directory
            os.chdir(old_cwd)

            # Attempt to clean up created directory
            shutil.rmtree(repo_dir)

    def ensure(self, remote, conf):
        """
        Ensures this project is present on gerrit.
        Can optionally create the project if it does not exits.
        Can optionally specify a configuration for the project.
        Can optionally sync the project with another repo.

        @param remote - gerrit.Remote object
        @param conf - Configuration dictionary

        """
        msg = "Project %s: Ensuring present." % self.name
        logger.info(msg)
        print msg

        ssh = remote.SSH()

        # Get list of groups for building groups file
        groups = get_groups(remote)

        # Create Project if needed
        self._create(ssh)

        # Create submit a configuration if needed
        self._config(remote, conf, groups)

        # Sync with source repo if needed
        self._sync(remote)


def get_groups(remote):
    """
    Executes a gerrit ls-groups --verbose command and parses output
    into groups. Returns a list of groups.

    @param remote - gerrit.Remote object
    @reurns list

    """
    cmd = 'gerrit ls-groups --verbose'
    groups = []
    ssh = remote.SSH()

    # Need a retcode of 0 for success
    retcode, out = ssh.exec_once(cmd)
    if retcode != 0:
        msg = "Unable to retrieve list of gerrit groups."
        logger.error(msg)
        print msg
        return groups

    # Send to buffer to easy read one line at a time
    _buffer = StringIO.StringIO()
    _buffer.write(out)
    _buffer.seek(0)

    # Parse each line into a group object and append to list
    for line in _buffer.readlines():
        tokens = re.split(r'\t+', line)
        group_data = {
            'name': tokens[0],
            'uuid': tokens[1],
            'description': None if len(tokens) == 5 else tokens[2],
            'owner': tokens[2] if len(tokens) == 5 else tokens[3],
            'owner-uuid': tokens[3] if len(tokens) == 5 else tokens[4]
        }
        groups.append(Group(group_data))

    # Return the final list.
    return groups


def groups_file_contents(groups):
    """
    Creates the contents of a groups file to be saved with a project's
    configuration.

    @param groups - List of gerrit.Group objects
    @return String

    """
    _buffer = StringIO.StringIO()
    for g in groups:
        _buffer.write("%s\t%s\n" % (g.uuid, g.name))
    return _buffer.getvalue()


def get_labels_for_upstream(conf, project_name):
    """
    Creates dictionary of label objects loaded from the config dictionary.

    @param conf - Dictionary configuration
    @param project_name - String name of a project
    @returns Dictionary of labels keyed by name

    """
    label_dicts = None

    # Look for project specific upstream labels.
    project_dicts = conf.get('projects', [])
    for project_dict in project_dicts:
        if project_dict.get('name') == project_name:
            label_dicts = project_dict.get('upstream-labels', None)
            break

    if label_dicts is None:
        label_dicts = conf.get('upstream-labels', [])

    label_objs = {}
    for l in label_dicts:
        label = Label(l['name'], int(l['min']), int(l['max']))
        label_objs.update({
            l['name']: label
        })
        logger.debug("Adding label %s with min %s and max %s"
                     % (label.name, label._min, label._max))
    return label_objs


def get_review_env():
    """
    Returns an environment to send to subprocess.Popen when using
    git review.

    @returns - Env
    """
    env = os.environ.copy()
    env['PATH'] = env.get('PATH', '') + ':/usr/local/bin/'
    logger.debug("PAth: %s" % env['PATH'])
    return env
