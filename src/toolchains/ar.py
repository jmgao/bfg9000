import os
import shlex

import utils

class ArLinker(object):
    def __init__(self, platform_info):
        self._platform_info = platform_info
        self.command_name = os.getenv('AR', 'ar')
        self.command_var = 'ar'
        self.link_var = 'ar'
        self.name = 'ar'
        self.global_args = shlex.split(os.getenv('ARFLAGS', 'cru'), posix=False)

    # TODO: Figure out a way to indicate that libs are useless here.
    def command(self, cmd, input, output, libs=None, args=None):
        result = [cmd]
        result.extend(utils.listify(args))
        result.append(output)
        result.extend(utils.listify(input))
        return result

    def output_name(self, basename):
        return self._platform_info.static_library_name(basename)

    link_library_name = output_name

    @property
    def mode_args(self):
        return []

    def rpath(self, paths):
        return []