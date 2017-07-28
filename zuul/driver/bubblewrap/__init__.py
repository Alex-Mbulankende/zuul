# Copyright 2012 Hewlett-Packard Development Company, L.P.
# Copyright 2013 OpenStack Foundation
# Copyright 2016 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import grp
import logging
import os
import pwd
import shlex
import subprocess
import sys

from typing import Dict, List  # flake8: noqa

from zuul.driver import (Driver, WrapperInterface)


class WrappedPopen(object):
    def __init__(self, command, passwd_r, group_r):
        self.command = command
        self.passwd_r = passwd_r
        self.group_r = group_r

    def __call__(self, args, *sub_args, **kwargs):
        try:
            args = self.command + args
            if kwargs.get('close_fds') or sys.version_info.major >= 3:
                # The default in py3 is close_fds=True, so we need to pass
                # our open fds in. However, this can only work right in
                # py3.2 or later due to the lack of 'pass_fds' in prior
                # versions. So until we are py3 only we can only bwrap
                # things that are close_fds=False
                pass_fds = list(kwargs.get('pass_fds', []))
                for fd in (self.passwd_r, self.group_r):
                    if fd not in pass_fds:
                        pass_fds.append(fd)
                kwargs['pass_fds'] = pass_fds
            proc = subprocess.Popen(args, *sub_args, **kwargs)
        finally:
            self.__del__()
        return proc

    def __del__(self):
        if self.passwd_r:
            try:
                os.close(self.passwd_r)
            except OSError:
                pass
            self.passwd_r = None
        if self.group_r:
            try:
                os.close(self.group_r)
            except OSError:
                pass
            self.group_r = None


class BubblewrapDriver(Driver, WrapperInterface):
    name = 'bubblewrap'
    log = logging.getLogger("zuul.BubblewrapDriver")

    mounts_map = {'rw': [], 'ro': []}  # type: Dict[str, List]

    def __init__(self):
        self.bwrap_command = self._bwrap_command()

    def reconfigure(self, tenant):
        pass

    def stop(self):
        pass

    def setMountsMap(self, ro_dirs=[], rw_dirs=[]):
        self.mounts_map = {'ro': ro_dirs, 'rw': rw_dirs}

    def getPopen(self, **kwargs):
        # Set zuul_dir if it was not passed in
        if 'zuul_dir' in kwargs:
            zuul_dir = kwargs['zuul_dir']
        else:
            zuul_python_dir = os.path.dirname(sys.executable)
            # We want the dir directly above bin to get the whole venv
            zuul_dir = os.path.normpath(os.path.join(zuul_python_dir, '..'))

        bwrap_command = list(self.bwrap_command)
        if not zuul_dir.startswith('/usr'):
            bwrap_command.extend(['--ro-bind', zuul_dir, zuul_dir])

        for mount_type in ('ro', 'rw'):
            bind_arg = '--ro-bind' if mount_type == 'ro' else '--bind'
            for bind in self.mounts_map[mount_type]:
                bwrap_command.extend([bind_arg, bind, bind])

        # Need users and groups
        uid = os.getuid()
        passwd = list(pwd.getpwuid(uid))
        # Replace our user's actual home directory with the work dir.
        passwd = passwd[:5] + [kwargs['work_dir']] + passwd[6:]
        passwd_bytes = b':'.join(
            ['{}'.format(x).encode('utf8') for x in passwd])
        (passwd_r, passwd_w) = os.pipe()
        os.write(passwd_w, passwd_bytes)
        os.write(passwd_w, b'\n')
        os.close(passwd_w)

        gid = os.getgid()
        group = grp.getgrgid(gid)
        group_bytes = b':'.join(
            ['{}'.format(x).encode('utf8') for x in group])
        group_r, group_w = os.pipe()
        os.write(group_w, group_bytes)
        os.write(group_w, b'\n')
        os.close(group_w)

        kwargs = dict(kwargs)  # Don't update passed in dict
        kwargs['uid'] = uid
        kwargs['gid'] = gid
        kwargs['uid_fd'] = passwd_r
        kwargs['gid_fd'] = group_r
        command = [x.format(**kwargs) for x in bwrap_command]

        self.log.debug("Bubblewrap command: %s",
                       " ".join(shlex.quote(c) for c in command))

        wrapped_popen = WrappedPopen(command, passwd_r, group_r)

        return wrapped_popen

    def _bwrap_command(self):
        bwrap_command = [
            'bwrap',
            '--dir', '/tmp',
            '--tmpfs', '/tmp',
            '--dir', '/var',
            '--dir', '/var/tmp',
            '--dir', '/run/user/{uid}',
            '--ro-bind', '/usr', '/usr',
            '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/bin', '/bin',
            '--ro-bind', '/sbin', '/sbin',
            '--ro-bind', '/etc/resolv.conf', '/etc/resolv.conf',
            '--ro-bind', '/etc/hosts', '/etc/hosts',
            '--ro-bind', '{ssh_auth_sock}', '{ssh_auth_sock}',
            '--dir', '{work_dir}',
            '--bind', '{work_dir}', '{work_dir}',
            '--dev', '/dev',
            '--chdir', '{work_dir}',
            '--unshare-all',
            '--share-net',
            '--die-with-parent',
            '--uid', '{uid}',
            '--gid', '{gid}',
            '--file', '{uid_fd}', '/etc/passwd',
            '--file', '{gid_fd}', '/etc/group',
        ]

        if os.path.isdir('/lib64'):
            bwrap_command.extend(['--ro-bind', '/lib64', '/lib64'])
        if os.path.isfile('/etc/nsswitch.conf'):
            bwrap_command.extend(['--ro-bind', '/etc/nsswitch.conf',
                                  '/etc/nsswitch.conf'])

        return bwrap_command


def main(args=None):
    logging.basicConfig(level=logging.DEBUG)

    driver = BubblewrapDriver()

    parser = argparse.ArgumentParser()
    parser.add_argument('--ro-bind', nargs='+')
    parser.add_argument('--rw-bind', nargs='+')
    parser.add_argument('work_dir')
    parser.add_argument('run_args', nargs='+')
    cli_args = parser.parse_args()

    ssh_auth_sock = os.environ.get('SSH_AUTH_SOCK')

    driver.setMountsMap(cli_args.ro_bind, cli_args.rw_bind)

    popen = driver.getPopen(work_dir=cli_args.work_dir,
                            ssh_auth_sock=ssh_auth_sock)
    x = popen(cli_args.run_args)
    x.wait()


if __name__ == '__main__':
    main()
