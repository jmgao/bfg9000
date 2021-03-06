import mock
import ntpath
import os
import re
import sys
import unittest
from collections import namedtuple

from .common import BuiltinTest
from ... import make_env

from bfg9000 import file_types, options as opts
from bfg9000.builtins import packages
from bfg9000.exceptions import PackageResolutionError, PackageVersionError
from bfg9000.file_types import Directory, HeaderDirectory
from bfg9000.packages import CommonPackage, Framework
from bfg9000.path import abspath
from bfg9000.platforms import platform_name
from bfg9000.versioning import SpecifierSet, Version

if sys.version_info >= (3,):
    open_name = 'builtins.open'
else:
    open_name = '__builtin__.open'


# Fix the mock package's mock_open function to work with iter(); note: this is
# already fixed in Python 3.7.1's unittest.mock.
def mock_open(*args, **kwargs):
    mo = mock.mock_open(*args, **kwargs)
    handle = mo.return_value
    handle.__iter__.side_effect = lambda: iter(handle.readlines.side_effect())
    return mo


def mock_which(*args, **kwargs):
    return [os.path.abspath('/command')]


def mock_execute(args, **kwargs):
    if args[-1] == '--version':
        return ('gcc (Ubuntu 5.4.0-6ubuntu1~16.04.9) 5.4.0 20160609\n' +
                'Copyright (C) 2015 Free Software Foundation, Inc.\n')
    elif args[-1] == '-Wl,--version':
        return '', '/usr/bin/ld --version\n'
    elif args[-1] == '-print-search-dirs':
        return 'libraries: =/usr/lib\n'
    elif args[-1] == '-print-sysroot':
        return '/\n'
    elif args[-1] == '--verbose':
        return 'SEARCH_DIR("/usr")\n'
    elif args[-1] == '/?':
        return ('Microsoft (R) C/C++ Optimizing Compiler Version ' +
                '19.12.25831 for x86')
    elif args[-1] == '--modversion':
        return '1.2.3\n'


class TestFramework(unittest.TestCase):
    def test_framework(self):
        env = make_env('darwin')
        self.assertEqual(
            packages.framework(env, 'name'),
            CommonPackage('name', env.target_platform.object_format,
                          link_options=opts.option_list(opts.lib(
                              Framework('name')
                          )))
        )

    def test_framework_suffix(self):
        env = make_env('darwin')
        self.assertEqual(
            packages.framework(env, 'name', 'suffix'),
            CommonPackage('name,suffix',
                          env.target_platform.object_format,
                          link_options=opts.option_list(opts.lib(
                              Framework('name', 'suffix')
                          )))
        )

    def test_frameworks_unsupported(self):
        env = make_env('linux')
        with self.assertRaises(PackageResolutionError):
            packages.framework(env, 'name')

        with self.assertRaises(PackageResolutionError):
            packages.framework(env, 'name', 'suffix')


class TestPackage(BuiltinTest):
    def test_name(self):
        with mock.patch('bfg9000.shell.execute', mock_execute), \
             mock.patch('bfg9000.shell.which', mock_which):  # noqa
            pkg = packages.package(self.env, 'name')
            self.assertEqual(pkg.name, 'name')
            self.assertEqual(pkg.version, Version('1.2.3'))
            self.assertEqual(pkg.specifier, SpecifierSet())
            self.assertEqual(pkg.static, False)

    def test_version(self):
        with mock.patch('bfg9000.shell.execute', mock_execute), \
             mock.patch('bfg9000.shell.which', mock_which):  # noqa
            pkg = packages.package(self.env, 'name', version='>1.0')
            self.assertEqual(pkg.name, 'name')
            self.assertEqual(pkg.version, Version('1.2.3'))
            self.assertEqual(pkg.specifier, SpecifierSet('>1.0'))
            self.assertEqual(pkg.static, False)

    def test_lang(self):
        with mock.patch('bfg9000.shell.execute', mock_execute), \
             mock.patch('bfg9000.shell.which', mock_which):  # noqa
            pkg = packages.package(self.env, 'name', lang='c++')
            self.assertEqual(pkg.name, 'name')
            self.assertEqual(pkg.version, Version('1.2.3'))
            self.assertEqual(pkg.specifier, SpecifierSet())
            self.assertEqual(pkg.static, False)

    def test_kind(self):
        with mock.patch('bfg9000.shell.execute', mock_execute), \
             mock.patch('bfg9000.shell.which', mock_which):  # noqa
            pkg = packages.package(self.env, 'name', kind='static')
            self.assertEqual(pkg.name, 'name')
            self.assertEqual(pkg.version, Version('1.2.3'))
            self.assertEqual(pkg.specifier, SpecifierSet())
            self.assertEqual(pkg.static, True)

    def test_invalid_kind(self):
        with self.assertRaises(ValueError):
            packages.package(self.env, 'name', kind='bad')


class TestBoostPackage(unittest.TestCase):
    def test_boost_version(self):
        data = '#define BOOST_LIB_VERSION "1_23_4"\n'
        with mock.patch(open_name, mock_open(read_data=data)):
            hdr = HeaderDirectory(abspath('path'))
            self.assertEqual(packages._boost_version(hdr, SpecifierSet('')),
                             Version('1.23.4'))

    def test_boost_version_too_old(self):
        data = '#define BOOST_LIB_VERSION "1_23_4"\n'
        with mock.patch(open_name, mock_open(read_data=data)):
            hdr = HeaderDirectory(abspath('path'))
            with self.assertRaises(PackageVersionError):
                packages._boost_version(hdr, SpecifierSet('>=1.30'))

    def test_boost_version_cant_parse(self):
        data = 'foobar\n'
        with mock.patch(open_name, mock_open(read_data=data)):
            hdr = HeaderDirectory(abspath('path'))
            with self.assertRaises(PackageVersionError):
                packages._boost_version(hdr, SpecifierSet(''))

    def test_posix(self):
        env = make_env('linux', clear_variables=True)

        def mock_exists(x):
            if ( re.search(r'[/\\]boost[/\\]version.hpp$', x) or
                 re.search(r'[/\\]libboost_thread', x) or
                 x in ['/usr/include', '/usr/lib']):
                return True
            return False

        with mock.patch('bfg9000.builtins.packages._boost_version',
                        return_value=Version('1.23')), \
             mock.patch('bfg9000.shell.which', return_value=['command']), \
             mock.patch('bfg9000.shell.execute', mock_execute), \
             mock.patch('os.path.exists', mock_exists):  # noqa
            pkg = packages.boost_package(env, 'thread')
            self.assertEqual(pkg.name, 'boost(thread)')
            self.assertEqual(pkg.version, Version('1.23'))

    def test_windows_default_location(self):
        env = make_env('windows', clear_variables=True)
        boost_incdir = r'C:\Boost\include\boost-1.23'

        def mock_walk(top):
            yield (top,) + (
                [('boost-1.23', ntpath.join(top, 'boost-1.23'))],
                []
            )

        def mock_execute(*args, **kwargs):
            if args[0][1] == '/?':
                return 'cl.exe'
            raise ValueError()

        def mock_exists(x):
            if re.search(r'[/\\]boost[/\\]version.hpp$', x):
                return True
            return False

        with mock.patch('bfg9000.builtins.find._walk_flat', mock_walk), \
             mock.patch('bfg9000.builtins.packages._boost_version',
                        return_value=Version('1.23')), \
             mock.patch('bfg9000.shell.which', return_value=['command']), \
             mock.patch('bfg9000.shell.execute', mock_execute), \
             mock.patch('os.path.exists', mock_exists):  # noqa
            pkg = packages.boost_package(env, 'thread')
            self.assertEqual(pkg.name, 'boost(thread)')
            self.assertEqual(pkg.version, Version('1.23'))
            self.assertEqual(pkg._compile_options, opts.option_list(
                opts.include_dir(HeaderDirectory(abspath(boost_incdir)))
            ))
            self.assertEqual(pkg._link_options, opts.option_list(
                opts.lib_dir(Directory(abspath(r'C:\Boost\lib')))
            ))


class TestSystemExecutable(BuiltinTest):
    def test_name(self):
        with mock.patch('bfg9000.builtins.packages.which', mock_which):
            self.assertEqual(
                packages.system_executable(self.env, 'name'),
                file_types.Executable(abspath('/command'),
                                      self.env.target_platform.object_format)
            )

    def test_format(self):
        with mock.patch('bfg9000.builtins.packages.which', mock_which):
            self.assertEqual(
                packages.system_executable(self.env, 'name', 'format'),
                file_types.Executable(abspath('/command'), 'format')
            )
