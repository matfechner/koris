"""
cli.py
======

misc functions to interact with the cluster, usually called from
``koris.koris.Kolt``.

Don't use directly
"""
import sys

from koris.util.hue import red, yellow  # pylint: disable=no-name-in-module
from koris.cloud.openstack import remove_cluster
from .util.util import get_kubeconfig_yaml, get_logger

LOGGER = get_logger(__name__)


def delete_cluster(config, nova, neutron, cinder, force=False):
    """
    completly delete a cluster from openstack.

    This function removes all compute instance, volume, loadbalancer,
    security groups rules and security groups
    """
    if not force:
        print(red("Are you really sure ? [y/N]"))
        ans = input(red("ARE YOU REALLY SURE???"))
    else:
        ans = 'y'

    if ans.lower() == 'y':
        remove_cluster(config, nova, neutron, cinder)
    else:
        sys.exit(1)


def write_kubeconfig(cluster_name, lb_ip, lb_port, cert_dir, ca_cert_name,
                     client_cert_name, client_key_name):
    """
    Write a kubeconfig file to the filesystem
    """
    path = None
    master_uri = "https://"+lb_ip+":"+lb_port
    ca_cert = cert_dir+"/"+ca_cert_name
    username = "admin"
    client_cert = cert_dir+"/"+client_cert_name
    client_key = cert_dir+"/"+client_key_name

    kubeconfig = get_kubeconfig_yaml(master_uri, ca_cert, username,
                                     client_cert, client_key)

    path = '-'.join((cluster_name, 'admin.conf'))
    LOGGER.info(yellow("You can use your config with:"))
    LOGGER.info(yellow("kubectl get nodes --kubeconfig=%s" % path))
    with open(path, "w") as fh:
        fh.write(kubeconfig)

    return path
