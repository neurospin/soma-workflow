'''
@author: Yann Cointepas
@author: Soizic Laguitton
@organization: U{IFR 49<http://www.ifr49.org>}
@license: U{CeCILL version 2<http://www.cecill.info/licences/Licence_CeCILL_V2-en.html>}
'''


from __future__ import with_statement
from soma.pipeline.somadrmaajobssip import DrmaaJobs
import Pyro.naming, Pyro.core
from Pyro.errors import NamingError
from datetime import date
from datetime import timedelta
import pwd
import os
import threading
import time
from datetime import datetime
import logging
import soma.jobs.constants as constants
import soma.jobs.jobServer 
import copy

__docformat__ = "epytext en"


refreshment_interval = 1 #seconds

class JobSchedulerError( Exception ): 
  def __init__(self, msg, logger = None):
    self.args = (msg,)
    if logger:
      logger.critical('EXCEPTION ' + msg)



class DrmaaJobScheduler( object ):

  '''
  Instances of this class opens a DRMAA session and allows to submit and control 
  the jobs. It updates constantly the jobs status on the L{JobServer}. 
  The L{DrmaaJobScheduler} must be created on one of the machine which is allowed
  to submit jobs by the DRMS.
  '''
  def __init__( self, job_server, parallel_job_submission_info = None):
    '''
    Opens a connection to the pool of machines and to the data server L{JobServer}.

    @type  job_server: L{JobServer}
    @type  parallel_job_submission_info: dictionary 
    @param parallel_job_submission_info: DRMAA doesn't provide an unified way of submitting
    parallel jobs. The value of parallel_job_submission is cluster dependant. 
    The keys are:
      - Drmaa job template attributes 
      - parallel configuration name as defined in soma.jobs.constants
    '''
    self.logger = logging.getLogger('ljp.drmaajs')
    
    self.__drmaa = DrmaaJobs()
    # patch for pbs-torque drmaa ##
    jobTemplateId = self.__drmaa.allocateJobTemplate()
    self.__drmaa.setCommand(jobTemplateId, "echo", [])
    self.__drmaa.setAttribute(jobTemplateId, "drmaa_output_path", "[void]:/dev/null")
    self.__drmaa.setAttribute(jobTemplateId, "drmaa_error_path", "[void]:/dev/null")
    self.__drmaa.runJob(jobTemplateId)
    ################################
    
    self.__jobServer = job_server

    self.logger.debug("Parallel job submission info: %s", repr(parallel_job_submission_info))
    self.__parallel_job_submission_info = parallel_job_submission_info

    try:
      userLogin = pwd.getpwuid(os.getuid())[0] 
    except Exception, e:
      self.logger.critical("Couldn't identify user %s: %s \n" %(type(e), e))
      raise SystemExit
    
    self.__user_id = self.__jobServer.registerUser(userLogin) 

    self.__jobs = {} 
    self.__workflows = {}
    self.__endedTransfers = set([])
    
    self.__lock = threading.RLock()
    
    self.__jobsEnded = False
    
    
    
    def startJobStatusUpdateLoop( self, interval ):
      logger_su = logging.getLogger('ljp.drmaajs.su')
      while True:
        # get rid of all the jobs that doesn't exist anymore
        with self.__lock:
          serverJobs = self.__jobServer.getJobs(self.__user_id)
          removed_from_server = set(self.__jobs.keys()).difference(serverJobs)
          for job_id in removed_from_server:
            del self.__jobs[job_id]
          #self.__jobs = self.__jobs.intersection(serverJobs)
          allJobsEnded = True
          ended = []
          for job_id in self.__jobs.keys():
            if self.__jobs[job_id].submitted:
              # get back the status from DRMAA
              status = self.__status(job_id)
              logger_su.debug("job " + repr(job_id) + " : " + status)
              if status == constants.DONE or status == constants.FAILED:
                # update the exit status and status on the job server 
                self.__endOfJob(job_id, status)
                ended.append(job_id)
              else:
                allJobsEnded = False
                # update the status on the job server 
                self.__jobServer.setJobStatus(job_id, status)
          self.__jobsEnded = allJobsEnded
          # get the exit information for terminated jobs and update the jobServer
          if ended or self.__endedTransfers:
            self.workflowProcessing(endedJobs = ended, endedTransfers = self.__endedTransfers )
            self.__endedTransfers.clear()
          for job_id in ended:
            del self.__jobs[job_id]
          
        logger_su.debug("---------- all jobs done : " + repr(self.__jobsEnded))
        time.sleep(interval)
    
    
    self.__job_status_thread = threading.Thread(name = "job_status_loop", 
                                                target = startJobStatusUpdateLoop, 
                                                args = (self, refreshment_interval))
    self.__job_status_thread.setDaemon(True)
    self.__job_status_thread.start()


   

  def __del__( self ):
    pass
    '''
    Closes the connection with the pool and the data server L{JobServer} and
    stops updating the L{JobServer}. (should be called when all the jobs are
    done) 
    '''
 
    
  def signalTransferEnded(self, local_file_path):
    '''
    WIP
    '''
    with self.__lock:
      self.logger.debug("signal transfer ended " + local_file_path)
      self.__endedTransfers.add(local_file_path)


  ########## JOB SUBMISSION #################################################

  def submit(self, jobTemplate):
    
    '''
    Implementation of the L{JobScheduler} method.
    '''
    self.logger.debug(">> submit")
      
    job_id = self.__registerJob(jobTemplate)
    
    self.__drmaaJobSubmission(jobTemplate.command, job_id)
    self.logger.debug("<< submit")
    return job_id
  
      
  def __registerJob(self,
                    jobTemplate_o,
                    workflow_id=-1):
    
    jobTemplate = copy.deepcopy(jobTemplate_o)
    
    expiration_date = date.today() + timedelta(hours=jobTemplate.disposal_timeout) 
    parallel_config_name = None
    max_node_number = 1

    if not jobTemplate.stdout_path:
      stdout_path = self.__jobServer.generateLocalFilePath(self.__user_id)
      stderr_path = self.__jobServer.generateLocalFilePath(self.__user_id)
      custom_submission = False #the std out and err file has to be removed with the job
    else:
      custom_submission = True #the std out and err file won't to be removed with the job
      stdout_path = jobTemplate.stdout_path
      stderr_path = jobTemplate.stderr_path
      
      
    if jobTemplate.parallel_job_info:
      parallel_config_name, max_node_number = jobTemplate.parallel_job_info
       
    command_info = ""
    for command_element in jobTemplate.command:
      command_info = command_info + " " + command_element
      
    with self.__lock:
      job_id = self.__jobServer.addJob( soma.jobs.jobServer.DBJob(
                                        user_id = self.__user_id, 
                                        custom_submission = custom_submission,
                                        expiration_date = expiration_date, 
                                        command = command_info,
                                        workflow_id = workflow_id,
                                        
                                        stdin_file = jobTemplate.stdin, 
                                        join_errout = jobTemplate.join_stderrout,
                                        stdout_file = stdout_path,
                                        stderr_file = stderr_path,
                                        working_directory = jobTemplate.working_directory,
                                        
                                        parallel_config_name = parallel_config_name,
                                        max_node_number = max_node_number,
                                        name_description = jobTemplate.name_description))
                                      
      if jobTemplate.referenced_input_files:
        self.__jobServer.registerInputs(job_id, jobTemplate.referenced_input_files)
      if jobTemplate.referenced_output_files:
        self.__jobServer.registerOutputs(job_id, jobTemplate.referenced_output_files)

    jobTemplate.job_id = job_id
    jobTemplate.workflow_id = workflow_id
    self.__jobs[job_id] = jobTemplate
    return job_id
        
  def __drmaaJobSubmission(self, command, job_id): 
    
    job = self.__jobServer.getJob(job_id)
    
    with self.__lock:
      
      drmaaJobTemplateId = self.__drmaa.allocateJobTemplate()
      self.__drmaa.setCommand(drmaaJobTemplateId, command[0], command[1:])
    
      self.__drmaa.setAttribute(drmaaJobTemplateId, "drmaa_output_path", "[void]:" + job.stdout_file)
      
      if job.join_errout:
        self.__drmaa.setAttribute(drmaaJobTemplateId,"drmaa_join_files", "y")
      else:
        if job.stderr_file:
          self.__drmaa.setAttribute(drmaaJobTemplateId, "drmaa_error_path", "[void]:" + job.stderr_file)
     
      if job.stdin_file:
        self.__drmaa.setAttribute(drmaaJobTemplateId, "drmaa_input_path", "[void]:" + job.stdin_file)
        
      if job.working_directory:
        self.__drmaa.setAttribute(drmaaJobTemplateId, "drmaa_wd", job.working_directory)
      
      if job.parallel_config_name :
        self.__setDrmaaParallelJobTemplate(drmaaJobTemplateId, job.parallel_config_name, job.max_node_number)

      drmaaSubmittedJobId = self.__drmaa.runJob(drmaaJobTemplateId)
      self.__drmaa.deleteJobTemplate(drmaaJobTemplateId)
     
      if drmaaSubmittedJobId == "":
        self.logger.error("Could not submit job: Drmaa problem.");
        return -1
      
      self.__jobServer.setSubmissionInformation(job_id, drmaaSubmittedJobId, date.today())
      self.__jobs[job_id].submitted = True
      
    self.logger.debug("job %s submitted! drmaa id = %s", job_id, drmaaSubmittedJobId)
    
    


  def __setDrmaaParallelJobTemplate(self, drmaa_job_template_id, configuration_name, max_num_node):
    '''
    Set the DRMAA job template information for a parallel job submission.
    The configuration file must provide the parallel job submission information specific 
    to the cluster in use. 

    @type  drmaa_job_template_id: string 
    @param drmaa_job_template_id: id of drmaa job template
    @type  parallel_job_info: tuple (string, int)
    @param parallel_job_info: (configuration_name, max_node_num)
    configuration_name: type of parallel job as defined in soma.jobs.constants (eg MPI, OpenMP...)
    max_node_num: maximum node number the job requests (on a unique machine or separated machine
    depending on the parallel configuration)
    ''' 

    self.logger.debug(">> __setDrmaaParallelJobTemplate")
    if not self.__parallel_job_submission_info:
      raise JobSchedulerError("Configuration file : Couldn't find parallel job submission information for this cluster.", self.logger)
    
    if configuration_name not in self.__parallel_job_submission_info:
      raise JobSchedulerError("Configuration file : couldn't find the parallel configuration %s for the current cluster." %(configuration_name), self.logger)

    cluster_specific_config_name = self.__parallel_job_submission_info[configuration_name]
    
    for drmaa_attribute in constants.PARALLEL_DRMAA_ATTRIBUTES:
      value = self.__parallel_job_submission_info.get(drmaa_attribute)
      if value: 
        #value = value.format(config_name=cluster_specific_config_name, max_node=max_num_node)
        value = value.replace("{config_name}", cluster_specific_config_name)
        value = value.replace("{max_node}", repr(max_num_node))
        with self.__lock:
          self.__drmaa.setAttribute( drmaa_job_template_id, 
                                    drmaa_attribute, 
                                    value)
          self.logger.debug("Parallel job, drmaa attribute = %s, value = %s ", drmaa_attribute, value) 


    job_env = []
    for parallel_env_v in constants.PARALLEL_JOB_ENV:
      value = self.__parallel_job_submission_info.get(parallel_env_v)
      if value: job_env.append(parallel_env_v+'='+value.rstrip())
    
    
    with self.__lock:
        self.__drmaa.setVectorAttribute(drmaa_job_template_id, 'drmaa_v_env', job_env)
        self.logger.debug("Parallel job environment : " + repr(job_env))
        
    self.logger.debug("<< __setDrmaaParallelJobTemplate")

  def dispose( self, job_id ):
    '''
    Implementation of the L{JobScheduler} method.
    '''
    self.logger.debug(">> dispose %s", job_id)
    with self.__lock:
      self.kill(job_id)
      self.__jobServer.deleteJob(job_id)
    self.logger.debug("<< dispose")


  ########## WORKFLOW SUBMISSION ############################################
  
  def submitWorkflow(self, workflow_o, disposal_timeout):
    # type checking for the workflow ?
    workflow = copy.deepcopy(workflow_o)
    expiration_date = date.today() + timedelta(hours=disposal_timeout) 
    workflow_id = self.__jobServer.addWorkflow(self.__user_id, expiration_date)
    workflow.wf_id = workflow_id 
    
    
    for node in workflow.nodes:
      if isinstance(node, constants.FileSending):
        node.local_file_path = self.__jobServer.generateLocalFilePath(self.__user_id, node.remote_file_path)
        self.__jobServer.addTransfer(node.local_file_path, node.remote_file_path, expiration_date, self.__user_id, constants.READY_TO_TRANSFER, workflow_id)
       
      else:
        if isinstance(node, constants.FileRetrieving):
          node.local_file_path = self.__jobServer.generateLocalFilePath(self.__user_id, node.remote_file_path)
          self.__jobServer.addTransfer(node.local_file_path, node.remote_file_path, expiration_date, self.__user_id, constants.TRANSFER_NOT_READY, workflow_id)
    
    for node in workflow.nodes:
      if isinstance(node, constants.JobTemplate):
       
        new_command = []
        for command_el in node.command:
          if isinstance(command_el, constants.FileTransfer):
            new_command.append(command_el.local_file_path)
          else:
            new_command.append(command_el)
        node.command = new_command
        
        new_referenced_input_files = []
        for input_file in node.referenced_input_files:
          if isinstance(input_file, constants.FileTransfer):
            new_referenced_input_files.append(input_file.local_file_path)
          else:
            new_referenced_input_files.append(input_file)
        node.referenced_input_files= new_referenced_input_files
       
        new_referenced_output_files = []
        for output_file in node.referenced_output_files:
          if isinstance(output_file, constants.FileTransfer):
            new_referenced_output_files.append(output_file.local_file_path)
          else:
            new_referenced_output_files.append(output_file)
        node.referenced_output_files = new_referenced_output_files
        
        if isinstance(node.stdin, constants.FileTransfer):
          node.stdin = node.stdin.local_file_path 
              
        job_id = self.__registerJob(node, workflow_id)
        node.job_id = job_id
     
    self.__jobServer.setWorkflow(workflow_id, workflow, self.__user_id)
    self.__workflows[workflow_id] = workflow
    
    
    
    for node in workflow.nodes:
      torun=True
      for dep in workflow.dependencies:
        torun = torun and not dep[1] == node
      if torun:
        if isinstance(node, constants.JobTemplate):
          self.__drmaaJobSubmission(node.command, node.job_id)
    return workflow
     
  def __isWFNodeCompleted(self, node):
    competed = False
    if isinstance(node, constants.JobTemplate):
      if node.job_id:
        status = self.__jobServer.getJobStatus(node.job_id)
        completed = status == constants.DONE or status == constants.FAILED
    if isinstance(node, constants.FileSending):
      if node.local_file_path:
        status = self.__jobServer.getTransferStatus(node.local_file_path)
        completed = status == constants.TRANSFERED
    if isinstance(node, constants.FileRetrieving):
      if node.local_file_path:
        status = self.__jobServer.getTransferStatus(node.local_file_path)
        completed = status == constants.READY_TO_TRANSFER
    return completed
      
    
   
  def workflowProcessing(self, endedJobs=[], endedTransfers=[]):
    
    self.logger.debug(">> workflowProcessing")
    wf_to_process = set([])
    for job_id in endedJobs:
      job = self.__jobs[job_id]
      self.logger.debug("==> ended job: " + job.name)
      if job.referenced_output_files:
        for local_file_path in job.referenced_output_files:
          self.__jobServer.setTransferStatus(local_file_path, constants.READY_TO_TRANSFER)
      if not job.workflow_id == -1:
        workflow = self.__workflows[job.workflow_id]
        wf_to_process.add(workflow)
    for local_file_path in endedTransfers:
      self.logger.debug("==> ended Transfer: " + local_file_path)
      workflow_id = self.__jobServer.getTransferInformation(local_file_path)[3]
      self.logger.debug("workflow_id " + repr(workflow_id))
      if not workflow_id == -1:
        workflow = self.__workflows[workflow_id]
        wf_to_process.add(workflow)
      
    to_run = []
    for workflow in wf_to_process:
      for node in workflow.nodes:
        if isinstance(node, constants.JobTemplate):
          status = self.__jobServer.getJobStatus(node.job_id)
          to_inspect = status[0] == constants.NOT_SUBMITTED
          #print "node " + node.name + " status " + status[0] + " to inspect " + repr(to_inspect)
        if isinstance(node, constants.FileTransfer):
          status = self.__jobServer.getTransferStatus(node.local_file_path)
          to_inspect = status == constants.TRANSFER_NOT_READY
          #print "node " + node.name + " status " + status + " to inspect " + repr(to_inspect)
        if to_inspect:
          self.logger.debug("to inspect : " + node.name)
          node_to_run = False
          for dep in workflow.dependencies:
            if dep[1] == node: 
              #print "node " + node.name + " dep: " + dep[0].name + " " + dep[1].name 
              node_to_run = self.__isWFNodeCompleted(dep[0])
              if not node_to_run: break
          if node_to_run: 
            self.logger.debug("to run : " + node.name)
            to_run.append(node)
      
    for node in to_run:
      if isinstance(node, constants.JobTemplate):
        self.__drmaaJobSubmission(node.command, node.job_id)
      if isinstance(node,constants.FileTransfer):
        self.__jobServer.setTransferStatus(node.local_file_path, constants.READY_TO_TRANSFER)
    
    self.logger.debug("<<< workflowProcessing")
    
        

  ########### DRMS MONITORING ################################################


  def __status( self, job_id ):
    '''
    Returns the status of a submitted job. => add a converstion from DRMAA job 
    status strings to the status defined in soma.jobs.constants ???
    
    @type  job_id: C{JobIdentifier}
    @param job_id: The job identifier (returned by L{submit} or L{jobs})
    @rtype:  C{JobStatus}
    @return: the status of the job. The possible values are defined in soma.jobs.constants
    '''
    self.logger.debug(">> __status")
    with self.__lock:
      drmaaJobId = self.__jobServer.getDrmaaJobId(job_id)
      if drmaaJobId:
        status = self.__drmaa.jobStatus(drmaaJobId) 
        #add conversion from DRMAA status strings to the status defined in soma.jobs.constants if needed
    self.logger.debug("<< __status")
    return status
   
     


  def __endOfJob(self, job_id, status):
    '''
    The method is called when the job status is DONE or FAILED,
    to get the job exit inforation from DRMAA and update the JobServer.
    The job_id is also remove form the job list.
    '''
    self.logger.debug(">> __endOfJob")
    with self.__lock:
      drmaaJobId = self.__jobServer.getDrmaaJobId(job_id)
      if drmaaJobId:
        self.logger.debug("End of job %s, drmaaJobId = %s", job_id, drmaaJobId)
      
        exit_status, exit_value, term_sig, resource_usage = self.__drmaa.wait(drmaaJobId, 0)
  
        self.logger.debug("job %s, exit_status=%s exit_value =%d", job_id, exit_status, exit_value)
        
        str_rusage = ''
        for rusage in resource_usage:
          str_rusage = str_rusage + rusage + ' '
        
        self.__jobServer.setJobExitInfo(job_id, exit_status, exit_value, term_sig, str_rusage)
        self.__jobServer.setJobStatus(job_id, status)
    
    self.logger.debug("<< __endOfJob")

  def areJobsDone(self):
    return self.__jobsEnded
    
  ########## JOB CONTROL VIA DRMS ########################################
  

  def stop( self, job_id ):
    '''
    Implementation of the L{JobScheduler} method.
    '''
    self.logger.debug(">> stop")
    status_changed = False
    with self.__lock:
      drmaaJobId = self.__jobServer.getDrmaaJobId(job_id)
      if drmaaJobId:
        status = self.__status(job_id)
        self.logger.debug("   status : " + status)
        
        if status==constants.RUNNING:
          self.__drmaa.suspend(drmaaJobId)
          status_changed = True
        
        if status==constants.QUEUED_ACTIVE:
          self.__drmaa.hold(drmaaJobId)
          status_changed = True
        
        
    if status_changed:
      self.__waitForStatusUpdate(job_id)
    self.logger.debug("<< stop")
    
    
  def restart( self, job_id ):
    '''
    Implementation of the L{JobScheduler} method.
    '''
    self.logger.debug(">> restart")
    status_changed = False
    with self.__lock:
      drmaaJobId = self.__jobServer.getDrmaaJobId(job_id)
      if drmaaJobId:
        status = self.__status(job_id)
        
        if status==constants.USER_SUSPENDED or status==constants.USER_SYSTEM_SUSPENDED:
          self.__drmaa.resume(drmaaJobId)
          status_changed = True
          
        if status==constants.USER_ON_HOLD or status==constants.USER_SYSTEM_ON_HOLD :
          self.__drmaa.release(drmaaJobId)
          status_changed = True
        
    if status_changed:
      self.__waitForStatusUpdate(job_id)
    self.logger.debug("<< restart")
    
  


  def kill( self, job_id ):
    '''
    Implementation of the L{JobScheduler} method.
    '''
    self.logger.debug(">> kill")
        
    with self.__lock:
      (status, last_status_update) = self.__jobServer.getJobStatus(job_id)

      if status and not status == constants.DONE and not status == constants.FAILED:
        drmaaJobId = self.__jobServer.getDrmaaJobId(job_id)
        if drmaaJobId:
          self.logger.debug("terminate job %s drmaa id %s with status %s", job_id, drmaaJobId, status)
          self.__drmaa.terminate(drmaaJobId)
        
          self.__jobServer.setJobExitInfo(job_id, 
                                          constants.USER_KILLED,
                                          None,
                                          None,
                                          None)
          
          self.__jobServer.setJobStatus(job_id, constants.FAILED)
        if job_id in self.__jobs.keys():
          del self.__jobs[job_id]
        
    self.logger.debug("<< kill")


  def __waitForStatusUpdate(self, job_id):
    
    self.logger.debug(">> __waitForStatusUpdate")
    drmaaActionTime = datetime.now()
    time.sleep(refreshment_interval)
    (status, last_status_update) = self.__jobServer.getJobStatus(job_id)
    while status and not status == constants.DONE and not status == constants.FAILED and last_status_update < drmaaActionTime:
      time.sleep(refreshment_interval)
      (status, last_status_update) = self.__jobServer.getJobStatus(job_id) 
      if last_status_update and datetime.now() - last_status_update > timedelta(seconds = refreshment_interval*5):
        raise JobSchedulerError('Could not get back status of job %s. The process updating its status failed.' %(job_id), self.logger)
    self.logger.debug("<< __waitForStatusUpdate")


class JobScheduler( object ):
  
  def __init__( self, job_server, drmaa_job_scheduler = None,  parallel_job_submission_info = None):
    ''' 
    @type  job_server: L{JobServer}
    @type  drmaa_job_scheduler: L{DrmaaJobScheduler} or None
    @param drmaa_job_scheduler: object of type L{DrmaaJobScheduler} to delegate all the tasks related to the DRMS. If None a new instance is created.
    '''
    
    self.logger = logging.getLogger('ljp.js')
    
    Pyro.core.initClient()

    # Drmaa Job Scheduler
    if drmaa_job_scheduler:
      self.__drmaaJS = drmaa_job_scheduler
    else:
      print "parallel_job_submission_info" + repr(parallel_job_submission_info)
      self.__drmaaJS = DrmaaJobScheduler(job_server, parallel_job_submission_info)
    
    # Job Server
    self.__jobServer= job_server
    
    try:
      userLogin = pwd.getpwuid(os.getuid())[0]
    except Exception, e:
      raise JobSchedulerError("Couldn't identify user %s: %s \n" %(type(e), e), self.logger)
    
    self.__user_id = self.__jobServer.registerUser(userLogin)
   
    self.__fileToRead = None
    self.__fileToWrite = None
    self.__stdoutFileToRead = None
    self.__stderrFileToRead = None
    
    

  def __del__( self ):
    pass

  ########## FILE TRANSFER ###############################################
  
  '''
  For the following methods:
    Local means that it is located on a directory shared by the machine of the pool
    Remote means that it is located on a remote machine or on any directory 
    owned by the user. 
    A transfer will associate remote file path to unique local file path.
  
  Use L{registerTransfer} then L{writeLine} or scp or 
  shutil.copy to transfer input file from the remote to the local 
  environment.
  Use L{registerTransfer} and once the job has run use L{readline} or scp or
  shutil.copy to transfer the output file from the local to the remote environment.
  '''

  def registerTransfer(self, remote_file_path, disposal_timeout=168): 
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
      
    local_input_file_path = self.__jobServer.generateLocalFilePath(self.__user_id, remote_file_path)
    expirationDate = date.today() + timedelta(hours=disposal_timeout) 
    self.__jobServer.addTransfer(local_input_file_path, remote_file_path, expirationDate, self.__user_id)
    return local_input_file_path


  def writeLine(self, line, local_file_path):
    '''
    Writes a line to the local file. The path of the local input file
    must have been generated using the L{registerTransfer} method.
    
    @type  line: string
    @param line: line to write in the local input file
    @type  local_file_path: string
    @param local_file_path: local file path to fill up
    '''
    
    if not self.__jobServer.isUserTransfer(local_file_path, self.__user_id):
      raise JobSchedulerError("Couldn't write to file %s: the transfer was not registered using 'registerTransfer' or the user doesn't own the file. \n" % local_file_path, self.logger)
    
    if not self.__fileToWrite or not self.__fileToWrite.name == local_file_path:
      if self.__fileToWrite: self.__fileToWrite.close()
      self.__fileToWrite = open(local_file_path, 'wt')
      os.chmod(local_file_path, 0777)
      
    self.__fileToWrite.write(line)
    self.__fileToWrite.flush()
    #os.fsync(self.__fileToWrite.fileno())
   
   
  
  def readline(self, local_file_path):
    '''
    Reads a line from the local file. The path of the local input file
    must have been generated using the L{registerTransfer} method.
    
    @type: string
    @param: local file path to fill up
    @rtype: string
    return: read line
    '''
    
    if not self.__jobServer.isUserTransfer(local_file_path, self.__user_id):
      raise JobSchedulerError("Couldn't read from file %s: the transfer was not registered using 'registerTransfer' or the user doesn't own the file. \n" % local_file_path, self.logger)
    
    
    if not self.__fileToRead or not self.__fileToRead.name == local_file_path:
      self.__fileToRead = open(local_file_path, 'rt')
    
    return self.__fileToRead.readline()

  
  def endTransfers(self):
    if self.__fileToWrite:
      self.__fileToWrite.close()
    if self.__fileToRead:
      self.__fileToRead.close()
    
    
  def setTransferStatus(self, local_file_path, status):
    '''
    WIP
    '''
     
    if not self.__jobServer.isUserTransfer(local_file_path, self.__user_id) :
      print "Couldn't set transfer status %s. It doesn't exist or is not owned by the current user \n" % local_file_path
      return
    
    self.__jobServer.setTransferStatus(local_file_path, status)

  def cancelTransfer(self, local_file_path):
    '''
     Implementation of the L{Jobs} method.
    '''
    
    if not self.__jobServer.isUserTransfer(local_file_path, self.__user_id) :
      print "Couldn't cancel transfer %s. It doesn't exist or is not owned by the current user \n" % local_file_path
      return

    self.__jobServer.removeTransfer(local_file_path)
    
  def signalTransferEnded(self, local_file_path):
    '''
    WIP
    '''
    self.__drmaaJS.signalTransferEnded(local_file_path)
    

  ########## JOB SUBMISSION ##################################################

  
  def submit( self,
              jobTemplate):
    '''
    Submits a job to the system. 
    
    @type  jobTemplate: L{constants.JobTemplate}
    @param jobTemplate: job informations 
    '''

    if len(jobTemplate.command) == 0:
      raise JobSchedulerError("Submission error: the command must contain at least one element \n", self.logger)

    # check the required_local_input_files, required_local_output_file and stdin ?
    
    
    
    job_id = self.__drmaaJS.submit(jobTemplate)
    
    return job_id




  def dispose( self, job_id ):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    
    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      print "Couldn't dispose job %d. It doesn't exist or is not owned by the current user \n" % job_id
      return
    
    self.__drmaaJS.dispose(job_id)


  ########## WORKFLOW SUBMISSION ############################################
  
  def submitWorkflow(self, workflow, disposal_timeout):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    return self.__drmaaJS.submitWorkflow(workflow, disposal_timeout)
  
  def disposeWorkflow(self, workflow_id):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    if not self.__jobServer.isUserWorkflow(worflow_id, self.__user_id):
      print "Couldn't dispose workflow %d. It doesn't exist or is not owned by the current user \n" % job_id
      return
    
    self.__jobServer.deleteWorkflow(workflow_id)

  ########## SERVER STATE MONITORING ########################################


  def jobs(self):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    return self.__jobServer.getJobs(self.__user_id)
    
  def transfers(self):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    return self.__jobServer.getTransfers(self.__user_id)
  
  
  def workflows(self):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    return self.__jobServer.getWorkflows(self.__user_id)
  
  def submittedWorkflow(self, wf_id):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    if not self.__jobServer.isUserWorkflow(wf_id, self.__user_id):
      print "Couldn't get workflow %d. It doesn't exist or is owned by a different user \n" %wf_id
    return self.__jobServer.getWorkflow(wf_id)

    
  def transferInformation(self, local_file_path):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    #TBI raise an exception if local_file_path is not valid transfer??
    
    if not self.__jobServer.isUserTransfer(local_file_path, self.__user_id):
      print "Couldn't get transfer information of %s. It doesn't exist or is owned by a different user \n" % local_file_path
      return
      
    return self.__jobServer.getTransferInformation(local_file_path)
   


  def status( self, job_id ):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      print "Could get the job status of job %d. It doesn't exist or is owned by a different user \n" %job_id
      return 
    
    return self.__jobServer.getJobStatus(job_id)[0]
        
        
  def transferStatus(self, local_file_path):
    '''
    WIP
    '''
    if not self.__jobServer.isUserTransfer(local_file_path, self.__user_id):
      print "Could get the job status the transfer associated with %s. It doesn't exist or is owned by a different user \n" %local_file_path
      return 
    
    return self.__jobServer.getTransferStatus(local_file_path)
    
    

  def exitInformation(self, job_id ):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
  
    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      print "Could get the exit information of job %d. It doesn't exist or is owned by a different user \n" %job_id
      return
  
    dbJob = self.__jobServer.getJob(job_id)
    exit_status = dbJob.exit_status
    exit_value = dbJob.exit_value
    terminating_signal =dbJob.terminating_signal
    resource_usage = dbJob.resource_usage
    
    return (exit_status, exit_value, terminating_signal, resource_usage)
    
 
  def jobInformation(self, job_id):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    
    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      print "Could get information about job %d. It doesn't exist or is owned by a different user \n" %job_id
      return
    
    dbJob = self.__jobServer.getJob(job_id)
    name_description = dbJob.name_description 
    command = dbJob.command
    submission_date = dbJob.submission_date
    
    return (name_description, command, submission_date)
    


  def stdoutReadLine(self, job_id):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      print "Could get not read std output for the job %d. It doesn't exist or is owned by a different user \n" %job_id
      return   

    stdout_path, stderr_path = self.__jobServer.getStdOutErrFilePath(job_id)
    
    if not self.__stdoutFileToRead or not self.__stdoutFileToRead.name == stdout_path:
      self.__stdoutFileToRead = open(stdout_path, 'rt')
      
    return self.__stdoutFileToRead.readline()


  def stderrReadLine(self, job_id):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      print "Could get not read std error for the job %d. It doesn't exist or is owned by a different user \n" %job_id
      return   

    stdout_path, stderr_path = self.__jobServer.getStdOutErrFilePath(job_id)
    
    if not stderr_path:
      self.__stderrFileToRead = None
      return 

    if not self.__stderrFileToRead or not self.__stderrFileToRead.name == stderr_path:
      self.__stderrFileToRead = open(stderr_path, 'rt')
      
    return self.__stderrFileToRead.readline()

    
  ########## JOB CONTROL VIA DRMS ########################################
  
  
  def wait( self, job_ids, timeout = -1):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    for jid in job_ids:
      if not self.__jobServer.isUserJob(jid, self.__user_id):
        raise JobSchedulerError( "Could not wait for job %d. It doesn't exist or is owned by a different user \n" %jid, self.logger)
      
    #self.__drmaaJS.wait(job_ids, timeout)
    self.logger.debug("        waiting...")
    
    waitForever = timeout < 0
    startTime = datetime.now()
    for jid in job_ids:
      (status, last_status_update) = self.__jobServer.getJobStatus(jid)
      if status:
        self.logger.debug("        job %s status: %s", jid, status)
        delta = datetime.now()-startTime
        delta_status_update = datetime.now() - last_status_update
        while status and not status == constants.DONE and not status == constants.FAILED and (waitForever or delta < timedelta(seconds=timeout)):
          time.sleep(refreshment_interval)
          (status, last_status_update) = self.__jobServer.getJobStatus(jid) 
          self.logger.debug("        job %s status: %s", jid, status)
          delta = datetime.now() - startTime
          if last_status_update and datetime.now() - last_status_update > timedelta(seconds = refreshment_interval*10):
            raise JobSchedulerError('Could not wait for job %s. The process updating its status failed.' %(jid), self.logger)
    

  def stop( self, job_id ):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      raise JobSchedulerError( "Could not stop job %d. It doesn't exist or is owned by a different user \n" %job_id, self.logger)
    
    self.__drmaaJS.stop(job_id)
   
  
  
  def restart( self, job_id ):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''
    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      raise JobSchedulerError( "Could not restart job %d. It doesn't exist or is owned by a different user \n" %job_id, self.logger)
    
    self.__drmaaJS.restart(job_id)


  def kill( self, job_id ):
    '''
    Implementation of soma.jobs.jobClient.Jobs API
    '''

    if not self.__jobServer.isUserJob(job_id, self.__user_id):
      raise JobSchedulerError( "Could not kill job %d. It doesn't exist or is owned by a different user \n" %job_id, self.logger)
    
    self.__drmaaJS.kill(job_id)


