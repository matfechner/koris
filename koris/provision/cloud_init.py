"""
This modules contains some helper functions to inject cloud-init
to booted machines. At the moment only Cloud Inits for Ubunut 16.04 are
provided
"""
import base64
from datetime import datetime
import os
import sys
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pkg_resources import (Requirement, resource_filename)

import yaml

from cryptography.hazmat.primitives import serialization

from koris import __version__, KUBERNETES_BASE_VERSION
from koris.ssl import b64_cert, b64_key
from koris.util.logger import Logger

LOGGER = Logger(__name__)


BOOTSTRAP_SCRIPTS_DIR = "/koris/provision/userdata/"


def get_audit_policy():
    """read the"""
    if getattr(sys, 'frozen', False):
        path = os.path.join(
            sys._MEIPASS,  # pylint: disable=no-member, protected-access
            'provision/userdata/manifests/audit-policy.yml')
    else:
        path = resource_filename(Requirement('koris'),
                                 os.path.join(BOOTSTRAP_SCRIPTS_DIR,
                                              'manifests',
                                              'audit-policy.yml'))
    with open(path) as policy:
        return policy.read()


class BaseInit:  # pylint: disable=unnecessary-lambda,no-member
    """
    Args:
       cloud_config   An OSCloudConfig instance describing the information
                      necessary for sending requests to the underlying cloud.
                      Needed e.g. for auto scaling.

    Attributes:
        cloud_config_data       this attribute contains the text/cloud-config
                                files that is passed to the instances
        attachments             this attribute contains other parts of the
                                userdata, e.g scripts that are directly
                                executed by cloud-init. They should be
                                instances of MIMEText with the header
                                'Content-Disposition' set to 'attachment'
    """
    def __init__(self, cloud_config):
        self.cloud_config = cloud_config

        self._cloud_config_data = {}
        self._attachments = []

        # if needed we can declare and use other sections at this point...
        self._cloud_config_data['write_files'] = []
        self._cloud_config_data['runcmd'] = []
        self._cloud_config_data['runcmd'].append('swapoff -a')

        # assemble the parts
        self._write_koris_info()

    def write_file(self, path, content, owner="root", group="root",
                   permissions="0600", encoder=lambda x: base64.b64encode(x)):
        """
        writes a file to the instance
        path: e.g. /etc/kubernetes/koris.conf
        content: string of the content of the file
        owner: e.g. root
        group: e.g. root
        permissions: e.g. "0644", as string
        encode: Optional encoder to use for the needed base64 encoding
        """
        data = {
            "path": path,
            "owner": owner + ":" + group,
            "encoding": "b64",
            "permissions": permissions,
            "content": encoder(content.encode()).decode()
        }
        self._cloud_config_data['write_files'].append(data)

    def add_bootstrap_script(self):
        """
        add a bootstrap script to each cluster member.
        """
        name, script = self._get_bootstrap_script()
        part = MIMEText(script, _subtype='x-shellscript')
        part.add_header('Content-Disposition', 'attachment',
                        filename=name)
        self._attachments.append(part)

    def add_ssh_public_key(self, ssh_key):
        """
        ssh_key istance of ``cryptography.hazmat.backends.openssl.rsa._RSAPrivateKey``
        """
        if isinstance(ssh_key, str):
            keyline = ssh_key
        else:
            keyline = ssh_key.public_key().public_bytes(
                serialization.Encoding.OpenSSH,
                serialization.PublicFormat.OpenSSH).decode()

        self._cloud_config_data["ssh_authorized_keys"] = []
        self._cloud_config_data["ssh_authorized_keys"].append(keyline)

    def _write_koris_info(self):
        """
        Generate the koris.conf configuration file.
        """
        content = """
        # This file contains meta information about koris
        koris_version={}
        creation_date={}
        """.format(
            __version__,
            datetime.strftime(datetime.now(), format="%c"))
        content = textwrap.dedent(content)

        self.write_file("/etc/kubernetes/koris.conf", content, "root", "root",
                        "0644")

    def _get_bootstrap_script(self):
        name = "bootstrap-k8s-%s-%s-%s.sh" % (
            self.role, self.os_type, self.os_version)

        if getattr(sys, 'frozen', False):
            path = os.path.join(
                sys._MEIPASS,  # pylint: disable=no-member, protected-access
                'provision/userdata', name)
        else:
            path = resource_filename(Requirement('koris'),
                                     os.path.join(BOOTSTRAP_SCRIPTS_DIR,
                                                  name))
        with open(path) as fh:
            script = fh.read()

        return name, script

    def _write_cloud_config(self):
        """
        write out the cloud provider configuration file for OpenStack
        """
        content = str(self.cloud_config)
        self.write_file("/etc/kubernetes/cloud-config", content, "root",
                        "root", "0600")

    def _write_kubelet_default(self):
        """
        write out flags for kubelet systemd unit
        """
        content = ('''KUBELET_EXTRA_ARGS="--cloud-provider=external''')
        self.write_file("/etc/default/kubelet", content, "root",
                        "root", "0600")

    def __str__(self):
        """
        This method generates a string from the cloud_config_data and the
        attachments that have been set in the corresponding attributes.
        """
        self.add_bootstrap_script()
        userdata = MIMEMultipart()

        # first add the cloud-config-data script
        config = MIMEText(yaml.dump(self._cloud_config_data),
                          _subtype='cloud-config')
        config.add_header('Content-Disposition', 'attachment')
        userdata.attach(config)

        for attachment in self._attachments:
            userdata.attach(attachment)

        return userdata.as_string()


class NthMasterInit(BaseInit):
    """
    Initialization userdata for an n-th master node. Nothing more than
    adding an public SSH key for access from the first master node needs
    to be done.
    """
    def __init__(  # pylint: disable=too-many-arguments
            self,
            cloud_config,
            ssh_key,
            os_type='ubuntu',
            os_version="16.04",
            dex=None,
            koris_env=None,
            k8s_conf=None,):
        """
        ssh_key is a RSA keypair (return value from create_key from util.ssl
            package)

        Args:
            k8s_conf (str) - path the the k8s configuration file
        """
        super().__init__(cloud_config)
        self.ssh_key = ssh_key
        self.os_type = os_type
        self.os_version = os_version
        self.role = 'nth-master'
        self.koris_env = koris_env
        self._write_koris_env(dex)
        if k8s_conf:
            self.write_file("/etc/kubernetes/admin.conf",
                            open(k8s_conf).read())
        # assemble the parts for an n-th master node
        self.add_ssh_public_key(self.ssh_key)

        # If Dex is to be installed, deploy its CA to the master
        if dex is not None:
            path = dex['ca_file']
            cert = dex['cert']
            self.write_file(path, b64_cert(cert),
                            "root", "root", "0600", lambda x: x)

        # write audit policy
        self.write_file("/etc/kubernetes/audit-policy.yml", get_audit_policy())

    def _write_koris_env(self, dex=None):
        """
        writes the necessary koris information for the node to the file
        /etc/kubernetes/koris.env
        """

        kenv = self.koris_env
        if not kenv:
            raise ValueError("koris_env dictionary can't be empty")

        def gk(key, dic=kenv, default=""):  # pylint: disable=invalid-name
            """Retrieves a key from a dictionary with a default."""
            val = dic.get(key, default)

            if val is None:
                return default
            return val

        content = f"""
            #!/bin/bash
            export MASTERS_IPS=( {" ".join(gk('master_ips'))} )
            export MASTERS=( {" ".join(gk('master_names'))} )
            export AUTO_JOIN="{gk('auto_join')}"
            export CURRENT_CLUSTER="{gk('current_cluster')}"

            export LOAD_BALANCER_DNS="{gk('lb_dns')}"
            export LOAD_BALANCER_IP="{gk('lb_ip')}"
            export LOAD_BALANCER_PORT="{gk('lb_port')}"

            export BOOTSTRAP_TOKEN="{gk('bootstrap_token')}"

            export POD_SUBNET="{gk('pod_subnet')}"
            export POD_NETWORK="{gk('pod_network')}"

            export KUBE_VERSION="{gk('k8s_version')}"
        """
        content = textwrap.dedent(content)

        # For Dex we need to start the apiserver with special args, such as
        # the location of the Dex CA certificate in order to verify incoming
        # tokens
        if dex is not None:
            dex_content = """
                export OIDC_ISSUER_URL="https://{}:{}"
                export OIDC_CLIENT_ID="{}"
                export OIDC_CA_FILE="{}"
                export OIDC_USERNAME_CLAIM="{}"
                export OIDC_GROUPS_CLAIM="{}"
            """.format(dex['issuer'], dex['ports']['listener'],
                       dex['client']['id'],
                       dex['ca_file'],
                       dex['username_claim'],
                       dex['groups_claim'])
            dex_content = textwrap.dedent(dex_content)
            content += dex_content
        content = textwrap.dedent(content)

        self.write_file("/etc/kubernetes/koris.env", content, "root", "root",
                        "0600")


# pylint: disable=too-many-arguments,too-many-locals
class FirstMasterInit(NthMasterInit):
    """
    This node executes the bootstrap strip to create the initial cluster.

    Args:
        ssh_key (RSAkeypair) - an RSA keypair instance from
                :func:`~koris.ssl.create_key`
        ca_bundle: The CA bundle for the CA that is used to permit accesses
            to the API server.
        cloud_config: An OSCloudConfig instance describing the information
            necessary for sending requests to the underlying cloud.
        os_type (str): the OS type the bootstrap script runs on
        os_version (str): OS version the bootstrap script runs on
        dex (dict): A dictionary containg information for Dex
        koris_env (dict): A dictionary containing information for the
            koris.env

    """

    def __init__(self, ssh_key, ca_bundle, cloud_config,
                 os_type='ubuntu', os_version="16.04", dex=None,
                 koris_env=None):
        super().__init__(cloud_config, ssh_key, os_type, os_version,
                         dex=dex, koris_env=koris_env)
        self.ca_bundle = ca_bundle
        self.role = 'master'

        # assemble the parts for the first master
        # use an encoder that just returns x, since b64_cert encodes already
        # in base64 mode
        self.write_file("/etc/kubernetes/pki/ca.crt", b64_cert(ca_bundle.cert),
                        "root", "root", "0600", lambda x: x)
        self.write_file("/etc/kubernetes/pki/ca.key", b64_key(ca_bundle.key),
                        "root", "root", "0600", lambda x: x)

        self._write_cloud_config()
        self._write_ssh_private_key()

    def _write_ssh_private_key(self):
        # path = "/home/{}/.ssh/id_rsa_masters".format(self.username)
        key = self.ssh_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()).decode()

        self._cloud_config_data["ssh_keys"] = {}
        self._cloud_config_data["ssh_keys"]["rsa_private"] = key


class NodeInit(BaseInit):
    """
    The node does nothing else than executing its bootstrap script.
    """
    def __init__(self, ca_cert, cloud_config, lb_ip, lb_port, bootstrap_token,
                 discovery_hash, lb_dns='', os_type='ubuntu',
                 os_version="16.04",
                 k8s_version=KUBERNETES_BASE_VERSION,
                 pod_network="CALICO"):
        """
        """
        super().__init__(cloud_config)
        self.ca_cert = ca_cert
        self.lb_ip = lb_ip
        self.lb_port = lb_port
        self.bootstrap_token = bootstrap_token
        self.discovery_hash = discovery_hash
        self.lb_dns = lb_dns
        self.os_type = os_type
        self.os_version = os_version
        self.role = "node"
        self.k8s_version = k8s_version
        self.pod_network = pod_network

        # assemble parts for the node
        self._write_koris_env()
        self._write_kubelet_default()
        self._write_cloud_config()

    def _write_koris_env(self):
        """
        writes the necessary koris information for the node to the file
        /etc/kubernetes/koris.env
        """
        content = """
            #!/bin/bash
            export B64_CA_CONTENT="{}"
            export LOAD_BALANCER_DNS="{}"
            export LOAD_BALANCER_IP="{}"
            export LOAD_BALANCER_PORT="{}"
            export BOOTSTRAP_TOKEN="{}"
            export DISCOVERY_HASH="{}"
            export KUBE_VERSION="{}"
            export POD_NETWORK="{}"
        """.format(b64_cert(self.ca_cert), self.lb_dns, self.lb_ip,
                   self.lb_port, self.bootstrap_token, self.discovery_hash,
                   self.k8s_version,
                   self.pod_network)
        content = textwrap.dedent(content)
        self.write_file("/etc/kubernetes/koris.env", content, "root", "root",
                        "0600")
