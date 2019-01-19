# Copyright 2019 BMW Group
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

import concurrent.futures
import configparser
import logging
import os
import shutil
import subprocess
import sys
import zuul.ansible

from pkg_resources import resource_string
from zuul.lib.config import get_default


class ManagedAnsible:
    log = logging.getLogger('zuul.managed_ansible')

    def __init__(self, config, version):
        self.version = version

        requirements = get_default(config, version, 'requirements')
        self._requirements = requirements.split(' ')

        self.default = get_default(config, version, 'default', False)
        self.deprecated = get_default(config, version, 'deprecated', False)

        self._ansible_root = os.path.join(
            sys.exec_prefix, 'lib', 'zuul', 'ansible')

    def ensure_ansible(self, upgrade=False):
        self._ensure_venv()

        self.log.info('Installing ansible %s, extra packages: %s',
                      self.version, self.extra_packages)
        self._run_pip(self._requirements + self.extra_packages,
                      upgrade=upgrade)

    def _run_pip(self, requirements, upgrade=False):
        cmd = [os.path.join(self.venv_path, 'bin', 'pip'), 'install']
        if upgrade:
            cmd.append('-U')
        cmd.extend(requirements)
        self.log.debug('Running pip: %s', ' '.join(cmd))

        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if p.returncode != 0:
            raise Exception('Package installation failed with exit code %s '
                            'during processing ansible %s:\n'
                            'stdout:\n%s\n'
                            'stderr:\n%s' % (p.returncode, self.version,
                                             p.stdout.decode(),
                                             p.stderr.decode()))
        self.log.debug('Successfully installed packages %s', requirements)

    def _ensure_venv(self):
        if os.path.exists(self.python_path):
            self.log.debug(
                'Virtual environment %s already existing', self.venv_path)
            return

        self.log.info('Creating venv %s', self.venv_path)

        python_executable = sys.executable
        if hasattr(sys, 'real_prefix'):
            # We're inside a virtual env and the venv module behaves strange
            # if we're calling it from there so default to
            # <real_prefix>/bin/python3
            python_executable = os.path.join(sys.real_prefix, 'bin', 'python3')

        # We don't use directly the venv module here because its behavior is
        # broken if we're already in a virtual environment.
        cmd = ['virtualenv', '-p', python_executable, self.venv_path]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if p.returncode != 0:
            raise Exception('venv creation failed with exit code %s:\n'
                            'stdout:\n%s\n'
                            'stderr:\n%s' % (p.returncode, p.stdout.decode(),
                                             p.stderr.decode()))

    @property
    def venv_path(self):
        return os.path.join(self._ansible_root, self.version)

    @property
    def python_path(self):
        return os.path.join(self.venv_path, 'bin', 'python')

    @property
    def extra_packages(self):
        mapping = str.maketrans({
            '.': None,
            '-': '_',
        })
        env_var = 'ANSIBLE_%s_EXTRA_PACKAGES' % self.version.upper().translate(
            mapping)

        packages = os.environ.get(env_var)
        if packages:
            return packages.strip().split(' ')

        return []

    def __repr__(self):
        return 'Ansible {a.version}, {a.default}, {a.deprecated}'.format(
            a=self)


class AnsibleManager:
    log = logging.getLogger('zuul.ansible_manager')

    def __init__(self, zuul_ansible_dir=None):
        self._supported_versions = {}
        self.default_version = None
        self.zuul_ansible_dir = zuul_ansible_dir

        self.load_ansible_config()

    def load_ansible_config(self):
        c = resource_string(__name__, 'ansible-config.conf').decode()
        config = configparser.ConfigParser()
        config.read_string(c)

        for version in config.sections():

            ansible = ManagedAnsible(config, version)

            if ansible.version in self._supported_versions:
                raise RuntimeError(
                    'Ansible version %s already defined' % ansible.version)

            self._supported_versions[ansible.version] = ansible

            if ansible.default:
                if self.default_version is not None:
                    raise RuntimeError(
                        'Default ansible version can only specified once')
                self.default_version = ansible.version

        if not self.default_version:
            raise RuntimeError('A default ansible version must be specified')

    def install(self, upgrade=False):
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = {executor.submit(a.ensure_ansible, upgrade): a
                       for a in self._supported_versions.values()}
            for future in concurrent.futures.as_completed(futures):
                future.result()

    def _getAnsible(self, version):
        if not version:
            version = self.default_version

        ansible = self._supported_versions.get(version)
        if not ansible:
            raise Exception('Requested ansible version %s not found' % version)
        return ansible

    def getAnsibleCommand(self, version=None, command='ansible-playbook'):
        ansible = self._getAnsible(version)
        return os.path.join(ansible.venv_path, 'bin', command)

    def getAnsibleDir(self, version=None):
        ansible = self._getAnsible(version)
        return os.path.join(self.zuul_ansible_dir, ansible.version)

    def getAnsiblePluginDir(self, version=None):
        return os.path.join(self.getAnsibleDir(version), 'zuul', 'ansible')

    def copyAnsibleFiles(self):
        if os.path.exists(self.zuul_ansible_dir):
            shutil.rmtree(self.zuul_ansible_dir)

        library_path = os.path.dirname(os.path.abspath(zuul.ansible.__file__))
        for ansible in self._supported_versions.values():
            ansible_dir = os.path.join(self.zuul_ansible_dir, ansible.version)
            plugin_dir = os.path.join(ansible_dir, 'zuul', 'ansible')
            source_path = os.path.join(library_path, ansible.version)

            os.makedirs(plugin_dir, exist_ok=True)
            for fn in os.listdir(source_path):
                if fn in ('__pycache__', 'base'):
                    continue
                full_path = os.path.join(source_path, fn)
                if os.path.isdir(full_path):
                    shutil.copytree(full_path, os.path.join(plugin_dir, fn))
                else:
                    shutil.copy(os.path.join(source_path, fn), plugin_dir)

            # We're copying zuul.ansible.* into a directory we are going
            # to add to pythonpath, so our plugins can "import
            # zuul.ansible".  But we're not installing all of zuul, so
            # create a __init__.py file for the stub "zuul" module.
            module_paths = [
                os.path.join(ansible_dir, 'zuul'),
                os.path.join(ansible_dir, 'zuul', 'ansible'),
            ]
            for fn in module_paths:
                with open(os.path.join(fn, '__init__.py'), 'w'):
                    # Nothing to do here, we just want the file to exist.
                    pass
