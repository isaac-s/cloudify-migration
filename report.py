import argparse
import json
import os
import urllib
import uuid
import sys
import tempfile
import threading
from cloudify_cli.utils import get_rest_client

from distutils import spawn
from subprocess import call


_DIRECTORY = os.path.dirname(os.path.realpath(__file__))
_TENNANTS = "http://git.cloud.td.com/its-cloud/management-cluster/raw/master/bootstrap/tenants.json"
_USER = 'cloudify'
_MANAGER_KEY = '~/td/ga-cloudify-manager-kp.pem'


def _get_agents_resource(resource):
    return os.path.join(_DIRECTORY, 'agents', resource)


class Command(object):

    def prepare_parser(self, subparsers):
        subparser = subparsers.add_parser(self.name)
        self.prepare_args(subparser)
        subparser.set_defaults(func=self.perform)

    def prepare_args(self, parser):
        pass


def _get_deployment_states(client, deployments):
    res = {}
    agents_count = 0
    for deployment in deployments:
        print 'Deployment {}'.format(deployment.id)
        dep_states = set()
        dep_agents = {}
        for node in client.nodes.list(deployment_id=deployment.id):
            for node_instance in client.node_instances.list(deployment_id=deployment.id,
                                                            node_name=node.id):
                dep_states.add(node_instance.state)
                if 'cloudify.nodes.Compute' in node.type_hierarchy:
                    dep_agents[node_instance.id] = {
                        'state': node_instance.state,
                        'ip': node_instance.runtime_properties.get('ip', node.properties.get('ip', '')),
                        'cloudify_agent': node.properties.get('cloudify_agent', {}),
                        'is_windows': 'cloudify.openstack.nodes.WindowsServer' in node.type_hierarchy
                    }
 
        if len(dep_states) > 1:
            status = 'mixed'
        elif len(dep_states) == 1:
            status = next(iter(dep_states))
        else:
            status = 'empty'
        agents_count += len(dep_agents)
        res[deployment.id] = {
            'status': status,
            'agents': dep_agents,
            'ok': status in ['empty', 'started'],
            'states': list(dep_states)}
    return res, agents_count


def _has_multi_sec_nodes(blueprint):
    types = {}
    for node in blueprint.plan['nodes']:
        types[node['name']] = node['type_hierarchy']
    for node in blueprint.plan['nodes']:
        name = node['name']
        if 'cloudify.nodes.Compute' in types[name]:
            connected_sec_groups = []
            for relationship in node['relationships']:
                target = relationship['target_id']
                if 'cloudify.nodes.SecurityGroup' in types[target]:
                    connected_sec_groups.append(target)
            if len(connected_sec_groups) > 1:
                return True
    return False

def insert_blueprint_report(res, client, blueprint, deployments, config):
    res['multi_sec_nodes'] = _has_multi_sec_nodes(blueprint)
    deployments = [dep for dep in deployments if dep.blueprint_id == blueprint.id]
    res['deployments_count'] = len(deployments)
    if config.deployment_states:
        res['deployments'], res['agents_count'] = _get_deployment_states(client, deployments)
 

def _get_blueprints(client, blueprints, deployments, config):
    threads = []
    res = {}
    for blueprint in blueprints:
        res[blueprint.id] = {}
        thread = threading.Thread(target=insert_blueprint_report,
                                  args=(res[blueprint.id], client, blueprint, deployments, config))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    agents_count = 0
    for name, blueprint_res in res.iteritems():
        agents_count = agents_count + blueprint_res.get('agents_count', 0)
    return res, agents_count

class RemoteFile():

    def __init__(self, handler, local_path, target_path):
        self.handler = handler
        self.local_path = local_path
        self.target_path = target_path
    def __enter__(self):
        self.handler.inject_file(self.local_path, self.target_path)
    def __exit__(self, type, value, traceback):
        self.handler.remove_file(self.target_path)


class ManagerHandler(object):

    def __init__(self, ip):
        self.manager_ip = ip

    def scp(self, local_path, path_on_manager, to_manager):
        scp_path = spawn.find_executable('scp')
        management_path = '{0}@{1}:{2}'.format(
            _USER,
            self.manager_ip,
            path_on_manager
        )
        command = [scp_path, '-i', os.path.expanduser(_MANAGER_KEY)]
        if to_manager:
            command += [local_path, management_path]
        else:
            command += [management_path, local_path]
        if call(command):
            raise RuntimeError('Could not scp to/from manager')
    def manager_file(self, local, target):
        return RemoteFile(self, local, target)
    def put_resource(self, source, resource):
        tmp_file = '/tmp/_resource_file'
        self.send_file(source, tmp_file)
        self.execute('sudo cp {0} /opt/manager/resources/{1}'.format(
            tmp_file, resource))

    def send_file(self, source, target):
        self.scp(source, target, True)

    def load_file(self, source, target):
        self.scp(target, source, False)

    def execute(self, cmd, timeout=None):
        ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-i',
            os.path.expanduser(_MANAGER_KEY), '{0}@{1}'.format(
            _USER, self.manager_ip), '-C', cmd]
        if timeout:
            cmd_list = ["timeout", str(timeout)]
            cmd_list.extend(ssh_cmd)
        else:
            cmd_list = ssh_cmd
        result = call(cmd_list)
        if result:
            raise RuntimeError('Could not execute remote command "{0}"'.format(cmd))


class ManagerHandler31(ManagerHandler):

    def inject_file(self, source, target):
        self.send_file(source, target)

    def retrieve_file(self, source, target):
        self.load_file(source, target)


    def remove_file(self, path):
        self.execute('rm {0}'.format(path))

    def python_call(self, cmd):
        self.execute('/opt/celery/cloudify.management__worker/env/bin/python {0}'.format(cmd))


class ManagerHandler32(ManagerHandler):
    
    def inject_file(self, source, target):
        temporary_file = '_tmp_file{0}'.format(uuid.uuid4())
        self.send_file(source, '~/{0}'.format(temporary_file))
        self.docker_execute('mv /tmp/home/{0} {1}'.format(temporary_file, target))

    def retrieve_file(self, source, target):
        temporary_file = '_tmp_file{0}'.format(uuid.uuid4())
        self.docker_execute('cp {0} /tmp/home/{1}'.format(source, temporary_file))
        self.load_file('~/{0}'.format(temporary_file), target)

    def docker_execute(self, cmd):
        self.execute('sudo docker exec cfy {0}'.format(cmd))

    def remove_file(self, path):
        self.docker_execute('rm {0}'.format(path))

    def python_call(self, cmd):
        self.docker_execute('/etc/service/celeryd-cloudify-management/env/bin/python {0}'.format(cmd))



def _get_handler(version, ip):
    if version.startswith('3.1'):
        return ManagerHandler31(ip)
    else:
        return ManagerHandler32(ip)

def _random_tmp_path():
    return '/tmp/_tmp_migration_report_file{0}'.format(uuid.uuid4())

def prepare_report(result, env, config):
    ip = env['config']['MANAGER_IP_ADDRESS']
    result['ip'] = ip
    if env.get('inactive'):
        result['inactive'] = True
        return
    status = call(['timeout', '2', 'wget', ip, '-o', '/tmp/index.html'])
    if status:
        result['msg'] = 'Cant connect to manager'
        return
    client = get_rest_client(manager_ip=ip)
    result['version'] = client.manager.get_version()['version']
    if config.test_manager_ssh:
        handler = _get_handler(result['version'], ip)
        try:
            handler.execute('echo test > /dev/null', timeout=4)
            tmp_file = _random_tmp_path()
            result_file = _random_tmp_path()
            content = str(uuid.uuid4())
            with handler.manager_file(_get_agents_resource('validate_manager_env.py'), tmp_file):
                handler.python_call('{0} {1} {2}'.format(tmp_file, result_file, content))
                _, path = tempfile.mkstemp()
                handler.retrieve_file(result_file, path)
                handler.remove_file(result_file)
                with open(path) as f:
                    res = f.read()
                    if res != content:
                        raise RuntimeError(
                            'Invalid result retrieved, expected {0}, got {1}'.format(
                                content, res))
            result['manager_ssh'] = True
        except Exception as e:
            result['manager_ssh'] = False
            result['manager_ssh_error'] = str(e)
    if config.blueprints_states:
        deployments = client.deployments.list()
        if config.blueprint:
            blueprints = [client.blueprints.get(blueprint_id=config.blueprint)]
        else:
            blueprints = client.blueprints.list()
        result['blueprints'], result['agents_count'] = _get_blueprints(client, blueprints, deployments, config)
        result['deployments_count'] = len(deployments)
        result['blueprints_count'] = len(blueprints)
    return result 


def insert_env_report(env_result, env, config):
    try:
        prepare_report(env_result, env, config)
    except Exception as e:
        env_result['error'] = 'Could not create report, cause: {0}'.format(str(e))


def _output(config, res):
    if config.output:
        with open(config.output, 'w') as out:
            out.write(json.dumps(res, indent=2))
    else:
        print json.dumps(res, indent=2)
 

class Generate(Command):
    
    @property
    def name(self):
        return 'generate'

    def prepare_args(self, parser):
        parser.add_argument('--manager')
        parser.add_argument('--env')
        parser.add_argument('--output')
        parser.add_argument('--deployment')
        parser.add_argument('--deployment-states', default=False, action='store_true')
        parser.add_argument('--blueprints-states', default=False, action='store_true')
        parser.add_argument('--blueprint')
        parser.add_argument('--test-manager-ssh', default=False, action='store_true')
        parser.add_argument('--test-agents-alive', default=False, action='store_true')

    def perform(self, config):
        tennants, _ = urllib.urlretrieve(_TENNANTS)
        with open(tennants) as f:
            managers = json.loads(f.read()) 
        if config.manager:
            managers = {
                config.manager: managers[config.manager]
            }
        if config.env:
            new_managers = {}
            for name, manager in managers.iteritems():
                envs = {}
                for env_name, env in manager['environments'].iteritems():
                    if env_name == config.env:
                        envs[env_name] = env
                if envs:
                    manager['environments'] = envs
                    new_managers[name] = manager
            managers = new_managers

        result = {}
        threads = []
        for mgr_name, manager in managers.iteritems():
            print 'Manager {0}'.format(mgr_name)
            mgr_result = {}
            for env_name, env in manager['environments'].iteritems():
                env_result = {}
                thread = threading.Thread(target=insert_env_report,
                                          args=(env_result, env, config))
                thread.start()
                threads.append(thread)
                mgr_result[env_name] = env_result
            result[mgr_name] = mgr_result
        for thread in threads:
            thread.join()
        res = {}
        res['managers'] = result
        for key in ['agents_count', 'deployments_count', 'blueprints_count']:
            val = 0
            for manager in res['managers'].itervalues():
                for env in manager.itervalues():
                    val = val + env.get(key, 0)
            res[key] = val
        _output(config, res)


_COMMANDS = [
    Generate
]



def _parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    for cmd_cls in _COMMANDS:
        cmd = cmd_cls()
        cmd.prepare_parser(subparsers)
    return parser


def main(args):
    parser = _parser()
    config = parser.parse_args(args)
    config.func(config)


if __name__ == '__main__':
    main(sys.argv[1:])
