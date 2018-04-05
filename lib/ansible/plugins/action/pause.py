# Copyright 2012, Tim Bielawa <tbielawa@redhat.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import datetime
import signal
import termios
import time
import tty
import sys

from os import isatty, ttyname

from ansible.errors import AnsibleError
from ansible.module_utils.six import PY3
from ansible.module_utils._text import to_text
from ansible.plugins.action import ActionBase

try:
    from __main__ import display
except ImportError:
    from ansible.utils.display import Display
    display = Display()


class AnsibleTimeoutExceeded(Exception):
    pass


def timeout_handler(signum, frame):
    raise AnsibleTimeoutExceeded


class ActionModule(ActionBase):
    ''' pauses execution for a length or time, or until input is received '''

    PAUSE_TYPES = ['seconds', 'minutes', 'prompt', 'echo', '']
    BYPASS_HOST_LOOP = True

    def run(self, tmp=None, task_vars=None):
        ''' run the pause action module '''
        if task_vars is None:
            task_vars = dict()

        result = super(ActionModule, self).run(tmp, task_vars)
        del tmp  # tmp no longer has any effect

        duration_unit = 'minutes'
        prompt = None
        seconds = None
        echo = True
        echo_prompt = ''
        result.update(dict(
            changed=False,
            rc=0,
            stderr='',
            stdout='',
            start=None,
            stop=None,
            delta=None,
            echo=echo
        ))

        if not set(self._task.args.keys()) <= set(self.PAUSE_TYPES):
            result['failed'] = True
            result['msg'] = "Invalid argument given. Must be one of: %s" % ", ".join(self.PAUSE_TYPES)
            return result

        # Should keystrokes be echoed to stdout?
        if 'echo' in self._task.args:
            echo = self._task.args['echo']
            if not type(echo) == bool:
                result['failed'] = True
                result['msg'] = "'%s' is not a valid setting for 'echo'." % self._task.args['echo']
                return result

            # Add a note saying the output is hidden if echo is disabled
            if not echo:
                echo_prompt = ' (output is hidden)'

        # Is 'prompt' a key in 'args'?
        if 'prompt' in self._task.args:
            prompt = "[%s]\n%s%s:" % (self._task.get_name().strip(), self._task.args['prompt'], echo_prompt)
        else:
            # If no custom prompt is specified, set a default prompt
            prompt = "[%s]\n%s%s:" % (self._task.get_name().strip(), 'Press enter to continue, Ctrl+C to abort', echo_prompt)

        # Are 'minutes' or 'seconds' keys that exist in 'args'?
        if 'minutes' in self._task.args or 'seconds' in self._task.args:
            try:
                if 'minutes' in self._task.args:
                    # The time() command operates in seconds so we need to
                    # recalculate for minutes=X values.
                    seconds = int(self._task.args['minutes']) * 60
                else:
                    seconds = int(self._task.args['seconds'])
                    duration_unit = 'seconds'

            except ValueError as e:
                result['failed'] = True
                result['msg'] = u"non-integer value given for prompt duration:\n%s" % to_text(e)
                return result

        ########################################################################
        # Begin the hard work!

        start = time.time()
        result['start'] = to_text(datetime.datetime.now())
        result['user_input'] = b''

        try:
            if seconds is not None:
                if seconds < 1:
                    seconds = 1

                # setup the alarm handler
                signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(seconds)

                # show the timer and control prompts
                prompt = "Pausing for %d seconds%s\n(ctrl+C then 'C' = continue early, ctrl+C then 'A' = abort)\n%s" % \
                         (seconds, echo_prompt, self._task.args.get('prompt', ''))

            # figure stdin out
            try:
                if PY3:
                    stdin = self._connection._new_stdin.buffer
                else:
                    stdin = self._connection._new_stdin
            except (ValueError, AttributeError):
                # ValueError: someone is using a closed file descriptor as stdin
                # AttributeError: someone is using a null file descriptor as stdin on windoez
                stdin = None

            sys.stdin = open(ttyname(stdin.fileno()))
            while True:
                try:
                    result['user_input'] = display.prompt(prompt, private=(not echo))
                except KeyboardInterrupt:
                    if seconds is not None:
                        signal.alarm(0)
                        if self._c_or_a(stdin):
                            break
                        else:
                            raise AnsibleError('user requested abort!')
                if not seconds:
                   break

        except AnsibleTimeoutExceeded:
            # this is the exception we expect when the alarm signal
            # fires, so we simply ignore it to move into the cleanup
            pass
        finally:
            # cleanup and save some information

            duration = time.time() - start
            result['stop'] = to_text(datetime.datetime.now())
            result['delta'] = int(duration)

            if duration_unit == 'minutes':
                duration = round(duration / 60.0, 2)
            else:
                duration = round(duration, 2)
            result['stdout'] = "Paused for %s %s" % (duration, duration_unit)

        result['user_input'] = to_text(result['user_input'], errors='surrogate_or_strict')
        return result

    def _c_or_a(self, stdin):

        try:
            # save the attributes on the existing (duped) stdin so
            # that we can restore them later after we set raw mode
            old_settings = None
            fd = stdin.fileno()
            if fd is not None:
                if isatty(fd):
                    new_settings = old_settings = termios.tcgetattr(fd)
                    new_settings[6][termios.VINTR] = '\0'
                    tty.setraw(fd)
                    termios.tcsetattr(fd, termios.TCSANOW, new_settings)
                    termios.tcflush(stdin, termios.TCIFLUSH)

            display.display("Press 'C' to continue the play or 'A' to abort \r"),
            while True:
                key_pressed = stdin.read(1)
                if key_pressed.lower() == b'a':
                    return False
                elif key_pressed.lower() == b'c':
                    return True
                else:
                    display.display("Invalid keypress detected, please press 'C' to continue or 'A' to abort \r")
        finally:
            if old_settings is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
