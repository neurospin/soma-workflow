from __future__ import with_statement, print_function

'''
author: Soizic Laguitton

organization: I2BM, Neurospin, Gif-sur-Yvette, France
organization: CATI, France
organization: IFR 49<http://www.ifr49.org

license: CeCILL version 2, http://www.cecill.info/licences/Licence_CeCILL_V2-en.html
'''

from ..scheduler import Scheduler
import sys
try:
    try:
        # subprocess32 seems to have its own zmq implementation,
        # which proves to be incompatible (too old) with some other modules
        # (such as ipython). We load the "official" one first to avoid problems
        import zmq
    except:
        pass
    import subprocess32 as subprocess
    import subprocess as _subprocess
    if hasattr(_subprocess, '_args_from_interpreter_flags'):
        # get this private function which is used somewhere in
        # multiprocessing
        subprocess._args_from_interpreter_flags \
            = _subprocess._args_from_interpreter_flags
    del _subprocess
    import sys
    sys.modules['subprocess'] = sys.modules['subprocess32']
#    print('using subprocess32')
except ImportError:
#    print('subprocess32 could not be loaded - processes may hangup')
    import subprocess
import threading
import time
import os
import signal
import ctypes
import atexit
import six
import logging

try:
    # psutil is used to correctly kill a job with its children processes
    import psutil
    have_psutil = True
except ImportError:
    have_psutil = False

import soma_workflow.constants as constants
from soma_workflow.configuration import LocalSchedulerCfg
from soma_workflow.configuration import default_cpu_number, cpu_count


class LocalSchedulerError(Exception):
    pass


class LocalScheduler(Scheduler):

    '''
    Allow to submit, kill and get the status of jobs.
    Run on one machine without dependencies.

    * _proc_nb *int*

    * _queue *list of scheduler jobs ids*

    * _jobs *dictionary job_id -> soma_workflow.engine_types.EngineJob*

    * _processes *dictionary job_id -> subprocess.Popen*

    * _status *dictionary job_id -> job status as defined in constants*

    * _exit_info * dictionay job_id -> exit info*

    * _loop *thread*

    * _interval *int*

    * _lock *threading.RLock*
    '''
    parallel_job_submission_info = None

    #logger = None

    _proc_nb = None

    _max_proc_nb = None

    _queue = None

    _jobs = None

    _processes = None

    _status = None

    _exit_info = None

    _loop = None

    _interval = None

    _lock = None

    _lasttime = None
    _lastidle = None

    def __init__(self, proc_nb=default_cpu_number(), interval=1,
                 max_proc_nb=0):
        super(LocalScheduler, self).__init__()

        self.parallel_job_submission_info = None

        self._proc_nb = proc_nb
        self._max_proc_nb = max_proc_nb
        self._interval = interval
        self._queue = []
        self._jobs = {}
        self._processes = {}
        self._status = {}
        self._exit_info = {}

        self._lock = threading.RLock()

        self.stop_thread_loop = False

        def loop(self):
            while not self.stop_thread_loop:
                with self._lock:
                    self._iterate()
                time.sleep(self._interval)

        self._loop = threading.Thread(name="scheduler_loop",
                                      target=loop,
                                      args=[self])
        self._loop.setDaemon(True)
        self._loop.start()

        atexit.register(LocalScheduler.end_scheduler_thread, self)
        if LocalScheduler.logger is None:
            LocalScheduler.logger = logging.getLogger('engine.Scheduler')

    def change_proc_nb(self, proc_nb):
        with self._lock:
            self._proc_nb = proc_nb

    def change_max_proc_nb(self, proc_nb):
        with self._lock:
            self._max_proc_nb = proc_nb

    def change_interval(self, interval):
        with self._lock:
            self._interval = interval

    def end_scheduler_thread(self):
        with self._lock:
            self.stop_thread_loop = True
            self._loop.join()
            # print("Soma scheduler thread ended nicely.")

    def _iterate(self):
        # Nothing to do if the queue is empty and nothing is running
        if not self._queue and not self._processes:
            return
        # print("#############################")
        # Control the running jobs
        ended_jobs = []
        for job_id, process in six.iteritems(self._processes):
            ret_value = process.poll()
            # print("job_id " + repr(job_id) + " ret_value " + repr(ret_value))
            if ret_value != None:
                ended_jobs.append(job_id)
                self._exit_info[job_id] = (constants.FINISHED_REGULARLY,
                                           ret_value,
                                           None,
                                           None)

        # update for the ended job
        for job_id in ended_jobs:
            # print("updated job_id " + repr(job_id) + " status DONE")
            self._status[job_id] = constants.DONE
            del self._processes[job_id]

        # run new jobs
        skipped_jobs = []
        #print('processing queue:', len(self._queue), file=sys.stderr)
        while self._queue:
            job_id = self._queue.pop(0)
            job = self._jobs[job_id]
            # print("new job " + repr(job.job_id))
            if job.is_barrier:
                # barrier jobs are not actually run using Popen:
                # they succeed immediately.
                self._exit_info[job.drmaa_id] = (constants.FINISHED_REGULARLY,
                                               0,
                                               None,
                                               None)
                self._status[job.drmaa_id] = constants.DONE
            else:
                ncpu = self._cpu_for_job(job)
                #print('job:', job.command, ', cpus:', ncpu, file=sys.stderr)
                if not self._can_submit_new_job(ncpu):
                    #print('cannot submit.', file=sys.stderr)
                    skipped_jobs.append(job_id) # postponed
                    if ncpu == 1: # no other job will be able to run now
                        break
                    else:
                        continue
                #print('submitting.', file=sys.stderr)
                process = LocalScheduler.create_process(job)
                if process == None:
                    LocalScheduler.logger.error('command process is None:' + job.name)
                    self._exit_info[job.drmaa_id] = (constants.EXIT_ABORTED,
                                                   None,
                                                   None,
                                                   None)
                    self._status[job.drmaa_id] = constants.FAILED
                else:
                    self._processes[job.drmaa_id] = process
                    self._status[job.drmaa_id] = constants.RUNNING
        self._queue = skipped_jobs + self._queue

    def _cpu_for_job(self, job):
        parallel_job_info = job.parallel_job_info
        if parallel_job_info is None:
            return 1
        ncpu = parallel_job_info.get('nodes_number', 1) \
            * parallel_job_info.get('cpu_per_node', 1)
        return ncpu

    def _can_submit_new_job(self, ncpu=1):
        n = sum([self._cpu_for_job(self._jobs[j])
                 for j in self._processes])
        n += ncpu
        if n <= self._proc_nb:
            return True
        max_proc_nb = self._max_proc_nb
        if max_proc_nb == 0:
            if have_psutil:
                max_proc_nb = cpu_count()
            else:
                max_proc_nb = cpu_count() - 1
        if n <= max_proc_nb and self.is_available_cpu(ncpu):
            return True
        return False

    @staticmethod
    def is_available_cpu(ncpu=1):
        # OK if there is at least one half CPU left idle
        if have_psutil:
            if LocalScheduler._lasttime is None \
                    or time.time() - LocalScheduler._lasttime > 0.1:
                LocalScheduler._lasttime = time.time()
                LocalScheduler._lastidle = psutil.cpu_times_percent().idle \
                    * psutil.cpu_count() * 0.01
            # we allow to run a new job if:
            # * the job is monocore and there is 20% of a CPU left
            # * there is at least 80% of a CPU. This is arbitrary and needs
            #   tweaking but it's not so easy since if the job asks for more
            #   CPU than there are actually, it must not be stuck forever
            #   anyway, and we can't really forecast if the machine load will
            #   actually decrease in the future. But it can certainly be better
            #   than this...
            if (ncpu == 1 and LocalScheduler._lastidle > 0.2) \
                    or (LocalScheduler._lastidle > 0.8):
                # decrease artificially idle because we will submit a job,
                # and next calls should take it into account
                # until another measurement is done.
                LocalScheduler._lastidle -= ncpu # increase load artificially
                return True
            return False
        # no psutil: get to the upper limit.
        return True

    @staticmethod
    def create_process(engine_job):
        '''
        * engine_job *EngineJob*

        * returns: *Subprocess process*
        '''

        command = engine_job.plain_command()
        env = engine_job.env

        stdout = engine_job.plain_stdout()
        stdout_file = None
        if stdout:
            try:
                stdout_file = open(stdout, "wb")
            except Exception as e:
                return None

        stderr = engine_job.plain_stderr()
        stderr_file = None
        if stderr:
            try:
                stderr_file = open(stderr, "wb")
            except Exception as e:
                if stdout_file:
                    stdout_file.close()
                return None

        stdin = engine_job.plain_stdin()
        stdin_file = None
        if stdin:
            try:
                stdin_file = open(stdin, "rb")
            except Exception as e:
                if stderr:
                    s = '%s: %s \n' % (type(e), e)
                    stderr_file.write(s)
                elif stdout:
                    s = '%s: %s \n' % (type(e), e)
                    stdout_file.write(s)
                if stderr_file:
                    stderr_file.close()
                if stdout_file:
                    stdout_file.close()
                return None

        working_directory = engine_job.plain_working_directory()

        try:
            if not have_psutil and sys.platform != 'win32':
                # if psutil is not here, use process group/session, to allow killing
                # children processes as well. see
                # http://stackoverflow.com/questions/4789837/how-to-terminate-a-python-subprocess-launched-with-shell-true
                kwargs = {'preexec_fn': os.setsid}
            else:
                kwargs = {}
            if env is not None:
                env2 = dict(os.environ)
                if sys.platform.startswith('win') and sys.version_info[0] < 3:
                    # windows cannot use unicode strings as env values
                    env2.update([(k.encode('utf-8'), v.encode('utf-8')) for k, v in six.iteritems(env)])
                env = env2
            LocalScheduler.logger.debug('run command:' + repr(command))
            LocalScheduler.logger.debug('with env:' + repr(env))
            process = subprocess.Popen(command,
                                       stdin=stdin_file,
                                       stdout=stdout_file,
                                       stderr=stderr_file,
                                       cwd=working_directory,
                                       env=env,
                                       **kwargs)
            if stderr_file:
                stderr_file.close()
            if stdout_file:
                stdout_file.close()

        except Exception as e:
            LocalScheduler.logger.error('exception while running command:' + repr(e))
            if stderr:
                s = '%s: %s \n' % (type(e), e)
                stderr_file.write(s)
            elif stdout:
                s = '%s: %s \n' % (type(e), e)
                stdout_file.write(s)
            if stderr_file:
                stderr_file.close()
            if stdout_file:
                stdout_file.close()
            return None

        return process

    def job_submission(self, job):
        '''
        * job *EngineJob*
        * return: *string*
            Job id for the scheduling system (DRMAA for example)
        '''
        if not job.job_id or job.job_id == -1:
            raise LocalSchedulerError("Invalid job: no id")
        with self._lock:
            # print("job submission " + repr(job.job_id))
            drmaa_id = str(job.job_id)
            self._queue.append(drmaa_id)
            self._jobs[drmaa_id] = job
            self._status[drmaa_id] = constants.QUEUED_ACTIVE
            self._queue.sort(key=lambda job_id: self._jobs[drmaa_id].priority,
                             reverse=True)
        return drmaa_id

    def get_job_status(self, scheduler_job_id):
        '''
        * scheduler_job_id *string*
            Job id for the scheduling system (DRMAA for example)
        * return: *string*
            Job status as defined in constants.JOB_STATUS
        '''
        if not scheduler_job_id in self._status:
            raise LocalSchedulerError("Unknown job %s." % scheduler_job_id)

        status = self._status[scheduler_job_id]
        return status

    def get_job_exit_info(self, scheduler_job_id):
        '''
        * scheduler_job_id *string*
            Job id for the scheduling system (DRMAA for example)
        * return: *tuple*
            exit_status, exit_value, term_sig, resource_usage
        '''
        # TBI errors
        with self._lock:
            exit_info = self._exit_info[scheduler_job_id]
            del self._exit_info[scheduler_job_id]
        return exit_info

    def kill_job(self, scheduler_job_id):
        '''
        * scheduler_job_id *string*
            Job id for the scheduling system (DRMAA for example)
        '''
        # TBI Errors

        with self._lock:
            if scheduler_job_id in self._processes:
                # print("    => kill the process ")
                process = self._processes[scheduler_job_id]
                if have_psutil:
                    kill_process_tree(process.pid)
                    # wait for actual termination, to avoid process writing files after
                    # we return from here.
                    process.communicate()
                else:
                    # psutil not available
                    if sys.version_info < (2, 6):
                        if sys.platform == 'win32':
                            PROCESS_TERMINATE = 1
                            handle = ctypes.windll.kernel32.OpenProcess(
                                PROCESS_TERMINATE,
                                False,
                                process.pid)
                            ctypes.windll.kernel32.TerminateProcess(handle, -1)
                            ctypes.windll.kernel32.CloseHandle(handle)
                        else:
                            os.kill(process.pid, signal.SIGKILL)
                            os.wait()
                    else:
                        if sys.platform == 'win32':
                            # children processes will probably not be killed
                            # immediately.
                            process.kill()
                        else:
                            # kill process group, to kill children processes as well
                            # see
                            # http://stackoverflow.com/questions/4789837/how-to-terminate-a-python-subprocess-launched-with-shell-true
                            os.killpg(process.pid, signal.SIGKILL)

                    # wait for actual termination, to avoid process writing files after
                    # we return from here.
                    process.communicate()

                del self._processes[scheduler_job_id]
                self._status[scheduler_job_id] = constants.FAILED
                self._exit_info[scheduler_job_id] = (constants.USER_KILLED,
                                                     None,
                                                     None,
                                                     None)
            elif scheduler_job_id in self._queue:
                # print("    => removed from queue ")
                self._queue.remove(scheduler_job_id)
                del self._jobs[scheduler_job_id]
                self._status[scheduler_job_id] = constants.FAILED
                self._exit_info[scheduler_job_id] = (constants.EXIT_ABORTED,
                                                     None,
                                                     None,
                                                     None)


def kill_process_tree(pid):
    """
    Kill a process with its children.
    Needs psutil to get children list
    """
    process = psutil.Process(pid)
    for proc in process.children(recursive=True):
        proc.kill()
    process.kill()


class ConfiguredLocalScheduler(LocalScheduler):

    '''
    Local scheduler synchronized with a configuration object.
    '''

    _config = None

    def __init__(self, config):
        '''
        * config *LocalSchedulerCfg*
        '''
        super(ConfiguredLocalScheduler, self).__init__(
            config.get_proc_nb(),
            config.get_interval(),
            config.get_max_proc_nb())
        self._config = config

        self._config.addObserver(self,
                                 "update_from_config",
                                 [LocalSchedulerCfg.PROC_NB_CHANGED,
                                  LocalSchedulerCfg.INTERVAL_CHANGED,
                                  LocalSchedulerCfg.MAX_PROC_NB_CHANGED,])

    def update_from_config(self, observable, event, msg):
        if event == LocalSchedulerCfg.PROC_NB_CHANGED:
            self.change_proc_nb(self._config.get_proc_nb())
        elif event == LocalSchedulerCfg.INTERVAL_CHANGED:
            self.change_interval(self._config.get_interval())
        elif event == LocalSchedulerCfg.MAX_PROC_NB_CHANGED:
            self.change_max_proc_nb(self._config.get_max_proc_nb())
        self._config.save_to_file()

    @classmethod
    def build_scheduler(cls, config):
        local_scheduler_config = config.get_scheduler_config()
        if local_scheduler_config is None:
            local_scheduler_cfg_file_path \
                = LocalSchedulerCfg.search_config_path()
            if local_scheduler_cfg_file_path:
                local_scheduler_config = LocalSchedulerCfg.load_from_file(
                    local_scheduler_cfg_file_path)
            else:
                local_scheduler_config = LocalSchedulerCfg()
            config.set_scheduler_config(local_scheduler_config)
            
        sch = ConfiguredLocalScheduler(local_scheduler_config)
        return sch


# set the main scheduler for this module
__main_scheduler__ = ConfiguredLocalScheduler
