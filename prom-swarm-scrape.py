#!/usr/bin/env python3

import docker
import http.server
import argparse
import socketserver
import os
from datetime import datetime, timezone

class MetricsHandler(http.server.BaseHTTPRequestHandler):
  def check_if_leader(self):
    try:
      swarm_state = self.server.client.info().get('Swarm', {}).get('LocalNodeState')

      # Make sure the node is stable
      if swarm_state != 'active':
        return False

      my_node_id = self.server.client.info().get('Swarm', {}).get('NodeID')

      myself = self.server.client.nodes.get(my_node_id)
      am_i_leader = myself.attrs.get('ManagerStatus', {}).get('Leader', False)
    except:
      am_i_leader = False

    return am_i_leader

  def nodes_metrics(self, metrics_dict, prometheus_metrics):
    metric_labels = {}
    value = 0

    nodes = self.server.client.nodes.list()
    for node in nodes:
      metric_labels['name']=node.attrs['Description']['Hostname']
      metric_labels['role']=node.attrs['Spec']['Role']
      metric_labels['addr']=node.attrs['Status']['Addr']
      metric_labels['version']=node.attrs['Description']['Engine']['EngineVersion']
      metric_labels['status']=node.attrs['Status']['State']

      value = 0 if metric_labels['status'].lower() != 'ready' else 1

      labels_str = '{' + ', '.join([f'{k}="{metric_labels[k]}"' for k in metric_labels]) + '}'
      prometheus_metrics.append('swarm_nodes_status' + labels_str + ' ' + str(value))

      metrics_dict['swarm_nodes_total'] += 1

  def services_metrics(self, metrics_dict, prometheus_metrics):
    metric_labels = {}
    stacks_dict = {}
    desired = 0

    STABLE = 0
    STOPPED = 1
    UNSTABLE = 2
    TERMINATED = 3

    status = STABLE
    services = self.server.client.services.list()

    for service in services:
      metric_labels['service_name']=service.attrs['Spec']['Name']
      stack = 'None'
      try:
        stack = service.attrs['Spec']['Labels']['com.docker.stack.namespace']
        stacks_dict[stack] = stacks_dict.get(stack, 0) + 1

        #print( (stack, stacks_dict[stack]))
      except:
        stack = 'None'

      metric_labels['stack']=stack

      if service.attrs['Spec']['Mode'].get('Global', 'Replicated') == 'Replicated':
        replicas = int(service.attrs['Spec']['Mode']['Replicated']['Replicas'])
      else:
        # Global : replicas is the number of nodes.
        replicas = metrics_dict['swarm_nodes_total']

      desired = 0
      tasks = service.tasks()
      tasks_status_complete = 0
      if len(tasks) == 0:
        desired = 0
      else:
        for task in tasks:
          if task['DesiredState'] == 'running':
            desired += 1
          if task['Status']['State'] == 'complete':
            tasks_status_complete += 1

      if replicas == desired and replicas == 0:
        status = STOPPED
      elif replicas == desired:
        status = STABLE
      elif replicas != desired and tasks_status_complete == replicas:
        status = TERMINATED
      else:
        status = UNSTABLE
        metrics_dict['swarm_services_unstable_total'] += 1

      #print((metric_labels['name'], replicas, desired, status))

      labels_str = '{' + ', '.join([f'{k}="{metric_labels[k]}"' for k in metric_labels]) + '}'
      prometheus_metrics.append('swarm_services_status' + labels_str + ' ' + str(status))
      metrics_dict['swarm_services_total'] += 1

    metrics_dict['swarm_stacks_total'] = len(stacks_dict)

  def containers_metrics(self, metrics_dict, prometheus_metrics):
    metric_labels = {}

    containers = self.server.client.containers.list(all=True)
    for container in containers:
      uptime_seconds = 0
      if container.status == 'running':
        started_at = container.attrs['State']['StartedAt']
        start_time = datetime.fromisoformat(started_at.split('.')[0] + 'Z').replace(tzinfo=timezone.utc)
        uptime = datetime.now(timezone.utc) - start_time
        uptime_seconds = str(uptime.total_seconds()).split('.')[0]
        metric_labels['state']='running'
        metrics_dict['swarm_containers_running_total'] += 1
      elif container.status == 'exited':
        exited_at = container.attrs['State']['FinishedAt']
        exited_time = datetime.fromisoformat(exited_at.split('.')[0] + 'Z').replace(tzinfo=timezone.utc)
        uptime = datetime.now(timezone.utc) - exited_time
        uptime_seconds = str(uptime.total_seconds()).split('.')[0]
        metric_labels['state']='exited'
        metrics_dict['swarm_containers_exited_total'] += 1
      if 'com.docker.stack.namespace' in container.labels:
        stack = container.labels['com.docker.stack.namespace']
        service_name = container.labels['com.docker.swarm.service.name']
      else:
        stack = 'None'
        service_name = 'None'

      metric_labels['stack']=stack
      metric_labels['service_name']=service_name
      metric_labels['name']=container.name
      metric_labels['short_name']='.'.join(container.name.split('.')[:2])

      labels_str = '{' + ', '.join([f'{k}="{metric_labels[k]}"' for k in metric_labels]) + '}'
      prometheus_metrics.append('swarm_containers_status' + labels_str + ' ' + str(uptime_seconds))

  def do_GET(self):
    prometheus_metrics = []
    metrics_dict = {}
    am_i_leader = False

    metrics_dict['swarm_stacks_total'] = 0
    metrics_dict['swarm_nodes_total'] = 0
    metrics_dict['swarm_services_total'] = 0
    metrics_dict['swarm_services_unstable_total'] = 0
    metrics_dict['swarm_configs_total'] = 0
    metrics_dict['swarm_secrets_total'] = 0
    metrics_dict['swarm_containers_running_total'] = 0
    metrics_dict['swarm_containers_exited_total'] = 0

    try:
      am_i_leader = self.check_if_leader()

      if am_i_leader == True:
        # Configs
        metrics_dict['swarm_configs_total'] = len(self.server.client.configs.list())

        # Secret
        metrics_dict['swarm_secrets_total'] = len(self.server.client.secrets.list())

        # Nodes
        self.nodes_metrics(metrics_dict, prometheus_metrics)

        # Services
        self.services_metrics(metrics_dict, prometheus_metrics)

      self.containers_metrics(metrics_dict, prometheus_metrics)
    except docker.errors.DockerException as e:
      print(f"Error connecting to Docker socket: {e}")

    for k in metrics_dict:
      v = metrics_dict[k]
      # Leader shows ALL metrics
      if am_i_leader == True:
        prometheus_metrics.append(f'{k} {v}')
      # Workers only shows containers metrics (Change this!)
      elif 'swarm_containers_' in k:
        prometheus_metrics.append(f'{k} {v}')

    # Join all metrics
    metrics_output = "\n".join(prometheus_metrics) + "\n"

    # Send response.
    self.send_response(200)
    self.send_header('Content-type', 'text/plain; version=0.0.4')
    self.send_header('Access-Control-Allow-Origin', '*')
    self.end_headers()
    self.wfile.write(metrics_output.encode('utf-8'))

def main():
  PORT = None

  parser = argparse.ArgumentParser(description='Use: --leader or --port')
  parser.add_argument('--port',
                        type=int,
                        default=9595,
                        help='Change the default port (default: 9595)')

  args = parser.parse_args()

  PORT = args.port

  try:
    client = docker.DockerClient(base_url='unix://var/run/docker.sock')
  except docker.errors.DockerException as e:
    print(f"Error connecting to Docker socket: {e}")
    raise

  with socketserver.TCPServer(("", PORT), MetricsHandler) as httpd:
    print(f"Metrics server running on port {PORT}")
    print(f"Access http://localhost:{PORT}/ Main page")
    print(f"Access http://localhost:{PORT}/metrics Prometheus metrics")
    print(f"Access http://localhost:{PORT}/health Health check")
    try:
      httpd.client = client
      httpd.serve_forever()
    except KeyboardInterrupt:
      print("\nStopping server ...")
    finally:
      httpd.server_close()

if __name__ == "__main__":
  main()
