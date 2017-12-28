# -*- coding: utf-8 -*-
# Copyright (c) 2011, Wojciech Bederski (wuub.net)
# All rights reserved.
# See LICENSE.txt for details.
from __future__ import absolute_import, unicode_literals, print_function, division

import subprocess
import os
import sys
from .repl import Repl
import signal
from sublime import load_settings, error_message
from .autocomplete_server import AutocompleteServer
from .killableprocess import Popen
import glob
import shlex
import collections
import io
PY3 = sys.version_info[0] == 3

if os.name == 'posix':
    POSIX = True
    import fcntl
    import select
else:
    POSIX = False


class Unsupported(Exception):
    def __init__(self, msgs):
        super(Unsupported, self).__init__()
        self.msgs = msgs

    def __repr__(self):
        return "\n".join(self.msgs)


def win_find_executable(executable, env):
    """Explicetely looks for executable in env["PATH"]"""
    if os.path.dirname(executable):
        return executable  # executable is already absolute filepath
    path = env.get("PATH", "")
    pathext = env.get("PATHEXT") or ".EXE"
    dirs = path.split(os.path.pathsep)
    (base, ext) = os.path.splitext(executable)
    if ext:
        extensions = [ext]
    else:
        extensions = pathext.split(os.path.pathsep)
    for directory in dirs:
        for extension in extensions:
            filepath = os.path.join(directory, base + extension)
            if os.path.exists(filepath):
                return filepath
    return None


class SubprocessRepl(Repl):
    TYPE = "subprocess"

    def __init__(self, encoding, cmd=None, env=None, cwd=None, extend_env=None, soft_quit="",
                 autocomplete_server=False, filt_warns=False, **kwds):
        super(SubprocessRepl, self).__init__(encoding, **kwds)
        settings = load_settings('SublimeREPL.sublime-settings')

        if cmd[0] == "[unsupported]":
            raise Unsupported(cmd[1:])

        self._autocomplete_server = None
        if autocomplete_server:
            self._autocomplete_server = AutocompleteServer(self, settings.get("autocomplete_server_ip"))
            self._autocomplete_server.start()

        env = self.env(env, extend_env, settings)
        env[b"SUBLIMEREPL_AC_PORT"] = str(self.autocomplete_server_port()).encode("utf-8")
        env[b"SUBLIMEREPL_AC_IP"] = settings.get("autocomplete_server_ip").encode("utf-8")

        if PY3:
            strings_env = {}
            for k, v in env.items():
                strings_env[k.decode("utf-8")] = v.decode("utf-8")
            env = strings_env

        self._filt_warns = filt_warns
        self._cmd = self.cmd(cmd, env)
        self._soft_quit = soft_quit
        self._killed = False
        self.popen = Popen(
            self._cmd,
            startupinfo=self.startupinfo(settings),
            creationflags=self.creationflags(settings),
            bufsize=io.DEFAULT_BUFFER_SIZE,  # -1 if self._filt_warns else 1
            cwd=self.cwd(cwd, settings), env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # try to filter out gtk warnings
        if self._filt_warns:
            print('__init__ with filt_warns')
            # strange, cat works, but grep does not
            # grep --line-buffered does something
            cmd = shlex.split("grep -v -e 'Gtk-WARNING' -e 'Gtk-Message' -e 'GLib-GIO-WARNING'") if 0 else 'cat'
            self.filt = Popen(cmd,
                              bufsize=io.DEFAULT_BUFFER_SIZE,
                              cwd=self.cwd(cwd, settings),
                              startupinfo=self.startupinfo(settings),
                              creationflags=self.creationflags(settings),
                              env=env,
                              stdin=self.popen.stdout,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              )
        else:
            self.filt = self.popen

        if POSIX:
            flags = fcntl.fcntl(self.filt.stdout, fcntl.F_GETFL)
            fcntl.fcntl(self.filt.stdout, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def autocomplete_server_port(self):
        if not self._autocomplete_server:
            return None
        return self._autocomplete_server.port()

    def autocomplete_available(self):
        if not self._autocomplete_server:
            return False
        return self._autocomplete_server.connected()

    def autocomplete_completions(self, whole_line, pos_in_line, prefix, whole_prefix, locations):
        return self._autocomplete_server.complete(
            whole_line=whole_line,
            pos_in_line=pos_in_line,
            prefix=prefix,
            whole_prefix=whole_prefix,
            locations=locations,
        )

    def cmd(self, cmd, env):
        """On Linux and OSX just returns cmd, on windows it has to find
           executable in env because of this: http://bugs.python.org/issue8557"""
        if os.name != "nt":
            return cmd
        if isinstance(cmd, str):
            _cmd = [cmd]
        else:
            _cmd = cmd
        executable = win_find_executable(_cmd[0], env)
        if executable:
            _cmd[0] = executable
        return _cmd

    def cwd(self, cwd, settings):
        if cwd and os.path.exists(cwd):
            return cwd
        return None

    def getenv(self, settings):
        """Tries to get most appropriate environent, on windows
           it's os.environ.copy, but on other system's we'll
           try get values from login shell"""

        getenv_command = settings.get("getenv_command")
        if getenv_command and POSIX:
            try:
                output = subprocess.check_output(getenv_command)
                lines = output.decode("utf-8", errors="replace").splitlines()
                kw_pairs = [line.split('=', 1) for line in lines]
                santized_kw_pairs = []
                for kw_pair in kw_pairs:
                    if len(kw_pair) == 1:
                        # no `=` inside the line, we will append this to previous
                        # kw pair's value
                        previous_pair = santized_kw_pairs[-1]
                        previous_pair[1] += "\n" + kw_pair[0]
                    elif len(kw_pair) == 2:
                        santized_kw_pairs.append(kw_pair)
                    else:
                        pass
                env = dict(santized_kw_pairs)
                return env
            except:
                import traceback
                traceback.print_exc()
                error_message(
                    "SublimeREPL: obtaining sane environment failed in getenv()\n"
                    "Check console and 'getenv_command' setting \n"
                    "WARN: Falling back to SublimeText environment")

        # Fallback to environ.copy() if not on POSIX or sane getenv failed
        return os.environ.copy()

    def env(self, env, extend_env, settings):
        updated_env = env if env else self.getenv(settings)
        default_extend_env = settings.get("default_extend_env")
        if default_extend_env:
            updated_env.update(self.interpolate_extend_env(updated_env, default_extend_env))
        if extend_env:
            updated_env.update(self.interpolate_extend_env(updated_env, extend_env))

        bytes_env = {}
        for k, v in list(updated_env.items()):
            try:
                enc_k = self.encoder(str(k))[0]
                enc_v = self.encoder(str(v))[0]
            except UnicodeDecodeError:
                continue  # f*** it, we'll do it live
            else:
                bytes_env[enc_k] = enc_v
        return bytes_env

    def interpolate_extend_env(self, env, extend_env):
        """Interpolates (subst) values in extend_env.
           Mostly for path manipulation"""
        new_env = {}
        for key, val in list(extend_env.items()):
            new_env[key] = str(val).format(**env)
        return new_env

    def startupinfo(self, settings):
        startupinfo = None
        if os.name == 'nt':
            from .killableprocess import STARTUPINFO, STARTF_USESHOWWINDOW
            startupinfo = STARTUPINFO()
            startupinfo.dwFlags |= STARTF_USESHOWWINDOW
            startupinfo.wShowWindow |= 1  # SW_SHOWNORMAL
        return startupinfo

    def creationflags(self, settings):
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x8000000  # CREATE_NO_WINDOW
        return creationflags

    def name(self):
        if self.external_id:
            return self.external_id
        if isinstance(self._cmd, str):
            return self._cmd
        return " ".join([str(x) for x in self._cmd])

    def is_alive(self):
        a = self.popen.poll() is None
        b = self.filt.poll() is None if self._filt_warns else 1
        return a and b

    def read_bytes(self):
        out = self.filt.stdout
        # stackoverflow.com/a/23696745
        if POSIX:
            rlist, wlist, xlist = [out], [], []
            bufsize = io.DEFAULT_BUFFER_SIZE  # io.DEFAULT_BUFFER_SIZE bytes (usually 8192)
            while 1:
                i, _, _ = select.select(rlist, wlist, xlist)
                if i:
                    return out.read(bufsize)
        else:
            # this is windows specific problem, that you cannot tell if there
            # are more bytes ready, so we read only 1 at a times

            while 1:
                byte = out.read(1)
                if byte == b'\r':
                    # f'in HACK, for \r\n -> \n translation on windows
                    # I tried universal_endlines but it was pain and misery! :'(
                    continue
                return byte

    def write_bytes(self, bytes):
        si = self.popen.stdin
        si.write(bytes)
        si.flush()

    def kill(self):
        self._killed = True
        self.write(self._soft_quit)
        self.popen.kill()
        if self._filt_warns:
            self.filt.kill()

    def available_signals(self):
        signals = {}
        for k, v in list(signal.__dict__.items()):
            if not k.startswith("SIG"):
                continue
            signals[k] = v
        return signals

    def send_signal(self, sig):
        if sig == signal.SIGTERM:
            self._killed = True
        if self.is_alive():
            self.popen.send_signal(sig)
            if self._filt_warns:
                self.filt.send_signal(sig)


VENVS_ENVIRON = dict()


class SubprocessReplVenv(SubprocessRepl):
    TYPE = 'spvenv'

    def _print(self, *args, **kwargs):
        if self.debug:
            _args = []
            for _ in args:
                if isinstance(_, dict):
                    _args.append('\n'.join(['%s=%s' % (k, v) for k, v in collections.OrderedDict(sorted(_.items())).items()]))
                else:
                    _args.append(_)
            print(*_args, **kwargs)

    def shell_source(self, script, py_ver):
        'emulates sourcing a script, return a dict env'
        # --login will make bash source either ~/.bash_profile, ~/.bash_login, or ~/.profile
        # -i will make bash source ~/.bashrc (interactive shell)
        # here we only need login to have access to the conda root activate ?
        cmd = shlex.split("bash --login -c '. {SCRIPT} {PY_VER}; env'".format(SCRIPT=script, PY_VER=py_ver))
        stdout = subprocess.Popen(cmd, stdout=subprocess.PIPE).stdout
        return dict((line.split('=', 1) for line in stdout.read().decode().splitlines()))

    def scan_for_virtualenvs(self, venv_paths, conda_min=4):
        '''
        conda 4.3.XXX
        -----------------
        scans a directory for a dir named bin with a script activate in it

        conda  4.4.XXX
        --------------
        scans for a dir named dir
        '''
        bin_dir = 'Scripts' if os.name == 'nt' else 'bin'
        found_dirs = set()
        for venv_path in venv_paths:
            p = os.path.expanduser(venv_path)
            # print(p)
            if conda_min == 3:
                pattern = os.path.join(p, '*', bin_dir, 'activate')
                found_dirs.update(list(map(os.path.dirname, glob.glob(pattern))))
            else:
                pattern = os.path.join(p, '*', bin_dir)
                found_dirs.update(glob.glob(pattern))
        print(found_dirs)
        return sorted(found_dirs)

    def __init__(self, encoding, cmd=None, env=None, cwd=None, extend_env=dict(),
                 soft_quit="", autocomplete_server=False, **kwargs):
        # super(SubprocessRepl, self).__init__(encoding, **kwds)

        settings = load_settings('SublimeREPL.sublime-settings')
        venv_paths = settings.get('python_virtualenv_paths')
        use_wrapped = settings.get('use_wrapped')
        force_source = settings.get('force_source')
        conda_minor = settings.get('conda_minor')
        self.debug = settings.get('debug')

        venvs_bin_dir = {os.path.basename(os.path.dirname(_)): _ for _ in self.scan_for_virtualenvs(venv_paths)}
        wrappers_dir = {k: os.path.join(v, 'wrappers/conda') for k, v in venvs_bin_dir.items()}

        self._print('venvs bin dirs', venvs_bin_dir)
        self._print('wrappers bin dirs', wrappers_dir)

        conda_env = env if env else self.getenv(settings)
        py_ver = extend_env.get('PY_VERSION', 'py3') if extend_env else 'py3'  # default 'py3' env

        if 'PY_VERSION' not in extend_env:
            extend_env['PY_VERSION'] = 'py3'

        if 'PYTHONIOENCODING' not in extend_env:
            extend_env['PYTHONIOENCODING'] = 'utf-8'

        py_ver = extend_env['PY_VERSION']

        # using wrappers created with the conda package exec-wrappers, if not found, fallback to sourcing ...
        if use_wrapped and wrappers_dir.get(py_ver):
            path = conda_env['PATH'].split(':')
            path.insert(0, wrappers_dir[py_ver])
            path.insert(1, venvs_bin_dir[py_ver])
            conda_env['PATH'] = ':'.join(path)
            self._print('\t==> use_wrapped <==')
        else:
            global VENVS_ENVIRON
            if py_ver not in ('root', 'base'):
                if VENVS_ENVIRON.get(py_ver) is None or force_source:
                    self._print('\t==> sourcing <==')
                    # speedup, avoid sourcing activate every time
                    for version in venvs_bin_dir.keys():
                        if conda_minor == 3:
                            # envs dir activate
                            activate_dir = venvs_bin_dir[version]
                        else:
                            # root dir activate
                            activate_dir = os.path.join(os.path.dirname(venv_paths[0]), 'bin')
                        VENVS_ENVIRON[version] = self.shell_source(os.path.join(activate_dir, 'activate'), version)
                self._print('--> env before', conda_env, sep='\n')
                # self.shell_source(os.path.join(venvdir, 'activate'), py_ver)
                conda_env.update(self.interpolate_extend_env(conda_env, VENVS_ENVIRON[py_ver]))
                self._print('--> env after', conda_env, sep='\n')

        # call __init__ with a new modified env
        print('PATH used:', conda_env['PATH'])
        super(SubprocessReplVenv, self).__init__(encoding, cmd, conda_env, cwd, extend_env, soft_quit, autocomplete_server, **kwargs)
