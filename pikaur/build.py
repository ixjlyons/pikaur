import os
import shutil
import platform

from .core import (
    DataType, CmdTaskWorker,
    MultipleTasksExecutor, SingleTaskExecutor,
    ConfigReader, isolate_root_cmd, remove_dir, running_as_root,
)
from .version import get_package_name_and_version_matcher_from_depend_line
from .config import CACHE_ROOT, AUR_REPOS_CACHE_DIR, BUILD_CACHE_DIR
from .aur import get_repo_url
from .pacman import find_local_packages, PackageDB
from .args import reconstruct_args
from .pprint import color_line, bold_line
from .prompt import retry_interactive_command
from .exceptions import (
    CloneError, DependencyError, BuildError, DependencyNotBuiltYet,
)


class SrcInfo():

    common_lines = None
    package_lines = None
    path = None
    repo_path = None

    def __init__(self, repo_path, package_name):
        self.path = os.path.join(
            repo_path,
            '.SRCINFO'
        )
        self.repo_path = repo_path

        self.common_lines = []
        self.package_lines = []
        destination = self.common_lines
        with open(self.path) as srcinfo_file:
            for line in srcinfo_file.readlines():
                if line.startswith('pkgname ='):
                    if line.split('=')[1].strip() == package_name:
                        destination = self.package_lines
                    else:
                        destination = []
                else:
                    destination.append(line)

    def get_values(self, field):
        prefix = field + ' = '
        values = []
        for lines in (self.common_lines, self.package_lines):
            for line in lines:
                if line.strip().startswith(prefix):
                    values.append(line.strip().split(prefix)[1])
        return values

    def get_install_script(self):
        values = self.get_values('install')
        if values:
            return values[0]
        return None

    def _get_depends(self, field):
        return [
            get_package_name_and_version_matcher_from_depend_line(dep)[0]
            for dep in self.get_values(field)
        ]

    def get_makedepends(self):
        return self._get_depends('makedepends')

    def get_depends(self):
        return self._get_depends('depends')

    def regenerate(self):
        with open(self.path, 'w') as srcinfo_file:
            result = SingleTaskExecutor(
                CmdTaskWorker(isolate_root_cmd(['makepkg', '--printsrcinfo'],
                                               cwd=self.repo_path),
                              cwd=self.repo_path)
            ).execute()
            srcinfo_file.write(result.stdout)


class MakepkgConfig(ConfigReader):
    default_config_path = "/etc/makepkg.conf"


class PackageBuild(DataType):
    clone = False
    pull = False

    package_name = None

    repo_path = None
    build_dir = None
    built_package_path = None

    already_installed = None
    failed = None

    def __init__(self, package_name):  # pylint: disable=super-init-not-called
        self.package_name = package_name

        self.build_dir = os.path.join(CACHE_ROOT, BUILD_CACHE_DIR,
                                      self.package_name)
        self.repo_path = os.path.join(CACHE_ROOT, AUR_REPOS_CACHE_DIR,
                                      self.package_name)

        if os.path.exists(self.repo_path):
            # pylint: disable=simplifiable-if-statement
            if os.path.exists(os.path.join(self.repo_path, '.git')):
                self.pull = True
            else:
                self.clone = True
        else:
            os.makedirs(self.repo_path)
            self.clone = True

    def create_clone_task(self):
        return CmdTaskWorker([
            'git',
            'clone',
            get_repo_url(self.package_name),
            self.repo_path,
        ])

    def create_pull_task(self):
        return CmdTaskWorker([
            'git',
            '-C',
            self.repo_path,
            'pull',
            'origin',
            'master'
        ])

    def git_reset_changed(self):
        return SingleTaskExecutor(CmdTaskWorker([
            'git',
            '-C',
            self.repo_path,
            'checkout',
            '--',
            "*"
        ])).execute()

    def git_clean(self):
        return SingleTaskExecutor(CmdTaskWorker([
            'git',
            '-C',
            self.repo_path,
            'clean',
            '-f',
            '-d',
            '-x'
        ])).execute()

    def create_task(self):
        if self.pull:
            return self.create_pull_task()
        elif self.clone:
            return self.create_clone_task()
        return NotImplemented

    @property
    def last_installed_file_path(self):
        return os.path.join(
            self.repo_path,
            'last_installed.txt'
        )

    @property
    def is_installed(self):
        return os.path.exists(self.last_installed_file_path)

    @property
    def last_installed_hash(self):
        if self.is_installed:
            with open(self.last_installed_file_path) as last_installed_file:
                return last_installed_file.readlines()[0].strip()
        return None

    def update_last_installed_file(self):
        shutil.copy2(
            os.path.join(
                self.repo_path,
                '.git/refs/heads/master'
            ),
            self.last_installed_file_path
        )

    @property
    def build_files_updated(self):
        if (
                self.is_installed
        ) and (
            self.last_installed_hash != self.current_hash
        ):
            return True
        return False

    @property
    def current_hash(self):
        with open(
            os.path.join(
                self.repo_path,
                '.git/refs/heads/master'
            )
        ) as current_hash_file:
            return current_hash_file.readlines()[0].strip()

    @property
    def version_already_installed(self):
        already_installed = False
        if (
                self.package_name in PackageDB.get_local_dict().keys()
        ) and (
            self.last_installed_hash == self.current_hash
        ):
            already_installed = True
        self.already_installed = already_installed
        return already_installed

    def _install_built_deps(self, args, all_package_builds, all_deps_to_install):
        built_deps_to_install = []
        for dep in all_deps_to_install[:]:
            # @TODO: check if dep is Provided by built package
            if dep in all_package_builds:
                if all_package_builds[dep].failed:
                    self.failed = True
                    raise DependencyError()
                built_package_path = all_package_builds[dep].built_package_path
                if not built_package_path:
                    raise DependencyNotBuiltYet()
                built_deps_to_install.append(built_package_path)
                all_deps_to_install.remove(dep)

        if built_deps_to_install:
            print('{} {} {}:'.format(
                color_line('::', 13),
                "Installing already built dependencies for",
                bold_line(self.package_name)
            ))
            if not retry_interactive_command(
                    [
                        'sudo',
                        'pacman',
                        '--upgrade',
                        '--asdeps',
                        '--noconfirm',
                    ] + reconstruct_args(args, ignore_args=[
                        'upgrade',
                        'asdeps',
                        'noconfirm',
                        'sync',
                        'sysupgrade',
                        'refresh',
                    ]) + built_deps_to_install,
            ):
                self.failed = True
                raise DependencyError()

    def _install_repo_deps(self, args, all_deps_to_install):
        if all_deps_to_install:
            local_provided = PackageDB.get_local_provided()
            for dep_name in all_deps_to_install[:]:
                if dep_name in local_provided:
                    all_deps_to_install.remove(dep_name)
        if all_deps_to_install:
            # @TODO: resolve makedeps in case if it was specified by Provides,
            # @TODO: not real name - 1) store them
            print('{} {} {}:'.format(
                color_line('::', 13),
                "Installing repository dependencies for",
                bold_line(self.package_name)
            ))
            if not retry_interactive_command(
                    [
                        'sudo',
                        'pacman',
                        '--sync',
                        '--asdeps',
                        '--needed',
                        '--noconfirm',
                    ] + reconstruct_args(args, ignore_args=[
                        'sync',
                        'asdeps',
                        'needed',
                        'noconfirm',
                        'sysupgrade',
                        'refresh',
                    ]) + all_deps_to_install,
            ):
                self.failed = True
                raise BuildError()

    def _remove_make_deps(self, new_make_deps_to_install):
        if new_make_deps_to_install:
            print('{} {} {}:'.format(
                color_line('::', 13),
                "Removing make dependencies for",
                bold_line(self.package_name)
            ))
            # @TODO: resolve makedeps in case if it was specified by Provides,
            # @TODO: not real name - 2) remove them
            retry_interactive_command(
                [
                    'sudo',
                    'pacman',
                    '-Rs',
                    '--noconfirm',
                ] + new_make_deps_to_install,
            )

    def _set_built_package_path(self):
        dest_dir = MakepkgConfig.get('PKGDEST', self.build_dir)
        pkg_ext = MakepkgConfig.get('PKGEXT', '.pkg.tar.xz')
        pkg_ext = MakepkgConfig.get(
            'PKGEXT', pkg_ext,
            config_path=os.path.join(dest_dir, 'PKGBUILD')
        )
        full_pkg_names = SingleTaskExecutor(CmdTaskWorker(
            isolate_root_cmd(['makepkg', '--packagelist'],
                             cwd=self.build_dir),
            cwd=self.build_dir
        )).execute().stdout.splitlines()
        full_pkg_name = full_pkg_names[0]
        if len(full_pkg_names) > 1:
            arch = platform.machine()
            for pkg_name in full_pkg_names:
                if arch in pkg_name:
                    full_pkg_name = pkg_name
        built_package_path = os.path.join(dest_dir, full_pkg_name+pkg_ext)
        if os.path.exists(built_package_path):
            self.built_package_path = built_package_path

    def build(self, args, all_package_builds):
        if running_as_root():
            # Let systemd-run setup the directories and symlinks
            true_cmd = isolate_root_cmd(['true'])
            SingleTaskExecutor(CmdTaskWorker(true_cmd)).execute()

            # Chown the private CacheDirectory to root to signal systemd that
            # it needs to recursively chown it to the correct user
            os.chown(os.path.realpath(CACHE_ROOT), 0, 0)

        if os.path.exists(self.build_dir):
            remove_dir(self.build_dir)
        shutil.copytree(self.repo_path, self.build_dir)

        src_info = SrcInfo(self.repo_path, self.package_name)
        make_deps = src_info.get_makedepends()
        _, new_make_deps_to_install = find_local_packages(make_deps)
        new_deps = src_info.get_depends()
        _, new_deps_to_install = find_local_packages(new_deps)
        all_deps_to_install = new_make_deps_to_install + new_deps_to_install

        self._install_built_deps(args, all_package_builds, all_deps_to_install)
        self._install_repo_deps(args, all_deps_to_install)

        makepkg_args = [
            '--nodeps',
        ]
        if not args.needed:
            makepkg_args.append('--force')

        print()
        build_succeeded = retry_interactive_command(
            isolate_root_cmd(['makepkg'] + makepkg_args,
                             cwd=self.build_dir),
            cwd=self.build_dir
        )

        self._remove_make_deps(new_make_deps_to_install)

        if not build_succeeded:
            if new_deps_to_install:
                print('{} {} {}:'.format(
                    color_line('::', 13),
                    "Removing already installed dependencies for",
                    bold_line(self.package_name)
                ))
                retry_interactive_command(
                    [
                        'sudo',
                        'pacman',
                        '-Rs',
                    ] + new_deps_to_install,
                )
            self.failed = True
            raise BuildError()
        else:
            self._set_built_package_path()


def clone_pkgbuilds_git_repos(package_names):
    package_builds = {
        package_name: PackageBuild(package_name)
        for package_name in package_names
    }
    results = MultipleTasksExecutor({
        repo_status.package_name: repo_status.create_task()
        for repo_status in package_builds.values()
    }).execute()
    for package_name, result in results.items():
        if result.return_code > 0:
            raise CloneError(
                build=package_builds[package_name],
                result=result
            )
    return package_builds
