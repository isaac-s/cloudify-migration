import sys
import time

from cloudify.plugins.workflows import _host_post_start,\
    _host_pre_stop, _is_host_node
from cloudify.decorators import workflow


ctx = {
    'blueprint_id': sys.argv[1],
    'deployment_id': sys.argv[2],
    'execution_id': sys.argv[3],
    'workflow_id': sys.argv[4]
}


@workflow
def replace_host_software(ctx, **kwargs):
    graph = ctx.graph_mode()
    for node in ctx.nodes:
        for instance in node.instances:
            if _is_host_node(instance):
                if sys.argv[4] == "migration_uninstall":
                    generate_tasks_fun = _host_pre_stop
                else:
                    def generate_tasks_fun(instance):
                        return _host_post_start(instance) + [
                            instance.execute_operation(
                                'cloudify.interfaces.monitoring.start')
                        ]
                sequence = graph.sequence()
                sequence.add(*generate_tasks_fun(instance))
    time.sleep(2)
    graph.execute()

replace_host_software(__cloudify_context=ctx)
