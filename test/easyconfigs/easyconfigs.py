##
# Copyright 2013-2018 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
Unit tests for easyconfig files.

@author: Kenneth Hoste (Ghent University)
"""

import copy
import glob
import os
import re
import shutil
import sys
import tempfile
from distutils.version import LooseVersion
from vsc.utils import fancylogger
from vsc.utils.missing import nub
from unittest import TestCase, TestLoader, main

import easybuild.main as main
import easybuild.tools.options as eboptions
from easybuild.easyblocks.generic.configuremake import ConfigureMake
from easybuild.framework.easyblock import EasyBlock
from easybuild.framework.easyconfig.default import DEFAULT_CONFIG
from easybuild.framework.easyconfig.format.format import DEPENDENCY_PARAMETERS
from easybuild.framework.easyconfig.easyconfig import EasyConfig
from easybuild.framework.easyconfig.easyconfig import get_easyblock_class, letter_dir_for, resolve_template
from easybuild.framework.easyconfig.parser import EasyConfigParser, fetch_parameters_from_easyconfig
from easybuild.framework.easyconfig.tools import check_sha256_checksums, dep_graph, get_paths_for, process_easyconfig
from easybuild.tools import config
from easybuild.tools.build_log import EasyBuildError
from easybuild.tools.config import build_option
from easybuild.tools.filetools import change_dir, write_file
from easybuild.tools.module_naming_scheme import GENERAL_CLASS
from easybuild.tools.module_naming_scheme.easybuild_mns import EasyBuildMNS
from easybuild.tools.module_naming_scheme.utilities import det_full_ec_version
from easybuild.tools.modules import modules_tool
from easybuild.tools.robot import check_conflicts, resolve_dependencies
from easybuild.tools.run import run_cmd
from easybuild.tools.options import set_tmpdir


# indicates whether all the single tests are OK,
# and that bigger tests (building dep graph, testing for conflicts, ...) can be run as well
# other than optimizing for time, this also helps to get around problems like http://bugs.python.org/issue10949
single_tests_ok = True


class EasyConfigTest(TestCase):
    """Baseclass for easyconfig testcases."""

    # initialize configuration (required for e.g. default modules_tool setting)
    eb_go = eboptions.parse_options()
    config.init(eb_go.options, eb_go.get_options_by_section('config'))
    build_options = {
        'check_osdeps': False,
        'external_modules_metadata': {},
        'force': True,
        'optarch': 'test',
        'robot_path': get_paths_for("easyconfigs")[0],
        'silent': True,
        'suffix_modules_path': GENERAL_CLASS,
        'valid_module_classes': config.module_classes(),
        'valid_stops': [x[0] for x in EasyBlock.get_steps()],
    }
    config.init_build_options(build_options=build_options)
    set_tmpdir()
    del eb_go

    # put dummy 'craype-test' module in place, which is required for parsing easyconfigs using Cray* toolchains
    TMPDIR = tempfile.mkdtemp()
    os.environ['MODULEPATH'] = TMPDIR
    write_file(os.path.join(TMPDIR, 'craype-test'), '#%Module\n')

    log = fancylogger.getLogger("EasyConfigTest", fname=False)

    # make sure a logger is present for main
    main._log = log
    ordered_specs = None
    parsed_easyconfigs = []

    def process_all_easyconfigs(self):
        """Process all easyconfigs and resolve inter-easyconfig dependencies."""
        # all available easyconfig files
        easyconfigs_path = get_paths_for("easyconfigs")[0]
        specs = glob.glob('%s/*/*/*.eb' % easyconfigs_path)

        # parse all easyconfigs if they haven't been already
        if not self.parsed_easyconfigs:
            for spec in specs:
                self.parsed_easyconfigs.extend(process_easyconfig(spec))

        # filter out external modules
        for ec in self.parsed_easyconfigs:
            for dep in ec['dependencies'][:]:
                if dep.get('external_module', False):
                    ec['dependencies'].remove(dep)

        self.ordered_specs = resolve_dependencies(self.parsed_easyconfigs, modules_tool(), retain_all_deps=True)

    def test_dep_graph(self):
        """Unit test that builds a full dependency graph."""
        # pygraph dependencies required for constructing dependency graph are not available prior to Python 2.6
        if LooseVersion(sys.version) >= LooseVersion('2.6') and single_tests_ok:
            # temporary file for dep graph
            (hn, fn) = tempfile.mkstemp(suffix='.dot')
            os.close(hn)

            if self.ordered_specs is None:
                self.process_all_easyconfigs()

            dep_graph(fn, self.ordered_specs)

            try:
                os.remove(fn)
            except OSError, err:
                log.error("Failed to remove %s: %s" % (fn, err))
        else:
            print "(skipped dep graph test)"

    def test_conflicts(self):
        """Check whether any conflicts occur in software dependency graphs."""

        if not single_tests_ok:
            print "(skipped conflicts test)"
            return

        if self.ordered_specs is None:
            self.process_all_easyconfigs()

        self.assertFalse(check_conflicts(self.ordered_specs, modules_tool(), check_inter_ec_conflicts=False),
                         "No conflicts detected")

    def test_dep_versions_per_toolchain_generation(self):
        """
        Check whether there's only one dependency version per toolchain generation actively used.
        This is enforced to try and limit the chance of running into conflicts when multiple modules built with
        the same toolchain are loaded together.
        """
        if self.ordered_specs is None:
            self.process_all_easyconfigs()

        def get_deps_for(ec):
            """Get list of (direct) dependencies for specified easyconfig."""
            deps = []
            for dep in ec['ec']['dependencies']:
                dep_mod_name = dep['full_mod_name']
                deps.append((dep['name'], dep['version'], dep['versionsuffix'], dep_mod_name))
                res = [x for x in self.ordered_specs if x['full_mod_name'] == dep_mod_name]
                if len(res) == 1:
                    deps.extend(get_deps_for(res[0]))
                else:
                    raise EasyBuildError("Failed to find %s in ordered list of easyconfigs", dep_mod_name)

            return deps

        def check_dep_vars(dep, dep_vars):
            """Check whether available variants of a particular dependency are acceptable or not."""

            # 'guilty' until proven 'innocent'
            res = False

            # filter out binutils with empty versionsuffix which is used to build toolchain compiler
            if dep == 'binutils' and len(dep_vars) > 1:
                empty_vsuff_vars = [v for v in dep_vars.keys() if v.endswith('versionsuffix: ')]
                if len(empty_vsuff_vars) == 1:
                    dep_vars = dict((k, v) for (k, v) in dep_vars.items() if k != empty_vsuff_vars[0])

            # multiple variants of HTSlib is OK as long as they are deps for a matching version of BCFtools
            elif dep == 'HTSlib' and len(dep_vars) > 1:
                for key, ecs in dep_vars.items():
                    # filter out HTSlib variants that are only used as dependency for BCFtools with same version
                    htslib_ver = re.search('^version: (?P<ver>[^;]+);', key).group('ver')
                    if all(ec.startswith('BCFtools-%s-' % htslib_ver) for ec in ecs):
                        dep_vars.pop(key)

            # filter out FFTW and imkl with -serial versionsuffix which are used in non-MPI subtoolchains
            elif dep in ['FFTW', 'imkl']:
                serial_vsuff_vars = [v for v in dep_vars.keys() if v.endswith('versionsuffix: -serial')]
                if len(serial_vsuff_vars) == 1:
                    dep_vars = dict((k, v) for (k, v) in dep_vars.items() if k != serial_vsuff_vars[0])

            # for some dependencies, we allow exceptions for software that depends on a particular version,
            # as long as that's indicated by the versionsuffix
            elif dep in ['Boost', 'R'] and len(dep_vars) > 1:
                for key in dep_vars.keys():
                    dep_ver = re.search('^version: (?P<ver>[^;]+);', key).group('ver')
                    # filter out dep version if all easyconfig filenames using it include specific dep version
                    if all(re.search('-%s-%s' % (dep, dep_ver), v) for v in dep_vars[key]):
                        dep_vars.pop(key)
                    # always retain at least one dep variant
                    if len(dep_vars) == 1:
                        break

            # filter out Java 'wrapper'
            # i.e. if the version of one is a prefix of the version of the other one (e.g. 1.8 & 1.8.0_181)
            elif dep == 'Java' and len(dep_vars) == 2:
                key1, key2 = sorted(dep_vars.keys())
                ver1, ver2 = [k.split(';')[0] for k in [key1, key2]]
                if ver1.startswith(ver2):
                    dep_vars.pop(key2)
                elif ver2.startswith(ver1):
                    dep_vars.pop(key1)

            # filter out variants that are specific to a particular version of CUDA
            cuda_dep_vars = [v for v in dep_vars.keys() if '-CUDA' in v]
            if len(dep_vars) > len(cuda_dep_vars):
                for key in dep_vars.keys():
                    if re.search('; versionsuffix: .*-CUDA-[0-9.]+', key):
                        dep_vars.pop(key)

            # some software packages require an old version of a particular dependency
            old_dep_versions = {
                # libxc (CP2K & ABINIT require libxc 2.x or 3.x)
                'libxc': r'[23]\.',
            }
            if dep in old_dep_versions and len(dep_vars) > 1:
                for key in dep_vars.keys():
                    # filter out known old dependency versions
                    if re.search('^version: %s' % old_dep_versions[dep], key):
                        dep_vars.pop(key)

            # only single variant is always OK
            if len(dep_vars) == 1:
                res = True

            elif len(dep_vars) == 2 and dep in ['Python', 'Tkinter']:
                # for Python & Tkinter, it's OK to have on 2.x and one 3.x version
                v2_dep_vars = [x for x in dep_vars.keys() if x.startswith('version: 2.')]
                v3_dep_vars = [x for x in dep_vars.keys() if x.startswith('version: 3.')]
                if len(v2_dep_vars) == 1 and len(v3_dep_vars) == 1:
                    res = True

            # two variants is OK if one is for Python 2.x and the other is for Python 3.x (based on versionsuffix)
            elif len(dep_vars) == 2:
                py2_dep_vars = [x for x in dep_vars.keys() if '; versionsuffix: -Python-2.' in x]
                py3_dep_vars = [x for x in dep_vars.keys() if '; versionsuffix: -Python-3.' in x]
                if len(py2_dep_vars) == 1 and len(py3_dep_vars) == 1:
                    res = True

            return res

        # restrict to checking dependencies of easyconfigs using common toolchains (start with 2018a)
        # and GCCcore subtoolchain for common toolchains, starting with GCCcore 7.x
        for pattern in ['201[89][ab]', '20[2-9][0-9][ab]', 'GCCcore-[7-9]\.[0-9]']:
            all_deps = {}
            regex = re.compile('^.*-(?P<tc_gen>%s).*\.eb$' % pattern)

            # collect variants for all dependencies of easyconfigs that use a toolchain that matches
            for ec in self.ordered_specs:
                ec_file = os.path.basename(ec['spec'])
                res = regex.match(ec_file)
                if res:
                    tc_gen = res.group('tc_gen')
                    all_deps_tc_gen = all_deps.setdefault(tc_gen, {})
                    for dep_name, dep_ver, dep_versuff, dep_mod_name in get_deps_for(ec):
                        dep_variants = all_deps_tc_gen.setdefault(dep_name, {})
                        # a variant is defined by version + versionsuffix
                        variant = "version: %s; versionsuffix: %s" % (dep_ver, dep_versuff)
                        # keep track of which easyconfig this is a dependency
                        dep_variants.setdefault(variant, set()).add(ec_file)

            # check which dependencies have more than 1 variant
            multi_dep_vars, multi_dep_vars_msg = [], ''
            for tc_gen in sorted(all_deps.keys()):
                for dep in sorted(all_deps[tc_gen].keys()):
                    dep_vars = all_deps[tc_gen][dep]
                    if not check_dep_vars(dep, dep_vars):
                        multi_dep_vars.append(dep)
                        multi_dep_vars_msg += "\nfound %s variants of '%s' dependency " % (len(dep_vars), dep)
                        multi_dep_vars_msg += "in easyconfigs using '%s' toolchain generation\n* " % tc_gen
                        multi_dep_vars_msg += '\n* '.join("%s as dep for %s" % v for v in sorted(dep_vars.items()))
                        multi_dep_vars_msg += '\n'

            error_msg = "No multi-variant deps found for '%s' easyconfigs:\n%s" % (regex.pattern, multi_dep_vars_msg)
            self.assertFalse(multi_dep_vars, error_msg)

    def test_sanity_check_paths(self):
        """Make sure specified sanity check paths adher to the requirements."""

        if self.ordered_specs is None:
            self.process_all_easyconfigs()

        for ec in self.parsed_easyconfigs:
            ec_scp = ec['ec']['sanity_check_paths']
            if ec_scp != {}:
                # if sanity_check_paths is specified (i.e., non-default), it must adher to the requirements
                # both 'files' and 'dirs' keys, both with list values and with at least one a non-empty list
                error_msg = "sanity_check_paths for %s does not meet requirements: %s" % (ec['spec'], ec_scp)
                self.assertEqual(sorted(ec_scp.keys()), ['dirs', 'files'], error_msg)
                self.assertTrue(isinstance(ec_scp['dirs'], list), error_msg)
                self.assertTrue(isinstance(ec_scp['files'], list), error_msg)
                self.assertTrue(ec_scp['dirs'] or ec_scp['files'], error_msg)

    def test_easyconfig_locations(self):
        """Make sure all easyconfigs files are in the right location."""
        easyconfig_dirs_regex = re.compile(r'/easybuild/easyconfigs/[0a-z]/[^/]+$')
        topdir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        for (dirpath, _, filenames) in os.walk(topdir):
            # ignore git/svn dirs & archived easyconfigs
            if '/.git/' in dirpath or '/.svn/' in dirpath or '__archive__' in dirpath:
                continue
            # check whether list of .eb files is non-empty
            easyconfig_files = [fn for fn in filenames if fn.endswith('eb')]
            if easyconfig_files:
                # check whether path matches required pattern
                if not easyconfig_dirs_regex.search(dirpath):
                    # only exception: TEMPLATE.eb
                    if not (dirpath.endswith('/easybuild/easyconfigs') and filenames == ['TEMPLATE.eb']):
                        self.assertTrue(False, "List of easyconfig files in %s is empty: %s" % (dirpath, filenames))

    def check_sha256_checksums(self, changed_ecs):
        """Make sure changed easyconfigs have SHA256 checksums in place."""

        # list of software for which checksums can not be required,
        # e.g. because 'source' files need to be constructed manually
        whitelist = ['Kent_tools-*', 'MATLAB-*']

        # the check_sha256_checksums function (again) creates an EasyBlock instance
        # for easyconfigs using the Bundle easyblock, this is a problem because the 'sources' easyconfig parameter
        # is updated in place (sources for components are added the 'parent' sources) in Bundle's __init__;
        # therefore, we need to reset 'sources' to an empty list here if Bundle is used...
        for ec in changed_ecs:
            if ec['easyblock'] == 'Bundle':
                ec['sources'] = []

        # filter out deprecated easyconfigs
        retained_changed_ecs = []
        for ec in changed_ecs:
            if not ec['deprecated']:
                retained_changed_ecs.append(ec)

        checksum_issues = check_sha256_checksums(retained_changed_ecs, whitelist=whitelist)
        self.assertTrue(len(checksum_issues) == 0, "No checksum issues:\n%s" % '\n'.join(checksum_issues))

    def test_changed_files_pull_request(self):
        """Specific checks only done for the (easyconfig) files that were changed in a pull request."""

        # $TRAVIS_PULL_REQUEST should be a PR number, otherwise we're not running tests for a PR
        if re.match('^[0-9]+$', os.environ.get('TRAVIS_PULL_REQUEST', '(none)')):

            # target branch should be anything other than 'master';
            # usually is 'develop', but could also be a release branch like '3.7.x'
            travis_branch = os.environ.get('TRAVIS_BRANCH', None)
            if travis_branch and travis_branch != 'master':

                if not self.parsed_easyconfigs:
                    self.process_all_easyconfigs()

                # relocate to top-level directory of repository to run 'git diff' command
                top_dir = os.path.dirname(os.path.dirname(get_paths_for('easyconfigs')[0]))
                cwd = change_dir(top_dir)

                # get list of changed easyconfigs
                cmd = "git diff --name-only --diff-filter=AM %s...HEAD" % travis_branch
                out, ec = run_cmd(cmd, simple=False)
                changed_ecs_filenames = [os.path.basename(f) for f in out.strip().split('\n') if f.endswith('.eb')]
                print("List of changed easyconfig files in this PR: %s" % changed_ecs_filenames)

                change_dir(cwd)

                # grab parsed easyconfigs for changed easyconfig files
                changed_ecs = []
                for ec_fn in changed_ecs_filenames:
                    for ec in self.parsed_easyconfigs:
                        if os.path.basename(ec['spec']) == ec_fn:
                            changed_ecs.append(ec['ec'])
                            break

                # run checks on changed easyconfigs
                self.check_sha256_checksums(changed_ecs)

    def test_zzz_cleanup(self):
        """Dummy test to clean up global temporary directory."""
        shutil.rmtree(self.TMPDIR)

def template_easyconfig_test(self, spec):
    """Tests for an individual easyconfig: parsing, instantiating easyblock, check patches, ..."""

    # set to False, so it's False in case of this test failing
    global single_tests_ok
    prev_single_tests_ok = single_tests_ok
    single_tests_ok = False

    # parse easyconfig
    ecs = process_easyconfig(spec)
    if len(ecs) == 1:
        ec = ecs[0]['ec']
    else:
        self.assertTrue(False, "easyconfig %s does not contain blocks, yields only one parsed easyconfig" % spec)

    # check easyconfig file name
    expected_fn = '%s-%s.eb' % (ec['name'], det_full_ec_version(ec))
    msg = "Filename '%s' of parsed easyconfig matches expected filename '%s'" % (spec, expected_fn)
    self.assertEqual(os.path.basename(spec), expected_fn, msg)

    name, easyblock = fetch_parameters_from_easyconfig(ec.rawtxt, ['name', 'easyblock'])

    # make sure easyconfig file is in expected location
    expected_subdir = os.path.join('easybuild', 'easyconfigs', letter_dir_for(name), name)
    subdir = os.path.join(*spec.split(os.path.sep)[-5:-1])
    fail_msg = "Easyconfig file %s not in expected subdirectory %s" % (spec, expected_subdir)
    self.assertEqual(expected_subdir, subdir, fail_msg)

    # sanity check for software name, moduleclass
    self.assertEqual(ec['name'], name)
    self.assertTrue(ec['moduleclass'] in build_option('valid_module_classes'))

    # instantiate easyblock with easyconfig file
    app_class = get_easyblock_class(easyblock, name=name)

    # check that automagic fallback to ConfigureMake isn't done (deprecated behaviour)
    fn = os.path.basename(spec)
    error_msg = "%s relies on automagic fallback to ConfigureMake, should use easyblock = 'ConfigureMake' instead" % fn
    self.assertTrue(easyblock or not app_class is ConfigureMake, error_msg)

    app = app_class(ec)

    # more sanity checks
    self.assertTrue(name, app.name)
    self.assertTrue(ec['version'], app.version)

    # make sure that $root is not used, since it is not compatible with module files in Lua syntax
    res = re.findall('.*\$root.*', ec.rawtxt, re.M)
    error_msg = "Found use of '$root', not compatible with modules in Lua syntax, use '%%(installdir)s' instead: %s"
    self.assertFalse(res, error_msg % res)

    # make sure old GitHub urls for EasyBuild that include 'hpcugent' are no longer used
    old_urls = [
        'github.com/hpcugent/easybuild',
        'hpcugent.github.com/easybuild',
        'hpcugent.github.io/easybuild',
    ]
    for old_url in old_urls:
        self.assertFalse(old_url in ec.rawtxt, "Old URL '%s' not found in %s" % (old_url, spec))

    # make sure binutils is included as a build dep if toolchain is GCCcore
    if ec['toolchain']['name'] == 'GCCcore':
        # with 'Tarball' easyblock: only unpacking, no building; Eigen is also just a tarball
        requires_binutils = ec['easyblock'] not in ['Tarball'] and ec['name'] not in ['Eigen']

        # let's also exclude the very special case where the system GCC is used as GCCcore, and only apply this
        # exception to the dependencies of binutils (since we should eventually build a new binutils with GCCcore)
        if ec['toolchain']['version'] == 'system':
            binutils_complete_dependencies = ['M4', 'Bison', 'flex', 'help2man', 'zlib', 'binutils']
            requires_binutils &= bool(ec['name'] not in binutils_complete_dependencies)
            
        # if no sources/extensions/components are specified, it's just a bundle (nothing is being compiled)
        requires_binutils &= bool(ec['sources'] or ec['exts_list'] or ec.get('components'))

        if requires_binutils:
            dep_names = [d['name'] for d in ec['builddependencies']]
            self.assertTrue('binutils' in dep_names, "binutils is a build dep in %s: %s" % (spec, dep_names))

    # make sure all patch files are available
    specdir = os.path.dirname(spec)
    specfn = os.path.basename(spec)
    for patch in ec['patches']:
        if isinstance(patch, (tuple, list)):
            patch = patch[0]
        # only check actual patch files, not other files being copied via the patch functionality
        if patch.endswith('.patch'):
            patch_full = os.path.join(specdir, patch)
            msg = "Patch file %s is available for %s" % (patch_full, specfn)
            self.assertTrue(os.path.isfile(patch_full), msg)
    ext_patches = []
    for ext in ec['exts_list']:
        if isinstance(ext, (tuple, list)) and len(ext) == 3:
            self.assertTrue(isinstance(ext[2], dict), "3rd element of extension spec is a dictionary")
            for ext_patch in ext[2].get('patches', []):
                if isinstance(ext_patch, (tuple, list)):
                    ext_patch = ext_patch[0]
                # only check actual patch files, not other files being copied via the patch functionality
                if ext_patch.endswith('.patch'):
                    ext_patch_full = os.path.join(specdir, ext_patch)
                    msg = "Patch file %s is available for %s" % (ext_patch_full, specfn)
                    self.assertTrue(os.path.isfile(ext_patch_full), msg)

    # check whether all extra_options defined for used easyblock are defined
    extra_opts = app.extra_options()
    for key in extra_opts:
        self.assertTrue(key in app.cfg)

    app.close_log()
    os.remove(app.logfile)

    # dump the easyconfig file
    handle, test_ecfile = tempfile.mkstemp()
    os.close(handle)

    ec.dump(test_ecfile)
    dumped_ec = EasyConfigParser(test_ecfile).get_config_dict()
    os.remove(test_ecfile)

    # inject dummy values for templates that are only known at a later stage
    dummy_template_values = {
        'builddir': '/dummy/builddir',
        'installdir': '/dummy/installdir',
    }
    ec.template_values.update(dummy_template_values)

    ec_dict = ec.parser.get_config_dict()
    orig_toolchain = ec_dict['toolchain']
    for key in ec_dict:
        # skip parameters for which value is equal to default value
        orig_val = ec_dict[key]
        if key in DEFAULT_CONFIG and orig_val == DEFAULT_CONFIG[key][0]:
            continue
        if key in extra_opts and orig_val == extra_opts[key][0]:
            continue
        if key not in DEFAULT_CONFIG and key not in extra_opts:
            continue

        orig_val = resolve_template(ec_dict[key], ec.template_values)
        dumped_val = resolve_template(dumped_ec[key], ec.template_values)

        # take into account that dumped value for *dependencies may include hard-coded subtoolchains
        # if no easyconfig was found for the dependency with the 'parent' toolchain,
        # if may get resolved using a subtoolchain, which is then hardcoded in the dumped easyconfig
        if key in DEPENDENCY_PARAMETERS:
            # number of dependencies should remain the same
            self.assertEqual(len(orig_val), len(dumped_val))
            for orig_dep, dumped_dep in zip(orig_val, dumped_val):
                # name/version should always match
                self.assertEqual(orig_dep[:2], dumped_dep[:2])

                # 3rd value is versionsuffix;
                if len(dumped_dep) >= 3:
                    # if no versionsuffix was specified in original dep spec, then dumped value should be empty string
                    if len(orig_dep) >= 3:
                        self.assertEqual(dumped_dep[2], orig_dep[2])
                    else:
                        self.assertEqual(dumped_dep[2], '')

                # 4th value is toolchain spec
                if len(dumped_dep) >= 4:
                    if len(orig_dep) >= 4:
                        self.assertEqual(dumped_dep[3], orig_dep[3])
                    else:
                        # if a subtoolchain is specifed (only) in the dumped easyconfig,
                        # it should *not* be the same as the parent toolchain
                        self.assertNotEqual(dumped_dep[3], (orig_toolchain['name'], orig_toolchain['version']))

        else:
            self.assertEqual(orig_val, dumped_val)

    # cache the parsed easyconfig, to avoid that it is parsed again
    self.parsed_easyconfigs.append(ecs[0])

    # test passed, so set back to True
    single_tests_ok = True and prev_single_tests_ok


def suite():
    """Return all easyblock initialisation tests."""
    # dynamically generate a separate test for each of the available easyconfigs
    # define new inner functions that can be added as class methods to InitTest
    easyconfigs_path = get_paths_for('easyconfigs')[0]
    cnt = 0
    for (subpath, _, specs) in os.walk(easyconfigs_path, topdown=True):

        # ignore archived easyconfigs
        if '__archive__' in subpath:
            continue

        for spec in specs:
            if spec.endswith('.eb') and spec != 'TEMPLATE.eb':
                cnt += 1
                exec("def innertest(self): template_easyconfig_test(self, '%s')" % os.path.join(subpath, spec))
                innertest.__doc__ = "Test for parsing of easyconfig %s" % spec
                # double underscore so parsing tests are run first
                innertest.__name__ = "test__parse_easyconfig_%s" % spec
                setattr(EasyConfigTest, innertest.__name__, innertest)

    print "Found %s easyconfigs..." % cnt
    return TestLoader().loadTestsFromTestCase(EasyConfigTest)

if __name__ == '__main__':
    main()
