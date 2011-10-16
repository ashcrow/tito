# Copyright (c) 2008-2011 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.

"""
Code for submitting builds for release.
"""

import os
import commands
import fedora_cert
import pyfedpkg
import tempfile
import subprocess

from tempfile import mkdtemp
from shutil import rmtree, copy

from tito.common import *
from tito.exception import TitoException

DEFAULT_KOJI_OPTS = "build --nowait"
DEFAULT_CVS_BUILD_DIR = "cvswork"

# List of CVS files to protect when syncing git with a CVS module:
PROTECTED_BUILD_SYS_FILES = ('branch', 'CVS', '.cvsignore', 'Makefile', 'sources', ".git", ".gitignore")

RSYNC_USERNAME = 'RSYNC_USERNAME' # environment variable name

class Releaser(object):
    """
    Parent class of all releasers.

    Can't really be used by itself, need to use one of the sub-classes.
    """
    GLOBAL_REQUIRED_CONFIG = ['releaser']
    REQUIRED_CONFIG = []
    OPTIONAL_CONFIG = []

    def __init__(self, name=None, version=None, tag=None, build_dir=None,
            pkg_config=None, global_config=None, user_config=None,
            target=None, releaser_config=None):

        self.builder_args = self._parse_builder_args(releaser_config, target)

        # While we create a builder here, we don't actually call run on it
        # unless the releaser needs to:
        self.builder = create_builder(name, tag,
                version, None, pkg_config,
                build_dir, global_config, user_config, self.builder_args)
        self.project_name = self.builder.project_name

        # TODO: if it looks like we need custom CVSROOT's for different users,
        # allow setting of a property to lookup in ~/.spacewalk-build-rc to
        # use instead. (if defined)
        self.cvs_workdir = os.path.join(self.builder.rpmbuild_basedir,
                DEFAULT_CVS_BUILD_DIR)
        debug("cvs_workdir = %s" % self.cvs_workdir)

        self.cvs_package_workdir = os.path.join(self.cvs_workdir,
                self.project_name)

        # When syncing files with CVS, only copy files with these extensions:
        self.cvs_copy_extensions = (".spec", ".patch")

        # Config for all releasers:
        self.releaser_config = releaser_config

        # The actual release target we're building:
        self.target = target

        self.dry_run = False

        self._check_releaser_config()

    def _check_releaser_config(self):
        """
        Verify this release target has all the config options it needs.
        """
        for opt in self.GLOBAL_REQUIRED_CONFIG:
            if not self.releaser_config.has_option(self.target, opt):
                raise TitoException(
                        "Release target '%s' missing required option '%s'" %
                        (self.target, opt))
        for opt in self.REQUIRED_CONFIG:
            if not self.releaser_config.has_option(self.target, opt):
                raise TitoException(
                        "Release target '%s' missing required option '%s'" %
                        (self.target, opt))

        # TODO: accomodate 'builder.*' for yum releaser and we can use this:
        #for opt in self.releaser_config.options(self.target):
        #    if opt not in self.GLOBAL_REQUIRED_CONFIG and \
        #            opt not in self.REQUIRED_CONFIG and \
        #            opt not in self.OPTIONAL_CONFIG:
        #        raise TitoException(
        #                "Release target '%s' has unknown option '%s'" %
        #                (self.target, opt))


    def _parse_builder_args(self, releaser_config, target):
        """
        Any properties found in a releaser target section starting with
        "builder." are assumed to be builder arguments.

        i.e.:

        builder.mock = epel-6-x86_64

        Would indicate that we need to pass an argument "mock" to whatever
        builder is configured.
        """
        args = {}
        for opt in releaser_config.options(target):
            if opt.startswith("builder."):
                args[opt[len("builder."):]] = releaser_config.get(target, opt)
        debug("Parsed custom builder args: %s" % args)
        return args

    def release(self, dry_run=False):
        pass

    def cleanup(self):
        debug("Cleaning up [%s]" % self.cvs_package_workdir)
        run_command("rm -rf %s" % self.cvs_package_workdir)

    def _list_files_to_copy(self):
        """
        Returns a list of the full file paths for each file that should be
        copied from our git project into the build system checkout. This
        is used to sync files to CVS or git during a release.

        i.e. spec file, .patches.

        It is assumed that any file found in the build system checkout
        but not in this list, and not in the protected files list, should
        probably be cleaned up.
        """
        # Include the spec file explicitly, in the case of SatelliteBuilder
        # we modify and then use a spec file copy from a different location.
        files_to_copy = [self.builder.spec_file] # full paths

        for filename in os.listdir(self.builder.rpmbuild_gitcopy):
            full_filepath = os.path.join(self.builder.rpmbuild_gitcopy, filename)
            if os.path.isdir(full_filepath):
                # skip it
                continue
            if filename in PROTECTED_BUILD_SYS_FILES:
                debug("   skipping:  %s (protected file)" % filename)
                continue
            elif filename.endswith(".spec"):
                # Skip the spec file, we already copy this explicitly as it
                # can come from a couple different locations depending on which
                # builder is in use.
                continue

            # Check if file ends with something this builder subclass wants
            # to copy:
            copy_it = False
            for extension in self.cvs_copy_extensions:
                if filename.endswith(extension):
                    copy_it = True
                    continue
            if copy_it:
                debug("   copying:   %s" % filename)
                files_to_copy.append(full_filepath)

        return files_to_copy

    def print_dry_run_warning(self, command_that_would_be_run_otherwise):
        print
        print("WARNING: Skipping command due to --dry-run: %s" %
                command_that_would_be_run_otherwise)
        print

    def _sync_files(self, files_to_copy, dest_dir):
        debug("Copying files: %s" % files_to_copy)
        debug("   to: %s" % dest_dir)
        os.chdir(dest_dir)

        # Need a list of just the filenames for a set comparison later:
        filenames_to_copy = []
        for filename in files_to_copy:
            filenames_to_copy.append(os.path.basename(filename))

        # Base filename for entirely new files:
        new_files = []

        # Base filenames for pre-existing files we copied over:
        copied_files = []

        # Base filenames that need to be removed by the caller:
        old_files = []

        for copy_me in files_to_copy:
            base_filename = os.path.basename(copy_me)
            dest_path = os.path.join(dest_dir, base_filename)

            if not os.path.exists(dest_path):
                print("   adding: %s" % base_filename)
                new_files.append(base_filename)
            else:
                print("   copying: %s" % base_filename)


            cmd = "cp %s %s" % (copy_me, dest_path)
            run_command(cmd)

        # Track filenames that will need to be deleted by the caller.
        # Could be git or CVS.
        for filename in os.listdir(dest_dir):
            if filename not in PROTECTED_BUILD_SYS_FILES and \
                    filename not in filenames_to_copy:
                print("   deleting: %s" % filename)
                old_files.append(filename)

        return new_files, copied_files, old_files


class YumRepoReleaser(Releaser):
    """
    A releaser which will rsync down a yum repo, build the desired packages,
    plug them in, update the repodata, and push the yum repo back out.

    Building of the packages is done via mock.

    WARNING: This will not work in all
    situations, depending on the current OS, and the mock target you
    are attempting to use.
    """
    REQUIRED_CONFIG = ['rsync', 'builder']

    def __init__(self, name=None, version=None, tag=None, build_dir=None,
            pkg_config=None, global_config=None, user_config=None,
            target=None, releaser_config=None):
        Releaser.__init__(self, name, version, tag, build_dir, pkg_config,
                global_config, user_config, target, releaser_config)

        self.build_dir = build_dir

        # Use the builder from the release target, rather than the default
        # one defined for this git repo or sub-package:
        self.builder = create_builder(name, tag,
                version, None, pkg_config,
                build_dir, global_config, user_config, self.builder_args,
                builder_class=self.releaser_config.get(self.target, 'builder'))

    def release(self, dry_run=False):
        # Should this run?
        self.builder.tgz()
        self.builder.srpm()
        self.builder._rpm()
        self.builder.cleanup()

        rsync_location = self.releaser_config.get(self.target, 'rsync')
        if RSYNC_USERNAME in os.environ:
            print("%s set, using rsync username: %s" % (RSYNC_USERNAME,
                    os.environ[RSYNC_USERNAME]))
            rsync_location = "%s@%s" % (os.environ[RSYNC_USERNAME], rsync_location)
        # Make a temp directory to sync the existing repo contents into:
        yum_temp_dir = mkdtemp(dir=self.build_dir, prefix="tito-yumrepo-")
        print("Syncing yum repo: %s -> %s" % (rsync_location, yum_temp_dir))
        output = run_command("rsync -avtz %s %s" % (rsync_location, yum_temp_dir))
        debug(output)

        for artifact in self.builder.artifacts:
            if artifact.endswith(".rpm") and not artifact.endswith(".src.rpm"):
                copy(artifact, yum_temp_dir)
                print("Copied %s to yum repo." % artifact)

        # TODO: should we clean up old versions of these packages in the repo?

        os.chdir(yum_temp_dir)
        print("Refreshing yum repodata...")
        output = run_command("createrepo ./")
        debug(output)

        print("Syncing yum repository back to: %s" % rsync_location)
        # TODO: configurable rsync options?
        output = run_command("rsync -avtz --delete %s/ %s" %
                (yum_temp_dir, rsync_location))
        debug(output)

        # TODO: Cleanup
        #rmtree(yum_temp_dir)


class FedoraGitReleaser(Releaser):

    REQUIRED_CONFIG = ['branches']

    def __init__(self, name=None, version=None, tag=None, build_dir=None,
            pkg_config=None, global_config=None, user_config=None,
            target=None, releaser_config=None):
        Releaser.__init__(self, name, version, tag, build_dir, pkg_config,
                global_config, user_config, target, releaser_config)

        self.git_branches = \
            self.releaser_config.get(self.target, "branches").split(" ")

    def release(self, dry_run=False):
        self.dry_run = dry_run
        self._git_release()

    def cleanup(self):
        debug("Cleaning up [%s]" % self.cvs_package_workdir)
        run_command("rm -rf %s" % self.cvs_package_workdir)

    def _git_release(self):

        commands.getoutput("mkdir -p %s" % self.cvs_workdir)
        os.chdir(self.cvs_workdir)
        user = fedora_cert.read_user_cert()
        pyfedpkg.clone(self.project_name, user, self.cvs_workdir)

        project_checkout = os.path.join(self.cvs_workdir, self.project_name)
        os.chdir(project_checkout)
        run_command("fedpkg switch-branch %s" % self.git_branches[0])

        self.builder.tgz()

        self._git_sync_files(project_checkout)
        self._git_upload_sources(project_checkout)
        self._git_user_confirm_commit(project_checkout)

    def _git_user_confirm_commit(self, project_checkout):
        """ Prompt user if they wish to proceed with commit. """
        print("")
        text = "Running 'git diff' in: %s" % project_checkout
        print("#" * len(text))
        print(text)
        print("#" * len(text))
        print("")

        main_branch = self.git_branches[0]

        os.chdir(project_checkout)
        (status, diff_output) = commands.getstatusoutput("git diff --cached")

        if diff_output.strip() == "":
            print("No changes in main branch, skipping commit for: %s" % main_branch)
        else:
            print(diff_output)
            print("")
            print("##### Please review the above diff #####")
            answer = raw_input("Do you wish to proceed with commit? [y/n] ")
            if answer.lower() not in ['y', 'yes', 'ok', 'sure']:
                print("Fine, you're on your own!")
                self.cleanup()
                sys.exit(1)

            print("Proceeding with commit.")
            cmd = 'fedpkg commit -m "Update %s to %s"' % (self.project_name,
                    self.builder.build_version)
            debug("git commit command: %s" % cmd)
            print
            os.chdir(self.cvs_package_workdir)
            output = run_command(cmd)

        cmd = "fedpkg push"
        if self.dry_run:
            self.print_dry_run_warning(cmd)
        else:
            # Push
            print(cmd)
            run_command(cmd)

        self._build()

        for branch in self.git_branches[1:]:
            print("Merging %s into %s" % (main_branch, branch))
            run_command("fedpkg switch-branch %s" % branch)
            run_command("git merge %s" % main_branch)

            cmd = "git push origin %s:%s" % (branch, branch)
            if self.dry_run:
                self.print_dry_run_warning(cmd)
            else:
                print(cmd)
                run_command(cmd)

            self._build()
            print

    def _build(self):
        """ Submit a Fedora build from current directory. """
        build_cmd = "fedpkg build --nowait"

        if self.dry_run:
            self.print_dry_run_warning(build_cmd)
            return

        print("Submitting build: %s" % build_cmd)
        (status, output) = commands.getstatusoutput(build_cmd)
        if status > 0:
            if "already been built" in output:
                print("Build has been submitted previously, continuing...")
            else:
                sys.stderr.write("ERROR: Unable to submit build.")
                sys.stderr.write("  Status code: %s" % status)
                sys.stderr.write("  Output: %s" % output)
                sys.exit(1)

    def _git_upload_sources(self, project_checkout):
        """
        Upload any tarballs to the lookaside directory. (if necessary)
        Uses the "fedpkg new-sources" command
        """
        if not self.builder.sources:
            debug("No sources need to be uploaded.")
            return

        print("Uploading sources to lookaside:")
        os.chdir(project_checkout)
        cmd = 'fedpkg new-sources %s' % (" ".join(self.builder.sources))
        debug(cmd)

        if self.dry_run:
            self.print_dry_run_warning(cmd)
            return

        output = run_command(cmd)
        debug(output)

    def _git_sync_files(self, project_checkout):
        """
        Copy files from our git into each git build branch and add them.

        A list of safe files is used to protect critical files both from
        being overwritten by a git file of the same name, as well as being
        deleted after.
        """

        # Build the list of all files we will copy:
        debug("Searching for files to copy to build system git:")
        files_to_copy = self._list_files_to_copy()

        os.chdir(project_checkout)

        # TODO:
        #for branch in self.cvs_branches:
        #print("Syncing files with git branch [%s]" % branch)
        new, copied, old =  \
                self._sync_files(files_to_copy, project_checkout)

        os.chdir(project_checkout)

        # Git add everything:
        for add_file in (new + copied):
            commands.getstatusoutput("git add %s" % add_file)

        # Cleanup obsolete files:
        for cleanup_file in old:
            # Can't delete via full path, must not chdir:
            run_command("git rm %s" % cleanup_file)


class CvsReleaser(Releaser):

    REQUIRED_CONFIG = ['cvsroot', 'branches']

    def __init__(self, name=None, version=None, tag=None, build_dir=None,
            pkg_config=None, global_config=None, user_config=None,
            target=None, releaser_config=None):
        Releaser.__init__(self, name, version, tag, build_dir, pkg_config,
                global_config, user_config, target, releaser_config)

        # Configure CVS variables if possible. Will check later that
        # they're actually defined if the user requested CVS work be done.
        if self.releaser_config.has_option(target, "cvsroot"):
            self.cvs_root = self.releaser_config.get(target, "cvsroot")
            debug("cvs_root = %s" % self.cvs_root)
        if self.releaser_config.has_option(target, "branches"):
            self.cvs_branches = \
                self.releaser_config.get(target, "branches").split(" ")

    def release(self, dry_run=False):
        self.dry_run = dry_run

        self._cvs_release()

    def cleanup(self):
        debug("Cleaning up [%s]" % self.cvs_package_workdir)
        run_command("rm -rf %s" % self.cvs_package_workdir)

    def _cvs_release(self):
        """
        Sync spec file/patches with CVS, create tags, and submit to brew/koji.
        """

        self._verify_cvs_module_not_already_checked_out()

        print("Building release in CVS...")
        commands.getoutput("mkdir -p %s" % self.cvs_workdir)
        debug("cvs_branches = %s" % self.cvs_branches)

        self.cvs_checkout_module()
        self.cvs_verify_branches_exist()

        # Get the list of all sources from the builder:
        self.builder.tgz()

        self.cvs_sync_files()

        # Important step here, ends up populating several important members
        # on the builder object so some of the below lines will not work
        # if moved above this one.
        self.cvs_upload_sources()

        self._cvs_user_confirm_commit()

        self._cvs_make_tag()
        self._cvs_make_build()

    def _verify_cvs_module_not_already_checked_out(self):
        """ Exit if CVS module appears to already be checked out. """
        # Make sure the cvs checkout directory doesn't already exist:
        cvs_co_dir = os.path.join(self.cvs_workdir, self.project_name)
        if os.path.exists(cvs_co_dir):
            error_out("CVS workdir exists, please remove and try again: %s"
                    % cvs_co_dir)

    def cvs_checkout_module(self):
        print("Checking out cvs module [%s]" % self.project_name)
        os.chdir(self.cvs_workdir)
        run_command("cvs -d %s co %s" % (self.cvs_root, self.project_name))
        for i in range(0, len(self.cvs_branches)):
            if self.cvs_branches[i].find('/') > -1:
                debug("Checking out zstream branch %s" % self.cvs_branches[i])
                (base, zstream) = self.cvs_branches[i].split('/')
                run_command("make -C %s zstreams" %
                        (os.path.join(self.cvs_package_workdir, base)))
                self.cvs_branches[i] = zstream

    def cvs_verify_branches_exist(self):
        """ Check that CVS checkout contains the branches we expect. """
        os.chdir(self.cvs_package_workdir)
        for branch in self.cvs_branches:
            if not os.path.exists(os.path.join(self.cvs_workdir,
                self.project_name, branch)):
                error_out("%s CVS checkout is missing branch: %s" %
                        (self.project_name, branch))

    def cvs_sync_files(self):
        """
        Copy files from git into each CVS branch and add them. Extra files
        found in CVS will then be deleted.

        A list of CVS safe files is used to protect critical files both from
        being overwritten by a git file of the same name, as well as being
        deleted after.
        """

        # Build the list of all files we will copy from git to CVS.
        debug("Searching for git files to copy to CVS:")
        files_to_copy = self._list_files_to_copy()

        for branch in self.cvs_branches:
            print("Syncing files with CVS branch [%s]" % branch)
            branch_dir = os.path.join(self.cvs_workdir, self.project_name,
                    branch)

            new, copied, old =  \
                    self._sync_files(files_to_copy, branch_dir)

            os.chdir(branch_dir)

            # For entirely new files we need to cvs add:
            for add_file in new:
                commands.getstatusoutput("cvs add %s" % add_file)

            # Cleanup obsolete files:
            for cleanup_file in old:
                # Can't delete via full path, must not chdir:
                run_command("cvs rm -Rf %s" % cleanup_file)

    def cvs_upload_sources(self):
        """
        Upload any tarballs to the CVS lookaside directory. (if necessary)
        Uses the "make new-sources" target in common.
        """
        if not self.builder.sources:
            debug("No sources need to be uploaded.")
            return

        print("Uploading sources to dist-cvs lookaside:")
        for branch in self.cvs_branches:
            branch_dir = os.path.join(self.cvs_workdir, self.project_name,
                    branch)
            os.chdir(branch_dir)
            cmd = 'make new-sources FILES="%s"' % (" ".join(self.builder.sources))
            debug(cmd)
            if self.dry_run:
                self.print_dry_run_warning(cmd)
                return

            output = run_command(cmd)
            debug(output)

    def _cvs_user_confirm_commit(self):
        """ Prompt user if they wish to proceed with commit. """
        print("")
        text = "Running 'cvs diff -u' in: %s" % self.cvs_package_workdir
        print("#" * len(text))
        print(text)
        print("#" * len(text))
        print("")

        os.chdir(self.cvs_package_workdir)
        (status, diff_output) = commands.getstatusoutput("cvs diff -u")
        print(diff_output)

        print("")
        print("##### Please review the above diff #####")
        answer = raw_input("Do you wish to proceed with commit? [y/n] ")
        if answer.lower() not in ['y', 'yes', 'ok', 'sure']:
            print("Fine, you're on your own!")
            self.cleanup()
            sys.exit(1)

        self._cvs_user_confirm_commit_msg(diff_output)

    def _cvs_user_confirm_commit_msg(self, diff_output):

        fd, name = tempfile.mkstemp()
        debug("Storing CVS commit message in temp file: %s" % name)
        os.write(fd, "Update %s to %s\n" % (self.project_name,
            self.builder.build_version))
        # Write out Resolves line for all bugzillas we see in commit diff:
        for line in extract_bzs(diff_output):
            os.write(fd, line + "\n")

        print("")
        print("##### CVS commit message: #####")
        print("")

        os.lseek(fd, 0, 0)
        file = os.fdopen(fd)
        for line in file.readlines():
            print line
        file.close()

        print("")
        print("###############################")
        print("")
        answer = raw_input("Would you like to edit this commit message? [y/n] ")
        if answer.lower() in ['y', 'yes', 'ok', 'sure']:
            debug("Opening editor for user to edit commit message in: %s" % name)
            editor = 'vi'
            if "EDITOR" in os.environ:
                editor = os.environ["EDITOR"]
            subprocess.call([editor, name])

        cmd = 'cvs commit -F %s' % name
        debug("CVS commit command: %s" % cmd)
        if self.dry_run:
            self.print_dry_run_warning(cmd)
        else:
            print("Proceeding with commit.")
            os.chdir(self.cvs_package_workdir)
            output = run_command(cmd)

        os.unlink(name)

    def _cvs_make_tag(self):
        """ Create a CVS tag based on what we just committed. """
        os.chdir(self.cvs_package_workdir)
        cmd = "make tag"
        if self.dry_run:
            self.print_dry_run_warning(cmd)
            return
        print("Creating CVS tags...")
        for branch in self.cvs_branches:
            branch_dir = os.path.join(self.cvs_workdir, self.project_name,
                    branch)
            os.chdir(branch_dir)
            (status, output) = commands.getstatusoutput(cmd)
            print(output)
            if status > 1:
                self.cleanup()
                sys.exit(1)

    def _cvs_make_build(self):
        """ Build srpm and submit to build system. """
        cmd = "BUILD_FLAGS=--nowait make build"
        if self.dry_run:
            self.print_dry_run_warning(cmd)
            return
        os.chdir(self.cvs_package_workdir)
        print("Submitting CVS builds...")
        for branch in self.cvs_branches:
            branch_dir = os.path.join(self.cvs_workdir, self.project_name,
                    branch)
            os.chdir(branch_dir)
            output = run_command(cmd)
            print(output)


class KojiReleaser(Releaser):

    def __init__(self, name=None, version=None, tag=None, build_dir=None,
            pkg_config=None, global_config=None, user_config=None,
            target=None, releaser_config=None):
        Releaser.__init__(self, name, version, tag, build_dir, pkg_config,
                global_config, user_config, target, releaser_config)

        self.only_tags = self.builder.only_tags
        self.scratch = self.builder.scratch

    def release(self, dry_run=False):
        self.dry_run = dry_run

        if self._can_build_in_koji():
            self._koji_release()

    def _can_build_in_koji(self):
        """
        Return True if this repo and branch are configured to auto build in
        any Koji tags.
        """
        if not self.builder.config.has_section("koji"):
            debug("No 'koji' section found in tito.props.")
            return False

        if not self.builder.config.has_option("koji", "autobuild_tags"):
            debug("Cannot build in Koji, no autobuild_tags "
                "defined in tito.props.")
            return False

        return True

    def _koji_release(self):
        """
        Lookup autobuild Koji tags from global config, create srpms with
        appropriate disttags, and submit builds to Koji.
        """
        autobuild_tags = self.builder.config.get("koji", "autobuild_tags")
        print("Building release in Koji...")
        debug("Koji tags: %s" % autobuild_tags)
        koji_tags = autobuild_tags.strip().split(" ")

        koji_opts = DEFAULT_KOJI_OPTS
        if 'KOJI_OPTIONS' in self.builder.user_config:
            koji_opts = self.builder.user_config['KOJI_OPTIONS']

        if self.scratch:
            koji_opts = ' '.join([koji_opts, '--scratch'])

        # TODO: need to re-do this metaphor to use release targets instead:
        for koji_tag in koji_tags:
            if self.only_tags and koji_tag not in self.only_tags:
                continue
            # Lookup the disttag configured for this Koji tag:
            disttag = self.builder.config.get(koji_tag, "disttag")
            if self.builder.config.has_option(koji_tag, "whitelist"):
                # whitelist implies only those packages can be built to the
                # tag,regardless if blacklist is also defined.
                if not self.__is_whitelisted(koji_tag):
                    print("WARNING: %s not specified in whitelist for %s" % (
                        self.project_name, koji_tag))
                    print("   Package *NOT* submitted to Koji.")
                    continue
            elif self.__is_blacklisted(koji_tag):
                print("WARNING: %s specified in blacklist for %s" % (
                    self.project_name, koji_tag))
                print("   Package *NOT* submitted to Koji.")
                continue

            # Getting tricky here, normally Builder's are only used to
            # create one rpm and then exit. Here we're going to try
            # to run multiple srpm builds:
            self.builder.srpm(dist=disttag, reuse_cvs_checkout=True)

            self._submit_build("koji", koji_opts, koji_tag)

    def __is_whitelisted(self, koji_tag):
        """ Return true if package is whitelisted in tito.props"""
        return self.builder.config.has_option(koji_tag, "whitelist") and \
            self.project_name in self.builder.config.get(koji_tag,
                        "whitelist").strip().split(" ")

    def __is_blacklisted(self, koji_tag):
        """ Return true if package is blacklisted in tito.props"""
        return self.builder.config.has_option(koji_tag, "blacklist") and \
            self.project_name in self.builder.config.get(koji_tag,
                        "blacklist").strip().split(" ")

    def list_tags(self):
        """ Print tags to which we build this package. """
        autobuild_tags = self.builder.config.get("koji", "autobuild_tags")
        koji_tags = autobuild_tags.strip().split(" ")
        for koji_tag in koji_tags:
            if self.__is_whitelisted(koji_tag):
                if 'DEBUG' in os.environ:
                    koji_tag += ' whitelisted'
            elif self.__is_blacklisted(koji_tag):
                if 'DEBUG' in os.environ:
                    koji_tag += ' blacklisted'
                else:
                    continue
            print koji_tag

    def _submit_build(self, executable, koji_opts, tag):
        """ Submit srpm to brew/koji. """
        cmd = "%s %s %s %s" % (executable, koji_opts, tag, self.builder.srpm_location)
        print("\nSubmitting build with: %s" % cmd)

        if self.dry_run:
            self.print_dry_run_warning(cmd)
            return

        output = run_command(cmd)
        print(output)







