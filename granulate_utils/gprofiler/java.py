#
# Copyright (C) 2022 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import errno
import functools
import logging
import os
import re
import secrets
import signal
from enum import Enum
from pathlib import Path
from subprocess import CompletedProcess
from threading import Event, Lock
from types import TracebackType
from typing import Any, List, Optional, Type, TypeVar, Union, cast

import psutil

from granulate_utils.gprofiler.platform import is_linux
from granulate_utils.java import DETECTED_JAVA_PROCESSES_REGEX, locate_hotspot_error_file

if is_linux():
    from granulate_utils.linux.ns import (
        get_proc_root_path,
        get_process_nspid,
        resolve_proc_root_links,
        run_in_ns,
    )
    from granulate_utils.linux.process import (
        is_musl,
        is_process_basename_matching,
        is_process_running,
        read_proc_file,
    )

from psutil import NoSuchProcess, Process

from granulate_utils.gprofiler.exceptions import (
    CalledProcessError,
    CalledProcessTimeoutError,
    NoRwExecDirectoryFoundError,
)
from granulate_utils.gprofiler.utils import (
    GPROFILER_DIRECTORY_NAME,
    TEMPORARY_STORAGE_PATH,
    remove_path,
    remove_prefix,
    run_process,
    touch_path,
)
from granulate_utils.gprofiler.utils.fs import is_owned_by_root, is_rw_exec_dir, mkdir_owned_root, safe_copy

libap_copy_lock = Lock()

# directories we check for rw,exec as candidates for libasyncProfiler.so placement.
POSSIBLE_AP_DIRS = (
    TEMPORARY_STORAGE_PATH,
    f"/run/{GPROFILER_DIRECTORY_NAME}",
    f"/opt/{GPROFILER_DIRECTORY_NAME}",
    f"/dev/{GPROFILER_DIRECTORY_NAME}",  # unfortunately, we encoundered some systems that left us no other option
)


def frequency_to_ap_interval(frequency: int) -> int:
    # async-profiler accepts interval between samples (nanoseconds)
    return int((1 / frequency) * 1_000_000_000)


@functools.lru_cache(maxsize=1024)
def needs_musl_ap_cached(process: Process) -> bool:
    """
    AP needs musl build if the JVM itself is built against musl. If the JVM is built against glibc,
    we need the glibc build of AP. For this reason we also check for glibc-compat, which is an indicator
    for glibc-based JVM despite having musl loaded.
    """
    maps = process.memory_maps()
    return is_musl(process, maps) and not any("glibc-compat" in m.path for m in maps)


class JavaSafemodeOptions(str, Enum):
    # a profiled process was OOM-killed and we saw it in the kernel log
    PROFILED_OOM = "profiled-oom"
    # a profiled process was signaled:
    # * fatally signaled and we saw it in the kernel log
    # * we saw an exit code of signal in a proc_events event.
    PROFILED_SIGNALED = "profiled-signaled"
    # hs_err file was written for a profiled process
    HSERR = "hserr"
    # a process was OOM-killed and we saw it in the kernel log
    GENERAL_OOM = "general-oom"
    # a process was fatally signaled and we saw it in the kernel log
    GENERAL_SIGNALED = "general-signaled"
    # we saw the PID of a profiled process in the kernel logs
    PID_IN_KERNEL_MESSAGES = "pid-in-kernel-messages"
    # employ extended version checks before deciding to profile
    # see _is_jvm_profiling_supported() docs for more information
    JAVA_EXTENDED_VERSION_CHECKS = "java-extended-version-checks"
    # refuse profiling if async-profiler is already loaded (and not by gProfiler)
    # in the target process
    AP_LOADED_CHECK = "ap-loaded-check"


JAVA_SAFEMODE_ALL = "all"  # magic value for *all* options from JavaSafemodeOptions
JAVA_SAFEMODE_ALL_OPTIONS = [o.value for o in JavaSafemodeOptions]
JAVA_SAFEMODE_DEFAULT_OPTIONS = [
    JavaSafemodeOptions.PROFILED_OOM.value,
    JavaSafemodeOptions.PROFILED_SIGNALED.value,
    JavaSafemodeOptions.HSERR.value,
]


SUPPORTED_AP_MODES = ["cpu", "itimer", "alloc"]


# see StackWalkFeatures
# https://github.com/async-profiler/async-profiler/blob/a17529378b47e6700d84f89d74ca5e6284ffd1a6/src/arguments.h#L95-L112
class AsyncProfilerFeatures(str, Enum):
    # these will be controllable via "features" in a future AP release:
    #
    # unknown_java
    # unwind_stub
    # unwind_comp
    # unwind_native
    # java_anchor
    # gc_traces

    # these are controllable via "features" in AP 3.0
    probe_sp = "probesp"
    vtable_target = "vtable"
    comp_task = "comptask"
    # as of AP 3.0


SUPPORTED_AP_FEATURES = [o.value for o in AsyncProfilerFeatures]
DEFAULT_AP_FEATURES = [
    AsyncProfilerFeatures.probe_sp.value,
    AsyncProfilerFeatures.vtable_target.value,
]

# see options still here and not in "features":
# https://github.com/async-profiler/async-profiler/blob/a17529378b47e6700d84f89d74ca5e6284ffd1a6/src/arguments.cpp#L262
# we don't want any of them disabled by default.
JAVA_ASYNC_PROFILER_DEFAULT_SAFEMODE = 0

PROBLEMATIC_FRAME_REGEX = re.compile(r"^# Problematic frame:\n# (.*?)\n#\n", re.MULTILINE | re.DOTALL)
"""
See VMError::report.
Example:
    # Problematic frame:
    # C  [libasyncProfiler.so+0x218a0]  Profiler::getJavaTraceAsync(void*, ASGCT_CallFrame*, int)+0xe0
"""


class JavaFlagCollectionOptions(str, Enum):
    ALL = "all"
    DEFAULT = "default"
    NONE = "none"


class JattachExceptionBase(CalledProcessError):
    def __init__(
        self,
        returncode: int,
        cmd: Any,
        stdout: Any,
        stderr: Any,
        target_pid: int,
        ap_log: str,
        ap_loaded: str,
    ):
        super().__init__(returncode, cmd, stdout, stderr)
        self._target_pid = target_pid
        self._ap_log = ap_log
        self._ap_loaded = ap_loaded

    def __str__(self) -> str:
        ap_log = self._ap_log.strip()
        if not ap_log:
            ap_log = "(empty)"
        loaded_msg = f"async-profiler DSO loaded: {self._ap_loaded}"
        return super().__str__() + f"\nJava PID: {self._target_pid}\n{loaded_msg}\nasync-profiler log:\n{ap_log}"

    def get_ap_log(self) -> str:
        return self._ap_log

    @property
    def is_ap_loaded(self) -> bool:
        return self._ap_loaded == "yes"


class JattachException(JattachExceptionBase):
    pass


# doesn't extend JattachException itself, we're not just a jattach error, we're
# specifically the timeout one.
class JattachTimeout(JattachExceptionBase):
    def __init__(
        self,
        returncode: int,
        cmd: Any,
        stdout: Any,
        stderr: Any,
        target_pid: int,
        ap_log: str,
        ap_loaded: str,
        timeout: int,
    ):
        super().__init__(returncode, cmd, stdout, stderr, target_pid, ap_log, ap_loaded)
        self._timeout = timeout

    def __str__(self) -> str:
        return super().__str__() + (
            f"\njattach timed out (timeout was {self._timeout} seconds);"
            " you can increase it with the --java-jattach-timeout parameter."
        )


class JattachSocketMissingException(JattachExceptionBase):
    def __str__(self) -> str:
        # the attach listener is initialized once, then it is marked as initialized:
        # (https://github.com/openjdk/jdk/blob/3d07b3c7f01b60ff4dc38f62407c212b48883dbf/src/hotspot/share/services/attachListener.cpp#L388)
        # and will not be initialized again:
        # https://github.com/openjdk/jdk/blob/3d07b3c7f01b60ff4dc38f62407c212b48883dbf/src/hotspot/os/linux/attachListener_linux.cpp#L509
        # since openjdk 2870c9d55efe, the attach socket will be recreated even when removed (and this exception
        # won't happen).
        return super().__str__() + (
            "\nJVM attach socket is missing and jattach could not create it. It has most"
            " likely been removed; the process has to be restarted for a new socket to be created."
        )


class JattachJcmdRunner:
    def __init__(
        self,
        stop_event: Event,
        jattach_timeout: int,
        asprof_path: str,
        logger: Union[logging.LoggerAdapter, logging.Logger],
    ):
        self.stop_event = stop_event
        self.jattach_timeout = jattach_timeout
        self.asprof_path = asprof_path
        self.logger = logger

    def run(self, process: Process, cmd: str) -> str:
        try:
            return run_process(
                [self.asprof_path, "jcmd", str(process.pid), cmd],
                self.logger,
                stop_event=self.stop_event,
                timeout=self.jattach_timeout,
            ).stdout.decode()
        except CalledProcessError as e:
            if f"Process {process.pid} not found" in str(e):
                raise NoSuchProcess(process.pid)
            raise e


def is_java_basename(process: Process) -> bool:
    return is_process_basename_matching(process, r"^java$")


_JAVA_VERSION_TIMEOUT = 5

JAVA_VERSION_CACHE_MAX = 1024


def _get_process_ns_java_path(process: Process) -> Optional[str]:
    """
    Look up path to java executable installed together with this process' libjvm.
    """
    # This has the benefit of working even if the Java binary was replaced, e.g due to an upgrade.
    # in that case, the libraries would have been replaced as well, and therefore we're actually checking
    # the version of the now installed Java, and not the running one.
    # but since this is used for the "JDK type" check, it's good enough - we don't expect that to change.
    # this whole check, however, is growing to be too complex, and we should consider other approaches
    # for it:
    # 1. purely in async-profiler - before calling any APIs that might harm blacklisted JDKs, we can
    #    check the JDK type in async-profiler itself.
    # 2. assume JDK type by the path, e.g the "java" Docker image has
    #    "/usr/lib/jvm/java-8-openjdk-amd64/jre/bin/java" which means "OpenJDK". needs to be checked for
    #    other JDK types.
    if is_java_basename(process):
        nspid = get_process_nspid(process.pid)
        return f"/proc/{nspid}/exe"  # it's a symlink and will be resolveable under process' mnt ns
    libjvm_path: Optional[str] = None
    for m in process.memory_maps():
        if re.match(DETECTED_JAVA_PROCESSES_REGEX, m.path):
            libjvm_path = m.path
            break
    if libjvm_path is not None:
        libjvm_dir = os.path.dirname(libjvm_path)
        # support two java layouts - it's either lib/server/../../bin/java or lib/{arch}/server/../../../bin/java:
        java_candidate_paths = [
            Path(libjvm_dir, "../../bin/java").resolve(),
            Path(libjvm_dir, "../../../bin/java").resolve(),
        ]
        for java_path in java_candidate_paths:
            # don't need resolve_proc_root_links here - paths in /proc/pid/maps are normalized.
            proc_relative_path = Path(f"/proc/{process.pid}/root", java_path.relative_to("/"))
            if proc_relative_path.exists():
                if os.access(proc_relative_path, os.X_OK):
                    return str(java_path)
    return None


# process is hashable and the same process instance compares equal
@functools.lru_cache(maxsize=JAVA_VERSION_CACHE_MAX)
def get_java_version(
    process: Process,
    stop_event: Event,
    logger: Union[logging.LoggerAdapter, logging.Logger],
) -> Optional[str]:
    # make sure we're able to find "java" binary bundled with process libjvm
    process_java_path = _get_process_ns_java_path(process)
    if process_java_path is None:
        return None

    def _run_java_version() -> "CompletedProcess[bytes]":
        return run_process(
            [
                cast(str, process_java_path),
                "-version",
            ],
            logger,
            stop_event=stop_event,
            timeout=_JAVA_VERSION_TIMEOUT,
        )

    # doesn't work without changing PID NS as well (I'm getting ENOENT for libjli.so)
    # Version is printed to stderr
    return run_in_ns(["pid", "mnt"], _run_java_version, process.pid).stderr.decode().strip()


def get_java_version_logged(
    process: Process,
    stop_event: Event,
    logger: Union[logging.LoggerAdapter, logging.Logger],
) -> Optional[str]:
    java_version = get_java_version(process, stop_event, logger)
    logger.debug("java -version output", extra={"java_version_output": java_version, "pid": process.pid})
    return java_version


T = TypeVar("T", bound="AsyncProfiledProcess")

# Format is defined by async-profiler here: (Granulate's fork logs the output to logger, hence the `INFO` prefix)
# https://github.com/jvm-profiling-tools/async-profiler/blob/7eaefdb18f331962dc4c78c82322aec257e95c6c/src/profiler.cpp#L1204

MEM_INFO_LOG_RE = re.compile(
    r"\[INFO\] Call trace storage:\s*(\d+) "
    r"KB\n\s*Dictionaries:\s*(\d+) KB\n\s*Code cache:\s*(\d+) KB\n-*\n\s*Total:\s*(\d+) "
    r"KB\n\n"
)


class AsyncProfiledProcess:
    """
    Represents a process profiled with async-profiler.
    """

    FORMAT_PARAMS = "ann,sig"
    OUTPUT_FORMAT = "collapsed"
    OUTPUTS_MODE = 0o622  # readable by root, writable by all

    # timeouts in seconds
    _FDTRANSFER_TIMEOUT = 10
    _DEFAULT_JATTACH_TIMEOUT = 30  # higher than jattach's timeout

    _DEFAULT_MCACHE = 30  # arbitrarily chosen, not too high & not too low.

    def __init__(
        self,
        process: Process,
        stop_event: Event,
        storage_dir: str,
        insert_dso_name: bool,
        asprof_path: str,
        ap_version: str,
        libap_path_glibc: str,
        libap_path_musl: str,
        mode: str,
        ap_safemode: int,
        ap_features: List[str],
        ap_args: str,
        logger: Union[logging.LoggerAdapter, logging.Logger],
        jattach_timeout: int = _DEFAULT_JATTACH_TIMEOUT,
        mcache: int = 0,
        collect_meminfo: bool = True,
        include_method_modifiers: bool = False,
        java_line_numbers: str = "none",
    ):
        self.logger = logger
        self.process = process
        # access the process' root via its topmost parent/ancestor which uses the same mount namespace.
        # this allows us to access the files after the process exits:
        # * for processes that run in host mount NS - their ancestor is always available (it's going to be PID 1)
        # * for processes that run in a container, and the container remains running after they exit - hence, the
        #   ancestor is still alive.
        # there is a hidden assumption here that neither the ancestor nor the process will change their mount
        # namespace. I think it's okay to assume that.
        self._process_root = get_proc_root_path(process)
        self._cmdline = process.cmdline()
        self._cwd = process.cwd()
        self._nspid = get_process_nspid(self.process.pid)

        # not using storage_dir for AP itself on purpose: this path should remain constant for the lifetime
        # of the target process, so AP is loaded exactly once (if we have multiple paths, AP can be loaded
        # multiple times into the process)
        # without depending on storage_dir here, we maintain the same path even if gProfiler is re-run,
        # because storage_dir changes between runs.
        # we embed the async-profiler version in the path, so future gprofiler versions which use another version
        # of AP case use it (will be loaded as a different DSO)
        self._ap_dir_base = self._find_rw_exec_dir()
        self._ap_dir_versioned = os.path.join(self._ap_dir_base, f"async-profiler-{ap_version}")
        self._ap_dir_host = os.path.join(
            self._ap_dir_versioned,
            "musl" if self._needs_musl_ap() else "glibc",
        )

        self._libap_path_host = os.path.join(self._ap_dir_host, "libasyncProfiler.so")
        self._libap_path_process = remove_prefix(self._libap_path_host, self._process_root)

        # for other purposes - we can use storage_dir.
        self._storage_dir_host = resolve_proc_root_links(self._process_root, storage_dir)

        self._output_path_host = os.path.join(self._storage_dir_host, f"async-profiler-{self.process.pid}.output")
        self._output_path_process = remove_prefix(self._output_path_host, self._process_root)
        self._log_path_host = os.path.join(self._storage_dir_host, f"async-profiler-{self.process.pid}.log")
        self._log_path_process = remove_prefix(self._log_path_host, self._process_root)

        self._stop_event = stop_event
        self._insert_dso_name = insert_dso_name
        self._asprof_path = asprof_path
        self._libap_path_glibc = libap_path_glibc
        self._libap_path_musl = libap_path_musl

        # assert mode in ("cpu", "itimer", "alloc"), f"unexpected mode: {mode}"
        self._mode = mode
        self._fdtransfer_path = f"@async-profiler-{process.pid}-{secrets.token_hex(10)}" if mode == "cpu" else None
        self._ap_safemode = ap_safemode
        self._ap_features = ap_features
        self._ap_args = ap_args
        self._jattach_timeout = jattach_timeout
        self._mcache = mcache
        self._collect_meminfo = collect_meminfo
        self._include_method_modifiers = ",includemm" if include_method_modifiers else ""
        self._include_line_numbers = ",includeln" if java_line_numbers == "line-of-function" else ""

    def _find_rw_exec_dir(self) -> str:
        """
        Find a rw & executable directory (in the context of the process) where we can place libasyncProfiler.so
        and the target process will be able to load it.
        This function creates the gprofiler_tmp directory as a directory owned by root, if it doesn't exist under the
        chosen rwx directory.
        It does not create the parent directory itself, if it doesn't exist (e.g /run).
        The chosen rwx directory needs to be owned by root.
        """
        for d in POSSIBLE_AP_DIRS:
            full_dir = Path(resolve_proc_root_links(self._process_root, d))
            if not full_dir.parent.exists():
                continue  # we do not create the parent.

            if not is_owned_by_root(full_dir.parent):
                continue  # the parent needs to be owned by root

            try:
                mkdir_owned_root(full_dir)
            except OSError as e:
                # dir is not r/w, try next one
                if e.errno == errno.EROFS:
                    continue
                raise

            if is_rw_exec_dir(full_dir, self.logger):
                return str(full_dir)
        else:
            raise NoRwExecDirectoryFoundError(
                f"Could not find a rw & exec directory out of {POSSIBLE_AP_DIRS} for {self._process_root}!"
            )

    def __enter__(self: T) -> T:
        # create the directory structure for executable libap, make sure it's owned by root
        # for sanity & simplicity, mkdir_owned_root() does not support creating parent directories, as this allows
        # the caller to absentmindedly ignore the check of the parents ownership.
        # hence we create the structure here part by part.
        assert is_owned_by_root(
            Path(self._ap_dir_base)
        ), f"expected {self._ap_dir_base} to be owned by root at this point"
        mkdir_owned_root(self._ap_dir_versioned)
        mkdir_owned_root(self._ap_dir_host)
        os.makedirs(self._storage_dir_host, 0o755, exist_ok=True)

        self._check_disk_requirements()

        # make out & log paths writable for all, so target process can write to them.
        # see comment on TemporaryDirectoryWithMode in GProfiler.__init__.
        touch_path(self._output_path_host, self.OUTPUTS_MODE)
        self._recreate_log()
        # copy libasyncProfiler.so if needed
        self._copy_libap()

        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_ctb: Optional[TracebackType],
    ) -> None:
        # ignore_errors because we are deleting paths via /proc/pid/root - and the pid
        # we're using might have gone down already.
        # remove them as best effort.
        remove_path(self._output_path_host, missing_ok=True)
        remove_path(self._log_path_host, missing_ok=True)

    def _existing_realpath(self, path: str) -> Optional[str]:
        """
        Return path relative to process working directory if it exists. Otherwise return None.
        """
        if not path.startswith("/"):
            # relative path
            path = f"{self._cwd}/{path}"
        path = resolve_proc_root_links(self._process_root, path)
        return path if os.path.exists(path) else None

    def locate_hotspot_error_file(self) -> Optional[str]:
        for path in locate_hotspot_error_file(self._nspid, self._cmdline):
            realpath = self._existing_realpath(path)
            if realpath is not None:
                return realpath
        return None

    def _needs_musl_ap(self) -> bool:
        """
        Should we use the musl build of AP for this process?
        """
        return needs_musl_ap_cached(self.process)

    def _copy_libap(self) -> None:
        # copy *is* racy with respect to other processes running in the same namespace, because they all use
        # the same directory for libasyncProfiler.so.
        # therefore, we need to synchronize copies from different threads that profile different processes.
        if os.path.exists(self._libap_path_host):
            # all good
            return

        with libap_copy_lock:
            if not os.path.exists(self._libap_path_host):
                # atomically copy it
                libap_resource = self._libap_path_musl if self._needs_musl_ap() else self._libap_path_glibc
                os.chmod(
                    libap_resource, 0o755
                )  # make it accessible for all; needed with PyInstaller, which extracts files as 0700
                safe_copy(libap_resource, self._libap_path_host)

    def _recreate_log(self) -> None:
        touch_path(self._log_path_host, self.OUTPUTS_MODE)

    def _check_disk_requirements(self) -> None:
        """
        Avoid running if disk space is low, so we don't reach out-of-disk space situation because of profiling data.
        """
        free_disk = psutil.disk_usage(self._storage_dir_host).free
        required = 250 * 1024
        if free_disk < required:
            raise Exception(
                f"Not enough free disk space: {free_disk}kb left, {250 * 1024}kb"
                f" required (on path: {self._output_path_host!r}"
            )

    def _get_base_cmd(self) -> List[str]:
        return [
            self._asprof_path,
            str(self.process.pid),
            "load",
            self._libap_path_process,
            "true",  # 'true' means the given path ^^ is absolute.
        ]

    def _get_extra_ap_args(self) -> str:
        return f",{self._ap_args}" if self._ap_args else ""

    def _get_ap_output_args(self) -> str:
        return (
            f",file={self._output_path_process},{self.OUTPUT_FORMAT},"
            + f"{self.FORMAT_PARAMS}{self._include_method_modifiers}{self._include_line_numbers}"
        )

    def _get_interval_arg(self, interval: int) -> str:
        if self._mode == "alloc":
            return f",alloc={interval}"
        return f",interval={interval}"

    def _get_start_cmd(self, interval: int, ap_timeout: int) -> List[str]:
        return self._get_base_cmd() + [
            f"start,event={self._mode}"
            f"{self._get_ap_output_args()}{self._get_interval_arg(interval)},"
            f"log={self._log_path_process}"
            f"{f',fdtransfer={self._fdtransfer_path}' if self._mode == 'cpu' else ''}"
            f",safemode={self._ap_safemode},"
            f",features={'+'.join(self._ap_features)},"  # asprof uses '+' as a separator: https://github.com/async-profiler/async-profiler/blob/a17529378b47e6700d84f89d74ca5e6284ffd1a6/src/launcher/main.cpp#L441  # noqa
            f"timeout={ap_timeout}"
            f"{',lib' if self._insert_dso_name else ''}{self._get_extra_ap_args()}"
        ]

    def _get_stop_cmd(self, with_output: bool) -> List[str]:
        return self._get_base_cmd() + [
            f"stop,log={self._log_path_process},mcache={self._mcache}"
            f"{self._get_ap_output_args() if with_output else ''}"
            f"{',lib' if self._insert_dso_name else ''}{',meminfolog' if self._collect_meminfo else ''}"
            f"{self._get_extra_ap_args()}"
        ]

    def _read_ap_log(self) -> str:
        if not os.path.exists(self._log_path_host):
            return "(log file doesn't exist)"

        log = Path(self._log_path_host)
        ap_log = log.read_text()
        # clean immediately so we don't mix log messages from multiple invocations.
        # this is also what AP's profiler.sh does.
        log.unlink()
        self._recreate_log()
        return ap_log

    def _run_async_profiler(self, cmd: List[str]) -> str:
        try:
            # kill jattach with SIGTERM if it hangs. it will go down
            run_process(
                cmd,
                self.logger,
                stop_event=self._stop_event,
                timeout=self._jattach_timeout,
                kill_signal=signal.SIGTERM,
            )
        except CalledProcessError as e:  # catches CalledProcessTimeoutError as well
            assert isinstance(e.stderr, str), f"unexpected type {type(e.stderr)}"

            ap_log = self._read_ap_log()
            try:
                ap_loaded = (
                    "yes" if f" {self._libap_path_process}\n" in read_proc_file(self.process, "maps").decode() else "no"
                )
            except NoSuchProcess:
                ap_loaded = "not sure, process exited"

            args = (
                e.returncode,
                e.cmd,
                e.stdout,
                e.stderr,
                self.process.pid,
                ap_log,
                ap_loaded,
            )
            if isinstance(e, CalledProcessTimeoutError):
                raise JattachTimeout(*args, timeout=self._jattach_timeout) from None
            elif e.stderr == "Could not start attach mechanism: No such file or directory\n":
                # this is true for jattach_hotspot
                raise JattachSocketMissingException(*args) from None
            else:
                raise JattachException(*args) from None
        else:
            ap_log = self._read_ap_log()
            ap_log_stripped = MEM_INFO_LOG_RE.sub("", ap_log)  # strip out mem info log only when for gProfiler log
            self.logger.debug("async-profiler log", extra={"jattach_cmd": cmd, "ap_log": ap_log_stripped})
            return ap_log

    def _run_fdtransfer(self) -> None:
        """
        Start fdtransfer; it will fork & exit once ready, so we can continue with jattach.
        """
        assert self._fdtransfer_path is not None  # should be set if fdntransfer is invoked
        run_process(
            # run fdtransfer with accept timeout that's slightly greater than the jattach timeout - to make
            # sure that fdtransfer is still around for the full duration of jattach, in case the application
            # takes a while to accept & handle the connection.
            [
                self._asprof_path,
                "fdtransfer",
                "--fd-path",
                self._fdtransfer_path,
                "--fdtransfer-timeout",
                str(self._jattach_timeout + 5),
                str(self.process.pid),
            ],
            self.logger,
            stop_event=self._stop_event,
            timeout=self._FDTRANSFER_TIMEOUT,
        )

    def start_async_profiler(self, interval: int, second_try: bool = False, ap_timeout: int = 0) -> bool:
        """
        Returns True if profiling was started; False if it was already started.
        ap_timeout defaults to 0, which means "no timeout" for AP (see call to startTimer() in profiler.cpp)
        """
        if self._mode == "cpu" and not second_try:
            self._run_fdtransfer()

        start_cmd = self._get_start_cmd(interval, ap_timeout)
        try:
            self._run_async_profiler(start_cmd)
            return True
        except JattachException as e:
            if e.is_ap_loaded:
                if (
                    e.returncode == 200  # 200 == AP's COMMAND_ERROR
                    # this is the error we get when we try to start AP on a process that already has it loaded.
                    # check with "in" and not "==" in case other warnings/infos are printed alongside it,
                    # but generally, we expect it to be the only output in this case.
                    and "[ERROR] Profiler already started\n" in e.get_ap_log()
                ):
                    # profiler was already running
                    return False
            raise

    def stop_async_profiler(self, with_output: bool) -> str:
        return self._run_async_profiler(self._get_stop_cmd(with_output))

    def read_output(self) -> Optional[str]:
        try:
            return Path(self._output_path_host).read_text()
        except FileNotFoundError:
            # perhaps it has exited?
            if not is_process_running(self.process):
                return None
            raise
