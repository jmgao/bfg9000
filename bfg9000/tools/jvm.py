import os
import re
from itertools import chain

from .common import BuildCommand, check_which
from .. import options as opts, safe_str, shell
from ..builtins.file_types import generated_file
from ..exceptions import PackageResolutionError
from ..file_types import *
from ..iterutils import flatten, iterate, uniques
from ..packages import Package
from ..path import Path, Root
from ..versioning import detect_version


class JvmBuilder(object):
    def __init__(self, env, langinfo, command, version_output):
        name = langinfo.var('compiler').lower()
        self.lang = langinfo.name
        self.object_format = 'jvm'

        flags_name = langinfo.var('cflags').lower()
        flags = shell.split(env.getvar(langinfo.var('cflags'), ''))

        jar_command = check_which(env.getvar('JAR', 'jar'), kind='jar builder')

        # The default command name to run JVM programs is (usually) the same as
        # the name of the language, so we'll just use that here as the default.
        run_name = langinfo.var('runner').lower()
        run_command = check_which(
            env.getvar(langinfo.var('runner'), self.lang),
            kind='{} runner'.format(self.lang)
        )

        self.brand = 'unknown'
        self.version = None
        if self.lang == 'java':
            try:
                # Get the brand from the run command (rather than the compile
                # command).
                output = env.execute(
                    run_command + ['-version'], stdout=shell.Mode.pipe,
                    stderr=shell.Mode.stdout
                )
                if re.search(r'Java\(TM\) (\w+ )?Runtime Environment', output):
                    self.brand = 'oracle'
                elif 'OpenJDK Runtime Environment' in output:
                    self.brand = 'openjdk'
            except (OSError, shell.CalledProcessError):
                pass
            self.version = detect_version(version_output)
        elif self.lang == 'scala':
            if 'EPFL' in version_output:
                self.brand = 'epfl'
                self.version = detect_version(version_output)

        self.compiler = JvmCompiler(self, env, name, command, flags_name,
                                    flags)
        self._linker = JarMaker(self, env, jar_command)
        self.packages = JvmPackageResolver(self, env, run_command)
        self.runner = JvmRunner(self, env, run_name, run_command)

    @staticmethod
    def check_command(env, command):
        return env.execute(command + ['-version'], stdout=shell.Mode.pipe,
                           stderr=shell.Mode.stdout)

    @property
    def flavor(self):
        return 'jvm'

    @property
    def family(self):
        return 'jvm'

    @property
    def can_dual_link(self):
        return False

    def linker(self, mode):
        if mode == 'static_library':
            raise ValueError('static linking not supported with {}'.format(
                self.brand
            ))
        if mode not in ('executable', 'shared_library'):
            raise KeyError(mode)
        return self._linker


class JvmCompiler(BuildCommand):
    def __init__(self, builder, env, name, command, flags_name, flags):
        BuildCommand.__init__(self, builder, env, name, name, command,
                              flags=(flags_name, flags))

    @property
    def brand(self):
        return self.builder.brand

    @property
    def version(self):
        return self.builder.version

    @property
    def flavor(self):
        return 'jvm'

    @property
    def deps_flavor(self):
        return None

    @property
    def needs_libs(self):
        return True

    @property
    def accepts_pch(self):
        return False

    def _call(self, cmd, input, output, flags=None):
        jvmoutput = self.env.tool('jvmoutput')
        result = list(chain(
            cmd, self._always_flags, iterate(flags), [input]
        ))
        return jvmoutput(output, result)

    @property
    def _always_flags(self):
        return ['-verbose', '-d', '.']

    def flags(self, options, output=None, mode='normal'):
        flags, class_path = [], []
        for i in options:
            if isinstance(i, opts.lib):
                class_path.append(i.library.path)
            elif isinstance(i, safe_str.stringy_types):
                flags.append(i)
            else:
                raise TypeError('unknown option type {!r}'.format(type(i)))

        if class_path:
            flags.extend(['-cp', safe_str.join(uniques(class_path),
                                               os.pathsep)])
        return flags

    def output_file(self, name, context):
        return ObjectFileList(Path(name + '.classlist'), Path(name + '.class'),
                              self.builder.object_format, self.lang)


class JarMaker(BuildCommand):
    flags_var = 'jarflags'

    def __init__(self, builder, env, command):
        global_flags = shell.split(env.getvar('JARFLAGS', 'cfm'))
        BuildCommand.__init__(self, builder, env, 'jar', 'jar', command,
                              flags=('jarflags', global_flags))

    @property
    def brand(self):
        return self.builder.brand

    @property
    def version(self):
        return self.builder.version

    @property
    def flavor(self):
        return 'jar'

    def can_link(self, format, langs):
        return format == 'jvm'

    @property
    def needs_libs(self):
        return False

    @property
    def has_link_macros(self):
        return False

    def pre_build(self, build, name, context):
        # Fix up paths for the Class-Path field: escape spaces, use forward
        # slashes on Windows, and prefix Windows drive letters with '/' to
        # disambiguate them from URLs.
        def fix_path(p):
            if self.env.target_platform.name == 'windows':
                if p[1] == ':':
                    p = '/' + p
                p = p.replace('\\', '/')
            return p.replace(' ', '%20')

        libs = getattr(context, 'libs', []) + flatten(
            i.libs for i in getattr(context, 'packages', [])
        )
        dirs = uniques(i.path for i in libs)
        base = Path(name).parent()

        context.manifest = File(Path(name + '-manifest.txt'))
        with generated_file(build, self.env, context.manifest) as out:
            classpath = ' '.join(fix_path(i.relpath(base)) for i in dirs)
            if classpath:
                out.write('Class-Path: {}\n'.format(classpath))

            if getattr(context, 'entry_point', None):
                out.write('Main-Class: {}\n'.format(context.entry_point))

        return opts.option_list()

    def _call(self, cmd, input, output, manifest, libs=None, flags=None):
        return list(chain(
            cmd, iterate(flags), [output, manifest], iterate(input)
        ))

    def transform_input(self, input):
        return ['@' + safe_str.safe_str(i) if isinstance(i, ObjectFileList)
                else i for i in input]

    def flags(self, options, output=None, mode='normal'):
        flags = []
        for i in options:
            if isinstance(i, safe_str.stringy_types):
                flags.append(i)
            else:
                raise TypeError('unknown option type {!r}'.format(type(i)))
        return flags

    def output_file(self, name, context):
        if getattr(context, 'entry_point', None):
            filetype = ExecutableLibrary
        else:
            filetype = Library
        return filetype(Path(name + '.jar'), self.builder.object_format,
                        self.lang)


class JvmPackage(Package):
    def __init__(self, name, format, libs=None):
        Package.__init__(self, name, format)
        self.libs = libs or []

    def compile_options(self, compiler, output):
        return opts.option_list(opts.lib(i) for i in self.libs)

    def link_options(self, linker, output):
        return opts.option_list()


class JvmPackageResolver(object):
    def __init__(self, builder, env, command):
        self.builder = builder

        if self.lang == 'scala':
            extra_env = {'JAVA_OPTS': '-XshowSettings:properties'}
            args = ['-version']
            returncode = 1
        else:
            extra_env = None
            args = ['-XshowSettings:properties', '-version']
            returncode = 0

        try:
            output = env.execute(
                command + args, env=extra_env, stdout=shell.Mode.devnull,
                stderr=shell.Mode.pipe, returncode=returncode
            )
            self.ext_dirs = self._get_dirs('java.ext.dirs', output)
            self.classpath = self._get_dirs('java.class.path', output)
        except (OSError, shell.CalledProcessError):
            self.ext_dirs = []
            self.classpath = []

    @property
    def lang(self):
        return self.builder.lang

    def _get_dirs(self, key, output):
        ex = r'^(\s*){} = (.*(?:\n\1\s+.*)*)'.format(re.escape(key))
        m = re.search(ex, output, re.MULTILINE)
        if not m:
            return []
        return [i.strip() for i in m.group(2).split('\n')]

    def _library(self, name):
        jarname = name + '.jar'
        for base in self.ext_dirs:
            fullpath = os.path.join(base, jarname)
            if os.path.exists(fullpath):
                return Library(Path(fullpath, Root.absolute),
                               self.builder.object_format,
                               external=True)

        for path in self.classpath:
            if os.path.basename(path) == jarname and os.path.exists(path):
                return Library(Path(path, Root.absolute),
                               self.builder.object_format,
                               external=True)

        raise PackageResolutionError("unable to find library '{}'"
                                     .format(name))

    def resolve(self, name, version, kind, headers, libs):
        return JvmPackage(name, self.builder.object_format,
                          libs=[self._library(name)])


class JvmRunner(BuildCommand):
    def __init__(self, builder, env, name, command):
        BuildCommand.__init__(self, builder, env, name, name, command)

    def _call(self, cmd, file, cp=None, jar=False):
        result = list(cmd)
        if jar and self.lang != 'scala':
            result.append('-jar')
        if cp:
            result.extend(['-cp', cp])
        result.append(file)
        return result

    def run_arguments(self, file):
        if isinstance(file, Executable):
            return self(file, jar=True)
        elif isinstance(file, ObjectFileList):
            return self.run_arguments(file.object_file)
        elif isinstance(file, ObjectFile):
            return self(file.path.stripext().basename(), cp=file.path.parent())
        raise TypeError('expected an executable or object file for {} to run'
                        .format(self.lang))
