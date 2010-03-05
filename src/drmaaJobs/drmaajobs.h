#ifndef DRMAA_JOB_H
#define DRMAA_JOB_H


#include <cstdio>
#include <stddef.h>
#include <unistd.h>
#include <soma/pipeline/drmaa/drmaa.h>
#include <map>
#include <list>
#include <string>


#define CONDOR_CONTACT "Condor"
//#define SGE_CONTACT


struct ltint
{
  bool operator()(int i1, int i2) const
  {
    return i1 < i2;   }
};


class DrmaaJobs {

    std::map<int, drmaa_job_template_t *, ltint> mJobTemplatesMap;

public :

    // Initialize the DRMAA session
    // in: contact string used to specify which DRM system to use
    // !! can't initialize several DRMAA sessions
    void initSession(const char * contactString =  "NULL");

    // Exit the current DRMAA session:
    // "do whatever work is required to disengage from the DRM systems ans allow the DRMAA implementation to perform any necessary cleanup"
    void exitSession();

//public :

   typedef enum {
        UNDETERMINED_JOB,           // The job doesn't exist
        EXIT_ABORTED,               // The job never ran
        FINISHED_REGULARLY,         // The job finished regularly
        FINISHED_TERM_SIG,          // The job finished due to a signal
        FINISHED_UNCLEAR_CONDITIONS // The job finished with unclear condition
    } ExitJobStatus;

    typedef enum {
        UNDETERMINED,           // Job status cannot be determined
        QUEUED_ACTIVE,          // Job is queued and active
        SYSTEM_ON_HOLD,         // Job is queued and in system hold
        USER_ON_HOLD,           // Job is queued and in user hold
        USER_SYSTEM_ON_HOLD,    // Job is queued and in user and system hold
        RUNNING,                // Job is running
        SYSTEM_SUSPENDED,       // Job is system suspended
        USER_SUSPENDED,         // Job is user suspended
        USER_SYSTEM_SUSPENDED,  // Job is user and system suspended
        DONE,                   // Job finished normally
        FAILED                  // Job finished but failed
    } JobStatus;

    typedef enum {
        SUSPEND,    //stop the job
        RESUME,     //(re)start the job
        HOLD,       //put the job on hold
        RELEASE,    // release the hold on the job
        TERMINATE   // kill the job
    } Action;

    static const int undefinedId = -1;

    // Init the Drmaa session:
    DrmaaJobs(const char * contactString =  "NULL");

    // Delete every job template and exit the DRMAA session
    ~DrmaaJobs();

    void displaySupportedAttributeNames();

    ///////////////////////////////////
    // JOB TEMPLATES

    // Allocate a new job template which attribute will be set using setAttribute methods
    // out: jobTemplateId
    int allocateJobTemplate();

    // Delete the job template with id jobTemplateId.
    // Has to be called before the session ended
    void deleteJobTemplate(int jobTemplateId);

    // Set the job template command
    // in: remote_command: path to the execution file
    // in: nbArgument: number of arguments
    // in: arguments: list of arguments
    void setCommand(int jobTemplateId, const char * remote_command, int nbArguments, const char ** arguments);

    // Set any job template attribute
    // in: job template id
    // in: attribute name among:
    //#define DRMAA_REMOTE_COMMAND "drmaa_remote_command"
    //#define DRMAA_JS_STATE "drmaa_js_state"
    //#define DRMAA_WD "drmaa_wd"
    //#define DRMAA_JOB_CATEGORY "drmaa_job_category"
    //#define DRMAA_NATIVE_SPECIFICATION "drmaa_native_specification"
    //#define DRMAA_BLOCK_EMAIL "drmaa_block_email"
    //#define DRMAA_START_TIME "drmaa_start_time"
    //#define DRMAA_JOB_NAME "drmaa_job_name"
    //#define DRMAA_INPUT_PATH "drmaa_input_path"
    //#define DRMAA_OUTPUT_PATH "drmaa_output_path"
    //#define DRMAA_ERROR_PATH "drmaa_error_path"
    //#define DRMAA_JOIN_FILES "drmaa_join_files"
    //#define DRMAA_TRANSFER_FILES "drmaa_transfer_files"
    //#define DRMAA_DEADLINE_TIME "drmaa_deadline_time"
    //#define DRMAA_WCT_HLIMIT "drmaa_wct_hlimit"
    //#define DRMAA_WCT_SLIMIT "drmaa_wct_slimit"
    //#define DRMAA_DURATION_HLIMIT "drmaa_duration_hlimit"
    //#define DRMAA_DURATION_SLIMIT "drmaa_duration_slimit"
    // in: value : attribute value
    void setAttribute(int jobTemplateId, const char *name, const char *value);

    // Set any job template vector attribute
    // in: job template id
    // in: attribute name among:
    //#define DRMAA_V_ARGV "drmaa_v_argv"
    //#define DRMAA_V_ENV "drmaa_v_env"
    //#define DRMAA_V_EMAIL "drmaa_v_email"
    // in: value: list of attribute values
    void setVectorAttribute(int jobTemplateId, const char* name, int nbArguments, const char **arguments);

    void displayJobTemplateAttributeValues(int jobTemplateId);

    ///////////////////////////////////
    // RUNNING JOBS

    // Run a job which information are given by a job template
    // in: job template id
    // out: runningJobId
    const std::string runJob(int jobTemplateId);

    // Run several identical jobs which information are given by a job template (usefull for test purpose)
    // in: job template id
    // in: nbJobs: number of jobs to run
    // out: runningJobIds_out: list of running job ids
    void runBulkJobs(int jobTemplateId, int nbJobs, std::list<std::string> & runningJobIds_out) ;

    // Wait for a job to finish execution or fail and display its status
    // in: running job id
    void wait(const std::string & runningJobId);

    // Wait for any job to finish execution or fail and display its status
    // out: id of ended job
    //int waitForAnyJob();

    // Wait until all jobs sepcified by the runningJobIds have finished execution and display their status.
    void synchronize(const std::list<std::string> & runningJobIds);


    // Control a submitted job
    void control(const std::string & runningJobId, Action action);

    ////////////////////////////////////
    // RUNNING JOBS IMFORMATION

    // Gets the status given a given job id
    JobStatus jobStatus(const std::string & runningJobId);
    void jobStatus(const std::list<std::string> & runningJobIds, std::list<JobStatus> & statusList_out);



protected :

    int m_currentId;
    int getNextId();

    bool isJobTemplateIdValid(int jobTemplateId);

    ExitJobStatus getJobStatus(int drmaa_exitStatus);

    std::string m_information;

};







#endif //DRMAA_JOB_H