import argparse
import json
import logging
import shlex
import sys
import uuid

import requests

from cook import colors, http, metrics, version
from cook.util import deep_merge, is_valid_uuid, read_lines, print_info, current_user, guard_no_cluster


def parse_raw_job_spec(job, r):
    """
    Parse a JSON string containing raw job data and merge with job template.
    Job data can either be a dict of job attributes (indicating a single job),
    or a list of dicts (indicating multiple jobs). In either case, the job attributes
    are merged with (and override) the `job` template attributes.
    Throws a ValueError if there is a problem parsing the data.
    """
    try:
        content = json.loads(r)

        if type(content) is dict:
            return [deep_merge(job, content)]
        elif type(content) is list:
            return [deep_merge(job, c) for c in content]
        else:
            raise ValueError('invalid format for raw job')
    except Exception:
        raise ValueError('malformed JSON for raw job')


def submit_succeeded_message(cluster_name, uuids):
    """Generates a successful submission message with the given cluster and uuid(s)"""
    if len(uuids) == 1:
        return 'Job submission %s on %s. Your job UUID is:\n%s' % \
               (colors.success('succeeded'), cluster_name, uuids[0])
    else:
        return 'Job submission %s on %s. Your job UUIDs are:\n%s' % \
               (colors.success('succeeded'), cluster_name, '\n'.join(uuids))


def submit_failed_message(cluster_name, reason):
    """Generates a failed submission message with the given cluster name and reason"""
    return 'Job submission %s on %s:\n%s' % (colors.failed('failed'), cluster_name, colors.reason(reason))


def print_submit_result(cluster, response):
    """
    Parses a submission response from cluster and returns a corresponding message. Note that
    Cook Scheduler returns text when the submission was successful, and JSON when the submission
    failed. Also, in the case of failure, there are different possible shapes for the failure payload.
    """
    cluster_name = cluster['name']
    if response.status_code == 201:
        text = response.text.strip('"')
        if ' submitted groups' in text:
            group_index = text.index(' submitted groups')
            text = text[:group_index]
        uuids = [p for p in text.split() if is_valid_uuid(p)]
        print_info(submit_succeeded_message(cluster_name, uuids), '\n'.join(uuids))
    else:
        try:
            data = response.json()
            if 'errors' in data:
                reason = json.dumps(data['errors'])
            elif 'error' in data:
                reason = data['error']
            else:
                reason = json.dumps(data)
        except json.decoder.JSONDecodeError:
            reason = '%s\n' % response.text
        print_info(submit_failed_message(cluster_name, reason))


def submit_federated(clusters, jobs, group):
    """
    Attempts to submit the provided jobs to each cluster in clusters, until a cluster
    returns a "created" status code. If no cluster returns "created" status, throws.
    """
    for cluster in clusters:
        cluster_name = cluster['name']
        cluster_url = cluster['url']
        try:
            print_info('Attempting to submit on %s cluster...' % colors.bold(cluster_name))

            json_body = {'jobs': jobs}
            if group:
                json_body['groups'] = [group]

            resp = http.post(cluster, 'rawscheduler', json_body)
            print_submit_result(cluster, resp)
            if resp.status_code == 201:
                metrics.inc('command.submit.jobs', len(jobs))
                return 0
        except requests.exceptions.ReadTimeout as rt:
            logging.exception(rt)
            print_info(colors.failed(
                f'Encountered read timeout with {cluster_name} ({cluster_url}). Your submission may have completed.'))
            return 1
        except IOError as ioe:
            logging.exception(ioe)
            reason = f'Cannot connect to {cluster_name} ({cluster_url})'
            message = submit_failed_message(cluster_name, reason)
            print_info(f'{message}\n')
    raise Exception(colors.failed('Job submission failed on all of your configured clusters.'))


def read_commands_from_stdin():
    """Prompts for and then reads commands, one per line, from stdin"""
    print_info('Enter the commands, one per line (press Ctrl+D on a blank line to submit)')
    commands = read_lines()
    if len(commands) < 1:
        raise Exception('You must specify at least one command.')
    return commands


def read_jobs_from_stdin():
    """Prompts for and then reads job(s) JSON from stdin"""
    print('Enter the raw job(s) JSON (press Ctrl+D on a blank line to submit)', file=sys.stderr)
    jobs_json = sys.stdin.read()
    return jobs_json


def acquire_commands(command_args):
    """
    Given the command_args list passed from the command line, returns a
    list of commands to use for job submission. If command_args is None,
    the user will be prompted to supply commands from stdin. Otherwise,
    the returned commands list will only contain 1 element, corresponding
    to the command specified at the command line.
    """
    if command_args:
        if len(command_args) == 1:
            commands = command_args
        else:
            if command_args[0] == '--':
                command_args = command_args[1:]
            commands = [' '.join([shlex.quote(s) for s in command_args])]
    else:
        commands = read_commands_from_stdin()

    logging.info('commands: %s' % commands)
    return commands


def submit(clusters, args, _):
    """
    Submits a job (or multiple jobs) to cook scheduler. Assembles a list of jobs,
    potentially getting data from configuration, the command line, and stdin.
    """
    guard_no_cluster(clusters)
    logging.debug('submit args: %s' % args)
    job_template = args
    raw = job_template.pop('raw', None)
    command_from_command_line = job_template.pop('command', None)
    command_prefix = job_template.pop('command-prefix')
    application_name = job_template.pop('application-name', 'cook-scheduler-cli')
    application_version = job_template.pop('application-version', version.VERSION)
    job_template['application'] = {'name': application_name, 'version': application_version}

    group = None
    if 'group-name' in job_template:
        # If the user did not also specify a group uuid, generate
        # one for them, and place the job(s) into the group
        if 'group' not in job_template:
            job_template['group'] = str(uuid.uuid4())

        # The group name is specified on the group object
        group = {'name': job_template.pop('group-name'), 'uuid': job_template['group']}

    if raw:
        if command_from_command_line:
            raise Exception('You cannot specify a command at the command line when using --raw/-r.')

        jobs_json = read_jobs_from_stdin()
        jobs = parse_raw_job_spec(job_template, jobs_json)
    else:
        commands = acquire_commands(command_from_command_line)

        if job_template.get('uuid') and len(commands) > 1:
            raise Exception('You cannot specify multiple subcommands with a single UUID.')

        if job_template.get('env'):
            job_template['env'] = dict([e.split('=', maxsplit=1) for e in job_template['env']])

        jobs = [deep_merge(job_template, {'command': c}) for c in commands]

    for job in jobs:
        if not job.get('uuid'):
            job['uuid'] = str(uuid.uuid4())

        if not job.get('name'):
            job['name'] = '%s_job' % current_user()

        if command_prefix:
            job['command'] = f'{command_prefix}{job["command"]}'

    logging.debug('jobs: %s' % jobs)
    return submit_federated(clusters, jobs, group)


def valid_uuid(s):
    """Allows argparse to flag user-provided job uuids as valid or not"""
    if is_valid_uuid(s):
        return str(uuid.UUID(s))
    else:
        raise argparse.ArgumentTypeError('%s is not a valid UUID' % s)


def register(add_parser, add_defaults):
    """Adds this sub-command's parser and returns the action function"""
    submit_parser = add_parser('submit', help='create job for command')
    submit_parser.add_argument('--uuid', '-u', help='uuid of job', type=valid_uuid)
    submit_parser.add_argument('--name', '-n', help='name of job')
    submit_parser.add_argument('--priority', '-p', help='priority of job, between 0 and 100 (inclusive), with 100 '
                                                        'being highest priority (default = 50)',
                               type=int, choices=range(0, 101), metavar='')
    submit_parser.add_argument('--max-retries', help='maximum retries for job',
                               dest='max-retries', type=int, metavar='COUNT')
    submit_parser.add_argument('--max-runtime', help='maximum runtime for job',
                               dest='max-runtime', type=int, metavar='MILLIS')
    submit_parser.add_argument('--cpus', '-c', help='cpus to reserve for job', type=float)
    submit_parser.add_argument('--mem', '-m', help='memory to reserve for job', type=int)
    submit_parser.add_argument('--group', '-g', help='group uuid for job', type=str, metavar='UUID')
    submit_parser.add_argument('--group-name', '-G', help='group name for job',
                               type=str, metavar='NAME', dest='group-name')
    submit_parser.add_argument('--env', '-e', help='environment variable for job (can be repeated)',
                               metavar='KEY=VALUE', action='append')
    submit_parser.add_argument('--ports', help='number of ports to reserve for job', type=int)
    submit_parser.add_argument('--application-name', '-a', help='name of application submitting the job',
                               dest='application-name')
    submit_parser.add_argument('--application-version', '-v', help='version of application submitting the job',
                               dest='application-version')
    submit_parser.add_argument('--executor', '-E', help='executor to use to run the job on the Mesos agent',
                               choices=('cook', 'mesos'))
    submit_parser.add_argument('--raw', '-r', help='raw job spec in json format', dest='raw', action='store_true')
    submit_parser.add_argument('--command-prefix', help='prefix to use for all commands', dest='command-prefix')
    submit_parser.add_argument('command', nargs=argparse.REMAINDER)

    add_defaults('submit', {'cpus': 1, 'max-retries': 1, 'mem': 128, 'command-prefix': ''})

    return submit
