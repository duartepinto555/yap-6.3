"""An Application for launching a kernel"""

# Copyright (c) YAP Development Team.
# Distributed under the terms of the Modified BSD License.

from __future__ import print_function

import atexit
import os
import sys
import signal
import traceback
import logging

from tornado import ioloop
import zmq
from zmq.eventloop import ioloop as zmq_ioloop
from zmq.eventloop.zmqstream import ZMQStream

from IPython.core.application import (
    BaseIPythonApplication, base_flags, base_aliases, catch_config_error
)

from IPython.core.profiledir import ProfileDir
from IPython.core.shellapp import (
    InteractiveShellApp, shell_flags, shell_aliases
)

from IPython.utils import io
from ipython_genutils.path import filefind, ensure_dir_exists
from traitlets import (
    Any, Instance, Dict, Unicode, Integer, Bool, DottedObjectName, Type, default
)
from ipython_genutils.importstring import import_item
from jupyter_core.paths import jupyter_runtime_dir
from jupyter_client import write_connection_file
from jupyter_client.connect import ConnectionFileMixin

# local imports
from ipykernel.iostream import IOPubThread
from ipykernel.heartbeat import Heartbeat
from .yap_kernel import YAPKernel
from ipykernel.parentpoller import ParentPollerUnix, ParentPollerWindows
from jupyter_client.session import (
    Session, session_flags, session_aliases,
)
from ipykernel.zmqshell import ZMQInteractiveShell

#-----------------------------------------------------------------------------
# Flags and Aliases
#-----------------------------------------------------------------------------

kernel_aliases = dict(base_aliases)
kernel_aliases.update({
    'ip' : 'YAPKernelApp.ip',
    'hb' : 'YAPKernelApp.hb_port',
    'shell' : 'YAPKernelApp.shell_port',
    'iopub' : 'YAPKernelApp.iopub_port',
    'stdin' : 'YAPKernelApp.stdin_port',
    'control' : 'YAPKernelApp.control_port',
    'f' : 'YAPKernelApp.connection_file',
    'transport': 'YAPKernelApp.transport',
})

kernel_flags = dict(base_flags)
kernel_flags.update({
    'no-stdout' : (
            {'YAPKernelApp' : {'no_stdout' : True}},
            "redirect stdout to the null device"),
    'no-stderr' : (
            {'YAPKernelApp' : {'no_stderr' : True}},
            "redirect stderr to the null device"),
    'pylab' : (
        {'YAPKernelApp' : {'pylab' : 'auto'}},
        """Pre-load matplotlib and numpy for interactive use with
        the default matplotlib backend."""),
})

# inherit flags&aliases for any IPython shell apps
kernel_aliases.update(shell_aliases)
kernel_flags.update(shell_flags)

# inherit flags&aliases for Sessions
kernel_aliases.update(session_aliases)
kernel_flags.update(session_flags)

_ctrl_c_message = """\
NOTE: When using the `ipython kernel` entry point, Ctrl-C will not work.

To exit, you will have to explicitly quit this process, by either sending
"quit" from a client, or using Ctrl-\\ in UNIX-like environments.

To read more about this, see https://github.com/ipython/ipython/issues/2049

"""

#-----------------------------------------------------------------------------
# Application class for starting an YAP Kernel
#-----------------------------------------------------------------------------

class YAPKernelApp(BaseIPythonApplication, InteractiveShellApp,
        ConnectionFileMixin):
    name='YAP-kernel'
    aliases = Dict(kernel_aliases)
    flags = Dict(kernel_flags)
    classes = [YAPKernel, ZMQInteractiveShell, ProfileDir, Session]
    # the kernel class, as an importstring
    kernel_class = Type('yap_kernel.yap_kernel.YAPKernel',
                        klass='ipykernel.kernelbase.Kernel',
    help="""The Kernel subclass to be used.

    This should allow easy re-use of the YAPKernelApp entry point
    to configure and launch kernels other than YAP's own.
    """).tag(config=True)
    kernel = Any()
    poller = Any() # don't restrict this even though current pollers are all Threads
    heartbeat = Instance(Heartbeat, allow_none=True)
    ports = Dict()

    subcommands = {
        'install': (
            'yap_kernel.kernelspec.InstallYAPKernelSpecApp',
            'Install the YAP kernel'
        ),
    }

    # connection info:
    connection_dir = Unicode()

    @default('connection_dir')
    def _default_connection_dir(self):
        return jupyter_runtime_dir()

    @property
    def abs_connection_file(self):
        if os.path.basename(self.connection_file) == self.connection_file:
            return os.path.join(self.connection_dir, self.connection_file)
        else:
            return self.connection_file

    # streams, etc.
    no_stdout = Bool(False, help="redirect stdout to the null device").tag(config=True)
    no_stderr = Bool(False, help="redirect stderr to the null device").tag(config=True)
    outstream_class = DottedObjectName('ipykernel.iostream.OutStream',
        help="The importstring for the OutStream factory").tag(config=True)
    displayhook_class = DottedObjectName('ipykernel.displayhook.ZMQDisplayHook',
        help="The importstring for the DisplayHook factory").tag(config=True)

    # polling
    parent_handle = Integer(int(os.environ.get('JPY_PARENT_PID') or 0),
        help="""kill this process if its parent dies.  On Windows, the argument
        specifies the HANDLE of the parent process, otherwise it is simply boolean.
        """).tag(config=True)
    interrupt = Integer(int(os.environ.get('JPY_INTERRUPT_EVENT') or 0),
        help="""ONLY USED ON WINDOWS
        Interrupt this process when the parent is signaled.
        """).tag(config=True)

    def init_crash_handler(self):
        sys.excepthook = self.excepthook

    def excepthook(self, etype, evalue, tb):
        # write uncaught traceback to 'real' stderr, not zmq-forwarder
        traceback.print_exception(etype, evalue, tb, file=sys.__stderr__)

    def init_poller(self):
        if sys.platform == 'win32':
            if self.interrupt or self.parent_handle:
                self.poller = ParentPollerWindows(self.interrupt, self.parent_handle)
        elif self.parent_handle:
            self.poller = ParentPollerUnix()

    def _bind_socket(self, s, port):
        iface = '%s://%s' % (self.transport, self.ip)
        if self.transport == 'tcp':
            if port <= 0:
                port = s.bind_to_random_port(iface)
            else:
                s.bind("tcp://%s:%i" % (self.ip, port))
        elif self.transport == 'ipc':
            if port <= 0:
                port = 1
                path = "%s-%i" % (self.ip, port)
                while os.path.exists(path):
                    port = port + 1
                    path = "%s-%i" % (self.ip, port)
            else:
                path = "%s-%i" % (self.ip, port)
            s.bind("ipc://%s" % path)
        return port

    def write_connection_file(self):
        """write connection info to JSON file"""
        cf = self.abs_connection_file
        self.log.debug("Writing connection file: %s", cf)
        write_connection_file(cf, ip=self.ip, key=self.session.key, transport=self.transport,
        shell_port=self.shell_port, stdin_port=self.stdin_port, hb_port=self.hb_port,
        iopub_port=self.iopub_port, control_port=self.control_port)

    def cleanup_connection_file(self):
        cf = self.abs_connection_file
        self.log.debug("Cleaning up connection file: %s", cf)
        try:
            os.remove(cf)
        except (IOError, OSError):
            pass

        self.cleanup_ipc_files()

    def init_connection_file(self):
        if not self.connection_file:
            self.connection_file = "kernel-%s.json"%os.getpid()
        try:
            self.connection_file = filefind(self.connection_file, ['.', self.connection_dir])
        except IOError:
            self.log.debug("Connection file not found: %s", self.connection_file)
            # This means I own it, and I'll create it in this directory:
            ensure_dir_exists(os.path.dirname(self.abs_connection_file), 0o700)
            # Also, I will clean it up:
            atexit.register(self.cleanup_connection_file)
            return
        try:
            self.load_connection_file()
        except Exception:
            self.log.error("Failed to load connection file: %r", self.connection_file, exc_info=True)
            self.exit(1)

    def init_sockets(self):
        # Create a context, a session, and the kernel sockets.
        self.log.info("Starting the kernel at pid: %i", os.getpid())
        context = zmq.Context.instance()
        # Uncomment this to try closing the context.
        # atexit.register(context.term)

        self.shell_socket = context.socket(zmq.ROUTER)
        self.shell_socket.linger = 1000
        self.shell_port = self._bind_socket(self.shell_socket, self.shell_port)
        self.log.debug("shell ROUTER Channel on port: %i" % self.shell_port)

        self.stdin_socket = context.socket(zmq.ROUTER)
        self.stdin_socket.linger = 1000
        self.stdin_port = self._bind_socket(self.stdin_socket, self.stdin_port)
        self.log.debug("stdin ROUTER Channel on port: %i" % self.stdin_port)

        self.control_socket = context.socket(zmq.ROUTER)
        self.control_socket.linger = 1000
        self.control_port = self._bind_socket(self.control_socket, self.control_port)
        self.log.debug("control ROUTER Channel on port: %i" % self.control_port)

        self.init_iopub(context)

    def init_iopub(self, context):
        self.iopub_socket = context.socket(zmq.PUB)
        self.iopub_socket.linger = 1000
        self.iopub_port = self._bind_socket(self.iopub_socket, self.iopub_port)
        self.log.debug("iopub PUB Channel on port: %i" % self.iopub_port)
        self.configure_tornado_logger()
        self.iopub_thread = IOPubThread(self.iopub_socket, pipe=True)
        self.iopub_thread.start()
        # backward-compat: wrap iopub socket API in background thread
        self.iopub_socket = self.iopub_thread.background_socket

    def init_heartbeat(self):
        """start the heart beating"""
        # heartbeat doesn't share context, because it mustn't be blocked
        # by the GIL, which is accessed by libzmq when freeing zero-copy messages
        hb_ctx = zmq.Context()
        self.heartbeat = Heartbeat(hb_ctx, (self.transport, self.ip, self.hb_port))
        self.hb_port = self.heartbeat.port
        self.log.debug("Heartbeat REP Channel on port: %i" % self.hb_port)
        self.heartbeat.start()

    def log_connection_info(self):
        """display connection info, and store ports"""
        basename = os.path.basename(self.connection_file)
        if basename == self.connection_file or \
            os.path.dirname(self.connection_file) == self.connection_dir:
            # use shortname
            tail = basename
        else:
            tail = self.connection_file
        lines = [
            "To connect another client to this kernel, use:",
            "    --existing %s" % tail,
        ]
        # log connection info
        # info-level, so often not shown.
        # frontends should use the %connect_info magic
        # to see the connection info
        for line in lines:
            self.log.info(line)
        # also raw print to the terminal if no parent_handle (`ipython kernel`)
        # unless log-level is CRITICAL (--quiet)
        if not self.parent_handle and self.log_level < logging.CRITICAL:
            io.rprint(_ctrl_c_message)
            for line in lines:
                io.rprint(line)

        self.ports = dict(shell=self.shell_port, iopub=self.iopub_port,
                                stdin=self.stdin_port, hb=self.hb_port,
                                control=self.control_port)

    def init_blackhole(self):
        """redirects stdout/stderr to devnull if necessary"""
        if self.no_stdout or self.no_stderr:
            blackhole = open(os.devnull, 'w')
            if self.no_stdout:
                sys.stdout = sys.__stdout__ = blackhole
            if self.no_stderr:
                sys.stderr = sys.__stderr__ = blackhole

    def init_io(self):
        """Redirect input streams and set a display hook."""
        if self.outstream_class:
            outstream_factory = import_item(str(self.outstream_class))
            sys.stdout = outstream_factory(self.session, self.iopub_thread, u'stdout')
            sys.stderr = outstream_factory(self.session, self.iopub_thread, u'stderr')
        if self.displayhook_class:
            displayhook_factory = import_item(str(self.displayhook_class))
            self.displayhook = displayhook_factory(self.session, self.iopub_socket)
            sys.displayhook = self.displayhook

        self.patch_io()

    def patch_io(self):
        """Patch important libraries that can't handle sys.stdout forwarding"""
        try:
            import faulthandler
        except ImportError:
            pass
        else:
            # Warning: this is a monkeypatch of `faulthandler.enable`, watch for possible
            # updates to the upstream API and update accordingly (up-to-date as of Python 3.5):
            # https://docs.python.org/3/library/faulthandler.html#faulthandler.enable

            # change default file to __stderr__ from forwarded stderr
            faulthandler_enable = faulthandler.enable
            def enable(file=sys.__stderr__, all_threads=True, **kwargs):
                return faulthandler_enable(file=file, all_threads=all_threads, **kwargs)

            faulthandler.enable = enable

            if hasattr(faulthandler, 'register'):
                faulthandler_register = faulthandler.register
                def register(signum, file=sys.__stderr__, all_threads=True, chain=False, **kwargs):
                    return faulthandler_register(signum, file=file, all_threads=all_threads,
                                                 chain=chain, **kwargs)
                faulthandler.register = register

    def init_signal(self):
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    def init_kernel(self):
        """Create the Kernel object itself"""
        shell_stream = ZMQStream(self.shell_socket)
        control_stream = ZMQStream(self.control_socket)

        kernel_factory = self.kernel_class.instance

        kernel = kernel_factory(parent=self, session=self.session,
                                shell_streams=[shell_stream, control_stream],
                                iopub_thread=self.iopub_thread,
                                iopub_socket=self.iopub_socket,
                                stdin_socket=self.stdin_socket,
                                log=self.log,
                                profile_dir=self.profile_dir,
                                user_ns=self.user_ns,
        )
        kernel.record_ports({
            name + '_port': port for name, port in self.ports.items()
        })
        self.kernel = kernel

        # Allow the displayhook to get the execution count
        self.displayhook.get_execution_count = lambda: kernel.execution_count

    def init_gui_pylab(self):
        """Enable GUI event loop integration, taking pylab into account."""

        # Register inline backend as default
        # this is higher priority than matplotlibrc,
        # but lower priority than anything else (mpl.use() for instance).
        # This only affects matplotlib >= 1.5
        if not os.environ.get('MPLBACKEND'):
            os.environ['MPLBACKEND'] = 'module://ipykernel.pylab.backend_inline'

        # Provide a wrapper for :meth:`InteractiveShellApp.init_gui_pylab`
        # to ensure that any exception is printed straight to stderr.
        # Normally _showtraceback associates the reply with an execution,
        # which means frontends will never draw it, as this exception
        # is not associated with any execute request.

        shell = self.shell
        _showtraceback = shell._showtraceback
        try:
            # replace error-sending traceback with stderr
            def print_tb(etype, evalue, stb):
                print ("GUI event loop or pylab initialization failed",
                       file=sys.stderr)
                print (shell.InteractiveTB.stb2text(stb), file=sys.stderr)
            shell._showtraceback = print_tb
            InteractiveShellApp.init_gui_pylab(self)
        finally:
            shell._showtraceback = _showtraceback

    def init_shell(self):
        self.shell = getattr(self.kernel, 'shell', None)
        if self.shell:
            self.shell.configurables.append(self)

    def init_extensions(self):
        super(YAPKernelApp, self).init_extensions()
        # BEGIN HARDCODED WIDGETS HACK
        # Ensure ipywidgets extension is loaded if available
        extension_man = self.shell.extension_manager
        if 'ipywidgets' not in extension_man.loaded:
            try:
                extension_man.load_extension('ipywidgets')
            except ImportError as e:
                self.log.debug('ipywidgets package not installed.  Widgets will not be available.')
        # END HARDCODED WIDGETS HACK

    def configure_tornado_logger(self):
        """ Configure the tornado logging.Logger.

            Must set up the tornado logger or else tornado will call
            basicConfig for the root logger which makes the root logger
            go to the real sys.stderr instead of the capture streams.
            This function mimics the setup of logging.basicConfig.
        """
        logger = logging.getLogger('tornado')
        handler = logging.StreamHandler()
        formatter = logging.Formatter(logging.BASIC_FORMAT)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    @catch_config_error
    def initialize(self, argv=None):
        super(YAPKernelApp, self).initialize(argv)
        if self.subapp is not None:
            return
        # register zmq IOLoop with tornado
        zmq_ioloop.install()
        self.init_blackhole()
        self.init_connection_file()
        self.init_poller()
        self.init_sockets()
        self.init_heartbeat()
        # writing/displaying connection info must be *after* init_sockets/heartbeat
        self.write_connection_file()
        # Log connection info after writing connection file, so that the connection
        # file is definitely available at the time someone reads the log.
        self.log_connection_info()
        self.init_io()
        self.init_signal()
        self.init_kernel()
        # shell init steps
        self.init_path()
        self.init_shell()
        if self.shell:
            self.init_gui_pylab()
            self.init_extensions()
            self.init_code()
        # flush stdout/stderr, so that anything written to these streams during
        # initialization do not get associated with the first execution request
        sys.stdout.flush()
        sys.stderr.flush()

    def start(self):
        if self.subapp is not None:
            return self.subapp.start()
        if self.poller is not None:
            self.poller.start()
        self.kernel.start()
        try:
            ioloop.IOLoop.instance().start()
        except KeyboardInterrupt:
            pass

launch_new_instance = YAPKernelApp.launch_instance

def main():
    """Run an IPKernel as an application"""
    app = YAPKernelApp.instance()
    app.initialize()
    app.start()


if __name__ == '__main__':
    main()
