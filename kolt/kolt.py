# https://support.ultimum.io/support/solutions/articles/1000125460-python-novaclient-neutronclient-glanceclient-swiftclient-heatclient
# http://docs.openstack.org/developer/python-novaclient/ref/v2/servers.html
import argparse
import asyncio
import os
import uuid
import textwrap
import sys

import yaml

from configparser import ConfigParser

from novaclient import client as nvclient
from novaclient.exceptions import (NotFound as NovaNotFound,
                                   ClientException as NovaClientException)
from cinderclient import client as cclient
from neutronclient.v2_0 import client as ntclient

from keystoneauth1 import identity
from keystoneauth1 import session

from .hue import red, info, que, lightcyan as cyan
from ._init import CloudInit


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


def distribute_hosts(hosts_zones):
    """
    Given  [(['host1', 'host2', 'host3'], 'A'), (['host4', 'host5'], 'B')]
    return:
    [(host1, zone),
     (host2, zone),
     (host3, zone),
     (host4, zone),
     (host5, zone)]
    """
    for item in hosts_zones:
        hosts, zone = item[0], item[1]
        for host in hosts:
            yield (host, zone)


def get_host_zones(hosts, zones):
    # brain fuck warning
    # this divides the lists of hosts into zones
    # >>> hosts
    # >>> ['host1', 'host2', 'host3', 'host4', 'host5']
    # >>> zones
    # >>> ['A', 'B']
    # >>> list(zip([hosts[i:i + n] for i in range(0, len(hosts), n)], zones)) # noqa
    # >>> [(['host1', 'host2', 'host3'], 'A'), (['host4', 'host5'], 'B')]  # noqa
    if len(zones) == len(hosts):
        return list(zip(hosts, zones))
    else:
        end = len(zones) + 1 if len(zones) % 2 else len(zones)
        host_zones = list(zip([hosts[i:i + end] for i in
                               range(0, len(hosts), end)],
                              zones))
        return distribute_hosts(host_zones)


# ugly global variables!
# don't do this to much
# only tolerated here because we don't define any classes for the sake of
# readablitiy. this will be refactored in v0.2

nova, cinder, neutron = None, None, None


async def create_instance_with_volume(name, zone, flavor, image, nics,
                                      keypair, secgroups, role, userdata,
                                      hosts):

    global nova, neutron, cinder

    try:
        print(que("Checking if %s does not already exist" % name))
        server = nova.servers.find(name=name)
        ip = server.interface_list()[0].fixed_ips[0]['ip_address']
        print(info("This machine already exists ... skipping"))
        hosts[name] = (ip, role)
        return
    except NovaNotFound:
        print(info("Okay, launching %s" % name))

    bdm_v2 = {
        "boot_index": 0,
        "source_type": "volume",
        "volume_size": "25",
        "destination_type": "volume",
        "delete_on_termination": True}

    try:
        v = cinder.volumes.create(12, name=uuid.uuid4(), imageRef=image.id,
                                  availability_zone=zone)

        while v.status != 'available':
            await asyncio.sleep(1)
            v = cinder.volumes.get(v.id)

        v.update(bootable=True)
        # wait for mark as bootable
        await asyncio.sleep(2)

        bdm_v2["uuid"] = v.id
        print("Creating instance %s... " % name)
        instance = nova.servers.create(name=name,
                                       availability_zone=zone,
                                       image=None,
                                       key_name=keypair.name,
                                       flavor=flavor,
                                       nics=nics, security_groups=secgroups,
                                       block_device_mapping_v2=[bdm_v2],
                                       userdata=userdata,
                                       )

    except NovaClientException:
        print(info(red("Something weired happend, I so I didn't create %s" %
                       name)))
    except KeyboardInterrupt:
        print(info(red("Oky doky, stopping as you interrupted me ...")))
        print(info(red("Cleaning after myself")))
        v.delete

    inst_status = instance.status
    print("waiting for 10 seconds for the machine to be launched ... ")
    await asyncio.sleep(10)

    while inst_status == 'BUILD':
        print("Instance: " + instance.name + " is in " + inst_status +
              " state, sleeping for 5 seconds more...")
        await asyncio.sleep(5)
        instance = nova.servers.get(instance.id)
        inst_status = instance.status

    print("Instance: " + instance.name + " is in " + inst_status + "state")

    ip = instance.interface_list()[0].fixed_ips[0]['ip_address']
    print("Instance booted! Name: " + instance.name + " Status: " +
          instance.status + ", IP: " + ip)

    hosts[name] = (ip, role)


def read_os_auth_variables():
    """
    Automagically read all OS_* variables and
    yield key: value pairs which can be used for
    OS connection
    """
    d = {}
    for k, v in os.environ.items():
        if k.startswith("OS_"):
            d[k[3:].lower()] = v

    not_in_default_rc = ('interface', 'region_name', 'identity_api_version')
    [d.pop(i) for i in not_in_default_rc if i in d]

    return d


def get_clients():
    try:
        auth = identity.Password(**read_os_auth_variables())
        sess = session.Session(auth=auth)
        nova = nvclient.Client('2.1', session=sess)
        neutron = ntclient.Client(session=sess)
        cinder = cclient.Client('3.0', session=sess)
    except TypeError:
        print(red("Did you source your OS rc file in v3?"))
        print(red("If your file has the key OS_ENDPOINT_TYPE it's the wrong one!"))
        sys.exit(1)
    except KeyError:
        print(red("Did you source your OS rc file?"))
        sys.exit(1)

    return nova, neutron, cinder


def create_userdata(role, img_name):
    """
    Create multipart userdata for Ubuntu
    """
    if 'ubuntu' in img_name.lower():
        userdata = str(CloudInit(role))
    else:
        userdata = """
                   #cloud-config
                   manage_etc_hosts: true
                   runcmd:
                    - swapoff -a
                   """
        userdata = textwrap.dedent(userdata).strip()
    return userdata


def create_machines(nova, neutron, cinder, config):

    print(info(cyan("gathering information from openstack ...")))
    keypair = nova.keypairs.get(config['keypair'])
    image = nova.glance.find_image(config['image'])
    master_flavor = nova.flavors.find(name=config['master_flavor'])
    node_flavor = nova.flavors.find(name=config['node_flavor'])
    secgroup = neutron.find_resource('security_group',
                                     config['security_group'])
    secgroups = [secgroup['id']]

    node_user_data = create_userdata('node', config['image'])
    master_user_data = create_userdata('master', config['image'])

    net = neutron.find_resource("network", config["private_net"])
    nics = [{'net-id': net['id']}]
    print(info(cyan("got my info, now launching machines ...")))

    hosts = {}
    build_args_master = [master_flavor, image, nics, keypair, secgroups,
                         "master", master_user_data, hosts]
    build_args_node = [node_flavor, image, nics, keypair, secgroups, "node",
                       node_user_data, hosts]
    cluster = config['cluster-name']
    masters = ["master-%s-%s" % (i, cluster) for i in
               range(1, config['n-masters'] + 1)]
    nodes = ["node-%s-%s" % (i, cluster) for i in
             range(1, config['n-nodes'] + 1)]

    masters_zones = list(get_host_zones(masters, config['availibity-zones']))
    nodes_zones = list(get_host_zones(nodes, config['availibity-zones']))
    loop = asyncio.get_event_loop()

    tasks = [loop.create_task(create_instance_with_volume(
             name, zone, *build_args_master)) for (name, zone) in
             masters_zones]

    tasks.extend([loop.create_task(create_instance_with_volume(
                  name, zone, *build_args_node)) for (name, zone) in
                  nodes_zones])

    loop.run_until_complete(asyncio.wait(tasks))

    loop.close()

    return create_inventory(hosts, config)


def create_inventory(hosts, config):
    """
    :hosts:
    :config: config dictionary
    """

    cfg = ConfigParser(allow_no_value=True, delimiters=('\t', ' '))

    [cfg.add_section(item) for item in ["all", "kube-master", "kube-node",
                                        "etcd", "k8s-cluster:children"]]
    masters = []
    nodes = []

    for host, (ip, role) in hosts.items():
        cfg.set("all", host, "ansible_ssh_host=%s  ip=%s" % (ip, ip))
        if role == "master":
            cfg.set("kube-master", host)
            masters.append(host)
        if role == "node":
            cfg.set("kube-node", host)
            nodes.append(host)

    # add etcd
    for master in masters:
        cfg.set("etcd", master)

    etcds_missing =  config["n-etcd"] - len(masters)
    for node in nodes[:etcds_missing]:
        cfg.set("etcd", node)

    # add all cluster groups
    cfg.set("k8s-cluster:children", "kube-node")
    cfg.set("k8s-cluster:children", "kube-master")

    return cfg


def main():
    global nova, neutron, cinder
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="YAML configuration")
    parser.add_argument("-i", "--inventory", help="Path to Ansible inventory")

    args = parser.parse_args()

    if not args.config:
        parser.print_help()
        sys.exit(2)

    with open(args.config, 'r') as stream:
        config = yaml.load(stream)
    
    if not (config['n-etcd'] % 2 and config['n-etcd'] > 1):
        print(red("You must have an odd number (>1) of etcd machines!"))
        sys.exit(2)

    nova, neutron, cinder = get_clients()
    cfg = create_machines(nova, neutron, cinder, config)

    if args.inventory:
        with open(args.inventory, 'w') as f:
            cfg.write(f)
    else:
        print(info("Here is your inventory ..."))
        print(red("You can save this inventory to a file with the option -i"))
        cfg.write(sys.stdout)