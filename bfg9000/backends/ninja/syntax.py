import re
from cStringIO import StringIO
from collections import namedtuple, OrderedDict
from enum import Enum

from ... import path
from ... import safe_str
from ... import shell
from ... import iterutils

Path = path.Path

NinjaRule = namedtuple('NinjaRule', ['command', 'depfile', 'deps', 'generator',
                                     'restat'])
NinjaBuild = namedtuple('NinjaBuild', ['outputs', 'rule', 'inputs', 'implicit',
                                       'order_only', 'variables'])

class NinjaWriter(object):
    def __init__(self, stream):
        self.stream = stream

    @staticmethod
    def escape_str(string, syntax):
        if '\n' in string:
            raise ValueError('illegal newline')

        if syntax == 'output':
            return re.sub(r'([:$ ])', r'$\1', string)
        elif syntax == 'input':
            return re.sub(r'([$ ])', r'$\1', string)
        elif syntax in ['shell', 'clean']:
            return string.replace('$', '$$')
        else:
            raise ValueError("unknown syntax '{}'".format(syntax))

    def write_literal(self, string):
        self.stream.write(string)

    def write(self, thing, syntax, shell_quote=shell.quote_info):
        thing = safe_str.safe_str(thing)
        shelly = syntax == 'shell'
        escaped = False

        if isinstance(thing, safe_str.escaped_str):
            self.write_literal(thing.string)
            escaped = True
        elif isinstance(thing, basestring):
            if shelly and shell_quote:
                thing, escaped = shell_quote(thing)
            self.write_literal(self.escape_str(thing, syntax))
        elif isinstance(thing, safe_str.jbos):
            for i in thing.bits:
                escaped |= self.write(i, syntax, shell_quote)
        elif isinstance(thing, Path):
            out = NinjaWriter(StringIO())
            thing = thing.realize(path_vars, shelly)
            escaped = out.write(thing, syntax, shell.escape)

            thing = out.stream.getvalue()
            if shelly and escaped:
                thing = shell.quote_escaped(thing)
            self.write_literal(thing)
        else:
            raise TypeError(type(thing))

        return escaped

    def write_each(self, things, syntax, delim=' ', prefix=None, suffix=None):
        for tween, i in iterutils.tween(things, delim, prefix, suffix):
            self.write_literal(i) if tween else self.write(i, syntax)

    def write_shell(self, thing, clean=False):
        syntax = 'clean' if clean else 'shell'
        if iterutils.isiterable(thing):
            self.write_each(thing, syntax)
        else:
            self.write(thing, syntax, shell_quote=None)

class NinjaVariable(object):
    def __init__(self, name):
        self.name = re.sub('\W', '_', name)

    def use(self):
        return safe_str.escaped_str('${}'.format(self.name))

    def _safe_str(self):
        return self.use()

    def __str__(self):
        raise NotImplementedError()

    def __repr__(self):
        return repr(self.use())

    def __hash__(self):
        return hash(self.name)

    def __cmp__(self, rhs):
        return cmp(self.name, rhs.name)

    def __add__(self, rhs):
        return self.use() + rhs

    def __radd__(self, lhs):
        return lhs + self.use()

def var(v):
    return v if isinstance(v, NinjaVariable) else NinjaVariable(v)

path_vars = {
    path.Root.srcdir:   NinjaVariable('srcdir'),
    path.Root.builddir: None,
}
path_vars.update({i: NinjaVariable(i.name) for i in path.InstallRoot})

Section = Enum('Section', ['path', 'command', 'flags', 'other'])

class NinjaFile(object):
    def __init__(self):
        self._var_table = set()
        self._variables = {i: [] for i in Section}

        self._rules = OrderedDict()

        self._builds = []
        self._build_outputs = set()
        self._defaults = []

    def variable(self, name, value, section=Section.other, exist_ok=True):
        name = var(name)
        if self.has_variable(name):
            if not exist_ok:
                raise ValueError("variable {!r} already exists".format(name))
        else:
            self._var_table.add(name)
            self._variables[section].append((name, value))
        return name

    def has_variable(self, name):
        return var(name) in self._var_table

    def rule(self, name, command, depfile=None, deps=None, generator=False,
             restat=False):
        if re.search('\W', name):
            raise ValueError('rule name contains invalid characters')
        if self.has_rule(name):
            raise ValueError("rule '{}' already exists".format(name))
        self._rules[name] = NinjaRule(command, depfile, deps, generator, restat)

    def has_rule(self, name):
        return name in self._rules

    def build(self, output, rule, inputs=None, implicit=None, order_only=None,
              variables=None):
        if rule != 'phony' and not self.has_rule(rule):
            raise ValueError("unknown rule '{}'".format(rule))

        variables = {var(k): v for k, v in (variables or {}).iteritems()}

        outputs = iterutils.listify(output)
        for i in outputs:
            if self.has_build(i):
                raise ValueError("build for '{}' already exists".format(i))
            self._build_outputs.add(i)
        self._builds.append(NinjaBuild(
            outputs, rule, iterutils.listify(inputs),
            iterutils.listify(implicit), iterutils.listify(order_only),
            variables
        ))

    def has_build(self, name):
        return name in self._build_outputs

    def default(self, paths):
        self._defaults.extend(paths)

    def _write_variable(self, out, name, value, clean=False, indent=0):
        out.write_literal(('  ' * indent) + name.name + ' = ')
        out.write_shell(value, clean)
        out.write_literal('\n')

    def _write_rule(self, out, name, rule):
        out.write_literal('rule ' + name + '\n')

        self._write_variable(out, var('command'), rule.command, indent=1)
        if rule.depfile:
            self._write_variable(out, var('depfile'), rule.depfile, indent=1)
        if rule.deps:
            self._write_variable(out, var('deps'), rule.deps, indent=1)
        if rule.generator:
            self._write_variable(out, var('generator'), '1', indent=1)
        if rule.restat:
            self._write_variable(out, var('restat'), '1', indent=1)

    def _write_build(self, out, build):
        out.write_literal('build ')
        out.write_each(build.outputs, syntax='output')
        out.write_literal(': ' + build.rule)

        out.write_each(build.inputs, syntax='input', prefix=' ')
        out.write_each(build.implicit, syntax='input', prefix=' | ')
        out.write_each(build.order_only, syntax='input', prefix=' || ')
        out.write_literal('\n')

        if build.variables:
            for k, v in build.variables.iteritems():
                self._write_variable(out, k, v, indent=1)

    def write(self, out):
        out = NinjaWriter(out)

        for section in Section:
            # Paths are inherently clean (read: don't need shell quoting).
            # XXX: This behavior is a bit strange and maybe should be reworked.
            clean = section == Section.path
            for name, value in self._variables[section]:
                self._write_variable(out, name, value, clean)
            if self._variables[section]:
                out.write_literal('\n')

        for name, rule in self._rules.iteritems():
            self._write_rule(out, name, rule)
            out.write_literal('\n')

        for build in self._builds:
            self._write_build(out, build)

        if self._defaults:
            out.write_literal('\ndefault ')
            out.write_each(self._defaults, syntax='input')
            out.write_literal('\n')
