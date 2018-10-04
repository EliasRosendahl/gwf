import re
import logging
import subprocess
from distutils.spawn import find_executable
from xml.etree import ElementTree

from . import Backend, Status
from .exceptions import BackendError, UnknownDependencyError, UnknownTargetError
from .logmanager import FileLogManager
from ..utils import PersistableDict

logger = logging.getLogger(__name__)


SGE_OPTIONS = {
    "cores": "-pe smp ",
    "memory": "-l h_vmem=",
    "walltime": "-l h_rt=",
    "queue": "-q ",
    "account": "-P ",
}


def _find_exe(name):
    exe = find_executable(name)
    if exe is None:
        msg = (
            'Could not find executable "{}". This backend requires Sun Grid '
            'Engine (SGE) to be installed on this host.'
        )
        raise BackendError(msg.format(name))
    return exe


def _call_generic(executable_name, *args, input=None):
    executable_path = _find_exe(executable_name)
    proc = subprocess.Popen(
        [executable_path] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, stderr = proc.communicate(input)

    if proc.returncode != 0:
        raise BackendError(stderr)
    return stdout


def _call_qstat():
    return _call_generic("qstat", "-f", "-xml")


def _call_qdel(job_id):
    # The --verbose flag here is necessary, otherwise we're not able to tell
    # whether the command failed. See the comment in _call_generic() if you
    # want to know more.
    return _call_generic("qdel", job_id)


def _call_qsub(script, dependencies):
    args = ["-terse"]
    if dependencies:
        args.append("-hold_jid")
        args.append(",".join(dependencies))
    return _call_generic("qsub", *args, input=script)


def _parse_qstat_output(stdout):
    job_states = {}
    root = ElementTree.fromstring(stdout)
    for job in root.iter('job_list'):
        job_id = job.find('JB_job_number').text
        state = job.find('state').text

        # Guessing job state based on
        # https://gist.github.com/cmaureir/4fa2d34bc9a1bd194af1
        if 'd' in state or 'E' in state:
            job_state = Status.UNKNOWN
        elif 'r' in state or 't' in state or 's' in state:
            job_state = Status.RUNNING
        else:
            job_state = Status.SUBMITTED
        job_states[job_id] = job_state
    return job_states


class SGEBackend(Backend):
    """Backend for Sun Grid Engine (SGE).

    To use this backend you must activate the `sge` backend. The backend
    currently assumes that a SGE parallel environment called "smp" is
    available. You can check which parallel environments are available on your
    system by running :command:`qconf -spl`.

    **Backend options:**

    None.

    **Target options:**

    * **cores (int):**
      Number of cores allocated to this target (default: 1).
    * **memory (str):**
      Memory allocated to this target (default: 1).
    * **walltime (str):**
      Time limit for this target (default: 01:00:00).
    * **queue (str):**
      Queue to submit the target to. To specify multiple queues, specify a
      comma-separated list of queue names.
    * **account (str):**
      Account to be used when running the target. Corresponds to the SGE
      project.
    """

    log_manager = FileLogManager()

    option_defaults = {
        "cores": 1,
        "memory": "1g",
        "walltime": "01:00:00",
        "queue": None,
        "account": None,
    }

    def __init__(self):
        self._status = _parse_qstat_output(_call_qstat())
        self._tracked = PersistableDict(path=".gwf/sge-backend-tracked.json")

    def status(self, target):
        try:
            return self._get_status(target)
        except KeyError:
            return Status.UNKNOWN

    def submit(self, target, dependencies):
        script = self._compile_script(target)
        dependency_ids = self._collect_dependency_ids(dependencies)
        stdout = _call_qsub(script, dependency_ids)
        job_id = stdout.strip()
        self._add_job(target, job_id)

    def cancel(self, target):
        try:
            job_id = self.get_job_id(target)
            _call_qdel(job_id)
        except (KeyError, BackendError):
            raise UnknownTargetError(target.name)
        else:
            self.forget_job(target)

    def close(self):
        self._tracked.persist()

    def forget_job(self, target):
        """Force the backend to forget the job associated with `target`."""
        job_id = self.get_job_id(target)
        del self._status[job_id]
        del self._tracked[target.name]

    def get_job_id(self, target):
        """Get the SGE job id for a target.

        :raises KeyError: if the target is not tracked by the backend.
        """
        return self._tracked[target.name]

    def _compile_script(self, target):
        option_str = "#$ {0}{1}"

        out = []
        out.append("#!/bin/bash")
        out.append("# Generated by: gwf")

        out.append(option_str.format("-N ", target.name))
        out.append("#$ -V")
        out.append("#$ -w v")
        out.append("#$ -cwd")

        for option_name, option_value in target.options.items():
            # SGE wants per-core memory, but gwf wants total memory.
            if option_name == 'memory':
                number = int(re.sub(r'[^0-9]+', '', option_value))
                unit = re.sub(r'[0-9]+', '', option_value)
                cores = target.options['cores']
                option_value = '{}{}'.format(number // cores, unit)
            out.append(option_str.format(SGE_OPTIONS[option_name], option_value))

        out.append(
            option_str.format("-o ", self.log_manager.stdout_path(target))
        )
        out.append(
            option_str.format("-e ", self.log_manager.stderr_path(target))
        )

        out.append("")
        out.append("cd {}".format(target.working_dir))
        out.append("export GWF_JOBID=$SGE_JOBID")
        out.append('export GWF_TARGET_NAME="{}"'.format(target.name))
        out.append("set -e")
        out.append("")
        out.append(target.spec)
        return "\n".join(out)

    def _add_job(self, target, job_id, initial_status=Status.SUBMITTED):
        self._set_job_id(target, job_id)
        self._set_status(target, initial_status)

    def _set_job_id(self, target, job_id):
        self._tracked[target.name] = job_id

    def _get_status(self, target):
        job_id = self.get_job_id(target)
        return self._status[job_id]

    def _set_status(self, target, status):
        job_id = self.get_job_id(target)
        self._status[job_id] = status

    def _collect_dependency_ids(self, dependencies):
        try:
            return [self._tracked[dep.name] for dep in dependencies]
        except KeyError as exc:
            raise UnknownDependencyError(exc.args[0])
