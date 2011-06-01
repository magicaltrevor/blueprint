import base64
from collections import defaultdict
import copy
import json
import logging
import os
import os.path
import re
import subprocess
import time
import urllib

# This must be called early - before the rest of the blueprint library loads.
logging.basicConfig(format='# [blueprint] %(message)s',
                    level=logging.INFO)

import context_managers
import git
from manager import Manager
import util


class Blueprint(dict):

    DISCLAIMER = """#
# Automatically generated by blueprint(7).  Edit at your own risk.
#
"""

    @classmethod
    def destroy(cls, name):
        """
        Destroy the named blueprint.
        """
        if not os.path.isdir(git.repo()):
            raise KeyError(name)
        try:
            git.git('branch', '-D', name)
        except:
            raise KeyError(name)

    @classmethod
    def iter(cls):
        """
        Yield the name of each blueprint.
        """
        if not os.path.isdir(git.repo()):
            return
        status, stdout = git.git('branch')
        for line in stdout.splitlines():
            yield line.strip()

    def __init__(self, name=None, commit=None, create=False):
        """
        Construct a blueprint in the new format in a backwards-compatible
        manner.
        """
        self.name = name
        self._commit = commit

        # Create a new blueprint object and populate it based on this server.
        if create:
            super(Blueprint, self).__init__()
            import backend
            for funcname in backend.__all__:
                getattr(backend, funcname)(self)

        # Create a blueprint from a Git repository.
        elif name is not None:
            git.init()
            if self._commit is None:
                self._commit = git.rev_parse('refs/heads/{0}'.format(name))
                if self._commit is None:
                    raise KeyError(name)
            tree = git.tree(self._commit)
            blob = git.blob(tree, 'blueprint.json')
            content = git.content(blob)
            super(Blueprint, self).__init__(**json.loads(content))

        # Create an empty blueprint object to be filled in later.
        else:
            super(Blueprint, self).__init__()

    def __sub__(self, other):
        """
        Subtracting one blueprint from another allows blueprints to remain
        free of superfluous packages from the base installation.  It takes
        three passes through the package tree.  The first two remove
        superfluous packages and the final one accounts for some special
        dependencies by adding them back to the tree.
        """
        b = copy.deepcopy(self)

        # The first pass removes all duplicate packages that are not
        # themselves managers.  Allowing multiple versions of the same
        # packages complicates things slightly.  For each package, each
        # version that appears in the other blueprint is removed from
        # this blueprint.  After that is finished, this blueprint is
        # normalized.  If no versions remain, the package is removed.
        def package(manager, package, version):
            if package in b.packages:
                return
            if manager.name in b.packages.get(manager.name, {}):
                return
            b_packages = b.packages[manager.name]
            if package not in b_packages:
                return
            b_versions = b_packages[package]
            try:
                del b_versions[b_versions.index(version)]
            except ValueError:
                pass
            if 0 == len(b_versions):
                del b_packages[package]
            else:
                b_packages[package] = b_versions
        other.walk(package=package)

        # The second pass removes managers that manage no packages, a
        # potential side-effect of the first pass.  This step must be
        # applied repeatedly until the blueprint reaches a steady state.
        def package(manager, package, version):
            if package not in b.packages:
                return
            if 0 == len(b.packages[package]):
                del b.packages[package]
                del b.packages[self.managers[package].name][package]
        while 1:
            l = len(b.packages)
            other.walk(package=package)
            if len(b.packages) == l:
                break

        # The third pass adds back special dependencies like `ruby*-dev`.
        # It isn't apparent from the rules above that a manager like RubyGems
        # needs more than just itself to function.  In some sense, this might
        # be considered a missing dependency in the Debian archive but in
        # reality it's only _likely_ that you need `ruby*-dev` to use
        # `rubygems*`.
        def after(manager):
            if manager.name not in b.packages:
                return

            deps = {r'^python(\d+(?:\.\d+)?)$': ['python{0}',
                                                 'python{0}-dev',
                                                 'python',
                                                 'python-devel'],
                    r'^ruby(\d+\.\d+(?:\.\d+)?)$': ['ruby{0}-dev'],
                    r'^rubygems(\d+\.\d+(?:\.\d+)?)$': ['ruby{0}',
                                                        'ruby{0}-dev',
                                                        'ruby',
                                                        'ruby-devel']}

            for pattern, packages in deps.iteritems():
                match = re.search(pattern, manager.name)
                if match is None:
                    continue
                for package in packages:
                    package = package.format(match.group(1))
                    for managername in ('apt', 'yum'):
                        mine = self.packages[managername].get(package, None)
                        if mine is not None:
                            b.packages[managername][package] = mine
        other.walk(after=after)

        return b

    def get_name(self):
        return self._name
    def set_name(self, name):
        """
        Validate and set the blueprint name.
        """
        if name is not None and re.search(r'[/ \t\r\n]', name):
            raise ValueError('invalid blueprint name')
        self._name = name
    name = property(get_name, set_name)

    @property
    def arch(self):
        if 'arch' not in self:
            self['arch'] = None
        return self['arch']

    @property
    def files(self):
        if 'files' not in self:
            self['files'] = defaultdict(dict)
        return self['files']

    @property
    def managers(self):
        """
        Build a hierarchy of managers for easy access when declaring
        dependencies.
        """
        if hasattr(self, '_managers'):
            return self._managers
        self._managers = {'apt': None, 'yum': None}

        def package(manager, package, version):
            if package in self.packages and manager != package:
                self._managers[package] = manager

        self.walk(package=package)
        return self._managers

    @property
    def packages(self):
        if 'packages' not in self:
            self['packages'] = defaultdict(lambda: defaultdict(list))
        return self['packages']

    @property
    def sources(self):
        if 'sources' not in self:
            self['sources'] = defaultdict(dict)
        return self['sources']

    def commit(self, message=''):
        """
        Create a new revision of this blueprint in the local Git repository.
        Include the blueprint JSON and any source archives referenced by
        the JSON.
        """
        git.init()
        refname = 'refs/heads/{0}'.format(self.name)
        parent = git.rev_parse(refname)

        # Start with an empty index every time.  Specifically, clear out
        # source tarballs from the parent commit.
        if parent is not None:
            for mode, type, sha, pathname in git.ls_tree(git.tree(parent)):
                git.git('update-index', '--remove', pathname)

        # Add `blueprint.json` to the index.
        f = open('blueprint.json', 'w')
        f.write(self.dumps())
        f.close()
        git.git('update-index', '--add', os.path.abspath('blueprint.json'))

        # Add source tarballs to the index.
        for filename in self.sources.itervalues():
            git.git('update-index', '--add', os.path.abspath(filename))

        # Add the `.blueprintignore` file to the index.  Since adding extra
        # syntax to this file, it no longer makes sense to store it as
        # `.gitignore`.
        try:
            os.link(os.path.expanduser('~/.blueprintignore'),
                    '.blueprintignore')
            git.git('update-index',
                    '--add',
                    os.path.abspath('.blueprintignore'))
        except OSError:
            pass

        # Write the index to Git's object store.
        tree = git.write_tree()

        # Write the commit and update the tip of the branch.
        self._commit = git.commit_tree(tree, message, parent)
        git.git('update-ref', refname, self._commit)

    def dumps(self):
        """
        Return a JSON serialization of this blueprint.  Make a best effort
        to prevent variance from run-to-run.  Remove superfluous empty keys.
        """
        if 'arch' in self and self['arch'] is None:
            del self['arch']
        for key in ['files', 'packages', 'sources']:
            if key in self and 0 == len(self[key]):
                del self[key]
        return json.dumps(self, indent=2, sort_keys=True)

    def puppet(self):
        """
        Generate Puppet code.
        """
        import puppet
        m = puppet.Manifest(self.name, comment=self.DISCLAIMER)

        # Set the default `PATH` for exec resources.
        m.add(puppet.Exec.defaults(path=os.environ['PATH']))

        # Extract source tarballs.
        tree = git.tree(self._commit)
        for dirname, filename in sorted(self.sources.iteritems()):
            blob = git.blob(tree, filename)
            content = git.content(blob)
            pathname = os.path.join('/tmp', filename)
            m['sources'].add(puppet.File(
                pathname,
                self.name,
                content,
                owner='root',
                group='root',
                mode='0644',
                source='puppet:///modules/{0}/{1}'.format(self.name,
                                                          pathname[1:])))
            m['sources'].add(puppet.Exec(
                'tar xf {0}'.format(pathname),
                cwd=dirname,
                require=puppet.File.ref(pathname)))

        # Place files.
        if 0 < len(self.files):
            for pathname, f in sorted(self.files.iteritems()):

                # Create resources for parent directories and let the
                # autorequire mechanism work out dependencies.
                dirnames = os.path.dirname(pathname).split('/')[1:]
                for i in xrange(len(dirnames)):
                    m['files'].add(puppet.File(
                        os.path.join('/', *dirnames[0:i + 1]),
                        ensure='directory'))

                # Create the actual file resource.
                if '120000' == f['mode'] or '120777' == f['mode']:
                    m['files'].add(puppet.File(pathname,
                                               None,
                                               None,
                                               owner=f['owner'],
                                               group=f['group'],
                                               ensure=f['content']))
                    continue
                content = f['content']
                if 'base64' == f['encoding']:
                    content = base64.b64decode(content)
                m['files'].add(puppet.File(pathname,
                                           self.name,
                                           content,
                                           owner=f['owner'],
                                           group=f['group'],
                                           mode=f['mode'][-4:],
                                           ensure='file'))

        # Install packages.
        deps = []

        def before(manager):
            if 0 == len(manager):
                return
            if 1 == len(manager) and manager.name in manager:
                return
            if 'apt' == manager.name:
                m['packages'].add(puppet.Exec('apt-get -q update',
                                              before=puppet.Class.ref('apt')))
            elif 'yum' == manager.name:
                m['packages'].add(puppet.Exec('yum makecache',
                                              before=puppet.Class.ref('yum')))
            deps.append(manager)

        def package(manager, package, version):

            # `apt` and `yum` are easy since they're the default for their
            # respective platforms.
            if manager.name in ('apt', 'yum'):
                m['packages'][manager].add(puppet.Package(package,
                                                          ensure=version))

                # If APT is installing RubyGems, get complicated.  This would
                # make sense to do with Yum, too, but there's no consensus on
                # where, exactly, you might find RubyGems from Yum.  Going
                # the other way, it's entirely likely that doing this sort of
                # forced upgrade goes against the spirit of Blueprint itself.
                match = re.match(r'^rubygems(\d+\.\d+(?:\.\d+)?)$', package)
                if match is not None and util.rubygems_update():
                    m['packages'][manager].add(puppet.Exec('/bin/sh -c "'
                        '/usr/bin/gem{0} install --no-rdoc --no-ri '
                        'rubygems-update; '
                        '/usr/bin/ruby{0} $(PATH=$PATH:/var/lib/gems/{0}/bin '
                        'which update_rubygems)"'.format(match.group(1)),
                        require=puppet.Package.ref(package)))

            # RubyGems for Ruby 1.8 is easy, too, because Puppet has a
            # built in provider.  This is called simply "rubygems" on
            # RPM-based distros.
            elif manager.name in ('rubygems', 'rubygems1.8'):
                m['packages'][manager].add(puppet.Package(package,
                    ensure=version,
                    provider='gem'))

            # Other versions of RubyGems are slightly more complicated.
            elif re.search(r'ruby', manager.name) is not None:
                match = re.match(r'^ruby(?:gems)?(\d+\.\d+(?:\.\d+)?)',
                                 manager.name)
                m['packages'][manager].add(puppet.Exec(
                    manager(package, version),
                    creates='{0}/{1}/gems/{2}-{3}'.format(util.rubygems_path(),
                                                          match.group(1),
                                                          package,
                                                          version)))

            # Python works basically like alternative versions of Ruby
            # but follows a less predictable directory structure so the
            # directory is not known ahead of time.  This just so happens
            # to be the way everything else works, too.
            else:
                m['packages'][manager].add(puppet.Exec(
                    manager(package, version)))

        self.walk(before=before, package=package)
        m['packages'].dep(*[puppet.Class.ref(dep) for dep in deps])

        # Strict ordering of classes.
        deps = []
        if 0 < len(self.sources):
            deps.append('sources')
        if 0 < len(self.files):
            deps.append('files')
        if 0 < len(self.packages):
            deps.append('packages')
        m.dep(*[puppet.Class.ref(dep) for dep in deps])

        return m

    def chef(self):
        """
        Generate Chef code.
        """
        import chef
        c = chef.Cookbook(self.name, comment=self.DISCLAIMER)

        # Extract source tarballs.
        tree = git.tree(self._commit)
        for dirname, filename in sorted(self.sources.iteritems()):
            blob = git.blob(tree, filename)
            content = git.content(blob)
            pathname = os.path.join('/tmp', filename)
            c.file(pathname,
                   content,
                   owner='root',
                   group='root',
                   mode='0644',
                   backup=False,
                   source=pathname[1:])
            c.execute('tar xf {0}'.format(pathname), cwd=dirname)

        # Place files.
        for pathname, f in sorted(self.files.iteritems()):
            c.directory(os.path.dirname(pathname),
                        group='root',
                        mode='0755',
                        owner='root',
                        recursive=True)
            if '120000' == f['mode'] or '120777' == f['mode']:
                c.link(pathname,
                       owner=f['owner'],
                       group=f['group'],
                       to=f['content'])
                continue
            content = f['content']
            if 'base64' == f['encoding']:
                content = base64.b64decode(content)
            c.file(pathname, content,
                   owner=f['owner'],
                   group=f['group'],
                   mode=f['mode'][-4:],
                   backup=False,
                   source=pathname[1:])

        # Install packages.
        def before(manager):
            if 0 == len(manager):
                return
            if 'apt' == manager.name:
                c.execute('apt-get -q update')
            elif 'yum' == manager.name:
                c.execute('yum makecache')

        def package(manager, package, version):
            if manager.name == package:
                return

            if manager.name in ('apt', 'yum'):
                c.package(package, version=version)

                # See comments on this section in `puppet` above.
                match = re.match(r'^rubygems(\d+\.\d+(?:\.\d+)?)$', package)
                if match is not None and util.rubygems_update():
                    c.execute('/usr/bin/gem{0} install --no-rdoc --no-ri '
                              'rubygems-update'.format(match.group(1)))
                    c.execute('/usr/bin/ruby{0} '
                              '$(PATH=$PATH:/var/lib/gems/{0}/bin '
                              'which update_rubygems)"'.format(match.group(1)))

            # All types of gems get to have package resources.
            elif 'rubygems' == manager.name:
                c.gem_package(package, version=version)
            elif re.search(r'ruby', manager.name) is not None:
                match = re.match(r'^ruby(?:gems)?(\d+\.\d+(?:\.\d+)?)',
                                 manager.name)
                c.gem_package(package,
                    gem_binary='/usr/bin/gem{0}'.format(match.group(1)),
                    version=version)

            # Everything else is an execute resource.
            else:
                c.execute(manager(package, version))

        self.walk(before=before, package=package)

        return c

    def sh(self, secret=None):
        """
        Generate shell code.
        """
        import sh
        s = sh.Script(self.name, comment=self.DISCLAIMER)

        # Extract source tarballs.
        if secret is not None:
            for dirname, filename in sorted(self.sources.iteritems()):
                s.add('wget https://devstructure.com/{0}/{1}/{2}',
                      secret,
                      self.name,
                      filename)
                s.add('tar xf "{0}" -C "{1}"',
                      filename,
                      dirname)
        else:
            tree = git.tree(self._commit)
            for dirname, filename in sorted(self.sources.iteritems()):
                blob = git.blob(tree, filename)
                content = git.content(blob)
                s.add('tar xf "{0}" -C "{1}"',
                      filename,
                      dirname,
                      sources={filename: content})

        # Place files.
        for pathname, f in sorted(self.files.iteritems()):
            s.add('mkdir -p "{0}"', os.path.dirname(pathname))
            if '120000' == f['mode'] or '120777' == f['mode']:
                s.add('ln -s "{0}" "{1}"', f['content'], pathname)
                continue
            command = 'cat'
            if 'base64' == f['encoding']:
                command = 'base64 --decode'
            eof = 'EOF'
            while re.search(r'{0}'.format(eof), f['content']):
                eof += 'EOF'
            s.add('{0} >"{1}" <<{2}', command, pathname, eof)
            s.add(raw=f['content'])
            if 0 < len(f['content']) and '\n' != f['content'][-1]:
                eof = '\n{0}'.format(eof)
            s.add(eof)
            if 'root' != f['owner']:
                s.add('chown {0} "{1}"', f['owner'], pathname)
            if 'root' != f['group']:
                s.add('chgrp {0} "{1}"', f['group'], pathname)
            if '000644' != f['mode']:
                s.add('chmod {0} "{1}"', f['mode'][-4:], pathname)

        # Install packages.
        def before(manager):
            if 0 == len(manager):
                return
            if 'apt' == manager.name:
                s.add('apt-get -q update')
            elif 'yum' == manager.name:
                s.add('yum makecache')

        def package(manager, package, version):
            if manager.name == package:
                return
            s.add(manager(package, version))
            if manager.name not in ('apt', 'yum'):
                return

            # See comments on this section in `puppet` above.
            match = re.match(r'^rubygems(\d+\.\d+(?:\.\d+)?)$', package)
            if match is not None and util.rubygems_update():
                s.add('/usr/bin/gem{0} install --no-rdoc --no-ri '
                  'rubygems-update', match.group(1))
                s.add('/usr/bin/ruby{0} $(PATH=$PATH:/var/lib/gems/{0}/bin '
                  'which update_rubygems)', match.group(1))

        self.walk(before=before, package=package)

        return s

    def blueprintignore(self):
        """
        Return the blueprint's ~/.blueprintignore file.  Prior to v3.0.4
        this file was stored as .gitignore in the repository.
        """
        tree = git.tree(self._commit)
        blob = git.blob(tree, '.blueprintignore')
        if blob is None:
            blob = git.blob(tree, '.gitignore')
        import ignore
        if blob is None:
            return ignore.Rules('')
        content = git.content(blob)
        if content is None:
            return ignore.Rules('')
        return ignore.Rules(content)

    def walk(self, managername=None, **kwargs):
        """
        Walk a package tree and execute callbacks along the way.  This is
        a bit like iteration but can't match the iterator protocol due to
        the varying argument lists given to each type of callback.  The
        available callbacks are:

        * `before(manager):`
          Executed before a manager's dependencies are enumerated.
        * `package(manager, package, versions):`
          Executed when a package is enumerated.
        * `after(manager):`
          Executed after a manager's dependencies are enumerated.
        """

        # Walking begins with the system package managers, `apt` and `yum`.
        if managername is None:
            self.walk('apt', **kwargs)
            self.walk('yum', **kwargs)
            return

        # Get the full manager from its name.  Watch out for KeyError (by
        # using dict.get instead of dict.__get__), which means the manager
        # isn't part of this blueprint.
        manager = Manager(managername, self.packages.get(managername, {}))

        # Give the manager a chance to setup for its dependencies.
        callable = getattr(kwargs.get('before', None), '__call__', None)
        if callable:
            callable(manager)

        # Each package gets its chance to take action.  Note which packages
        # are themselves managers so they may be visited recursively later.
        managers = []
        callable = getattr(kwargs.get('package', None), '__call__', None)
        for package, versions in sorted(manager.iteritems()):
            if callable:
                for version in versions:
                    callable(manager, package, version)
            if managername != package and package in self.packages:
                managers.append(package)

        # Give the manager a change to cleanup after itself.
        callable = getattr(kwargs.get('after', None), '__call__', None)
        if callable:
            callable(manager)

        # Now recurse into each manager that was just installed.  Recursing
        # here is safer because there may be secondary dependencies that are
        # not expressed in the hierarchy (for example the `mysql2` gem
        # depends on `libmysqlclient-dev` in addition to its manager).
        for managername in managers:
            self.walk(managername, **kwargs)
