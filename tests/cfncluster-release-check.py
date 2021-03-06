#!/usr/bin/python
#
# Copyright 2018      Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy
# of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, express or implied. See the License for the specific
# language governing permissions and limitations under the License.
#
#
# Build a cluster for each combination of region, base_os, and
# scheduler, and run a test script on each cluster.  To avoid bouncing
# against limits in each region, the number of simultaneously built
# clusters in each region is a configuration parameter.
#
# NOTE:
# - This script requires python2
# - To simplify this script, at least one subnet in every region
#   to be tested must have a resource tag named "CfnClusterTestSubnet"
#   (value does not matter). That subnet will be used as the launch
#   target for the cluster.

import datetime
import errno
import os
import signal
import sys
import subprocess as sub
import threading
import time
import re
import argparse
import Queue
import boto3
import process_helper as prochelp
from builtins import exit


class ReleaseCheckException(Exception):
    pass

#
# configuration
#
username_map = { 'alinux' : 'ec2-user',
                 'centos6' : 'centos',
                 'centos7' : 'centos',
                 'ubuntu1404' : 'ubuntu',
                 'ubuntu1604' : 'ubuntu' }

#
# global variables (sigh)
#
setup = {}

results_lock = threading.Lock()
failure = 0
success = 0

# PID of the actual test process
_child = 0
# True if parent process has been asked to terminate
_termination_caught = False

_TIMESTAMP_FORMAT = '%Y%m%d%H%M%S'
_timestamp = datetime.datetime.now().strftime(_TIMESTAMP_FORMAT)


def _dirname():
    return os.path.dirname(os.path.realpath(sys.argv[0]))

def _time():
    return datetime.datetime.now()

# Write both on stdout and the specified file object
def _double_writeln(fileo, message):
    print(message)
    fileo.write(message + '\n')

# Helper method to get the name of the autoscaling group
def check_asg_capacity(stack_name, region, out_f):
    asg_conn = boto3.client('autoscaling', region_name=region)
    iter = 0
    capacity = -1
    while iter < 24 and capacity != 0:
        try:
            r = asg_conn.describe_tags(Filters=[{'Name': 'value', 'Values': [stack_name]}])
            asg_name = r.get('Tags')[0].get('ResourceId')
            response = asg_conn.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
            capacity = response["AutoScalingGroups"][0]["DesiredCapacity"]
            iter += 1
            time.sleep(10)
        except Exception as e:
            _double_writeln(out_f, "check_asg_capacity failed with %s exception: %s" % (type(e), e))
            raise

    _double_writeln(out_f, "ASG Capacity was %s after %s second(s)" % (capacity, 10 * iter))
    if capacity != 0:
        raise ReleaseCheckException("Autoscaling group's desired capacity was not zero. Capacity was %s" % capacity)

#
# run a single test, possibly in parallel
#
def run_test(region, distro, scheduler, instance_type, key_name, extra_args):
    scaledown_idletime = 2
    testname = '%s-%s-%s-%s-%s' % (region, distro, scheduler, instance_type.replace('.', ''), _timestamp)
    test_filename = "%s-config.cfg" % testname
    key_path = extra_args['key_path']
    custom_cookbook = extra_args['custom_cookbook_url']
    custom_node = extra_args['custom_node_url']
    custom_template = extra_args['custom_template_url']

    print("--> %s: Starting" % (testname))

    file = open(test_filename, "w")
    file.write("[aws]\n")
    file.write("aws_region_name = %s\n" % region)
    file.write("[cluster default]\n")
    file.write("vpc_settings = public\n")
    file.write("key_name = %s\n" % key_name)
    file.write("base_os = %s\n" % distro)
    file.write("master_instance_type = %s\n" % instance_type)
    file.write("compute_instance_type = %s\n" % instance_type)
    file.write("initial_queue_size = 1\n")
    file.write("maintain_initial_size = false\n")
    file.write("scheduler = %s\n" % (scheduler))
    file.write("scaling_settings = custom\n")
    if custom_template:
        file.write("template_url = %s\n" % custom_template)
    if custom_cookbook:
        file.write("custom_chef_cookbook = %s\n" % custom_cookbook)
    if custom_node:
        file.write('extra_json = { "cfncluster" : { "custom_node_package" : "%s" } }\n' % custom_node)
    file.write("[vpc public]\n")
    file.write("master_subnet_id = %s\n" % (setup[region]['subnet']))
    file.write("vpc_id = %s\n" % (setup[region]['vpc']))
    file.write("[global]\n")
    file.write("cluster_template = default\n")
    file.write("[scaling custom]\n")
    file.write("scaledown_idletime = %s\n" % scaledown_idletime)
    file.close()

    out_f = open('%s-out.txt' % testname, 'w', 0)

    master_ip = ''
    username = username_map[distro]
    _create_interrupted = False;
    _create_done = False;
    try:
        # build the cluster
        prochelp.exec_command(['cfncluster', 'create', '--config', test_filename, testname],
                              stdout=out_f, stderr=sub.STDOUT, universal_newlines=True)
        _create_done = True
        # get the master ip, which means grepping through cfncluster status gorp
        dump = prochelp.exec_command(['cfncluster', 'status', '--config', test_filename,
                                        testname], stderr=sub.STDOUT, universal_newlines=True)
        dump_array = dump.splitlines()
        for line in dump_array:
            m = re.search('MasterPublicIP: (.+)$', line)
            if m:
                master_ip = m.group(1)
                break
        if master_ip == '':
            _double_writeln(out_f, '!! %s: Master IP not found; exiting !!' % (testname))
            raise ReleaseCheckException('--> %s: Master IP not found!' % testname)
        _double_writeln(out_f, "--> %s Master IP: %s" % (testname, master_ip))

        # run test on the cluster...
        ssh_params = ['-o', 'StrictHostKeyChecking=no']
        ssh_params += ['-o', 'BatchMode=yes']
        # ssh_params += ['-o', 'ConnectionAttempts=30']
        ssh_params += ['-o', 'ConnectTimeout=60']
        ssh_params += ['-o', 'ServerAliveCountMax=5']
        ssh_params += ['-o', 'ServerAliveInterval=30']
        if key_path:
            ssh_params.extend(['-i', key_path])

        prochelp.exec_command(['scp'] + ssh_params + [os.path.join(_dirname(), 'cluster-check.sh'), '%s@%s:.' % (username, master_ip)],
                              stdout=out_f, stderr=sub.STDOUT, universal_newlines=True)
        prochelp.exec_command(['ssh', '-n'] + ssh_params + ['%s@%s' % (username, master_ip), '/bin/bash --login cluster-check.sh submit %s' % scheduler],
                              stdout=out_f, stderr=sub.STDOUT, universal_newlines=True)

        # Sleep for scaledown_idletime to give time for the instances to scale down
        time.sleep(60*scaledown_idletime)

        check_asg_capacity('cfncluster-' + testname, region, out_f)

        prochelp.exec_command(['ssh', '-n'] + ssh_params + ['%s@%s' % (username, master_ip), '/bin/bash --login cluster-check.sh scaledown_check %s' % scheduler],
                              stdout=out_f, stderr=sub.STDOUT, universal_newlines=True)

        _double_writeln(out_f, 'SUCCESS:  %s!!' % testname)
        open('%s.success' % testname, 'w').close()
    except prochelp.ProcessHelperError as exc:
        if not _create_done and isinstance(exc, prochelp.KilledProcessError):
            _create_interrupted = True
            _double_writeln(out_f, "--> %s: Interrupting cfncluster create!" % testname)
        _double_writeln(out_f, '!! ABORTED: %s!!' % (testname))
        open('%s.aborted' % testname, 'w').close()
        raise exc
    except Exception as exc:
        if not _create_done:
            _create_interrupted = True
        _double_writeln(out_f, "Unexpected exception %s: %s" % (str(type(exc)), str(exc)))
        _double_writeln(out_f, "!! FAILURE: %s!!" % (testname))
        open('%s.failed' % testname, 'w').close()
        raise exc
    finally:
        if _create_interrupted or _create_done:
            # if the create process was interrupted it may take few seconds for the stack id to be actually registered
            _max_del_iters = _del_iters = 10
        else:
            # No delete is necessary if cluster creation wasn't started (process_helper.AbortedProcessError)
            _del_iters = 0
        if _del_iters > 0:
            _del_done = False
            _double_writeln(out_f, "--> %s: Deleting - max iterations: %s" % (testname, _del_iters))
            while not _del_done and _del_iters > 0:
                try:
                    time.sleep(2)
                    # clean up the cluster
                    _del_output = sub.check_output(['cfncluster', 'delete', '--config', test_filename, '-nw', testname], stderr=sub.STDOUT, universal_newlines=True)
                    _del_done = "DELETE_IN_PROGRESS" in _del_output or "DELETE_COMPLETE" in _del_output
                    out_f.write(_del_output + '\n')
                except sub.CalledProcessError as exc:
                    out_f.write("CalledProcessError exception launching 'cfncluster delete': %s - Output:\n%s\n" % (str(exc), exc.output))
                except Exception as exc:
                    out_f.write("Unexpected exception launching 'cfncluster delete' %s: %s\n" % (str(type(exc)), str(exc)))
                finally:
                    _double_writeln(out_f, "--> %s: Deleting - iteration: %s - successfully submitted: %s" % (testname, (_max_del_iters - _del_iters + 1), _del_done))
                    _del_iters -= 1

            try:
                prochelp.exec_command(['cfncluster', 'status', '--config', test_filename, testname], stdout=out_f, stderr=sub.STDOUT, universal_newlines=True)
            except (prochelp.ProcessHelperError, sub.CalledProcessError):
                # Usually it terminates with exit status 1 since at the end of the delete operation the stack is not found.
                pass
            except Exception as exc:
                out_f.write("Unexpected exception launching 'cfncluster status' %s: %s\n" % (str(type(exc)), str(exc)))
        out_f.close()
    print("--> %s: Finished" % (testname))

#
# worker thread, there will be config['parallelism'] of these running
# per region, dispatching work from the work queue
#
def test_runner(region, q, key_name, extra_args):
    global success
    global failure
    global results_lock

    while True:
        item = q.get()

        retval = 1
        # just in case we miss an exception in run_test, don't abort everything...
        try:
            if not prochelp.termination_caught():
                run_test(region=region, distro=item['distro'], scheduler=item['scheduler'],
                         instance_type=item['instance_type'], key_name=key_name, extra_args=extra_args)
                retval = 0
        except (ReleaseCheckException, prochelp.ProcessHelperError, sub.CalledProcessError):
            pass
        except Exception as exc:
            print("[test_runner] Unexpected exception %s: %s\n" % (str(type(exc)), str(exc)))

        results_lock.acquire(True)
        if retval == 0:
            success += 1
        else:
            failure += 1
        results_lock.release()
        q.task_done()

def _term_handler_parent(_signo, _stack_frame):
    global _termination_caught

    if not _termination_caught:
        _termination_caught = True
        print("Termination handler setting _termination_caught = True")
        print("Sending TERM signal to child process %s" % _child)
        os.kill(_child, signal.SIGTERM)

def _bind_signals_parent():
    signal.signal(signal.SIGINT, _term_handler_parent)
    signal.signal(signal.SIGTERM, _term_handler_parent)
    signal.signal(signal.SIGHUP, _term_handler_parent)

def _main_parent():
    _bind_signals_parent()
    print("Child pid: %s" % _child)
    status = 0
    child_terminated = False
    max_num_exc = 10
    while not child_terminated and max_num_exc > 0:
        try:
            (pid, status) = os.wait()
            child_terminated = True
        except OSError as ose:
            # errno.ECHILD - No child processes
            child_terminated = ose.errno == errno.ECHILD
            if not child_terminated:
                print("OSError exception while waiting for child process %s, errno: %s - %s" % (_child, errno.errorcode[ose.errno], str(ose)))
        except BaseException as exc:
            print("Unexpected exception while waiting for child process %s, %s: %s" % (_child, str(type(exc)), str(exc)))
            max_num_exc -= 1
    print("Child pid: %s - Exit status: %s" % (pid, status))
    # status is a 16-bit number, whose low byte is the signal number that killed the process, and whose high byte is the exit status
    exit(status>>8)

def _bind_signals_child():
    # This is important - otherwise SIGINT propagates downstream to threads and child processes
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, prochelp.term_handler)
    signal.signal(signal.SIGHUP, prochelp.term_handler)

def _proc_alive(pid):
    if pid <= 1:
        return False
    alive = False
    try:
        # No real signal is sent but error checking is performed
        os.kill(pid, 0)
        alive = True
    except OSError as ose:
        # ose.errno == errno.EINVAL - Invalid signal number (this shouldn't happen)
        # ose.errno == errno.ESRCH - No such process
        # ose.errno == errno.EPERM - No permissions to check 'pid' process.
        pass
    except Exception as exc:
        print("Unexpected exception checking process %s, %s: %s" % (pid, str(type(exc)), str(exc)))

    return alive

def _killme_gently():
    os.kill(os.getpid(), signal.SIGTERM)

def _main_child():
    _bind_signals_child()
    parent = os.getppid()
    print("Parent pid: %s" % parent)
    config = { 'parallelism' : 3,
               'regions' : 'us-east-1,us-east-2,us-west-1,us-west-2,' +
                           'ca-central-1,eu-west-1,eu-west-2,eu-central-1,' +
                           'ap-southeast-1,ap-southeast-2,ap-northeast-1,' +
                           'ap-south-1,sa-east-1,eu-west-3',
               'distros' : 'alinux,centos6,centos7,ubuntu1404,ubuntu1604',
               'schedulers' : 'sge,slurm,torque',
               'instance_types': 'c4.xlarge',
               'key_path' : '',
               'custom_node_url' : None,
               'custom_cookbook_url' : None,
               'custom_template_url' : None }

    parser = argparse.ArgumentParser(description = 'Test runner for CfnCluster')
    parser.add_argument('--parallelism', help = 'Number of tests per region to run in parallel',
                        type = int, default = 3)
    parser.add_argument('--regions', help = 'Comma separated list of regions to test',
                        type = str)
    parser.add_argument('--distros', help = 'Comma separated list of distributions to test',
                        type = str)
    parser.add_argument('--schedulers', help = 'Comma separated list of schedulers to test',
                        type = str)
    parser.add_argument('--instance-types', type=str,
                        help='Comma separated list of instance types to use for both Master and Compute nodes')
    parser.add_argument('--key-name', help = 'Key Pair to use for EC2 instances',
                        type = str, required = True)
    parser.add_argument('--key-path', help = 'Key path to use for SSH connections',
                        type = str)
    parser.add_argument('--custom-node-url', help = 'S3 URL to a custom cfncluster-node package',
                        type = str)
    parser.add_argument('--custom-cookbook-url', help = 'S3 URL to a custom cfncluster-cookbook package',
                        type = str)
    parser.add_argument('--custom-template-url', help = 'S3 URL to a custom cfncluster CloudFormation template',
                        type = str)

    for key, value in vars(parser.parse_args()).iteritems():
        if not value == None:
            config[key] = value

    region_list = config['regions'].split(',')
    distro_list = config['distros'].split(',')
    scheduler_list = config['schedulers'].split(',')
    instance_type_list = config['instance_types'].split(',')

    print("==> Regions: %s" % (', '.join(region_list)))
    print("==> Instance Types: %s" % (', '.join(instance_type_list)))
    print("==> Distros: %s" % (', '.join(distro_list)))
    print("==> Schedulers: %s" % (', '.join(scheduler_list)))
    print("==> Parallelism: %d" % (config['parallelism']))
    print("==> Key Pair: %s" % (config['key_name']))

    # Optional params
    if config['key_path']:
        print("==> Key Path: %s" % (config['key_path']))
    if config['custom_cookbook_url']:
        print("==> Custom cfncluster-cookbook URL: %s" % (config['custom_cookbook_url']))
    if config['custom_node_url']:
        print("==> Custom cfncluster-node URL: %s" % (config['custom_node_url']))
    if config['custom_template_url']:
        print("==> Custom cfncluster template URL: %s" % (config['custom_template_url']))

    # Populate subnet / vpc data for all regions we're going to test.
    for region in region_list:
        client = boto3.client('ec2', region_name=region)
        response = client.describe_tags(Filters=[{'Name': 'key',
                                                  'Values': [ 'CfnClusterTestSubnet' ]}],
                                        MaxResults=16)
        if len(response['Tags']) == 0:
            print('Could not find subnet in %s with CfnClusterTestSubnet tag.  Aborting.' %
                  (region))
            exit(1)
        subnetid = response['Tags'][0]['ResourceId']

        response = client.describe_subnets(SubnetIds = [ subnetid ])
        if len(response) == 0:
            print('Could not find subnet info for %s' % (subnetid))
            exit(1)
        vpcid = response['Subnets'][0]['VpcId']

        setup[region] = { 'vpc' : vpcid, 'subnet' : subnetid }

    work_queues = {}
    # build up a per-region list of work to do
    for region in region_list:
        work_queues[region] = Queue.Queue()
        for distro in distro_list:
            for scheduler in scheduler_list:
                for instance in instance_type_list:
                    work_item = {'distro': distro, 'scheduler': scheduler, 'instance_type': instance}
                    work_queues[region].put(work_item)

    # start all the workers
    for region in region_list:
        for i in range(0, config['parallelism']):
            t = threading.Thread(target=test_runner,
                                 args=(region, work_queues[region], config['key_name'], config))
            t.daemon = True
            t.start()


    # Wait for all the work queues to be completed in each region
    # WARN: The work_queues[region].join() approach prevents the SIGINT signal to be caught from the main thread,
    #       that is actually blocked in the join.
    all_finished = False
    self_killed = False
    while not all_finished:
        time.sleep(1)
        all_finished = True
        for queue in work_queues.values():
            all_finished = all_finished and queue.unfinished_tasks == 0
        # In the case parent process was SIGKILL-ed
        if not _proc_alive(parent) and not self_killed:
            print("Parent process with pid %s died - terminating..." % parent)
            _killme_gently()
            self_killed = True

    print("%s - Regions workers queues all done: %s" % (_time(), all_finished))

    # print status...
    print("==> Success: %d" % (success))
    print("==> Failure: %d" % (failure))
    if failure != 0:
        exit(1)

if __name__ == '__main__':
    _child = os.fork()

    if _child == 0:
        _main_child()
    else:
        _main_parent()
